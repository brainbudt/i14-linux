"""Host-only privacy and identity checks for the read-only inventory payload."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import os


SOURCE = Path(os.environ.get("I14_OBSERVER_INVENTORY",
              Path(__file__).resolve().parents[1] / "scripts/i14-soak-observer-inventory.py"))
inventory = None
if SOURCE.is_file():
    spec = importlib.util.spec_from_file_location("observer_inventory", SOURCE)
    inventory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inventory)


class Inventory(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(inventory, "read-only observer inventory is not implemented")

    def test_identity_accepts_nul_separated_exact_platform_only(self):
        inventory.check_identity(0, "i14", "3.18.19", "mediatek,MT6735\0other\0")
        for value in [(1, "i14", "3.18.19", "mediatek,MT6735"),
                      (0, "other", "3.18.19", "mediatek,MT6735"),
                      (0, "i14", "7.0", "mediatek,MT6735"),
                      (0, "i14", "3.18.19", "mediatek,MT6737")]:
            with self.assertRaises(ValueError):
                inventory.check_identity(*value)

    def test_shell_installer_cron_line_is_recognised(self):
        line = f"* * * * * /bin/sh '{inventory.SAMPLER}' # i14-soak-sample\n"
        self.assertEqual(inventory.cron_summary(line.encode())["sampler_lines"], 1)

    def test_cron_summarises_known_sampler_without_exposing_other_commands(self):
        raw = (b"* * * * * '/mnt/data/media/0/i14-linux/soak/i14-soak-sample' # i14-soak-sample\n"
               b"0 1 * * * tool SECRET_TOKEN=do-not-output\n")
        info = inventory.cron_summary(raw)
        self.assertEqual(info["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(info["sampler_lines"], 1)
        self.assertEqual(info["other_command_count"], 1)
        self.assertNotIn("SECRET_TOKEN", repr(info))
        self.assertNotIn("do-not-output", repr(info))

    def test_cron_recognises_actual_existing_rust_sampler_row(self):
        raw = b"* * * * * '/mnt/data/media/0/i14-linux/soak/i14-soak-sample-rs' # i14-soak-sample\n"
        info = inventory.cron_summary(raw)
        self.assertEqual(info["sampler_lines"], 1)
        self.assertEqual(info["other_command_count"], 0)

    def test_log_metadata_retains_hash_and_stamp_boundaries_without_log_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "messages"
            raw = b"Nov 15 17:00:00 i14 secret=hidden\nNov 15 17:01:00 i14 other-secret\n"
            path.write_bytes(raw)
            info = inventory.file_info(path, log=True)
        self.assertEqual(info["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(info["first_stamp"], "Nov 15 17:00:00")
        self.assertEqual(info["last_stamp"], "Nov 15 17:01:00")
        self.assertTrue(info["newline_terminated"])
        self.assertNotIn("hidden", repr(info))
        self.assertNotIn("other-secret", repr(info))

    def test_missing_log_is_explicit_and_not_an_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "messages.4"
            self.assertFalse(path.exists())
            info = inventory.file_info(path, log=True)
        self.assertEqual(info, {"present": False})

    def test_unexpected_symlink_is_refused_before_reading_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve() / "private-target"
            target.write_text("not an inventory input")
            path = Path(directory).resolve() / "messages"
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                inventory.file_info(path, log=True)

    def test_unexpected_large_input_is_refused_without_hashing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "samples.jsonl"
            with path.open("wb") as stream:
                stream.truncate(64 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                inventory.file_info(path)

    def test_growth_after_size_check_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "messages"
            path.write_bytes(b"original\n")
            real_read = os.read
            changed = False
            def grow_then_read(fd, size):
                nonlocal changed
                if not changed:
                    changed = True
                    with path.open("ab") as stream:
                        stream.write(b"grew\n")
                return real_read(fd, size)
            with mock.patch.object(inventory.os, "read", side_effect=grow_then_read):
                with self.assertRaises(ValueError):
                    inventory.file_info(path, log=True)

    def test_symlinked_ancestor_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory).resolve() / "outside"
            target.mkdir()
            (target / "samples.jsonl").write_bytes(b"private\n")
            link = Path(directory).resolve() / "soak"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                inventory.file_info(link / "samples.jsonl")

    def test_battery_iso_boundaries_do_not_expose_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "battery"
            path.write_bytes(b"2026-11-15 17:00:00 private=hidden\n2026-11-15 17:01:00 hidden\n")
            info = inventory.file_info(path, log=True)
        self.assertEqual(info["first_stamp"], "2026-11-15 17:00:00")
        self.assertEqual(info["last_stamp"], "2026-11-15 17:01:00")
        self.assertNotIn("hidden", repr(info))

    def test_log_summary_uses_same_snapshot_as_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "messages"
            original = b"Nov 15 17:00:00 i14 original\n"
            replacement = b"Nov 15 17:59:00 i14 changed\n"
            path.write_bytes(original)
            real_read_bytes = Path.read_bytes
            def replace_on_reread(value):
                if value == path:
                    path.write_bytes(replacement)
                return real_read_bytes(value)
            with mock.patch.object(Path, "read_bytes", replace_on_reread):
                info = inventory.file_info(path, log=True)
        self.assertEqual(info["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(info["first_stamp"], "Nov 15 17:00:00")


if __name__ == "__main__":
    unittest.main()
