"""Offline structure checks; these do not test encryption or phone acceptance."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import unittest

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "seccfg-v3.py"


class SecCfgV3Tests(unittest.TestCase):
    def load_module(self):
        self.assertTrue(MODULE.exists(), "Offline V3 builder is missing")
        spec = importlib.util.spec_from_file_location("seccfg_v3", MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_plaintext_matches_bound_lk_field_offsets(self):
        m = self.load_module()
        body = m.plaintext_body()
        self.assertEqual(len(body), 0x1830)
        self.assertEqual(body[:0x820], bytes(20 * 0x68))
        self.assertEqual(struct.unpack_from("<III", body, 0x820),
                         (0, 0x43434343, 0x44444444))
        self.assertEqual(body[0x82C:], bytes(0x1004))

    def test_partition_has_v3_header_and_preserves_all_other_bytes(self):
        m = self.load_module()
        original = bytes(524288)
        ciphertext = bytes(range(256)) * 24 + bytes(range(48))
        result = m.assemble_partition(original, ciphertext)
        self.assertEqual(len(result), 524288)
        self.assertEqual(result[:16], b"AND_SECCFG_v" + bytes(4))
        self.assertEqual(struct.unpack_from("<5I", result, 0x10),
                         (0x4D4D4D4D, 3, 0x1860, 0, 0x07F20000))
        self.assertEqual(result[0x24:0x2C], bytes(8))
        self.assertEqual(result[0x2C:0x185C], ciphertext)
        self.assertEqual(struct.unpack_from("<I", result, 0x185C)[0], 0x45454545)
        self.assertEqual(result[0x1860:], original[0x1860:])
        self.assertEqual(hashlib.sha256(original).hexdigest(),
                         "07854d2fef297a06ba81685e660c332de36d5d18d546927d30daad6d7fda1541")

    def test_changed_original_partition_is_rejected(self):
        m = self.load_module()
        altered = bytearray(524288)
        altered[-1] = 1
        with self.assertRaises(ValueError):
            m.assemble_partition(bytes(altered), bytes(0x1830))

    def test_bad_partition_and_ciphertext_sizes_are_rejected(self):
        m = self.load_module()
        for original, cipher in [(bytes(524287), bytes(0x1830)),
                                 (bytes(524288), bytes(0x182F)),
                                 (bytes(524288), bytes(0x1831))]:
            with self.subTest(original=len(original), cipher=len(cipher)):
                with self.assertRaises(ValueError):
                    m.assemble_partition(original, cipher)


if __name__ == "__main__":
    unittest.main()
