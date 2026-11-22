"""PSF2 bitmap font (Terminus) with its unicode table, rendered to RGBA pixel rows."""
import gzip
import struct


class Font:
    def __init__(self, path):
        data = gzip.open(path).read()
        if data[:4] != b"\x72\xb5\x4a\x86":
            raise ValueError("not a PSF2 font")
        _, hdr, flags, count, self.size, self.h, self.w = struct.unpack_from("<7I", data, 4)
        self.rowbytes = (self.w + 7) // 8
        self.glyphs = data[hdr:hdr + count * self.size]
        self.map = {}
        if flags & 1:  # unicode table: per glyph, UTF-8 chars, then sequences after 0xFE, ended by 0xFF
            for index, entry in enumerate(data[hdr + count * self.size:].split(b"\xff")[:count]):
                for ch in entry.split(b"\xfe")[0].decode("utf-8", "ignore"):
                    self.map.setdefault(ch, index)
        else:
            self.map = {chr(i): i for i in range(min(count, 256))}
        self.cache = {}

    def has(self, ch):
        return ch in self.map

    def rows(self, ch, scale, color, bg=(0, 0, 0)):
        """Pixel rows (RGBA bytes) for one character, already scaled."""
        key = (ch, scale, color, bg)
        out = self.cache.get(key)
        if out is None:
            index = self.map.get(ch, self.map.get("?", 0))
            g = self.glyphs[index * self.size:(index + 1) * self.size]
            on, off = bytes(color) + b"\xff", bytes(bg) + b"\xff"
            out = []
            for y in range(self.h):
                bits = int.from_bytes(g[y * self.rowbytes:(y + 1) * self.rowbytes], "big")
                row = b"".join((on if bits >> (self.rowbytes * 8 - 1 - x) & 1 else off) * scale
                               for x in range(self.w))
                out.extend([row] * scale)
            if len(self.cache) > 20000:
                self.cache.clear()
            self.cache[key] = out
        return out
