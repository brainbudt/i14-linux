"""Host tests for scripts/i14-window-audit.py with synthetic window data.

Each test starts from a scenario that passes every condition and breaks exactly one thing.
The window is shortened to 30 minutes; syslog and battery-log times are written in the
phone's local time (Europe/Amsterdam, UTC+1 in November) as on the device."""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# I14_WINDOW_AUDIT points the tests at another copy of the script (used to show a test RED before its fix).
spec = importlib.util.spec_from_file_location(
    "i14_window_audit", os.environ.get("I14_WINDOW_AUDIT", ROOT / "scripts/i14-window-audit.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

BOOT = "a3840ac8-8b15-43c4-a2a4-7b9b1feabccd"
START = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc).timestamp()
END = START + 1800
BOOT_EPOCH = START - 50000
LOCAL = timedelta(hours=1)  # CET
SERVICES = ("i14-power", "i14-cpufreq", "i14-ui", "unbound", "i14-wifi", "i14-netwatch", "sshd")
PATHS = list(audit.BASELINE_REQUIRED) + ["/usr/libexec/i14-rs/i14-ui", "/etc/unbound/unbound.conf"]
PROCS = [  # pid, command
    (1724, "supervise-daemon unbound --start --pidfile /run/unbound.pid /usr/sbin/unbound -- -d"),
    (1727, "/usr/sbin/unbound -d -c /etc/unbound/unbound.conf"),
    (1747, "/usr/sbin/chronyd -f /etc/chrony/chrony.conf -F 1"),
    (1748, "/usr/sbin/chronyd -f /etc/chrony/chrony.conf -F 1"),
    (1789, "supervise-daemon i14-cpufreq --start /usr/libexec/i14-rs/i14-cpufreq --"),
    (1790, "/usr/libexec/i14-rs/i14-cpufreq"),
    (1873, "supervise-daemon i14-wifi --start /sbin/wpa_supplicant -- -i wlan0"),
    (1874, "/sbin/wpa_supplicant -i wlan0 -D nl80211 -c /run/i14-wpa.conf"),
    (2044, "supervise-daemon i14-netwatch --start /usr/libexec/i14-rs/i14-netwatch --"),
    (2047, "/usr/libexec/i14-rs/i14-netwatch"),
    (2065, "supervise-daemon i14-power --start /usr/libexec/i14-rs/i14-battery-guard --"),
    (2068, "/usr/libexec/i14-rs/i14-battery-guard"),
    (2123, "supervise-daemon i14-ui --start /usr/libexec/i14-rs/i14-ui --"),
    (2124, "/usr/libexec/i14-rs/i14-ui"),
    (2147, "sshd: /usr/sbin/sshd [listener] 0 of 10-100 startups"),
    (14701, "/sbin/syslogd -t -s 2048 -b 4 -n"),
    (14724, "/usr/sbin/crond -c /etc/crontabs -f"),
]


def ticks(epoch):
    """/proc/<pid>/stat start time (clock ticks since boot) of a process started at `epoch`."""
    return int((epoch - BOOT_EPOCH) * 100)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(epoch, fmt):
    return (datetime.fromtimestamp(epoch, timezone.utc) + LOCAL).strftime(fmt)


def syslog(epoch, text):
    return f"{local(epoch, '%b %e %H:%M:%S')} i14 {text}"


class Scenario:
    def __init__(self):
        self.phone = list(range(int(START) - 120, int(END) + 121, 60))
        self.overrides = {}  # epoch -> function(row) mutating the row
        self.extra_phone_lines = []  # (position index, raw line)
        self.dns = [START - 200 + 300 * k for k in range(8)]  # 13:56:40 .. 14:31:40
        self.dns_overrides = {}  # index -> {column: value}
        self.maintenance = ["2026-11-08T13:15:00 2026-11-08T13:35:00 pre-window deploy"]
        self.end_hashes = {}
        self.end_header = {}
        self.end_chrony = "chrony:chrony:750"
        self.end_baseline = True
        self.procs_final = {pid: 1000 * 100 for pid, _ in PROCS}  # started 1000 s after boot
        self.final_snapshot = True
        self.absent = set()  # pids left out of both process snapshots
        self.absent_final = set()  # pids left out of the final snapshot only
        self.absent_first = set()  # pids left out of the first (reference) snapshot only
        self.messages_until = END + 60
        self.messages_from = START - 600
        self.extra_messages = []
        self.start_header = {}
        self.preamble = False  # two junk lines long before the window (any malformed record blocks condition 0)
        self.line_edits = {}  # epoch -> function(raw JSON line) -> raw line
        self.dns_line_edits = {}  # index -> function(raw CSV line) -> raw line
        self.extra_snapshots = []  # (epoch, {pid: ticks}, boot id, extra raw lines)
        self.final_extra_lines = []  # raw lines appended to the final process snapshot
        self.raw_messages = []  # raw lines added to the final messages copy
        self.extra_message_files = {}  # logs/<dir> -> raw lines of an additional messages copy
        self.unterminated = set()  # extra_message_files written without a final newline
        self.failed_captures = []  # epochs of captures whose procs.txt is empty and failed in index.txt
        self.index_lines = {}  # logs/<dir> -> index.txt line replacing the default "ok" line (None: omit)
        self.dir_snapshots = []  # (logs/<dir>, capture epoch or None for a malformed time line, extra raw lines
        #                          [, {pid: ticks} instead of the reference rows])

    def sample(self, epoch):
        row = {"epoch": epoch, "iso": iso(epoch), "boot_id": BOOT, "uptime_seconds": epoch - BOOT_EPOCH + 0.05,
               "services": dict.fromkeys(SERVICES, 0), "dns": {"ok": True, "ms": 40},
               "charge_status": json.dumps({"schema": 1, "backend": "bcct", "boot_id": BOOT,
                                            "updated_monotonic": epoch - BOOT_EPOCH - 10, "paused": True,
                                            "valid": True, "enabled": True, "error": None, "limit": 80,
                                            "raw_level": 81, "zcv_mv": 4120}),
               "bcct_state": "curr_limit 1\nmtk-cl-bcct00 1 mA, state 1"}
        if epoch in self.overrides:
            self.overrides[epoch](row)
        return row

    def write(self, base):
        base = Path(base)
        lines = [json.dumps({"epoch": int(START) - 90000, "iso": "x"}), "{truncated long before"] if self.preamble else []
        lines += [self.line_edits.get(e, lambda raw: raw)(json.dumps(self.sample(e))) for e in self.phone]
        for position, raw in self.extra_phone_lines:
            lines.insert(position, raw)
        (base / "samples.jsonl").write_text("\n".join(lines) + "\n")
        rows = ["time,health_udp,ms,health_tcp,ms,example_udp,ms,signed_ad,ms,bogus,ms"]
        for index, t in enumerate(self.dns):
            values = {"health_udp": "NOERROR", "health_tcp": "NOERROR", "example_udp": "NOERROR",
                      "signed_ad": "NOERROR+AD", "bogus": "SERVFAIL"}
            values.update(self.dns_overrides.get(index, {}))
            stamp = iso(t)[:-1] + ("+00:00" if index % 2 else "")
            row = ",".join([stamp] + [f"{values[k]},{12 + index}" for k in values])
            rows.append(self.dns_line_edits.get(index, lambda raw: raw)(row))
        (base / "dns.csv").write_text("\n".join(rows) + "\n")
        (base / "maintenance.txt").write_text("# START END reason\n" + "\n".join(self.maintenance) + "\n")
        hashes = {p: f"{i:064x}" for i, p in enumerate(PATHS)}
        self._baseline(base / "baseline-start.txt",
                       {"utc": iso(START + 1), "uptime": f"{START + 1 - BOOT_EPOCH:.2f}", **self.start_header},
                       "chrony:chrony:750", hashes)
        if self.end_baseline:
            self._baseline(base / "baseline-end.txt",
                           {"utc": iso(END + 120), "uptime": f"{END + 120 - BOOT_EPOCH:.2f}", **self.end_header},
                           self.end_chrony, {**hashes, **self.end_hashes})
        logs = base / "logs"
        first = logs / "20261108T135900Z"
        last = logs / "20261108T143100Z"
        first.mkdir(parents=True)
        procs_first = {pid: 1000 * 100 for pid, _ in PROCS if pid not in self.absent | self.absent_first}
        self._procs(first / "procs.txt", START - 60, procs_first)
        stamp = int(START) - 3600
        early = [syslog(t, "cron.info crond[14724]: USER root pid 1 cmd 'sampler'") for t in range(stamp, int(START) - 59, 60)]
        (first / "messages").write_text("\n".join(early) + "\n")
        (first / "messages.err").write_text("Nov  8 15:10:00 i14 daemon.info x[1]: restarting everything\n")
        (first / "i14-battery.log").write_text("")
        if self.final_snapshot:
            last.mkdir()
            final = {pid: t for pid, t in self.procs_final.items() if pid not in self.absent | self.absent_final}
            self._procs(last / "procs.txt", END + 60, final, extra=self.final_extra_lines)
            body = [syslog(t, f"cron.info crond[14724]: USER root pid {t % 9999} cmd 'sampler'")
                    for t in range(int(self.messages_from), int(self.messages_until) + 1, 60)]
            body += [syslog(t, text) for t, text in self.extra_messages] + self.raw_messages
            (last / "messages").write_text("\n".join(sorted(body)) + "\n")
            battery = [f"{local(t, '%Y-%m-%d %H:%M:%S')} capacity=100 gauge=81 zcv=4120 status=Full "
                       f"discharge={1 if t < START + 900 else 0} valid=1 backend=bcct"
                       for t in range(int(START), int(END) + 1, 300)]
            (last / "i14-battery.log").write_text("\n".join(battery) + "\n")
        for when, ticks, boot, extra in self.extra_snapshots:
            folder = logs / datetime.fromtimestamp(when, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            folder.mkdir(exist_ok=True)
            self._procs(folder / "procs.txt", when, ticks, boot=boot, extra=extra)
        for name, when, extra, *rows in self.dir_snapshots:  # the directory name need not match the capture time
            (logs / name).mkdir(exist_ok=True)
            self._procs(logs / name / "procs.txt", when if when is not None else START,
                        rows[0] if rows else procs_first, extra=extra)
            if when is None:
                path = logs / name / "procs.txt"
                path.write_text("not-a-time\n" + path.read_text().split("\n", 1)[1])
        for name, raw in self.extra_message_files.items():
            (logs / name).mkdir(exist_ok=True)
            ending = "" if name in self.unterminated else "\n"  # no final newline: the last line was cut off
            (logs / name / "messages").write_text("\n".join(raw) + ending)
            if not (logs / name / "procs.txt").exists():  # a complete hourly capture, unchanged processes
                when = datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
                self._procs(logs / name / "procs.txt", when, procs_first)
        index = {}
        for when in self.failed_captures:
            name = datetime.fromtimestamp(when, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (logs / name).mkdir(exist_ok=True)
            (logs / name / "procs.txt").write_text("")
            index[name] = f"{name} ok: absent: none failed: messages(rc=255) procs(rc=255)"
        for folder in sorted(p for p in logs.iterdir() if p.is_dir()):
            index.setdefault(folder.name, f"{folder.name} ok: messages procs absent: none failed: none")
        index.update(self.index_lines)
        (logs / "index.txt").write_text("".join(f"{line}\n" for _, line in sorted(index.items()) if line))
        return base

    @staticmethod
    def _baseline(path, header, chrony, hashes):
        head = {"utc": "", "boot_id": BOOT, "kernel": "3.18.19", "alpine": "3.24.3", "uptime": ""}
        head.update(header)
        text = ["# " + " ".join(f"{k}={v}" for k, v in head.items()), f"# chrony_dir={chrony}"]
        text += [f"{h}  {p}" for p, h in hashes.items()]
        path.write_text("\n".join(text) + "\n")

    @staticmethod
    def _procs(path, when, ticks, boot=BOOT, extra=()):
        lines = [iso(when), boot] + [f"{pid} {ticks[pid]} {cmd}" for pid, cmd in PROCS if pid in ticks]
        lines += [f"{30000 + int(when) % 1000} {int((when - BOOT_EPOCH) * 100)} sshd-session: root [priv]"]
        lines += list(extra)
        path.write_text("\n".join(lines) + "\n")


def run(scenario, patch=None, **overrides):
    with tempfile.TemporaryDirectory() as tmp:
        base = scenario.write(tmp)
        if patch:
            patch(Path(base))
        argv = ["--samples", str(base / "samples.jsonl"), "--dns-csv", str(base / "dns.csv"),
                "--maintenance", str(base / "maintenance.txt"), "--baseline-start", str(base / "baseline-start.txt"),
                "--logs", str(base / "logs"), "--start", iso(START), "--end", iso(END),
                "--json-out", str(base / "report.json")]
        if scenario.end_baseline:
            argv += ["--baseline-end", str(base / "baseline-end.txt")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = audit.main(argv)
        report = json.loads((base / "report.json").read_text())
    return code, report, out.getvalue()


def status(report, number):
    return report["conditions"][number]["status"]


def kinds(report, number, key="failures"):
    return [item["kind"] for item in report["conditions"][number][key]]


class WindowAuditTest(unittest.TestCase):
    def assertOnly(self, report, number, expected):
        """Condition `number` has `expected`; every other gating condition passes."""
        for c in report["conditions"]:
            if not c["gating"]:
                continue
            want = expected if c["condition"] == number else "PASS"
            if isinstance(number, (set, tuple)):
                want = expected if c["condition"] in number else "PASS"
            self.assertEqual(c["status"], want, (c["condition"], c["failures"], c["cannot_evaluate"]))

    def test_clean_window_passes(self):
        code, report, text = run(Scenario())
        self.assertEqual(code, 0, text)
        self.assertEqual(report["verdict"], "PASS")
        self.assertIn("Verdict: PASS", text)
        self.assertEqual(status(report, 6), "REPORTED")
        self.assertEqual(report["conditions"][3]["info"]["final_sample"]["time"], iso(END))
        bands = report["conditions"][6]["info"]["battery_log_discharge_transitions"]
        self.assertEqual([b["discharge"] for b in bands], [["1", "0"]])
        self.assertTrue(all(len(v["sha256"]) == 64 for v in report["inputs"]["files"].values()))

    def test_end_boundary_missing_cannot_pass(self):
        s = Scenario()
        s.phone = [e for e in s.phone if e < END]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertEqual(status(report, 1), "CANNOT-EVALUATE")
        self.assertIn("boundary_not_covered", kinds(report, 1, "cannot_evaluate"))
        self.assertIn("phone_data_does_not_reach_window_end", kinds(report, 3, "cannot_evaluate"))
        self.assertEqual(status(report, 4), "CANNOT-EVALUATE")

    def test_start_boundary_bracket_too_wide_fails(self):
        s = Scenario()
        s.phone = [e for e in s.phone if e not in (START - 60, START, START + 60)]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertEqual(status(report, 1), "FAIL")
        self.assertIn("boundary_bracket_too_wide", kinds(report, 1))

    def test_gap_of_181_seconds_fails_and_180_passes(self):
        s = Scenario()
        s.phone = [e for e in s.phone if e not in (START + 600, START + 660)]
        code, report, text = run(s)
        self.assertEqual(code, 0, text)  # 540 -> 720 is exactly 180 s
        s.phone = [e + 1 if e == START + 720 else e for e in s.phone]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 3, "FAIL")
        gap = report["conditions"][3]["failures"][0]
        self.assertEqual((gap["kind"], gap["seconds"]), ("phone_gap", 181))
        self.assertEqual(gap["from"]["time"], iso(START + 540))

    def test_final_sample_service_failure_fails(self):
        s = Scenario()
        s.overrides[END] = lambda row: row["services"].update({"i14-ui": 3})
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 3, "FAIL")
        failure = report["conditions"][3]["failures"][0]
        self.assertEqual((failure["kind"], failure["time"], failure["statuses"]),
                         ("service_status_nonzero", iso(END), {"i14-ui": 3}))
        # A failure after the window end is outside the audit.
        s.overrides = {END + 60: lambda row: row["services"].update({"i14-ui": 3})}
        self.assertEqual(run(s)[0], 0)

    def test_malformed_rows_prevent_pass(self):
        s = Scenario()
        s.overrides[START + 300] = lambda row: row["services"].update({"unbound": "0"})
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        # Condition 0 is unevaluable for any malformed record (global rule); condition 3 fails.
        self.assertEqual((status(report, 0), status(report, 3)), ("CANNOT-EVALUATE", "FAIL"))
        self.assertIn("phone_invalid_row", kinds(report, 3))
        s = Scenario()
        s.overrides[START + 300] = lambda row: row.pop("services")
        self.assertEqual(status(run(s)[1], 3), "FAIL")
        s = Scenario()
        s.overrides[START + 300] = lambda row: row["dns"].pop("ok")
        code, report, _ = run(s)
        self.assertEqual((status(report, 0), status(report, 4)), ("CANNOT-EVALUATE", "FAIL"))
        s = Scenario()
        s.extra_phone_lines.append((10, '{"epoch": 17941'))  # truncated line between in-window rows
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 0, "CANNOT-EVALUATE")
        self.assertEqual(kinds(report, 0, "cannot_evaluate"), ["phone_malformed_record"])

    def test_s19_non_standard_json_constants_prevent_pass(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            s = Scenario()
            s.line_edits[START + 300] = lambda raw, c=constant: raw[:-1] + f', "temperature_c": {c}}}'
            code, report, _ = run(s)
            self.assertEqual(code, 1, constant)
            self.assertEqual(status(report, 0), "CANNOT-EVALUATE", constant)

    def test_s20_empty_baseline_version_prevents_pass(self):
        for key in ("kernel", "alpine"):
            s = Scenario()
            s.start_header = {key: ""}
            s.end_header = {key: ""}
            code, report, _ = run(s)
            self.assertEqual(code, 1, key)
            self.assertNotEqual(status(report, 2), "PASS", key)

    def test_s21_invalid_utf8_in_any_used_input_prevents_pass(self):
        def damage_sample(base):
            path = base / "samples.jsonl"
            lines = path.read_bytes().split(b"\n")
            lines[5] = lines[5][:-1] + b', "temperature_c": "bad\xffbyte"}'
            path.write_bytes(b"\n".join(lines))
        def damage_syslog(base):
            path = next(p for p in sorted((base / "logs").glob("*/messages")))
            path.write_bytes(path.read_bytes().replace(b"\n", b" rest\xffarting\n", 1))
        for patch in (damage_sample, damage_syslog):
            code, report, _ = run(Scenario(), patch=patch)
            self.assertEqual(code, 1, patch.__name__)
            self.assertEqual(status(report, 0), "CANNOT-EVALUATE", patch.__name__)
            self.assertIn("input_not_utf8", kinds(report, 0, "cannot_evaluate"))

    def test_s21_explicit_input_named_err_is_still_checked(self):
        def rename(base):
            src = base / "samples.jsonl"
            lines = src.read_bytes().split(b"\n")
            lines[5] = lines[5][:-1] + b', "temperature_c": "bad\xffbyte"}'
            src.write_bytes(b"\n".join(lines))
        s = Scenario()
        with tempfile.TemporaryDirectory() as tmp:
            base = s.write(tmp)
            rename(Path(base))
            err = Path(base) / "samples.err"
            (Path(base) / "samples.jsonl").rename(err)
            argv = ["--samples", str(err), "--dns-csv", str(base / "dns.csv"),
                    "--maintenance", str(base / "maintenance.txt"), "--baseline-start", str(base / "baseline-start.txt"),
                    "--logs", str(base / "logs"), "--start", iso(START), "--end", iso(END),
                    "--json-out", str(base / "report.json")]
            if s.end_baseline:
                argv += ["--baseline-end", str(base / "baseline-end.txt")]
            with contextlib.redirect_stdout(io.StringIO()):
                code = audit.main(argv)
            report = json.loads((base / "report.json").read_text())
        self.assertEqual(code, 1)
        self.assertIn("input_not_utf8", kinds(report, 0, "cannot_evaluate"))

    def test_s21_explicit_err_input_inside_logs_is_still_checked(self):
        s = Scenario()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(s.write(tmp))
            src = base / "samples.jsonl"
            lines = src.read_bytes().split(b"\n")
            lines[5] = lines[5][:-1] + b', "temperature_c": "bad\xffbyte"}'
            err = base / "logs" / "samples.err"
            err.write_bytes(b"\n".join(lines))
            argv = ["--samples", str(err), "--dns-csv", str(base / "dns.csv"),
                    "--maintenance", str(base / "maintenance.txt"), "--baseline-start", str(base / "baseline-start.txt"),
                    "--logs", str(base / "logs"), "--start", iso(START), "--end", iso(END),
                    "--json-out", str(base / "report.json")]
            if s.end_baseline:
                argv += ["--baseline-end", str(base / "baseline-end.txt")]
            with contextlib.redirect_stdout(io.StringIO()):
                code = audit.main(argv)
            report = json.loads((base / "report.json").read_text())
        self.assertEqual(code, 1)
        self.assertIn("input_not_utf8", kinds(report, 0, "cannot_evaluate"))

    def test_s23_outputs_cannot_overwrite_inputs(self):
        s = Scenario()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(s.write(tmp))
            before = (base / "samples.jsonl").read_bytes()
            common = ["--samples", str(base / "samples.jsonl"), "--dns-csv", str(base / "dns.csv"),
                      "--maintenance", str(base / "maintenance.txt"), "--baseline-start", str(base / "baseline-start.txt"),
                      "--logs", str(base / "logs"), "--start", iso(START), "--end", iso(END)]
            for flag, target in (("--json-out", base / "samples.jsonl"), ("--summary-out", base / "dns.csv"),
                                 ("--json-out", base / "logs" / "x.json")):
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                        contextlib.redirect_stdout(io.StringIO()):
                    audit.main(common + [flag, str(target)])
            self.assertEqual((base / "samples.jsonl").read_bytes(), before)

    def test_s23_hard_and_symbolic_link_aliases_cannot_overwrite_inputs(self):
        s = Scenario()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(s.write(tmp))
            common = ["--samples", str(base / "samples.jsonl"), "--dns-csv", str(base / "dns.csv"),
                      "--maintenance", str(base / "maintenance.txt"), "--baseline-start", str(base / "baseline-start.txt"),
                      "--logs", str(base / "logs"), "--start", iso(START), "--end", iso(END)]
            procs = next((base / "logs").glob("*/procs.txt"))
            before = procs.read_bytes()
            hard = base / "report-hard.json"
            os.link(procs, hard)  # output hard-linked to a log input
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                audit.main(common + ["--json-out", str(hard)])
            self.assertEqual(procs.read_bytes(), before)
            out = base / "report-target.json"
            out.write_text("{}")
            (base / "logs" / "alias.txt").symlink_to(out)  # a log input that points at the output
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                audit.main(common + ["--json-out", str(out)])
            (base / "logs" / "alias.txt").unlink()
            outside = base / "elsewhere"
            outside.mkdir()
            (base / "logs" / "20261108T150000Z").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                audit.main(common + ["--json-out", str(base / "r.json")])  # symlinked capture directory
            (base / "logs" / "20261108T150000Z").unlink()
            # A pre-planted link at the old predictable temporary name must stay untouched.
            target = base / "safe.json"
            planted = base / f".safe.json.{os.getpid()}.tmp"
            os.link(procs, planted)
            with contextlib.redirect_stdout(io.StringIO()):
                audit.main(common + ["--json-out", str(target)])
            self.assertEqual(procs.read_bytes(), before)
            self.assertTrue(target.is_file())
            one, two = base / "one.json", base / "two.txt"
            one.write_text("{}")
            os.link(one, two)  # the two outputs hard-linked to each other
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()):
                audit.main(common + ["--json-out", str(one), "--summary-out", str(two)])

    def test_s22_non_finite_or_non_positive_log_gap_is_refused(self):
        for value in ("nan", "inf", "0", "-5"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                audit.main(["--samples", "x", "--dns-csv", "x", "--maintenance", "x",
                            "--baseline-start", "x", "--logs", "x", "--max-log-gap", value])

    def test_duplicate_and_out_of_order_timestamps_fail(self):
        s = Scenario()
        s.phone.insert(5, s.phone[4])
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertIn("phone_duplicate_timestamp", kinds(report, 0))
        self.assertIn("phone_duplicate_timestamp", kinds(report, 3))
        s = Scenario()
        s.phone[5], s.phone[6] = s.phone[6], s.phone[5]
        self.assertIn("phone_out_of_order", kinds(run(s)[1], 0))

    def test_dns_gap_of_451_seconds_fails_and_450_passes(self):
        s = Scenario()
        s.dns = [START - 200, START + 100, START + 550] + [START + 550 + 300 * k for k in range(1, 6)]
        self.assertEqual(run(s)[0], 0)
        s.dns = [START - 200, START + 100, START + 551] + [START + 551 + 300 * k for k in range(1, 6)]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 5, "FAIL")
        self.assertEqual(report["conditions"][5]["failures"][0]["seconds"], 451)

    def test_wrong_or_unknown_dns_result_fails(self):
        s = Scenario()
        s.dns_overrides[3] = {"bogus": "NOERROR"}
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 5, "FAIL")
        failure = report["conditions"][5]["failures"][0]
        self.assertEqual(failure["results"], {"bogus": {"got": "NOERROR", "want": "SERVFAIL"}})
        s = Scenario()
        s.dns_overrides[3] = {"signed_ad": "NOERROR"}  # AD flag missing
        self.assertEqual(status(run(s)[1], 5), "FAIL")
        s = Scenario()
        s.dns_overrides[3] = {"health_tcp": "ERROR:timeout"}
        self.assertEqual(status(run(s)[1], 5), "FAIL")
        s = Scenario()
        s.dns_overrides[3] = {"health_udp": "MAYBE"}
        code, report, _ = run(s)
        self.assertEqual((status(report, 0), status(report, 5)), ("CANNOT-EVALUATE", "FAIL"))
        s = Scenario()
        s.dns_overrides[0] = {"bogus": "NOERROR"}  # before the window: not audited
        self.assertEqual(run(s)[0], 0)

    def test_maintenance_inside_window_fails(self):
        s = Scenario()
        s.maintenance.append("2026-11-08T14:20:00 2026-11-08T14:25:00 unavoidable restart")
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["maintenance_inside_window"])
        s = Scenario()
        s.maintenance.append("2026-11-08T13:50:00 2026-11-08T14:00:00 overlapping the start")
        self.assertEqual(status(run(s)[1], 2), "FAIL")

    def test_baseline_changes_fail(self):
        s = Scenario()
        s.end_hashes = {"/mnt/data/media/0/i14-linux/ui.json": "f" * 64}
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(report["conditions"][2]["failures"][0]["path"], "/mnt/data/media/0/i14-linux/ui.json")
        s = Scenario()
        s.end_hashes = {"/etc/new-file": "e" * 64}
        self.assertIn("baseline_path_added_at_end", kinds(run(s)[1], 2))
        s = Scenario()
        s.end_chrony = "root:root:755"
        self.assertIn("chrony_dir_changed", kinds(run(s)[1], 2))
        s = Scenario()
        s.end_header = {"boot_id": "00000000-0000-0000-0000-000000000000"}
        self.assertIn("boot_id_changed", kinds(run(s)[1], 2))
        s = Scenario()
        s.end_baseline = False
        code, report, _ = run(s)
        self.assertEqual(status(report, 2), "CANNOT-EVALUATE")

    def test_restart_of_supervised_process_fails(self):
        s = Scenario()
        s.procs_final[2124] = ticks(START + 700)  # i14-ui child respawned at 14:11:40Z
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        failures = report["conditions"][2]["failures"]
        self.assertEqual(kinds(report, 2), ["process_set_changed"])
        self.assertEqual((failures[0]["removed"], failures[0]["added"]),
                         ([["i14-ui", 2124, 100000]], [["i14-ui", 2124, ticks(START + 700)]]))

    def test_missing_monitored_process_fails(self):
        s = Scenario()
        s.absent = {2124}  # i14-ui child never seen
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual([(f["kind"], f["role"]) for f in report["conditions"][2]["failures"]],
                         [("monitored_process_absent", "i14-ui")])
        s = Scenario()
        s.absent_final = {1874}  # wpa_supplicant gone by the end snapshot
        code, report, _ = run(s)
        self.assertOnly(report, 2, "FAIL")
        failure = report["conditions"][2]["failures"][0]
        self.assertEqual((failure["kind"], failure["removed"], failure["added"]),
                         ("process_set_changed", [["wpa_supplicant", 1874, 100000]], []))

    def test_restart_line_in_logs_fails_with_local_time_conversion(self):
        s = Scenario()
        # 15:15 local is 14:15Z, inside; 14:50 local is 13:50Z, before the window.
        s.extra_messages = [(START - 600, "daemon.info supervise-daemon[1]: i14-ui, pid 9, exited with return code 1"),
                            (START + 900, "daemon.info supervise-daemon[2123]: respawning i14-ui")]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        failures = report["conditions"][2]["failures"]
        self.assertEqual([(f["kind"], f["time"]) for f in failures], [("restart_line_in_logs", iso(START + 900))])
        self.assertIn("respawning i14-ui", failures[0]["text"])

    def test_per_connection_sshd_and_err_files_are_ignored(self):
        s = Scenario()
        s.extra_messages = [(START + 60, "auth.info sshd-session[99]: Connection closed by 172.16.42.2 port 1 [preauth]"),
                            (START + 61, "auth.info sshd-session[99]: Received disconnect: 11: disconnected by user")]
        code, report, text = run(s)
        self.assertEqual(code, 0, text)

    def test_log_coverage_short_cannot_pass(self):
        s = Scenario()
        s.messages_until = END - 120
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "CANNOT-EVALUATE")
        self.assertIn("log_coverage_short", kinds(report, 2, "cannot_evaluate"))
        s = Scenario()
        s.messages_from = START + 600  # hole between the early copy (ends 13:59Z) and 14:10Z
        code, report, _ = run(s)
        self.assertIn("log_gap", kinds(report, 2, "cannot_evaluate"))

    def test_no_process_snapshot_after_window_end_cannot_pass(self):
        s = Scenario()
        s.final_snapshot = False
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertEqual(status(report, 2), "CANNOT-EVALUATE")
        reasons = [u.get("reason", "") for u in report["conditions"][2]["cannot_evaluate"]]
        self.assertTrue(any("after the window end" in r for r in reasons), reasons)

    def test_local_dns_failure_fails_condition_4(self):
        s = Scenario()
        s.overrides[START + 1200] = lambda row: row.update(dns={"ok": False, "ms": 5000})
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 4, "FAIL")

    def test_battery_findings_are_reported_without_gating(self):
        def stale(row):
            status = json.loads(row["charge_status"])
            status["updated_monotonic"] -= 500
            row["charge_status"] = json.dumps(status)

        def released(row):
            status = json.loads(row["charge_status"])
            status["paused"] = False
            row["charge_status"] = json.dumps(status)

        s = Scenario()
        s.overrides = {START + 300: stale, START + 600: released}
        code, report, _ = run(s)
        self.assertEqual(code, 0)
        info = report["conditions"][6]["info"]
        self.assertEqual([i["time"] for i in info["guard_invalid_or_stale"]], [iso(START + 300)])
        self.assertEqual([(t["time"], t["paused"]) for t in info["bcct_pause_transitions"]],
                         [(iso(START + 600), [True, False]), (iso(START + 660), [False, True])])

    # Strict continuity rule: the monitored rows and boot id are identical in every capture from the last
    # valid one at or before the start to the first valid one at or after the end.

    def test_identical_captures_pass(self):
        s = Scenario()
        s.extra_snapshots = [(START + 600, dict(s.procs_final), BOOT, []),
                             (START + 1200, dict(s.procs_final), BOOT, [])]
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        span = report["conditions"][2]["info"]["process_span"]
        self.assertEqual([c["capture"] for c in span],
                         ["20261108T135900Z", "20261108T141000Z", "20261108T142000Z", "20261108T143100Z"])

    def test_one_pid_change_mid_window_fails_with_the_rows(self):
        s = Scenario()
        mid = {pid: t for pid, t in s.procs_final.items() if pid != 2124}
        respawned = f"2200 {ticks(START + 700)} /usr/libexec/i14-rs/i14-ui"
        s.extra_snapshots = [(START + 900, mid, BOOT, [respawned])]
        s.absent_final, s.final_extra_lines = {2124}, [respawned]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        failures = report["conditions"][2]["failures"]
        self.assertEqual([(f["kind"], f["captured_after"], f["removed"], f["added"]) for f in failures],
                         [("process_set_changed", iso(START + 900), [["i14-ui", 2124, 100000]],
                           [["i14-ui", 2200, ticks(START + 700)]])])

    def test_failed_missing_or_unindexed_capture_between_valid_ones_is_only_listed(self):
        # Identical rows in the valid captures around it prove continuity: a restart would show
        # a new pid and a later start tick there. The skipped capture is listed, not counted.
        name = "20261108T141500Z"
        s = Scenario()  # complete procs.txt, but index.txt records the capture as failed
        s.extra_snapshots = [(START + 900, dict(s.procs_final), BOOT, [])]
        s.index_lines = {name: f"{name} ok: messages absent: none failed: procs(rc=5)"}
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        skipped = report["conditions"][2]["info"]["process_captures_skipped"]
        self.assertEqual([c["capture"] for c in skipped], [name])
        self.assertIn("capture failed per logs/index.txt: procs(rc=5)", skipped[0]["problems"])
        s = Scenario()  # index.txt names a capture whose directory does not exist
        s.index_lines = {name: f"{name} ok: messages procs absent: none failed: none"}
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        self.assertIn("procs.txt missing", report["conditions"][2]["info"]["process_captures_skipped"][0]["problems"])
        s = Scenario()  # a capture directory that index.txt does not record
        s.extra_snapshots = [(START + 900, dict(s.procs_final), BOOT, [])]
        s.index_lines = {name: None}
        self.assertEqual(run(s)[0], 0)
        s = Scenario()  # a failed capture before the reference is outside the span
        s.failed_captures = [START - 1800]
        code, report, text = run(s)
        self.assertEqual(code, 0, text)

    def test_damaged_capture_with_a_changed_row_fails(self):
        s = Scenario()
        changed = dict(s.procs_final)
        changed[2124] = ticks(START + 700)  # i14-ui restarted, seen only in a damaged capture
        s.extra_snapshots = [(START + 900, changed, BOOT, ["garbage"])]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["process_changed_in_damaged_capture"])

    def test_malformed_capture_and_definite_change_fail(self):
        s = Scenario()
        s.extra_snapshots = [(START + 900, dict(s.procs_final), BOOT, ["garbage"])]
        s.procs_final[2124] = ticks(END + 30)
        code, report, _ = run(s)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["process_set_changed"])

    def test_iso_may_trail_epoch_by_one_second_but_not_lead(self):
        def iso_problems(epoch, stamp_epoch):
            return [p for p in audit.sample_problems({"epoch": epoch, "iso": audit.iso(stamp_epoch)})
                    if p.startswith("iso")]
        self.assertEqual(iso_problems(START, START + 1), [])
        self.assertTrue(iso_problems(START, START + 2))
        self.assertTrue(iso_problems(START, START - 1))

    # Review findings: each test reproduces the reviewer's counterexample (S1-S14).

    def test_s1_empty_interior_capture_is_listed_and_bracketing_proves_continuity(self):
        s = Scenario()
        s.failed_captures = [START + 900]  # zero-byte procs.txt, failed: procs(rc=255) in index.txt
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        skipped = report["conditions"][2]["info"]["process_captures_skipped"]
        self.assertEqual(skipped[0]["problems"], ["procs.txt empty", "capture failed per logs/index.txt: procs(rc=255)"])

    def test_s2_malformed_syslog_line_in_window_cannot_pass_without_recovery(self):
        s = Scenario()
        s.raw_messages.append(syslog(START + 905, "daemon.warn supe"))  # cut off mid-line
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "CANNOT-EVALUATE")
        self.assertIn("log_line_malformed", kinds(report, 2, "cannot_evaluate"))
        full = [syslog(t, f"cron.info crond[14724]: USER root pid {t % 9999} cmd 'sampler'")
                for t in range(int(START) - 600, int(START) + 601, 60)]
        variants = {
            # damaged line between two lines that are adjacent in the complete final copy
            "neighbours": full[:-1] + ["\x00\x00 garbage"] + full[-1:],
            "cut_tail": full[:-1] + [full[-1][:40]],  # last line cut off
            "unterminated": full,  # well-formed text, but the final newline is missing
        }
        for label, lines in variants.items():
            s = Scenario()
            s.extra_message_files["20261108T141000Z"] = lines
            if label == "unterminated":
                s.unterminated = {"20261108T141000Z"}
            code, report, _ = run(s)
            self.assertEqual(code, 1, label)
            self.assertOnly(report, 2, "CANNOT-EVALUATE")
            self.assertEqual(kinds(report, 2, "cannot_evaluate"), ["log_line_malformed"], label)

    def test_s3_invalid_baseline_uptime_cannot_pass(self):
        for uptime in ("-1000", "nan"):
            s = Scenario()
            s.start_header = {"uptime": uptime}
            code, report, _ = run(s)
            self.assertEqual(code, 1, uptime)
            self.assertOnly(report, 2, "CANNOT-EVALUATE")
            self.assertIn("baseline_start_malformed", kinds(report, 2, "cannot_evaluate"))

    def test_s4_duplicate_pid_cannot_pass(self):
        s = Scenario()
        s.final_extra_lines = ["2147 100000 sshd-session: root [priv]"]  # the sshd listener's pid again
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "CANNOT-EVALUATE")
        reasons = [u.get("reason", "") for u in report["conditions"][2]["cannot_evaluate"]]
        self.assertTrue(any("after the window end" in r for r in reasons), reasons)

    def test_s5_duplicate_json_keys_are_rejected_at_every_level(self):
        s = Scenario()
        s.line_edits[START + 300] = lambda raw: raw.replace('"i14-ui": 0', '"i14-ui": 3, "i14-ui": 0')
        code, report, _ = run(s)  # unparseable, so a malformed record: condition 0 cannot pass
        self.assertEqual(code, 1)
        self.assertOnly(report, 0, "CANNOT-EVALUATE")
        self.assertEqual(kinds(report, 0, "cannot_evaluate"), ["phone_malformed_record"])
        s = Scenario()
        s.line_edits[START + 300] = lambda raw: raw.replace('"dns": {', '"dns": {"ok": false}, "dns": {')
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 0, "CANNOT-EVALUATE")

    def test_s6_csv_syntax_error_prevents_pass(self):
        s = Scenario()
        s.dns_line_edits[3] = lambda raw: raw.replace(",15,", ',"1"5,', 1)  # lenient csv reads 15
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertEqual(kinds(report, 0, "cannot_evaluate"), ["dns_malformed_record"])
        self.assertEqual((status(report, 0), status(report, 5)), ("CANNOT-EVALUATE", "FAIL"))
        self.assertEqual(kinds(report, 5), ["dns_gap"])  # the unreadable round leaves no evidence

    def test_s7_only_capture_time_and_uptime_may_differ_in_the_baseline_header(self):
        s = Scenario()
        s.start_header = {"_build": "a"}
        s.end_header = {"_build": "b"}
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["baseline_header_changed"])

    def test_s8_exact_boundary_samples_need_no_exterior_records(self):
        s = Scenario()
        s.preamble = False
        s.phone = list(range(int(START), int(END) + 1, 60))
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        s = Scenario()  # far exterior records, one from another boot, are irrelevant
        s.phone = [int(START) - 1000] + list(range(int(START), int(END) + 1, 60)) + [int(END) + 1000]
        s.overrides[int(END) + 1000] = lambda row: row.update(boot_id="00000000-0000-0000-0000-000000000000")
        code, report, text = run(s)
        self.assertEqual(code, 0, text)

    def test_s9_replacement_starting_after_the_window_fails(self):
        s = Scenario()
        s.procs_final[2124] = ticks(END + 30)  # the predecessor may have died inside the window
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["process_set_changed"])
        s.procs_final[2124] = ticks(START - 30)  # replaced after the reference capture: also inside the span
        self.assertEqual(kinds(run(s)[1], 2), ["process_set_changed"])

    def test_s10_boot_continuity_is_bounded_to_the_covering_snapshots(self):
        other = "00000000-0000-0000-0000-000000000000"
        s = Scenario()
        s.extra_snapshots.append((END + 3600, {pid: 100 for pid, _ in PROCS}, other, []))
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        s = Scenario()
        s.extra_snapshots.append((START + 900, {pid: 100 for pid, _ in PROCS}, other, []))
        self.assertIn("boot_id_changed", kinds(run(s)[1], 2))

    def test_s11_supervise_daemon_existence_probe_is_not_a_restart(self):
        s = Scenario()
        s.extra_messages = [(START + 900, "daemon.info supervise-daemon[2123]: Sending signal 0 to 2124")]
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        s.extra_messages = [(START + 900, "daemon.warn supervise-daemon[2123]: /usr/libexec/i14-rs/i14-ui, "
                                          "pid 2124, terminated by signal 11")]
        self.assertIn("restart_line_in_logs", kinds(run(s)[1], 2))

    def test_s12_later_clock_offset_change_cannot_hide_an_in_window_restart(self):
        def stepped(row):  # the wall clock steps 40 s forward after 14:15Z: epoch - uptime grows by 40 s
            row["uptime_seconds"] -= 40
            status = json.loads(row["charge_status"])
            status["updated_monotonic"] -= 40
            row["charge_status"] = json.dumps(status)

        s = Scenario()
        s.overrides = {e: stepped for e in s.phone if e >= START + 900}
        s.procs_final[2124] = ticks(START - 30)  # mapped with the start anchor: before the window
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["process_set_changed"])

    def test_s13_process_appearing_after_the_reference_fails_under_the_strict_rule(self):
        s = Scenario()
        s.absent_first = {14724}  # crond missing at the 13:59Z reference capture
        s.extra_snapshots = [(START + 5, dict(s.procs_final), BOOT, [])]  # present and unchanged from 14:00:05Z
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        changed = [f for f in report["conditions"][2]["failures"] if f["kind"] == "process_set_changed"]
        self.assertEqual([(f["removed"], f["added"], f["captured_after"], f["may_be_outside_window"])
                          for f in changed],
                         [([], [["crond", 14724, 100000]], iso(START + 5), True)])
        # Once a valid capture at or before the start shows it, the earlier absence is outside the span.
        s.extra_snapshots.append((START - 30, dict(s.procs_final), BOOT, []))
        code, report, text = run(s)
        self.assertEqual(code, 0, text)

    def test_s14_malformed_battery_fields_are_reported_under_condition_6(self):
        def charge(**changes):
            def edit(row):
                status = json.loads(row["charge_status"])
                status.update(changes)
                row["charge_status"] = json.dumps(status)
            return edit

        s = Scenario()
        s.overrides = {START + 300: lambda row: row.update(bcct_state=123),
                       START + 360: lambda row: row.update(bcct_state=["state 1"]),
                       START + 420: lambda row: row.update(charge_status=123),
                       START + 480: charge(raw_level="81"),
                       START + 540: charge(paused="yes")}
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        info = report["conditions"][6]["info"]
        self.assertEqual([i["time"] for i in info["bcct_readback_invalid"]], [iso(START + 300), iso(START + 360)])
        self.assertEqual(info["bcct_readback_changes"], [])
        self.assertEqual([i["time"] for i in info["guard_invalid_or_stale"]],
                         [iso(START + 420), iso(START + 480), iso(START + 540)])


    # Review round 3 (S2, S15, S16).

    def test_s2_malformed_line_with_in_window_timestamp_is_not_excluded_by_its_neighbours(self):
        def cron(t):
            return syslog(t, f"cron.info crond[14724]: USER root pid {t % 9999} cmd 'sampler'")
        # 15:15:05 CET (inside) cut off between the valid 15:31 and 15:32 CET lines (both outside)
        s = Scenario()
        s.extra_message_files["20261108T143300Z"] = [cron(END + 60), syslog(START + 905, "daemon.warn supe"),
                                                     cron(END + 120)]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "CANNOT-EVALUATE")
        self.assertEqual(kinds(report, 2, "cannot_evaluate"), ["log_line_malformed"])
        # A malformed line whose own timestamp is clearly outside the window (and max gap) is ignored.
        s = Scenario()
        s.extra_message_files["20261108T143300Z"] = [cron(END + 60), syslog(END + 3600, "daemon.warn supe"),
                                                     cron(END + 120)]
        code, report, text = run(s)
        self.assertEqual(code, 0, text)

    def test_s15_process_span_is_ordered_by_capture_time_not_directory_name(self):
        extra = [f"15000 {ticks(START + 90)} /usr/sbin/crond -c /etc/crontabs -f"]  # a new monitored pid
        s = Scenario()
        s.dir_snapshots = [("20261108T135000Z", START + 100, extra)]  # captured 14:01:40Z, within the slack
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertIn("process_set_changed", kinds(report, 2))
        # A damaged capture whose time is unknown is checked against the reference wherever it lies.
        s = Scenario()
        s.dir_snapshots = [("20261108T135000Z", None, extra)]
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["process_changed_in_damaged_capture"])
        s.dir_snapshots = [("20261108T135000Z", None, [])]  # nothing changed: only listed
        code, report, text = run(s)
        self.assertEqual(code, 0, text)
        skipped = report["conditions"][2]["info"]["process_captures_skipped"]
        self.assertEqual([c["capture"] for c in skipped], ["20261108T135000Z"])

    def test_s16_malformed_records_never_establish_boundary_coverage(self):
        s = Scenario()
        s.dns_line_edits[0] = lambda raw: raw.split(",", 1)[0]  # 13:56:40Z round reduced to its timestamp
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, {0, 1, 5}, "CANNOT-EVALUATE")
        self.assertIn("boundary_not_covered", kinds(report, 1, "cannot_evaluate"))
        s = Scenario()
        s.phone.remove(int(START))
        for epoch in (int(START) - 120, int(START) - 60):  # schema-invalid exterior samples
            s.overrides[epoch] = lambda row: row.pop("services")
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, {0, 1, 3, 4}, "CANNOT-EVALUATE")
        self.assertIn("boundary_not_covered", kinds(report, 1, "cannot_evaluate"))


    # Review round 4 (S15, S17, S18).

    def test_s15_captures_tied_at_the_end_anchor_time_are_all_checked(self):
        changed = {pid: 1000 * 100 for pid, _ in PROCS}
        changed[14724] = ticks(END - 100)  # crond restarted, visible only in the second capture at END
        s = Scenario()
        s.dir_snapshots = [("20261108T142900Z", END, []),  # unchanged, captured exactly at END
                           ("20261108T143000Z", END, [], changed)]  # same capture time, changed row
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["process_set_changed"])
        span = [c["capture"] for c in report["conditions"][2]["info"]["process_span"]]
        self.assertEqual(span, ["20261108T135900Z", "20261108T142900Z", "20261108T143000Z"])

    def test_s17_damaged_capture_with_another_boot_id_fails(self):
        other = "00000000-0000-0000-0000-000000000000"
        s = Scenario()
        s.extra_snapshots = [(START + 900, dict(s.procs_final), other, ["garbage"])]  # rows unchanged
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 2, "FAIL")
        self.assertEqual(kinds(report, 2), ["boot_id_changed"])

    def test_s18_malformed_record_anywhere_blocks_condition_0(self):
        s = Scenario()  # in-window sample with duplicate keys, appended after the post-window samples
        s.extra_phone_lines.append((len(s.phone), json.dumps(s.sample(START + 330)).replace(
            '"i14-ui": 0', '"i14-ui": 3, "i14-ui": 0')))
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 0, "CANNOT-EVALUATE")
        found = report["conditions"][0]["cannot_evaluate"]
        self.assertEqual([(u["kind"], u["line"]) for u in found], [("phone_malformed_record", len(s.phone) + 1)])
        self.assertTrue(found[0]["file"].endswith("samples.jsonl"))
        s = Scenario()  # in-window DNS round with malformed quotes, after the last (post-window) round
        bad = f'{iso(START + 900)},NOERROR,"1"5,NOERROR,15,NOERROR,15,NOERROR+AD,15,SERVFAIL,15'
        s.dns_line_edits[len(s.dns) - 1] = lambda raw: raw + "\n" + bad
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 0, "CANNOT-EVALUATE")
        self.assertEqual(kinds(report, 0, "cannot_evaluate"), ["dns_malformed_record"])
        s = Scenario()  # junk long before the window (a truncated and a schema-invalid sample)
        s.preamble = True
        code, report, _ = run(s)
        self.assertEqual(code, 1)
        self.assertOnly(report, 0, "CANNOT-EVALUATE")
        self.assertEqual([u["line"] for u in report["conditions"][0]["cannot_evaluate"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
