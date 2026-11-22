#!/usr/bin/env python3
"""Summarise the I14 DNS soak test (scripts/i14-dns-soak.py).

Usage: i14-dns-soak-report.py [--max-gap-seconds N] [CSV] [MAINTENANCE]
       (defaults: ~/i14-soak/dns.csv, ~/i14-soak/maintenance.txt)

A run passes when the health name answers over UDP and TCP, a real name answers, a signed name carries
the AD flag and a bogus-signed name gets SERVFAIL. Failed runs inside a planned maintenance window
(one per line: START END reason, ISO times in the CSV's clock, UTC) are counted apart; a missing
maintenance file means no planned maintenance.

The CSV must be one the recorder writes: the header
"time,health_udp,ms,health_tcp,ms,example_udp,ms,signed_ad,ms,bogus,ms" (one rcode-or-ERROR and one
milliseconds column per check; the repeated ms columns are intentional) followed by at least one
observation row. Anything else -- an unreadable or non-UTF-8 file, a different header, a wrong row
width, a bad timestamp, a latency outside 0..2^63-1 integer milliseconds or an empty answer -- is invalid input:
a line/path diagnostic is printed and the exit code is 2, with no availability verdict.

Both input files are streamed from the opened file in fixed-size chunks and decoded as UTF-8 as
the bytes arrive, under a hard byte cap counted on the bytes actually taken from the opened input
(at most cap+1 bytes, the extra byte being the EOF check that tells an exactly-at-cap file from an
over-cap one): the CSV at most 16 MiB and the maintenance file at most 1 MiB (both inclusive). For
a regular file already larger than its cap the size metadata raises the same diagnostic before the
first read, but the counted bytes remain the guard because files can change. A larger file is
invalid evidence: a path/limit diagnostic is printed, the exit code is 2 and no partial report is
written. The reader does not load a full file up front. Peak memory still depends on the largest
record and the number of parsed observations and maintenance windows the report must hold.

With --max-gap-seconds N (finite and positive), adjacent recorded rounds are compared in file order after UTC
normalisation: duplicate or backwards timestamps are ambiguous evidence (exit 2), and every interval
strictly longer than N seconds is reported as missing monitoring coverage, never as a guessed DNS
outage or a guessed missing-round count. The check assesses only the interval bounded by the first and
last recorded rounds. A lone observation contains no interval to check.

Report output: run count and time range, passed / planned-maintenance / unplanned-failure counts,
availability outside maintenance over the observed rounds, health_udp and example_udp latency stats,
the coverage result when requested, and one UNPLANNED line per unplanned failure. Text that came
from the input files (answer strings, original timestamp displays, paths in diagnostics) is printed
with every non-printable character -- controls, newlines, Unicode line separators -- shown as an
escape sequence, so input content cannot forge extra report lines; printable text is shown as is.

Exit codes: 0 = no unplanned failures observed and no issues found by the requested coverage check;
1 = unplanned DNS failures were observed on valid evidence; 2 = invalid input, ambiguous timestamps
or monitoring-coverage gaps (observed unplanned failures are still listed).
"""
import argparse
import csv
import datetime
import io
import math
import os
import stat
import statistics
import sys

WANT = {"health_udp": "NOERROR", "health_tcp": "NOERROR", "example_udp": "NOERROR",
        "signed_ad": "NOERROR+AD", "bogus": "SERVFAIL"}
HEADER = ["time", "health_udp", "ms", "health_tcp", "ms", "example_udp", "ms",
          "signed_ad", "ms", "bogus", "ms"]
MAX_LATENCY_MS = 2**63 - 1
MAX_CSV_BYTES = 16 * 1024 * 1024     # inclusive byte cap on the recorder CSV
MAX_MAINTENANCE_BYTES = 1024 * 1024  # inclusive byte cap on the maintenance file
READ_CHUNK_BYTES = 64 * 1024         # maximum raw read size


class InvalidInput(Exception):
    """Malformed input file; the message carries the path and line diagnostic."""


def cap_message(path, limit):
    return (f"{path}: file is larger than the {limit}-byte input cap;"
            " refusing to read it as evidence")


