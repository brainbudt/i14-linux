"""BCCT guard policy against ordinary files and an inert actuator."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rootfs/overlay/usr/lib"))
from i14ui import charging


class Control:
    def __init__(self):
        self.stopped = False
        self.changes = []

    def paused(self):
        return self.stopped

    def set_paused(self, value):
        self.changes.append(value)
        self.stopped = value

    def release(self):
        if self.stopped:
            self.set_paused(False)


class BatteryPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.b = self.d / "battery"
        self.b.mkdir()
        for key, value in {"capacity": "100", "status": "Full", "batt_vol": "4100", "batt_temp": "250"}.items():
            (self.b / key).write_text(value)
        for key, value in {"type": "2", "usb": "1", "ac": "0", "cmd": "sentinel",
                           "readback": "get_usb_current_unlimited=0,\ncmd_discharging = -1\n",
                           "gauge": "4109", "activity": "1500", "boot_id": "boot-a"}.items():
            (self.d / key).write_text(value)
        (self.d / "ui.json").write_text('{"charge_limit_enabled":true,"charge_limit":80}')
        self.now = 1000.0
        self.control = Control()
        patch = mock.patch.object(charging, "BOOT_ID", str(self.d / "boot_id"), create=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.guard = self.make_guard()

    def make_guard(self):
        return charging.Guard(battery=self.b, charger_type=self.d / "type", command=self.d / "cmd",
                              readback=self.d / "readback", usb=self.d / "usb", ac=self.d / "ac",
                              settings=self.d / "ui.json", log=self.d / "log", gauge=self.d / "gauge",
                              gauge_activity=self.d / "activity", control=self.control,
                              status_path=self.d / "status.json", clock=lambda: self.now)

    def tick(self, zcv=4109, activity=1501):
        (self.d / "gauge").write_text(str(zcv))
        (self.d / "activity").write_text(str(activity))
        return self.guard.tick()

    def start_live(self):
        self.tick(activity=1500)
        self.now += 60
        self.tick()

    def test_no_capacity100_fallback_before_live_voltage(self):
        self.tick(activity=1500)
        self.assertFalse(self.control.stopped)
        self.assertEqual((self.d / "cmd").read_text(), "sentinel")
        status = json.loads((self.d / "status.json").read_text())
        self.assertIsNone(status["level"])
        self.assertFalse(status["valid"])

    def test_band_uses_voltage_landmarks_without_legacy_transitions(self):
        self.start_live()
        self.assertTrue(self.control.stopped)
        for zcv, expected in ((4067, True), (4004, True), (4003, False), (4108, False), (4109, True)):
            self.now += 60
            self.tick(zcv=zcv, activity=int(self.now))
            self.assertEqual(self.control.stopped, expected, zcv)
        self.assertEqual((self.d / "cmd").read_text(), "sentinel")

    def test_stale_debug_activity_releases_and_invalidates_status(self):
        self.start_live()
        self.now += 181
        self.tick(activity=1501)
        self.assertFalse(self.control.stopped)
        status = json.loads((self.d / "status.json").read_text())
        self.assertIsNone(status["level"])
        self.assertFalse(status["valid"])
        self.assertIn("stale gauge activity", (self.d / "log").read_text())

    def test_steady_current_changing_zcv_keeps_pause(self):
        self.start_live()
        for zcv in (4108, 4107, 4106, 4105):
            self.now += 60.08
            self.tick(zcv=zcv, activity=1501)
        self.assertTrue(self.control.stopped)

    def test_steady_current_changing_car_keeps_pause(self):
        (self.d / "FG_g_fg_dbg_bat_car").write_text("-10")
        self.start_live()
        for car in (-12, -14, -16, -18):
            self.now += 60.08
            (self.d / "FG_g_fg_dbg_bat_car").write_text(str(car))
            self.tick(activity=1501)
        self.assertTrue(self.control.stopped)

    def test_missing_car_does_not_create_activity(self):
        self.start_live()
        self.now += 181
        self.tick(activity=1501)
        self.assertFalse(self.control.stopped)

    def _restart_after_quiet_snapshot(self, at, legacy):
        # Live and paused (activity changed at 1060), a quiet tick refreshes the snapshot at
        # 1180 without a change, then the guard restarts at `at`.
        self.start_live()
        self.now = 1180.0
        self.tick()
        if legacy:  # status written before activity_zcv/activity_car existed
            status = json.loads((self.d / "status.json").read_text())
            del status["activity_zcv"], status["activity_car"]
            (self.d / "status.json").write_text(json.dumps(status))
        self.now = at
        self.guard = self.make_guard()

    def test_restart_restores_zcv_comparison_from_legacy_status(self):
        self._restart_after_quiet_snapshot(1239.9, legacy=True)
        self.tick(zcv=4108)  # first tick after restart: ZCV moved
        self.now += 60.08
        self.tick(zcv=4108)
        self.assertTrue(self.control.stopped)

    def test_restart_keeps_comparison_values_after_trust_expired(self):
        self._restart_after_quiet_snapshot(1240.08, legacy=False)
        self.tick(zcv=4108)
        self.assertTrue(self.control.stopped)

    def test_malformed_snapshot_zcv_does_not_fall_back_to_zcv_mv(self):
        for raw in (9223372036854775808, -9223372036854775809, 18446744073709551616, 4108.5, "4108"):
            self.setUp()
            self._restart_after_quiet_snapshot(1240.08, legacy=False)
            status = json.loads((self.d / "status.json").read_text())
            status["activity_zcv"] = raw
            (self.d / "status.json").write_text(json.dumps(status))
            self.guard = self.make_guard()
            self.tick(zcv=4108)
            self.assertFalse(self.control.stopped, raw)

    def test_restart_inside_band_uses_fresh_status_and_hardware_pause(self):
        self.start_live()
        self.now += 60
        self.tick(zcv=4067, activity=1502)
        self.guard = self.make_guard()
        self.now += 10
        self.tick(zcv=4067, activity=1502)
        self.assertTrue(self.control.stopped)
        self.assertEqual((self.d / "cmd").read_text(), "sentinel")

    def test_previous_boot_cannot_restore_gauge_trust(self):
        self.start_live()
        (self.d / "boot_id").write_text("boot-b")
        self.guard = self.make_guard()
        self.tick(activity=1501)
        self.assertFalse(self.control.stopped)

    def test_publishes_one_current_estimate_and_effective_pause(self):
        self.start_live()
        status = charging.read_status(self.d / "status.json", now=self.now, boot_id="boot-a")
        self.assertEqual((status["schema"], status["backend"], status["level"], status["zcv_mv"]),
                         (1, "bcct", 80, 4109))
        self.assertTrue(status["paused"])
        self.assertTrue(status["valid"])
        self.assertEqual((self.d / "status.json").stat().st_mode & 0o777, 0o644)
        self.assertIsNone(charging.read_status(self.d / "status.json", now=self.now + 151, boot_id="boot-a"))

    def test_bad_feedback_releases_without_writing_unknown_legacy_state(self):
        self.start_live()
        (self.d / "readback").write_text("garbage")
        self.now += 60
        self.tick(activity=1503)
        self.assertFalse(self.control.stopped)
        self.assertEqual((self.d / "cmd").read_text(), "sentinel")

    def test_failed_migration_latch_survives_clean_restart(self):
        (self.d / "readback").write_text("get_usb_current_unlimited=1,\ncmd_discharging = 1\n")
        with mock.patch.object(self.guard,"write_command",wraps=self.guard.write_command) as writer:
            self.guard.tick()
            self.assertEqual(writer.call_count,1)
        self.guard.close()
        self.now += 151  # measurement expiry must not erase same-boot command custody
        self.guard=self.make_guard()
        with mock.patch.object(self.guard,"write_command") as writer:
            self.guard.tick()
            writer.assert_not_called()
        self.assertFalse(self.control.paused())

    def test_control_failure_is_visible_and_low_battery_guard_still_runs(self):
        self.start_live()
        (self.b / "capacity").write_text("3")
        with mock.patch.object(self.control, "paused", side_effect=OSError("readback unavailable")):
            self.assertTrue(self.tick(activity=1503))
        status = json.loads((self.d / "status.json").read_text())
        self.assertFalse(status["valid"])
        self.assertIn("readback unavailable", status["error"])


if __name__ == "__main__":
    unittest.main()
