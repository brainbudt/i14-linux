#!/usr/bin/env python3
"""Independent raw-data audit of acceptance window B (artifacts/soak-rust-20261108/PROTOCOL.md).

Evaluates conditions 0-6 from the raw inputs only; it shares no code with the summary reporters
(scripts/i14-soak-report.py, i14-dns-soak-report.py). Every condition gets PASS, FAIL or
CANNOT-EVALUATE (battery, condition 6, is REPORTED and does not gate). A definite failure beats
an unevaluable part. Exit code 0 only when conditions 0-5 all PASS; 1 otherwise; 2 on usage errors.

Usage:
  i14-window-audit.py --samples samples.jsonl.1 samples.jsonl --dns-csv dns.csv \
      --maintenance maintenance.txt --baseline-start baseline-start-....txt \
      [--baseline-end baseline-end-....txt] --logs logs/ [--json-out report.json]

Time handling: phone epochs, the DNS CSV (a time without zone is UTC, the monitoring host's clock), the
maintenance file and the baselines are UTC. /var/log/messages and i14-battery.log carry the phone's
local time (Europe/Amsterdam, no year); they are converted with --log-tz and the year nearest the
window start. Raw inputs are parsed strictly (duplicate JSON keys, CSV syntax errors and malformed
captures or log lines prevent PASS). Any malformed record anywhere in the phone sample files or dns.csv
(invalid JSON, duplicate keys, schema-invalid, CSV syntax error, wrong field count, unparsable time)
makes condition 0 CANNOT-EVALUATE whatever its position or time; it is listed by file and line.

Process continuity (condition 2) uses one strict rule and no clock mapping of start ticks: the span runs
from the last valid procs.txt capture at or before the window start to the first valid one at or after
the window end, including every capture whose time lies between those two times inclusive, so every
capture tied with either anchor time (directories under logs/ and lines of logs/index.txt), selected
and ordered by the capture time inside procs.txt, not the directory name; a damaged capture whose time
cannot be read is always included and checked. A readable boot id other than the window boot in any
capture of the span, damaged or not, fails. The monitored (role, pid, start ticks) rows and the boot id must be identical in every
valid capture of the span; any difference fails. A process that restarts gets a new pid and a later
start tick, so identical rows in two valid captures prove it ran throughout the time between them; a
missing, empty or failed capture between two valid ones therefore loses no evidence and is only listed.
A damaged capture still counts as evidence of a change when one of its readable rows is not in the
reference. No valid capture on either side makes continuity unevaluable.
"""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

WINDOW_START = "2026-11-08T14:00:00Z"
WINDOW_END = "2026-11-10T14:00:00Z"
WINDOW_BOOT = "a3840ac8-8b15-43c4-a2a4-7b9b1feabccd"
SERVICES = ("i14-power", "i14-cpufreq", "i14-ui", "unbound", "i14-wifi", "i14-netwatch", "sshd")
PHONE_LIMIT = 180  # s: maximum gap and boundary bracket for phone samples
DNS_LIMIT = 450  # s: the same for external DNS rounds
GUARD_MAX_AGE = 150  # s: older BCCT status JSON is stale
CAPTURE_SLACK = (60, 900)  # s: a capture's own time may precede / follow its directory stamp by this much
DNS_HEADER = ["time", "health_udp", "ms", "health_tcp", "ms", "example_udp", "ms", "signed_ad", "ms",
              "bogus", "ms"]
DNS_WANT = {"health_udp": "NOERROR", "health_tcp": "NOERROR", "example_udp": "NOERROR",
            "signed_ad": "NOERROR+AD", "bogus": "SERVFAIL"}
DNS_RESULT = re.compile(r"(?:(?:NOERROR|SERVFAIL|NXDOMAIN|REFUSED|\d+)(?:\+AD)?|ERROR:\w+)")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BASELINE_REQUIRED = ("/mnt/data/media/0/i14-linux/ui.json", "/mnt/data/media/0/i14-linux/soak/i14-soak-sample",
                     "/mnt/data/media/0/i14-linux/soak/i14-soak-sample-rs", "/lib/apk/db/installed",
                     "/etc/conf.d/syslog")
SUPERVISED = ("unbound", "i14-cpufreq", "i14-wifi", "i14-netwatch", "i14-power", "i14-ui")
LONG_LIVED = {"unbound", "wpa_supplicant", "chronyd", "crond", "syslogd", "i14-cpufreq", "i14-netwatch",
              "i14-battery-guard", "i14-ui"}
REQUIRED_ROLES = tuple(f"supervise-daemon:{s}" for s in SUPERVISED) + (
    "unbound", "wpa_supplicant", "i14-cpufreq", "i14-netwatch", "i14-battery-guard", "i14-ui",
    "chronyd", "crond", "syslogd", "sshd-listener")
# Lines that announce a (re)start, stop, crash or respawn of a daemon. Deliberately broad: a match inside
# the window fails condition 2 and is listed verbatim for the reader. supervise-daemon lines count by
# their lifecycle words (child exited/terminated, respawning, command line of a new child, a real
# signal); its "Sending signal 0" existence probe is not a lifecycle event.
RESTART_LINE = re.compile(
    r"respawn|restart|start-stop-daemon|\bstart(?:ed|ing)\b|\bstopp(?:ed|ing)\b|"
    r"\bexit(?:ed|ing)\b|\bterminat|\bkilled\b|crash|healthcheck|too many times|Child command line|"
    r"Supervisor command line|Sending signal [1-9]|stopping a boot service|Server listening on|"
    r"Successfully initialized wpa_supplicant|chronyd version|CPU policy (?:started|stopped)",
    re.IGNORECASE)
SYSLOG_LINE = re.compile(r"([A-Z][a-z]{2}) ([ \d]\d) (\d\d):(\d\d):(\d\d) (\S+) (\S+) (\S+?)(?:\[\d+\])?:? (.*)")
SYSLOG_STAMP = re.compile(r"([A-Z][a-z]{2}) ([ \d]\d) (\d\d):(\d\d):(\d\d)(?:\s|$)")  # prefix of a damaged line
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                                      "Oct", "Nov", "Dec"), 1)}
BATTERY_LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)")
MESSAGES_FILE = re.compile(r"messages(?:\.\d+)?")
STAMP = re.compile(r"\d{8}T\d{6}Z")  # capture directory / index.txt stamp
INDEX_LINE = re.compile(r"(\S+) ok:(.*?)(?: absent:(.*?))? failed:(.*)")
BCCT00 = re.compile(r"mtk-cl-bcct00 (-?\d+) mA, state ([01])")
HUMAN_LIMIT = 15  # offending items printed per list in the human summary (the JSON has all)


