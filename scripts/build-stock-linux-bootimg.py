#!/usr/bin/env python3
"""Repack the original stock boot.img with an own ramdisk (for `fastboot boot` only).

Header fields, cmdline and the kernel payload (gzip Image + appended stock DTB) are
copied byte for byte from the verified stock backup; only the ramdisk is replaced.
"""
import argparse
import hashlib
import pathlib
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
STOCK = ROOT / "backups/20261030T011515Z-native-serial/boot.img"
STOCK_SHA256 = "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492"
MAX_SIZE = 16 * 1024 * 1024


def pad(n, page):
    return (-n) % page


def parse(img):
    if img[:8] != b"ANDROID!":
        raise ValueError("missing Android boot magic")
    ks, kaddr, rs, raddr, ss, saddr, tags, page = struct.unpack_from("<8I", img, 8)
    if page != 2048 or ss != 0:
        raise ValueError("unexpected stock header (page size / second stage)")
    return {"kernel_size": ks, "ramdisk_size": rs, "page": page}


def repack(stock, ramdisk, cmdline_extra=""):
    h = parse(stock)
    page, ks = h["page"], h["kernel_size"]
    header = bytearray(stock[:page])
    struct.pack_into("<I", header, 16, len(ramdisk))
    if cmdline_extra:
        cmdline = header[64:576].split(b"\0")[0] + b" " + cmdline_extra.encode()
        if len(cmdline) >= 512:
            raise ValueError("cmdline too long")
        header[64:576] = cmdline.ljust(512, b"\0")
    kernel = stock[page:page + ks]
    sha = hashlib.sha1()  # mkbootimg v0 id: kernel, size, ramdisk, size, second, size
    for blob in (kernel, ramdisk, b""):
        sha.update(blob)
        sha.update(struct.pack("<I", len(blob)))
    header[576:608] = sha.digest() + b"\0" * 12
    out = bytes(header) + kernel + b"\0" * pad(ks, page) + ramdisk + b"\0" * pad(len(ramdisk), page)
    if len(out) > MAX_SIZE:
        raise ValueError("image exceeds the 16 MiB boot partition")
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ramdisk", type=pathlib.Path, help="gzip newc cpio")
    p.add_argument("output", type=pathlib.Path)
    a = p.parse_args()
    stock = STOCK.read_bytes()
    if hashlib.sha256(stock).hexdigest() != STOCK_SHA256:
        sys.exit("stock boot backup hash mismatch")
    ramdisk = a.ramdisk.read_bytes()
    if ramdisk[:2] != b"\x1f\x8b":
        sys.exit("ramdisk must be gzip")
    if a.output.exists():
        sys.exit("output exists")
    out = repack(stock, ramdisk)
    a.output.write_bytes(out)
    print(f"{a.output} {len(out)} bytes sha256={hashlib.sha256(out).hexdigest()}")


if __name__ == "__main__":
    main()
