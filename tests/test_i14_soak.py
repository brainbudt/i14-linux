"""Offline soak sampling/reporting; all device paths and commands are isolated."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SAMPLER = ROOT / "scripts/i14-soak-sample"
REPORT = ROOT / "scripts/i14-soak-report.py"
SERVICES = ("i14-power", "i14-cpufreq", "i14-ui", "unbound", "i14-wifi", "i14-netwatch", "sshd")


class Sampler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.tree = self.d / "device"
        self.bin = self.d / "bin"
        self.bin.mkdir()
        self.log = self.d / "soak/samples.jsonl"
        self.cron = self.d / "root.crontab"
        timeout = shutil.which("timeout")
        if timeout is None:
            # macOS has no timeout. Emulate this invocation with a real deadline.
            timeout = str(self.bin / "timeout")
            self.command("timeout", f"#!{shutil.which('python3')}\n"
                         "import subprocess, sys\n"
                         "try:\n    sys.exit(subprocess.run(sys.argv[4:], timeout=float(sys.argv[3])).returncode)\n"
                         "except subprocess.TimeoutExpired:\n    sys.exit(137)\n")
        self.env = dict(((k, v) for k, v in os.environ.items() if not k.startswith("I14_")),
                        I14_SOAK_ROOT=str(self.tree), I14_SOAK_LOG=str(self.log),
                        I14_SOAK_LOCK=str(self.d / "lock"), I14_SOAK_CRONTAB=str(self.cron),
                        I14_SOAK_RC_SERVICE=str(self.bin / "rc-service"),
                        I14_SOAK_BUSYBOX=str(self.bin / "busybox"),
                        I14_SOAK_IP=str(self.bin / "ip"),
                        I14_SOAK_TIMEOUT=timeout,
                        PATH=f"{self.bin}:{os.environ['PATH']}")
        self.command("rc-service", '#!/bin/sh\n[ "$1" != i14-ui ]\n')
        self.command("busybox", '#!/bin/sh\n[ "$1" = nslookup ] && [ "$3" = 127.0.0.1 ]\n')
        self.command("ip", '#!/bin/sh\nprintf "3: wlan0 inet 192.0.2.14/24 scope global wlan0\\n"\n')
        self.files = {
            "proc/sys/kernel/random/boot_id": "boot-a\n",
            "proc/uptime": "1234.56 890.00\n", "proc/loadavg": "0.01 0.02 0.03 1/42 123\n",
            "proc/meminfo": "MemTotal: 1000000 kB\nMemAvailable: 654321 kB\n",
            "proc/mtk_battery_cmd/battery_cmd": "=> get_usb_current_unlimited=1,\ncmd_discharging = -1\n",
            "sys/class/power_supply/battery/capacity": "79\n",
            "sys/class/power_supply/battery/status": 'Charging "ok"\\test\nsecond\tline\x01\n',
            "sys/class/power_supply/battery/batt_vol": "4100\n",
            "sys/class/power_supply/battery/batt_temp": "250\n",
            "sys/devices/platform/battery_meter/FG_Current": "-1234\n",
            "sys/devices/platform/battery_meter/FG_g_fg_dbg_bat_zcv": "4109\n",
            "run/i14-charge-status.json": '{"schema":1,"backend":"bcct","paused":true}\n',
            "proc/driver/thermal/clbcct": "klog 0\ncurr_limit 1\nmtk-cl-bcct00 1 mA, state 1\n",
            "sys/devices/platform/battery/Charger_Type": "2\n",
            "sys/class/power_supply/usb/online": "1\n", "sys/class/power_supply/ac/online": "0\n",
            "sys/class/leds/lcd-backlight/brightness": "0\n",
            "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq": "299000\n",
            "proc/cpufreq/cpufreq_limited_max_freq_by_user": "299000\n",
            "proc/cpufreq/cpufreq_fix_freq_in_es": "0\n",
            "var/log/i14-battery.log": 'old\nlast "quoted" \\ line\t\x02\n',
        }
        for path, value in self.files.items():
            self.write(path, value)
        # A read would block the test: the dangerous node must never be opened.
        os.mkfifo(self.tree / "proc/mtk_battery_cmd/current_cmd")

    def command(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def write(self, path, value):
        dest = self.tree / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(value)

    def run_sample(self, *args):
        shell = [shutil.which("busybox"), "sh"] if shutil.which("busybox") else ["sh"]
        return subprocess.run([*shell, str(SAMPLER), *args], env=self.env,
                              capture_output=True, text=True, timeout=4, check=True)

    def sample(self):
        self.run_sample()
        return json.loads(self.log.read_text().splitlines()[-1])

    def test_measurements_and_escaping_without_device_writes(self):
        row = self.sample()
        self.assertEqual(row["boot_id"], "boot-a")
        self.assertEqual(row["uptime_seconds"], 1234.56)
        self.assertEqual(row["capacity"], 79)
        self.assertEqual(row["status"], self.files["sys/class/power_supply/battery/status"].strip())
        self.assertEqual(row["fg_current_deci_ma"], -1234)
        self.assertEqual(row["zcv_mv"], 4109)
        self.assertTrue(json.loads(row["charge_status"])["paused"])
        self.assertIn("curr_limit 1",row["bcct_state"])
        self.assertEqual(row["cmd_discharging"], -1)
        self.assertEqual(row["get_usb_current_unlimited"], 1)
        self.assertEqual(row["batt_vol_mv"], 4100)
        self.assertEqual(row["batt_temp_deci_c"], 250)
        self.assertEqual(row["charger_type"], 2)
        self.assertEqual((row["usb_online"], row["ac_online"]), (1, 0))
        self.assertEqual(row["backlight_brightness"], 0)
        self.assertEqual(row["scaling_cur_freq_khz"], 299000)
        self.assertEqual(row["cpufreq_limited_max_freq_by_user"], 299000)
        self.assertEqual(row["cpufreq_fix_freq_in_es"], 0)
        self.assertEqual(row["loadavg"], "0.01 0.02 0.03 1/42 123")
        self.assertEqual(row["mem_available_kb"], 654321)
        self.assertEqual(row["wlan0_ipv4"], "192.0.2.14/24")
        self.assertEqual(row["services"]["i14-ui"], 1)
        self.assertEqual(row["services"]["i14-cpufreq"], 0)
        self.assertTrue(row["dns"]["ok"])
        self.assertGreaterEqual(row["dns"]["ms"], 0)
        self.assertIn('last "quoted" \\ line\t\x02', row["battery_log_last"])
        self.assertIsInstance(row["epoch"], int)
        self.assertTrue(row["iso"].endswith("Z"))
        for path, value in self.files.items():
            self.assertEqual((self.tree / path).read_text(), value)

    def test_missing_files_and_commands_are_null(self):
        shutil.rmtree(self.tree)
        self.command("ip", "#!/bin/sh\nexit 1\n")
        self.command("rc-service", "#!/bin/sh\nexit 127\n")
        self.command("busybox", "#!/bin/sh\nexit 1\n")
        row = self.sample()
        for key in ("boot_id", "uptime_seconds", "capacity", "status", "cmd_discharging",
                    "mem_available_kb", "wlan0_ipv4", "battery_log_last", "scaling_cur_freq_khz"):
            self.assertIsNone(row[key], key)
        self.assertFalse(row["dns"]["ok"])
        self.assertEqual(row["services"]["sshd"], 127)

    def test_rotation_and_lock(self):
        self.sample()
        original = self.log.read_bytes()
        self.env["I14_SOAK_MAX_BYTES"] = "1"
        self.sample()
        self.assertEqual(Path(str(self.log) + ".1").read_bytes(), original)
        before = self.log.read_bytes()
        if shutil.which("flock"):
            import fcntl
            with open(self.env["I14_SOAK_LOCK"], "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.run_sample()
        else:
            lock = Path(self.env["I14_SOAK_LOCK"] + ".d")
            lock.mkdir()
            before = self.log.read_bytes()
            self.run_sample()
            lock.rmdir()
        self.assertEqual(self.log.read_bytes(), before)

    def test_partial_log_tail_is_separated_from_next_sample(self):
        self.log.parent.mkdir()
        self.log.write_text('{"partial":')
        row = self.sample()
        self.assertEqual(row["capacity"], 79)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_flock_is_held_for_the_whole_sample(self):
        # Exercise descriptor locking also on macOS, which has no flock CLI.
        self.command("flock", f"#!{shutil.which('python3')}\n"
                     "import fcntl, sys\n"
                     "try:\n    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                     "except BlockingIOError:\n    sys.exit(1)\n")
        self.sample()
        import fcntl
        with open(self.env["I14_SOAK_LOCK"], "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.log.read_bytes()
            self.run_sample()
            self.assertEqual(self.log.read_bytes(), before)

    def test_slow_probes_run_in_parallel_and_time_out(self):
        self.command("rc-service", "#!/bin/sh\nexec sleep 5\n")
        self.command("busybox", "#!/bin/sh\nexec sleep 5\n")
        start = time.monotonic()
        row = self.sample()
        self.assertLess(time.monotonic() - start, 3)
        self.assertTrue(all(rc != 0 for rc in row["services"].values()))
        self.assertFalse(row["dns"]["ok"])

    def test_install_uninstall_preserve_other_cron_jobs(self):
        original = "# keep this\n5 * * * * echo other\n"
        self.cron.write_text(original)
        self.run_sample("install")
        self.run_sample("install")
        installed = self.cron.read_text()
        self.assertEqual(installed.count("# i14-soak-sample"), 1)
        self.assertIn(original, installed)
        self.assertIn(str(SAMPLER), installed)
        self.run_sample("uninstall")
        self.run_sample("uninstall")
        self.assertEqual(self.cron.read_text(), original)
        self.assertFalse(self.log.exists())


class Report(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("i14_soak_report", REPORT)
        self.report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.report)

    def row(self, t, cap=75, cmd=0, boot="a", up=None, down=False, screen=0):
        return {"epoch": 1800000000 + t, "boot_id": boot, "uptime_seconds": 1000 + t if up is None else up,
                "capacity": cap, "cmd_discharging": cmd, "usb_online": 1, "ac_online": 0,
                "fg_current_deci_ma": 100 if cmd == 1 else -1000, "backlight_brightness": screen,
                "scaling_cur_freq_khz": 299000 if screen == 0 else 819000,
                "batt_temp_deci_c": 250, "mem_available_kb": 600000 - t,
                "dns": {"ok": not down, "ms": 10},
                "services": {name: (1 if down and name == "i14-cpufreq" else 0) for name in SERVICES}}

    def bcct_row(self,t,level,zcv,paused):
        row=self.row(t,100,-1)
        row["get_usb_current_unlimited"]=0
        row["charge_status"]=json.dumps(dict(schema=1,backend="bcct",boot_id="a",updated_monotonic=row["uptime_seconds"],
          level=level,zcv_mv=zcv,paused=paused,valid=True,enabled=True,limit=80,stop_mv=4109,resume_mv=4003,error=None))
        row["bcct_state"]=f"klog 0\ncurr_limit {1 if paused else 65535}\nmtk-cl-bcct00 1 mA, state {int(paused)}"
        return row

    def test_bcct_cycle_uses_effective_pause_not_legacy_flag(self):
        rows=[self.bcct_row(0,71,4016,True),self.bcct_row(60,70,4003,False),self.bcct_row(120,80,4109,True)]
        result=self.report.analyze(rows)["charge"]
        self.assertEqual([(c["level"],c["result"]) for c in result["threshold_checks"]],[(70,"observed"),(80,"observed")])
        self.assertEqual(result["observed_seconds"]["paused"],60)
        self.assertEqual(result["observed_seconds"]["charging"],60)
        self.assertEqual(len(result["transitions"]),2)

    def test_bcct_report_uses_raw_estimate_when_display_is_smoothed(self):
        rows=[self.bcct_row(0,79,4004,True),self.bcct_row(60,78,4003,False),self.bcct_row(120,79,4109,True)]
        for row,raw in zip(rows,(70,70,80)):
            status=json.loads(row["charge_status"]);status["raw_level"]=raw;row["charge_status"]=json.dumps(status)
        checks=self.report.analyze(rows)["charge"]["threshold_checks"]
        self.assertEqual([(c["level"],c["result"]) for c in checks],[(70,"observed"),(80,"observed")])
        for bad in (-1,101,True,70.0,"70",None):
            status=json.loads(rows[1]["charge_status"]);status["raw_level"]=bad
            row=dict(rows[1],charge_status=json.dumps(status))
            self.assertIsNone(self.report.level_of(row),bad)

    def test_missing_stale_wrong_boot_bcct_status_is_unknown(self):
        for change in (None,{"updated_monotonic":849},{"boot_id":"old"},{"error":"readback failed"}):
            row=self.bcct_row(0,80,4109,True)
            if change is None:row["charge_status"]=None
            else:
                status=json.loads(row["charge_status"]);status.update(change);row["charge_status"]=json.dumps(status)
            self.assertEqual(self.report.charge_state(row),"unknown",change)
            self.assertIsNone(self.report.level_of(row),change)

    def test_bcct_bypass_and_legacy_stop_never_count_as_band_evidence(self):
        for unlimited,cmd in ((1,-1),(0,1),(None,-1),(0,None)):
            rows=[self.bcct_row(0,70,4003,False),self.bcct_row(60,80,4109,True)]
            for row in rows:
                row["get_usb_current_unlimited"]=unlimited;row["cmd_discharging"]=cmd
                self.assertEqual(self.report.charge_state(row),"unknown")
            self.assertEqual(self.report.analyze(rows)["charge"]["threshold_checks"],[])

    def test_bcct_release_above_voltage_threshold_is_not_band_evidence(self):
        rows=[self.bcct_row(0,71,4016,True),self.bcct_row(60,70,4010,False)]
        self.assertEqual(self.report.analyze(rows)["charge"]["threshold_checks"][0]["result"],"outside_band")

    def test_cycle_uses_the_gauge_when_capacity_is_held(self):
        rows = [self.row(0, 100, 1), self.row(60, 100, 0), self.row(120, 100, 1)]
        for row, gauge in zip(rows, (71, 70, 80)):
            row["fg_percentage"] = 100  # the coulomb counter is not what the guard uses
            row["battery_log_last"] = f"... capacity=100 gauge={gauge} zcv=4000 status=Cmd discharging ..."
        checks = self.report.analyze(rows)["charge"]["threshold_checks"]
        self.assertEqual([(c["level"], c["result"]) for c in checks], [(70, "observed"), (80, "observed")])

    def test_cycle_follows_the_guard_when_the_gauge_is_not_trusted(self):
        # Frozen gauge (AUXADC fallback): the guard logs gauge=None and resumes on capacity.
        rows = [self.row(0, 71, 1), self.row(60, 70, 0)]
        for row in rows:
            row["fg_percentage"] = 100
            row["battery_log_last"] = "... capacity=71 gauge=None status=Cmd discharging ..."
        checks = self.report.analyze(rows)["charge"]["threshold_checks"]
        self.assertEqual([(c["level"], c["result"]) for c in checks], [(70, "observed")])

    def test_cycle_gaps_services_and_statistics(self):
        rows = [self.row(0, 100, 1), self.row(60, 80, 1, down=True),
                self.row(120, 70, 0), self.row(180, 75, -1, screen=100), self.row(240, 80, 1),
                self.row(600, 79, 1), self.row(1200, 75, 0, boot="b", up=30),
                self.row(1260, 76, -1, boot="b", up=90)]
        result = self.report.analyze(rows)
        self.assertEqual(result["samples"], 8)
        self.assertEqual([g["kind"] for g in result["gaps"]], ["missed_samples", "reboot"])
        self.assertEqual(result["reboots"][0]["downtime_upper_bound_seconds"], 570)
        self.assertEqual(result["services_down_minutes"]["i14-cpufreq"], 1)
        self.assertEqual(result["dns"]["failures"], 1)
        charge = result["charge"]
        self.assertEqual((charge["capacity_min"], charge["capacity_max"]), (70, 100))
        self.assertEqual(len(charge["transitions"]), 5)  # includes the driver's 0 -> -1 acknowledgements
        self.assertEqual([c["result"] for c in charge["threshold_checks"]], ["observed", "observed", "unobserved"])
        self.assertEqual(charge["observed_seconds"]["paused"], 120)
        self.assertEqual(charge["observed_seconds"]["charging"], 180)
        self.assertEqual(result["current_ma"]["screen_on"]["mean"], -100)
        self.assertEqual(result["cpu_screen_off"]["299000"]["samples"], 7)
        self.assertEqual(result["temperature_c"], {"min": 25.0, "max": 25.0})
        self.assertEqual(result["memory_kb"]["last"], 598740)

    def test_wrong_threshold_and_no_claim_across_gap_or_reboot(self):
        result = self.report.analyze([self.row(0, 75), self.row(60, 76, 1), self.row(120, 74),
                                      self.row(600, 80, 1), self.row(660, 70, boot="b", up=20)])
        self.assertEqual([c["result"] for c in result["charge"]["threshold_checks"]],
                         ["outside_band", "outside_band", "unobserved", "unobserved"])
        self.assertEqual(result["charge"]["observed_seconds"]["charging"], 60)

    def test_clock_jump_and_unknowns_do_not_inflate_durations(self):
        rows = [self.row(0, down=True), self.row(3600, up=1060, down=True),
                {"epoch": 1800003660, "boot_id": None}]
        result = self.report.analyze(rows)
        self.assertEqual(result["services_down_minutes"]["i14-cpufreq"], 1)
        self.assertEqual(len(result["clock_jumps"]), 1)
        self.assertFalse(result["reboots"])

    def test_reboot_retains_poweroff_context(self):
        before = self.row(0, 3, boot="a", up=2000)
        before["battery_log_last"] = "low battery: poweroff"
        after = self.row(7200, 10, boot="b", up=40)
        result = self.report.analyze([before, after])
        reboot = result["reboots"][0]
        self.assertEqual(reboot["last_capacity"], 3)
        self.assertEqual(reboot["first_capacity"], 10)
        self.assertEqual(reboot["last_battery_log"], "low battery: poweroff")

    def test_cli_truncated_tail_json_and_empty_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "samples.jsonl"
            path.write_text(json.dumps(self.row(0)) + '\n{"epoch":')
            proc = subprocess.run(["python3", "-B", str(REPORT), str(path), "--json"],
                                  capture_output=True, text=True, check=True)
            result = json.loads(proc.stdout)
            self.assertEqual(result["samples"], 1)
            self.assertEqual(result["ignored_lines"], 1)
            proc = subprocess.run(["python3", "-B", str(REPORT), str(path)],
                                  capture_output=True, text=True, check=True)
            self.assertIn("Samples: 1", proc.stdout)
            self.assertIn("not proven", proc.stdout)
            path.write_text("")
            proc = subprocess.run(["python3", "-B", str(REPORT), str(path), "--json"],
                                  capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(proc.stdout)["samples"], 0)


if __name__ == "__main__":
    unittest.main()
