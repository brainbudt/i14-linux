#!/usr/bin/env python3
"""Extract pinned own-stock Image/DTB offline. This is not a loader or hardware GO."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
BACKUP = "backups/20261030T011515Z-native-serial"
PINS = {"boot": "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492",
        "recovery": "362e322972f2e8a66dc3319c1959a45a86a3a00a467fc01c52502fb3854b4c6a"}
IMAGE_HASH = "71b449fdf2b61207a0c48ea2e9bb977ae6f704ffe44a63edb2e97e1fe438b4e9"
DTB_HASH = "848b87843320cd09c76623444afb9fd383ff380eccf6dd129f2e59338adb1afb"
IMAGE_BYTES, DTB_BYTES, ENTRY, RUNTIME = 16446424, 73677, 0x40080000, 0x1286000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def overlaps(a, b):
    return a[0] < a[1] and b[0] < b[1] and a[0] < b[1] and b[0] < a[1]


def parse_boot(data, kind):
    require(kind in PINS, "unknown stock source")
    require(len(data) == 16777216 and data[:8] == b"ANDROID!", "Android v0 header/partition size mismatch")
    fields = struct.unpack_from("<10I", data, 8)
    require(fields == (7013326, ENTRY, {"boot": 1897233, "recovery": 2909835}[kind],
                       0x44000000, 0, 0x40f00000, 0x4e000000, 2048, 0, 0),
            "Android header/layout mismatch")
    ramdisk_offset = 2048 + ((fields[0] + 2047) // 2048) * 2048
    require(ramdisk_offset + fields[2] <= len(data), "Android payload outside partition")
    # Own stock has no 512-byte MTK component wrapper: gzip starts at the Android kernel offset.
    payload = data[2048:2048 + fields[0]]
    require(payload[:3] == b"\x1f\x8b\x08", "unexpected MTK wrapper or gzip header")
    return payload


def validate_image(image):
    require(len(image) == IMAGE_BYTES and image[56:60] == b"ARMd", "ARM64 Image header/size mismatch")
    require(struct.unpack_from("<QQQ", image, 8) == (0x80000, RUNTIME, 0), "ARM64 offset/runtime/flags mismatch")
    require(sha(image) == IMAGE_HASH, "Image hash mismatch")
    return {"bytes": len(image), "sha256": sha(image), "text_offset": 0x80000,
            "runtime_bytes": RUNTIME, "flags": 0, "entry": ENTRY,
            "runtime_end_exclusive": ENTRY + RUNTIME}


def parse_dtb(data):
    require(len(data) >= 40, "DTB truncated header")
    magic, total, off, strings, reserve, version, compat, cpu, slen, size = struct.unpack_from(">10I", data)
    require(magic == 0xd00dfeed and total == len(data) and version == 17 and compat == 16,
            "DTB header/total/version mismatch")
    require(off % 4 == 0 and reserve % 8 == 0 and size >= 4 and size % 4 == 0,
            "DTB alignment/structure size mismatch")
    blocks = [(0, 40), (off, off + size), (strings, strings + slen)]
    require(all(0 <= a <= b <= total for a, b in blocks) and reserve >= 40, "DTB block bounds")
    reservations, pos = [], reserve
    while True:
        require(pos + 16 <= total, "DTB unterminated reservation map")
        address, length = struct.unpack_from(">QQ", data, pos); pos += 16
        if address == length == 0:
            break
        require(length > 0 and address + length <= 2**64, "DTB invalid reserved range")
        reservations.append([address, address + length])
    blocks.append((reserve, pos))
    require(not any(overlaps(a, b) for i, a in enumerate(blocks) for b in blocks[i+1:]), "DTB overlapping blocks")
    nodes, stack, pos, ended = {}, [], off, False
    end = off + size
    while pos < end:
        require(pos + 4 <= end, "DTB token bounds")
        token = struct.unpack_from(">I", data, pos)[0]; pos += 4
        if token == 1:
            stop = data.find(b"\0", pos, end)
            require(stop >= pos, "DTB unterminated node")
            try:
                name = data[pos:stop].decode("ascii")
            except UnicodeDecodeError as error:
                raise ValueError("DTB non-ASCII node") from error
            require("/" not in name and (bool(stack) or name == ""), "DTB invalid node name")
            stack.append(name)
            path = "/" + "/".join(stack[1:])
            require(path not in nodes, "DTB duplicate node")
            nodes[path] = {}
            pos = (stop + 4) & ~3
        elif token == 2:
            require(bool(stack), "DTB unbalanced node")
            stack.pop()
        elif token == 3:
            require(bool(stack) and pos + 8 <= end, "DTB property header bounds")
            length, nameoff = struct.unpack_from(">II", data, pos); pos += 8
            require(nameoff < slen and pos + length <= end, "DTB property bounds")
            stop = data.find(b"\0", strings + nameoff, strings + slen)
            require(stop >= strings + nameoff, "DTB property name bounds")
            try:
                name = data[strings + nameoff:stop].decode("ascii")
            except UnicodeDecodeError as error:
                raise ValueError("DTB non-ASCII property") from error
            props = nodes["/" + "/".join(stack[1:])]
            require(name not in props, "DTB duplicate property")
            props[name] = data[pos:pos + length]
            pos = (pos + length + 3) & ~3
        elif token == 4:
            pass
        elif token == 9:
            require(not stack and pos == end and "/" in nodes, "DTB premature/trailing END")
            ended = True
        else:
            raise ValueError("DTB unknown token")
        require(pos <= end, "DTB aligned token bounds")
    require(ended, "DTB missing END")

    def cells(props, key, default):
        raw = props.get(key)
        if raw is None:
            return default
        require(len(raw) == 4, "DTB cell count length")
        count = int.from_bytes(raw, "big")
        require(count in (1, 2), "DTB unsupported cell count")
        return count

    def ranges(raw, address_cells, size_cells):
        width = 4 * (address_cells + size_cells)
        require(bool(raw) and len(raw) % width == 0, "DTB reg range width")
        result = []
        for at in range(0, len(raw), width):
            address = int.from_bytes(raw[at:at + 4*address_cells], "big")
            length = int.from_bytes(raw[at + 4*address_cells:at + width], "big")
            require(length > 0 and address + length <= 2**64, "DTB reg range overflow/empty")
            result.append([address, address + length])
        return result

    ac, sc = cells(nodes["/"], "#address-cells", 2), cells(nodes["/"], "#size-cells", 1)
    memory = [{"path": path, "ranges": ranges(props["reg"], ac, sc)} for path, props in nodes.items()
              if props.get("device_type") == b"memory\0"]
    reserved, parent = [], nodes.get("/reserved-memory", {})
    rac, rsc = cells(parent, "#address-cells", ac), cells(parent, "#size-cells", sc)
    require(parent.get("ranges", b"") == b"", "DTB reserved-memory translation unresolved")
    for path, props in nodes.items():
        if path.startswith("/reserved-memory/"):
            item = {"path": path, "no_map": "no-map" in props}
            if "reg" in props:
                item["ranges"] = ranges(props["reg"], rac, rsc)
            for key in ("size", "alignment", "alloc-ranges"):
                if key in props:
                    if key == "alloc-ranges":
                        item[key] = ranges(props[key], rac, rsc)
                    else:
                        require(len(props[key]) == 4*rsc, "DTB dynamic reservation size width")
                        item[key] = int.from_bytes(props[key], "big")
            reserved.append(item)
    chosen = nodes.get("/chosen", {})
    initrd = {}
    for key, suffix in (("start", "start"), ("end", "end")):
        raw = chosen.get("linux,initrd-" + suffix)
        require(raw is None or len(raw) in (4, 8), "DTB chosen initrd width")
        initrd[key] = None if raw is None else int.from_bytes(raw, "big")
    require((initrd["start"] is None) == (initrd["end"] is None), "DTB incomplete initrd range")
    if initrd["start"] is not None:
        require(initrd["start"] < initrd["end"], "DTB invalid initrd range")
    static = reservations + [r for item in reserved for r in item.get("ranges", [])]
    require(not any(overlaps(a, b) for i, a in enumerate(static) for b in static[i+1:]), "DTB reserved ranges overlap")
    return {"reservation_map": reservations, "reserved_memory": reserved, "memory": memory,
            "chosen_initrd": initrd, "chosen_atag_boot_present": "atag,boot" in chosen,
            "boot_cpuid_phys": cpu}


def parse_payload(payload):
    require(payload[:3] == b"\x1f\x8b\x08", "stock gzip header/MTK wrapper mismatch")
    decoder = zlib.decompressobj(31)
    try:
        image = decoder.decompress(payload, IMAGE_BYTES + 1)
    except zlib.error as error:
        raise ValueError("invalid stock gzip") from error
    require(len(image) <= IMAGE_BYTES, "stock gzip decompressed size limit")
    require(decoder.eof and not decoder.unconsumed_tail, "stock gzip truncated/bounds")
    dtb = decoder.unused_data
    require(len(dtb) == DTB_BYTES, "stock gzip tail/DTB size mismatch")
    image_info = validate_image(image)
    metadata = parse_dtb(dtb)
    require(sha(dtb) == DTB_HASH, "stock DTB hash mismatch")
    return image, dtb, image_info, metadata


def validate_pair(boot, recovery):
    for kind, data in (("boot", boot), ("recovery", recovery)):
        require(sha(data) == PINS[kind], kind + " hash mismatch")
    payload = parse_boot(boot, "boot")
    require(payload == parse_boot(recovery, "recovery"), "boot/recovery kernel payload differs")
    return parse_payload(payload)


def prepare(root, output):
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), "refusing existing output")
    sources = {}
    inputs = {}
    for kind in PINS:
        path = Path(root) / BACKUP / (kind + ".img")
        require(path.stat().st_size == 16777216, kind + " partition size mismatch")
        inputs[kind] = path.read_bytes()
        sources[kind] = {"path": BACKUP + "/" + kind + ".img", "bytes": len(inputs[kind]), "sha256": sha(inputs[kind])}
    image, dtb, info, metadata = validate_pair(inputs["boot"], inputs["recovery"])
    manifest = {"status": "OFFLINE_COMPONENTS_VERIFIED", "hardware_go": False,
                "sources": sources, "image": info, "dtb": {"bytes": len(dtb), "sha256": sha(dtb)},
                "kernel_payload_byte_equal": True, "mtk_component_header": "ABSENT; gzip at Android offset 2048",
                "dtb_metadata": metadata, "restore_verified": False,
                "validator_sha256": sha(Path(__file__).read_bytes()),
                "handoff_contract": {"executable": False, "entry": ENTRY, "x0": "effective vendor DTB; UNRESOLVED",
                    "unresolved": ["LK memory/bootmode/cmdline/atag mutations and exact vendor DTB delta",
                        "own initrd start/end, payload hashes, segment placement and all static/dynamic reservations",
                        "source shutdown, exception level EL1/EL2 and CPU parking",
                        "MMU/cache coherency, interrupts and DMA quiescence",
                        "PMIC/charger warm state and vendor probe compatibility",
                        "reconciled owner/session/latch; existing voltage/power guards remain mandatory",
                        "independent physical charging, temperature, source classification and fault acceptance",
                        "qualified ACM control, finite trial and proven normal return to installed Linux"],
                    "prohibitions": ["no loader/DTB patch/boot target/flash image", "no mainline cap bypass or hardware allowlist"]}}
    # Exclusive creation preserves existing evidence, including a race-created directory.
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    (output / "Image").write_bytes(image)
    (output / "stock.dtb").write_bytes(dtb)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (output / "SHA256SUMS").write_text("".join(sha((output / name).read_bytes()) + "  " + name + "\n"
                                                for name in ("Image", "stock.dtb", "manifest.json")))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(ROOT, args.output)
    except (ValueError, OSError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "hardware_go": False, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
