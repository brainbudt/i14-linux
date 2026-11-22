#!/usr/bin/env python3
"""Inspect and rebuild the I14 Pro Max "logo" partition (MT6735 LK boot splash).

Offline only: this tool reads and writes files on the Mac and never talks to the phone.
Python 3 standard library only (PNG reading and writing included).

  list    LOGO                 table of all images in a logo image
  extract LOGO DIR             every image as PNG, plus DIR/manifest.json
  replace LOGO INDEX PNG OUT   put PNG into image INDEX ("0" or "0,88") and write a new 8 MiB image
  verify  OUT                  re-parse an image and decode every picture

Format (backup 20261102-phase0; details in docs/research/SPLASH-2026-11-02.md):
  0x000  512-byte mkimg header: magic 0x58881688, u32 dsize, name "logo",
         ext magic 0x58891689, hdr_size 0x200, rest 0xFF
  0x200  logo table: u32 count, u32 total (== dsize), u32 off[count]; offsets count from 0x200
         image i is the zlib stream [0x200+off[i], 0x200+off[i+1]); the last ends at 0x200+total
         every stream inflates to raw pixels: bytes B,G,R,A (A = 0xFF), rows packed, upright
  align_up(0x200+dsize, 4 KiB)   (LK 0x41E02EAC: (dsize + 0x11FF) & ~0xFFF)
         256-byte RSA-2048-PSS-SHA256 signature over every byte before it
  rest   zeros up to the partition size (8 MiB)

The table stores no width or height. For the stock 139-image layout the sizes come from
LK/libshowlogo (720x1600 screens, 45x64 digits, 57x64 '%', 163x29 waves, 163x1 line).

Safety rules: inputs are opened read-only, outputs must not exist yet (no overwrite),
OUT may not be LOGO, and nothing is written on /Volumes/I14_BACKUP (the backup volume; checked by
device and inode on the opened output directory, so case or path aliases do not bypass it).
replace and verify expect the I14 layout of 139 images unless --any-layout is given.
"""
import argparse
import hashlib
import itertools
import json
import os
import struct
import sys
import zlib

PARTITION_SIZE = 0x800000
HDR_SIZE = 0x200
MKIMG_MAGIC = 0x58881688
EXT_MAGIC = 0x58891689
NAME = b"logo"
SIG_ALIGN = 0x1000
SIG_LEN = 256
ZLIB_LEVEL = 9
INFLATE_LIMIT = 16 * 1024 * 1024

STOCK_SHA256 = "919068673649da0af3650c8eb7ce8bc486fefb0fef0d883ad0b161d254bfb372"
STOCK_DSIZE = 2198142
STOCK_COUNT = 139
# Header words 0x28..0x4F of the stock image: maddr, mode, ext_magic, hdr_size,
# hdr_version, img_type, img_list_end, align_size, dsize_ext, maddr_ext.
STOCK_HEADER_WORDS = (0xFFFFFFFF, 0xFFFFFFFF, EXT_MAGIC, HDR_SIZE, 1, 0, 0, 0x10, 0, 0)

# The public RSA-2048 key that LK's sec_logo_check uses sits in lk.img at this file offset
# (e = 65537). Only its fingerprint is kept here; pass --lk to verify a signature.
LK_MODULUS_OFFSET = 0x43BA0
LK_MODULUS_SHA256 = "c44f426a899bd4136d3bbecdcfda6e04d5ed39a93f5907bd5785114a6e952662"
RSA_E = 65537
PSS_SALT_LEN = 32

SCREEN = (720, 1600)
FORMAT = "BGRA8888"  # bytes B,G,R,A per pixel = one little-endian ARGB8888 word
KNOWN_SIZES = {
    720 * 1600 * 4: (720, 1600),
    45 * 64 * 4: (45, 64),
    57 * 64 * 4: (57, 64),
    163 * 29 * 4: (163, 29),
    163 * 1 * 4: (163, 1),
}
PROTECTED = ["/Volumes/I14_BACKUP"]  # compared by device/inode, see refuse_protected()
COLOUR_CHUNKS = (b"iCCP", b"gAMA", b"cHRM", b"cICP")  # colour management this tool does not apply
PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_MAX_PIXELS = 4096 * 4096


class LogoError(Exception):
    """Invalid input, or a request this tool refuses."""


# ---------------------------------------------------------------- stock layout

def stock_geometry(index):
    """(width, height) of image `index` in the stock 139-image layout."""
    if 4 <= index <= 13:
        return (45, 64)
    if index == 14:
        return (57, 64)
    if 15 <= index <= 24:
        return (163, 29)
    if index == 36:
        return (163, 1)
    return SCREEN


def role(index, count):
    """What the phone uses image `index` for (verified for the stock 139-image layout)."""
    if count != STOCK_COUNT:
        return ""
    fixed = {0: "power-on logo (LK + Android, selector N=0)",
             1: "KPOC old-style charging background",
             2: "low battery (LK + Android)",
             3: "charger over-voltage (Android)",
             14: "KPOC '%' sign",
             35: "KPOC background",
             36: "KPOC fill line",
             37: "KPOC full battery (100%)",
             88: "kernel logo (Android only, N=0)"}
    if index in fixed:
        return fixed[index]
    if 4 <= index <= 13:
        return f"KPOC digit {index - 4}"
    if 15 <= index <= 24:
        return f"KPOC wave frame {index - 15}"
    if 25 <= index <= 34:
        return f"KPOC <10% frame {index - 25}"
    if 38 <= index <= 87:
        return f"alternative power-on logo N={index - 37}"
    if 89 <= index <= 138:
        return f"alternative kernel logo N={index - 88}"
    return ""


