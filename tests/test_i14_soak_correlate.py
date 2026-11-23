"""scripts/i14-soak-correlate.py: DNS soak vs power correlation on synthetic data only."""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/i14-soak-correlate.py"


def load_script(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


correlate = load_script(SCRIPT)

HEAD = "time,health_udp,ms,health_tcp,ms,example_udp,ms,signed_ad,ms,bogus,ms\n"
GOOD = "NOERROR,3,NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300\n"
BAD_UDP = "SERVFAIL,5005,NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300\n"  # health_udp wrong
BAD_TWO = "NOERROR,3,NOERROR,14,NOERROR,60,NOERROR,5,NOERROR,300\n"         # signed_ad + bogus wrong
BASE = "2026-11-04T00:00:00Z"
OMIT = object()


def ep(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def row(t, body=GOOD):
    return f"{t},{body}"


def power(iso, boot="boot-1", usb=1, ac=0, uptime=None, **extra):
    e = ep(iso)
    rec = {"epoch": e, "boot_id": boot, "uptime_seconds": e - ep(BASE) if uptime is None else uptime}
    if usb is not OMIT:
        rec["usb_online"] = usb
    if ac is not OMIT:
        rec["ac_online"] = ac
    rec.update(extra)
    return json.dumps(rec)


class CorrelateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)

    def write(self, name, content):
        p = self.dir / name
        p.write_bytes(content.encode() if isinstance(content, str) else content)
        return p

    def dns_file(self, name, rows):
        return self.write(name, HEAD + "".join(rows))

    def argv(self, dns, power=(), maint=None, dns_gap=300, power_gap=180):
        a = ["--dns-max-gap-seconds", str(dns_gap), "--power-max-gap-seconds", str(power_gap)]
        for p in dns:
            a += ["--dns", str(p)]
        for p in power:
            a += ["--power", str(p)]
        if maint is not None:
            a += ["--maintenance", str(maint)]
        return a

    def cli(self, dns, power=(), maint=None, dns_gap=300, power_gap=180):
        return subprocess.run([sys.executable, "-B", str(SCRIPT)]
                              + self.argv(dns, power, maint, dns_gap, power_gap),
                              capture_output=True, text=True, timeout=120)

    def go(self, dns, power=(), maint=None, dns_gap=300, power_gap=180):
        rc, out, err = self.run_main(dns, power, maint, dns_gap, power_gap)
        return rc, json.loads(out) if out else None, out, err

    def run_main(self, dns, power=(), maint=None, dns_gap=300, power_gap=180):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = correlate.main(self.argv(dns, power, maint, dns_gap, power_gap))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 2
        return code, out.getvalue(), err.getvalue()

    def assert_invalid(self, r, needle=None):
        rc, rep, out, err = r
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")           # no stdout on invalid input
        self.assertIsNone(rep)
        self.assertEqual(err.count("\n"), 1)  # one physical line per error
        if needle:
            self.assertIn(needle, err)
        return err


class HappyPath(CorrelateCase):
    def test_clean_run_exit_0_with_full_schema(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:01:00Z")])
        p = self.write("p.jsonl", "\n".join(power(t) for t in
                                             ("2026-11-04T21:00:00Z", "2026-11-04T21:01:00Z")) + "\n")
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(rep["schema"], "i14-soak-correlate/1")
        self.assertEqual(rep["dns"]["effective_observations"], 2)
        self.assertEqual(rep["dns"]["first_utc"], "2026-11-04T21:00:00Z")
        self.assertEqual(rep["dns"]["last_utc"], "2026-11-04T21:01:00Z")
        self.assertEqual(rep["dns"]["passed"], 2)
        self.assertEqual(rep["dns"]["planned_failures"], 0)
        self.assertEqual(rep["dns"]["unplanned_failures"], 0)
        self.assertEqual(rep["maintenance"], {"provided": False, "windows": 0,
                                              "merged_windows": 0, "merged_seconds": 0})
        self.assertEqual(rep["coverage"]["status"], "complete")
        self.assertEqual(rep["coverage"]["gaps"], [])
        self.assertEqual(rep["coverage"]["single_observation"], False)
        self.assertEqual(rep["power"]["effective_samples"], 2)
        self.assertEqual(rep["power"]["qualified_intervals"], 1)
        self.assertEqual(rep["unplanned_failures"], [])
        self.assertEqual(rep["unplanned_by_power_state"], {"online": 0, "offline": 0, "unknown": 0})
        self.assertEqual(rep["exit_code"], 0)
        self.assertTrue(any("neither an SLA nor proof" in n for n in rep["notes"]))

    def test_unplanned_failure_exit_1_correlates_left_state_online(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:00:30Z", BAD_UDP),
                                        row("2026-11-04T21:01:00Z")])
        p = self.write("p.jsonl", "\n".join(power(t) for t in
                                             ("2026-11-04T21:00:00Z", "2026-11-04T21:00:30Z",
                                              "2026-11-04T21:01:00Z")) + "\n")
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rc, 1)
        self.assertEqual(rep["dns"]["passed"], 2)
        self.assertEqual(rep["unplanned_failures"][0]["time_utc"], "2026-11-04T21:00:30Z")
        self.assertEqual(rep["unplanned_failures"][0]["checks"], ["health_udp"])
        self.assertEqual(rep["unplanned_failures"][0]["external_power"], "online")
        self.assertNotIn("power_reason", rep["unplanned_failures"][0])
        self.assertEqual(rep["unplanned_by_power_state"], {"online": 1, "offline": 0, "unknown": 0})

    def test_all_failing_check_names_are_retained_in_order(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z", BAD_TWO), row("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([dns])
        self.assertEqual(rc, 1)
        self.assertEqual(rep["unplanned_failures"][0]["checks"], ["signed_ad", "bogus"])

    def test_planned_failure_inside_window_is_not_unplanned(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:00:30Z", BAD_UDP),
                                        row("2026-11-04T21:01:00Z")])
        m = self.write("m.txt", "# planned\n2026-11-04T21:00:00 2026-11-04T21:01:00 reboot test\n")
        rc, rep, out, err = self.go([dns], maint=m)
        self.assertEqual(rc, 0)
        self.assertEqual(rep["dns"]["planned_failures"], 1)
        self.assertEqual(rep["dns"]["unplanned_failures"], 0)
        self.assertEqual(rep["maintenance"]["windows"], 1)
        self.assertEqual(rep["maintenance"]["merged_windows"], 1)
        self.assertEqual(rep["maintenance"]["merged_seconds"], 60)

    def test_output_is_deterministic(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:00:30Z", BAD_UDP),
                                        row("2026-11-04T21:01:00Z")])
        p = self.write("p.jsonl", "\n".join(power(t, usb=0, ac=0) for t in
                                             ("2026-11-04T21:00:00Z", "2026-11-04T21:00:30Z",
                                              "2026-11-04T21:01:00Z")) + "\n")
        m = self.write("m.txt", "2026-11-04T20:00:00Z 2026-11-04T20:30:00Z x\n")
        a = self.cli([dns], [p], m)
        b = self.cli([dns], [p], m)
        self.assertEqual(a.stdout, b.stdout)
        self.assertEqual(a.returncode, b.returncode)


