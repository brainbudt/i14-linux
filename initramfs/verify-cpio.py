#!/usr/bin/env python3
"""Inspect produced newc bytes independently of make-cpio.py."""
import argparse
import gzip
import hashlib
import pathlib
import stat
import struct


def require(condition, message):
    if not condition:
        raise SystemExit(message)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("build", type=pathlib.Path)
parser.add_argument("helpers", nargs="*", type=pathlib.Path)
parser.add_argument("--status", type=pathlib.Path)
parser.add_argument("--debug", action="store_true", help="require the USB development marker")
parser.add_argument("--evdev-module", type=pathlib.Path)
parser.add_argument("--edt-ft5x06-module", type=pathlib.Path)
parser.add_argument("--pwm-module", type=pathlib.Path)
parser.add_argument("--backlight-module", type=pathlib.Path)
args = parser.parse_args()
if bool(args.pwm_module) != bool(args.backlight_module):
    parser.error("--pwm-module and --backlight-module must be supplied together")
build = args.build
source = pathlib.Path(__file__).resolve().parent
binary = (build / "busybox").read_bytes()
require(binary[:6] == b"\x7fELF\x02\x01", "expected little-endian BusyBox ELF64")
require(struct.unpack_from("<HH", binary, 16) == (2, 183), "expected executable AArch64 BusyBox")
offset = struct.unpack_from("<Q", binary, 32)[0]
size, count = struct.unpack_from("<HH", binary, 54)
require(not {2, 3}.intersection(struct.unpack_from("<I", binary, offset + i * size)[0] for i in range(count)),
        "unexpected dynamic BusyBox segment or interpreter")
compressed = (build / "initramfs.cpio.gz").read_bytes()
require(compressed[4:8] == b"\0\0\0\0", "gzip timestamp not fixed")
archive = gzip.decompress(compressed)
offset = 0
entries = {}
while True:
    require(archive[offset:offset + 6] == b"070701", f"invalid newc header at offset {offset}")
    fields = [int(archive[offset + 6 + i * 8:offset + 14 + i * 8], 16) for i in range(13)]
    namesize, datasize = fields[11], fields[6]
    name = archive[offset + 110:offset + 110 + namesize - 1].decode()
    start = (offset + 110 + namesize + 3) & ~3
    if name == "TRAILER!!!":
        break
    path = pathlib.PurePosixPath(name)
    require(name != "." and not path.is_absolute() and name == path.as_posix() and ".." not in path.parts,
            f"invalid archive path: {name!r}")
    require(name not in entries, f"{name}: duplicate archive entry")
    require(fields[2:4] == [0, 0], f"{name}: not root-owned")
    require(fields[5] == 0, f"{name}: timestamp not fixed")
    entries[name] = fields, archive[start:start + datasize]
    offset = (start + datasize + 3) & ~3
marker = entries.get("etc/i14-debug")
if args.debug:
    require(marker is not None and marker[0][1] == stat.S_IFREG | 0o600 and marker[1] == b"",
            "missing or invalid debug marker")
else:
    require(marker is None, "unexpected debug marker")
require(entries["bin/busybox"][1] == binary, "BusyBox archive bytes differ")
require(entries["bin/busybox"][0][1] == stat.S_IFREG | 0o755, "unexpected BusyBox mode")
helpers = set()
require(all(helper.name in {"i14-pmic-id", "i14-pwrap-status"} for helper in args.helpers),
        "only the two fixed helpers are supported")
