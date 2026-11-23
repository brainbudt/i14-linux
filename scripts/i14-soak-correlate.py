#!/usr/bin/env python3
"""Correlate recorded I14 DNS and power samples offline. See docs/SOAK-CORRELATE.md."""
import argparse
import bisect
import datetime
import json
import re
import sys
from decimal import Decimal, DecimalException, Inexact, localcontext
from itertools import pairwise
from typing import NamedTuple

CHECKS = ("health_udp", "health_tcp", "example_udp", "signed_ad", "bogus")
EXPECTED = ("NOERROR", "NOERROR", "NOERROR", "NOERROR+AD", "SERVFAIL")
HEADER = ["time", "health_udp", "ms", "health_tcp", "ms", "example_udp", "ms",
          "signed_ad", "ms", "bogus", "ms"]
MAX_LATENCY_MS = 2 ** 63 - 1
MAX_DNS_BYTES = 16 * 1024 * 1024        # inclusive byte cap per DNS CSV file
MAX_POWER_BYTES = 16 * 1024 * 1024      # inclusive byte cap per power JSONL file
MAX_MAINTENANCE_BYTES = 1024 * 1024     # inclusive byte cap on the maintenance file
INTEGER_RE = re.compile(r"[0-9]+")
# Match one complete recorder field, allowing doubled quotes in quoted fields.
CSV_FIELD = re.compile(r'(?:[^",\r\n]*|"[^"\r\n]*(?:""[^"\r\n]*)*")(?=,|\Z)')
ISO_TIME = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?"
)
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
NS = 10**9
SCHEMA = "i14-soak-correlate/1"
NOTES = [
    "Correlation of sampled DNS and power logs only: neither an SLA nor proof of physical"
    " power state, boot events or causation.",
    "Coverage gaps are missing monitoring evidence between the first and last DNS"
    " observation; they never count as DNS failures.",
    "external_power is the left sample of a qualified adjacent same-boot interval [a,b);"
    " missing or conflicting power telemetry never changes a DNS outcome, only the state"
    " becomes unknown with a power_reason.",
]


class InvalidInput(Exception):
    """Malformed input file; the message carries the path/line diagnostic."""


class Parser(argparse.ArgumentParser):
    """Argument parser whose diagnostics are one physical line on stderr, exit 2, no stdout."""

    def error(self, message):
        sys.stderr.write(f"invalid input: {visible(message)}\n")
        sys.exit(2)