class BudgetedRaw(io.RawIOBase):
    """Raw layer over an open binary file that never hands out more than limit+1 bytes.

    Bytes are counted as they are taken from the opened file, so the cap holds even when the
    file's size metadata lies or the file changes while it is being read. The +1 byte is the EOF
    check that separates an exactly-at-cap file (valid: the next read returns EOF) from an
    over-cap one (the limit+1-th byte is delivered, which proves the breach) and raises the cap
    diagnostic right there, so no further input is decoded or parsed. Every request passed to
    the wrapped file is clipped to READ_CHUNK_BYTES, so reads happen in fixed-size chunks.
    """

    def __init__(self, raw, path, limit):
        super().__init__()
        self._raw = raw
        self._path = path
        self._limit = limit
        self._budget = limit + 1

    def readable(self):
        return True

    def readinto(self, b):
        if self._budget <= 0:
            return 0
        want = min(len(b), self._budget, READ_CHUNK_BYTES)
        n = self._raw.readinto(memoryview(b)[:want])
        if n is None:
            return None
        if n:
            self._budget -= n
            if self._budget == 0:
                raise InvalidInput(cap_message(self._path, self._limit))
        return n

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


def open_bounded(path, limit, newline=""):
    """Open path for incremental UTF-8 reading under the inclusive byte cap limit.

    Returns a text stream over a BudgetedRaw in a BufferedReader; closing the stream closes the
    whole chain and the file. Bytes are decoded as they arrive. FileNotFoundError propagates for
    the caller's missing-file rule. Other open errors and cap violations raise InvalidInput.
    Regular file size is checked before reading. BudgetedRaw also enforces the cap while reading
    because files can change.
    """
    try:
        # The budget must apply before buffering, at the actual file read.
        f = open(path, "rb", buffering=0)
    except FileNotFoundError:
        raise
    except OSError as e:
        raise InvalidInput(f"{path}: cannot read file: {e.strerror or e}") from None
    try:
        try:
            st = os.fstat(f.fileno())
        except OSError:
            st = None  # no usable fd metadata (e.g. a pipe): the byte count below is the guard
        if st is not None and stat.S_ISREG(st.st_mode) and st.st_size > limit:
            raise InvalidInput(cap_message(path, limit))
        raw = BudgetedRaw(f, path, limit)
        return io.TextIOWrapper(io.BufferedReader(raw, READ_CHUNK_BYTES),
                                encoding="utf-8", newline=newline)
    except BaseException:
        f.close()
        raise


def visible(text):
    """Escape non-printable characters for display; printable text is returned unchanged.

    Input-file text must never reach the report raw: every non-printable character (control
    characters, newlines, Unicode line separators such as U+2028/U+2029) is shown as its escape
    sequence so injected content cannot forge report lines.
    """
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


