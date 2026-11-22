"""BCCT policy against a temporary kernel-shaped tree, with no device access."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rootfs/overlay/usr/lib"))
from i14ui.bcct import Bcct, write_command


class BcctTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "clbcct"
        self.thermal = self.root / "thermal"
        self.device = self.thermal / "cooling_device25"
        self.device.mkdir(parents=True)
        (self.device / "type").write_text("mtk-cl-bcct00\n")
        (self.device / "max_state").write_text("1\n")
        self.klog = 0
        self.limits = [0, -1, 0]
        self.states = [0, 0, 0]
        self.writes = []
        self.render()
        self.backend = Bcct(config=self.config, thermal_root=self.thermal)
        self.patch = mock.patch("i14ui.bcct.write_command", side_effect=self.kernel_write)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def render(self, effective=None):
        if effective is None:
            active = [limit for limit, state in zip(self.limits, self.states) if state and limit > 0]
            effective = min(active, default=65535)
        lines = [f"klog {self.klog}", f"curr_limit {effective}"]
        lines += [f"mtk-cl-bcct{i:02} {limit} mA, state {state}"
                  for i, (limit, state) in enumerate(zip(self.limits, self.states))]
        self.config.write_text("\n".join(lines) + "\n")
        (self.device / "cur_state").write_text(f"{self.states[0]}\n")

    def kernel_write(self, path, text):
        data = (text + "\n").encode()
        self.writes.append((Path(path), data))
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 1)
        if Path(path) == self.config:
            self.klog, *self.limits = map(int, data.split())
        else:
            self.assertEqual(Path(path), self.device / "cur_state")
            self.states[0] = int(data)
        self.render()

    def test_pause_and_release_preserve_other_limits_states_and_logging(self):
        self.klog, self.limits, self.states = 1, [-1, 500, 300], [0, 1, 1]
        self.render()
        self.backend.set_paused(True)
        self.assertTrue(self.backend.paused())
        self.assertEqual(self.writes, [(self.config, b"1 1 500 300\n"),
                                      (self.device / "cur_state", b"1\n")])
        self.backend.release()
        self.assertFalse(self.backend.paused())
        self.assertEqual((self.klog, self.limits, self.states), (1, [1, 500, 300], [0, 1, 1]))
        self.assertIn("curr_limit 300\n", self.config.read_text())

    def test_restart_adopts_own_pause_and_noops_when_already_confirmed(self):
        self.limits[0], self.states[0] = 1, 1
        self.render()
        self.assertTrue(self.backend.paused())
        self.backend.set_paused(True)
        self.assertEqual(self.writes, [])
        self.backend.release()
        self.backend.release()
        self.assertEqual(self.writes, [(self.device / "cur_state", b"0\n")])

    def test_release_does_not_claim_a_free_slot(self):
        self.backend.release()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.limits[0], 0)

    def test_owned_slot_is_reused_without_rewriting_the_whole_config(self):
        self.limits[0] = 1
        self.render()
        self.backend.set_paused(True)
        self.assertEqual(self.writes, [(self.device / "cur_state", b"1\n")])

    def test_foreign_own_slot_or_unowned_active_slot_is_not_changed(self):
        for limit, state in ((70, 0), (-1, 1), (0, 1)):
            with self.subTest(limit=limit, state=state):
                self.limits[0], self.states[0] = limit, state
                self.render()
                with self.assertRaises(ValueError):
                    self.backend.set_paused(True)
        self.assertEqual(self.writes, [])

    def test_sysfs_and_config_state_must_match(self):
        (self.device / "cur_state").write_text("1\n")
        with self.assertRaises(ValueError):
            self.backend.set_paused(True)
        self.assertEqual(self.writes, [])

    def test_pause_requires_effective_one_ma_readback_even_on_noop(self):
        self.limits[0], self.states[0] = 1, 1
        self.render(effective=65535)
        with self.assertRaises(ValueError):
            self.backend.set_paused(True)
        self.assertEqual(self.writes, [])

    def test_release_clears_own_state_even_when_pause_is_ineffective(self):
        self.limits[0], self.states[0] = 1, 1
        self.render(effective=65535)
        self.backend.release()
        self.assertEqual(self.writes, [(self.device / "cur_state", b"0\n")])
        self.assertEqual(self.states[0], 0)

    def test_thermal_binding_rejects_control_but_unrelated_binding_is_allowed(self):
        zone = self.thermal / "thermal_zone0"
        zone.mkdir()
        (zone / "cdev0").symlink_to(self.device)
        with self.assertRaises(ValueError):
            self.backend.set_paused(True)
        self.assertEqual(self.writes, [])
        (zone / "cdev0").unlink()
        other = self.thermal / "cooling_device1"
        other.mkdir()
        (other / "type").write_text("other\n")
        (zone / "cdev0").symlink_to(other)
        self.backend.set_paused(True)
        self.assertTrue(self.backend.paused())

    def test_missing_duplicate_or_changed_device_is_rejected(self):
        duplicate = self.thermal / "cooling_device4"
        duplicate.mkdir()
        (duplicate / "type").write_text("mtk-cl-bcct00\n")
        with self.assertRaises(ValueError):
            self.backend.set_paused(True)
        (duplicate / "type").write_text("other\n")
        (self.device / "type").write_text("changed\n")
        with self.assertRaises(ValueError):
            self.backend.set_paused(True)
        self.assertEqual(self.writes, [])

    def test_damaged_or_missing_config_is_rejected_without_writes(self):
        original = self.config.read_text()
        for text in ("", original + original, original.replace("klog 0", "klog 2"),
                     original.replace("state 0", "state 2"), original.replace("-1 mA", "40000 mA")):
            self.config.write_text(text)
            with self.assertRaises(ValueError):
                self.backend.set_paused(True)
        self.config.unlink()
        with self.assertRaises(OSError):
            self.backend.set_paused(True)
        self.assertEqual(self.writes, [])

    def test_unaccepted_config_or_state_write_readback_raises(self):
        with mock.patch("i14ui.bcct.write_command"):
            with self.assertRaises(ValueError):
                self.backend.set_paused(True)
        self.limits[0] = 1
        self.render()
        with mock.patch("i14ui.bcct.write_command"):
            with self.assertRaises(ValueError):
                self.backend.set_paused(True)

    def test_each_kernel_command_is_one_write_and_partial_write_is_not_retried(self):
        path = self.root / "write-boundary"
        path.write_bytes(b"")
        real_write = os.write
        with mock.patch("i14ui.bcct.os.write", wraps=real_write) as write:
            write_command(path, "0 1 -1 0")
        self.assertEqual(path.read_bytes(), b"0 1 -1 0\n")
        self.assertEqual(write.call_count, 1)
        with mock.patch("i14ui.bcct.os.write", return_value=2) as short:
            with self.assertRaises(OSError):
                write_command(path, "0 1 -1 0")
        self.assertEqual(short.call_count, 1)


if __name__ == "__main__":
    unittest.main()
