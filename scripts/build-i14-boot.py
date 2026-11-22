#!/usr/bin/env python3
"""Build the installable I14 boot image: stock kernel + stock Android ramdisk + /i14init.

The ramdisk is the untouched stock Android ramdisk followed by a small second cpio
archive (the kernel unpacks concatenated archives in order). The kernel cmdline gets
`rdinit=/i14init`. Android is gone from the phone: nothing starts its /init any more,
every fallback is the rescue mode in /i14init.
"""
import argparse
import gzip
import hashlib
import importlib.util
import os
import pathlib
import re
import stat
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUSYBOX = ROOT / "artifacts/tooling/alpine-3.24/busybox.static"  # Alpine 3.24 busybox-static 1.37.0-r31
BUSYBOX_SHA256 = "999cb969d09093a71716cfc747bb53cdada3f332c05eb5046c56e0f66a4d6d22"
APPLETS = ("sh", "cat", "date", "echo", "grep", "ls", "mkdir", "rm", "sleep", "mount", "umount",
           "mdev", "readlink", "switch_root", "sync", "mv", "reboot", "poweroff", "dd", "od", "tr", "tail",
           "getty", "login")
E2FSCK = ROOT / "artifacts/build-tools/e2fsprogs/out/e2fsck"  # scripts/build-e2fsck.sh, e2fsprogs 1.47.3
E2FSCK_SHA256 = "fa1adeb5ce925a31e31323b382437569b39e0a572a70078946695be343331fe6"
# The RTC can be wrong after a dead battery; that alone must not force a full check or a stop.
E2FSCK_CONF = b"[options]\n\tbroken_system_clock = 1\n"
CMDLINE_EXTRA = "rdinit=/i14init"
# RAM-boot trial of the charging path: pretend power-off charging, with short waits.
CMDLINE_CHARGE_TEST = CMDLINE_EXTRA + " androidboot.mode=charger i14.autoboot=test"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bootimg = load("bootimg", ROOT / "scripts/build-stock-linux-bootimg.py")
cpio = load("cpio", ROOT / "initramfs/make-stock-charge-cpio.py")


def read_rescue_hash(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        flags = os.fstat(source.fileno())
        if (not stat.S_ISREG(flags.st_mode) or flags.st_uid != os.geteuid()
                or stat.S_IMODE(flags.st_mode) not in (0o400, 0o600)):
            raise ValueError("rescue password hash file must be private and owned by the build user")
        value = source.read(160).removesuffix(b"\n")
    if not re.fullmatch(rb"\$6\$(?:rounds=[1-9][0-9]{3,5}\$)?[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}", value):
        raise ValueError("rescue password must be configured with a SHA-512 crypt hash")
    return value


def entries(rescue_hash):
    busybox, e2fsck = BUSYBOX.read_bytes(), E2FSCK.read_bytes()
    if hashlib.sha256(busybox).hexdigest() != BUSYBOX_SHA256:
        raise ValueError("busybox.static hash mismatch")
    if hashlib.sha256(e2fsck).hexdigest() != E2FSCK_SHA256:
        raise ValueError("e2fsck hash mismatch")
    result = {n: (stat.S_IFDIR | 0o755, b"", 0, 0) for n in ("etc", "i14", "i14/bin", "i14/data", "i14/root")}
    result["etc/passwd"] = (stat.S_IFREG | 0o644, b"root:x:0:0:I14 rescue:/i14:/i14/bin/sh\n", 0, 0)
    result["etc/group"] = (stat.S_IFREG | 0o644, b"root:x:0:\n", 0, 0)
    result["etc/shadow"] = (stat.S_IFREG | 0o600, b"root:" + rescue_hash + b":0:0:99999:7:::\n", 0, 0)
    result["etc/securetty"] = (stat.S_IFREG | 0o600, b"ttyGS0\n", 0, 0)
    result["i14/rescue-login-enabled"] = (stat.S_IFREG | 0o600, b"", 0, 0)
    result["i14init"] = (stat.S_IFREG | 0o755, (ROOT / "initramfs/i14init").read_bytes(), 0, 0)
    result["i14/charge"] = (stat.S_IFREG | 0o644, (ROOT / "initramfs/i14charge").read_bytes(), 0, 0)
    result["i14/e2fsck.conf"] = (stat.S_IFREG | 0o644, E2FSCK_CONF, 0, 0)
    result["i14/bin/busybox"] = (stat.S_IFREG | 0o755, busybox, 0, 0)
    result["i14/bin/e2fsck"] = (stat.S_IFREG | 0o755, e2fsck, 0, 0)
    for name in APPLETS:
        result["i14/bin/" + name] = (stat.S_IFLNK | 0o777, b"busybox", 0, 0)
    return result


def stock_ramdisk(stock):
    ks, rs, page = struct.unpack_from("<I", stock, 8)[0], struct.unpack_from("<I", stock, 16)[0], 2048
    start = page + ks + (-ks % page)
    return stock[start:start + rs]


def build(stock, rescue_hash, cmdline=CMDLINE_EXTRA):
    ours = cpio.archive(entries(rescue_hash))
    android = stock_ramdisk(stock)
    ramdisk = android + b"\0" * (-len(android) % 4) + ours
    return bootimg.repack(stock, ramdisk, cmdline)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=pathlib.Path)
    p.add_argument("--rescue-password-hash-file", required=True, type=pathlib.Path,
                   help="private local file containing the rescue account's SHA-512 crypt hash")
    p.add_argument("--charge-test", action="store_true",
                   help="RAM-boot trial only: start in the charging path with short waits (never flash)")
    a = p.parse_args()
    try:
        rescue_hash = read_rescue_hash(a.rescue_password_hash_file)
    except ValueError as exc:
        p.error(str(exc))
    except OSError:
        p.error("unable to read the private rescue password hash file")
    stock = bootimg.STOCK.read_bytes()
    if hashlib.sha256(stock).hexdigest() != bootimg.STOCK_SHA256:
        sys.exit("stock boot backup hash mismatch")
    if a.output.exists():
        sys.exit("output exists")
    out = build(stock, rescue_hash, CMDLINE_CHARGE_TEST if a.charge_test else CMDLINE_EXTRA)
    fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(out)
    print(f"{a.output} {len(out)} bytes sha256={hashlib.sha256(out).hexdigest()}")


if __name__ == "__main__":
    main()
