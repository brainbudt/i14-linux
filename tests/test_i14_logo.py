"""Tests for scripts/i14-logo.py: synthetic containers and PNGs, plus the real backup if present.

Run: python3 -B tests/test_i14_logo.py   (offline; never touches the phone)
The real-backup tests skip cleanly when /Volumes/I14_BACKUP/i14-backups/20261102-phase0/logo.img is absent.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("i14_logo", ROOT / "scripts/i14-logo.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

BACKUP = Path("/Volumes/I14_BACKUP/i14-backups/20261102-phase0/logo.img")
LK = BACKUP.with_name("lk.img")
MiB = 1024 * 1024


# ------------------------------------------------------------- independent helpers

def pattern(width, height, seed):
    """Deterministic BGRA pixels (alpha 0xFF) with some structure."""
    rng = random.Random(seed)
    base = [rng.randrange(256) for _ in range(3)]
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes(((base[0] + x) & 255, (base[1] + y) & 255, (base[2] + x * y) & 255, 255))
    return bytes(out)


def container(raws, partition=0x10000, signature=b"\x5a" * 256):
    """A logo image written independently of the tool, in the stock layout."""
    streams = [zlib.compress(raw, 9) for raw in raws]
    count = len(streams)
    offsets, position = [], 8 + 4 * count
    for stream in streams:
        offsets.append(position)
        position += len(stream)
    header = bytearray(b"\xff" * 512)
    struct.pack_into("<II", header, 0, 0x58881688, position)
    header[8:40] = b"logo".ljust(32, b"\0")
    struct.pack_into("<10I", header, 0x28, 0xFFFFFFFF, 0xFFFFFFFF, 0x58891689, 0x200, 1, 0, 0, 0x10, 0, 0)
    body = bytes(header) + struct.pack(f"<II{count}I", count, position, *offsets) + b"".join(streams)
    slot = (len(body) + 0xFFF) // 0x1000 * 0x1000
    assert slot + 256 <= partition, "test container does not fit"
    data = bytearray(partition)
    data[:len(body)] = body
    data[slot:slot + 256] = signature
    return bytes(data)


def chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def filtered_png(width, height, rows, ctype, depth, bpp, extra=(), interlace=0, filters=None):
    """PNG whose row y uses filter filters[y] (default y % 5): a reference encoder for the decoder."""
    out, prev = bytearray(), bytes(len(rows[0]))
    for y, row in enumerate(rows):
        kind = (filters or [y % 5 for _ in rows])[y]
        line = bytearray()
        for i, value in enumerate(row):
            left = row[i - bpp] if i >= bpp else 0
            upleft = prev[i - bpp] if i >= bpp else 0
            pred = (0, left, prev[i], (left + prev[i]) // 2, paeth(left, prev[i], upleft))[kind]
            line.append((value - pred) & 255)
        out += bytes([kind]) + line
        prev = row
    ihdr = struct.pack(">IIBBBBB", width, height, depth, ctype, 0, 0, interlace)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + b"".join(chunk(t, b) for t, b in extra)
            + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b""))


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        yield out, err


# ------------------------------------------------------------- PNG

class PngTests(unittest.TestCase):
    def test_own_encoder_round_trip(self):
        raw = pattern(45, 64, 1)
        rgb, alpha = tool.bgra_to_rgb(raw)
        width, height, rgba = tool.png_decode(tool.png_encode(45, 64, rgb, alpha))
        self.assertEqual((width, height), (45, 64))
        flat, blended = tool.flatten(rgba)
        self.assertEqual(blended, 0)
        self.assertEqual(tool.rgb_to_bgra(flat), raw)

    def test_rgba_round_trip_keeps_alpha(self):
        rgb = bytes(range(256)) * 3
        alpha = bytes((i * 7) & 255 for i in range(256))
        _, _, rgba = tool.png_decode(tool.png_encode(16, 16, rgb, alpha))
        self.assertEqual(rgba[3::4], alpha)
        self.assertEqual(rgba[0::4], rgb[0::3])

    def test_every_filter_type_8_and_16_bit(self):
        rng = random.Random(7)
        for ctype, channels, depth in ((2, 3, 8), (6, 4, 8), (2, 3, 16), (0, 1, 8), (4, 2, 8)):
            with self.subTest(ctype=ctype, depth=depth):
                width, height = 13, 11
                size = channels * depth // 8
                rows = [bytes(rng.randrange(256) for _ in range(width * size)) for _ in range(height)]
                png = filtered_png(width, height, rows, ctype, depth, size)
                _, _, rgba = tool.png_decode(png)
                samples = b"".join(rows)[0::2] if depth == 16 else b"".join(rows)
                if ctype == 2:
                    self.assertEqual(rgba[0::4] + rgba[1::4] + rgba[2::4],
                                     samples[0::3] + samples[1::3] + samples[2::3])
                    self.assertEqual(rgba[3::4], b"\xff" * width * height)
                elif ctype == 6:
                    self.assertEqual(rgba, samples)
                elif ctype == 0:
                    self.assertEqual(rgba[1::4], samples)
                else:
                    self.assertEqual(rgba[2::4] + rgba[3::4], samples[0::2] + samples[1::2])

    def test_palette_with_transparency_and_low_bit_grey(self):
        # 2-bit palette, 5x2: indices 0..3, entry 1 half transparent
        plte = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9])
        indices = [[0, 1, 2, 3, 0], [3, 2, 1, 0, 1]]
        rows = [bytes([(r[0] << 6) | (r[1] << 4) | (r[2] << 2) | r[3], r[4] << 6]) for r in indices]
        png = filtered_png(5, 2, rows, 3, 2, 1, extra=((b"PLTE", plte), (b"tRNS", b"\xff\x80")))
        _, _, rgba = tool.png_decode(png)
        flat = [i for row in indices for i in row]
        self.assertEqual(list(rgba[0::4]), [plte[3 * i] for i in flat])
        self.assertEqual(list(rgba[3::4]), [0x80 if i == 1 else 0xFF for i in flat])
        # 1-bit grey with tRNS key 0 -> black transparent, white opaque
        png = filtered_png(9, 1, [bytes([0b10110001, 0b10000000])], 0, 1, 1,
                           extra=((b"tRNS", b"\x00\x00"),))
        _, _, rgba = tool.png_decode(png)
        self.assertEqual(list(rgba[0::4]), [255, 0, 255, 255, 0, 0, 0, 255, 255])
        self.assertEqual(list(rgba[3::4]), [255, 0, 255, 255, 0, 0, 0, 255, 255])

    def test_rgb_transparency_key_16_bit(self):
        rows = [bytes([0, 10, 0, 20, 0, 30, 1, 0, 2, 0, 3, 0])]
        png = filtered_png(2, 1, rows, 2, 16, 6, extra=((b"tRNS", struct.pack(">3H", 10, 20, 30)),))
        _, _, rgba = tool.png_decode(png)
        self.assertEqual(list(rgba[3::4]), [0, 255])
        self.assertEqual(list(rgba[0::4]), [0, 1])

    def test_colour_order_is_b_g_r_a(self):
        self.assertEqual(tool.rgb_to_bgra(b"\xff\x00\x00\x00\x80\xff"), b"\x00\x00\xff\xff\xff\x80\x00\xff")
        self.assertEqual(tool.bgra_to_rgb(b"\x00\x00\xff\xff"), (b"\xff\x00\x00", b"\xff"))

    def test_alpha_is_composited_over_background(self):
        rgba = bytes([200, 100, 0, 255, 200, 100, 0, 0, 200, 100, 0, 128])
        rgb, blended = tool.flatten(rgba, (10, 20, 30))
        self.assertEqual(blended, 2)
        self.assertEqual(list(rgb), [200, 100, 0, 10, 20, 30, 105, 60, 15])

    def test_refuses_interlaced_bad_crc_and_truncated(self):
        good = filtered_png(4, 4, [bytes(12)] * 4, 2, 8, 3)
        with self.assertRaisesRegex(tool.LogoError, "interlaced"):
            tool.png_decode(filtered_png(4, 4, [bytes(12)] * 4, 2, 8, 3, interlace=1))
        broken = bytearray(good)
        broken[20] ^= 1
        with self.assertRaisesRegex(tool.LogoError, "CRC"):
            tool.png_decode(bytes(broken))
        with self.assertRaises(tool.LogoError):
            tool.png_decode(good[:-20])
        with self.assertRaisesRegex(tool.LogoError, "not a PNG"):
            tool.png_decode(b"GIF89a")


# ------------------------------------------------------------- synthetic container

class SyntheticContainerTests(unittest.TestCase):
    def setUp(self):
        self.raws = [pattern(45, 64, 1), pattern(57, 64, 2), pattern(163, 29, 3), pattern(163, 1, 4)]
        self.data = container(self.raws)
        self.logo = tool.Logo(self.data)

    def test_parse_fields(self):
        logo = self.logo
        self.assertEqual(logo.count, 4)
        self.assertEqual(logo.offsets[0], 8 + 16)
        self.assertEqual(logo.total, logo.dsize)
        self.assertEqual(logo.sig_offset, 0x1000 * ((0x200 + logo.dsize + 0xFFF) // 0x1000))
        self.assertEqual(logo.signature, b"\x5a" * 256)
        self.assertEqual(logo.tail_problems, [])
        infos = [info for info, _ in tool.images(logo)]
        self.assertEqual([(i["width"], i["height"]) for i in infos], [(45, 64), (57, 64), (163, 29), (163, 1)])
        self.assertTrue(all(i["format"] == "BGRA8888" for i in infos))

    def test_identity_rebuild_is_byte_identical(self):
        self.assertEqual(tool.rebuild(self.logo, self.logo.streams), self.data)
        same, report = tool.replace_images(self.logo, {1: self.raws[1]})
        self.assertEqual(same, self.data)
        self.assertEqual(report["changed"], [])
        self.assertTrue(report["signature"].startswith("kept"))

    def test_replacing_one_image_changes_only_that_image(self):
        new_raw = pattern(57, 64, 99)
        data, report = tool.replace_images(self.logo, {1: new_raw})
        new = tool.Logo(data)
        self.assertEqual(len(data), len(self.data))
        self.assertEqual(report["changed"], [1])
        self.assertEqual(new.dsize, new.total)
        self.assertEqual(data[:4] + data[8:0x200], self.data[:4] + self.data[8:0x200])
        for i in (0, 2, 3):
            self.assertEqual(new.streams[i], self.logo.streams[i])
            self.assertEqual(tool.inflate(new.streams[i]), self.raws[i])
        self.assertEqual(tool.inflate(new.streams[1]), new_raw)
        self.assertEqual(new.signature, bytes(256))
        self.assertEqual(new.tail_problems, [])
        ok, lines = tool.verify_logo(data, against=self.logo, any_size=True, any_layout=True)
        self.assertTrue(ok, lines)
        self.assertIn("INFO images with different pixels than the reference: [1]", lines)

    def test_signature_policies(self):
        new_raw = pattern(45, 64, 5)
        stale, _ = tool.replace_images(self.logo, {0: new_raw}, signature="stale")
        self.assertEqual(tool.Logo(stale).signature, b"\x5a" * 256)
        given, report = tool.replace_images(self.logo, {0: new_raw}, signature_bytes=b"\x01" * 256)
        self.assertEqual(tool.Logo(given).signature, b"\x01" * 256)
        self.assertEqual(report["signature"], "from file")
        with self.assertRaises(tool.LogoError):
            tool.replace_images(self.logo, {0: new_raw}, signature_bytes=b"\x01" * 255)

    def test_size_and_dimension_refusals(self):
        with self.assertRaisesRegex(tool.LogoError, "needs 11520 decoded bytes"):
            tool.replace_images(self.logo, {0: pattern(57, 64, 1)})
        with self.assertRaisesRegex(tool.LogoError, "out of range"):
            tool.replace_images(self.logo, {4: self.raws[0]})
        with self.assertRaisesRegex(tool.LogoError, "alpha"):
            tool.replace_images(self.logo, {0: self.raws[0][:-1] + b"\x00"})
        noise = tool.rgb_to_bgra(random.Random(1).randbytes(163 * 29 * 3))
        with self.assertRaisesRegex(tool.LogoError, "over the limit"):
            tool.replace_images(self.logo, {2: noise}, max_dsize=self.logo.dsize)
        tight = tool.Logo(container(self.raws, partition=self.logo.sig_offset + 256))
        self.assertEqual(tool.rebuild(tight, tight.streams), tight.data)
        with self.assertRaisesRegex(tool.LogoError, "does not fit"):
            tool.replace_images(tight, {2: noise}, max_dsize=None)

    def test_parse_rejects_corrupt_containers(self):
        cases = {
            "magic": lambda d: d.__setitem__(slice(0, 4), b"\0\0\0\0"),
            "name": lambda d: d.__setitem__(slice(8, 12), b"LOGO"),
            "total": lambda d: struct.pack_into("<I", d, 0x204, 1234),
            "offset": lambda d: struct.pack_into("<I", d, 0x20C, 5),
            "extended": lambda d: struct.pack_into("<I", d, 0x30, 0),
        }
        for name, damage in cases.items():
            with self.subTest(name=name), self.assertRaises(tool.LogoError):
                data = bytearray(self.data)
                damage(data)
                tool.Logo(bytes(data))

    def test_trailing_bytes_and_unknown_tail_are_caught(self):
        raws = self.raws
        streams = [zlib.compress(r, 9) for r in raws]
        streams[1] += b"\0\0"
        logo = tool.Logo(self._from_streams(streams))
        ok, lines = tool.verify_logo(logo.data, any_size=True)
        self.assertFalse(ok)
        self.assertTrue(any("image 1" in line and "after the end" in line for line in lines), lines)
        data = bytearray(self.data)
        data[-1] = 1
        logo = tool.Logo(bytes(data))
        self.assertTrue(logo.tail_problems)
        with self.assertRaisesRegex(tool.LogoError, "tail"):
            tool.replace_images(logo, {0: pattern(45, 64, 8)})

    def test_wrong_decoded_size_fails_verify(self):
        data = self._from_streams([zlib.compress(b"\0\0\0\xff" * 100, 9)])
        ok, lines = tool.verify_logo(data, any_size=True)
        self.assertFalse(ok)
        self.assertTrue(any("not a known image size" in line for line in lines), lines)

    def test_non_i14_layout_fails_verify_by_default(self):
        # review C-S3: one 45x64 sprite in an 8 MiB container used to verify OK
        data = container([pattern(45, 64, 1)], partition=8 * MiB)
        ok, lines = tool.verify_logo(data)
        self.assertFalse(ok)
        self.assertTrue(any(line.startswith("FAIL 1 images; the I14 layout has 139") for line in lines), lines)
        self.assertTrue(tool.verify_logo(data, any_layout=True)[0])

    def test_require_signature_fails_unless_valid(self):
        # review A-S2: an empty or invalid signature is a FAIL with require_signature
        fake_key = b"\xff" * 256
        ok, lines = tool.verify_logo(self.data, any_size=True, any_layout=True, modulus=fake_key,
                                     require_signature=True)
        self.assertFalse(ok)
        self.assertIn("FAIL signature: INVALID; a valid signature is required (--require-signature)", lines)
        unsigned, _ = tool.replace_images(self.logo, {0: pattern(45, 64, 5)})
        ok, lines = tool.verify_logo(unsigned, any_size=True, any_layout=True, modulus=fake_key,
                                     require_signature=True)
        self.assertFalse(ok)
        self.assertIn("FAIL signature: empty; a valid signature is required (--require-signature)", lines)
        self.assertTrue(tool.verify_logo(unsigned, any_size=True, any_layout=True, modulus=fake_key)[0])

    def _from_streams(self, streams):
        count = len(streams)
        offsets, position = [], 8 + 4 * count
        for stream in streams:
            offsets.append(position)
            position += len(stream)
        data = bytearray(self.data[:0x200]) + bytearray(0x10000 - 0x200)
        struct.pack_into("<I", data, 4, position)
        payload = struct.pack(f"<II{count}I", count, position, *offsets) + b"".join(streams)
        data[0x200:0x200 + len(payload)] = payload
        return bytes(data)


# ------------------------------------------------------------- CLI on synthetic 8 MiB images

class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.raws = [pattern(45, 64, 1), pattern(57, 64, 2)]
        self.logo = self.dir / "logo.img"
        self.logo.write_bytes(container(self.raws, partition=8 * MiB))
        self.before = hashlib.sha256(self.logo.read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.logo.read_bytes()).hexdigest(), self.before, "input changed")
        self.tmp.cleanup()

    def png(self, name, width, height, seed):
        rgb, _ = tool.bgra_to_rgb(pattern(width, height, seed))
        path = self.dir / name
        path.write_bytes(tool.png_encode(width, height, rgb))
        return path

    def run_tool(self, *args):
        with quiet() as (out, err):
            code = tool.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def test_extract_replace_verify(self):
        code, _, _ = self.run_tool("extract", self.logo, self.dir / "x")
        self.assertEqual(code, 0)
        manifest = json.loads((self.dir / "x/manifest.json").read_text())
        self.assertEqual([(i["index"], i["width"], i["height"], i["format"]) for i in manifest["images"]],
                         [(0, 45, 64, "BGRA8888"), (1, 57, 64, "BGRA8888")])
        self.assertEqual(manifest["images"][1]["offset"], 0x200 + manifest["images"][1]["rel_offset"])
        same = self.dir / "same.img"
        code, out, _ = self.run_tool("replace", self.logo, 1, self.dir / "x/001_57x64.png", same, "--any-layout")
        self.assertEqual(code, 0)
        self.assertEqual(same.read_bytes(), self.logo.read_bytes())
        self.assertEqual(json.loads(out)["changed"], [])
        changed = self.dir / "changed.img"
        code, out, err = self.run_tool("replace", self.logo, 1, self.png("new.png", 57, 64, 3), changed,
                                       "--any-layout")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["changed"], [1])
        self.assertIn("no longer signed", err)
        code, out, _ = self.run_tool("verify", changed, "--against", self.logo, "--any-layout")
        self.assertEqual(code, 0, out)
        self.assertIn("reference: [1]", out)
        code, out, _ = self.run_tool("verify", changed)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL 2 images; the I14 layout has 139", out)
        code, _, err = self.run_tool("verify", changed, "--require-signature")
        self.assertEqual(code, 1)
        self.assertIn("--require-signature needs --lk", err)
        code, _, err = self.run_tool("extract", self.logo, self.dir / "x")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_replace_refusals_write_nothing(self):
        good = self.png("good.png", 57, 64, 3)
        cases = [
            (("replace", self.logo, 1, good, self.logo), "same file"),
            (("replace", self.logo, 1, good, good), "same file"),
            (("replace", self.logo, 1, self.png("big.png", 58, 64, 3), self.dir / "o1.img"), "needs exactly 57x64"),
            (("replace", self.logo, 0, good, self.dir / "o2.img"), "needs exactly 45x64"),
            (("replace", self.logo, 7, good, self.dir / "o3.img"), "out of range"),
            (("replace", self.logo, 1, good, self.dir / "o5.img", "--signature-file", good), "needs --lk"),
        ]
        for args, message in cases:
            with self.subTest(message=message):
                code, _, err = self.run_tool(*args, "--any-layout")
                self.assertEqual(code, 1)
                self.assertIn(message, err)
        with self.subTest(message="layout"):
            code, _, err = self.run_tool("replace", self.logo, 1, good, self.dir / "o6.img")
            self.assertEqual(code, 1)
            self.assertIn("2 images; the I14 layout has 139", err)
        existing = self.dir / "exists.img"
        existing.write_bytes(b"keep")
        code, _, err = self.run_tool("replace", self.logo, 1, good, existing)
        self.assertEqual((code, existing.read_bytes()), (1, b"keep"))
        self.assertIn("already exists", err)
        small = self.dir / "small.img"
        small.write_bytes(container(self.raws))
        code, _, err = self.run_tool("replace", small, 1, good, self.dir / "o4.img")
        self.assertIn("a logo partition image is 8388608", err)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["big.png", "exists.img", "good.png", "logo.img", "small.img"])

    def test_corrupt_unselected_image_is_refused(self):
        # review C-S2: a broken image that is not replaced must not reach a written output
        data = bytearray(self.logo.read_bytes())
        end_of_0 = 0x200 + struct.unpack_from("<I", data, 0x200 + 12)[0]
        data[end_of_0 - 1] ^= 0xFF  # last Adler-32 byte of image 0
        broken = self.dir / "broken.img"
        broken.write_bytes(bytes(data))
        out = self.dir / "out.img"
        code, _, err = self.run_tool("replace", broken, 1, self.png("new.png", 57, 64, 3), out, "--any-layout")
        self.assertEqual(code, 1)
        self.assertIn("result fails verify, nothing written: image 0: zlib error", err)
        self.assertFalse(out.exists())

    def protected_vault(self):
        """A temporary directory added to tool.PROTECTED for this test (the real entries stay)."""
        vault = self.dir / "Vault"
        (vault / "sub").mkdir(parents=True)
        patcher = mock.patch.object(tool, "PROTECTED", tool.PROTECTED + [str(vault)])
        patcher.start()
        self.addCleanup(patcher.stop)
        return vault

    def test_protected_directory_aliases_are_refused(self):
        # reviews C-S1 / A-S3: the guard compares device and inode, not path text
        vault = self.protected_vault()
        (self.dir / "link").symlink_to(vault)
        good = self.png("good.png", 57, 64, 3)
        targets = [vault / "x.img", vault / "sub/x.img", self.dir / "link/x.img", self.dir / "link/sub/new/x.img"]
        if (self.dir / "VAULT").exists():  # case-insensitive file system, as on this Mac
            targets += [self.dir / "VAULT/x.img", self.dir / "vault/sub/x.img"]
        for target in targets:
            with self.subTest(target=str(target)):
                code, _, err = self.run_tool("replace", self.logo, 1, good, target, "--any-layout")
                self.assertEqual(code, 1)
                self.assertIn("backup volume", err)
                code, _, err = self.run_tool("extract", self.logo, target.parent / "dump")
                self.assertEqual(code, 1)
                self.assertIn("backup volume", err)
        self.assertEqual(sorted(p.name for p in vault.rglob("*")), ["sub"])

    def test_redirect_after_check_cannot_move_the_write(self):
        # review C-S4: the file is created relative to the checked directory descriptor
        vault = self.protected_vault()
        safe = self.dir / "safe"
        safe.mkdir()
        link = self.dir / "outdir"
        link.symlink_to(safe)
        real_check = tool.refuse_protected

        def check_then_redirect(dir_fd, shown):
            real_check(dir_fd, shown)
            link.unlink()
            link.symlink_to(vault)  # an attacker re-points the parent after the check

        with mock.patch.object(tool, "refuse_protected", check_then_redirect):
            tool.write_new(str(link / "x.img"), b"data")
        self.assertEqual((safe / "x.img").read_bytes(), b"data")
        self.assertEqual(sorted(p.name for p in vault.rglob("*")), ["sub"])
        with self.assertRaisesRegex(tool.LogoError, "backup volume"):
            tool.write_new(str(link / "y.img"), b"data")

    def test_extract_dotdot_through_symlink_is_refused(self):
        # round 2 S1: /tmp/safe/link/../new must not create a directory on the protected volume
        vault = self.protected_vault()
        (vault / "existing").mkdir()
        safe = self.dir / "safe"
        safe.mkdir()
        (safe / "link").symlink_to(vault / "existing")
        code, _, err = self.run_tool("extract", self.logo, f"{safe}/link/../new")
        self.assertEqual(code, 1)
        self.assertIn("..", err)
        self.assertFalse((vault / "new").exists())
        # a symlinked extract directory resolves before anything is created
        (safe / "into_vault").symlink_to(vault)
        code, _, err = self.run_tool("extract", self.logo, safe / "into_vault" / "dump")
        self.assertEqual(code, 1)
        self.assertIn("backup volume", err)
        self.assertEqual(sorted(p.name for p in vault.rglob("*")), ["existing", "sub"])

    def test_parent_swap_before_mkdir_cannot_create_in_vault(self):
        # round 3 S3: swap a parent for a symlink to the vault right before directories are made
        vault = self.protected_vault()
        safe = self.dir / "safe"
        safe.mkdir()
        real_mkdir = os.mkdir
        swapped = {"done": False}

        def swap_then_mkdir(name, mode=0o777, *, dir_fd=None):
            if not swapped["done"]:
                swapped["done"] = True
                safe.rename(self.dir / "safe-moved")
                safe.symlink_to(vault)  # 'safe' now points into the vault
            return real_mkdir(name, mode, dir_fd=dir_fd)

        with mock.patch.object(tool.os, "mkdir", swap_then_mkdir):
            try:
                tool.make_dirs_checked(str(safe / "new" / "dump"))
            except (tool.LogoError, OSError):
                pass
        self.assertTrue(swapped["done"])
        self.assertEqual(sorted(p.name for p in vault.rglob("*")), ["sub"])

    def test_noop_replace_of_unsigned_input_warns(self):
        # round 2 S2: report the real signature state even when no pixel changed
        data = bytearray(self.logo.read_bytes())
        sig = tool.Logo(bytes(data)).sig_offset
        data[sig:sig + 256] = bytes(256)  # an unsigned input
        unsigned = self.dir / "unsigned.img"
        unsigned.write_bytes(bytes(data))
        code, _, _ = self.run_tool("extract", unsigned, self.dir / "x")
        self.assertEqual(code, 0)
        out = self.dir / "same.img"
        code, report, err = self.run_tool("replace", unsigned, 1, self.dir / "x/001_57x64.png", out, "--any-layout")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(report)["changed"])
        self.assertEqual(json.loads(report)["signature_state"], "empty")
        self.assertIn("signature is empty", err)

    def test_colour_chunks_are_reported(self):
        # review A-S6: iCCP/gAMA are not applied, but no longer silently
        rgb, _ = tool.bgra_to_rgb(pattern(57, 64, 3))
        rows = [rgb[y * 57 * 3:(y + 1) * 57 * 3] for y in range(64)]
        png = self.dir / "p3.png"
        png.write_bytes(filtered_png(57, 64, rows, 2, 8, 3, filters=[0] * 64, extra=(
            (b"iCCP", b"Display P3\0\0" + zlib.compress(b"profile")), (b"gAMA", struct.pack(">I", 45455)))))
        code, out, err = self.run_tool("replace", self.logo, 1, png, self.dir / "out.img", "--any-layout")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["png_colour_chunks_ignored"], ["iCCP", "gAMA"])
        self.assertIn("warning: the PNG has colour-management chunks (iCCP, gAMA)", err)
        code, out, err = self.run_tool("replace", self.logo, 1, self.png("plain.png", 57, 64, 4),
                                       self.dir / "out2.img", "--any-layout")
        self.assertEqual((code, json.loads(out)["png_colour_chunks_ignored"]), (0, []))
        self.assertNotIn("colour-management", err)


@unittest.skipUnless(os.path.ismount("/Volumes/I14_BACKUP"), "/Volumes/I14_BACKUP not mounted")
class BackupVolumeGuardTests(unittest.TestCase):
    """Only check_new_path is called here: it never writes, so a failure cannot touch the volume."""

    def test_case_and_firmlink_aliases_are_refused(self):
        aliases = ["/Volumes/I14_BACKUP/never.img", "/Volumes/I14_BACKUP/never.img", "/volumes/i14_backup/never.img",
                   "/System/Volumes/Data/Volumes/I14_BACKUP/never.img", "/Volumes/I14_BACKUP/i14-backups/never/x.img"]
        for alias in aliases:
            with self.subTest(alias=alias):
                if not os.path.isdir(alias.split("/never")[0]):
                    self.skipTest(f"{alias} does not resolve on this file system")
                with self.assertRaisesRegex(tool.LogoError, "backup volume"):
                    tool.check_new_path(alias)


# ------------------------------------------------------------- real backup

@unittest.skipUnless(BACKUP.is_file(), f"{BACKUP} not present")
class RealBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = BACKUP.read_bytes()
        cls.logo = tool.Logo(cls.data)

    def test_structure_matches_the_authoritative_table(self):
        logo = self.logo
        self.assertEqual(len(self.data), 8 * MiB)
        self.assertEqual(hashlib.sha256(self.data).hexdigest(), tool.STOCK_SHA256)
        self.assertEqual((logo.count, logo.dsize, logo.offsets[0], logo.sig_offset), (139, 2198142, 564, 0x219000))
        self.assertEqual(logo.tail_problems, [])
        infos = [info for info, _ in tool.images(logo)]
        spot = {0: (0x434, 57900), 1: (0xE660, 874530), 36: (0x13BD59, 439), 88: (0x1A6F20, 57900),
                138: (0x2165F2, 9868)}
        for index, (offset, stored) in spot.items():
            self.assertEqual((infos[index]["offset"], infos[index]["stored_size"]), (offset, stored))
        self.assertEqual(sum(1 for i in infos if (i["width"], i["height"]) == (720, 1600)), 117)
        self.assertTrue(all(i["format"] == "BGRA8888" for i in infos))
        self.assertEqual(infos[88]["dup_of"], 0)
        self.assertEqual([infos[i]["dup_of"] for i in range(89, 139)], list(range(38, 88)))
        self.assertTrue(all(i["dup_of"] is None for i in infos[:88]))
        ok, lines = tool.verify_logo(self.data)
        self.assertTrue(ok, lines)

    def test_boot_logo_colours_read_as_white_with_orange_text(self):
        rgb, alpha = tool.bgra_to_rgb(tool.inflate(self.logo.streams[0]))
        pixels = [rgb[i:i + 3] for i in range(0, len(rgb), 3)]
        self.assertEqual(pixels[0], b"\xff\xff\xff")
        self.assertGreater(pixels.count(bytes.fromhex("ff6200")), 2000)
        self.assertEqual(pixels.count(bytes.fromhex("0062ff")), 0)
        self.assertEqual(alpha.count(255), 720 * 1600)

    def test_identity_rebuild_is_byte_identical(self):
        self.assertEqual(tool.rebuild(self.logo, self.logo.streams), self.data)

    def test_recompressed_rebuild_is_identical_or_equivalent(self):
        streams = [zlib.compress(tool.inflate(s), 9) for s in self.logo.streams]
        data = tool.rebuild(self.logo, streams)
        if data != self.data:  # a different zlib build may compress differently
            new = tool.Logo(data)
            self.assertEqual(data[:4] + data[8:0x200], self.data[:4] + self.data[8:0x200])
            for a, b in zip(new.streams, self.logo.streams):
                self.assertEqual(tool.inflate(a), tool.inflate(b))
            self.assertTrue(tool.verify_logo(data)[0])

    @unittest.skipUnless(LK.is_file(), f"{LK} not present")
    def test_signature_is_valid_and_breaks_on_change(self):
        modulus = tool.lk_modulus(LK.read_bytes())
        self.assertEqual(self.logo.signature_state(modulus), "valid")
        data, _ = tool.replace_images(self.logo, {0: self.flat_splash()}, signature="stale")
        self.assertEqual(tool.Logo(data).signature_state(modulus), "INVALID")

    @unittest.skipUnless(LK.is_file(), f"{LK} not present")
    def test_signature_is_checked_on_replace_and_required_by_verify(self):
        # review A-S2: a bogus --signature-file is refused; --require-signature fails when unsigned
        rgb, _ = tool.bgra_to_rgb(self.flat_splash())
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            png = d / "splash.png"
            png.write_bytes(tool.png_encode(720, 1600, rgb))
            bogus = d / "bogus.sig"
            bogus.write_bytes(random.Random(3).randbytes(256))
            with quiet() as (_, err):
                code = tool.main(["replace", str(BACKUP), "0,88", str(png), str(d / "signed.img"),
                                  "--signature-file", str(bogus), "--lk", str(LK)])
            self.assertEqual(code, 1)
            self.assertIn("--signature-file is INVALID for the new content", err.getvalue())
            self.assertFalse((d / "signed.img").exists())
            unsigned = d / "unsigned.img"
            with quiet() as (out, _):
                self.assertEqual(tool.main(["replace", str(BACKUP), "0,88", str(png), str(unsigned),
                                            "--lk", str(LK)]), 0)
            self.assertEqual(json.loads(out.getvalue())["signature_check"], "empty")
            with quiet() as (out, _):
                self.assertEqual(tool.main(["verify", str(unsigned), "--lk", str(LK)]), 0)
                self.assertEqual(tool.main(["verify", str(unsigned), "--lk", str(LK), "--require-signature"]), 1)
                self.assertEqual(tool.main(["verify", str(BACKUP), "--lk", str(LK), "--require-signature"]), 0)
            self.assertIn("FAIL signature: empty; a valid signature is required", out.getvalue())
        self.assertEqual(hashlib.sha256(BACKUP.read_bytes()).hexdigest(), tool.STOCK_SHA256)

    def flat_splash(self):
        """720x1600 flat design: dark background, one light block (compresses small)."""
        row_bg = b"\x20\x10\x08\xff" * 720
        row_mark = b"\x20\x10\x08\xff" * 260 + b"\xf0\xe0\xd0\xff" * 200 + b"\x20\x10\x08\xff" * 260
        return row_bg * 700 + row_mark * 200 + row_bg * 700

    def test_replacing_boot_logos_changes_only_0_and_88(self):
        raw = self.flat_splash()
        data, report = tool.replace_images(self.logo, {0: raw, 88: raw})
        new = tool.Logo(data)
        self.assertEqual(report["changed"], [0, 88])
        self.assertEqual(len(data), 8 * MiB)
        self.assertLessEqual(new.dsize, tool.STOCK_DSIZE)
        for i, stream in enumerate(new.streams):
            if i in (0, 88):
                self.assertEqual(tool.inflate(stream), raw)
            else:
                self.assertEqual(stream, self.logo.streams[i], i)
        self.assertEqual(new.signature, bytes(256))
        ok, lines = tool.verify_logo(data, against=self.logo)
        self.assertTrue(ok, lines)
        self.assertIn("INFO images with different pixels than the reference: [0, 88]", lines)

    def test_noise_is_refused_by_stock_limit_and_partition_size(self):
        noise = tool.rgb_to_bgra(random.Random(2).randbytes(720 * 1600 * 3))
        with self.assertRaisesRegex(tool.LogoError, "over the limit"):
            tool.replace_images(self.logo, {0: noise})
        with self.assertRaisesRegex(tool.LogoError, "does not fit"):
            tool.replace_images(self.logo, {0: noise, 88: noise}, max_dsize=None)

    def test_cli_replace_with_extracted_pixels_is_byte_identical(self):
        rgb, alpha = tool.bgra_to_rgb(tool.inflate(self.logo.streams[0]))
        with tempfile.TemporaryDirectory() as directory:
            png = Path(directory) / "000.png"
            png.write_bytes(tool.png_encode(720, 1600, rgb, alpha))
            out = Path(directory) / "out.img"
            with quiet():
                self.assertEqual(tool.main(["replace", str(BACKUP), "0,88", str(png), str(out)]), 0)
                self.assertEqual(tool.main(["verify", str(out)]), 0)
            self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), tool.STOCK_SHA256)
        self.assertEqual(hashlib.sha256(BACKUP.read_bytes()).hexdigest(), tool.STOCK_SHA256)


if __name__ == "__main__":
    unittest.main()