def geometry(index, count, decoded_size):
    """(width, height) for a decoded image, or None when the size does not fit."""
    if count == STOCK_COUNT:
        w, h = stock_geometry(index)
        return (w, h) if w * h * 4 == decoded_size else None
    return KNOWN_SIZES.get(decoded_size)


# ---------------------------------------------------------------- container

def align_up(value, align):
    return (value + align - 1) // align * align


def _first_nonzero(data, start, end=None):
    """Offset of the first non-zero byte in data[start:end], or None."""
    segment = data[start:end]
    rest = len(segment.lstrip(b"\0"))
    return None if rest == 0 else start + len(segment) - rest


class Logo:
    """A parsed logo partition image. Raises LogoError for anything LK would not accept."""

    def __init__(self, data):
        data = bytes(data)
        self.data = data
        if len(data) < HDR_SIZE + 8:
            raise LogoError(f"file is {len(data)} bytes, too small for a logo image")
        magic, self.dsize = struct.unpack_from("<II", data, 0)
        if magic != MKIMG_MAGIC:
            raise LogoError(f"mkimg magic is 0x{magic:08x}, expected 0x{MKIMG_MAGIC:08x}")
        name = data[8:40].split(b"\0", 1)[0]
        if name != NAME:
            raise LogoError(f"header name is {name!r}; LK requires 'logo'")
        self.header_words = struct.unpack_from("<10I", data, 0x28)
        ext_magic, hdr_size = self.header_words[2:4]
        if ext_magic != EXT_MAGIC or hdr_size != HDR_SIZE:
            raise LogoError(f"extended header 0x{ext_magic:08x}/0x{hdr_size:x} is not "
                            f"0x{EXT_MAGIC:08x}/0x{HDR_SIZE:x}; layout not supported")
        if self.dsize > len(data) - HDR_SIZE:
            raise LogoError(f"dsize {self.dsize} is larger than the image allows")
        if self.dsize < 8:
            raise LogoError(f"dsize {self.dsize} is too small for a logo table")
        self.count, self.total = struct.unpack_from("<II", data, HDR_SIZE)
        if self.total != self.dsize:
            raise LogoError(f"table total {self.total} differs from header dsize {self.dsize}")
        table = 8 + 4 * self.count
        if self.count == 0 or table > self.total:
            raise LogoError(f"image count {self.count} does not fit in {self.total} bytes")
        self.offsets = struct.unpack_from(f"<{self.count}I", data, HDR_SIZE + 8)
        if self.offsets[0] != table:
            raise LogoError(f"first offset is {self.offsets[0]}, expected {table} (right after the table)")
        ends = self.offsets[1:] + (self.total,)
        for i, (start, end) in enumerate(zip(self.offsets, ends)):
            if not start < end:
                raise LogoError(f"image {i}: offset {start} is not below the next one ({end})")
        self.streams = [data[HDR_SIZE + a:HDR_SIZE + b] for a, b in zip(self.offsets, ends)]

        end = HDR_SIZE + self.dsize
        self.sig_offset = align_up(end, SIG_ALIGN)
        self.signature = data[self.sig_offset:self.sig_offset + SIG_LEN]
        self.tail_problems = []
        bad = _first_nonzero(data, end, self.sig_offset)
        if bad is not None:
            self.tail_problems.append(f"non-zero byte at 0x{bad:x} between payload end and signature slot")
        if len(self.signature) != SIG_LEN:
            self.tail_problems.append("no room for the 256-byte signature slot")
        bad = _first_nonzero(data, self.sig_offset + SIG_LEN)
        if bad is not None:
            self.tail_problems.append(f"non-zero byte at 0x{bad:x} after the signature slot")

    def signed_range(self):
        return self.data[:self.sig_offset]

    def signature_state(self, modulus=None):
        """'empty', 'valid', 'INVALID', or 'present' (no key given)."""
        if not self.signature.strip(b"\0"):
            return "empty"
        if modulus is None:
            return "present"
        return "valid" if pss_verify(self.signed_range(), self.signature, modulus) else "INVALID"


def inflate(stream, limit=INFLATE_LIMIT):
    """Inflate one complete zlib stream; refuse truncated streams and trailing bytes."""
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(stream, limit + 1)
    except zlib.error as error:
        raise LogoError(f"zlib error: {error}") from None
    if len(raw) > limit or inflater.unconsumed_tail:
        raise LogoError(f"inflates to more than {limit} bytes")
    if not inflater.eof:
        raise LogoError("zlib stream is truncated")
    if inflater.unused_data:
        raise LogoError(f"{len(inflater.unused_data)} bytes after the end of the zlib stream")
    return raw


def images(logo):
    """Yield (info, raw pixels) for every image, decoding one at a time."""
    first_seen = {}
    for i, stream in enumerate(logo.streams):
        try:
            raw = inflate(stream)
        except LogoError as error:
            raise LogoError(f"image {i}: {error}") from None
        digest = hashlib.sha256(stream).hexdigest()
        size = geometry(i, logo.count, len(raw))
        info = {
            "index": i,
            "offset": HDR_SIZE + logo.offsets[i],
            "rel_offset": logo.offsets[i],
            "stored_size": len(stream),
            "decoded_size": len(raw),
            "format": FORMAT if size else "unknown",
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "dup_of": first_seen.get(digest),
            "role": role(i, logo.count),
            "stream_sha256": digest,
            "pixels_sha256": hashlib.sha256(raw).hexdigest(),
        }
        first_seen.setdefault(digest, i)
        yield info, raw


