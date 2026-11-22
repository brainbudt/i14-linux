import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("wait_native", Path(__file__).resolve().parents[1] / "scripts/wait-native-once.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class WaitNativeTests(unittest.TestCase):
    def device(self, **changes):
        d = {"idVendor": 0x1d6b, "idProduct": 0x0104, "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}
        d.update(changes)
        return d

    def test_exact_identity_only(self):
        self.assertTrue(m.target_present([self.device()]))
        for change in ({"idVendor": 0xe8d}, {"idProduct": 1}, {"USB Serial Number": "OTHER"}, {"locationID": 0x2120000}):
            self.assertFalse(m.target_present([self.device(**change)]))

    def test_nested_registry(self):
        self.assertTrue(m.target_present(m.records([{"IORegistryEntryChildren": [self.device()]}])))

    def test_capture_once_and_exit(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m, "scan", return_value=[self.device()]), patch.object(m, "capture", return_value=0) as capture:
            self.assertEqual(m.watch(Path(d), 5), 0)
            capture.assert_called_once()
            self.assertTrue((Path(d) / "attempt.json").exists())

    def test_failed_capture_is_not_retried(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m, "scan", return_value=[self.device()]), patch.object(m, "capture", return_value=2) as capture:
            self.assertEqual(m.watch(Path(d), 5), 2)
            capture.assert_called_once()

    def test_existing_attempt_refuses_reentry(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m, "capture") as capture:
            (Path(d) / "attempt.json").write_text("{}")
            self.assertEqual(m.watch(Path(d), 5), 2)
            capture.assert_not_called()

    def test_timeout_sends_no_command(self):
        with tempfile.TemporaryDirectory() as d, patch.object(m, "capture") as capture:
            self.assertEqual(m.watch(Path(d), 0), 3)
            capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