def visible(text):
    """Escape non-printable characters so a diagnostic stays one physical line."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


class Dns(NamedTuple):
    time: int
    answers: tuple[str, ...]
    latencies: tuple[int, ...]


class Power(NamedTuple):
    time: int
    boot: str
    uptime: int
    usb: int | None
    ac: int | None


def input_lines(path, limit):
    """Bound even a single unterminated line by the bytes remaining in this file."""
    try:
        with open(path, "rb") as stream:
            remaining = limit
            line_no = 0
            while raw := stream.readline(remaining + 1):
                remaining -= len(raw)
                if remaining < 0:
                    raise InvalidInput(f"{path}: file is larger than the {limit}-byte input cap")
                line_no += 1
                try:
                    yield line_no, raw.decode("utf-8").removesuffix("\n").removesuffix("\r")
                except UnicodeDecodeError:
                    raise InvalidInput(f"{path}:{line_no}: not valid UTF-8") from None
    except OSError as exc:
        raise InvalidInput(f"{path}: cannot read file: {exc.strerror}") from None


def parse_time(text):
    match = ISO_TIME.fullmatch(text)
    if not match:
        raise ValueError
    whole, fraction, zone = match.groups()
    delta = datetime.datetime.fromisoformat(whole + (zone if zone and zone != "Z" else "+00:00")).astimezone(EPOCH.tzinfo) - EPOCH
    return (delta.days * 86400 + delta.seconds) * NS + int((fraction or "").ljust(9, "0"))


def iso_z(t):
    seconds, ns = divmod(t, NS)
    stamp = (EPOCH + datetime.timedelta(seconds=seconds)).replace(tzinfo=None)
    fraction = f".{ns:09d}".rstrip("0") if ns else ""
    return stamp.isoformat() + fraction + "Z"


def seconds_number(ns):
    return ns // NS if ns % NS == 0 else ns / NS


def nanoseconds(value):
    # Trap rounding so adjacent observations cannot silently become duplicates.
    if type(value) not in (int, Decimal):
        raise ValueError
    value = Decimal(value)
    if not value.is_finite() or (value and abs(value.adjusted()) > 1000):
        raise ValueError
    with localcontext() as ctx:
        ctx.traps[Inexact] = True
        return int(value.scaleb(9).to_integral_exact())


def append_ordered(records, record, where, kind, previous_name):
    if records and record.time <= records[-1].time:
        if record.time < records[-1].time:
            raise InvalidInput(f"{where}: earlier than the previous {previous_name}")
        if record != records[-1]:
            raise InvalidInput(f"{where}: conflicting {kind} records at the same timestamp")
    else:
        records.append(record)


def csv_fields(line, where):
    if '"' not in line and "\r" not in line:
        return line.split(",", len(HEADER))
    fields = []
    start = 0
    while len(fields) <= len(HEADER):
        match = CSV_FIELD.match(line, start)
        if not match:
            raise InvalidInput(f"{where}: malformed CSV quoting")
        field = match[0]
        if field.startswith('"'):
            field = field[1:-1].replace('""', '"')
        fields.append(field)
        if match.end() == len(line):
            break
        start = match.end() + 1
    return fields


def load_dns(paths):
    records = []
    for path in paths:
        lines = input_lines(path, MAX_DNS_BYTES)
        header = next(lines, None)
        if header is None:
            raise InvalidInput(f"{path}: empty file")
        if csv_fields(header[1], f"{path}:1") != HEADER:
            raise InvalidInput(f"{path}:1: unsupported header")
        for ln, line in lines:
            where = f"{path}:{ln}"
            fields = csv_fields(line, where)
            if len(fields) != len(HEADER):
                raise InvalidInput(f"{where}: expected 11 fields")
            try:
                t = parse_time(fields[0])
            except (ValueError, OverflowError):
                raise InvalidInput(f"{where}: invalid timestamp") from None
            answers = tuple(fields[1::2])
            latencies = []
            for check, answer, raw in zip(CHECKS, answers, fields[2::2]):
                if not answer.strip():
                    raise InvalidInput(f"{where}: empty answer for {check}")
                digits = raw.lstrip("0") or "0"
                if not INTEGER_RE.fullmatch(raw) or len(digits) > 19 or int(digits) > MAX_LATENCY_MS:
                    raise InvalidInput(f"{where}: invalid {check} latency")
                latencies.append(int(digits))
            append_ordered(records, Dns(t, answers, tuple(latencies)), where, "DNS", "observation")
    if not records:
        raise InvalidInput("no effective DNS observation in the --dns inputs")
    return records


def unique_keys(pairs):
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ValueError
    return row


def reject_constant(_):
    raise ValueError


def online_field(row, key):
    value = row.get(key)
    if value is None:
        return None
    if type(value) is bool or value not in (0, 1):
        raise ValueError
    return int(value)


def load_power(paths):
    records = []
    for path in paths:
        for ln, line in input_lines(path, MAX_POWER_BYTES):
            where = f"{path}:{ln}"
            try:
                row = json.loads(line, parse_float=Decimal, parse_constant=reject_constant,
                                 object_pairs_hook=unique_keys)
                if not isinstance(row, dict):
                    raise ValueError
                epoch = nanoseconds(row.get("epoch"))
                uptime = nanoseconds(row.get("uptime_seconds"))
                boot = row.get("boot_id")
                if not (-62135596800 * NS <= epoch < 253402300800 * NS and uptime >= 0):
                    raise ValueError
                if not isinstance(boot, str) or not boot.strip():
                    raise ValueError
                record = Power(epoch, boot, uptime, online_field(row, "usb_online"), online_field(row, "ac_online"))
            except (DecimalException, ValueError, RecursionError):
                raise InvalidInput(f"{where}: invalid power record") from None
            append_ordered(records, record, where, "power", "sample")
    return records


def load_maintenance(path):
    windows = []
    if path is not None:
        for ln, line in input_lines(path, MAX_MAINTENANCE_BYTES):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                first, last, *_ = line.split(None, 2)
                start, end = parse_time(first), parse_time(last)
            except (ValueError, OverflowError):
                raise InvalidInput(f"{path}:{ln}: invalid maintenance window") from None
            if start > end:
                raise InvalidInput(f"{path}:{ln}: reversed maintenance window")
            windows.append((start, end))
    return windows


def merge_windows(windows):
    """Sort and merge unordered/overlapping (and touching) windows into a disjoint union."""
    merged = []
    for a, b in sorted(windows):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def make_membership(merged):
    """Inclusive window membership test over merged (sorted, disjoint) windows."""
    starts = [a for a, _ in merged]
    ends = [b for _, b in merged]

    def inside(t):
        i = bisect.bisect_right(starts, t) - 1
        return i >= 0 and t <= ends[i]

    return inside


def coverage_gaps(obs, merged, max_gap):
    """Uncovered pieces strictly longer than max_gap between adjacent DNS observations.

    For each adjacent pair the union of planned maintenance is subtracted from the open
    interval between the samples; every remaining contiguous piece whose duration is
    strictly greater than max_gap seconds is emitted as a gap. Equality passes.
    """
    gaps = []

    def add_gap(start, end):
        if end - start > max_gap:
            gaps.append({"start_utc": iso_z(start), "end_utc": iso_z(end),
                         "duration_seconds": seconds_number(end - start)})

    # Walk the two ordered timelines together. A window spanning the next
    # observation stays current until its right endpoint has been consumed.
    index = 0
    for prev, cur in pairwise(obs):
        edge = prev.time
        while index < len(merged) and merged[index][0] < cur.time:
            start, end = merged[index]
            add_gap(edge, start)
            edge = max(edge, end)
            if end >= cur.time:
                break
            index += 1
        add_gap(edge, cur.time)
    return gaps


def classify(obs, inside):
    """Split observations into passed / planned-failure counts and unplanned failures."""
    passed = planned = 0
    unplanned = []
    for o in obs:
        failed = [c for c, want, got in zip(CHECKS, EXPECTED, o.answers) if got != want]
        if not failed:
            passed += 1
        elif inside(o.time):
            planned += 1
        else:
            unplanned.append({"t": o.time, "checks": failed})
    return passed, planned, unplanned


def left_power_state(sample):
    usb, ac = sample.usb, sample.ac
    if usb == 1 or ac == 1:
        return "online"
    if usb == 0 and ac == 0:
        return "offline"
    return "unknown"


def analyse_power(samples, max_gap):
    """Build the power-state lookup for DNS failure times.

    An interval [a,b) between adjacent effective samples is qualified only when both
    samples share one boot, the wall-clock and uptime deltas are each positive and
    <= max_gap, and they disagree by at most 2 s. Returns (state_at, qualified_count);
    state_at(t) yields (state, reason) with a stable reason label when unknown.
    """
    epochs = [sample.time for sample in samples]
    reasons = []
    for a, b in pairwise(samples):
        wall = b.time - a.time
        up = b.uptime - a.uptime
        if a.boot != b.boot:
            reason = "different_boot"
        elif wall > max_gap:
            reason = "wall_gap_exceeds_power_max_gap"
        elif up <= 0:
            reason = "uptime_not_increasing"
        elif up > max_gap:
            reason = "uptime_gap_exceeds_power_max_gap"
        elif abs(wall - up) > 2 * NS:
            reason = "wall_uptime_disagreement"
        else:
            reason = None
        reasons.append(reason)

    def state_at(t):
        if not samples:
            return "unknown", "no_power_samples"
        i = bisect.bisect_right(epochs, t) - 1
        if i < 0:
            return "unknown", "before_first_power_sample"
        if i == len(reasons):
            return "unknown", "at_or_after_last_power_sample"
        if reasons[i]:
            return "unknown", reasons[i]
        state = left_power_state(samples[i])
        return state, "left_state_unknown" if state == "unknown" else None

    return state_at, reasons.count(None)


def correlate(dns_paths, power_paths, maintenance_path, dns_max_gap, power_max_gap):
    """Load, validate and correlate the streams; returns (report dict, exit code)."""
    obs = load_dns(dns_paths)
    samples = load_power(power_paths)
    windows = load_maintenance(maintenance_path)
    merged = merge_windows(windows)
    inside = make_membership(merged)
    gaps = coverage_gaps(obs, merged, dns_max_gap)
    passed, planned, unplanned = classify(obs, inside)
    state_at, qualified_count = analyse_power(samples, power_max_gap)
    failures = []
    by_state = {"online": 0, "offline": 0, "unknown": 0}
    for u in unplanned:
        state, reason = state_at(u["t"])
        by_state[state] += 1
        entry = {"time_utc": iso_z(u["t"]), "checks": u["checks"], "external_power": state}
        if state == "unknown":
            entry["power_reason"] = reason
        failures.append(entry)
    complete = len(obs) > 1 and not gaps
    if not complete:
        code = 2
    elif unplanned:
        code = 1
    else:
        code = 0
    report = {
        "schema": SCHEMA,
        "dns": {
            "files": len(dns_paths),
            "effective_observations": len(obs),
            "first_utc": iso_z(obs[0].time),
            "last_utc": iso_z(obs[-1].time),
            "passed": passed,
            "planned_failures": planned,
            "unplanned_failures": len(unplanned),
        },
        "maintenance": {
            "provided": maintenance_path is not None,
            "windows": len(windows),
            "merged_windows": len(merged),
            "merged_seconds": seconds_number(sum(b - a for a, b in merged)),
        },
        "coverage": {
            "max_gap_seconds": seconds_number(dns_max_gap),
            "status": "complete" if complete else "incomplete",
            "single_observation": len(obs) == 1,
            "gaps": gaps,
        },
        "power": {
            "files": len(power_paths),
            "effective_samples": len(samples),
            "max_gap_seconds": seconds_number(power_max_gap),
            "qualified_intervals": qualified_count,
        },
        "unplanned_failures": failures,
        "unplanned_by_power_state": by_state,
        "exit_code": code,
        "notes": list(NOTES),
    }
    return report, code


def positive_gap(text):
    try:
        ns = nanoseconds(Decimal(text))
        if ns <= 0:
            raise ValueError
        return ns
    except (DecimalException, ValueError):
        raise argparse.ArgumentTypeError("expected finite positive seconds with nanosecond precision") from None


def main(argv=None):
    p = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dns", action="append", required=True, metavar="CSV",
                   help="recorder DNS CSV, repeatable in acquisition/rotation order, at most 16 MiB each")
    p.add_argument("--power", action="append", default=[], metavar="JSONL",
                   help="power JSONL stream, repeatable in acquisition/rotation order, at most 16 MiB each;"
                        " may be omitted or empty")
    p.add_argument("--maintenance", metavar="FILE",
                   help="planned maintenance windows, one 'START END [reason]' per line, at most 1 MiB;"
                        " omitted means zero windows")
    p.add_argument("--dns-max-gap-seconds", type=positive_gap, required=True, metavar="N",
                   help="required finite positive seconds; uncovered DNS intervals strictly longer than N"
                        " are coverage gaps")
    p.add_argument("--power-max-gap-seconds", type=positive_gap, required=True, metavar="N",
                   help="required finite positive seconds; power intervals longer than N are not qualified"
                        " for correlation")
    a = p.parse_args(argv)
    try:
        report, code = correlate(a.dns, a.power, a.maintenance,
                                 a.dns_max_gap_seconds, a.power_max_gap_seconds)
    except InvalidInput as e:
        print(f"invalid input: {visible(str(e))}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
