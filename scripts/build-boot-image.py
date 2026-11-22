#!/usr/bin/env python3
"""Build the current I14 DTB-shim image; retain the old direct format for diagnostics."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
TOOL_COMMIT = "d2bb0af5ba6d3198a3e99529c97eda1be0b5a093"
KERNEL_ADDR, RAMDISK_ADDR, TAGS_ADDR = 0x40200000, 0x44000000, 0x4E000000
PAGE, PARTITION_BYTES, IMAGE_CAP = 2048, 16 * 1024 * 1024, 28 * 1024 * 1024
BOOTOPT = "bootopt=64S3,32N2,64N2"
CMDLINE = (BOOTOPT + " console=tty0 console=ttyS0,921600n8 root=/dev/ram rdinit=/init"
           " ignore_loglevel loglevel=8 clk_ignore_unused pd_ignore_unused"
           " regulator_ignore_unused nokaslr panic=10")
INPUTS = {
    "stock_boot": ("backups/20261030T011515Z-native-serial/boot.img", "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492"),
    "Image.gz": ("artifacts/kernel-build-20261030-wdtfix/Image.gz", "9698778174be3c7ac7e47ab0b6ce50e4dbf11faf97c82e55c4fa78a2d2a9241f"),
    "Image": ("artifacts/kernel-build-20261030-wdtfix/Image", "64a613e14f7c79e84a0c9f62962e216601df444a100def5ff603a17494d57185"),
    "dtb": ("artifacts/diagnostic-dtb-20261030-v12/mt6735-i14-bringup-v12.dtb", "54436e8abc563aed938295be4b99b1acc00134a1875c655fe0797d4883314f97"),
    "initramfs": ("builds/busybox-usb-pwrap-status/initramfs.cpio.gz", "b799184de2c2350e7775a1942458a6b24d3d1d47e4029e133a4e2d69b8ce77dc"),
}


class ImageError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def require_hash(data, expected, label):
    if sha256(data) != expected:
        raise ImageError(f"{label}: hash mismatch")


def inflate_gzip(data, limit, label, allow_tail=False):
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(data, limit + 1)
    except zlib.error as error:
        raise ImageError(f"{label}: invalid gzip") from error
    if len(decoded) > limit:
        raise ImageError(f"{label}: decompressed size limit exceeded")
    if not decoder.eof:
        raise ImageError(f"{label}: gzip EOF missing")
    if decoder.unused_data and not allow_tail:
        raise ImageError(f"{label}: unexpected gzip tail or second member")
    return decoded, decoder.unused_data


def overlaps(a, b):
    return a[0] < a[1] and b[0] < b[1] and a[0] < b[1] and b[0] < a[1]


def validate_fdt(data):
    if len(data) < 40:
        raise ImageError("DTB: truncated header")
    magic, total, structure, strings, reserve, version, compat, _, string_size, struct_size = struct.unpack_from(">10I", data)
    if magic != 0xD00DFEED or total != len(data) or version != 17 or compat > 17:
        raise ImageError("DTB: magic, totalsize or version mismatch")
    if structure % 4 or reserve % 8 or struct_size < 4 or struct_size % 4:
        raise ImageError("DTB: invalid alignment or structure size")
    blocks = [(0, 40), (structure, structure + struct_size), (strings, strings + string_size)]
    if any(start < 0 or end > total for start, end in blocks) or reserve < 40:
        raise ImageError("DTB: block outside totalsize")
    end = reserve
    while True:
        if end + 16 > total:
            raise ImageError("DTB: unterminated reservation map")
        address, size = struct.unpack_from(">QQ", data, end)
        end += 16
        if address == size == 0:
            break
    blocks.append((reserve, end))
    if any(overlaps(a, b) for i, a in enumerate(blocks) for b in blocks[i + 1:]):
        raise ImageError("DTB: overlapping blocks")
    if data[structure + struct_size - 4:structure + struct_size] != struct.pack(">I", 9):
        raise ImageError("DTB: structure lacks FDT_END")
    return {"bytes": total, "version": version, "sha256": sha256(data)}


def arm64_info(data, load=KERNEL_ADDR):
    if len(data) < 64 or data[56:60] != b"ARM\x64":
        raise ImageError("Image: ARM64 header missing")
    if len(data) >= IMAGE_CAP:
        raise ImageError("Image: file must be below the 28 MiB cap")
    text_offset, image_size, flags = struct.unpack_from("<QQQ", data, 8)
    if text_offset != 0 or image_size < len(data) or flags != 10:
        raise ImageError("Image: unexpected text_offset, image_size or flags")
    if (load - text_offset) % 0x200000:
        raise ImageError("Image: placement must use a 2 MiB aligned base")
    return {"file_bytes": len(data), "image_size": image_size, "text_offset": text_offset,
            "flags": flags, "load": load, "file_end": load + len(data),
            "effective_end": load + image_size, "sha256": sha256(data)}


def check_ranges(image, ramdisk_size, dtb_size):
    if ramdisk_size <= 0 or dtb_size <= 0:
        raise ImageError("ramdisk and DTB must be nonempty")
    kernel = (KERNEL_ADDR, image["effective_end"])
    ramdisk = (RAMDISK_ADDR, RAMDISK_ADDR + ramdisk_size)
    tags = (TAGS_ADDR, TAGS_ADDR + dtb_size)
    guards = {"LK": (0x41E00000, 0x43000000), "ATF": (0x43000000, 0x43030000),
              "ramconsole": (0x43F00000, 0x44000000), "upper reserved RAM": (0x78000000, 0x80000000)}
    if kernel[1] <= kernel[0]:
        raise ImageError("Image: empty or inverted range")
    for name, region in (("kernel", kernel), ("ramdisk", ramdisk), ("tags", tags)):
        if region[0] < 0x40000000 or region[1] > 0x80000000:
            raise ImageError(f"{name}: outside the first RAM bank")
        for guard, reserved in guards.items():
            if overlaps(region, reserved):
                raise ImageError(f"{name}: overlaps {guard}")
    if overlaps(kernel, ramdisk) or overlaps(kernel, tags):
        raise ImageError("kernel overlaps ramdisk or tags")
    if overlaps(ramdisk, tags):
        raise ImageError("ramdisk overlaps tags")
    return {name: {"start": start, "end_exclusive": end}
            for name, (start, end) in {"kernel": kernel, "ramdisk": ramdisk, "tags": tags, **guards}.items()}


def parse_boot_v0(data):
    if len(data) < PAGE or data[:8] != b"ANDROID!":
        raise ImageError("boot: Android header missing")
    keys = ("kernel_size", "kernel_addr", "ramdisk_size", "ramdisk_addr", "second_size",
            "second_addr", "tags_addr", "page_size", "header_version", "os_version")
    header = dict(zip(keys, struct.unpack_from("<10I", data, 8)))
    if header["header_version"] != 0 or header["page_size"] != PAGE:
        raise ImageError("boot: expected v0 and 2048-byte pages")
    try:
        header["cmdline"] = (data[64:576].split(b"\0", 1)[0] + data[608:1632].split(b"\0", 1)[0]).decode("ascii")
    except UnicodeDecodeError as error:
        raise ImageError("boot: non-ASCII cmdline") from error
    aligned = lambda size: (size + PAGE - 1) // PAGE * PAGE
    header["kernel_file_offset"] = PAGE
    header["ramdisk_file_offset"] = PAGE + aligned(header["kernel_size"])
    header["padded_bytes"] = (header["ramdisk_file_offset"] + aligned(header["ramdisk_size"])
                              + aligned(header["second_size"]))
    if not header["kernel_size"] or not header["ramdisk_size"] or header["padded_bytes"] > len(data):
        raise ImageError("boot: component sizes exceed file or are empty")
    return header


def validate_candidate(data, kernel, ramdisk):
    if len(data) >= PARTITION_BYTES:
        raise ImageError("boot: candidate must be below the 16 MiB partition size")
    header = parse_boot_v0(data)
    expected = {"kernel_addr": KERNEL_ADDR, "ramdisk_addr": RAMDISK_ADDR, "tags_addr": TAGS_ADDR,
                "second_size": 0, "second_addr": 0, "os_version": 0, "cmdline": CMDLINE}
    for key, value in expected.items():
        if header[key] != value:
            raise ImageError(f"boot: {key} mismatch")
    if header["padded_bytes"] != len(data):
        raise ImageError("boot: unexpected bytes after padded components")
    start = header["kernel_file_offset"]
    if data[start:start + header["kernel_size"]] != kernel:
        raise ImageError("boot: kernel payload mismatch")
    start = header["ramdisk_file_offset"]
    if data[start:start + header["ramdisk_size"]] != ramdisk:
        raise ImageError("boot: ramdisk payload mismatch")
    return header


def run(args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH="")
    try:
        return subprocess.run(["rtk", "proxy", *map(str, args)], check=True,
                              capture_output=True, env=env).stdout
    except subprocess.CalledProcessError as error:
        raise ImageError("local tool failed: " + error.stderr.decode(errors="replace")[:600]) from error


def validate_inputs():
    data, sources = {}, {}
    for name, (relative, expected) in INPUTS.items():
        path = ROOT / relative
        if path.stat().st_size > max(IMAGE_CAP, PARTITION_BYTES):
            raise ImageError(f"{name}: input file too large")
        data[name] = path.read_bytes()
        require_hash(data[name], expected, name)
        sources[name] = {"path": relative, "bytes": len(data[name]), "sha256": expected}
    stock = data["stock_boot"]
    if len(stock) != PARTITION_BYTES:
        raise ImageError("stock: expected exactly 16 MiB")
    header = parse_boot_v0(stock)
    for key, expected in {"kernel_addr": 0x40080000, "ramdisk_addr": RAMDISK_ADDR,
                          "tags_addr": TAGS_ADDR, "second_size": 0,
                          "os_version": 0, "cmdline": BOOTOPT}.items():
        if header[key] != expected:
            raise ImageError(f"stock: {key} conflicts with measured layout")
    start = header["kernel_file_offset"]
    stock_image, stock_dtb = inflate_gzip(stock[start:start + header["kernel_size"]], IMAGE_CAP, "stock kernel", allow_tail=True)
    validate_fdt(stock_dtb)
    if stock_image[56:60] != b"ARM\x64" or struct.unpack_from("<Q", stock_image, 8)[0] != 0x80000:
        raise ImageError("stock: unexpected ARM64 header/text_offset")
    start = header["ramdisk_file_offset"]
    inflate_gzip(stock[start:start + header["ramdisk_size"]], PARTITION_BYTES, "stock ramdisk")
    analysis_path = ROOT / "artifacts/20261030T011515Z-stock-boot/header-analysis.json"
    analysis_bytes = analysis_path.read_bytes()
    analysis = json.loads(analysis_bytes)
    if analysis.get("source_sha256") != sources["stock_boot"]["sha256"] or analysis.get("cmdline_allowlist") != [BOOTOPT]:
        raise ImageError("stock analysis does not match pinned source or allowlist")
    for key in ("source_bytes", "kernel_size", "kernel_addr", "ramdisk_size", "ramdisk_addr",
                "second_size", "tags_addr", "page_size", "header_version", "os_version"):
        if analysis.get(key) != (len(stock) if key == "source_bytes" else header[key]):
            raise ImageError(f"stock analysis conflicts with raw header: {key}")
    sources["stock_header_analysis"] = {"path": str(analysis_path.relative_to(ROOT)),
                                        "bytes": len(analysis_bytes), "sha256": sha256(analysis_bytes)}
    decoded, _ = inflate_gzip(data["Image.gz"], IMAGE_CAP, "Image.gz")
    if decoded != data["Image"]:
        raise ImageError("Image.gz does not decompress to the pinned Image")
    image = arm64_info(decoded)
    dtb = validate_fdt(data["dtb"])
    rootfs, _ = inflate_gzip(data["initramfs"], PARTITION_BYTES, "initramfs")
    if not rootfs.startswith(b"070701"):
        raise ImageError("initramfs: expected newc archive")
    ranges = check_ranges(image, len(data["initramfs"]), len(data["dtb"]))
    return data, sources, header, image, dtb, ranges


def build(output):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ImageError(f"Refusing existing output: {output}")
    output = output.resolve()
    if sys.byteorder != "little":
        raise ImageError("pinned AOSP scripts require a little-endian host")
    data, sources, stock, image, dtb, ranges = validate_inputs()
    tools = ROOT / "artifacts/tooling/mkbootimg"
    commit = run(["git", "-C", tools, "rev-parse", "HEAD"]).decode().strip()
    if commit != TOOL_COMMIT or run(["git", "-C", tools, "status", "--porcelain", "--untracked-files=all"]).strip():
        raise ImageError("AOSP tool revision mismatch or modified worktree")
    tool_hashes = {name: sha256((tools / name).read_bytes()) for name in
                   ("mkbootimg.py", "unpack_bootimg.py", "gki/generate_gki_certificate.py")}
    payload = data["Image.gz"] + data["dtb"]
    decoded, tail = inflate_gzip(payload, IMAGE_CAP, "custom kernel payload", allow_tail=True)
    if decoded != data["Image"] or tail != data["dtb"]:
        raise ImageError("custom kernel payload: gzip/DTB boundary mismatch")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    (output / "INCOMPLETE").write_text("Experimental candidate under construction; do not use.\n")
    (output / "kernel-payload.bin").write_bytes(payload)
    arguments = ["--header_version", "0", "--pagesize", str(PAGE), "--base", "0",
                 "--kernel_offset", hex(KERNEL_ADDR), "--ramdisk_offset", hex(RAMDISK_ADDR),
                 "--second_offset", "0", "--tags_offset", hex(TAGS_ADDR), "--cmdline", CMDLINE,
                 "--kernel", str(output / "kernel-payload.bin"), "--ramdisk", str(ROOT / INPUTS["initramfs"][0])]
    run([sys.executable, tools / "mkbootimg.py", *arguments, "--output", output / "boot.img"])
    candidate = (output / "boot.img").read_bytes()
    header = validate_candidate(candidate, payload, data["initramfs"])
    unpacked = output / "unpacked"
    raw_args = run([sys.executable, tools / "unpack_bootimg.py", "--boot_img", output / "boot.img",
                    "--out", unpacked, "--format", "mkbootimg", "-0"])
    values = raw_args.decode().rstrip("\0").split("\0")
    unpack_args = dict(zip(values[::2], values[1::2]))
    expected_args = {"--header_version": "0", "--pagesize": "0x00000800", "--base": "0x00000000",
                     "--kernel_offset": "0x40200000", "--ramdisk_offset": "0x44000000",
                     "--tags_offset": "0x4e000000", "--second_offset": "0x00000000", "--cmdline": CMDLINE}
    if any(unpack_args.get(key) != value for key, value in expected_args.items()):
        raise ImageError("AOSP round-trip header mismatch")
    if (unpacked / "kernel").read_bytes() != payload or (unpacked / "ramdisk").read_bytes() != data["initramfs"]:
        raise ImageError("AOSP round-trip component mismatch")
    run([sys.executable, tools / "mkbootimg.py", *values, "--output", output / "roundtrip.img"])
    if (output / "roundtrip.img").read_bytes() != candidate:
        raise ImageError("AOSP repacked image differs from candidate")
    (output / "unpacked-arguments.json").write_text(json.dumps(unpack_args, indent=2) + "\n")
    manifest = {"status": {"experimental": True, "intended_use": "temporary RAM trial candidate only",
                            "native_boot": "UNKNOWN", "device_calls": False, "flash_attempted": False},
                "inputs": sources, "stock_layout_allowlisted": stock,
                "aosp_tools": {"commit": commit, "files_sha256": tool_hashes},
                "builder_sha256": sha256(Path(__file__).read_bytes()), "python": sys.version,
                "arm64_image": image, "dtb": dtb, "memory_ranges": ranges, "candidate_header": header,
                "limits": {"Image_file_strictly_below": IMAGE_CAP, "boot_file_strictly_below": PARTITION_BYTES},
                "outputs": {"boot.img": {"bytes": len(candidate), "sha256": sha256(candidate)},
                            "kernel-payload.bin": {"bytes": len(payload), "sha256": sha256(payload)}},
                "roundtrip": {"kernel_payload_exact": True, "gzip_image_exact": True, "dtb_exact": True,
                              "initramfs_exact": True, "header_fields_exact": True, "repacked_boot_exact": True},
                "mkbootimg_arguments": arguments}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    checksums = "".join(f"{sha256((output / name).read_bytes())}  {name}\n" for name in
                        ("boot.img", "kernel-payload.bin", "roundtrip.img", "unpacked/kernel", "unpacked/ramdisk", "manifest.json", "unpacked-arguments.json"))
    (output / "SHA256SUMS").write_text(checksums)
    (output / "INCOMPLETE").unlink()
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--direct-diagnostic", action="store_true",
                        help="historical direct format; its own DTB is ignored by this LK during RAM boot")
    args = parser.parse_args(argv)
    if args.output is None:
        prefix = "i14-direct-diagnostic-" if args.direct_diagnostic else "i14-dtb-shim-"
        args.output = ROOT / "builds" / (prefix + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    try:
        if not args.direct_diagnostic:
            dtb_path, dtb_hash = INPUTS["dtb"]
            output = run([sys.executable, ROOT / "scripts/build-dtb-shim.py",
                          "--dtb", ROOT / dtb_path, "--dtb-sha256", dtb_hash,
                          "--output", args.output])
            print(output.decode(), end="")
            return 0
        manifest = build(args.output)
    except (ImageError, OSError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "boot.img": manifest["outputs"]["boot.img"], "native_boot": "UNKNOWN"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
