"""Charge policy with inert BCCT; actual kernel boundary is tested in test_i14_bcct."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rootfs/overlay/usr/lib"))
from i14ui import charging


class Control:
    def __init__(self, state_path=None):
        self.state_path = state_path
        self.stopped = False
    def paused(self):
        return Path(self.state_path).read_text() == "1" if self.state_path else self.stopped
    def set_paused(self, value):
        self.stopped = value
        if self.state_path:
            Path(self.state_path).write_text(str(int(value)))
    def release(self):
        self.set_paused(False)


class ChargeLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.b = self.d / "battery"
        self.b.mkdir()
        for key,value in {"capacity":"80", "status":"Charging", "batt_vol":"4100", "batt_temp":"250"}.items():
            (self.b/key).write_text(value)
        for key,value in {"type":"2", "usb":"1", "ac":"0", "cmd":"sentinel", "activity":"1001",
                          "gauge":"4109", "readback":"get_usb_current_unlimited=0,\ncmd_discharging = -1\n"}.items():
            (self.d/key).write_text(value)
        self.control = Control()
        self.save()
        self.guard = charging.Guard(battery=self.b, charger_type=self.d/"type", command=self.d/"cmd",
                                    readback=self.d/"readback", usb=self.d/"usb", ac=self.d/"ac",
                                    settings=self.d/"ui.json", log=self.d/"log", gauge=self.d/"gauge",
                                    gauge_activity=self.d/"activity", control=self.control,
                                    status_path=self.d/"status.json")
        # These tests cover steady policy; cold-start proof has its own regression tests.
        self.guard.activity, self.guard.activity_changed = 1000, time.monotonic()

    def save(self, **changes):
        data = dict(charge_limit_enabled=True, charge_limit=80)
        data.update(changes)
        (self.d/"ui.json").write_text(json.dumps(data))

    def step(self, zcv=4109, paused=True, cap=None):
        (self.d/"gauge").write_text(str(zcv))
        if cap is not None:
            (self.b/"capacity").write_text(str(cap))
        result = self.guard.tick()
        self.assertEqual(self.control.paused(), paused)
        return result

    def test_hysteresis_stops_and_resumes_at_voltage_landmarks(self):
        for voltage, paused in ((4108,False),(4109,True),(4067,True),(4004,True),(4003,False),(4067,False)):
            self.step(voltage,paused)
        self.assertEqual((self.d/"cmd").read_text(),"sentinel")

    def test_band_works_while_capacity_is_held_at_100(self):
        self.step(4109,True,cap=100)
        self.step(4003,False,cap=100)
        self.assertEqual((self.b/"capacity").read_text(),"100")
        self.assertEqual((self.d/"cmd").read_text(),"sentinel")

    def test_all_offered_limits(self):
        for limit,stop,resume in ((60,3924,3850),(70,4003,3924),(80,4109,4003),(90,4213,4109),(100,4324,4213)):
            self.save(charge_limit=limit)
            self.step(stop,True)
            self.step(resume,False)

    def test_disable_raise_and_reenable(self):
        self.step()
        self.save(charge_limit=90)
        self.step(paused=False)
        self.save()
        self.step()
        self.save(charge_limit_enabled=False)
        self.step(paused=False)

    def test_bad_or_missing_settings_release_without_overwriting(self):
        for text in ('{broken','[]','{"charge_limit_enabled":"yes"}',
                     '{"charge_limit_enabled":true,"charge_limit":85}',
                     '{"charge_limit_enabled":true,"charge_limit":80.0}',
                     '{"pin":"broken","charge_limit_enabled":true}'):
            self.save();self.step()
            (self.d/"ui.json").write_text(text)
            self.step(paused=False)
            self.assertEqual((self.d/"ui.json").read_text(),text)
        (self.d/"ui.json").unlink()
        self.step(paused=False)

    def test_bad_measurements_and_missing_cable_release(self):
        cases=[(self.b/"capacity",v) for v in ("","?","-1","101","80.0")]
        cases += [(self.b/"status",""),(self.b/"status","Unknown"),(self.b/"batt_vol","3600"),
                  (self.b/"batt_temp","460"),(self.d/"type","?"),(self.d/"readback",""),
                  (self.d/"usb","0"),(self.d/"usb","?")]
        for path,bad in cases:
            with self.subTest(path=path.name,bad=bad):
                self.step();original=path.read_text();path.write_text(bad)
                self.guard.tick();self.assertFalse(self.control.paused());path.write_text(original)
        self.step();(self.b/"capacity").unlink();self.guard.tick();self.assertFalse(self.control.paused())

    def test_unreadable_or_frozen_gauge_releases_without_capacity_fallback(self):
        self.step()
        (self.d/"gauge").write_text("garbage")
        self.guard.tick();self.assertFalse(self.control.paused())
        (self.d/"gauge").unlink()
        self.guard.tick();self.assertFalse(self.control.paused())
        self.guard.activity_changed=None
        self.step(paused=False,cap=100)

    def test_low_battery_guard_keeps_thresholds(self):
        for status,cap,off in (("Discharging",5,True),("Discharging",6,False),("Charging",4,False),
                               ("Charging",3,True),("Full",3,True),("Cmd discharging",5,True)):
            (self.b/"status").write_text(status)
            self.assertEqual(self.step(zcv=3690,paused=False,cap=cap),off)
        (self.b/"capacity").write_text("?")
        self.assertFalse(self.guard.tick())

    def test_low_voltage_estimate_does_not_poweroff_above_capacity_threshold(self):
        self.assertFalse(self.step(3690,False,cap=50))

    def test_ocv_table_mapping(self):
        self.assertEqual([charging.ocv_percent(v) for v in (None,3000,3400,3712,4003,4016,4101,4109,4324,4400)],
                         [None,0,0,15,70,71,79,80,100,100])

    def display_sample(self, zcv, current, advance=60, paused=True):
        if not hasattr(self, "now"):
            # Integer base: float monotonic can round an exact +60s step to 59.999... and skip the move.
            self.now = [int(time.monotonic())]
            boot=self.d/"boot_id";boot.write_text("test-boot")
            patch=mock.patch.object(charging,"BOOT_ID",str(boot))
            patch.start();self.addCleanup(patch.stop)
            self.guard.boot_id="test-boot"
        self.now[0] += advance
        self.guard.clock = lambda: self.now[0]
        (self.d/"activity").write_text(str(current))
        self.step(zcv, paused)
        return json.loads((self.d/"status.json").read_text())

    def test_display_does_not_rise_during_confirmed_pause(self):
        self.assertEqual(self.display_sample(4113,1500,0)["level"],80)
        snapshot=self.display_sample(4172,6000)
        self.assertEqual(snapshot["level"],80)
        self.assertEqual(snapshot["raw_level"],86)
        self.assertEqual(snapshot["zcv_mv"],4172)
        self.assertEqual(self.display_sample(4158,1400)["level"],80)

    def test_display_moves_gradually_with_current_direction(self):
        self.assertEqual(self.display_sample(3962,-6000,0,False)["level"],65)
        self.assertEqual(self.display_sample(3924,-6100,paused=False)["level"],65)
        snapshot=self.display_sample(4003,-6200,paused=False)
        self.assertEqual((snapshot["level"],snapshot["raw_level"]),(66,70))
        self.assertEqual(self.display_sample(4016,-6300,10,False)["level"],66)
        self.assertEqual(self.display_sample(4016,-6400,60,False)["level"],67)

    def test_display_lag_does_not_delay_hardware_resume(self):
        self.display_sample(4113,1500,0)
        self.assertEqual(self.display_sample(4004,6000)["level"],79)
        snapshot=self.display_sample(4003,6100,paused=False)
        self.assertEqual((snapshot["level"],snapshot["raw_level"]),(78,70))
        self.assertFalse(snapshot["paused"])
        self.assertEqual((self.d/"cmd").read_text(),"sentinel")

    def test_unknown_measurement_retires_display_tracking(self):
        self.display_sample(4113,1500,0)
        (self.d/"gauge").write_text("unavailable")
        self.guard.tick()
        self.assertIsNone(json.loads((self.d/"status.json").read_text())["level"])
        self.assertEqual(self.display_sample(4003,1600,paused=False)["level"],70)

    def test_fresh_restart_preserves_display_and_rate_limit(self):
        self.display_sample(3962,-6000,0,False)
        self.assertEqual(self.display_sample(4003,-6100,paused=False)["level"],66)
        self.now[0] += 10
        self.guard = charging.Guard(battery=self.b, charger_type=self.d/"type", command=self.d/"cmd",
            readback=self.d/"readback", usb=self.d/"usb", ac=self.d/"ac", settings=self.d/"ui.json",
            log=self.d/"log", gauge=self.d/"gauge", gauge_activity=self.d/"activity", control=self.control,
            status_path=self.d/"status.json", clock=lambda:self.now[0])
        self.assertEqual(self.display_sample(4016,-6200,0,False)["level"],66)
        self.assertEqual(self.display_sample(4016,-6300,60,False)["level"],67)

    def test_legacy_migration_happens_once_with_confirmed_readback(self):
        (self.d/"cmd").write_text("")
        (self.d/"readback").write_text("get_usb_current_unlimited=1,\ncmd_discharging = 1\n")
        original=self.guard.write_command
        def acknowledge(unlimited,paused):
            original(unlimited,paused)
            (self.d/"readback").write_text("get_usb_current_unlimited=0,\ncmd_discharging = 0\n")
        with mock.patch.object(self.guard,"write_command",side_effect=acknowledge) as writer:
            self.step();self.step(4003,False);self.step()
        self.assertEqual(writer.call_count,1)
        self.assertEqual((self.d/"cmd").read_text(),"0 0\n")

    def test_legacy_migration_failure_releases_bcct_and_reports_error(self):
        (self.d/"readback").write_text("get_usb_current_unlimited=1,\ncmd_discharging = 1\n")
        with mock.patch.object(self.guard,"write_command",side_effect=OSError("write failed")):
            self.step(paused=False)
        self.assertIn("write failed",(self.d/"log").read_text())

    def test_failed_migration_is_not_retried_next_tick(self):
        (self.d/"readback").write_text("get_usb_current_unlimited=1,\ncmd_discharging = 1\n")
        with mock.patch.object(self.guard,"write_command",wraps=self.guard.write_command) as writer:
            self.step(paused=False)
            self.step(paused=False)
        self.assertEqual(writer.call_count,1)
        self.assertTrue(self.guard.migrated)

    def test_pause_write_failure_attempts_release_and_recovers(self):
        original=self.control.set_paused
        def fail(value):
            if value:raise OSError("pause failed")
            original(value)
        with mock.patch.object(self.control,"set_paused",side_effect=fail):
            self.step(paused=False)
        self.step();self.guard.release();self.assertFalse(self.control.paused())

    def test_steady_state_never_reenables_over_stock_safety(self):
        self.step(4003,False)
        with mock.patch.object(self.control,"set_paused",wraps=self.control.set_paused) as writer:
            self.step(4067,False)
        writer.assert_not_called()
        self.assertEqual((self.d/"cmd").read_text(),"sentinel")

    def test_external_legacy_override_is_visible_without_repeated_resets(self):
        self.step()
        (self.d/"readback").write_text("get_usb_current_unlimited=1,\ncmd_discharging = 1\n")
        with mock.patch.object(self.guard,"write_command") as writer:
            self.step(paused=False)
        writer.assert_not_called()
        self.assertIn("outside i14-power",(self.d/"log").read_text())

    def test_service_sigterm_releases_while_waiting(self):
        state=self.d/"state";state.write_text("0")
        code="""import sys,time