def utc(text):
    """ISO time as naive UTC (the CSV's form); a zone such as Z or +01:00 is converted."""
    t = datetime.datetime.fromisoformat(text)
    return t.astimezone(datetime.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def windows(path):
    """Planned maintenance windows as (start, end, reason) in naive UTC; a missing file means none.

    The file is streamed line by line under its byte cap and decoded as it is read; only the
    parsed windows are retained.
    """
    result = []
    try:
        stream = open_bounded(path, MAX_MAINTENANCE_BYTES, newline=None)
    except FileNotFoundError:
        return result
    try:
        with stream as text:
            for ln, line in enumerate(text, 1):
                parts = line.strip().split(None, 2)
                if not parts or parts[0].startswith("#"):
                    continue
                if len(parts) < 2:
                    raise InvalidInput(f"{path}:{ln}: incomplete maintenance window, need START END [reason]: {line.strip()!r}")
                try:
                    a, b = utc(parts[0]), utc(parts[1])
                except (ValueError, OverflowError):
                    raise InvalidInput(f"{path}:{ln}: bad maintenance time: {line.strip()!r}") from None
                if a > b:
                    raise InvalidInput(f"{path}:{ln}: reversed maintenance window {a.isoformat()} .. {b.isoformat()}")
                result.append((a, b, parts[2] if len(parts) > 2 else ""))
    except UnicodeDecodeError as e:
        raise InvalidInput(f"{path}: not valid UTF-8: {e}") from None
    except OSError as e:
        raise InvalidInput(f"{path}: cannot read file: {e.strerror or e}") from None
    return result


def load(csv_path):
    """Read and validate the recorder CSV within its byte cap, streaming row by row.

    Returns one (line_number, answers, naive-UTC time, check->ms) tuple per observation row and
    raises InvalidInput with a path/line diagnostic on anything malformed. The CSV reader pulls
    lines from a text stream that decodes fixed-size chunks of the file as they arrive, so no
    full-file byte or decoded-text buffer exists; only the parsed observations are retained.
    """
    obs = []
    seen_header = False
    try:
        with open_bounded(csv_path, MAX_CSV_BYTES, newline="") as text:
            reader = csv.reader(text, strict=True)
            try:
                for r in reader:
                    ln = reader.line_num
                    if not seen_header:
                        if r != HEADER:
                            raise InvalidInput(f"{csv_path}:{ln}: unsupported header {r!r}, expected {HEADER!r}")
                        seen_header = True
                        continue
                    if len(r) != len(HEADER):
                        raise InvalidInput(f"{csv_path}:{ln}: expected {len(HEADER)} fields, got {len(r)}")
                    rec = dict(zip(HEADER, r))
                    try:
                        t = utc(rec["time"])
                    except (ValueError, OverflowError):
                        raise InvalidInput(f"{csv_path}:{ln}: bad timestamp {rec['time']!r}") from None
                    for k in WANT:
                        if not rec[k].strip():
                            raise InvalidInput(f"{csv_path}:{ln}: empty answer for {k}")
                    rec_ms = {}
                    for k, v in zip(HEADER[1::2], r[2::2]):
                        try:
                            ms = int(v)
                        except ValueError:
                            raise InvalidInput(f"{csv_path}:{ln}: {k} latency {v!r} is not an integer") from None
                        if ms < 0:
                            raise InvalidInput(f"{csv_path}:{ln}: {k} latency {v!r} is negative")
                        if ms > MAX_LATENCY_MS:
                            raise InvalidInput(f"{csv_path}:{ln}: {k} latency exceeds {MAX_LATENCY_MS} ms")
                        rec_ms[k] = ms
                    obs.append((ln, rec, t, rec_ms))
            except csv.Error as e:
                raise InvalidInput(f"{csv_path}:{reader.line_num}: malformed CSV: {e}") from None
    except UnicodeDecodeError as e:
        raise InvalidInput(f"{csv_path}: not valid UTF-8: {e}") from None
    except OSError as e:
        raise InvalidInput(f"{csv_path}: cannot read file: {e.strerror or e}") from None
    if not seen_header:
        raise InvalidInput(f"{csv_path}: empty file, expected header {','.join(HEADER)}")
    if not obs:
        raise InvalidInput(f"{csv_path}: no observation rows after the header")
    return obs


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0


def median(values):
    return statistics.median(values) if values else 0


def coverage(obs, csv_path, max_gap_seconds):
    """Compare adjacent recorded rounds in file order.

    Returns (gaps, error): gaps lists (start, end, seconds) for intervals strictly longer than
    max_gap_seconds; error is a diagnostic when the timestamp order is ambiguous.
    """
    gaps, prev = [], None
    for ln, _rec, t, _ms in obs:
        if prev is not None:
            d = (t - prev[1]).total_seconds()
            if d == 0:
                return gaps, f"{csv_path}:{ln}: duplicate timestamp {t.isoformat()} (line {prev[0]} has the same time)"
            if d < 0:
                return gaps, (f"{csv_path}:{ln}: timestamp {t.isoformat()} is before the previous round"
                              f" {prev[1].isoformat()} (line {prev[0]})")
            if d > max_gap_seconds:
                gaps.append((prev[1], t, d))
        prev = (ln, t)
    return gaps, None


def seconds_text(value):
    return str(value).removesuffix(".0") + "s"


def report(csv_path, maint_path, out=sys.stdout, max_gap_seconds=None):
    """Summarise one recorder CSV; returns the exit code (0, 1 or 2)."""
    if max_gap_seconds is not None and not 0 < max_gap_seconds < math.inf:
        raise ValueError("max_gap_seconds must be finite and positive")
    try:
        planned = windows(maint_path)
        obs = load(csv_path)
    except InvalidInput as e:
        print(f"invalid input: {visible(str(e))}", file=out)
        return 2
    gaps = []
    if max_gap_seconds is not None:
        gaps, error = coverage(obs, csv_path, max_gap_seconds)
        if error:
            print(f"invalid evidence: {visible(error)}", file=out)
            return 2
    ok = in_maint = 0
    bad, lat = [], {"health_udp": [], "example_udp": []}
    for _ln, rec, t, rec_ms in obs:
        failed = [k for k, v in WANT.items() if rec[k] != v]
        if not failed:
            ok += 1
            for k in lat:
                lat[k].append(rec_ms[k])
        elif any(a <= t <= b for a, b, _ in planned):
            in_maint += 1
        else:
            bad.append((rec["time"], ", ".join(f"{k}={rec[k]}" for k in failed)))
    counted = len(obs) - in_maint
    print(f"runs {len(obs)} ({visible(obs[0][1]['time'])} .. {visible(obs[-1][1]['time'])})", file=out)
    print(f"passed {ok}; failed in planned maintenance {in_maint}; unplanned failures {len(bad)}", file=out)
    if counted:
        print(f"availability outside maintenance: {100 * (counted - len(bad)) / counted:.2f}%", file=out)
    for k, v in lat.items():
        print(f"{k} ms: median {median(v):g}, p95 {pct(v, 0.95)}, max {max(v) if v else 0}", file=out)
    if max_gap_seconds is not None:
        first, last = obs[0][2].isoformat(), obs[-1][2].isoformat()
        limit = seconds_text(max_gap_seconds)
        if len(obs) == 1:
            print(f"coverage: no adjacent rounds to check (single observation at {first} UTC)", file=out)
        elif gaps:
            for a, b, d in gaps:
                print(f"coverage gap: {a.isoformat()} .. {b.isoformat()} ({seconds_text(d)} > {limit})"
                      " is missing monitoring coverage, not a DNS outage", file=out)
            print(f"coverage: {len(gaps)} missing interval(s) over {limit} within recorded endpoints "
                  f"{first} .. {last} UTC", file=out)
        else:
            largest = max((obs[i][2] - obs[i - 1][2]).total_seconds() for i in range(1, len(obs)))
            print(f"coverage: OK, largest interval {seconds_text(largest)} <= {limit} within recorded endpoints "
                  f"{first} .. {last} UTC", file=out)
    for t, why in bad:
        print(f"UNPLANNED {visible(t)}: {visible(why)}", file=out)
    if gaps:
        return 2
    return 1 if bad else 0


def main(argv=None):
    home = os.path.expanduser("~/i14-soak")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", nargs="?", default=f"{home}/dns.csv",
                   help="recorder CSV, at most 16 MiB (default: %(default)s)")
    p.add_argument("maintenance", nargs="?", default=f"{home}/maintenance.txt",
                   help="planned maintenance windows, one 'START END reason' per line, at most 1 MiB"
                        " (default: %(default)s); a missing file means no planned maintenance")
    p.add_argument("--max-gap-seconds", type=float, default=None, metavar="N",
                   help="also require monitoring coverage: every interval between adjacent recorded rounds"
                        " longer than N seconds is missing coverage (exit 2), not a DNS outage")
    a = p.parse_args(argv)
    if a.max_gap_seconds is not None and not 0 < a.max_gap_seconds < math.inf:
        p.error("--max-gap-seconds must be finite and positive")
    return report(a.csv, a.maintenance, max_gap_seconds=a.max_gap_seconds)


if __name__ == "__main__":
    sys.exit(main())
