#!/usr/bin/env python3
"""Create a root-owned deterministic newc initramfs without privileged mknod."""
import argparse
import gzip
import hashlib
import pathlib
import stat
import struct


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def check_static(binary):
    require(binary[:6] == b"\x7fELF\x02\x01", "expected little-endian ELF64")
    require(struct.unpack_from("<HH", binary, 16) == (2, 183), "expected executable AArch64")
    offset = struct.unpack_from("<Q", binary, 32)[0]
    size, count = struct.unpack_from("<HH", binary, 54)
    types = {struct.unpack_from("<I", binary, offset + i * size)[0] for i in range(count)}
    require(not types.intersection({2, 3}), "unexpected dynamic segment or interpreter")


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("build", type=pathlib.Path)
parser.add_argument("template", type=pathlib.Path)
parser.add_argument("helpers", nargs="*", type=pathlib.Path)
parser.add_argument("--status", type=pathlib.Path)
parser.add_argument("--debug", action="store_true", help="include unauthenticated USB development services")
parser.add_argument("--evdev-module", type=pathlib.Path)
parser.add_argument("--edt-ft5x06-module", type=pathlib.Path)
parser.add_argument("--pwm-module", type=pathlib.Path)
parser.add_argument("--backlight-module", type=pathlib.Path)
args = parser.parse_args()
if bool(args.pwm_module) != bool(args.backlight_module):
    parser.error("--pwm-module and --backlight-module must be supplied together")
build, template = args.build, args.template
actual_config = set((build / "busybox.config").read_text().splitlines())
for setting in (template.parent / "busybox.config").read_text().splitlines():
    require(setting in actual_config, f"Kconfig did not retain {setting}")

binary = (build / "busybox").read_bytes()
check_static(binary)

entries = {}
for name in ("bin", "sbin", "usr", "usr/bin", "usr/sbin", "dev", "dev/pts",
             "proc", "sys", "tmp", "run", "etc", "lib", "lib/modules"):
    entries[name] = (stat.S_IFDIR | (0o1777 if name == "tmp" else 0o755), b"", 0, 0)
entries["bin/busybox"] = (stat.S_IFREG | 0o755, binary, 0, 0)
entries["init"] = (stat.S_IFREG | 0o755, template.read_bytes(), 0, 0)
if args.debug:
    entries["etc/i14-debug"] = (stat.S_IFREG | 0o600, b"", 0, 0)
entries["usb-network"] = (stat.S_IFREG | 0o755, (template.parent / "usb-network").read_bytes(), 0, 0)
entries["storage-readonly"] = (stat.S_IFREG | 0o755, (template.parent / "storage-readonly").read_bytes(), 0, 0)
helpers = set()
for helper in args.helpers:
    require(helper.name in {"i14-pmic-id", "i14-pwrap-status"}, "only the two fixed helpers are supported")
    require(helper.name not in helpers, "duplicate fixed helper")
    helpers.add(helper.name)
    helper_data = helper.read_bytes()
    check_static(helper_data)
    entries["usr/bin/" + helper.name] = (stat.S_IFREG | 0o755, helper_data, 0, 0)
if args.status:
    status_data = args.status.read_bytes()
    check_static(status_data)
    entries["usr/bin/i14-status"] = (stat.S_IFREG | 0o755, status_data, 0, 0)
for name, path, expected_size, expected_hash in (
    ("evdev.ko", args.evdev_module, 26560, "fbedd8646760b0db86b870567d887184fb4492ce5afedebf8319e17aba4752bd"),
    ("edt-ft5x06.ko", args.edt_ft5x06_module, 40992, "ea515fc5bed2d6c4a9e8b5c853dc3536538a1610ac1491cb2285c4530e2d903e"),
    ("pwm-mtk-disp.ko", args.pwm_module, 14632, "244aa41f5bcea5c2220533326a05ba9ee63da57594c557e95c7a084675220bb6"),
    ("pwm_bl.ko", args.backlight_module, 15112, "4e2597fc8257c4757b1d651dc208b9622f03f521926c7d9ad7d181f946a93a1c"),
):
    if path is None:
        continue
    data = path.read_bytes()
    require(len(data) == expected_size and hashlib.sha256(data).hexdigest() == expected_hash,
            f"{name}: exact module hash/size mismatch")
    require(data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<HH", data, 16) == (1, 183),
            "expected relocatable AArch64 module")
    require(b"\0vermagic=7.0.10-i14-bringup SMP preempt mod_unload aarch64\0" in data, f"{name}: vermagic mismatch")
    require(b"\0depends=\0" in data, f"{name}: unexpected dependencies")
    entries["lib/modules/i14"] = (stat.S_IFDIR | 0o755, b"", 0, 0)
    entries["lib/modules/i14/" + name] = (stat.S_IFREG | 0o644, data, 0, 0)
entries["dev/console"] = (stat.S_IFCHR | 0o600, b"", 5, 1)
entries["dev/null"] = (stat.S_IFCHR | 0o666, b"", 1, 3)
entries["dev/ptmx"] = (stat.S_IFCHR | 0o666, b"", 5, 2)
entries["dev/kmsg"] = (stat.S_IFCHR | 0o600, b"", 1, 11)
for link in (build / "busybox.links").read_text().splitlines():
    name = link.lstrip("/")
    path = pathlib.PurePosixPath(name)
    require(name != "." and name == path.as_posix() and ".." not in path.parts,
            f"invalid applet path: {link!r}")
    require(name != "etc/i14-debug" and name not in entries, f"applet path already reserved: {link!r}")
    entries[name] = (stat.S_IFLNK | 0o777, b"/bin/busybox", 0, 0)
for name in ("bin/sh", "bin/mount", "bin/dmesg", "sbin/ip", "sbin/ifconfig",
             "sbin/route", "sbin/udhcpc", "sbin/mdev", "usr/bin/nc",
             "usr/sbin/httpd", "usr/sbin/telnetd", "usr/bin/tcpsvd", "bin/ln", "usr/sbin/udhcpd", "sbin/blockdev"):
    require(name in entries and stat.S_ISLNK(entries[name][0]), f"required applet link missing: {name}")
if args.evdev_module or args.edt_ft5x06_module or args.pwm_module:
    require("sbin/insmod" in entries and stat.S_ISLNK(entries["sbin/insmod"][0]),
            "required applet link missing: sbin/insmod")

archive = bytearray()
for ino, (name, (mode, data, major, minor)) in enumerate(
        [*sorted(entries.items()), ("TRAILER!!!", (0, b"", 0, 0))], 1):
    encoded = name.encode() + b"\0"
    fields = (ino, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, 0,
              len(data), 0, 0, major, minor, len(encoded), 0)
    archive.extend(b"070701" + "".join(f"{f:08x}" for f in fields).encode() + encoded)
    archive.extend(b"\0" * (-len(archive) % 4))
    archive.extend(data)
    archive.extend(b"\0" * (-len(archive) % 4))
archive.extend(b"\0" * (-len(archive) % 512))
(build / "initramfs.cpio.gz").write_bytes(gzip.compress(archive, mtime=0))
print(f"Validated static AArch64 ELF; packaged {len(entries)} root-owned entries")
