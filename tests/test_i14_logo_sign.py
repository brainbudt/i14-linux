"""scripts/i14-logo-sign.py with the local (git-ignored) key and lk.img; skipped when they are absent."""
import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("logo_sign", ROOT / "scripts/i14-logo-sign.py")
sign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sign)
KEY = ROOT / "artifacts/splash-key/i14-logo-privkey.json"
LK = pathlib.Path("/Volumes/I14_BACKUP/i14-backups/20261102-phase0/lk.img")
STOCK = ROOT / "backups/20261102-phase0-logo/logo.img"


@unittest.skipUnless(KEY.exists() and LK.exists() and STOCK.exists(), "key, lk.img or stock logo not present")
class LogoSign(unittest.TestCase):
    def setUp(self):
        self.key, self.lk, self.stock = json.loads(KEY.read_text()), LK.read_bytes(), STOCK.read_bytes()

    def test_signs_and_verifies_with_lks_key(self):
        modulus = sign.tool.lk_modulus(self.lk)
        unsigned = bytearray(self.stock)
        logo = sign.tool.Logo(self.stock)
        unsigned[logo.sig_offset:logo.sig_offset + 256] = bytes(256)
        self.assertEqual(sign.tool.Logo(bytes(unsigned)).signature_state(modulus), "empty")
        out = sign.sign_image(bytes(unsigned), self.key, self.lk)
        self.assertEqual(sign.tool.Logo(out).signature_state(modulus), "valid")
        self.assertEqual(out[:logo.sig_offset], self.stock[:logo.sig_offset])  # only the slot changed
        self.assertEqual(out[logo.sig_offset + 256:], self.stock[logo.sig_offset + 256:])

    def test_rejects_a_key_for_another_modulus(self):
        other = dict(self.key, modulus_n_hex=format(int(self.key["modulus_n_hex"], 16) - 2, "x"))
        with self.assertRaises(sign.tool.LogoError):
            sign.sign_image(self.stock, other, self.lk)

    def test_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "x.img"
            out.write_bytes(b"x")
            with self.assertRaises(sign.tool.LogoError):
                sign.main([str(STOCK), str(KEY), str(LK), str(out)])


if __name__ == "__main__":
    unittest.main()