def rebuild(logo, streams, *, signature="zero", signature_bytes=None):
    """A complete image with these streams, padded to the size of the original image.

    The header is copied with only dsize changed. The 256-byte signature slot moves to
    align_up(0x200+dsize, 4 KiB). If the signed range is unchanged the original signature
    is kept. Otherwise: signature_bytes if given, else zeros ("zero") or the old, now
    invalid block ("stale").
    """
    if logo.tail_problems:
        raise LogoError("refusing to rebuild, the tail holds data this tool does not understand: "
                        + "; ".join(logo.tail_problems))
    count = len(streams)
    offsets, position = [], 8 + 4 * count
    for stream in streams:
        offsets.append(position)
        position += len(stream)
    total = position
    header = bytearray(logo.data[:HDR_SIZE])
    struct.pack_into("<I", header, 4, total)
    body = bytes(header) + struct.pack(f"<II{count}I", count, total, *offsets) + b"".join(streams)
    sig_offset = align_up(len(body), SIG_ALIGN)
    size = len(logo.data)
    if sig_offset + SIG_LEN > size:
        raise LogoError(f"does not fit: payload plus signature slot needs {sig_offset + SIG_LEN} bytes, "
                        f"the partition image is {size}")
    signed = body + bytes(sig_offset - len(body))
    if signed == logo.signed_range():
        sig = logo.signature
    elif signature_bytes is not None:
        if len(signature_bytes) != SIG_LEN:
            raise LogoError(f"signature must be {SIG_LEN} bytes, got {len(signature_bytes)}")
        sig = bytes(signature_bytes)
    elif signature == "stale":
        sig = logo.signature
    elif signature == "zero":
        sig = bytes(SIG_LEN)
    else:
        raise LogoError(f"unknown signature policy {signature!r}")
    return signed + sig + bytes(size - sig_offset - SIG_LEN)


def replace_images(logo, replacements, *, max_dsize=STOCK_DSIZE, signature="zero", signature_bytes=None):
    """Replace decoded images {index: BGRA bytes}; return (new image bytes, report).

    Unchanged images keep their compressed bytes. A replacement that equals the current
    pixels keeps the original stream too, so a no-op gives a byte-identical image.
    max_dsize=None lifts the "no larger than stock" limit (the partition limit stays).
    """
    streams = list(logo.streams)
    originals = {}
    for index, raw in replacements.items():
        if not 0 <= index < logo.count:
            raise LogoError(f"index {index} is out of range 0..{logo.count - 1}")
        original = inflate(logo.streams[index])
        size = geometry(index, logo.count, len(original))
        want = f"{size[0]}x{size[1]} {FORMAT}" if size else "same size"
        if len(raw) != len(original):
            raise LogoError(f"image {index} needs {len(original)} decoded bytes ({want}), got {len(raw)}")
        if raw[3::4].count(255) != len(raw) // 4:
            raise LogoError(f"image {index}: every alpha byte must be 0xFF")
        originals[index] = original
    changed = []
    for index in sorted(replacements):
        raw = bytes(replacements[index])
        if raw != originals[index]:
            streams[index] = zlib.compress(raw, ZLIB_LEVEL)
            changed.append(index)
    dsize = 8 + 4 * len(streams) + sum(map(len, streams))
    if max_dsize is not None and dsize > max_dsize:
        raise LogoError(f"payload would be {dsize} bytes, over the limit of {max_dsize} "
                        f"(stock size; LK's logo buffer size is unknown). Simplify the picture "
                        f"(flat colours, no noise) or pass --allow-growth deliberately")
    data = rebuild(logo, streams, signature=signature, signature_bytes=signature_bytes)

    # Self-check: re-parse; replaced images decode to the new pixels, all others are byte-identical.
    new = Logo(data)
    if new.count != logo.count or len(data) != len(logo.data):
        raise LogoError("self-check failed: count or size changed")
    for i, stream in enumerate(new.streams):
        if i in replacements:
            if inflate(stream) != bytes(replacements[i]):
                raise LogoError(f"self-check failed: image {i} does not decode to the new pixels")
        elif stream != logo.streams[i]:
            raise LogoError(f"self-check failed: image {i} changed")
    report = {
        "indices": sorted(replacements),
        "changed": changed,
        "dsize": new.dsize,
        "dsize_stock": STOCK_DSIZE,
        "dsize_headroom": STOCK_DSIZE - new.dsize,
        "stored_sizes": {str(i): len(new.streams[i]) for i in sorted(replacements)},
        "signature_offset": f"0x{new.sig_offset:x}",
        "signature": ("kept (signed range unchanged)" if data[:new.sig_offset] == logo.signed_range()
                      else "from file" if signature_bytes is not None
                      else "stale copy, INVALID for the new content" if signature == "stale"
                      else "zeros (unsigned)"),
        "unchanged_streams_identical": True,
    }
    return data, report


# ---------------------------------------------------------------- RSA-PSS

def _mgf1(seed, length):
    out = b""
    for counter in itertools.count():
        if len(out) >= length:
            return out[:length]
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()


def pss_verify(message, signature, modulus, e=RSA_E, salt_len=PSS_SALT_LEN):
    """RSASSA-PSS check (SHA-256, MGF1-SHA-256) of signature over message; modulus big-endian."""
    n = int.from_bytes(modulus, "big")
    k = len(modulus)
    s = int.from_bytes(signature, "big")
    if len(signature) != k or s >= n:
        return False
    mod_bits = n.bit_length()
    em_len = (mod_bits - 1 + 7) // 8
    em = pow(s, e, n).to_bytes(k, "big")
    if k > em_len:
        if em[0]:
            return False
        em = em[k - em_len:]
    h_len = 32
    if em_len < h_len + salt_len + 2 or em[-1] != 0xBC:
        return False
    masked, digest = em[:em_len - h_len - 1], em[em_len - h_len - 1:-1]
    unused = 8 * em_len - (mod_bits - 1)
    if unused and masked[0] >> (8 - unused):
        return False
    db = bytearray(a ^ b for a, b in zip(masked, _mgf1(digest, len(masked))))
    if unused:
        db[0] &= 0xFF >> unused
    pad = em_len - h_len - salt_len - 2
    if any(db[:pad]) or db[pad] != 1:
        return False
    salt = bytes(db[pad + 1:])
    m_hash = hashlib.sha256(message).digest()
    return hashlib.sha256(b"\0" * 8 + m_hash + salt).digest() == digest