for helper, name in [(helper, helper.name) for helper in args.helpers] + ([(args.status, "i14-status")] if args.status else []):
    require(name not in helpers, f"{name}: duplicate helper")
    helpers.add(name)
    mode, data = entries["usr/bin/" + name][0][1], entries["usr/bin/" + name][1]
    require(mode == stat.S_IFREG | 0o755, f"{name}: unexpected helper mode")
    require(data == helper.read_bytes(), f"{name}: helper bytes differ")
    require(data[:6] == b"\x7fELF\x02\x01", f"{name}: expected little-endian ELF64")
    require(struct.unpack_from("<HH", data, 16) == (2, 183), f"{name}: expected executable AArch64")
    table = struct.unpack_from("<Q", data, 32)[0]
    stride, n = struct.unpack_from("<HH", data, 54)
    require(not {2, 3}.intersection(struct.unpack_from("<I", data, table+i*stride)[0] for i in range(n)),
            f"{name}: unexpected dynamic segment or interpreter")
for name in {"i14-pmic-id", "i14-pwrap-status", "i14-status"} - helpers:
    require("usr/bin/" + name not in entries, f"{name}: explicit fixed helper verification required")
for name, path, expected_size, expected_hash in (
    ("evdev.ko", args.evdev_module, 26560, "fbedd8646760b0db86b870567d887184fb4492ce5afedebf8319e17aba4752bd"),
    ("edt-ft5x06.ko", args.edt_ft5x06_module, 40992, "ea515fc5bed2d6c4a9e8b5c853dc3536538a1610ac1491cb2285c4530e2d903e"),
    ("pwm-mtk-disp.ko", args.pwm_module, 14632, "244aa41f5bcea5c2220533326a05ba9ee63da57594c557e95c7a084675220bb6"),
    ("pwm_bl.ko", args.backlight_module, 15112, "4e2597fc8257c4757b1d651dc208b9622f03f521926c7d9ad7d181f946a93a1c"),
):
    archive_name = "lib/modules/i14/" + name
    if path is None:
        require(archive_name not in entries, f"{name}: explicit fixed module verification required")
        continue
    fields, data = entries[archive_name]
    require(fields[1] == stat.S_IFREG | 0o644, f"{name}: unexpected module mode")
    require(data == path.read_bytes(), f"{name}: module bytes differ")
    require(len(data) == expected_size and hashlib.sha256(data).hexdigest() == expected_hash,
            f"{name}: exact module hash/size mismatch")
    require(data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<HH", data, 16) == (1, 183),
            f"{name}: expected relocatable AArch64 module")
    require(b"\0vermagic=7.0.10-i14-bringup SMP preempt mod_unload aarch64\0" in data, f"{name}: vermagic mismatch")
    require(b"\0depends=\0" in data, f"{name}: unexpected dependencies")
    require(entries["lib/modules/i14"][0][1] == stat.S_IFDIR | 0o755, f"{name}: unexpected module directory mode")
for name in ("init", "usb-network", "storage-readonly"):
    require(entries[name][0][1] == stat.S_IFREG | 0o755, f"{name}: unexpected script mode")
    require(entries[name][1] == (source / name).read_bytes(), f"{name}: script bytes differ")
for name, device_numbers in (("dev/console", [5, 1]), ("dev/null", [1, 3]), ("dev/ptmx", [5, 2]), ("dev/kmsg", [1, 11])):
    fields = entries[name][0]
    require(stat.S_ISCHR(fields[1]) and fields[9:11] == device_numbers, f"{name}: unexpected character device")
for name in ("bin/sh", "bin/mount", "bin/dmesg", "bin/ln", "bin/kill", "sbin/ip", "sbin/blockdev", "usr/sbin/udhcpd", "usr/sbin/httpd", "usr/sbin/telnetd"):
    require(stat.S_ISLNK(entries[name][0][1]) and entries[name][1] == b"/bin/busybox",
            f"{name}: expected a symlink to BusyBox")
if args.evdev_module or args.edt_ft5x06_module or args.pwm_module:
    require(entries["sbin/insmod"][0][1] == stat.S_IFLNK | 0o777 and entries["sbin/insmod"][1] == b"/bin/busybox",
            "sbin/insmod: expected a symlink to BusyBox")
print(f"PASS: static AArch64 ELF; {len(entries)} root-owned newc entries; exact init/USB scripts and required applets")