# ---------------------------------------------------------------- helpers

def parse_utc(text):
    """ISO 8601 time -> epoch seconds; a time without zone is UTC."""
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def iso(epoch):
    if epoch is None:
        return None
    value = datetime.fromtimestamp(epoch, timezone.utc)
    spec = "seconds" if float(epoch).is_integer() else "milliseconds"
    return value.isoformat(timespec=spec).replace("+00:00", "Z")


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def unique_keys(pairs):
    """object_pairs_hook: a repeated key at any nesting level makes the JSON invalid."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def overlaps(a, b, start, end):
    return a <= end and b >= start


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Condition:
    def __init__(self, number, title, gating=True):
        self.number, self.title, self.gating = number, title, gating
        self.failures, self.unknowns, self.info = [], [], {}

    def fail(self, kind, **detail):
        self.failures.append({"kind": kind, **detail})

    def unknown(self, kind, **detail):
        self.unknowns.append({"kind": kind, **detail})

    @property
    def status(self):
        if not self.gating:
            return "REPORTED"
        return "FAIL" if self.failures else "CANNOT-EVALUATE" if self.unknowns else "PASS"

    def as_dict(self):
        return {"condition": self.number, "title": self.title, "status": self.status, "gating": self.gating,
                "failures": self.failures, "cannot_evaluate": self.unknowns, "info": self.info}


# ---------------------------------------------------------------- time series (phone and DNS)

class Record:
    """One raw input line. time is None when the line has no usable timestamp."""

    def __init__(self, source, line, raw):
        self.source, self.line, self.raw = source, line, raw
        self.time, self.row, self.problems = None, None, []

    def ref(self, **extra):
        return {"file": self.source, "line": self.line, "time": iso(self.time), **extra}

    def raw_ref(self):
        return self.ref(raw=self.raw[:300])


def malformed_records(records, condition0, label):
    """Global rule: a malformed record anywhere in the file blocks condition 0, whatever its time."""
    for record in records:
        if record.problems:
            condition0.unknown(f"{label}_malformed_record", **record.raw_ref(), problems=list(record.problems))


def series_checks(records, start, end, condition0, label):
    """Chronological order and uniqueness among the lines that touch the window (file order)."""
    timed = [r for r in records if r.time is not None]
    problems = []
    for before, after in zip(timed, timed[1:]):
        if not overlaps(min(before.time, after.time), max(before.time, after.time), start, end):
            continue
        if after.time == before.time:
            problems.append(("duplicate_timestamp", before, after))
        elif after.time < before.time:
            problems.append(("out_of_order", before, after))
    reported = {(id(a), id(b)) for _, a, b in problems}
    seen = {}
    for record in timed:
        if start <= record.time <= end:
            if record.time in seen and (id(seen[record.time]), id(record)) not in reported:
                problems.append(("duplicate_timestamp", seen[record.time], record))
            seen.setdefault(record.time, record)
    for kind, a, b in problems:
        condition0.fail(f"{label}_{kind}", first=a.ref(), second=b.ref())
    return problems


def valid_evidence(record):
    """Only a record that passes full schema validation can establish boundary coverage."""
    return record.time is not None and record.row is not None and not record.problems


def brackets(records, boundary):
    times = sorted({r.time for r in records if valid_evidence(r)})
    exact = boundary in times
    before = max((t for t in times if t < boundary), default=None)
    after = min((t for t in times if t > boundary), default=None)
    return exact, before, after


def boundary_check(condition, records, boundary, limit, label, name):
    exact, before, after = brackets(records, boundary)
    entry = {"series": label, "boundary": name, "at": iso(boundary), "exact": exact,
             "before": iso(before), "after": iso(after),
             "separation_seconds": after - before if before is not None and after is not None else None,
             "limit_seconds": limit}
    condition.info.setdefault("boundaries", []).append(entry)
    if exact:
        return
    if before is None or after is None:
        condition.unknown("boundary_not_covered", **entry,
                          reason="no data " + ("before" if before is None else "after") + " the boundary")
    elif after - before > limit:
        condition.fail("boundary_bracket_too_wide", **entry)


def window_sequence(records, start, end):
    """In-window timed records in time order plus the nearest valid records outside on each side
    (a malformed exterior record never brackets a boundary)."""
    timed = sorted((r for r in records if r.time is not None), key=lambda r: (r.time, r.line))
    inside = [r for r in timed if start <= r.time <= end]
    before = [r for r in timed if r.time < start and valid_evidence(r)]
    after = [r for r in timed if r.time > end and valid_evidence(r)]
    return (before[-1] if before else None), inside, (after[0] if after else None)


def gap_checks(condition, records, start, end, limit, label, uptime=False):
    before, inside, after = window_sequence(records, start, end)
    # An exterior record is used only when its boundary has no exact sample and needs bracketing.
    need_before = not inside or inside[0].time > start
    need_after = not inside or inside[-1].time < end
    sequence = ([before] if before and need_before else []) + inside + ([after] if after and need_after else [])
    gaps = []
    for a, b in zip(sequence, sequence[1:]):
        wall = b.time - a.time
        elapsed = None
        if uptime and a.row and b.row and a.row.get("boot_id") == b.row.get("boot_id"):
            ua, ub = a.row.get("uptime_seconds"), b.row.get("uptime_seconds")
            if is_number(ua) and is_number(ub):
                elapsed = ub - ua
                if elapsed <= 0:
                    condition.fail(f"{label}_uptime_not_increasing", first=a.ref(), second=b.ref(),
                                   uptime_delta=elapsed)
                elif abs(wall - elapsed) > 5:
                    condition.info.setdefault("clock_steps", []).append(
                        {"from": a.ref(), "to": b.ref(), "wall_seconds": wall, "uptime_seconds": elapsed})
        if uptime and a.row and b.row and a.row.get("boot_id") != b.row.get("boot_id"):
            condition.fail(f"{label}_boot_changed", first=a.ref(boot_id=a.row.get("boot_id")),
                           second=b.ref(boot_id=b.row.get("boot_id")))
        span = max(wall, elapsed or 0)
        if span > limit:
            gaps.append({"from": a.ref(), "to": b.ref(), "seconds": span, "limit_seconds": limit})
    for gap in gaps:
        condition.fail(f"{label}_gap", **gap)
    if need_before and before is None:
        condition.unknown(f"{label}_no_data_before_window",
                          first=inside[0].ref() if inside else None)
    if need_after and after is None:
        condition.unknown(f"{label}_data_does_not_reach_window_end",
                          last=inside[-1].ref() if inside else None, window_end=iso(end))
    condition.info[f"{label}_rows_in_window"] = len(inside)
    return inside


# ---------------------------------------------------------------- phone samples

def load_samples(paths, inputs):
    records = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            inputs["errors"].append({"input": str(path), "error": str(error)})
            continue
        for number, raw in enumerate(text.splitlines(), 1):
            record = Record(str(path), number, raw)
            try:
                row = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=reject_constant)
            except ValueError as error:
                record.problems.append(f"not valid JSON: {error}")
                records.append(record)
                continue
            if not isinstance(row, dict):
                record.problems.append("not a JSON object")
            else:
                record.row = row
                epoch = row.get("epoch")
                if is_number(epoch) and 0 <= epoch <= 253402300799:
                    record.time = float(epoch)
                else:
                    record.problems.append(f"epoch missing or invalid: {epoch!r}")
            records.append(record)
    return records


def reject_constant(name):
    """NaN, Infinity and -Infinity are not JSON; Python accepts them unless told otherwise."""
    raise ValueError(f"non-standard JSON constant {name}")


def sample_problems(row):
    """Schema problems of a parsed sample (another boot id is not a schema problem)."""
    problems = []
    epoch = row.get("epoch")
    stamp = row.get("iso")
    try:
        # The shell sampler reads the clock twice (epoch, then iso), so iso may trail epoch by
        # one second (41 such rows from 5-7 November); never more, and never ahead.
        if not isinstance(stamp, str) or not 0 <= parse_utc(stamp) - epoch <= 1:
            problems.append(f"iso {stamp!r} missing or does not match epoch")
    except (ValueError, TypeError):
        problems.append(f"iso {stamp!r} unparseable")
    if not isinstance(row.get("boot_id"), str) or not UUID.fullmatch(row["boot_id"]):
        problems.append(f"boot_id {row.get('boot_id')!r} malformed")
    uptime = row.get("uptime_seconds")
    if not is_number(uptime) or uptime < 0:
        problems.append(f"uptime_seconds {uptime!r} invalid")
    services = row.get("services")
    if not isinstance(services, dict):
        problems.append("services missing or not an object")
    else:
        for name in SERVICES:
            if type(services.get(name)) is not int:
                problems.append(f"services.{name} {services.get(name)!r} is not an integer")
    dns = row.get("dns")
    if not isinstance(dns, dict) or type(dns.get("ok")) is not bool:
        problems.append("dns.ok missing or not a boolean")
    return problems


def audit_phone(records, start, end, boot_id, c0, c1, c3, c4):
    for record in records:
        if record.row is not None and record.time is not None:
            record.problems.extend(sample_problems(record.row))
    malformed_records(records, c0, "phone")
    for record in records:  # valid, but from another boot: never evidence for this window
        boot = (record.row or {}).get("boot_id")
        if not record.problems and record.row is not None and boot != boot_id:
            record.problems.append(f"boot_id {boot} is not the window boot {boot_id}")
    series = series_checks(records, start, end, c0, "phone")
    for kind, a, b in series:
        c3.fail(f"phone_{kind}", first=a.ref(), second=b.ref())
    boundary_check(c1, records, start, PHONE_LIMIT, "phone", "start")
    boundary_check(c1, records, end, PHONE_LIMIT, "phone", "end")
    inside = gap_checks(c3, records, start, end, PHONE_LIMIT, "phone", uptime=True)
    for record in inside:
        if record.problems:
            c3.fail("phone_invalid_row", **record.ref(), problems=record.problems)
        row = record.row
        services = row.get("services") if isinstance(row.get("services"), dict) else {}
        bad = {name: services.get(name) for name in SERVICES
               if type(services.get(name)) is int and services[name] != 0}
        if bad:
            c3.fail("service_status_nonzero", **record.ref(), statuses=bad)
        dns = row.get("dns")
        if isinstance(dns, dict) and dns.get("ok") is True:
            continue
        if isinstance(dns, dict) and dns.get("ok") is False:
            c4.fail("local_dns_failed", **record.ref(), dns=dns)
        else:
            c4.fail("local_dns_missing_or_malformed", **record.ref(), dns=dns)
    c4.info["samples_in_window"] = len(inside)
    for item in c3.unknowns:  # "every sample" needs samples spanning the whole window
        if item["kind"] in ("phone_no_data_before_window", "phone_data_does_not_reach_window_end"):
            c4.unknown(item["kind"], **{k: v for k, v in item.items() if k != "kind"})
    if inside:
        last = inside[-1]
        c3.info["final_sample"] = last.ref(services=(last.row or {}).get("services"))
        c4.info["final_sample"] = last.ref(dns=(last.row or {}).get("dns"))
    else:
        c3.unknown("phone_no_samples_in_window")
        c4.unknown("phone_no_samples_in_window")
    return inside


# ---------------------------------------------------------------- external DNS

def csv_fields(line):
    """One CSV line, parsed strictly: a quote must enclose a whole field."""
    return next(csv.reader([line], strict=True), [])


def load_dns(path, inputs, c0):
    records, header_ok = [], False
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError as error:
        inputs["errors"].append({"input": str(path), "error": str(error)})
        return records, header_ok
    if lines[-1] == "":
        lines.pop()
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    try:
        header = csv_fields(lines[0]) if lines else None
    except csv.Error:
        header = lines[0]
    if header != DNS_HEADER:
        c0.fail("dns_header_mismatch", header=header, expected=DNS_HEADER)
        return records, header_ok
    header_ok = True
    for number, line in enumerate(lines[1:], 2):
        record = Record(str(path), number, line)
        try:
            fields = csv_fields(line)
        except csv.Error as error:
            record.problems.append(f"CSV syntax error: {error}")
            records.append(record)
            continue
        try:
            record.time = parse_utc(fields[0])
        except (ValueError, IndexError):
            record.problems.append(f"time {fields[0] if fields else ''!r} unparseable")
            records.append(record)
            continue
        if len(fields) != len(DNS_HEADER):
            record.problems.append(f"{len(fields)} fields, expected {len(DNS_HEADER)}")
        else:
            row = {}
            for index in range(1, len(DNS_HEADER), 2):
                name, result, ms = DNS_HEADER[index], fields[index], fields[index + 1]
                if not DNS_RESULT.fullmatch(result):
                    record.problems.append(f"{name} result {result!r} unknown")
                if not re.fullmatch(r"\d+", ms):
                    record.problems.append(f"{name} ms {ms!r} malformed")
                row[name] = result
            record.row = row
        records.append(record)
    return records, header_ok


def audit_dns(records, header_ok, start, end, c0, c1, c5):
    if not header_ok:
        c5.unknown("dns_input_unusable")
        c1.unknown("dns_input_unusable")
        return
    malformed_records(records, c0, "dns")
    for kind, a, b in series_checks(records, start, end, c0, "dns"):
        c5.fail(f"dns_{kind}", first=a.ref(), second=b.ref())
    boundary_check(c1, records, start, DNS_LIMIT, "dns", "start")
    boundary_check(c1, records, end, DNS_LIMIT, "dns", "end")
    inside = gap_checks(c5, records, start, end, DNS_LIMIT, "dns")
    for record in inside:
        if record.problems:
            c5.fail("dns_invalid_round", **record.raw_ref(), problems=record.problems)
            continue
        wrong = {k: {"got": record.row.get(k), "want": v} for k, v in DNS_WANT.items() if record.row.get(k) != v}
        if wrong:
            c5.fail("dns_wrong_result", **record.ref(), results=wrong, raw=record.raw)
    if not inside:
        c5.unknown("dns_no_rounds_in_window")


# ---------------------------------------------------------------- condition 2: version and restarts

def parse_baseline(path):
    """-> (raw header dict, anchor {"utc": epoch, "uptime": s} (None when invalid), chrony_dir,
    {path: hash}, [problems])."""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    header, anchor, chrony, hashes, problems = {}, {"utc": None, "uptime": None}, None, {}, []
    if not lines or not lines[0].startswith("# "):
        return header, anchor, chrony, hashes, ["missing capture header"]
    for token in lines[0][2:].split():
        key, sep, value = token.partition("=")
        if not sep:
            problems.append(f"header token {token!r} malformed")
        if key in header:
            problems.append(f"header key {key!r} repeated")
        header[key] = value
    for key in ("utc", "boot_id", "kernel", "alpine", "uptime"):
        if key not in header:
            problems.append(f"header lacks {key}")
        elif not header[key]:
            problems.append(f"header {key} is empty")
    try:
        anchor["utc"] = parse_utc(header.get("utc", ""))
    except ValueError:
        problems.append("header utc unparseable")
    try:
        uptime = float(header.get("uptime", ""))
        if not math.isfinite(uptime) or uptime < 0:
            raise ValueError
        anchor["uptime"] = uptime
    except ValueError:
        problems.append(f"header uptime {header.get('uptime')!r} is not a finite nonnegative number")
    for number, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        match = re.fullmatch(r"# chrony_dir=([^:\s]+:[^:\s]+:[0-7]{3,4})", line)
        if match:
            if chrony is not None:
                problems.append(f"line {number}: second chrony_dir")
            chrony = match[1]
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (/\S.*)", line)
        if not match:
            problems.append(f"line {number}: unrecognised {line[:120]!r}")
        elif match[2] in hashes:
            problems.append(f"line {number}: duplicate path {match[2]}")
        else:
            hashes[match[2]] = match[1]
    if chrony is None:
        problems.append("chrony_dir missing")
    return header, anchor, chrony, hashes, problems


def audit_baselines(start_path, end_path, start, end, boot_id, c2):
    try:
        h0, a0, chrony0, hash0, p0 = parse_baseline(start_path)
    except OSError as error:
        c2.unknown("baseline_start_unreadable", error=str(error))
        return
    for problem in p0:
        c2.unknown("baseline_start_malformed", problem=problem)
    c2.info["baseline_start"] = {"utc": h0.get("utc"), "boot_id": h0.get("boot_id"), "paths": len(hash0),
                                 "chrony_dir": chrony0}
    if h0.get("boot_id") != boot_id:
        c2.fail("boot_id_changed", where="start baseline", got=h0.get("boot_id"), want=boot_id)
    if a0["utc"] is not None and not start - 3600 <= a0["utc"] <= start + 300:
        c2.unknown("baseline_start_not_at_window_start", utc=h0.get("utc"))
    for required in BASELINE_REQUIRED:
        if required not in hash0:
            c2.unknown("baseline_lacks_required_path", path=required)
    if end_path is None:
        c2.unknown("baseline_end_missing", reason="no end baseline given (window not finished?)")
        return
    try:
        h1, a1, chrony1, hash1, p1 = parse_baseline(end_path)
    except OSError as error:
        c2.unknown("baseline_end_unreadable", error=str(error))
        return
    for problem in p1:
        c2.unknown("baseline_end_malformed", problem=problem)
    c2.info["baseline_end"] = {"utc": h1.get("utc"), "boot_id": h1.get("boot_id"), "paths": len(hash1),
                               "chrony_dir": chrony1}
    if a1["utc"] is not None and a1["utc"] < end:
        c2.unknown("baseline_end_before_window_end", utc=h1.get("utc"))
    for key in sorted(set(h0) | set(h1)):
        if key in ("utc", "uptime"):  # the only header fields allowed to differ
            continue
        if h0.get(key) != h1.get(key):
            kind = "boot_id_changed" if key == "boot_id" else "baseline_header_changed"
            c2.fail(kind, key=key, start=h0.get(key), end=h1.get(key))
    if chrony0 != chrony1:
        c2.fail("chrony_dir_changed", start=chrony0, end=chrony1)
    for path in sorted(set(hash0) | set(hash1)):
        if path not in hash1:
            c2.fail("baseline_path_missing_at_end", path=path, start=hash0[path])
        elif path not in hash0:
            c2.fail("baseline_path_added_at_end", path=path, end=hash1[path])
        elif hash0[path] != hash1[path]:
            c2.fail("baseline_hash_changed", path=path, start=hash0[path], end=hash1[path])


def audit_maintenance(path, start, end, c2):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        c2.unknown("maintenance_unreadable", error=str(error))
        return
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(None, 2)
        try:
            a, b = parse_utc(parts[0]), parse_utc(parts[1])
            if b < a:
                raise ValueError("end before start")
        except (ValueError, IndexError) as error:
            c2.unknown("maintenance_line_malformed", line=number, text=line, error=str(error))
            continue
        if overlaps(a, b, start, end):
            c2.fail("maintenance_inside_window", line=number, text=line, start=iso(a), end=iso(b))


def process_role(command):
    """-> role name, or None for excluded per-connection/collector/unknown processes."""
    command = command.strip()
    if command.startswith("sshd-session") or re.match(r"sshd: \S+@|sshd: .*\[priv\]", command):
        return None
    match = re.match(r"supervise-daemon (\S+)", command)
    if match:
        return f"supervise-daemon:{match[1]}"
    if re.match(r"sshd: \S*sshd \[listener\]", command):
        return "sshd-listener"
    base = command.split()[0].rsplit("/", 1)[-1] if command else ""
    if base == "sshd":
        return "sshd-listener"
    return base if base in LONG_LIVED else None


def parse_procs(path):
    """-> {"time", "boot_id", "rows": {(role, pid, ticks)} of monitored processes, "roles", "problems"}."""
    snap = {"time": None, "boot_id": None, "rows": set(), "roles": set(), "problems": []}
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        snap["problems"].append(str(error))
        return snap
    if lines and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", lines[0]):
        try:
            snap["time"] = parse_utc(lines[0])
        except ValueError:
            pass
    if snap["time"] is None:
        snap["problems"].append(f"capture time {lines[0] if lines else None!r} malformed")
    if len(lines) > 1 and UUID.fullmatch(lines[1]):
        snap["boot_id"] = lines[1]
    else:
        snap["problems"].append(f"boot id {lines[1] if len(lines) > 1 else None!r} malformed")
    pids, count = set(), 0
    for line in lines[2:]:
        match = re.fullmatch(r"(\d+) (\d+) (.*)", line)
        if not match:
            snap["problems"].append(f"process line {line[:120]!r} malformed")
            continue
        count += 1
        pid, ticks, command = int(match[1]), int(match[2]), match[3].strip()
        if pid <= 0 or pid in pids:
            snap["problems"].append(f"pid {pid} {'repeated' if pid in pids else 'invalid'}")
        pids.add(pid)
        role = process_role(command)
        if role is not None:
            snap["rows"].add((role, pid, ticks))
            snap["roles"].add(role)
    if count < 10:
        snap["problems"].append("fewer than ten processes (incomplete capture)")
    return snap


def stamp_time(stamp):
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def load_index(logs, c2):
    """logs/index.txt -> {stamp: [lines]}, or None when it cannot be read."""
    try:
        text = (logs / "index.txt").read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        c2.unknown("capture_index_unreadable", error=str(error))
        return None
    entries = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        stamp = line.split(" ", 1)[0]
        if not STAMP.fullmatch(stamp) or stamp_time(stamp) is None:
            c2.unknown("capture_index_line_malformed", line=number, text=line[:300])
            continue
        entries.setdefault(stamp, []).append(line)
    return entries


def index_problem(lines):
    """None when index.txt records exactly one successful procs capture for the stamp, else the reason."""
    if not lines:
        return "not recorded in logs/index.txt"
    if len(lines) > 1:
        return "recorded more than once in logs/index.txt"
    match = INDEX_LINE.fullmatch(lines[0].rstrip())
    if not match:
        return f"logs/index.txt line malformed: {lines[0][:200]!r}"
    failed = [token for token in match[4].split() if token.startswith("procs")]
    if failed:
        return "capture failed per logs/index.txt: " + " ".join(failed)
    if "procs" not in match[2].split():
        return "procs not listed as copied in logs/index.txt"
    return None


def load_captures(logs, c2):
    """Every capture (directory under logs/ or index.txt stamp) in stamp order with its problems."""
    index = load_index(logs, c2)
    stamps = {p.name for p in logs.iterdir() if p.is_dir() and STAMP.fullmatch(p.name)} | set(index or {})
    captures = []
    for stamp in sorted(stamps):
        stamped = stamp_time(stamp)
        path = logs / stamp / "procs.txt"
        capture = {"stamp": stamp, "file": str(path), "time": None, "boot_id": None, "rows": set(),
                   "roles": set(), "problems": []}
        if stamped is None:
            capture["problems"].append("directory stamp is not a valid time")
        if not path.is_file():
            capture["problems"].append("procs.txt missing")
        elif path.stat().st_size == 0:
            capture["problems"].append("procs.txt empty")
        else:
            snap = parse_procs(path)
            capture.update(snap)
            if None not in (snap["time"], stamped) and not (
                    stamped - CAPTURE_SLACK[0] <= snap["time"] <= stamped + CAPTURE_SLACK[1]):
                capture["problems"].append(f"capture time {iso(snap['time'])} does not match its directory")
        reason = "logs/index.txt unreadable" if index is None else index_problem(index.get(stamp, []))
        if reason:
            capture["problems"].append(reason)
        captures.append(capture)
    return captures


def audit_procs(logs, start, end, boot_id, c2):
    """Condition 2 process continuity under the strict span rule (see the module docstring)."""
    loaded = load_captures(logs, c2)
    # The span is selected and ordered by the time inside procs.txt, not by directory name. A damaged
    # capture without a readable time may lie anywhere, so it is always checked (conservatively).
    captures = sorted((c for c in loaded if c["time"] is not None), key=lambda c: (c["time"], c["stamp"]))
    untimed = [c for c in loaded if c["time"] is None]
    valid = [not c["problems"] for c in captures]
    ref = max((i for i, c in enumerate(captures) if valid[i] and c["time"] <= start), default=None)
    final = next((i for i, c in enumerate(captures) if valid[i] and c["time"] >= end), None)
    # Inclusive anchor times: every capture tied with either anchor's time is in the span.
    low_time = captures[ref]["time"] if ref is not None else start
    high_time = captures[final]["time"] if final is not None else math.inf
    span = [c for c in captures if low_time <= c["time"] <= high_time] + untimed
    c2.info["process_span"] = [{"capture": c["stamp"], "time": iso(c["time"]), "boot_id": c["boot_id"],
                                "problems": c["problems"]} for c in span]
    good = [c for c in span if not c["problems"]]
    reference_rows = good[0]["rows"] if good else set()
    skipped = []
    for capture in span:
        if not capture["problems"]:
            continue
        # Bracketed by valid captures it adds nothing; but a readable changed row is a restart.
        skipped.append({"capture": capture["stamp"], "problems": capture["problems"]})
        changed = sorted(capture["rows"] - reference_rows) if good else []
        if changed:
            c2.fail("process_changed_in_damaged_capture", capture=capture["stamp"], file=capture["file"],
                    rows=changed, problems=capture["problems"])
    c2.info["process_captures_skipped"] = skipped
    for capture in span:  # a readable boot id in a damaged capture is boot evidence too
        if capture["boot_id"] is not None and capture["boot_id"] != boot_id:
            c2.fail("boot_id_changed", where=capture["file"], got=capture["boot_id"], want=boot_id)
    for before, after in zip(good, good[1:]):
        if before["rows"] != after["rows"]:
            # Any change in the span fails (strict). Between captures that straddle a boundary it may
            # have happened just outside the window; the report says so instead of hiding it.
            c2.fail("process_set_changed", snapshot_before=before["file"], snapshot_after=after["file"],
                    captured_before=iso(before["time"]), captured_after=iso(after["time"]),
                    may_be_outside_window=not (start <= before["time"] and after["time"] <= end),
                    removed=sorted(before["rows"] - after["rows"]), added=sorted(after["rows"] - before["rows"]))
    if good:
        for role in REQUIRED_ROLES:
            if role not in good[0]["roles"]:
                c2.fail("monitored_process_absent", role=role, snapshot=good[0]["file"])
    if ref is None:
        c2.unknown("restart_continuity_unevaluable", reason="no valid procs.txt capture at or before the window start")
    else:
        c2.info["process_reference"] = captures[ref]["file"]
    if final is None:
        latest = max((c["time"] for c, ok in zip(captures, valid) if ok), default=None)
        c2.unknown("restart_continuity_unevaluable", reason="no valid procs.txt capture at or after the window end",
                   latest_valid=iso(latest))
    else:
        c2.info["process_final"] = captures[final]["file"]


def local_to_utc(naive, zone):
    return naive.replace(tzinfo=zone).astimezone(timezone.utc).timestamp()


def syslog_time(match, zone, start):
    """Syslog 'Mon dd HH:MM:SS' in local time -> UTC epoch; the year nearest the window start."""
    year0 = datetime.fromtimestamp(start, timezone.utc).year
    best = None
    for year in (year0 - 1, year0, year0 + 1):
        try:
            when = local_to_utc(datetime(year, MONTHS[match[1]], int(match[2]), int(match[3]), int(match[4]),
                                         int(match[5])), zone)
        except (ValueError, KeyError):
            return None
        if best is None or abs(when - start) < abs(best - start):
            best = when
    return best


def audit_messages(logs, start, end, zone, max_gap, c2):
    """Merged syslog copies: a malformed or truncated line that may lie inside the window makes the logs
    unevaluable (no recovery from other copies); coverage, gaps and lifecycle lines as in the protocol.
    A damaged line is placed by its own timestamp prefix only, never by its neighbours: it is ignored
    only when that prefix parses to a time more than max_gap outside the window."""
    copies = []  # (file, [(line number, text, time or None, tag, truncated)]) in file order
    for path in sorted(logs.glob("*/messages*")):
        if not MESSAGES_FILE.fullmatch(path.name) or path.stat().st_size == 0:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        unterminated = lines[-1] != ""  # the last line has no newline: cut off
        if not unterminated:
            lines.pop()
        parsed = []
        for number, line in enumerate(lines, 1):
            match = SYSLOG_LINE.fullmatch(line)
            when = syslog_time(match, zone, start) if match else None
            truncated = unterminated and number == len(lines)
            parsed.append((number, line, when, match[8] if match and when is not None else None, truncated))
        copies.append((str(path), parsed))
    c2.info["messages_files"] = len(copies)
    entries = sorted({(when, line, tag) for _, parsed in copies for _, line, when, tag, cut in parsed
                      if when is not None and not cut})
    c2.info["messages_lines"] = len(entries)
    bad_lines = 0
    for name, parsed in copies:
        for number, line, when, _, cut in parsed:
            if when is not None and not cut:
                continue
            bad_lines += 1
            if when is None:
                prefix = SYSLOG_STAMP.match(line)
                when = syslog_time(prefix, zone, start) if prefix else None
            if when is None or overlaps(when - max_gap, when + max_gap, start, end):
                c2.unknown("log_line_malformed", file=name, line=number, text=line[:300], truncated=cut,
                           line_time=iso(when))
    if bad_lines:
        c2.info["messages_malformed_lines"] = bad_lines
    if not entries:
        c2.unknown("log_coverage_short", reason="no messages lines")
        return
    first, last = entries[0][0], entries[-1][0]
    c2.info["messages_coverage"] = {"first": iso(first), "last": iso(last)}
    if first > start:
        c2.unknown("log_coverage_short", side="start", first=iso(first), window_start=iso(start))
    if last < end:
        c2.unknown("log_coverage_short", side="end", last=iso(last), window_end=iso(end))
    for (a, _, _), (b, line, _) in zip(entries, entries[1:]):
        if b - a > max_gap and overlaps(a, b, start, end):
            c2.unknown("log_gap", start=iso(a), end=iso(b), seconds=b - a, limit_seconds=max_gap)
    for when, line, tag in entries:
        if start <= when <= end and tag != "sshd-session" and RESTART_LINE.search(line):
            c2.fail("restart_line_in_logs", time=iso(when), text=line)


def audit_index(logs, start, end, c2):
    """Informational: index.txt lines inside the window that record any capture failure."""
    try:
        text = (logs / "index.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        c2.info["capture_index"] = "absent"
        return
    failed = []
    for line in text.splitlines():
        when = stamp_time(line.split(" ", 1)[0])
        match = re.search(r"failed:(.*)$", line)
        if when is not None and start <= when <= end and match and match[1].strip() != "none":
            failed.append(line)
    c2.info["capture_failures_in_window"] = failed


# ---------------------------------------------------------------- condition 6: battery (reported)

def guard_state(row):
    """-> (status dict or None, problem or None). Every field is type-checked; nothing raises."""
    raw = row.get("charge_status")
    if not isinstance(raw, str):
        return None, f"charge_status {type(raw).__name__} is not a JSON string"
    try:
        status = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=reject_constant)
    except ValueError:
        return None, "charge_status not JSON"
    if not isinstance(status, dict):
        return None, "charge_status not an object"
    if status.get("schema") != 1 or status.get("backend") != "bcct":
        return None, "unexpected schema/backend"
    if status.get("boot_id") != row.get("boot_id"):
        return None, "status from another boot"
    if status.get("error") is not None:
        return status, f"guard error {status.get('error')!r}"
    if status.get("valid") is not True:
        return status, "guard reading not valid"
    stamp, uptime = status.get("updated_monotonic"), row.get("uptime_seconds")
    if not is_number(stamp) or not is_number(uptime):
        return status, "status age unknown"
    if not 0 <= uptime - stamp <= GUARD_MAX_AGE:
        return status, f"stale: {uptime - stamp:.0f} s old"
    if type(status.get("paused")) is not bool:
        return status, "paused not boolean"
    for key in ("raw_level", "zcv_mv"):
        if key in status and status[key] is not None and not is_number(status[key]):
            return status, f"{key} {status[key]!r} is not a number"
    return status, None


def bcct00_state(row):
    """-> ("0"/"1", None) from the bcct_state readback, or (None, problem)."""
    raw = row.get("bcct_state")
    if not isinstance(raw, str):
        return None, f"bcct_state {raw!r} is not a string"
    match = BCCT00.search(raw)
    if not match:
        return None, "bcct_state has no mtk-cl-bcct00 line"
    return match[2], None


def audit_battery(samples, logs, start, end, zone, c6):
    invalid, transitions, readback, readback_invalid = [], [], [], []
    previous = previous_readback = None
    for record in samples:
        status, problem = guard_state(record.row or {})
        if problem:
            invalid.append(record.ref(problem=problem))
        elif previous is not None and status["paused"] != previous[1]["paused"]:
            transitions.append({"from_time": iso(previous[0].time), **record.ref(),
                                "paused": [previous[1]["paused"], status["paused"]],
                                "raw_level": status.get("raw_level"), "zcv_mv": status.get("zcv_mv"),
                                "estimate": True})
        if not problem:
            previous = (record, status)
        state, problem = bcct00_state(record.row or {})
        if problem:
            readback_invalid.append(record.ref(problem=problem))
            continue
        if previous_readback is not None and state != previous_readback:
            readback.append(record.ref(bcct00_state=[previous_readback, state]))
        previous_readback = state
    c6.info.update(guard_invalid_or_stale=invalid, bcct_pause_transitions=transitions,
                   bcct_readback_changes=readback, bcct_readback_invalid=readback_invalid)
    log_lines = set()
    for path in sorted(logs.glob("*/i14-battery.log")):
        if path.stat().st_size:
            log_lines.update(path.read_text(encoding="utf-8", errors="replace").splitlines())
    entries, log_invalid, bands, untimed = [], [], [], 0
    for line in log_lines:
        match = BATTERY_LINE.fullmatch(line)
        try:
            when = local_to_utc(datetime.fromisoformat(match[1]), zone) if match else None
        except ValueError:
            when = None
        if when is None:
            untimed += 1
            continue
        if start <= when <= end:
            fields = dict(token.partition("=")[::2] for token in match[2].split() if "=" in token)
            entries.append((when, line, fields))
    entries.sort(key=lambda e: e[0])
    previous = None
    for when, line, fields in entries:
        if fields.get("valid") != "1":
            log_invalid.append({"time": iso(when), "text": line})
        if previous is not None and fields.get("discharge") != previous.get("discharge"):
            bands.append({"time": iso(when), "discharge": [previous.get("discharge"), fields.get("discharge")],
                          "gauge": fields.get("gauge"), "zcv": fields.get("zcv")})
        previous = fields
    c6.info.update(battery_log_lines_in_window=len(entries), battery_log_invalid=log_invalid,
                   battery_log_discharge_transitions=bands, battery_log_untimed_lines=untimed,
                   note="Percentages are uncalibrated estimates; charge band transitions are recorded, not required.")


# ---------------------------------------------------------------- driver

def audit(args):
    start, end = parse_utc(args.start), parse_utc(args.end)
    if end <= start:
        raise ValueError("window end must be after its start")
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(args.log_tz)
    logs = Path(args.logs)
    conditions = [
        Condition(0, "Evaluation method: raw-data schema, order and uniqueness (phone + external DNS)"),
        Condition(1, "Boundaries: exact or bracketing samples (phone <=180 s, DNS <=450 s)"),
        Condition(2, "Version: frozen hashes, chrony dir, boot id, maintenance, restarts, log coverage"),
        Condition(3, "Phone samples: chronological, unique, valid, gaps <=180 s, seven statuses 0"),
        Condition(4, "Local DNS: dns.ok true in every sample"),
        Condition(5, "External DNS: every round valid with five expected results, gaps <=450 s"),
        Condition(6, "Battery: invalid/stale guard readings and BCCT transitions", gating=False),
    ]
    c0, c1, c2, c3, c4, c5, c6 = conditions
    inputs = {"errors": [], "files": {}}
    named = [*args.samples, args.dns_csv, args.maintenance, args.baseline_start] + (
        [args.baseline_end] if args.baseline_end else [])
    explicit = {str(path) for path in named}
    named += sorted(str(p) for p in logs.glob("**/*") if p.is_file()) if logs.is_dir() else []
    for path in named:
        try:
            inputs["files"][str(path)] = {"sha256": sha256(path), "bytes": Path(path).stat().st_size}
        except OSError as error:
            inputs["errors"].append({"input": str(path), "error": str(error)})
    if not logs.is_dir():
        inputs["errors"].append({"input": str(logs), "error": "logs directory missing"})
    # Readers below decode leniently; damaged bytes must not be repaired into valid-looking text.
    # Any invalid UTF-8 in an input that is used (not collector .err files) prevents PASS.
    for path in named:
        # Only the collector's stderr files found under logs/ are unused; explicit inputs are always checked.
        if str(path).endswith(".err") and str(path) not in explicit:
            continue
        try:
            raw = Path(path).read_bytes()
        except OSError:
            continue
        for number, line in enumerate(raw.split(b"\n"), 1):
            try:
                line.decode("utf-8")
            except UnicodeDecodeError as error:
                c0.unknown("input_not_utf8", file=str(path), line=number, error=str(error))
                break

    samples = load_samples(args.samples, inputs)
    inside = audit_phone(samples, start, end, args.boot_id, c0, c1, c3, c4)
    dns, header_ok = load_dns(args.dns_csv, inputs, c0)
    audit_dns(dns, header_ok, start, end, c0, c1, c5)
    audit_baselines(args.baseline_start, args.baseline_end, start, end, args.boot_id, c2)
    audit_maintenance(args.maintenance, start, end, c2)
    if logs.is_dir():
        audit_procs(logs, start, end, args.boot_id, c2)
        audit_messages(logs, start, end, zone, args.max_log_gap, c2)
        audit_index(logs, start, end, c2)
    else:
        c2.unknown("logs_directory_missing", path=str(logs))
    for record in inside:
        if record.row and record.row.get("boot_id") != args.boot_id:
            c2.fail("boot_id_changed", where=record.ref(), got=record.row.get("boot_id"), want=args.boot_id)
    audit_battery(inside, logs, start, end, zone, c6)
    for error in inputs["errors"]:
        c0.unknown("input_unreadable", **error)
    c0.info["note"] = ("Independent of the summary reporters; no failure is excluded as maintenance. "
                       "Preserve the inputs listed under inputs.files (sha256) with this report.")
    passed = all(c.status == "PASS" for c in conditions if c.gating)
    return {"window": {"start": iso(start), "end": iso(end), "boot_id": args.boot_id, "log_tz": args.log_tz},
            "generated": iso(round(datetime.now(timezone.utc).timestamp())),
            "verdict": "PASS" if passed else "NOT PASS",
            "conditions": [c.as_dict() for c in conditions], "inputs": inputs}


def describe(item):
    return ", ".join(f"{k}={json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}"
                     for k, v in item.items())


def summary(report):
    out = [f"Window {report['window']['start']} .. {report['window']['end']} (boot {report['window']['boot_id']})",
           f"Verdict: {report['verdict']}"]
    for c in report["conditions"]:
        out.append(f"[{c['status']}] {c['condition']}. {c['title']}")
        for label, items in (("FAIL", c["failures"]), ("CANNOT-EVALUATE", c["cannot_evaluate"])):
            for item in items[:HUMAN_LIMIT]:
                out.append(f"    {label}: {describe(item)}")
            if len(items) > HUMAN_LIMIT:
                out.append(f"    ... {len(items) - HUMAN_LIMIT} more {label} items (see JSON)")
        info = c["info"]
        if c["condition"] == 1:
            for b in info.get("boundaries", []):
                out.append(f"    {b['series']} {b['boundary']} {b['at']}: exact={b['exact']} before={b['before']} "
                           f"after={b['after']}")
        if c["condition"] == 3 and "final_sample" in info:
            out.append(f"    final sample {info['final_sample']['time']}: {info['final_sample'].get('services')}")
        if c["condition"] == 2:
            for key in ("messages_coverage", "process_reference", "process_final", "capture_failures_in_window"):
                if key in info:
                    out.append(f"    {key}: {info[key]}")
        if c["condition"] == 6:
            out.append(f"    guard invalid/stale readings: {len(info.get('guard_invalid_or_stale', []))}; "
                       f"BCCT pause transitions: {len(info.get('bcct_pause_transitions', []))}; "
                       f"readback changes: {len(info.get('bcct_readback_changes', []))}; "
                       f"readback invalid: {len(info.get('bcct_readback_invalid', []))}; "
                       f"battery-log invalid: {len(info.get('battery_log_invalid', []))}; "
                       f"discharge transitions: {len(info.get('battery_log_discharge_transitions', []))}")
            for t in info.get("bcct_pause_transitions", [])[:HUMAN_LIMIT]:
                out.append(f"    BCCT {t['time']}: paused {t['paused'][0]} -> {t['paused'][1]} "
                           f"(raw_level {t['raw_level']}, zcv {t['zcv_mv']} mV, estimate)")
    for error in report["inputs"]["errors"]:
        out.append(f"Input error: {error['input']}: {error['error']}")
    return "\n".join(out) + "\n"


def write_new(path, text):
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)  # exclusive, fresh inode
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def positive_seconds(text):
    """A finite, positive number of seconds; nan/inf/0 would disable the gap checks."""
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite positive number of seconds")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", nargs="+", required=True,
                        help="phone samples JSONL in acquisition order (rotated .1 first)")
    parser.add_argument("--dns-csv", required=True, help="copy of the monitoring host's dns.csv")
    parser.add_argument("--maintenance", required=True, help="copy of the monitoring host's maintenance.txt")
    parser.add_argument("--baseline-start", required=True)
    parser.add_argument("--baseline-end", help="baseline captured at/after the window end")
    parser.add_argument("--logs", required=True, help="logs/ directory (index.txt, <UTC>/messages*, procs.txt)")
    parser.add_argument("--start", default=WINDOW_START)
    parser.add_argument("--end", default=WINDOW_END)
    parser.add_argument("--boot-id", default=WINDOW_BOOT)
    parser.add_argument("--log-tz", default="Europe/Amsterdam", help="phone local zone of syslog/battery log")
    parser.add_argument("--max-log-gap", type=positive_seconds, default=PHONE_LIMIT,
                        help="seconds without any syslog line (crond logs every minute) that break log coverage")
    parser.add_argument("--json-out", help="write the JSON report here")
    parser.add_argument("--summary-out", help="also write the human summary here")
    args = parser.parse_args(argv)
    outputs = [Path(p) for p in (args.json_out, args.summary_out) if p]
    inputs = [Path(p) for p in (*args.samples, args.dns_csv, args.maintenance, args.baseline_start,
                                args.baseline_end) if p]
    logs_dir = Path(args.logs)
    if logs_dir.is_dir():
        # The collector writes plain files only; a symlink could make readers and checks disagree.
        links = [str(p) for p in logs_dir.rglob("*") if p.is_symlink()]
        if links:
            parser.error(f"symbolic links under --logs are not accepted: {links[:5]}")
        inputs += [p for p in logs_dir.rglob("*") if p.is_file()]
    def same(a, b):
        try:
            return a.resolve() == b.resolve() or (a.exists() and b.exists() and a.samefile(b))
        except OSError:
            return True  # cannot tell: refuse rather than risk the evidence
    for out in outputs:
        if out.resolve().is_relative_to(logs_dir.resolve()) or any(same(out, p) for p in inputs):
            parser.error(f"output {out} would overwrite an audited input")
    if len(outputs) == 2 and same(outputs[0], outputs[1]):
        parser.error("--json-out and --summary-out are the same file")
    try:
        report = audit(args)
    except (ValueError, KeyError) as error:
        parser.error(str(error))
    text = summary(report)
    sys.stdout.write(text)
    # New files replace the paths atomically (a fresh inode), so no hard or symbolic link to an
    # input can be written through.
    if args.json_out:
        write_new(Path(args.json_out), json.dumps(report, indent=2, allow_nan=False, default=str) + "\n")
    if args.summary_out:
        write_new(Path(args.summary_out), text)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
