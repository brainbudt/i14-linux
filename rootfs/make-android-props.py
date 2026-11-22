#!/usr/bin/env python3
"""Write a minimal Android 6 (bionic) property area for the MediaTek Wi-Fi tools.

Under Linux there is no Android property service. wmt_loader and 6620_launcher only
read the properties below; 6620_launcher waits until service.wcn.driver.ready=yes.
Bionic maps /dev/__properties__ read-only, so a pre-filled area is enough.
Values match what stock Android sets on this phone.

Layout (bionic M): 128-byte header (bytes_used, serial, magic, version, reserved),
then a trie of prop_bt nodes (u8 namelen, 3 pad, u32 prop/left/right/children, name)
and prop_info records (u32 serial = len(value) << 24, char value[92], name).
"""
import argparse
import pathlib
import struct

PROPS = {
    "service.wcn.driver.ready": "yes",
    "persist.mtk.wcn.combo.chipid": "0x0321",
    "service.wcn.coredump.mode": "0",
    "ro.build.type": "user",
}
SIZE = 128 * 1024
HEADER = 128
MAGIC, VERSION = 0x504F5250, 0xFC6ED0AB
VALUE_MAX = 92


class Area:
    def __init__(self):
        self.data = bytearray(SIZE - HEADER)
        self.used = 0
        self.node(b"")  # root

    def alloc(self, size):
        off = self.used
        self.used += (size + 3) & ~3
        if self.used > len(self.data):
            raise ValueError("property area full")
        return off

    def node(self, name):
        off = self.alloc(20 + len(name) + 1)
        self.data[off] = len(name)
        self.data[off + 20:off + 20 + len(name)] = name
        return off

    def field(self, off, index, value=None):  # 0=prop 1=left 2=right 3=children
        pos = off + 4 + 4 * index
        if value is None:
            return struct.unpack_from("<I", self.data, pos)[0]
        struct.pack_into("<I", self.data, pos, value)

    def child(self, parent, name):
        """Find or add `name` among the children of `parent` (a binary tree: shorter names
        first, then strcmp; smaller goes left), like bionic's find_prop_bt."""
        cur = self.field(parent, 3)
        if not cur:
            new = self.node(name)
            self.field(parent, 3, new)
            return new
        while True:
            other = bytes(self.data[cur + 20:cur + 20 + self.data[cur]])
            cmp = (len(name) > len(other)) - (len(name) < len(other)) or (name > other) - (name < other)
            if cmp == 0:
                return cur
            side = 1 if cmp < 0 else 2
            nxt = self.field(cur, side)
            if not nxt:
                new = self.node(name)
                self.field(cur, side, new)
                return new
            cur = nxt

    def add(self, key, value):
        if len(value) >= VALUE_MAX:
            raise ValueError(f"value too long: {key}")
        cur = 0
        for part in key.encode().split(b"."):
            cur = self.child(cur, part)
        info = self.alloc(4 + VALUE_MAX + len(key) + 1)
        struct.pack_into("<I", self.data, info, len(value) << 24)
        self.data[info + 4:info + 4 + len(value)] = value.encode()
        self.data[info + 4 + VALUE_MAX:info + 4 + VALUE_MAX + len(key)] = key.encode()
        self.field(cur, 0, info)

    def bytes(self):
        return struct.pack("<IIII", self.used, 0, MAGIC, VERSION).ljust(HEADER, b"\0") + bytes(self.data)


def read(blob):
    """Parse a property area into {name: value} (used by the tests and to check the stock file)."""
    used, _, magic, version = struct.unpack_from("<IIII", blob, 0)
    if (magic, version) != (MAGIC, VERSION):
        raise ValueError("not a bionic M property area")
    out = {}

    def walk(off):
        prop, left, right, children = struct.unpack_from("<IIII", blob, HEADER + off + 4)
        for nxt in (left, right, children):
            if nxt:
                walk(nxt)
        if prop:
            p = HEADER + prop
            value = blob[p + 4:p + 4 + VALUE_MAX].split(b"\0")[0].decode()
            out[blob[p + 4 + VALUE_MAX:].split(b"\0")[0].decode()] = value

    walk(0)
    return out


def build(props=PROPS):
    area = Area()
    for key, value in props.items():
        area.add(key, value)
    return area.bytes()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=pathlib.Path)
    a = p.parse_args()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_bytes(build())


if __name__ == "__main__":
    main()
