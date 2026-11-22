"""Offline parser boundaries; fixtures are local original backups only."""
import importlib.util
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare-stock-charge-components.py"


def module():
    spec = importlib.util.spec_from_file_location("stock_components", SCRIPT)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class StockComponentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SCRIPT.exists():
            cls.m = module()
            cls.boot = (ROOT / "backups/20261030T011515Z-native-serial/boot.img").read_bytes()
            cls.payload = cls.boot[2048:7015374]
            decoder = zlib.decompressobj(31)
            cls.image = decoder.decompress(cls.payload)
            cls.dtb = decoder.unused_data

    def setUp(self):
        self.assertTrue(SCRIPT.exists(), "offline component validator is not implemented")

    def test_real_pair_outputs_only_image_dtb_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "components"
            result = self.m.prepare(ROOT, out)
            self.assertEqual(result["status"], "OFFLINE_COMPONENTS_VERIFIED")
            self.assertIs(result["hardware_go"], False)
            self.assertEqual(set(p.name for p in out.iterdir()), {"Image", "stock.dtb", "manifest.json", "SHA256SUMS"})
            self.assertEqual((out / "Image").read_bytes(), self.image)
            self.assertEqual((out / "stock.dtb").read_bytes(), self.dtb)
            self.assertEqual(result["image"]["runtime_end_exclusive"], 0x41306000)
            self.assertEqual(result["dtb_metadata"]["memory"], [{"path": "/memory@40000000", "ranges": [[0x40000000, 0x5f000000]]}])
            self.assertEqual(result["dtb_metadata"]["chosen_initrd"], {"start": None, "end": None})
            self.assertGreaterEqual(len(result["dtb_metadata"]["reserved_memory"]), 6)

    def test_wrong_hash_rejected(self):
        bad = bytearray(self.boot); bad[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "hash"):
            self.m.validate_pair(bytes(bad), self.boot)

    def test_android_header_truncation_and_layout_rejected(self):
        for value in (self.boot[:100], self.boot[:3000], b"MTK!" + self.boot[4:]):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                self.m.parse_boot(value, "boot")
        for offset, value in ((8, 0xffffffff), (12, 0x40200000), (36, 4096), (40, 1)):
            bad = bytearray(self.boot); struct.pack_into("<I", bad, offset, value)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.m.parse_boot(bytes(bad), "boot")

    def test_gzip_bounds_and_tail_rejected(self):
        for value in (self.payload[:100], self.payload[:-1], b"\x88\x16\x88\x58" + self.payload, self.payload + b"x", zlib.compress(b"x")):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                self.m.parse_payload(value)
        huge = zlib.compressobj(wbits=31)
        bomb = huge.compress(b"x" * 16446425) + huge.flush()
        with self.assertRaisesRegex(ValueError, "size"):
            self.m.parse_payload(bomb)

    def test_arm64_header_rejected(self):
        for offset in (8, 16, 24, 56):
            bad = bytearray(self.image); bad[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.m.validate_image(bytes(bad))

    def test_dtb_bounds_overlap_and_tokens_rejected(self):
        for offset, value in ((4, len(self.dtb)+4), (8, 0xfffffffc), (12, 56), (16, 48)):
            bad = bytearray(self.dtb); struct.pack_into(">I", bad, offset, value)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.m.parse_dtb(bytes(bad))
        bad = bytearray(self.dtb); struct.pack_into(">I", bad, 56, 99)
        with self.assertRaises(ValueError):
            self.m.parse_dtb(bytes(bad))

    def test_dtb_reg_width_rejected(self):
        # Corrupt a real static reservation property length, keeping all block bounds valid.
        raw = bytes.fromhex("00000000430000000000000000030000")
        at = self.dtb.index(raw)
        bad = bytearray(self.dtb); struct.pack_into(">I", bad, at - 8, 12)
        with self.assertRaises(ValueError):
            self.m.parse_dtb(bytes(bad))

    def test_existing_output_and_symlink_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "existing"; out.mkdir()
            marker = out / "keep"; marker.write_text("unchanged")
            with self.assertRaisesRegex(ValueError, "existing"):
                self.m.prepare(ROOT, out)
            self.assertEqual(marker.read_text(), "unchanged")
            link = Path(tmp) / "link"; link.symlink_to(Path(tmp) / "absent")
            with self.assertRaisesRegex(ValueError, "existing"):
                self.m.prepare(ROOT, link)


if __name__ == "__main__":
    unittest.main()
