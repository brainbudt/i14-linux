"""Battery-current A/B/A analysis and cleanup; no phone access."""
import argparse
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/i14-cpu-power-aba.py"


def sample(elapsed=0, **changes):
    item = dict(event="sample", elapsed_s=elapsed, monotonic=1000 + elapsed,
                wall_time="2026-11-05T12:00:00+00:00", boot_id="same-boot",
                FG_Current="1234", batt_vol="4200", capacity="90",
                status="Cmd discharging", charger_type="2",
                battery_cmd="=> get_usb_current_unlimited=1,\ncmd_discharging = 1",
                backlight="0", cpufreq_state="dvfs_disable_by_early_suspend = 1\n",
                cpufreq_limited_max_freq_by_user="299000", cpufreq_fix_freq_in_es="0",
                scaling_cur_freq="299000", physical_clock="299000 KHz",
                batt_temp="240", loadavg="6.00 6.00 6.00 1/80 123")
    item.update(changes)
    return item


class CpuPowerABA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.exists():
            raise AssertionError("CPU power measurement tool is not implemented")
        spec = importlib.util.spec_from_file_location("cpu_power_aba", SCRIPT)
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    def parsed(self, elapsed=0, **changes):
        return self.tool.parse_sampler_line(json.dumps(sample(elapsed, **changes)))

    def block(self, values=None, **kwargs):
        values = values or [sample(t) for t in (0, 5, 10)]
        lines = [json.dumps(s) for s in values]
        lines.append(json.dumps(dict(event="end", complete=True, elapsed_s=10)))
        return self.tool.summarize_block(lines, "A", 5, 10, self.parsed(), **kwargs)

    def test_parse_signed_current_units_and_physical_clock(self):
        s = self.parsed(FG_Current="-1234")
        self.assertAlmostEqual(s["current_ma"], -123.4)
        self.assertEqual(s["voltage_v"], 4.2)
        self.assertEqual(s["physical_khz"], 299000)
        self.assertEqual(s["early_suspend"], 1)
        self.assertEqual(s["battery_flags"]["cmd_discharging"], 1)

    def test_bad_or_missing_sampler_fields_are_not_zero(self):
        for line in ("garbage", "{}", json.dumps(sample(FG_Current="ERROR")),
                     json.dumps(sample(physical_clock="unknown")),
                     json.dumps(sample(monotonic=float("nan")))):
            with self.subTest(line=line), self.assertRaises(ValueError):
                self.tool.parse_sampler_line(line)

    def test_preconditions_reject_screen_charge_capacity_and_charger_changes(self):
        cases = [dict(backlight="1"), dict(cpufreq_state="dvfs_disable_by_early_suspend = 0"),
                 dict(battery_cmd="get_usb_current_unlimited=1, cmd_discharging = 0"),
                 dict(battery_cmd="get_usb_current_unlimited=0, cmd_discharging = 1"),
                 dict(status="Charging"), dict(capacity="20"), dict(capacity="19"),
                 dict(charger_type="1"), dict(boot_id="another-boot")]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertTrue(self.tool.invalid_reasons(self.parsed(**changes), self.parsed()))

    def test_normal_capacity_fall_above_threshold_is_allowed(self):
        self.assertFalse(self.tool.invalid_reasons(self.parsed(capacity="89"), self.parsed()))

    def test_violation_during_settling_invalidates_entire_block(self):
        b = self.block([sample(0, backlight="50"), sample(5), sample(10)])
        self.assertFalse(b["valid"])
        self.assertNotIn("mean_current_ma", b)

    def test_settling_is_dropped_and_power_is_mean_of_sample_products(self):
        b = self.block([sample(0, FG_Current="9000"),
                        sample(5, FG_Current="1000", batt_vol="4000"),
                        sample(10, FG_Current="2000", batt_vol="4200")])
        self.assertTrue(b["valid"], b)
        self.assertEqual(b["n"], 2)
        self.assertEqual(b["mean_current_ma"], 150)
        self.assertEqual(b["median_current_ma"], 150)
        self.assertAlmostEqual(b["mean_voltage_v"], 4.1)
        self.assertEqual(b["mean_power_mw"], 620)
        self.assertEqual(b["physical_clock_distribution"]["299000"]["count"], 2)

    def test_block_must_show_the_requested_policy_state(self):
        stopped = dict(cpufreq_fix_freq_in_es="1", cpufreq_limited_max_freq_by_user="0")
        self.assertIn("policy not active in A block", self.block([sample(t, **stopped) for t in (0, 5, 10)])["reasons"])
        unchanged = [json.dumps(sample(t)) for t in (0, 5, 10)] + [json.dumps(dict(event="end", complete=True, elapsed_s=10))]
        b = self.tool.summarize_block(unchanged, "B", 5, 10, self.parsed())
        self.assertIn("policy still active in B block", b["reasons"])

    def test_sparse_sampling_is_rejected(self):
        b = self.block([sample(0), sample(10)], interval=4)
        self.assertIn("sampling gap over twice the interval", b["reasons"])
        self.assertTrue(self.block(interval=5)["valid"])

    def test_incomplete_or_corrupt_block_is_invalid(self):
        for lines in ([json.dumps(sample())], ["broken"]):
            b = self.tool.summarize_block(lines, "B", 0, 10, self.parsed())
            self.assertFalse(b["valid"])

    def test_sampler_checks_each_sample_and_stops_on_charge_transition(self):
        output = io.StringIO()
        values = [sample(0), sample(5, status="Charging"), sample(10)]
        with patch.object(self.tool, "read_sample", side_effect=values) as read, \
                patch.object(self.tool.time, "sleep"), \
                patch.object(self.tool.time, "monotonic", return_value=1000), \
                patch("sys.stdout", output):
            self.tool.sampler(10, 5, self.parsed())
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(read.call_count, 2)
        self.assertIn("charging is not paused", records[-2]["violations"])
        self.assertFalse(records[-1]["complete"])

    def test_aba_contrast_uses_neighbours_and_reports_pair_spread(self):
        blocks = [dict(label=label, valid=True, mean_current_ma=current,
                       mean_power_mw=current * 4)
                  for label, current in zip("ABABA", [100, 130, 110, 150, 120])]
        result = self.tool.contrast(blocks)
        self.assertEqual([p["saved_current_ma"] for p in result["pairs"]], [25, 35])
        self.assertEqual(result["saved_current_ma"]["mean"], 30)
        self.assertEqual(result["saved_current_ma"]["range"], 10)
        self.assertAlmostEqual(result["saved_current_ma"]["stdev"], 50 ** 0.5)
        self.assertEqual(result["saved_power_mw"]["mean"], 120)
        blocks[0]["valid"] = False
        self.assertEqual(len(self.tool.contrast(blocks)["pairs"]), 1)
        blocks[2]["valid"] = False
        self.assertEqual(self.tool.contrast(blocks)["pairs"], [])

    def fake_runner(self, command, *, source=None, output=None, timeout=30):
        if command == "rc-service i14-cpufreq stop":
            self.policy = dict(cpufreq_fix_freq_in_es="1", cpufreq_limited_max_freq_by_user="0")
        elif command == "rc-service i14-cpufreq start":
            self.policy = {}
        if source is not None:
            duration = float(shlex.split(command)[4])
            times = (0,) if duration == 0 else (0, 5, 10)
            for t in times:
                output.write(json.dumps(sample(t, **getattr(self, "policy", {}))) + "\n")
            output.write(json.dumps(dict(event="end", complete=True, elapsed_s=duration)) + "\n")
        return subprocess.CompletedProcess(command, 0, " * status: started\n", "")

    def args(self, directory):
        return argparse.Namespace(sequence="ABA", block_seconds=10, sample_interval=5,
                                  settle_seconds=5, output_dir=Path(directory) / "measurement")

    def test_restore_start_and_status_run_on_exception_after_stop(self):
        def fail_after_stop(command, **kwargs):
            if command == "rc-service i14-cpufreq stop":
                raise RuntimeError("simulated lost SSH acknowledgement")
            return self.fake_runner(command, **kwargs)

        runner = Mock(side_effect=fail_after_stop)
        with tempfile.TemporaryDirectory() as directory:
            summary = self.tool.run_experiment(self.args(directory), runner)
            saved = json.loads((self.args(directory).output_dir / "summary.json").read_text())
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(commands[-2:], ["rc-service i14-cpufreq start", "rc-service i14-cpufreq status"])
        self.assertIn("simulated lost SSH", summary["error"])
        self.assertTrue(saved["restore"]["verified_started"])

    def test_restore_failure_is_explicit_and_status_is_still_checked(self):
        def fail_start(command, **kwargs):
            if command == "rc-service i14-cpufreq start":
                raise OSError("phone unreachable")
            if command.endswith(" status"):
                return subprocess.CompletedProcess(command, 3, " * status: stopped\n", "")
            return self.fake_runner(command, **kwargs)

        runner = Mock(side_effect=fail_start)
        with tempfile.TemporaryDirectory() as directory:
            summary = self.tool.run_experiment(self.args(directory), runner)
        self.assertFalse(summary["restore"]["verified_started"])
        self.assertIn("phone unreachable", summary["restore"]["start_error"])
        self.assertEqual(runner.call_args_list[-1].args[0], "rc-service i14-cpufreq status")

    def test_complete_sequence_writes_raw_and_summary_without_touching_phone(self):
        runner = Mock(side_effect=self.fake_runner)
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", io.StringIO()):
            args = self.args(directory)
            summary = self.tool.run_experiment(args, runner)
            self.assertEqual(len(list(args.output_dir.glob("0?-?.jsonl"))), 3)
        self.assertNotIn("error", summary)
        self.assertTrue(all(b["valid"] for b in summary["blocks"]))
        self.assertEqual(summary["contrast"]["saved_current_ma"]["mean"], 0)
        self.assertTrue(summary["restore"]["verified_started"])
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(commands.count("rc-service i14-cpufreq stop"), 1)

    def test_interrupted_sampler_preserves_partial_raw_and_invalidates_block(self):
        def interrupt(command, **kwargs):
            if kwargs.get("source") is not None and float(shlex.split(command)[4]) > 0:
                kwargs["output"].write(json.dumps(sample(0)) + "\n")
                raise InterruptedError("signal 15")
            return self.fake_runner(command, **kwargs)

        runner = Mock(side_effect=interrupt)
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", io.StringIO()):
            args = self.args(directory)
            summary = self.tool.run_experiment(args, runner)
            self.assertEqual(len((args.output_dir / "01-A.jsonl").read_text().splitlines()), 1)
        self.assertFalse(summary["blocks"][0]["valid"])
        self.assertEqual(summary["contrast"]["pairs"], [])
        self.assertTrue(summary["restore"]["verified_started"])
        self.assertIn("signal 15", summary["error"])


if __name__ == "__main__":
    unittest.main()
