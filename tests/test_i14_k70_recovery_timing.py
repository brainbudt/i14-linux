"""Offline tests for the host-only M0 recovery-timing analyzer.

All traces are synthetic fixtures generated here; no device, capture or
network access. The analyzer proves timing only, never M0 acceptance.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/i14-k70-recovery-timing.py"
SESSION = "host-session-A"
EXP_BOOT = "exp-boot-1"
PROD_BOOT = "prod-boot-2"
KERNEL = "3.18.19-stock"
IMAGE = "alpine-v10c-img"


def load_module():
    spec = importlib.util.spec_from_file_location("i14_k70_recovery_timing_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def records(crash_t=1000.0, return_t=1032.5):
    return [
        {"type": "contract", "schema": "i14-k70-recovery-trace/1",
         "host_clock_session": SESSION, "experimental_boot_id": EXP_BOOT,
         "expected_kernel": KERNEL, "expected_image": IMAGE},
        {"type": "crash_trigger", "clock": "host_monotonic", "session": SESSION,
         "boot_id": EXP_BOOT, "t_s": crash_t},
        {"type": "production_return", "clock": "host_monotonic", "session": SESSION,
         "boot_id": PROD_BOOT, "kernel": KERNEL, "image": IMAGE,
         "t_s": return_t, "confirmed": True},
    ]


def stage(name, phase, t, boot_id=PROD_BOOT, clock="boot_uptime"):
    return {"type": "stage", "stage": name, "phase": phase, "t_s": t,
            "boot_id": boot_id, "clock": clock}


def valid_stages():
    return [stage("early_init", "start", 0.2), stage("early_init", "end", 1.1),
            stage("sysinit_services", "start", 1.1), stage("sysinit_services", "end", 6.5),
            stage("mdev_coldplug", "start", 6.5), stage("mdev_coldplug", "end", 9.25)]


class TraceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = load_module()

    def write(self, trace):
        path = self.root / "trace.jsonl"
        if isinstance(trace, str):
            path.write_text(trace, encoding="utf-8")
        else:
            path.write_text("".join(json.dumps(record) + "\n" for record in trace),
                            encoding="utf-8")
        return path

    def run_main(self, trace):
        path = self.write(trace)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.module.main([str(path)])
        text = stdout.getvalue()
        self.assertEqual(len(text.splitlines()), 1)
        return code, json.loads(text)

    def run_cli(self, args):
        return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *args],
                              capture_output=True, text=True, timeout=10)

    def reasons(self, output):
        return [item["reason"] for item in output["problems"]]

    def stage_reasons(self, output):
        return [item["reason"] for item in output["stage_issues"]]


class TimingTests(TraceCase):
    def test_normal_timing_is_within_limit_and_reports_the_transition(self):
        code, output = self.run_main(records() + valid_stages())
        self.assertEqual(code, 0)
        self.assertEqual(output["verdict"], "WITHIN_LIMIT")
        self.assertEqual(output["exit_code"], 0)
        self.assertEqual(output["limit_s"], 40.0)
        recovery = output["recovery"]
        self.assertEqual(recovery["duration_s"], 32.5)
        self.assertEqual(recovery["crash_trigger_s"], 1000.0)
        self.assertEqual(recovery["production_return_s"], 1032.5)
        self.assertEqual(recovery["clock"], "host_monotonic")
        self.assertEqual(recovery["host_clock_session"], SESSION)
        self.assertEqual(recovery["from_boot_id"], EXP_BOOT)
        self.assertEqual(recovery["to_boot_id"], PROD_BOOT)
        self.assertTrue(recovery["kernel_confirmed"])
        self.assertTrue(recovery["image_confirmed"])
        self.assertEqual(recovery["kernel"], KERNEL)
        self.assertEqual(recovery["image"], IMAGE)
        self.assertEqual(output["problems"], [])

    def test_exactly_40_seconds_is_within_limit_inclusive(self):
        code, output = self.run_main(records(1000.0, 1040.0))
        self.assertEqual(code, 0)
        self.assertEqual(output["verdict"], "WITHIN_LIMIT")
        self.assertEqual(output["recovery"]["duration_s"], 40.0)

    def test_just_over_40_seconds_is_over_limit(self):
        code, output = self.run_main(records(1000.0, 1040.001))
        self.assertEqual(code, 1)
        self.assertEqual(output["verdict"], "OVER_LIMIT")
        self.assertEqual(output["recovery"]["duration_s"], 40.001)

    def test_historical_70_6_seconds_is_over_limit(self):
        code, output = self.run_main(records(1000.0, 1070.6))
        self.assertEqual(code, 1)
        self.assertEqual(output["verdict"], "OVER_LIMIT")
        self.assertEqual(output["exit_code"], 1)
        self.assertEqual(output["recovery"]["duration_s"], 70.6)

    def test_within_limit_claims_timing_only_not_m0_acceptance(self):
        _, output = self.run_main(records())
        note = output["scope_note"]
        self.assertIn("Timing evidence only", note)
        self.assertIn("Does not establish M0 acceptance", note)
        self.assertIn("pstore", note)
        self.assertIn("restoration K", note)
        self.assertIn("or a cause", note)
        self.assertNotIn("accepted", output["verdict"])

    def test_stage_uptimes_never_change_the_recovery_duration(self):
        _, plain = self.run_main(records())
        late = [stage("mdev_coldplug", "start", 500.0), stage("mdev_coldplug", "end", 900.0)]
        _, staged = self.run_main(records() + late)
        self.assertEqual(plain["recovery"]["duration_s"], staged["recovery"]["duration_s"])
        self.assertEqual(staged["recovery"]["duration_s"], 32.5)

    def test_clock_domains_stay_separate_in_the_output(self):
        _, output = self.run_main(records() + valid_stages())
        recovery = output["recovery"]
        self.assertEqual(recovery["duration_s"],
                         recovery["production_return_s"] - recovery["crash_trigger_s"])
        self.assertEqual(output["stage_clock"], "boot_uptime")
        self.assertEqual(output["stage_boot_id"], PROD_BOOT)
        for item in output["stages"]:
            self.assertEqual(item["clock"], "boot_uptime")
            self.assertEqual(item["boot_id"], PROD_BOOT)
            self.assertEqual(item["duration_s"], round(item["end_uptime_s"] - item["start_uptime_s"], 6))
            self.assertNotIn("host_clock_session", item)

    def test_unconfirmed_return_is_never_an_anchor(self):
        for flag in (False, None):
            with self.subTest(flag=flag):
                trace = records()
                if flag is None:
                    del trace[2]["confirmed"]
                else:
                    trace[2]["confirmed"] = flag
                code, output = self.run_main(trace)
                self.assertEqual(code, 2)
                self.assertEqual(output["verdict"], "INCOMPLETE")
                self.assertIn("missing_return_anchor", self.reasons(output))
                self.assertIsNone(output["recovery"])
                self.assertEqual(output["diagnostics"]["unconfirmed_returns"], 1)

    def test_stage_completion_without_a_confirmed_return_stays_incomplete(self):
        trace = records()
        trace.pop()
        code, output = self.run_main(trace + valid_stages())
        self.assertEqual(code, 2)
        self.assertEqual(output["verdict"], "INCOMPLETE")
        self.assertIsNone(output["recovery"])
        self.assertEqual(output["stages"], [])
        self.assertEqual(output["stage_evidence"], "unevaluated")

    def test_wall_clock_fields_are_ignored_not_used_as_anchors(self):
        trace = records()
        trace[1]["utc"] = "2026-11-17T12:00:00Z"
        trace[2]["utc"] = "2026-11-17T12:00:32Z"
        code, output = self.run_main(trace)
        self.assertEqual(code, 0)
        self.assertEqual(output["recovery"]["duration_s"], 32.5)
        self.assertNotIn("2026-11-17", json.dumps(output))


class EvidenceTests(TraceCase):
    def test_empty_trace_is_incomplete(self):
        code, output = self.run_main([])
        self.assertEqual(code, 2)
        self.assertEqual(output["verdict"], "INCOMPLETE")
        self.assertEqual(set(self.reasons(output)),
                         {"missing_contract", "missing_crash_trigger", "missing_return_anchor"})

    def test_missing_contract_or_crash_trigger_is_incomplete(self):
        for index, reason in ((0, "missing_contract"), (1, "missing_crash_trigger")):
            with self.subTest(reason=reason):
                trace = records()
                trace.pop(index)
                code, output = self.run_main(trace)
                self.assertEqual(code, 2)
                self.assertEqual(output["verdict"], "INCOMPLETE")
                self.assertIn(reason, self.reasons(output))

    def test_duplicate_required_records_are_inconsistent(self):
        for index, reason in ((0, "duplicate_contract"), (1, "duplicate_crash_trigger"),
                              (2, "ambiguous_return_anchor")):
            with self.subTest(reason=reason):
                trace = records()
                trace.append(dict(trace[index]))
                code, output = self.run_main(trace)
                self.assertEqual(code, 2)
                self.assertEqual(output["verdict"], "INCONSISTENT")
                self.assertIn(reason, self.reasons(output))
                self.assertIsNone(output["recovery"])

    def test_mixed_host_clock_sessions_are_inconsistent(self):
        for index, reason in ((1, "crash_session_mismatch"), (2, "return_session_mismatch")):
            with self.subTest(reason=reason):
                trace = records()
                trace[index]["session"] = "host-session-B"
                code, output = self.run_main(trace)
                self.assertEqual(code, 2)
                self.assertEqual(output["verdict"], "INCONSISTENT")
                self.assertIn(reason, self.reasons(output))

    def test_non_monotonic_anchor_clocks_are_rejected(self):
        for clock in ("wall", "boot_uptime", "utc"):
            for index, reason in ((1, "crash_clock_domain"), (2, "return_clock_domain")):
                with self.subTest(clock=clock, index=index):
                    trace = records()
                    trace[index]["clock"] = clock
                    code, output = self.run_main(trace)
                    self.assertEqual(code, 2)
                    self.assertEqual(output["verdict"], "INCONSISTENT")
                    self.assertIn(reason, self.reasons(output))

    def test_return_must_be_a_different_boot_identity(self):
        trace = records()
        trace[2]["boot_id"] = EXP_BOOT
        code, output = self.run_main(trace)
        self.assertEqual(code, 2)
        self.assertEqual(output["verdict"], "INCONSISTENT")
        self.assertIn("no_new_production_boot", self.reasons(output))

    def test_crash_must_be_on_the_contract_experimental_boot(self):
        trace = records()
        trace[1]["boot_id"] = "exp-boot-other"
        code, output = self.run_main(trace)
        self.assertEqual(code, 2)
        self.assertIn("crash_boot_mismatch", self.reasons(output))

    def test_kernel_and_image_must_match_the_contract(self):
        for key, reason in (("kernel", "kernel_mismatch"), ("image", "image_mismatch")):
            with self.subTest(key=key):
                trace = records()
                trace[2][key] = "different-identity"
                code, output = self.run_main(trace)
                self.assertEqual(code, 2)
                self.assertEqual(output["verdict"], "INCONSISTENT")
                self.assertIn(reason, self.reasons(output))

    def test_missing_identity_fields_are_malformed_not_inferred(self):
        for key in ("kernel", "image", "boot_id", "session"):
            with self.subTest(key=key):
                trace = records()
                del trace[2][key]
                code, output = self.run_main(trace)
                self.assertEqual(code, 2)
                self.assertIn("malformed_return", self.reasons(output))

    def test_return_must_be_strictly_after_the_crash_trigger(self):
        for return_t in (999.0, 1000.0):
            with self.subTest(return_t=return_t):
                code, output = self.run_main(records(1000.0, return_t))
                self.assertEqual(code, 2)
                self.assertEqual(output["verdict"], "INCONSISTENT")
                self.assertIn("return_not_after_crash", self.reasons(output))
                self.assertIsNone(output["recovery"])

    def test_malformed_anchor_timestamps_are_rejected_without_traceback(self):
        for value in ("1032.5", -1.0, True, None, [], {}):
            for index in (1, 2):
                with self.subTest(value=value, index=index):
                    trace = records()
                    trace[index]["t_s"] = value
                    code, output = self.run_main(trace)
                    self.assertEqual(code, 2)
                    self.assertEqual(output["verdict"], "INCONSISTENT")
                    self.assertIsNone(output["recovery"])

    def test_nonfinite_and_overflow_timestamps_are_rejected(self):
        for raw in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(raw=raw):
                trace = records()
                text = "".join(json.dumps(record) + "\n" for record in trace)
                text = text.replace('"t_s": 1032.5', '"t_s": ' + raw)
                code, output = self.run_main(text)
                self.assertEqual(code, 2)
                self.assertEqual(output["verdict"], "INCONSISTENT")

    def test_malformed_contract_is_inconsistent(self):
        for key, value in (("schema", "other/2"), ("host_clock_session", ""),
                           ("expected_kernel", 3), ("experimental_boot_id", None)):
            with self.subTest(key=key):
                trace = records()
                trace[0][key] = value
                code, output = self.run_main(trace)
                self.assertEqual(code, 2)
                self.assertIn("malformed_contract", self.reasons(output))

    def test_non_bool_confirmed_flag_is_malformed(self):
        trace = records()
        trace[2]["confirmed"] = "yes"
        code, output = self.run_main(trace)
        self.assertEqual(code, 2)
        self.assertIn("malformed_return", self.reasons(output))

    def test_structural_trace_errors_fail_closed(self):
        good = "".join(json.dumps(record) + "\n" for record in records())
        fixtures = [
            ("malformed_json", good + "not json\n"),
            ("blank_line", good + "\n"),
            ("record_not_object", good + "[1, 2]\n"),
            ("unknown_record_type", good + '{"type": "note"}\n'),
            ("unknown_record_type", good + '{"t_s": 1}\n'),
            ("malformed_json", good + '{"type": "stage", "stage": "early_init", "stage": "x"}\n'),
        ]
        for reason, text in fixtures:
            with self.subTest(reason=reason):
                code, output = self.run_main(text)
                self.assertEqual(code, 2)
                self.assertEqual(output["verdict"], "INCONSISTENT")
                self.assertIn(reason, self.reasons(output))
                self.assertIsNone(output["recovery"])

    def test_missing_and_malformed_evidence_together_is_inconsistent(self):
        trace = records()
        trace.pop(1)
        trace[1]["t_s"] = "bad"
        code, output = self.run_main(trace)
        self.assertEqual(code, 2)
        self.assertEqual(output["verdict"], "INCONSISTENT")

    def test_record_limits_are_enforced(self):
        with mock.patch.object(self.module, "MAX_RECORDS", 2):
            code, output = self.run_main(records())
        self.assertEqual(code, 2)
        self.assertIn("too_many_records", self.reasons(output))
        with mock.patch.object(self.module, "MAX_TRACE_BYTES", 16):
            code, output = self.run_main(records())
        self.assertEqual(code, 2)
        self.assertIn("trace_too_large", self.reasons(output))


class StageTests(TraceCase):
    def test_valid_paired_stages_are_reported_in_start_order(self):
        shuffled = valid_stages()
        shuffled = [shuffled[i] for i in (4, 0, 5, 2, 1, 3)]
        code, output = self.run_main(records() + shuffled)
        self.assertEqual(code, 0)
        self.assertEqual(output["stage_evidence"], "valid")
        self.assertEqual(output["stage_issues"], [])
        self.assertEqual([item["stage"] for item in output["stages"]],
                         ["early_init", "sysinit_services", "mdev_coldplug"])
        self.assertEqual([item["duration_s"] for item in output["stages"]], [0.9, 5.4, 2.75])
        self.assertEqual(output["stages"][0]["start_uptime_s"], 0.2)
        self.assertEqual(output["stages"][0]["end_uptime_s"], 1.1)

    def test_absent_stages_are_allowed(self):
        code, output = self.run_main(records())
        self.assertEqual(code, 0)
        self.assertEqual(output["stage_evidence"], "absent")
        self.assertEqual(output["stages"], [])
        self.assertEqual(output["stage_issues"], [])

    def test_mixed_boot_stage_is_called_out_and_excluded(self):
        trace = records() + valid_stages()
        trace.append(stage("early_init", "start", 0.1, boot_id="prod-boot-other"))
        trace.append(stage("early_init", "end", 0.4, boot_id="prod-boot-other"))
        code, output = self.run_main(trace)
        self.assertEqual(code, 0)
        self.assertEqual(output["verdict"], "WITHIN_LIMIT")
        self.assertEqual(output["stage_evidence"], "issues")
        self.assertEqual(self.stage_reasons(output).count("stage_mixed_boot"), 2)
        self.assertEqual([item["stage"] for item in output["stages"]],
                         ["early_init", "sysinit_services", "mdev_coldplug"])
        self.assertEqual(output["stages"][0]["duration_s"], 0.9)
        for item in output["stages"]:
            self.assertEqual(item["boot_id"], PROD_BOOT)

    def test_duplicate_stage_endpoints_are_called_out_separately(self):
        trace = records() + valid_stages()
        trace.append(stage("early_init", "start", 0.3))
        trace.append(stage("sysinit_services", "end", 6.6))
        code, output = self.run_main(trace)
        self.assertEqual(code, 0)
        self.assertIn("duplicate_stage_start", self.stage_reasons(output))
        self.assertIn("duplicate_stage_end", self.stage_reasons(output))
        self.assertEqual(output["stages"][0]["start_uptime_s"], 0.2)
        self.assertEqual(output["stages"][1]["end_uptime_s"], 6.5)

    def test_unpaired_stages_are_called_out_and_never_measured(self):
        trace = records() + [stage("early_init", "start", 0.2),
                             stage("mdev_coldplug", "end", 9.25)]
        code, output = self.run_main(trace)
        self.assertEqual(code, 0)
        self.assertEqual(self.stage_reasons(output).count("unpaired_stage"), 2)
        self.assertEqual(output["stages"], [])
        self.assertEqual(output["stage_evidence"], "issues")

    def test_reversed_stage_pair_is_called_out_and_excluded(self):
        trace = records() + [stage("early_init", "start", 1.1),
                             stage("early_init", "end", 0.2)]
        code, output = self.run_main(trace)
        self.assertEqual(code, 0)
        self.assertIn("reversed_stage", self.stage_reasons(output))
        self.assertEqual(output["stages"], [])

    def test_stage_on_wrong_clock_domain_is_called_out(self):
        trace = records() + [stage("early_init", "start", 0.2, clock="host_monotonic"),
                             stage("early_init", "end", 1.1, clock="host_monotonic")]
        code, output = self.run_main(trace)
        self.assertEqual(code, 0)
        self.assertEqual(self.stage_reasons(output).count("stage_wrong_clock"), 2)
        self.assertEqual(output["stages"], [])

    def test_unknown_and_malformed_stages_are_called_out(self):
        trace = records() + [
            stage("late_cleanup", "start", 10.0),
            {"type": "stage", "stage": "early_init", "phase": "begin", "t_s": 0.2,
             "boot_id": PROD_BOOT, "clock": "boot_uptime"},
            {"type": "stage", "stage": "sysinit_services", "phase": "start", "t_s": "1.1",
             "boot_id": PROD_BOOT, "clock": "boot_uptime"},
            {"type": "stage", "phase": "end", "t_s": 6.5,
             "boot_id": PROD_BOOT, "clock": "boot_uptime"},
        ]
        code, output = self.run_main(trace)
        self.assertEqual(code, 0)
        reasons = self.stage_reasons(output)
        self.assertIn("unknown_stage", reasons)
        self.assertEqual(reasons.count("malformed_stage"), 3)
        self.assertEqual(output["stages"], [])

    def test_stage_issues_do_not_change_the_timing_verdict(self):
        trace = records(1000.0, 1070.6) + [stage("early_init", "start", 0.2)]
        code, output = self.run_main(trace)
        self.assertEqual(code, 1)
        self.assertEqual(output["verdict"], "OVER_LIMIT")
        self.assertEqual(output["recovery"]["duration_s"], 70.6)
        self.assertEqual(output["stage_evidence"], "issues")
        self.assertIn("unpaired_stage", self.stage_reasons(output))

    def test_issue_lines_identify_the_offending_records(self):
        trace = records() + valid_stages()
        trace.append(stage("early_init", "start", 0.3))
        _, output = self.run_main(trace)
        issue = next(item for item in output["stage_issues"]
                     if item["reason"] == "duplicate_stage_start")
        self.assertEqual(issue["line"], len(trace))
        self.assertEqual(issue["stage"], "early_init")


class CommandLineTests(TraceCase):
    def test_cli_within_limit_is_one_json_line_with_empty_stderr(self):
        path = self.write(records() + valid_stages())
        run = self.run_cli([str(path)])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, "")
        self.assertEqual(len(run.stdout.splitlines()), 1)
        output = json.loads(run.stdout)
        self.assertEqual(output["verdict"], "WITHIN_LIMIT")
        self.assertEqual(output["recovery"]["duration_s"], 32.5)

    def test_cli_over_limit_exits_one(self):
        path = self.write(records(1000.0, 1070.6))
        run = self.run_cli([str(path)])
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertEqual(run.stderr, "")
        self.assertEqual(json.loads(run.stdout)["verdict"], "OVER_LIMIT")

    def test_cli_inconsistent_evidence_exits_two_without_traceback(self):
        trace = records()
        trace[2]["session"] = "host-session-B"
        path = self.write(trace)
        run = self.run_cli([str(path)])
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stderr, "")
        self.assertNotIn("Traceback", run.stdout)
        output = json.loads(run.stdout)
        self.assertEqual(output["verdict"], "INCONSISTENT")
        self.assertIn("return_session_mismatch", self.reasons(output))

    def test_cli_incomplete_evidence_exits_two(self):
        trace = records()
        trace.pop()
        path = self.write(trace)
        run = self.run_cli([str(path)])
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stderr, "")
        self.assertEqual(json.loads(run.stdout)["verdict"], "INCOMPLETE")

    def test_cli_usage_errors_exit_two_without_traceback(self):
        for args in ([], [str(self.root / "a.jsonl"), "extra"]):
            with self.subTest(args=args):
                run = self.run_cli(args)
                self.assertEqual(run.returncode, 2)
                self.assertEqual(run.stderr, "")
                output = json.loads(run.stdout)
                self.assertEqual(output["verdict"], "INCONSISTENT")
                self.assertIn("usage_error", self.reasons(output))

    def test_cli_unreadable_trace_exits_two(self):
        for path in (self.root / "missing.jsonl", self.root):
            with self.subTest(path=path):
                run = self.run_cli([str(path)])
                self.assertEqual(run.returncode, 2)
                self.assertEqual(run.stderr, "")
                output = json.loads(run.stdout)
                self.assertIn("trace_unreadable", self.reasons(output))

    def test_cli_non_utf8_trace_exits_two(self):
        path = self.root / "trace.jsonl"
        path.write_bytes(b"\xff\xfe\x00")
        run = self.run_cli([str(path)])
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stderr, "")
        self.assertIn("invalid_encoding", self.reasons(json.loads(run.stdout)))

    def test_cli_oversized_trace_exits_two(self):
        path = self.root / "trace.jsonl"
        path.write_bytes(b"x" * (1024 * 1024 + 1))
        run = self.run_cli([str(path)])
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stderr, "")
        self.assertIn("trace_too_large", self.reasons(json.loads(run.stdout)))

    def test_cli_output_is_deterministic(self):
        path = self.write(records() + valid_stages())
        first = self.run_cli([str(path)])
        second = self.run_cli([str(path)])
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.returncode, second.returncode)


if __name__ == "__main__":
    unittest.main()