def lk_modulus(lk_data):
    """The logo-signature modulus from lk.img, checked against its pinned fingerprint."""
    modulus = lk_data[LK_MODULUS_OFFSET:LK_MODULUS_OFFSET + 256]
    if hashlib.sha256(modulus).hexdigest() != LK_MODULUS_SHA256:
        raise LogoError(f"lk image has no known logo key at 0x{LK_MODULUS_OFFSET:x} (fingerprint mismatch)")
    return modulus


# ---------------------------------------------------------------- pixels and PNG

def bgra_to_rgb(raw):
    """B,G,R,A bytes -> (R,G,B bytes, alpha bytes)."""
    n = len(raw) // 4
    rgb = bytearray(n * 3)
    rgb[0::3] = raw[2::4]
    rgb[1::3] = raw[1::4]
    rgb[2::3] = raw[0::4]
    return bytes(rgb), raw[3::4]


def rgb_to_bgra(rgb):
    """R,G,B bytes -> B,G,R,A bytes with A = 0xFF."""
    n = len(rgb) // 3
    out = bytearray(n * 4)
    out[0::4] = rgb[2::3]
    out[1::4] = rgb[1::3]
    out[2::4] = rgb[0::3]
    out[3::4] = b"\xff" * n
    return bytes(out)


def flatten(rgba, background=(0, 0, 0)):
    """RGBA -> (RGB, number of blended pixels): alpha composited over the background colour."""
    n = len(rgba) // 4
    rgb = bytearray(n * 3)
    rgb[0::3] = rgba[0::4]
    rgb[1::3] = rgba[1::4]
    rgb[2::3] = rgba[2::4]
    alpha = rgba[3::4]
    if alpha.count(255) == n:
        return bytes(rgb), 0
    blended = 0
    for i, a in enumerate(alpha):
        if a != 255:
            for c in range(3):
                rgb[3 * i + c] = (rgb[3 * i + c] * a + background[c] * (255 - a) + 127) // 255
            blended += 1
    return bytes(rgb), blended


def _chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))


def png_encode(width, height, rgb, alpha=None, level=6):
    """8-bit PNG: RGB, or RGBA when alpha is given and not all 0xFF. Rows use filter 0."""
    if alpha is not None and alpha.count(255) != len(alpha):
        data = bytearray(width * height * 4)
        data[0::4], data[1::4], data[2::4], data[3::4] = rgb[0::3], rgb[1::3], rgb[2::3], alpha
        ctype, channels = 6, 4
    else:
        data, ctype, channels = rgb, 2, 3
    stride = width * channels
    if len(data) != stride * height:
        raise LogoError("pixel buffer does not match width x height")
    rows = b"".join(b"\0" + data[y * stride:(y + 1) * stride] for y in range(height))
    return (PNG_SIG + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(rows, level)) + _chunk(b"IEND", b""))


def _sub(line, bpp):
    out = bytearray(line)
    for c in range(bpp):
        out[c::bpp] = bytes(itertools.accumulate(line[c::bpp], lambda x, y: (x + y) & 255))
    return out


def _avg(line, prev, bpp):
    out = bytearray(line)
    for i in range(len(out)):
        left = out[i - bpp] if i >= bpp else 0
        out[i] = (out[i] + ((left + prev[i]) >> 1)) & 255
    return out