class Coverage(CorrelateCase):
    def test_gap_strictly_greater_than_limit_emits_json_and_exit_2(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:05:00Z", BAD_UDP)])
        rc, rep, out, err = self.go([dns], dns_gap=299)
        self.assertEqual(rc, 2)
        self.assertIsNotNone(rep)  # valid incomplete report still emits JSON
        self.assertEqual(rep["coverage"]["status"], "incomplete")
        self.assertEqual(rep["coverage"]["gaps"], [
            {"start_utc": "2026-11-04T21:00:00Z", "end_utc": "2026-11-04T21:05:00Z",
             "duration_seconds": 300}])
        self.assertEqual(rep["dns"]["passed"], 1)
        self.assertEqual(rep["unplanned_failures"][0]["checks"], ["health_udp"])  # failures still included
        self.assertEqual(err, "")

    def test_gap_equal_to_the_limit_passes(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:05:00Z")])
        rc, rep, out, err = self.go([dns], dns_gap=300)
        self.assertEqual(rc, 0)
        self.assertEqual(rep["coverage"]["status"], "complete")
        self.assertEqual(rep["coverage"]["gaps"], [])

    def test_a_gap_never_counts_as_a_dns_failure(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:10:00Z")])
        rc, rep, out, err = self.go([dns], dns_gap=300)
        self.assertEqual(rc, 2)
        self.assertEqual(rep["dns"]["passed"], 2)
        self.assertEqual(rep["dns"]["unplanned_failures"], 0)
        self.assertEqual(len(rep["coverage"]["gaps"]), 1)

    def test_single_observation_is_incomplete_coverage(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z")])
        rc, rep, out, err = self.go([dns])
        self.assertEqual(rc, 2)
        self.assertIsNotNone(rep)
        self.assertEqual(rep["coverage"]["status"], "incomplete")
        self.assertEqual(rep["coverage"]["single_observation"], True)
        self.assertEqual(rep["coverage"]["gaps"], [])
        self.assertEqual(rep["dns"]["effective_observations"], 1)

    def test_maintenance_is_subtracted_from_the_open_interval(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:10:00Z")])
        m = self.write("m.txt", "2026-11-04T21:01:40 2026-11-04T21:08:20 maintenance\n")
        rc, rep, out, err = self.go([dns], maint=m, dns_gap=150)
        self.assertEqual(rc, 0)
        self.assertEqual(rep["coverage"]["gaps"], [])  # pieces are 100 s each
        rc, rep, out, err = self.go([dns], maint=m, dns_gap=99.999)
        self.assertEqual(rc, 2)
        self.assertEqual(rep["coverage"]["gaps"], [
            {"start_utc": "2026-11-04T21:00:00Z", "end_utc": "2026-11-04T21:01:40Z",
             "duration_seconds": 100},
            {"start_utc": "2026-11-04T21:08:20Z", "end_utc": "2026-11-04T21:10:00Z",
             "duration_seconds": 100}])

    def test_window_endpoints_are_inclusive_for_observation_membership(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z", BAD_UDP),
                                        row("2026-11-04T21:00:30Z", BAD_UDP),
                                        row("2026-11-04T21:01:00Z", BAD_UDP),
                                        row("2026-11-04T21:02:00Z")])
        m = self.write("m.txt", "2026-11-04T21:00:30Z 2026-11-04T21:01:00Z window\n")
        rc, rep, out, err = self.go([dns], maint=m)
        self.assertEqual(rc, 1)
        self.assertEqual(rep["dns"]["planned_failures"], 2)   # both endpoints inclusive
        self.assertEqual(rep["dns"]["unplanned_failures"], 1)  # 21:00:00 is outside

    def test_unordered_overlapping_zoned_windows_are_merged(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T22:00:00Z", BAD_UDP),
                                        row("2026-11-04T23:00:00Z")])
        m = self.write("m.txt", "# comment\n\n"
                                "2026-11-04T23:00:00+01:00 2026-11-04T23:30:00+01:00 later\n"
                                "2026-11-04T21:00:00Z 2026-11-04T22:10:00Z earlier\n")
        rc, rep, out, err = self.go([dns], maint=m, dns_gap=3600)
        self.assertEqual(rc, 0)  # the bad 22:00Z sample is planned, coverage complete (equality passes)
        self.assertEqual(rep["maintenance"]["windows"], 2)
        self.assertEqual(rep["maintenance"]["merged_windows"], 1)
        self.assertEqual(rep["maintenance"]["merged_seconds"], 5400)  # 21:00Z..22:30Z
        self.assertEqual(rep["dns"]["planned_failures"], 1)

    def test_gaps_apply_only_between_first_and_last_observation(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:01:00Z"),
                                        row("2026-11-04T21:02:00Z")])
        m = self.write("m.txt", "2026-11-04T23:00:00Z 2026-11-04T23:30:00Z far away\n")
        rc, rep, out, err = self.go([dns], maint=m, dns_gap=300)
        self.assertEqual(rc, 0)
        self.assertEqual(rep["coverage"]["gaps"], [])

    def test_fractional_durations(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"),
                                        row("2026-11-04T21:00:00.500000Z")])
        rc, rep, out, err = self.go([dns], dns_gap=0.5)
        self.assertEqual(rc, 0)  # equality passes
        rc, rep, out, err = self.go([dns], dns_gap=0.4)
        self.assertEqual(rc, 2)
        self.assertEqual(rep["coverage"]["gaps"][0]["duration_seconds"], 0.5)


class Ordering(CorrelateCase):
    def test_identical_records_at_one_instant_collapse_across_zones_and_files(self):
        f1 = self.dns_file("dns.csv.1", [row("2026-11-04T21:00:00Z"), row("2026-11-04T22:00:00+01:00")])
        f2 = self.dns_file("dns.csv", [row("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([f1, f2])
        self.assertEqual(rc, 0)
        self.assertEqual(rep["dns"]["effective_observations"], 2)  # 21:00Z twice collapsed

    def test_conflicting_records_at_one_instant_fail_with_file_and_line(self):
        f1 = self.dns_file("dns.csv.1", [row("2026-11-04T21:00:00Z")])
        f2 = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z", GOOD.replace("NOERROR,3,", "NOERROR,4,"))])
        err = self.assert_invalid(self.go([f1, f2]), "conflicting DNS records")
        self.assertIn(f"{f2}:2:", err)
        self.assertNotIn("NOERROR", err)

    def test_backwards_timestamp_fails_even_across_rotation_files(self):
        f1 = self.dns_file("dns.csv.1", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:01:00Z")])
        f2 = self.dns_file("dns.csv", [row("2026-11-04T21:00:30Z")])
        err = self.assert_invalid(self.go([f1, f2]), "earlier than the previous observation")
        self.assertIn(f"{f2}:2:", err)

    def test_rotation_files_form_one_stream(self):
        f1 = self.dns_file("dns.csv.1", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:01:00Z")])
        f2 = self.dns_file("dns.csv", [row("2026-11-04T21:02:00Z"), row("2026-11-04T21:03:00Z", BAD_UDP)])
        rc, rep, out, err = self.go([f1, f2])
        self.assertEqual(rc, 1)
        self.assertEqual(rep["dns"]["effective_observations"], 4)
        self.assertEqual(rep["dns"]["first_utc"], "2026-11-04T21:00:00Z")
        self.assertEqual(rep["dns"]["last_utc"], "2026-11-04T21:03:00Z")
        self.assertEqual(rep["dns"]["files"], 2)

    def test_every_dns_file_needs_the_recorder_header(self):
        f1 = self.dns_file("dns.csv.1", [row("2026-11-04T21:00:00Z")])
        f2 = self.write("dns.csv", row("2026-11-04T21:01:00Z"))
        self.assert_invalid(self.go([f1, f2]), "unsupported header")

    def test_mixed_timezone_order_is_checked_after_utc_normalization(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T22:00:00+01:00"), row("2026-11-04T21:00:30Z")])
        rc, rep, out, err = self.go([dns])
        self.assertEqual(rc, 0)  # 21:00:00Z then 21:00:30Z
        dns = self.dns_file("dns2.csv", [row("2026-11-04T21:00:30Z"), row("2026-11-04T22:00:00+01:00")])
        self.assert_invalid(self.go([dns]), "earlier than the previous observation")


class PowerCorrelation(CorrelateCase):
    def dns_with_failure_at(self, *times):
        rows = [row("2026-11-04T21:00:00Z")]
        for t in times:
            rows.append(row(t, BAD_UDP))
        rows.append(row("2026-11-04T21:02:00Z"))
        return self.dns_file("dns.csv", rows)

    def power_file(self, specs):
        return self.write("p.jsonl", "\n".join(json.dumps(s) if isinstance(s, dict) else s
                                                for s in specs) + "\n")

    def test_offline_and_unknown_left_states(self):
        dns = self.dns_with_failure_at("2026-11-04T21:00:15Z", "2026-11-04T21:00:45Z")
        p = self.power_file([power("2026-11-04T21:00:00Z", usb=0, ac=0),
                             power("2026-11-04T21:00:30Z", usb=OMIT, ac=None),
                             power("2026-11-04T21:01:00Z", usb=1, ac=0)])
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rc, 1)
        online, unknown = rep["unplanned_failures"]
        self.assertEqual(online["external_power"], "offline")
        self.assertNotIn("power_reason", online)
        self.assertEqual(unknown["external_power"], "unknown")
        self.assertEqual(unknown["power_reason"], "left_state_unknown")
        self.assertEqual(rep["unplanned_by_power_state"], {"online": 0, "offline": 1, "unknown": 1})

    def test_intervals_are_half_open_left_state_wins(self):
        dns = self.dns_with_failure_at("2026-11-04T21:00:15Z", "2026-11-04T21:00:30Z")
        p = self.power_file([power("2026-11-04T21:00:00Z", usb=1, ac=0),
                             power("2026-11-04T21:00:30Z", usb=0, ac=0),
                             power("2026-11-04T21:01:00Z", usb=1, ac=0)])
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rc, 1)
        first, second = rep["unplanned_failures"]
        self.assertEqual(first["external_power"], "online")    # [21:00:00, 21:00:30)
        self.assertEqual(second["external_power"], "offline")  # at 21:00:30 the next interval starts

    def test_last_power_sample_has_no_interval(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:00:30Z"),
                                        row("2026-11-04T21:01:00Z", BAD_UDP)])
        p = self.power_file([power("2026-11-04T21:00:00Z"), power("2026-11-04T21:00:30Z"),
                             power("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rc, 1)
        self.assertEqual(rep["unplanned_failures"][0]["external_power"], "unknown")
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "at_or_after_last_power_sample")

    def test_before_first_power_sample(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z", BAD_UDP), row("2026-11-04T21:01:00Z")])
        p = self.power_file([power("2026-11-04T21:00:30Z"), power("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rc, 1)
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "before_first_power_sample")

    def test_no_power_input_and_empty_power_file_are_allowed(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z", BAD_UDP), row("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([dns])
        self.assertEqual(rc, 1)
        self.assertEqual(rep["power"]["effective_samples"], 0)
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "no_power_samples")
        empty = self.write("empty.jsonl", "")
        rc, rep, out, err = self.go([dns], [empty])
        self.assertEqual(rc, 1)
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "no_power_samples")

    def test_reboot_between_samples_disqualifies_the_interval(self):
        dns = self.dns_with_failure_at("2026-11-04T21:00:15Z")
        p = self.power_file([power("2026-11-04T21:00:00Z", boot="boot-1"),
                             power("2026-11-04T21:00:30Z", boot="boot-2"),
                             power("2026-11-04T21:01:00Z", boot="boot-2")])
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rc, 1)  # the DNS failure still counts; only the power state is unknown
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "different_boot")
        self.assertEqual(rep["power"]["qualified_intervals"], 1)

    def test_wall_gap_boundary_against_power_max_gap(self):
        dns = self.dns_with_failure_at("2026-11-04T21:01:00Z")
        p = self.power_file([power("2026-11-04T21:00:00Z"), power("2026-11-04T21:05:00Z")])
        rc, rep, out, err = self.go([dns], [p], power_gap=180)
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "wall_gap_exceeds_power_max_gap")
        rc, rep, out, err = self.go([dns], [p], power_gap=300)  # equality qualifies
        self.assertEqual(rep["unplanned_failures"][0]["external_power"], "online")

    def test_uptime_reset_and_uptime_gap(self):
        dns = self.dns_with_failure_at("2026-11-04T21:00:15Z")
        p = self.power_file([power("2026-11-04T21:00:00Z", uptime=100.0),
                             power("2026-11-04T21:00:30Z", uptime=50.0),
                             power("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "uptime_not_increasing")
        p = self.power_file([power("2026-11-04T21:00:00Z", uptime=0.0),
                             power("2026-11-04T21:00:30Z", uptime=300.0),
                             power("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([dns], [p], power_gap=180)
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "uptime_gap_exceeds_power_max_gap")

    def test_wall_uptime_disagreement_boundary(self):
        dns = self.dns_with_failure_at("2026-11-04T21:00:15Z")
        p = self.power_file([power("2026-11-04T21:00:00Z", uptime=0.0),
                             power("2026-11-04T21:00:30Z", uptime=32.0),
                             power("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rep["unplanned_failures"][0]["external_power"], "online")  # |30-32| <= 2
        p = self.power_file([power("2026-11-04T21:00:00Z", uptime=0.0),
                             power("2026-11-04T21:00:30Z", uptime=32.5),
                             power("2026-11-04T21:01:00Z")])
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rep["unplanned_failures"][0]["power_reason"], "wall_uptime_disagreement")

    def test_missing_power_never_changes_a_dns_outcome(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:01:00Z")])
        p = self.power_file([power("2026-11-04T21:00:00Z", boot="a"),
                             power("2026-11-04T21:00:30Z", boot="b")])  # reboot, no qualified interval
        rc, rep, out, err = self.go([dns], [p])
        self.assertEqual(rc, 0)  # all DNS passed: power gaps cannot fail the run


class PowerStream(CorrelateCase):
    def dns(self):
        return self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:01:00Z")])

    def test_null_and_omitted_online_fields_collapse_at_one_epoch(self):
        a = {"epoch": ep("2026-11-04T21:00:00Z"), "boot_id": "b", "uptime_seconds": 10.0,
             "usb_online": None}
        b = {"epoch": ep("2026-11-04T21:00:00Z"), "boot_id": "b", "uptime_seconds": 10.0}
        p1 = self.write("p.jsonl.1", json.dumps(a) + "\n")
        p2 = self.write("p.jsonl", json.dumps(b) + "\n"
                        + power("2026-11-04T21:01:00Z") + "\n")
        rc, rep, out, err = self.go([self.dns()], [p1, p2])
        self.assertEqual(rc, 0)
        self.assertEqual(rep["power"]["effective_samples"], 2)  # one collapsed across rotation files

    def test_conflicting_power_records_at_one_epoch_fail(self):
        a = {"epoch": ep("2026-11-04T21:00:00Z"), "boot_id": "b", "uptime_seconds": 10.0, "usb_online": 1}
        b = {"epoch": ep("2026-11-04T21:00:00Z"), "boot_id": "b", "uptime_seconds": 10.0, "usb_online": 0}
        p = self.write("p.jsonl", json.dumps(a) + "\n" + json.dumps(b) + "\n")
        self.assert_invalid(self.go([self.dns()], [p]), "conflicting power records")

    def test_backwards_epoch_across_rotation_files_fails(self):
        p1 = self.write("p.jsonl.1", power("2026-11-04T21:01:00Z") + "\n")
        p2 = self.write("p.jsonl", power("2026-11-04T21:00:00Z") + "\n")
        err = self.assert_invalid(self.go([self.dns()], [p1, p2]), "earlier than the previous sample")
        self.assertIn(f"{p2}:1:", err)

    def test_extra_power_fields_are_ignored(self):
        p = self.write("p.jsonl", power("2026-11-04T21:00:00Z", capacity=88, dns={"ok": True},
                                        services={"unbound": 0}) + "\n"
                        + power("2026-11-04T21:01:00Z") + "\n")
        rc, rep, out, err = self.go([self.dns()], [p])
        self.assertEqual(rc, 0)
        self.assertEqual(rep["power"]["effective_samples"], 2)

    def test_invalid_power_rows(self):
        e = ep("2026-11-04T21:00:00Z")
        bad_lines = [
            "not json",
            "[1, 2]",
            '"text"',
            "",
            json.dumps({"boot_id": "b", "uptime_seconds": 1.0}),
            json.dumps({"epoch": True, "boot_id": "b", "uptime_seconds": 1.0}),
            '{"epoch": NaN, "boot_id": "b", "uptime_seconds": 1.0}',
            '{"epoch": Infinity, "boot_id": "b", "uptime_seconds": 1.0}',
            json.dumps({"epoch": 1e300, "boot_id": "b", "uptime_seconds": 1.0}),
            json.dumps({"epoch": e, "boot_id": "", "uptime_seconds": 1.0}),
            json.dumps({"epoch": e, "boot_id": 7, "uptime_seconds": 1.0}),
            json.dumps({"epoch": e, "boot_id": "b"}),
            json.dumps({"epoch": e, "boot_id": "b", "uptime_seconds": -1.0}),
            json.dumps({"epoch": e, "boot_id": "b", "uptime_seconds": "1"}),
            json.dumps({"epoch": e, "boot_id": "b", "uptime_seconds": True}),
            json.dumps({"epoch": e, "boot_id": "b", "uptime_seconds": 1.0, "usb_online": 2}),
            json.dumps({"epoch": e, "boot_id": "b", "uptime_seconds": 1.0, "usb_online": True}),
            json.dumps({"epoch": e, "boot_id": "b", "uptime_seconds": 1.0, "ac_online": "1"}),
        ]
        for line in bad_lines:
            with self.subTest(line=line[:40]):
                p = self.write("p.jsonl", line + "\n")
                err = self.assert_invalid(self.go([self.dns()], [p]))
                if line.strip():
                    self.assertNotIn(line.strip()[:20], err)  # no raw payload echoed

    def test_non_utf8_power_file_is_invalid(self):
        p = self.write("p.jsonl", b'{"epoch": 1, "boot_id": "b\xff", "uptime_seconds": 1}\n')
        self.assert_invalid(self.go([self.dns()], [p]), "UTF-8")


class DnsInvalid(CorrelateCase):
    def dns(self):
        return self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z")])

    def test_missing_and_empty_inputs(self):
        self.assert_invalid(self.go([self.dir / "nope.csv"]), "cannot read file")
        self.assert_invalid(self.go([self.write("e.csv", b"")]), "empty file")
        self.assert_invalid(self.go([self.write("h.csv", HEAD.encode())]), "no effective DNS observation")

    def test_bad_header_width_and_quoting(self):
        self.assert_invalid(self.go([self.write("a.csv", "time,health_udp,ms\n" + row("2026-11-04T21:00:00Z"))]),
                            "unsupported header")
        self.assert_invalid(self.go([self.dns_file("a.csv", [row("2026-11-04T21:00:00Z").rstrip("\n") + ",extra\n"])]),
                            "expected 11 fields")
        self.assert_invalid(self.go([self.dns_file("a.csv", ["2026-11-04T21:00:00Z,NOERROR\n"])]),
                            "expected 11 fields")
        self.assert_invalid(self.go([self.write("a.csv", HEAD + row("2026-11-04T21:00:00Z").replace(",300\n", ',"300\n'))]),
                            "malformed CSV")

    def test_bad_timestamps(self):
        for t in ("not-a-time", "2026-11-04", "2026-13-04T21:00:00Z", "0001-01-01T00:00:00+01:00"):
            with self.subTest(t=t):
                self.assert_invalid(self.go([self.dns_file("a.csv", [row(t)])]), "invalid timestamp")

    def test_bad_answers(self):
        self.assert_invalid(self.go([self.dns_file("a.csv", [row("2026-11-04T21:00:00Z", GOOD.replace(
            "NOERROR,3,", ",3,"))])]), "empty answer for health_udp")
        self.assert_invalid(self.go([self.dns_file("a.csv", [row("2026-11-04T21:00:00Z", GOOD.replace(
            "NOERROR,3,", "  ,3,"))])]), "empty answer")

    def test_bad_latencies(self):
        for bad in ("12.5", "-1", "+3", " 3", "1_0", "0x10", "1e3", "", "9" * 25):
            with self.subTest(bad=bad):
                body = GOOD.replace("NOERROR,3,", f"NOERROR,{bad},")
                self.assert_invalid(self.go([self.dns_file("a.csv", [row("2026-11-04T21:00:00Z", body)])]),
                                    "latency")

    def test_latency_bounds_are_inclusive(self):
        for good in ("0", str(2 ** 63 - 1)):
            with self.subTest(good=good):
                body = GOOD.replace("NOERROR,3,", f"NOERROR,{good},")
                rc, rep, out, err = self.go([self.dns_file("a.csv", [row("2026-11-04T21:00:00Z", body),
                                                                     row("2026-11-04T21:01:00Z")])])
                self.assertEqual(rc, 0, err)

    def test_non_utf8_and_payload_are_not_echoed(self):
        raw = (HEAD + '2026-11-04T21:00:00Z,NOERROR,"3\nEVILPAYLOAD",NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300\n').encode()
        err = self.assert_invalid(self.go([self.write("a.csv", raw)]), "malformed CSV")
        self.assertNotIn("EVILPAYLOAD", err)
        self.assert_invalid(self.go([self.write("a.csv", HEAD.encode() + b"2026-11-04T21:00:00Z,\xff,3\n")]),
                            "UTF-8")

    def test_maintenance_file_must_exist_when_supplied_and_reasons_are_not_echoed(self):
        self.assert_invalid(self.go([self.dns()], maint=self.dir / "nope.txt"), "cannot read file")
        m = self.write("m.txt", "2026-11-04T22:00:00Z 2026-11-04T21:00:00Z EVILREASON\n")
        err = self.assert_invalid(self.go([self.dns()], maint=m), "reversed maintenance window")
        self.assertNotIn("EVILREASON", err)
        for content in ("2026-11-04T21:00:00Z\n", "nope 2026-11-04T22:00:00Z x\n"):
            with self.subTest(content=content):
                m = self.write("m.txt", content)
                self.assert_invalid(self.go([self.dns()], maint=m))


class Caps(CorrelateCase):
    def test_caps_are_16_mib_dns_power_and_1_mib_maintenance(self):
        self.assertEqual(correlate.MAX_DNS_BYTES, 16 * 1024 * 1024)
        self.assertEqual(correlate.MAX_POWER_BYTES, 16 * 1024 * 1024)
        self.assertEqual(correlate.MAX_MAINTENANCE_BYTES, 1024 * 1024)

    def test_cap_is_per_file_inclusive_and_counted_from_bytes_read(self):
        raw = (HEAD + row("2026-11-04T21:00:00Z")).encode()
        raw2 = (HEAD + row("2026-11-04T21:01:00Z")).encode()
        self.assertEqual(len(raw), len(raw2))
        f1, f2 = self.write("a.csv", raw), self.write("b.csv", raw2)
        with mock.patch.object(correlate, "MAX_DNS_BYTES", len(raw)):
            code, out, err = self.run_main([f1, f2])
            self.assertEqual(err, "")  # two files, each exactly at the cap, are both fine
            self.assertIn('"effective_observations": 2', out)
        with mock.patch.object(correlate, "MAX_DNS_BYTES", len(raw) - 1):
            code, out, err = self.run_main([f1])
            self.assertEqual((code, out), (2, ""))
            self.assertIn(f"larger than the {len(raw) - 1}-byte input cap", err)
        # a pipe has no size metadata: the byte count alone must enforce the cap
        fifo = self.dir / "pipe.csv"
        os.mkfifo(fifo)
        with mock.patch.object(correlate, "MAX_DNS_BYTES", len(raw)):
            def feed():
                with open(fifo, "wb") as f:
                    f.write(raw + b"x")
            t = threading.Thread(target=feed)
            t.start()
            code, out, err = self.run_main([fifo])
            t.join()
            self.assertEqual((code, out), (2, ""))
            self.assertIn("larger than the", err)

    def test_power_and_maintenance_caps(self):
        line = power("2026-11-04T21:00:00Z") + "\n"
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:01:00Z")])
        p = self.write("p.jsonl", line.encode())
        with mock.patch.object(correlate, "MAX_POWER_BYTES", len(line)):
            code, out, err = self.run_main([dns], [p])
            self.assertEqual(err, "")
        with mock.patch.object(correlate, "MAX_POWER_BYTES", len(line) - 1):
            code, out, err = self.run_main([dns], [p])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("larger than the", err)
        m = self.write("m.txt", b"2026-11-04T21:00:00Z 2026-11-04T21:01:00Z x\n")
        with mock.patch.object(correlate, "MAX_MAINTENANCE_BYTES", m.stat().st_size):
            code, out, err = self.run_main([dns], maint=m)
            self.assertEqual(err, "")
        with mock.patch.object(correlate, "MAX_MAINTENANCE_BYTES", m.stat().st_size - 1):
            code, out, err = self.run_main([dns], maint=m)
            self.assertEqual((code, out), (2, ""))
            self.assertIn("larger than the", err)

    def test_dns_cap_at_real_scale_is_inclusive(self):
        prefix = HEAD + "2026-11-04T21:00:00Z,"
        suffix = ",3,NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300\n"
        pad = correlate.MAX_DNS_BYTES - len(prefix.encode()) - len(suffix.encode())
        raw = (prefix + "X" * pad + suffix).encode()
        self.assertEqual(len(raw), correlate.MAX_DNS_BYTES)
        dns = self.write("big.csv", raw)
        rc, rep, out, err = self.go([dns])
        self.assertEqual(err, "")            # exactly 16 MiB is read, not rejected
        self.assertIsNotNone(rep)
        self.assertEqual(rep["dns"]["effective_observations"], 1)
        dns = self.write("big2.csv", raw + b" ")
        err = self.assert_invalid(self.go([dns]), "larger than the 16777216-byte input cap")
        self.assertNotIn("XXXX", err)


class Regressions(CorrelateCase):
    def test_dns_writer_and_auditor_sample_schema(self):
        writer = load_script(SCRIPT.with_name("i14-dns-soak.py"))
        audit = load_script(SCRIPT.with_name("i14-window-audit.py"))
        dns = self.dir / "dns.csv"
        epoch = int(ep(BASE))
        samples = []
        times = [datetime.datetime.fromtimestamp(epoch + offset, datetime.timezone.utc)
                 for offset in (0, 60)]
        with (mock.patch.object(writer, "SERVER", "offline.invalid"),
              mock.patch.object(writer, "OUT", dns),
              mock.patch.object(writer, "datetime") as clock,
              mock.patch.object(writer, "query") as query):
            clock.datetime.now.side_effect = times
            for stamp, first_answer in zip(times, ("ERROR:TimeoutError", "NOERROR")):
                query.side_effect = [first_answer + ",2", "NOERROR,1", "NOERROR,1",
                                     "NOERROR+AD,1", "SERVFAIL,1"]
                writer.main()
                sample = {"epoch": int(stamp.timestamp()), "iso": stamp.isoformat(),
                          "boot_id": "00000000-0000-4000-8000-000000000001",
                          "uptime_seconds": stamp.timestamp() - epoch + 0.05,
                          "usb_online": 0, "ac_online": 1,
                          "dns": {"ok": True, "ms": 2}, "services": dict.fromkeys(audit.SERVICES, 0)}
                self.assertEqual(audit.sample_problems(sample), [])
                samples.append(json.dumps(sample))
        path = self.write("samples.jsonl", "\n".join(samples))
        rc, report, _, err = self.go([dns], [path])
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(report["dns"]["passed"], 1)
        self.assertEqual(report["unplanned_by_power_state"]["online"], 1)
        self.assertEqual(report["unplanned_failures"][0]["checks"], ["health_udp"])

    def test_fractional_clock_boundaries(self):
        dns = self.dns_file("dns.csv", [
            row("2026-11-04T00:00:00.000000001Z", BAD_UDP),
            row("2026-11-04T00:00:00.000000002Z"),
        ])
        epoch = int(ep(BASE))
        samples = self.write("samples.jsonl", "\n".join(
            f'{{"epoch":{epoch}.{fraction},"uptime_seconds":0.{fraction},'
            '"boot_id":"sample","usb_online":1}' for fraction in ("000000001", "100000001")
        ))
        rc, report, _, err = self.go([dns], [samples], dns_gap="0.000000001", power_gap="0.1")
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(report["dns"]["effective_observations"], 2)
        self.assertEqual(report["unplanned_by_power_state"]["online"], 1)

    def test_malformed_and_valid_quoted_answers(self):
        for answer, valid in (('NO"ERROR', False), ('"NO""ERROR,other"', True)):
            with self.subTest(answer=answer):
                dns = self.dns_file("dns.csv", [row(BASE, GOOD.replace("NOERROR,3", answer + ",3")),
                                                 row("2026-11-04T00:01:00Z")])
                result = self.go([dns])
                if valid:
                    self.assertEqual(result[1]["unplanned_failures"][0]["checks"], ["health_udp"])
                else:
                    self.assert_invalid(result, "malformed CSV quoting")

    def test_duplicate_keys_nonstandard_constants_and_excess_json_depth(self):
        dns = self.dns_file("dns.csv", [row(BASE)])
        for extra in ('"boot_id":"other"', '"ignored":NaN', '"ignored":' + "[" * 5000):
            with self.subTest(extra=extra[:20]):
                sample = power(BASE)[:-1] + "," + extra + "}"
                path = self.write("samples.jsonl", sample)
                err = self.assert_invalid(self.go([dns], [path]), f"{path}:1:")
                self.assertNotIn(extra[:20], err)

    def test_point_maintenance_splits_gap_and_covers_only_that_instant(self):
        middle = "2026-11-04T00:01:00Z"
        end = "2026-11-04T00:02:00Z"
        maint = self.write("maintenance.txt", f"{middle} {middle} marker")
        dns = self.dns_file("dns.csv", [row(BASE), row(end)])
        rc, report, _, err = self.go([dns], maint=maint, dns_gap=60)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(report["maintenance"]["merged_seconds"], 0)
        dns = self.dns_file("dns.csv", [row(BASE, BAD_UDP), row(middle, BAD_UDP), row(end)])
        rc, report, _, err = self.go([dns], maint=maint, dns_gap=60)
        self.assertEqual((rc, err), (1, ""))
        self.assertEqual(report["dns"]["planned_failures"], 1)


class Cli(CorrelateCase):
    def dns(self):
        return self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:01:00Z")])

    def test_required_flags(self):
        dns = self.dns()
        r = subprocess.run([sys.executable, "-B", str(SCRIPT), "--dns", str(dns),
                            "--power-max-gap-seconds", "180"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        r = subprocess.run([sys.executable, "-B", str(SCRIPT),
                            "--dns-max-gap-seconds", "300"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")

    def test_gap_limits_must_be_finite_positive(self):
        dns = self.dns_file("dns.csv", [row("2026-11-04T21:00:00Z"), row("2026-11-04T21:00:00.500000Z")])
        for bad in ("0", "-1", "nan", "inf", "-inf", "abc"):
            with self.subTest(bad=bad):
                r = subprocess.run([sys.executable, "-B", str(SCRIPT), "--dns", str(dns),
                                    "--dns-max-gap-seconds", bad, "--power-max-gap-seconds", "180"],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, "")
                self.assertEqual(r.stderr.count("\n"), 1)
        rc, rep, out, err = self.go([dns], dns_gap=0.5, power_gap=0.25)
        self.assertEqual(rc, 0)
        self.assertEqual(rep["coverage"]["max_gap_seconds"], 0.5)
        self.assertEqual(rep["power"]["max_gap_seconds"], 0.25)

    def test_repeatable_ordered_flags(self):
        f1 = self.dns_file("dns.csv.1", [row("2026-11-04T21:00:00Z")])
        f2 = self.dns_file("dns.csv", [row("2026-11-04T21:01:00Z")])
        p1 = self.write("p.jsonl.1", power("2026-11-04T21:00:00Z") + "\n")
        p2 = self.write("p.jsonl", power("2026-11-04T21:01:00Z") + "\n")
        rc, rep, out, err = self.go([f1, f2], [p1, p2])
        self.assertEqual(rc, 0)
        self.assertEqual(rep["dns"]["files"], 2)
        self.assertEqual(rep["power"]["files"], 2)
        self.assertNotIn(str(self.dir), out)


if __name__ == "__main__":
    unittest.main()