from pathlib import Path
from i14ui import charging
from test_i14_charge_limit import Control
d=Path(sys.argv[1]);c=Control(d/'state')
g=charging.Guard(battery=d/'battery',charger_type=d/'type',command=d/'cmd',readback=d/'readback',
 usb=d/'usb',ac=d/'ac',settings=d/'ui.json',log=d/'log',gauge=d/'gauge',gauge_activity=d/'activity',
 control=c,status_path=d/'status.json')
g.activity=1000;g.activity_changed=time.monotonic()
charging.Guard=lambda **kwargs:g
charging.main()
"""
        env=dict(os.environ,PYTHONPATH=str(ROOT/'rootfs/overlay/usr/lib')+os.pathsep+str(ROOT/'tests'))
        with subprocess.Popen([sys.executable,"-B","-c",code,str(self.d)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as proc:
            try:
                end=time.monotonic()+5
                while state.read_text()!="1" and time.monotonic()<end:time.sleep(.02)
                self.assertEqual(state.read_text(),"1")
            finally:
                proc.terminate();out,err=proc.communicate(timeout=5)
            self.assertEqual(proc.returncode,0,err)
        self.assertEqual(state.read_text(),"0")
        snapshot=charging.read_status(self.d/"status.json")
        self.assertTrue(snapshot is None or not snapshot["paused"])

if __name__=="__main__":unittest.main()