def _paeth(line, prev, bpp):
    out = bytearray(line)
    for i in range(len(out)):
        b = prev[i]
        if i >= bpp:
            a, c = out[i - bpp], prev[i - bpp]
        else:
            a = c = 0
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        out[i] = (out[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
    return out


def _unfilter(raw, height, stride, bpp):
    out = bytearray(height * stride)
    prev = bytes(stride)
    low7 = int.from_bytes(b"\x7f" * stride, "big")
    high = int.from_bytes(b"\x80" * stride, "big")
    for y in range(height):
        p = y * (stride + 1)
        kind, line = raw[p], raw[p + 1:p + 1 + stride]
        if kind == 0:
            cur = line
        elif kind == 1:
            cur = _sub(line, bpp)
        elif kind == 2:  # bytewise add without carries between bytes
            a, b = int.from_bytes(line, "big"), int.from_bytes(prev, "big")
            cur = (((a & low7) + (b & low7)) ^ ((a ^ b) & high)).to_bytes(stride, "big")
        elif kind == 3:
            cur = _avg(line, prev, bpp)
        elif kind == 4:
            cur = _paeth(line, prev, bpp)
        else:
            raise LogoError(f"PNG row {y}: unknown filter type {kind}")
        out[y * stride:(y + 1) * stride] = cur
        prev = cur
    return bytes(out)


def png_decode(data, ignored=None):
    """Decode a non-interlaced PNG to (width, height, RGBA8 bytes).

    Grey, RGB, palette, grey+alpha and RGBA at every PNG bit depth; tRNS honoured;
    16-bit samples keep their high byte. Colour profiles and gamma are not applied (values
    are used as sRGB); the names of such chunks (COLOUR_CHUNKS) are appended to `ignored`.
    """
    if data[:8] != PNG_SIG:
        raise LogoError("not a PNG file")
    pos, ihdr, plte, trns, idat, ended = 8, None, None, None, [], False
    while pos < len(data):
        if pos + 12 > len(data):
            raise LogoError("PNG is truncated")
        length, tag = struct.unpack_from(">I4s", data, pos)
        if pos + 12 + length > len(data):
            raise LogoError(f"PNG chunk {tag!r} is truncated")
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack_from(">I", data, pos + 8 + length)
        if zlib.crc32(tag + body) != crc:
            raise LogoError(f"PNG chunk {tag!r} has a bad CRC")
        pos += 12 + length
        if ihdr is None and tag != b"IHDR":
            raise LogoError("PNG does not start with IHDR")
        if tag == b"IHDR":
            if length != 13:
                raise LogoError("PNG IHDR has the wrong length")
            ihdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            plte = body
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            ended = True
            break
        elif tag in COLOUR_CHUNKS:
            if ignored is not None:
                ignored.append(tag.decode())
        elif not tag[0] & 0x20:
            raise LogoError(f"unsupported critical PNG chunk {tag!r}")
    if not ended:
        raise LogoError("PNG has no IEND chunk")
    width, height, depth, ctype, compression, filtering, interlace = ihdr
    allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if depth not in allowed.get(ctype, ()):
        raise LogoError(f"invalid PNG colour type {ctype} with bit depth {depth}")
    if compression or filtering:
        raise LogoError("unknown PNG compression or filter method")
    if interlace:
        raise LogoError("interlaced (Adam7) PNG is not supported; save it without interlacing")
    if not width or not height or width * height > PNG_MAX_PIXELS:
        raise LogoError(f"PNG size {width}x{height} is not supported")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    stride = (width * channels * depth + 7) // 8
    expected = height * (stride + 1)
    raw = inflate(b"".join(idat), limit=expected)
    if len(raw) != expected:
        raise LogoError(f"PNG image data is {len(raw)} bytes, expected {expected}")
    samples = _unfilter(raw, height, stride, max(1, channels * depth // 8))
    n = width * height

    if depth < 8:  # grey or palette: unpack to one byte per sample
        per, top = 8 // depth, (1 << depth) - 1
        table = [bytes((b >> (8 - depth * (k + 1))) & top for k in range(per)) for b in range(256)]
        samples = b"".join(b"".join(table[b] for b in samples[y * stride:(y + 1) * stride])[:width]
                           for y in range(height))
    alpha = None
    if trns is not None and ctype in (0, 2):  # one colour is transparent
        keys = struct.unpack(f">{len(trns) // 2}H", trns[:len(trns) // 2 * 2])
        if len(keys) != (1 if ctype == 0 else 3):
            raise LogoError("PNG tRNS chunk has the wrong length")
        size = channels * max(1, depth // 8)
        top = (1 << depth) - 1
        if all(k <= top for k in keys):
            key = struct.pack(f">{len(keys)}H", *keys) if depth == 16 else bytes(keys)
            alpha = bytes(0 if samples[i * size:(i + 1) * size] == key else 255 for i in range(n))
    if depth == 16:
        samples = samples[0::2]
    if ctype == 0 and depth < 8:
        top = (1 << depth) - 1
        samples = samples.translate(bytes(min(v, top) * 255 // top for v in range(256)))

    out = bytearray(n * 4)
    if ctype == 0:
        out[0::4] = out[1::4] = out[2::4] = samples
        out[3::4] = alpha or b"\xff" * n
    elif ctype == 2:
        out[0::4], out[1::4], out[2::4] = samples[0::3], samples[1::3], samples[2::3]
        out[3::4] = alpha or b"\xff" * n
    elif ctype == 4:
        out[0::4] = out[1::4] = out[2::4] = samples[0::2]
        out[3::4] = samples[1::2]
    elif ctype == 6:
        out[:] = samples
    else:
        if plte is None or len(plte) % 3 or not 0 < len(plte) <= 768:
            raise LogoError("PNG palette is missing or invalid")
        entries = len(plte) // 3
        if max(samples) >= entries:
            raise LogoError("PNG pixel uses a colour outside the palette")
        if trns is not None and len(trns) > entries:
            raise LogoError("PNG tRNS has more entries than the palette")
        pad = bytes(256 - entries)
        out[0::4] = samples.translate(plte[0::3] + pad)
        out[1::4] = samples.translate(plte[1::3] + pad)
        out[2::4] = samples.translate(plte[2::3] + pad)
        out[3::4] = samples.translate((trns or b"") + b"\xff" * (256 - len(trns or b"")))
    return width, height, bytes(out)


# ---------------------------------------------------------------- files

def read_file(path):
    with open(path, "rb") as handle:
        return handle.read()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def refuse_protected(dir_fd, shown):
    """Refuse when the directory open at dir_fd is a protected directory or lies under one.

    Compares device and inode numbers, not path text: a protected mount point blocks its
    whole volume, so differently cased names (/Volumes/I14_BACKUP), /System/Volumes/Data aliases
    and symlinks all hit the guard. The walk goes up with ".." from the descriptor itself.
    """
    marks = []
    for protected in PROTECTED:
        try:
            st = os.stat(protected)
        except OSError:
            continue  # not present (volume not mounted): nothing there to protect
        marks.append((protected, st.st_dev, st.st_ino, os.path.ismount(protected)))
    if not marks:
        return
    fd = os.dup(dir_fd)
    try:
        for _ in range(4096):
            st = os.fstat(fd)
            for protected, dev, ino, mount in marks:
                if st.st_dev == dev and (mount or st.st_ino == ino):
                    raise LogoError(f"refusing to write {shown}: it is on {protected} (read-only backup volume)")
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = parent
            up = os.fstat(fd)
            if (up.st_dev, up.st_ino) == (st.st_dev, st.st_ino):
                return  # reached /
        raise LogoError(f"cannot find the root above {shown}; refusing")
    finally:
        os.close(fd)


def open_existing_dir(path):
    """Descriptor of the deepest existing directory on the way to path (normally its parent)."""
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        try:
            return os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            if os.path.dirname(directory) == directory:
                raise
            directory = os.path.dirname(directory)


def check_new_path(path, inputs=()):
    """Refuse outputs that exist (or are an input) and outputs on the backup volume."""
    if os.path.lexists(path):
        for source in inputs:
            if os.path.exists(path) and os.path.samefile(path, source):
                raise LogoError(f"output {path} is the same file as input {source}; refusing")
        raise LogoError(f"{path} already exists; refusing to overwrite")
    fd = open_existing_dir(path)
    try:
        refuse_protected(fd, path)
    finally:
        os.close(fd)


def make_dirs_checked(path):
    """Create the missing directories of path one by one, each relative to an already opened
    and checked parent descriptor (O_NOFOLLOW), so swapping a parent for a symlink after a
    check cannot move the creation onto a protected volume."""
    path = os.path.abspath(path)
    missing = []
    existing = path
    while not os.path.isdir(existing):
        existing, name = os.path.split(existing)
        missing.append(name)
    fd = os.open(existing, os.O_RDONLY | os.O_DIRECTORY)
    try:
        refuse_protected(fd, path)
        for name in reversed(missing):
            try:
                os.mkdir(name, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            refuse_protected(fd, path)
    finally:
        os.close(fd)


def write_new(path, data):
    """Create path exclusively (never overwrite) and fsync it.

    The directory is opened and checked first and the file is created relative to that
    descriptor, so redirecting a parent symlink after the check cannot move the write.
    """
    directory, name = os.path.split(os.path.abspath(path))
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        refuse_protected(dir_fd, path)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            os.unlink(name, dir_fd=dir_fd)
            raise
    finally:
        os.close(dir_fd)


def load_logo(path, *, any_size=False):
    data = read_file(path)
    if len(data) != PARTITION_SIZE and not any_size:
        raise LogoError(f"{path} is {len(data)} bytes; a logo partition image is {PARTITION_SIZE}")
    return Logo(data)


# ---------------------------------------------------------------- commands

def summary_lines(logo, path, modulus=None):
    data = logo.data
    known = "  (stock backup 20261102-phase0)" if sha256(data) == STOCK_SHA256 else ""
    lines = [
        f"file      {path}  {len(data)} bytes  sha256 {sha256(data)}{known}",
        f"header    magic 0x{MKIMG_MAGIC:08x}  name 'logo'  dsize {logo.dsize}  "
        f"ext 0x{logo.header_words[2]:08x}  hdr_size 0x{logo.header_words[3]:x}",
        f"table     count {logo.count}  total {logo.total}  first offset {logo.offsets[0]}",
        f"signature slot 0x{logo.sig_offset:x} (256 bytes): {logo.signature_state(modulus)}",
        "tail      " + ("; ".join(logo.tail_problems) if logo.tail_problems else "zero padding, as expected"),
    ]
    return lines


def cmd_list(args):
    logo = load_logo(args.logo, any_size=True)
    rows = [info for info, _ in images(logo)]
    if args.json:
        print(json.dumps({"file": args.logo, "sha256": sha256(logo.data), "dsize": logo.dsize,
                          "count": logo.count, "signature_offset": logo.sig_offset,
                          "images": rows}, indent=1))
        return 0
    print("\n".join(summary_lines(logo, args.logo)))
    print()
    print(f"{'idx':>3} {'offset':>9} {'stored':>8} {'decoded':>8} {'WxH':<9} {'format':<8} {'dup':>4}  role")
    for r in rows:
        size = f"{r['width']}x{r['height']}" if r["width"] else "?"
        dup = "" if r["dup_of"] is None else f"#{r['dup_of']}"
        print(f"{r['index']:>3} 0x{r['offset']:07x} {r['stored_size']:>8} {r['decoded_size']:>8} "
              f"{size:<9} {r['format']:<8} {dup:>4}  {r['role']}")
    return 0


def cmd_extract(args):
    logo = load_logo(args.logo, any_size=True)
    check_new_path(os.path.join(args.dir, "manifest.json"), [args.logo])
    if ".." in os.path.normpath(args.dir).split(os.sep) or ".." in args.dir.split(os.sep):
        raise LogoError(f"{args.dir}: '..' is not allowed in the extract directory")
    if os.path.isdir(args.dir) and os.listdir(args.dir):
        raise LogoError(f"{args.dir} is not empty; extract into a new or empty directory")
    target = os.path.realpath(args.dir)  # symlinks resolved before anything is created
    check_new_path(os.path.join(target, "manifest.json"), [args.logo])
    make_dirs_checked(target)  # every file below is again written through a checked descriptor
    args.dir = target
    entries = []
    for info, raw in images(logo):
        if info["width"]:
            name = f"{info['index']:03d}_{info['width']}x{info['height']}.png"
            rgb, alpha = bgra_to_rgb(raw)
            blob = png_encode(info["width"], info["height"], rgb, alpha)
        else:
            name, blob = f"{info['index']:03d}.raw", raw
        path = os.path.join(args.dir, name)
        check_new_path(path, [args.logo])
        write_new(path, blob)
        info["file"] = name
        entries.append(info)
    manifest = {
        "source": os.path.abspath(args.logo),
        "source_sha256": sha256(logo.data),
        "source_size": len(logo.data),
        "dsize": logo.dsize,
        "count": logo.count,
        "pixel_format": "bytes B,G,R,A per pixel (little-endian ARGB8888), rows packed, upright",
        "signature_offset": logo.sig_offset,
        "signature_state": logo.signature_state(),
        "images": entries,
    }
    write_new(os.path.join(args.dir, "manifest.json"), (json.dumps(manifest, indent=1) + "\n").encode())
    print(f"{len(entries)} images and manifest.json written to {args.dir}")
    return 0


def parse_indices(text):
    try:
        indices = sorted({int(part) for part in text.split(",") if part.strip()})
    except ValueError:
        raise LogoError(f"INDEX must be a number or a comma list such as 0,88; got {text!r}") from None
    if not indices or indices[0] < 0:
        raise LogoError(f"invalid INDEX {text!r}")
    return indices


def parse_color(text):
    text = text.lstrip("#")
    if len(text) != 6:
        raise LogoError(f"--bg must be RRGGBB, got {text!r}")
    try:
        return tuple(bytes.fromhex(text))
    except ValueError:
        raise LogoError(f"--bg must be RRGGBB, got {text!r}") from None


def cmd_replace(args):
    for name, path in (("LOGO", args.logo), ("PNG", args.png)):
        if os.path.exists(args.out) and os.path.samefile(args.out, path):
            raise LogoError(f"OUT is the same file as {name}; refusing")
    check_new_path(args.out, [args.logo, args.png])
    if args.signature_file and not args.lk:
        raise LogoError("--signature-file needs --lk lk.img, so the signature is checked before writing")
    modulus = lk_modulus(read_file(args.lk)) if args.lk else None
    indices = parse_indices(args.index)
    logo = load_logo(args.logo)
    if logo.count != STOCK_COUNT and not args.any_layout:
        raise LogoError(f"{args.logo} has {logo.count} images; the I14 layout has {STOCK_COUNT} "
                        f"(--any-layout for other containers)")
    colour_chunks = []
    width, height, rgba = png_decode(read_file(args.png), colour_chunks)
    for index in indices:
        if not 0 <= index < logo.count:
            raise LogoError(f"index {index} is out of range 0..{logo.count - 1}")
        need = geometry(index, logo.count, len(inflate(logo.streams[index])))
        if need is None:
            raise LogoError(f"image {index} has an unknown size; refusing to guess its dimensions")
        if (width, height) != need:
            raise LogoError(f"PNG is {width}x{height}, image {index} needs exactly {need[0]}x{need[1]} "
                            f"(no scaling is done)")
    rgb, blended = flatten(rgba, parse_color(args.bg))
    raw = rgb_to_bgra(rgb)
    signature_bytes = read_file(args.signature_file) if args.signature_file else None
    data, report = replace_images(logo, {i: raw for i in indices},
                                  max_dsize=None if args.allow_growth else STOCK_DSIZE,
                                  signature=args.signature, signature_bytes=signature_bytes)
    # Check the whole result (every stream, not only the replaced ones) before writing.
    ok, lines = verify_logo(data, allow_growth=args.allow_growth, any_layout=args.any_layout)
    if not ok:
        raise LogoError("result fails verify, nothing written: "
                        + "; ".join(line[5:] for line in lines if line.startswith("FAIL")))
    state = Logo(data).signature_state(modulus)
    if signature_bytes is not None and state != "valid":
        raise LogoError(f"--signature-file is {state} for the new content with LK's key; nothing written")
    write_new(args.out, data)
    if sha256(read_file(args.out)) != sha256(data):
        raise LogoError(f"read-back of {args.out} differs from what was written")
    report.update({
        "input": os.path.abspath(args.logo), "input_sha256": sha256(logo.data),
        "png": os.path.abspath(args.png), "png_sha256": sha256(read_file(args.png)),
        "alpha_blended_pixels": blended,
        "png_colour_chunks_ignored": colour_chunks,
        "signature_check": state if modulus is not None else "not checked (no --lk)",
        "signature_state": Logo(data).signature_state(),
        "output": os.path.abspath(args.out), "output_sha256": sha256(data), "output_size": len(data),
    })
    print(json.dumps(report, indent=1))
    if colour_chunks:
        print(f"warning: the PNG has colour-management chunks ({', '.join(colour_chunks)}) that are not "
              "applied; pixel values are used as sRGB. Export as sRGB without a profile for exact colours.",
              file=sys.stderr)
    if report["changed"] and state != "valid":
        print("note: the logo is no longer signed. If LK enforces the logo signature "
              "(boot_chk_logo = 1) it hangs before fastboot. See SPLASH-2026-11-02.md.", file=sys.stderr)
    elif state != "valid":
        print(f"note: the result's signature is {state} ({report['signature_state']}), not verified as valid. "
              "If LK enforces the logo signature (boot_chk_logo = 1) an unsigned logo hangs it before "
              "fastboot. See SPLASH-2026-11-02.md.", file=sys.stderr)
    return 0


def verify_logo(data, *, modulus=None, against=None, allow_growth=False, any_size=False,
                any_layout=False, require_signature=False):
    """Check an image completely. Returns (ok, report lines).

    By default the image must have the I14 layout of 139 images (sizes fixed per index);
    any_layout=True accepts other counts and infers sizes from the decoded length.
    require_signature=True (needs modulus) fails unless the signature is valid.
    """
    lines, ok = [], True

    def fail(text):
        nonlocal ok
        ok = False
        lines.append("FAIL " + text)

    try:
        logo = Logo(data)
    except LogoError as error:
        return False, [f"FAIL parse: {error}"]
    lines.append(f"OK   parsed: {logo.count} images, dsize {logo.dsize}, signature slot 0x{logo.sig_offset:x}")
    if sha256(data) == STOCK_SHA256:
        lines.append("OK   this is the stock backup 20261102-phase0 (sha256 919068...b372)")
    if len(data) != PARTITION_SIZE:
        text = f"size is {len(data)}, the logo partition is {PARTITION_SIZE}"
        lines.append("WARN " + text) if any_size else fail(text)
    if logo.header_words != STOCK_HEADER_WORDS:
        lines.append("WARN header words 0x28..0x4F differ from stock")
    if data[0x50:HDR_SIZE] != b"\xff" * (HDR_SIZE - 0x50):
        lines.append("WARN header padding 0x50..0x1FF is not all 0xFF as in stock")
    for problem in logo.tail_problems:
        fail("tail: " + problem)
    if logo.dsize > STOCK_DSIZE:
        text = f"dsize {logo.dsize} > stock {STOCK_DSIZE}: LK's logo buffer size is unknown"
        lines.append("WARN " + text) if allow_growth else fail(text)
    if logo.count != STOCK_COUNT:
        text = f"{logo.count} images; the I14 layout has {STOCK_COUNT}"
        lines.append("WARN " + text) if any_layout else fail(text + " (--any-layout for other containers)")

    reference = list(against.streams) if against is not None else None
    if against is not None and against.count != logo.count:
        fail(f"image count {logo.count} differs from the reference ({against.count})")
        reference = None
    changed, not_opaque, broken = [], [], 0
    for i, stream in enumerate(logo.streams):
        try:
            raw = inflate(stream)
        except LogoError as error:
            fail(f"image {i}: {error}")
            broken += 1
            continue
        if geometry(i, logo.count, len(raw)) is None:
            expected = " (needs %dx%dx4)" % stock_geometry(i) if logo.count == STOCK_COUNT else ""
            fail(f"image {i}: {len(raw)} decoded bytes is not a known image size{expected}")
        if raw[3::4].count(255) != len(raw) // 4:
            not_opaque.append(i)
        if reference is not None and stream != reference[i]:
            try:
                if raw != inflate(reference[i]):
                    changed.append(i)
            except LogoError as error:
                fail(f"reference image {i}: {error}")
    if not broken:
        lines.append(f"OK   all {logo.count} zlib streams inflate completely with no trailing bytes")
    if not_opaque:
        lines.append(f"WARN images with alpha other than 0xFF: {not_opaque}")
    if reference is not None:
        lines.append(f"INFO images with different pixels than the reference: {changed or 'none'}")

    state = logo.signature_state(modulus)
    if require_signature and state != "valid":
        fail(f"signature: {state}; a valid signature is required (--require-signature)")
    elif state == "valid":
        lines.append("OK   signature: valid RSA-2048-PSS-SHA256 for this content (LK key)")
    elif state == "INVALID":
        lines.append("WARN signature: INVALID for this content. LK hangs if boot_chk_logo = 1")
    elif state == "empty":
        lines.append("WARN signature: empty (unsigned). LK hangs if boot_chk_logo = 1")
    else:
        lines.append("INFO signature: present, not checked (pass --lk lk.img)")
    return ok, lines


def cmd_verify(args):
    if args.require_signature and not args.lk:
        raise LogoError("--require-signature needs --lk lk.img")
    data = read_file(args.image)
    modulus = lk_modulus(read_file(args.lk)) if args.lk else None
    against = Logo(read_file(args.against)) if args.against else None
    ok, lines = verify_logo(data, modulus=modulus, against=against,
                            allow_growth=args.allow_growth, any_size=args.any_size,
                            any_layout=args.any_layout, require_signature=args.require_signature)
    print(f"verify {args.image}  sha256 {sha256(data)}")
    print("\n".join(lines))
    print("RESULT " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="i14-logo.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="table of all images")
    p.add_argument("logo")
    p.add_argument("--json", action="store_true", help="print the table as JSON")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("extract", help="all images as PNG plus manifest.json")
    p.add_argument("logo")
    p.add_argument("dir")
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser("replace", help="encode a PNG into one or more images and write a new image")
    p.add_argument("logo")
    p.add_argument("index", help="image index, or a comma list such as 0,88")
    p.add_argument("png")
    p.add_argument("out")
    p.add_argument("--bg", default="000000", help="colour under transparent pixels, RRGGBB (default 000000)")
    p.add_argument("--allow-growth", action="store_true",
                   help="allow a payload larger than stock (LK buffer size unknown; only the partition limit stays)")
    p.add_argument("--signature", choices=("zero", "stale"), default="zero",
                   help="signature slot when content changed: zeros (default) or the old, invalid block")
    p.add_argument("--signature-file", help="256-byte signature to place in the slot (needs --lk; must be valid)")
    p.add_argument("--lk", help="lk.img; checks the resulting signature with LK's logo key")
    p.add_argument("--any-layout", action="store_true", help="accept a LOGO that does not have the I14 139-image layout")
    p.set_defaults(func=cmd_replace)
    p = sub.add_parser("verify", help="re-parse an image and decode every picture")
    p.add_argument("image")
    p.add_argument("--lk", help="lk.img; checks the RSA-PSS signature with LK's logo key")
    p.add_argument("--against", help="reference logo image; lists images whose pixels differ")
    p.add_argument("--allow-growth", action="store_true", help="dsize above stock is a warning, not a failure")
    p.add_argument("--any-size", action="store_true", help="file size other than 8 MiB is a warning")
    p.add_argument("--any-layout", action="store_true", help="image count other than 139 is a warning")
    p.add_argument("--require-signature", action="store_true",
                   help="fail unless the signature is valid for this content (needs --lk)")
    p.set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (LogoError, OSError) as error:
        print(f"i14-logo: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
