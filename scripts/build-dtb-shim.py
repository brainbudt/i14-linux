#!/usr/bin/env python3
"""Build the fixed I14 AArch64 DTB-selection RAM candidate, without device calls."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "60b9bc5aee5d03f9de3cf0914430d1d16615519699ff107b181670319e685444"
ASM_SHA = "c6460af64a0771cb51e1659dd1c1d21c7b33f4410580eeeb6f2471c40c81d634"
helper = ROOT / "scripts/build-boot-image.py"
if hashlib.sha256(helper.read_bytes()).hexdigest() != BASE_SHA:
    raise RuntimeError("base image validator source hash mismatch")
spec = importlib.util.spec_from_file_location("i14_boot_base", helper)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

PREFIX_SIZE, DTB_OFFSET, OUTER_BASE = 0x200000, 0x1000, 0x40000000
INITRD_END = 0x44038E53
DISPLAY_INITRD_END = 0x4404BB41
DISPLAY_INITRD = ("builds/busybox-display-touch-backlight-status-20261030T095131Z/initramfs.cpio.gz",
                  "734c707d1d366297c90bded2fdab7ece3f8c1510ca5e21e580147f49e7f7e97b")
DTB_BOOTARGS = ("console=tty0 console=ttyS0,921600n8 root=/dev/ram rdinit=/init"
                " ignore_loglevel loglevel=8 nokaslr panic=10"
                " clk_ignore_unused pd_ignore_unused regulator_ignore_unused")
LLVM = Path(os.environ["LLVM_BIN"]) if "LLVM_BIN" in os.environ else Path(shutil.which("clang") or "clang").parent
CLANG = LLVM / "clang"


def check_embedded_size(size):
    if size <= 0 or size > PREFIX_SIZE - DTB_OFFSET:
        raise base.ImageError("diagnostic DTB does not fit fixed prefix")


def chosen_properties(data, target=("", "chosen")):
    base.validate_fdt(data)
    _, _, position, strings, _, _, _, _, string_size, struct_size = struct.unpack_from(">10I", data)
    end = position + struct_size
    string_table = data[strings:strings + string_size]
    stack, properties = [], {}
    while position + 4 <= end:
        token = struct.unpack_from(">I", data, position)[0]
        position += 4
        if token == 1:
            stop = data.find(b"\0", position, end)
            if stop < 0:
                raise base.ImageError("DTB: unterminated node")
            stack.append(data[position:stop].decode("ascii"))
            position = (stop + 4) & ~3
        elif token == 2:
            if not stack:
                raise base.ImageError("DTB: unbalanced nodes")
            stack.pop()
        elif token == 3:
            if position + 8 > end:
                raise base.ImageError("DTB: truncated property")
            size, name_offset = struct.unpack_from(">II", data, position)
            position += 8
            stop = string_table.find(b"\0", name_offset)
            if stop < 0 or position + size > end:
                raise base.ImageError("DTB: invalid property bounds/name")
            name = string_table[name_offset:stop].decode("ascii")
            if tuple(stack) == target:
                if name in properties:
                    raise base.ImageError("DTB: duplicate chosen property")
                properties[name] = data[position:position + size]
            position = (position + size + 3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            if stack:
                raise base.ImageError("DTB: missing node end")
            return properties
        else:
            raise base.ImageError("DTB: unknown structure token")
    raise base.ImageError("DTB: missing structure end")


def validate_display_properties(props):
    # Exact active layer measured on this I14; never accept reference-board RAM.
    expected = {
        "compatible": b"simple-framebuffer\0", "status": b"okay\0",
        "reg": struct.pack(">4I", 0, 0xBF48E000, 0, 0x465000),
        "width": struct.pack(">I", 720), "height": struct.pack(">I", 1600),
        "stride": struct.pack(">I", 2880), "format": b"a8r8g8b8\0",
    }
    for name, value in expected.items():
        if props.get(name) != value:
            raise base.ImageError(f"DTB: measured I14 framebuffer {name} mismatch")
    return {"address": 0xBF48E000, "bytes": 4608000, "width": 720,
            "height": 1600, "stride": 2880, "format": "a8r8g8b8"}


def validate_diagnostic_dtb(data, expected, persistent=False, display_ui=False):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise base.ImageError("DTB SHA-256 must be explicit lowercase hexadecimal")
    base.require_hash(data, expected, "diagnostic DTB")
    check_embedded_size(len(data))
    props = chosen_properties(data)
    if display_ui or b"simple-framebuffer\0" in data:
        validate_display_properties(chosen_properties(data, ("", "chosen", "framebuffer@bf48e000")))
        reserved = chosen_properties(data, ("", "reserved-memory", "framebuffer@bf010000"))
        if reserved.get("reg") != struct.pack(">4I", 0, 0xBF010000, 0, 0xFB0000) or reserved.get("no-map") != b"":
            raise base.ImageError("DTB: own framebuffer reservation mismatch")
    initrd_end = DISPLAY_INITRD_END if display_ui else INITRD_END
    for name, value in (("linux,initrd-start", base.RAMDISK_ADDR), ("linux,initrd-end", initrd_end)):
        raw = props.get(name, b"")
        if len(raw) not in (4, 8) or int.from_bytes(raw, "big") != value:
            raise base.ImageError(f"DTB: exact {name} required")
    bootargs = DTB_BOOTARGS + (" consoleblank=0" if display_ui else "") + (" i14.trial_reboot=off" if persistent else "")
    for name in ("bootargs", "zimage64,cmdline-override"):
        if props.get(name) != bootargs.encode() + b"\0":
            raise base.ImageError(f"DTB: diagnostic {name} mismatch")
    return {"initrd_start": base.RAMDISK_ADDR, "initrd_end": initrd_end,
            "bootargs": bootargs, "bytes": len(data), "sha256": expected,
            "trial_reboot": "explicitly disabled" if persistent else "120 seconds"}


def require_no_relocations(data):
    if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<HH", data, 16) != (1, 183):
        raise base.ImageError("shim: expected relocatable AArch64 ELF")
    table = struct.unpack_from("<Q", data, 40)[0]
    entry_size, count = struct.unpack_from("<HH", data, 58)
    for i in range(count):
        offset = table + i * entry_size
        kind = struct.unpack_from("<I", data, offset + 4)[0]
        size = struct.unpack_from("<Q", data, offset + 32)[0]
        if kind in (4, 9) and size:
            raise base.ImageError("shim: unresolved relocation would require a linker")


def assemble_prefix(output, dtb_path, dtb_data, image):
    check_embedded_size(len(dtb_data))
    source = ROOT / "kernel/dtb-shim.S"
    base.require_hash(source.read_bytes(), ASM_SHA, "shim assembly source")
    effective = PREFIX_SIZE + image["image_size"]
    file_end = OUTER_BASE + PREFIX_SIZE + image["file_bytes"]
    if file_end >= OUTER_BASE + base.IMAGE_CAP or OUTER_BASE + effective > 0x41E00000:
        raise base.ImageError("shim outer file/effective range exceeds guarded bound")
    target = output / "shim.o"
    definitions = [f"-DDTB_SIZE={len(dtb_data)}", f"-DOUTER_EFFECTIVE_SIZE={effective}",
                   f"-DOUTER_FILE_END={file_end}", "-DDTB_FILE=" + json.dumps(str(dtb_path.resolve()))]
    base.run([CLANG, "--target=aarch64-none-elf", "-march=armv8-a", "-c", source, *definitions, "-o", target])
    require_no_relocations(target.read_bytes())
    base.run([LLVM / "llvm-objcopy", "--only-section=.text", "-O", "binary", target, output / "shim-prefix.bin"])
    disassembly = base.run([LLVM / "llvm-objdump", "-d", "--no-show-raw-insn", "--start-address=64",
                            "--stop-address=512", target]).decode()
    (output / "shim-disassembly.txt").write_text(disassembly)
    prefix = (output / "shim-prefix.bin").read_bytes()
    if len(prefix) != DTB_OFFSET + len(dtb_data) or prefix[DTB_OFFSET:] != dtb_data:
        raise base.ImageError("shim: embedded DTB bytes/offset differ")
    return prefix, disassembly


def combine_outer(prefix, image, dtb, image_size):
    if len(prefix) != DTB_OFFSET + len(dtb) or prefix[DTB_OFFSET:] != dtb:
        raise base.ImageError("outer: wrong embedded DTB")
    check_embedded_size(len(dtb))
    if len(image) > image_size:
        raise base.ImageError("outer: inner file exceeds effective size")
    if prefix[:4] != struct.pack("<I", 0x14000010) or prefix[56:60] != b"ARM\x64":
        raise base.ImageError("outer: expected branch-to-64 ARM64 header")
    if struct.unpack_from("<QQQ", prefix, 8) != (0, PREFIX_SIZE + image_size, 10):
        raise base.ImageError("outer: ARM64 placement/header mismatch")
    outer = prefix + bytes(PREFIX_SIZE - len(prefix)) + image
    base.arm64_info(outer, load=OUTER_BASE)
    if outer[PREFIX_SIZE:] != image or OUTER_BASE + PREFIX_SIZE != base.KERNEL_ADDR:
        raise base.ImageError("outer: original kernel placement mismatch")
    return outer


def pinned_inputs(display_ui=False):
    data, sources = {}, {}
    for name in ("stock_boot", "Image.gz", "Image", "initramfs"):
        relative, expected = DISPLAY_INITRD if display_ui and name == "initramfs" else base.INPUTS[name]
        blob = (ROOT / relative).read_bytes()
        base.require_hash(blob, expected, name)
        data[name] = blob
        sources[name] = {"path": relative, "bytes": len(blob), "sha256": expected}
    stock = data["stock_boot"]
    header = base.parse_boot_v0(stock)
    expected = {"kernel_addr": 0x40080000, "ramdisk_addr": base.RAMDISK_ADDR,
                "tags_addr": base.TAGS_ADDR, "second_size": 0, "os_version": 0, "cmdline": base.BOOTOPT}
    if len(stock) != base.PARTITION_BYTES or any(header[key] != value for key, value in expected.items()):
        raise base.ImageError("stock: original boot layout changed")
    start = header["kernel_file_offset"]
    original, stock_dtb = base.inflate_gzip(stock[start:start + header["kernel_size"]], base.IMAGE_CAP, "stock kernel", allow_tail=True)
    base.validate_fdt(stock_dtb)
    if original[56:60] != b"ARM\x64" or struct.unpack_from("<Q", original, 8)[0] != 0x80000:
        raise base.ImageError("stock: original ARM64 entry header changed")
    decoded, _ = base.inflate_gzip(data["Image.gz"], base.IMAGE_CAP, "base kernel")
    if decoded != data["Image"]:
        raise base.ImageError("base Image.gz/Image mismatch")
    base.inflate_gzip(data["initramfs"], base.PARTITION_BYTES, "initramfs")
    if base.RAMDISK_ADDR + len(data["initramfs"]) != (DISPLAY_INITRD_END if display_ui else INITRD_END):
        raise base.ImageError("initramfs size conflicts with diagnostic DTB")
    return data, sources, header, stock_dtb


def validate_shim_boot(data, payload, ramdisk):
    if len(data) >= base.PARTITION_BYTES:
        raise base.ImageError("shim boot must be below 16 MiB")
    header = base.parse_boot_v0(data)
    expected = {"kernel_addr": OUTER_BASE, "ramdisk_addr": base.RAMDISK_ADDR,
                "tags_addr": base.TAGS_ADDR, "second_size": 0, "second_addr": 0,
                "os_version": 0, "cmdline": base.CMDLINE}
    if any(header[k] != v for k, v in expected.items()) or header["padded_bytes"] != len(data):
        raise base.ImageError("shim boot header/ranges differ")
    for name, expected_blob in (("kernel", payload), ("ramdisk", ramdisk)):
        start = header[name + "_file_offset"]
        if data[start:start + header[name + "_size"]] != expected_blob:
            raise base.ImageError(f"shim boot {name} payload mismatch")
    return header


def build(output, dtb_path, expected_dtb, persistent=False, display_ui=False):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise base.ImageError(f"Refusing existing output: {output}")
    output = output.resolve()
    dtb_path = Path(dtb_path).resolve()
    dtb = dtb_path.read_bytes()
    diagnostic = validate_diagnostic_dtb(dtb, expected_dtb, persistent=persistent, display_ui=display_ui)
    data, sources, stock_header, stock_dtb = pinned_inputs(display_ui=display_ui)
    image = base.arm64_info(data["Image"])
    ranges = base.check_ranges(image, len(data["initramfs"]), len(dtb))
    tools = ROOT / "artifacts/tooling/mkbootimg"
    if base.run(["git", "-C", tools, "rev-parse", "HEAD"]).decode().strip() != base.TOOL_COMMIT or base.run(["git", "-C", tools, "status", "--porcelain", "--untracked-files=all"]).strip():
        raise base.ImageError("AOSP tool pin/worktree mismatch")
    if sys.byteorder != "little":
        raise base.ImageError("AOSP wrapper requires little-endian host")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    (output / "INCOMPLETE").write_text("Private experimental RAM candidate; do not use incomplete output.\n")
    prefix, _ = assemble_prefix(output, dtb_path, dtb, image)
    outer = combine_outer(prefix, data["Image"], dtb, image["image_size"])
    outer_gzip = gzip.compress(outer, mtime=0)
    payload = outer_gzip + stock_dtb
    decoded, tail = base.inflate_gzip(payload, base.IMAGE_CAP, "outer gzip", allow_tail=True)
    if decoded != outer or tail != stock_dtb or payload.rfind(b"\xd0\x0d\xfe\xed") != len(outer_gzip):
        raise base.ImageError("outer gzip/stock DTB boundary or last DTB magic mismatch")
    (output / "outer-Image").write_bytes(outer)
    (output / "outer-Image.gz").write_bytes(outer_gzip)
    (output / "kernel-payload.bin").write_bytes(payload)
    (output / "stock-loader.dtb").write_bytes(stock_dtb)
    (output / "own-kernel.dtb").write_bytes(dtb)
    args = ["--header_version", "0", "--pagesize", str(base.PAGE), "--base", "0",
            "--kernel_offset", hex(OUTER_BASE), "--ramdisk_offset", hex(base.RAMDISK_ADDR),
            "--tags_offset", hex(base.TAGS_ADDR), "--second_offset", "0", "--cmdline", base.CMDLINE,
            "--kernel", str(output / "kernel-payload.bin"), "--ramdisk", str(ROOT / sources["initramfs"]["path"])]
    base.run([sys.executable, tools / "mkbootimg.py", *args, "--output", output / "boot.img"])
    candidate = (output / "boot.img").read_bytes()
    header = validate_shim_boot(candidate, payload, data["initramfs"])
    raw_args = base.run([sys.executable, tools / "unpack_bootimg.py", "--boot_img", output / "boot.img",
                         "--out", output / "unpacked", "--format", "mkbootimg", "-0"])
    values = raw_args.decode().rstrip("\0").split("\0")
    unpack_args = dict(zip(values[::2], values[1::2]))
    if any(int(unpack_args.get(k, "-1"), 0) != v for k, v in
           (("--kernel_offset", OUTER_BASE), ("--ramdisk_offset", base.RAMDISK_ADDR),
            ("--tags_offset", base.TAGS_ADDR), ("--pagesize", base.PAGE), ("--header_version", 0))):
        raise base.ImageError("AOSP unpack header differs")
    if (output / "unpacked/kernel").read_bytes() != payload or (output / "unpacked/ramdisk").read_bytes() != data["initramfs"]:
        raise base.ImageError("AOSP unpack component differs")
    base.run([sys.executable, tools / "mkbootimg.py", *values, "--output", output / "roundtrip.img"])
    if (output / "roundtrip.img").read_bytes() != candidate:
        raise base.ImageError("AOSP repacked image differs")
    sources["diagnostic_dtb"] = {"path": str(dtb_path.relative_to(ROOT)), **diagnostic}
    manifest = {"status": {"experimental": True, "native_boot": "UNKNOWN",
                            "intended_use": ("persistent candidate; requires its own RAM proof before installation"
                                             if persistent else "temporary RAM trial only"),
                            "persistent_candidate": persistent,
                            "device_calls": False, "flash_attempted": False, "contains_private_stock_DTB": True,
                            "delivery": "local ignored artifacts only; no remote/Git"},
                "sources": sources, "stock_header_allowlisted": stock_header,
                "source_sha256": {"builder": base.sha256(Path(__file__).read_bytes()), "base_validator": BASE_SHA, "assembly": ASM_SHA},
                "aosp_commit": base.TOOL_COMMIT,
                "aosp_files_sha256": {name: base.sha256((tools / name).read_bytes()) for name in
                                      ("mkbootimg.py", "unpack_bootimg.py", "gki/generate_gki_certificate.py")},
                "local_tools": {name: {"sha256": base.sha256((LLVM / name).read_bytes()),
                                        "version": base.run([LLVM / name, "--version"]).decode().splitlines()[0]}
                                for name in ("clang", "llvm-objcopy", "llvm-objdump")},
                "inner_image": image, "outer_image": base.arm64_info(outer, load=OUTER_BASE),
                "layout": {"outer_base": OUTER_BASE, "kernel_offset": PREFIX_SIZE, "kernel_entry": base.KERNEL_ADDR,
                           "embedded_DTB_offset": DTB_OFFSET, "DTB_destination": base.TAGS_ADDR,
                           "outer_file_end": OUTER_BASE + len(outer), "effective_end": image["effective_end"]},
                "memory_ranges": ranges, "candidate_header": header,
                "stock_loader_DTB": {"bytes": len(stock_dtb), "sha256": base.sha256(stock_dtb), "kernel_payload_offset": len(outer_gzip)},
                "own_kernel_DTB": diagnostic, "last_DTB_magic_offset": payload.rfind(b"\xd0\x0d\xfe\xed"),
                "gzip_DTB_magic_count": outer_gzip.count(b"\xd0\x0d\xfe\xed"),
                "entry_guards": "EL1/EL2, MMU off, little endian; otherwise park; no EL/MMU transition",
                "cache_handoff": "CTR_EL0 line size; CIVAC entire loaded outer image and copied DTB; DSB SY; IC IALLU; DSB SY; ISB",
                "roundtrip": {"payload_exact": True, "stock_DTB_exact": True, "own_DTB_exact": True,
                              "original_Image_exact": True, "initramfs_exact": True, "repacked_boot_exact": True},
                "outputs": {"boot.img": {"bytes": len(candidate), "sha256": base.sha256(candidate)},
                            "kernel-payload.bin": {"bytes": len(payload), "sha256": base.sha256(payload)}}}
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    files = ["boot.img", "roundtrip.img", "kernel-payload.bin", "outer-Image", "outer-Image.gz", "shim.o",
             "shim-prefix.bin", "shim-disassembly.txt", "stock-loader.dtb", "own-kernel.dtb",
             "unpacked/kernel", "unpacked/ramdisk", "manifest.json"]
    (output / "SHA256SUMS").write_text("".join(f"{base.sha256((output / name).read_bytes())}  {name}\n" for name in files))
    (output / "INCOMPLETE").unlink()
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dtb", type=Path, required=True)
    parser.add_argument("--dtb-sha256", required=True)
    parser.add_argument("--persistent", action="store_true",
                        help="require the exact DTB timer opt-out; still builds locally without flashing")
    parser.add_argument("--display-ui", action="store_true",
                        help="use the exact pinned display/touch status initramfs and matching DTB end")
    parser.add_argument("--output", type=Path, default=ROOT / "builds" / ("i14-dtb-shim-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    args = parser.parse_args(argv)
    try:
        manifest = build(args.output, args.dtb, args.dtb_sha256, persistent=args.persistent, display_ui=args.display_ui)
    except (base.ImageError, OSError, UnicodeDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "boot.img": manifest["outputs"]["boot.img"], "native_boot": "UNKNOWN", "private_stock_DTB": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
