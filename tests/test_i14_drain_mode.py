"""Drain mode of the production charge guard (charging.py): the request file and the decisions.

The Rust port has the same cases in rust/crates/i14-power/tests/drain_mode.rs, and
rust/crates/i14-power/tests/differential.rs compares both guards step by step in drain mode.
"""
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rootfs/overlay/usr/lib"))
from i14ui import charging

UID = os.getuid()


class Control:
    def __init__(self):
        self.stopped = False
        self.changes = []
        self.fail = None

    def paused(self):
        return self.stopped

    def set_paused(self, value):
        if self.fail:
            raise OSError(errno.EIO, self.fail)
        self.changes.append(value)
        self.stopped = value

    def release(self):
        if self.stopped:
            self.stopped = False
            self.changes.append(False)


class Request(unittest.TestCase):
    """charging.drain_request: every validity case."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "drain-request"

    def put(self, text, mode=0o600):
        if self.path.exists():
            self.path.unlink()
        self.path.write_bytes(text.encode() if isinstance(text, str) else text)
        os.chmod(self.path, mode)

    def check(self, now=1000.0, boot="boot-a", owner=UID):
        return charging.drain_request(self.path, boot, now, owner)

    def test_missing_is_no_request(self):
        self.assertEqual(self.check(), (None, None))

    def test_valid_with_and_without_newline(self):
        self.put("boot-a 4600\n")
        self.assertEqual(self.check(), (4600, None))
        self.put("boot-a 4600")
        self.assertEqual(self.check(), (4600, None))
        self.put("4257b01c-e114-476f-8ed6-5d82e8d59f21 4600\n")
        self.assertEqual(self.check(boot="4257b01c-e114-476f-8ed6-5d82e8d59f21"), (4600, None))

    def test_other_boot_and_unknown_boot(self):
        self.put("boot-b 4600\n")
        self.assertEqual(self.check(), (None, "other boot"))
        self.put("boot-a 4600\n")
        self.assertEqual(self.check(boot=""), (None, "other boot"))

    def test_expired_at_and_after_the_deadline(self):
        self.put("boot-a 1000\n")
        self.assertEqual(self.check(now=1000.0), (None, "expired"))
        self.assertEqual(self.check(now=999.5), (1000, None))
        self.assertEqual(self.check(now=2000.0), (None, "expired"))

    def test_at_most_twelve_hours_ahead(self):
        self.put(f"boot-a {1000 + charging.DRAIN_MAX_S}\n")
        self.assertEqual(self.check(now=1000.0), (1000 + charging.DRAIN_MAX_S, None))
        self.assertEqual(self.check(now=999.5), (None, "too long"))

    def test_unreadable_clock(self):
        self.put("boot-a 4600\n")
        self.assertEqual(self.check(now=None), (None, "clock unavailable"))

    def test_malformed(self):
        for text in ("", "\n", "boot-a\n", "boot-a 12.5\n", "boot-a  4600\n", " boot-a 4600\n", "boot-a 4600 \n",
                     "boot-a 4600\n\n", "boot-a -4600\n", "boot-a +4600\n", "boot-a 1234567890\n",
                     "boot-a 4600\r\n", "boot_a 4600\n", "boot-a 4600\nboot-a 4600\n", "a" * 65 + " 4600\n",
                     "boot-a 4٠\n"):
            self.put(text)
            self.assertEqual(self.check(), (None, "malformed"), repr(text))
        self.put(b"boot-a 4600\n" + b"x" * 120)
        self.assertEqual(self.check(), (None, "malformed"))
        self.put(b"caf\xc3\xa9 4600\n")
        self.assertEqual(self.check(), (None, "malformed"))

    def test_mode_and_owner(self):
        for mode in (0o644, 0o640, 0o604, 0o601, 0o610):
            self.put("boot-a 4600\n", mode)
            self.assertEqual(self.check(), (None, "group or other access"), oct(mode))
        self.put("boot-a 4600\n", 0o400)
        self.assertEqual(self.check(), (4600, None))
        self.put("boot-a 4600\n")
        self.assertEqual(self.check(owner=UID + 1), (None, "not owned by root"))

    def test_symlink_directory_and_fifo(self):
        real = self.path.with_name("real")
        real.write_text("boot-a 4600\n")
        os.chmod(real, 0o600)
        self.path.symlink_to(real)
        self.assertEqual(self.check(), (None, f"unreadable (errno {errno.ELOOP})"))
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(self.check(), (None, "not a regular file"))
        self.path.rmdir()
        os.mkfifo(self.path, 0o600)
        self.assertEqual(self.check(), (None, "not a regular file"))  # O_NONBLOCK: no hang


class DrainMode(unittest.TestCase):
    """The guard's decisions with a request present."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.b = self.d / "battery"
        self.b.mkdir()
        for key, value in {"capacity": "60", "status": "Charging", "batt_vol": "3900", "batt_temp": "250"}.items():
            (self.b / key).write_text(value)
        for key, value in {"type": "4", "usb": "1", "ac": "0", "cmd": "sentinel",
                           "readback": "get_usb_current_unlimited=0,\ncmd_discharging = -1\n",
                           "gauge": "3900", "activity": "1500", "boot_id": "boot-a"}.items():
            (self.d / key).write_text(value)
        (self.d / "ui.json").write_text('{"charge_limit_enabled":true,"charge_limit":80}')
        self.now = 1000.0
        self.up = 500.0  # boot-relative uptime
        self.activity = 1500
        self.control = Control()
        patch = mock.patch.object(charging, "BOOT_ID", str(self.d / "boot_id"))
        patch.start()
        self.addCleanup(patch.stop)
        self.request = self.d / "drain-request"
        self.guard = self.make_guard()

    def make_guard(self):
        return charging.Guard(battery=self.b, charger_type=self.d / "type", command=self.d / "cmd",
                              readback=self.d / "readback", usb=self.d / "usb", ac=self.d / "ac",
                              settings=self.d / "ui.json", log=self.d / "log", gauge=self.d / "gauge",
                              gauge_activity=self.d / "activity", control=self.control,
                              status_path=self.d / "status.json", clock=lambda: self.now,
                              drain_path=self.request, uptime=self.uptime, drain_owner=UID)

    def uptime(self):
        if self.up is None:
            raise OSError(errno.EINVAL, "no clock")
        return self.up

    def ask(self, ahead=3600, boot="boot-a", mode=0o600):
        self.request.write_text(f"{boot} {int(self.up) + ahead}\n")
        os.chmod(self.request, mode)

    def tick(self, zcv=None, **battery):
        self.now += 60
        if self.up is not None:
            self.up += 60
        self.activity += 1
        (self.d / "activity").write_text(str(self.activity))
        if zcv is not None:
            (self.d / "gauge").write_text(str(zcv))
        for key, value in battery.items():
            (self.b / key).write_text(str(value))
        return self.guard.tick()

    def status(self):
        return json.loads((self.d / "status.json").read_text())

    def log(self):
        return (self.d / "log").read_text()

    def drain_lines(self):
        return [line[20:] for line in self.log().splitlines() if "drain" in line]

    def start_drain(self):
        """Live gauge, the band pause reached, then a request (the drain starts from a paused band)."""
        self.tick(zcv=4109)
        self.tick(zcv=4109)
        self.assertTrue(self.control.stopped)
        self.ask()
        self.tick(zcv=4100, status="Discharging")
        self.assertTrue(self.status()["drain"])

    def test_without_request_nothing_changes(self):
        self.tick(zcv=4109)
        self.tick(zcv=4109)
        self.assertTrue(self.control.stopped)
        self.tick(zcv=4003)
        self.assertFalse(self.control.stopped)
        status = self.status()
        self.assertEqual((status["drain"], status["drain_until"]), (False, None))
        self.assertEqual(self.drain_lines(), [])

    def test_stays_paused_below_the_band_the_reserve_and_15_percent(self):
        self.start_drain()
        until = self.status()["drain_until"]
        for zcv, volt in ((4003, 3950), (3800, 3780), (3712, 3700), (3690, 3650), (3500, 3400), (3420, 3331)):
            self.assertFalse(self.tick(zcv=zcv, batt_vol=volt, capacity=20, status="Discharging"))
            self.assertTrue(self.control.stopped, (zcv, volt))
            status = self.status()
            self.assertEqual((status["paused"], status["drain"], status["drain_until"]), (True, True, until))
        self.assertFalse(self.status()["valid"])  # the normal reading is invalid below 3700 mV
        self.assertEqual(self.control.changes, [True])  # paused once, never released
        self.assertEqual(self.drain_lines(),
                         [f"drain mode on: BCCT stays paused until uptime {until} s; low-battery poweroff stays active"])

    def test_a_drain_can_start_unpaused(self):
        self.tick(zcv=3800)
        self.tick(zcv=3800)
        self.assertFalse(self.control.stopped)
        self.ask()
        self.tick(zcv=3800)
        self.assertTrue(self.control.stopped)

    def test_still_powers_off_at_the_low_battery_rule(self):
        self.start_drain()
        self.assertFalse(self.tick(zcv=3420, batt_vol=3400, capacity=6, status="Discharging"))
        self.assertTrue(self.tick(zcv=3420, batt_vol=3400, capacity=5, status="Discharging"))
        self.assertTrue(self.control.stopped)  # main() releases, logs and runs poweroff after a low tick
        self.assertTrue(self.tick(zcv=3420, batt_vol=3400, capacity=3, status="Charging"))

    def test_main_releases_then_powers_off_in_drain_mode(self):
        self.start_drain()
        (self.b / "capacity").write_text("5")
        (self.b / "status").write_text("Discharging")
        calls = []
        with mock.patch.object(charging, "Guard", lambda **kwargs: self.guard), \
                mock.patch.object(charging.subprocess, "run",
                                  lambda argv, check: calls.append((argv, self.control.stopped))), \
                mock.patch.object(charging.signal, "signal"), \
                mock.patch.object(charging.time, "sleep", side_effect=AssertionError("not a low tick")):
            charging.main()
        self.assertEqual(calls, [(["poweroff"], False)])
        self.assertIn("low battery: powering off", self.log())

    def ends(self, why, change=None, **battery):
        self.start_drain()
        if change:
            change()
        self.tick(zcv=3800, **battery)
        self.assertFalse(self.control.stopped, why)
        self.assertFalse(self.status()["drain"])
        self.assertFalse(self.request.exists(), "a request the guard no longer honours is removed")
        self.assertEqual(self.drain_lines()[-1], f"drain mode ended: {why}")

    def test_invalid_reading_releases(self):
        self.ends("invalid reading", capacity="x")

    def test_unreadable_temperature_releases(self):
        self.ends("invalid reading", batt_temp="451")

    def test_charger_offline_releases(self):
        self.ends("invalid reading", lambda: (self.d / "usb").write_text("0"))

    def test_stale_gauge_releases(self):
        self.start_drain()
        for _ in range(4):
            self.now += 60
            self.up += 60
            self.guard.tick()  # activity, ZCV and CAR unchanged
        self.assertFalse(self.control.stopped)
        self.assertIn("stale gauge activity", self.log())
        self.assertEqual(self.drain_lines()[-1], "drain mode ended: invalid reading")

    def test_temperature_releases_at_40_degrees(self):
        self.start_drain()
        self.tick(zcv=3800, batt_temp=399)
        self.assertTrue(self.control.stopped)
        self.tick(zcv=3800, batt_temp=400)
        self.assertFalse(self.control.stopped)
        self.assertEqual(self.drain_lines()[-1], "drain mode ended: battery temperature 40.0 C")
        self.tick(zcv=3800, batt_temp=250)
        self.assertFalse(self.control.stopped, "latched off: the request is gone")

    def test_voltage_floor_releases(self):
        self.ends(f"voltage 3330 mV at or below {charging.DRAIN_FLOOR_MV} mV", batt_vol=3330)

    def test_disabled_limit_releases(self):
        self.ends("invalid reading",
                  lambda: (self.d / "ui.json").write_text('{"charge_limit_enabled":false,"charge_limit":80}'))

    def test_deadline_ends_drain_mode(self):
        self.tick(zcv=3800)
        self.tick(zcv=3800)
        self.ask(ahead=150)
        self.tick(zcv=3800)
        self.tick(zcv=3800)
        self.assertTrue(self.control.stopped)
        self.tick(zcv=3800)
        self.assertFalse(self.control.stopped)
        self.assertFalse(self.request.exists())
        self.assertEqual(self.drain_lines()[-1], "drain mode ended: deadline reached")

    def test_removal_ends_drain_mode_at_the_next_tick(self):
        self.start_drain()
        self.tick(zcv=3800)
        self.request.unlink()
        self.tick(zcv=3800)
        self.assertFalse(self.control.stopped)
        self.assertEqual(self.drain_lines()[-1], "drain mode ended: request removed")
        self.tick(zcv=3800)
        self.assertEqual(self.drain_lines()[-1], "drain mode ended: request removed")  # logged once

    def test_removal_above_the_resume_point_keeps_the_normal_band(self):
        self.start_drain()
        self.request.unlink()
        self.tick(zcv=4050)  # between resume (4003) and stop (4109): the normal hysteresis holds
        self.assertTrue(self.control.stopped)
        self.tick(zcv=4003)
        self.assertFalse(self.control.stopped)

    def test_clock_failure_ends_drain_mode(self):
        self.start_drain()
        self.up = None
        self.tick(zcv=3800)
        self.assertFalse(self.control.stopped)
        self.assertEqual(self.drain_lines()[-1], "drain mode ended: clock unavailable")

    def test_mode_change_ends_drain_mode(self):
        self.start_drain()
        os.chmod(self.request, 0o644)
        self.tick(zcv=3800)
        self.assertFalse(self.control.stopped)
        self.assertEqual(self.drain_lines()[-1], "drain mode ended: group or other access")

    def test_control_error_releases_and_ends(self):
        self.start_drain()
        self.control.stopped = False  # someone released BCCT behind the guard's back
        self.control.fail = "pause write failed"
        self.tick(zcv=3800)
        self.assertFalse(self.request.exists())
        self.assertFalse(self.status()["drain"])
        self.assertIn("ERROR charge control failed: [Errno 5] pause write failed", self.log())
        self.assertEqual(self.drain_lines()[-1], "drain mode ended: charge control error")

    def test_restart_rereads_the_request(self):
        self.start_drain()
        until = self.status()["drain_until"]
        self.guard = self.make_guard()  # supervise-daemon respawn after a crash
        self.tick(zcv=3700, batt_vol=3650)
        self.assertTrue(self.control.stopped)
        self.assertEqual(self.status()["drain_until"], until)
        self.assertEqual(self.drain_lines().count(
            f"drain mode on: BCCT stays paused until uptime {until} s; low-battery poweroff stays active"), 2)

    def test_clean_stop_releases_and_keeps_the_request(self):
        self.start_drain()
        self.guard.close()  # rc-service stop: BCCT released; a restarted guard resumes the drain
        self.assertFalse(self.control.stopped)
        self.assertTrue(self.request.exists())

    def test_rejected_requests_are_removed_and_logged_once(self):
        self.tick(zcv=3800)
        for ask, why in ((dict(boot="boot-b"), "other boot"), (dict(ahead=43300), "too long"),
                         (dict(ahead=-10), "expired"), (dict(mode=0o644), "group or other access")):
            self.ask(**ask)
            self.tick(zcv=3800)
            self.assertFalse(self.request.exists(), why)
            self.assertFalse(self.control.stopped)
            self.assertEqual(self.drain_lines()[-1], f"drain request rejected: {why}")
            self.tick(zcv=3800)
        self.ask()
        self.tick(zcv=3800, batt_temp=410)
        self.assertEqual(self.drain_lines()[-1], "drain request rejected: battery temperature 41.0 C")
        self.assertFalse(self.request.exists())

    def test_removal_failure_is_logged_once(self):
        self.tick(zcv=3800)
        self.request.mkdir()
        self.tick(zcv=3800)
        self.tick(zcv=3800)
        lines = self.drain_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("drain request rejected: not a regular file; removal failed (errno "))


if __name__ == "__main__":
    unittest.main()
