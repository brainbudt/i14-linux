"""Offline image-boundary tests; no phone, USB, or firmware writes."""
import gzip
import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-boot-image.py"
spec = importlib.util.spec_from_file_location("boot_image", SCRIPT)
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


def image(size=128, image_size=0x1810000, text_offset=0, flags=10):
    data = bytearray(size)
    struct.pack_into("<QQQ", data, 8, text_offset, image_size, flags)
    data[56:60] = b"ARM\x64"
    return bytes(data)


def fdt():
    structure = struct.pack(">I", 1) + b"\0" * 4 + struct.pack(">II", 2, 9)
    return (struct.pack(">10I", 0xD00DFEED, 73, 56, 72, 40, 17, 16, 0, 1, 16)
            + b"\0" * 16 + structure + b"\0")


def envelope(kernel=b"k", ramdisk=b"r", **changes):
    fields = dict(kernel_size=len(kernel), kernel_addr=0x40200000,
                  ramdisk_size=len(ramdisk), ramdisk_addr=0x44000000,
                  second_size=0, second_addr=0, tags_addr=0x4E000000,
                  page_size=2048, header_version=0, os_version=0)
    fields.update(changes)
    data = bytearray(2048)
    struct.pack_into("<8s10I", data, 0, b"ANDROID!", *fields.values())
    cmdline = boot.CMDLINE.encode()
    data[64:64 + len(cmdline)] = cmdline
    for component in (kernel, ramdisk):
        data.extend(component)
        data.extend(b"\0" * (-len(data) % 2048))
    return bytes(data)


class BootImageTests(unittest.TestCase):
    def test_exact_input_hash_is_required(self):
        boot.require_hash(b"test", hashlib.sha256(b"test").hexdigest(), "test")
        with self.assertRaisesRegex(boot.ImageError, "hash mismatch"):
            boot.require_hash(b"changed", hashlib.sha256(b"test").hexdigest(), "test")

    def test_gzip_requires_eof_and_rejects_unexpected_tail_or_second_member(self):
        compressed = gzip.compress(b"image", mtime=0)
        self.assertEqual(boot.inflate_gzip(compressed, 100, "test"), (b"image", b""))
        for data in (compressed[:-1], compressed + b"garbage", compressed + compressed):
            with self.subTest(data=data), self.assertRaises(boot.ImageError):
                boot.inflate_gzip(data, 100, "test")
        self.assertEqual(boot.inflate_gzip(compressed + fdt(), 100, "stock", allow_tail=True),
                         (b"image", fdt()))
        with self.assertRaisesRegex(boot.ImageError, "size limit"):
            boot.inflate_gzip(gzip.compress(b"x" * 101), 100, "test")

    def test_fdt_requires_exact_totalsize_and_sane_nonoverlapping_blocks(self):
        self.assertEqual(boot.validate_fdt(fdt())["bytes"], 73)
        invalid = [b"", fdt()[:-1], fdt() + b"x"]
        for offset, value in ((0, 0), (4, 10000), (8, 40), (12, 10000),
                              (16, 41), (20, 16), (36, 10000)):
            data = bytearray(fdt())
            struct.pack_into(">I", data, offset, value)
            invalid.append(bytes(data))
        for data in invalid:
            with self.subTest(data=data[:40]), self.assertRaises(boot.ImageError):
                boot.validate_fdt(data)

    def test_arm64_requires_known_header_and_aligned_placement(self):
        info = boot.arm64_info(image())
        self.assertEqual(info["effective_end"], 0x41A10000)
        invalid = [b"x" * 64, image(text_offset=0x80000), image(image_size=0),
                   image(image_size=64), image(flags=11)]
        for data in invalid:
            with self.subTest(data=data[:64]), self.assertRaises(boot.ImageError):
                boot.arm64_info(data)
        with self.assertRaisesRegex(boot.ImageError, "2 MiB"):
            boot.arm64_info(image(), load=0x40201000)

    def test_image_file_cap_is_strictly_below_28_mib(self):
        with self.assertRaisesRegex(boot.ImageError, "28 MiB"):
            boot.arm64_info(image(size=28 * 1024 * 1024, image_size=28 * 1024 * 1024))

    def test_memory_ranges_allow_adjacency_but_reject_one_byte_overlap(self):
        boot.check_ranges({"effective_end": 0x41E00000}, 1024, 73)
        with self.assertRaisesRegex(boot.ImageError, "LK"):
            boot.check_ranges({"effective_end": 0x41E00001}, 1024, 73)
        with self.assertRaisesRegex(boot.ImageError, "ramdisk.*tags"):
            boot.check_ranges({"effective_end": 0x41A10000}, 0xA000001, 73)
        with self.assertRaises(boot.ImageError):
            boot.check_ranges({"effective_end": 0x41A10000}, 0, 73)

    def test_candidate_header_payloads_and_cmdline_must_match(self):
        valid = envelope()
        boot.validate_candidate(valid, b"k", b"r")
        for data in (valid[:-1], valid + b"\0" * 2048,
                     envelope(kernel_addr=0x40080000), envelope(ramdisk_addr=0x43F00000),
                     envelope(tags_addr=0x4E001000), envelope(header_version=1),
                     envelope(page_size=4096), envelope(second_size=1),
                     envelope(os_version=1)):
            with self.subTest(header=data[:48]), self.assertRaises(boot.ImageError):
                boot.validate_candidate(data, b"k", b"r")
        bad_cmdline = bytearray(valid)
        bad_cmdline[64] = ord("x")
        with self.assertRaisesRegex(boot.ImageError, "cmdline"):
            boot.validate_candidate(bytes(bad_cmdline), b"k", b"r")
        with self.assertRaisesRegex(boot.ImageError, "kernel payload"):
            boot.validate_candidate(valid, b"other", b"r")

    def test_candidate_must_be_strictly_smaller_than_stock_partition(self):
        with self.assertRaisesRegex(boot.ImageError, "16 MiB"):
            boot.validate_candidate(envelope() + bytes(16 * 1024 * 1024 - 6144), b"k", b"r")

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            marker = output / "keep"
            marker.write_bytes(b"preserve")
            with self.assertRaisesRegex(boot.ImageError, "existing output"):
                boot.build(output)
            self.assertEqual(list(output.iterdir()), [marker])
            self.assertEqual(marker.read_bytes(), b"preserve")

    def test_existing_dangling_symlink_is_refused_before_reading_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "existing-link"
            output.symlink_to(root / "absent-target")
            with patch.object(boot, "validate_inputs", side_effect=AssertionError("unexpected input read")):
                with self.assertRaisesRegex(boot.ImageError, "existing output"):
                    boot.build(output)
            self.assertTrue(output.is_symlink())
            self.assertFalse((root / "absent-target").exists())


if __name__ == "__main__":
    unittest.main()
