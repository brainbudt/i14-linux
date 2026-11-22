"""scripts/i14-dns-soak-report.py: planned maintenance is counted apart, everything else is a failure."""
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT = ROOT / "scripts/i14-dns-soak-report.py"
spec = importlib.util.spec_from_file_location("soak", REPORT)
soak = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak)

HEAD = "time,health_udp,ms,health_tcp,ms,example_udp,ms,signed_ad,ms,bogus,ms\n"
GOOD = "NOERROR,3,NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300\n"
DOWN = "ERROR:TimeoutError,5005,ERROR:OSError,3120,ERROR:TimeoutError,5005,ERROR:TimeoutError,5004,ERROR:TimeoutError,5005\n"


def run(lines, maintenance="", *, max_gap=None, raw=None, write_maint=True):
    with tempfile.TemporaryDirectory() as d:
        c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
        if raw is not None:
            c.write_bytes(raw)
        else:
            c.write_text(HEAD + "".join(lines))
        if write_maint:
            m.write_text(maintenance)
        out = io.StringIO()
        kw = {} if max_gap is None else {"max_gap_seconds": max_gap}
        return soak.report(c, m, out, **kw), out.getvalue()


class SoakReport(unittest.TestCase):
    def test_all_good(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:05:00," + GOOD])
        self.assertEqual(rc, 0)
        self.assertIn("availability outside maintenance: 100.00%", text)
        self.assertIn("health_udp ms: median 3", text)

    def test_outage_inside_a_window_is_planned(self):
        rc, text = run(["2026-11-04T21:30:05," + DOWN, "2026-11-04T21:35:00," + GOOD],
                       "# planned\n2026-11-04T21:27:00 2026-11-04T22:01:00 tests\n")
        self.assertEqual(rc, 0)
        self.assertIn("failed in planned maintenance 1; unplanned failures 0", text)

    def test_median_of_an_even_count_is_the_mean_of_the_middle_two(self):
        _, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:05:00," + GOOD.replace("NOERROR,3,", "NOERROR,9,", 1)])
        self.assertIn("health_udp ms: median 6,", text)

    def test_windows_may_carry_a_zone_and_an_indented_comment(self):
        rc, text = run(["2026-11-04T21:30:05," + DOWN],
                       "  # planned\n2026-11-04T22:27:00+01:00 2026-11-04T21:40:00Z tests\n")
        self.assertEqual(rc, 0, text)

    def test_outage_outside_a_window_fails(self):
        rc, text = run(["2026-11-04T23:30:05," + DOWN], "2026-11-04T21:27:00 2026-11-04T22:01:00 tests\n")
        self.assertEqual(rc, 1)
        self.assertIn("UNPLANNED 2026-11-04T23:30:05", text)

    def test_a_wrong_answer_is_a_failure_too(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD.replace("SERVFAIL", "NOERROR")])
        self.assertEqual(rc, 1)
        self.assertIn("bogus=NOERROR", text)


class InvalidInput(unittest.TestCase):
    def test_missing_csv_is_a_clean_diagnostic(self):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "nope.csv"), pathlib.Path(d, "m.txt")
            m.write_text("")
            out = io.StringIO()
            rc = soak.report(c, m, out)
            self.assertEqual(rc, 2)
            self.assertIn("nope.csv", out.getvalue())

    def test_empty_file_is_invalid(self):
        rc, text = run([], raw=b"")
        self.assertEqual(rc, 2)
        self.assertIn("empty", text)
        self.assertIn("dns.csv", text)

    def test_invalid_utf8_is_invalid(self):
        raw = HEAD.encode() + b"2026-11-04T21:00:00,\xff,3,NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300\n"
        rc, text = run([], raw=raw)
        self.assertEqual(rc, 2)
        self.assertIn("UTF-8", text)
        self.assertIn("dns.csv", text)

    def test_header_only_has_no_observations(self):
        rc, text = run([])
        self.assertEqual(rc, 2)
        self.assertIn("no observation", text)
        self.assertNotIn("availability", text)

    def test_unsupported_header_is_invalid(self):
        rc, text = run([], raw=b"time,health_udp,ms\n2026-11-04T21:00:00,NOERROR,3\n")
        self.assertEqual(rc, 2)
        self.assertIn("unsupported header", text)

    def test_wrong_width_reports_the_line(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD.rstrip("\n") + ",extra\n"])
        self.assertEqual(rc, 2)
        self.assertIn("got 12", text)
        self.assertIn(":2:", text)
        rc, text = run(["2026-11-04T21:00:00,NOERROR\n"])
        self.assertEqual(rc, 2)
        self.assertIn("got 2", text)

    def test_bad_timestamp_is_invalid(self):
        rc, text = run(["not-a-time," + GOOD])
        self.assertEqual(rc, 2)
        self.assertIn("bad timestamp", text)
        self.assertIn(":2:", text)

    def test_unterminated_csv_field_is_invalid(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD.removesuffix("300\n") + '"300'])
        self.assertEqual(rc, 2)
        self.assertIn("malformed CSV", text)
        self.assertNotIn("availability", text)

    def test_utc_timestamp_outside_datetime_range_is_invalid(self):
        bad_time = "0001-01-01T00:00:00+01:00"
        for lines, maintenance in (
            ([bad_time + "," + GOOD], ""),
            (["2026-11-04T21:00:00," + GOOD], bad_time + " 2026-11-04T22:00:00Z"),
        ):
            with self.subTest(maintenance=bool(maintenance)):
                rc, text = run(lines, maintenance)
                self.assertEqual(rc, 2)
                self.assertIn("invalid input", text)
                self.assertNotIn("availability", text)

    def test_non_integer_latency_is_invalid(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD.replace("NOERROR,3,", "NOERROR,slow,", 1)])
        self.assertEqual(rc, 2)
        self.assertIn("not an integer", text)

    def test_out_of_range_latency_is_invalid_before_reporting(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD.replace("NOERROR,3,", "NOERROR," + "9" * 400 + ",", 1)])
        self.assertEqual(rc, 2)
        self.assertIn("latency", text)
        self.assertNotIn("availability", text)

    def test_empty_answer_field_is_invalid(self):
        rc, text = run(["2026-11-04T21:00:00,," + GOOD.split(",", 1)[1]])
        self.assertEqual(rc, 2)
        self.assertIn("empty answer", text)
        self.assertIn("health_udp", text)

    def test_malformed_maintenance_rows_are_invalid(self):
        for maint in ("2026-11-04T21:00:00\n",
                      "nope 2026-11-04T22:00:00 x\n",
                      "2026-11-04T22:00:00 2026-11-04T21:00:00 x\n"):
            with self.subTest(maint=maint):
                rc, text = run(["2026-11-04T21:00:00," + GOOD], maint)
                self.assertEqual(rc, 2)
                self.assertIn("m.txt", text)

    def test_absent_maintenance_file_means_no_planned_maintenance(self):
        rc, text = run(["2026-11-04T21:30:05," + DOWN], write_maint=False)
        self.assertEqual(rc, 1)
        self.assertIn("unplanned failures 1", text)


class Hardening(unittest.TestCase):
    """Input text is escaped on output and both inputs are read under a byte cap."""

    def run_bytes(self, csv_bytes, maint_bytes=None):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_bytes(csv_bytes)
            if maint_bytes is not None:
                m.write_bytes(maint_bytes)
            out = io.StringIO()
            return soak.report(c, m, out), out.getvalue()

    def test_the_caps_are_16_mib_csv_and_1_mib_maintenance(self):
        self.assertEqual(soak.MAX_CSV_BYTES, 16 * 1024 * 1024)
        self.assertEqual(soak.MAX_MAINTENANCE_BYTES, 1024 * 1024)

    def test_control_newline_and_unicode_separator_in_an_answer_are_escaped(self):
        rows = ('2026-11-04T21:00:00,"NOERROR\nEVIL",3,NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300\n'
                '2026-11-04T21:05:00,NOERROR,3,NOERROR,14,NOERROR,60,NOERROR+AD,5,"SERVFAIL\u2028EVIL",300\n')
        rc, text = self.run_bytes((HEAD + rows).encode(), b"")
        self.assertEqual(rc, 1)
        self.assertIn("UNPLANNED 2026-11-04T21:00:00: health_udp=NOERROR\\nEVIL", text)
        self.assertIn("UNPLANNED 2026-11-04T21:05:00: bogus=SERVFAIL\\u2028EVIL", text)
        self.assertNotIn("NOERROR\nEVIL", text)
        self.assertNotIn("SERVFAIL\u2028EVIL", text)
        unplanned = [ln for ln in text.splitlines() if ln.startswith("UNPLANNED")]
        self.assertEqual(len(unplanned), 2)
        self.assertIn("runs 2 (2026-11-04T21:00:00 .. 2026-11-04T21:05:00)", text)

    def test_recorder_statuses_and_printable_unicode_are_shown_verbatim(self):
        row = "2026-11-04T21:00:00,ERROR:TimeoutError,5005,error:timeout,14,NOERROR\u2603,60,NOERROR+AD,5,SERVFAIL,300\n"
        rc, text = self.run_bytes((HEAD + row).encode(), b"")
        self.assertEqual(rc, 1)
        self.assertIn("health_udp=ERROR:TimeoutError, health_tcp=error:timeout, example_udp=NOERROR\u2603", text)

    def test_a_control_character_in_an_invalid_path_is_escaped(self):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "evil\nname\u2028x.csv"), pathlib.Path(d, "m.txt")
            m.write_text("")
            out = io.StringIO()
            rc = soak.report(c, m, out)
            text = out.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("evil\\nname\\u2028x.csv", text)
        self.assertNotIn("evil\nname\u2028x.csv", text)

    def test_csv_at_the_cap_is_accepted_and_one_byte_over_is_rejected(self):
        raw = (HEAD + "2026-11-04T21:00:00," + GOOD).encode()
        with mock.patch.object(soak, "MAX_CSV_BYTES", len(raw)):
            rc, text = self.run_bytes(raw, b"")
            self.assertEqual(rc, 0, text)
            self.assertIn("runs 1", text)
        with mock.patch.object(soak, "MAX_CSV_BYTES", len(raw) - 1):
            rc, text = self.run_bytes(raw, b"")
            self.assertEqual(rc, 2)
            self.assertIn("invalid input", text)
            self.assertIn("dns.csv", text)
            self.assertIn(str(len(raw) - 1), text)
            self.assertNotIn("runs ", text)
            self.assertNotIn("availability", text)
            self.assertNotIn("UNPLANNED", text)

    def test_maintenance_at_the_cap_is_accepted_and_one_byte_over_is_rejected(self):
        csv = (HEAD + "2026-11-04T21:30:05," + DOWN).encode()
        maint = b"2026-11-04T21:27:00 2026-11-04T22:01:00 tests\n"
        with mock.patch.object(soak, "MAX_MAINTENANCE_BYTES", len(maint)):
            rc, text = self.run_bytes(csv, maint)
            self.assertEqual(rc, 0, text)
            self.assertIn("failed in planned maintenance 1", text)
        with mock.patch.object(soak, "MAX_MAINTENANCE_BYTES", len(maint) - 1):
            rc, text = self.run_bytes(csv, maint)
            self.assertEqual(rc, 2)
            self.assertIn("invalid input", text)
            self.assertIn("m.txt", text)
            self.assertIn(str(len(maint) - 1), text)
            self.assertNotIn("availability", text)

    def test_a_missing_maintenance_file_is_still_allowed_under_a_lower_cap(self):
        with mock.patch.object(soak, "MAX_MAINTENANCE_BYTES", 4):
            rc, text = self.run_bytes((HEAD + "2026-11-04T21:30:05," + DOWN).encode(), None)
        self.assertEqual(rc, 1)
        self.assertIn("unplanned failures 1", text)

    def test_crlf_inputs_still_parse(self):
        raw = (HEAD + "2026-11-04T21:00:00," + GOOD + "2026-11-04T21:05:00," + GOOD).replace("\n", "\r\n").encode()
        rc, text = self.run_bytes(raw, b"2026-11-04T21:00:00 2026-11-04T22:00:00 tests\r\n")
        self.assertEqual(rc, 0, text)
        self.assertIn("availability outside maintenance: 100.00%", text)


class TrackingFile:
    """An open binary file that records the size of every raw read request it receives."""

    def __init__(self, f, with_metadata=True):
        self._f = f
        self._with_metadata = with_metadata
        self.requests = []
        self.consumed = 0
        self.closed = False

    def readinto(self, b):
        self.requests.append(len(b))
        n = self._f.readinto(b)
        if n:
            self.consumed += n
        return n

    def fileno(self):
        if not self._with_metadata:
            raise OSError("no file descriptor")
        return self._f.fileno()

    def close(self):
        if not self.closed:
            self.file_offset = os.lseek(self._f.fileno(), 0, os.SEEK_CUR)
        self.closed = True
        self._f.close()


class StreamingInputs(unittest.TestCase):
    """Inputs stream in fixed-size chunks under the byte cap instead of one whole-file read."""

    def run_bytes(self, csv_bytes, maint_bytes=b""):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_bytes(csv_bytes)
            m.write_bytes(maint_bytes)
            out = io.StringIO()
            return soak.report(c, m, out), out.getvalue()

    def track_open(self, opened, with_metadata=True):
        def fake_open(path, mode="rb", *args, **kwargs):
            t = TrackingFile(open(path, mode, *args, **kwargs), with_metadata)
            opened[str(path)] = t
            return t
        return mock.patch.object(soak, "open", fake_open, create=True)

    def test_every_raw_read_is_a_fixed_chunk_and_no_read_covers_the_whole_file(self):
        rows = [f"2026-11-04T21:{minute:02d}:00," + GOOD for minute in range(60)]
        csv_bytes = (HEAD + "".join(rows)).encode()
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_bytes(csv_bytes)
            m.write_bytes(b"2026-11-04T21:00:00 2026-11-04T22:00:00 tests\n")
            opened = {}
            with self.track_open(opened), mock.patch.object(soak, "READ_CHUNK_BYTES", 64):
                out = io.StringIO()
                rc = soak.report(c, m, out)
            text = out.getvalue()
            self.assertEqual(rc, 0, text)
            self.assertIn("runs 60", text)
            self.assertIn("availability outside maintenance: 100.00%", text)
            t = opened[str(c)]
            self.assertGreater(len(t.requests), 1)
            self.assertLessEqual(max(t.requests), 64)
            self.assertLess(max(t.requests), len(csv_bytes))  # no single read grabbed the whole file
            self.assertEqual(t.consumed, len(csv_bytes))  # every byte was read exactly once
            self.assertTrue(t.closed)
            self.assertLessEqual(max(opened[str(m)].requests), 64)
            self.assertTrue(opened[str(m)].closed)

    def test_the_first_line_is_consumed_before_the_whole_file_is_read(self):
        rows = [f"2026-11-04T21:{minute:02d}:00," + GOOD for minute in range(60)]
        csv_bytes = (HEAD + "".join(rows)).encode()
        with tempfile.TemporaryDirectory() as d:
            c = pathlib.Path(d, "dns.csv")
            c.write_bytes(csv_bytes)
            opened = {}
            with self.track_open(opened), mock.patch.object(soak, "READ_CHUNK_BYTES", 64):
                text = soak.open_bounded(c, soak.MAX_CSV_BYTES)
                first = text.readline()
                t = opened[str(c)]
                self.assertEqual(first, HEAD)
                self.assertGreater(t.consumed, 0)
                self.assertLess(t.consumed, len(csv_bytes))
                text.close()
            self.assertTrue(t.closed)

    def test_an_oversized_regular_file_is_refused_on_metadata_before_any_byte_is_read(self):
        at_cap = (HEAD + "2026-11-04T21:00:00," + GOOD).encode()
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_bytes(at_cap + b"x" * 10)
            m.write_bytes(b"")
            opened = {}
            with mock.patch.object(soak, "MAX_CSV_BYTES", len(at_cap)), self.track_open(opened):
                out = io.StringIO()
                rc = soak.report(c, m, out)
            text = out.getvalue()
            self.assertEqual(rc, 2, text)
            self.assertIn(f"larger than the {len(at_cap)}-byte input cap", text)
            self.assertEqual(opened[str(c)].consumed, 0)
            self.assertTrue(opened[str(c)].closed)
            self.assertNotIn("runs ", text)
            self.assertNotIn("availability", text)

    def test_cap_exact_and_over_are_enforced_by_counting_without_size_metadata(self):
        at_cap = (HEAD + "2026-11-04T21:00:00," + GOOD).encode()
        over = at_cap + (b"2026-11-04T21:05:00," + GOOD.encode()) * 100
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            m.write_bytes(b"")
            for raw, want_rc in ((at_cap, 0), (over, 2)):
                with self.subTest(size=len(raw)):
                    c.write_bytes(raw)
                    opened = {}
                    with mock.patch.object(soak, "MAX_CSV_BYTES", len(at_cap)), \
                            self.track_open(opened, with_metadata=False):
                        out = io.StringIO()
                        rc = soak.report(c, m, out)
                    text = out.getvalue()
                    self.assertEqual(rc, want_rc, text)
                    t = opened[str(c)]
                    self.assertTrue(t.closed)
                    if want_rc == 0:
                        self.assertIn("runs 1", text)
                        self.assertEqual(t.consumed, len(at_cap))  # EOF check at the exact cap
                        self.assertEqual(t.file_offset, len(at_cap))
                    else:
                        self.assertIn(f"larger than the {len(at_cap)}-byte input cap", text)
                        self.assertEqual(t.consumed, len(at_cap) + 1)  # at most cap+1 bytes consumed
                        self.assertEqual(t.file_offset, len(at_cap) + 1)
                        self.assertNotIn("runs ", text)
                        self.assertNotIn("availability", text)

    def test_multibyte_utf8_and_crlf_split_across_read_boundaries(self):
        row1 = "2026-11-04T21:00:00,NOERROR,3,NOERROR,14,NOERROR\u2603,60,NOERROR+AD,5,SERVFAIL,300"
        row2 = '2026-11-04T21:05:00,"SERVFAIL\u2028x\nEVIL",3,NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300'
        raw = "\r\n".join([HEAD.rstrip("\n"), row1, row2]).encode("utf-8") + b"\r\n"
        maint = b"2026-11-04T22:00:00 2026-11-04T23:00:00 tests\r\n"
        with mock.patch.object(soak, "READ_CHUNK_BYTES", 3):
            rc, text = self.run_bytes(raw, maint)
        self.assertEqual(rc, 1, text)
        self.assertIn("runs 2", text)
        self.assertIn("failed in planned maintenance 0; unplanned failures 2", text)
        self.assertIn("example_udp=NOERROR\u2603", text)
        self.assertIn("health_udp=SERVFAIL\\u2028x\\nEVIL", text)
        self.assertNotIn("SERVFAIL\u2028x\nEVIL", text)

    def test_bare_cr_separators_keep_physical_line_numbers_across_chunks(self):
        raw = (HEAD.rstrip("\n") + "\r" + "2026-11-04T21:00:00," + GOOD.rstrip("\n")
               + "\r" + "2026-11-04T21:05:00,NOERROR\r").encode()
        with mock.patch.object(soak, "READ_CHUNK_BYTES", 5):
            rc, text = self.run_bytes(raw)
        self.assertEqual(rc, 2, text)
        self.assertIn("got 2", text)
        self.assertIn(":3:", text)

    def test_a_quoted_multiline_field_spanning_chunks_keeps_physical_line_numbers(self):
        row = '2026-11-04T21:00:00,"NOERROR\nEVIL",3,NOERROR,14,NOERROR,60,NOERROR+AD,5,SERVFAIL,300\n'
        raw = (HEAD + row + "not-a-time," + GOOD).encode()
        with mock.patch.object(soak, "READ_CHUNK_BYTES", 7):
            rc, text = self.run_bytes(raw)
        self.assertEqual(rc, 2, text)
        self.assertIn("bad timestamp", text)
        self.assertIn(":4:", text)

    def test_late_invalid_csv_yields_no_partial_report_and_still_closes_the_file(self):
        good = "2026-11-04T21:00:00," + GOOD
        for raw in ((HEAD + good + "2026-11-04T21:05:00," + GOOD).encode() + b"\xff\xfe\n",
                    (HEAD + good + "not-a-time," + GOOD).encode()):
            with self.subTest(tail=raw[-12:]):
                with tempfile.TemporaryDirectory() as d:
                    c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
                    c.write_bytes(raw)
                    m.write_bytes(b"")
                    opened = {}
                    with self.track_open(opened), mock.patch.object(soak, "READ_CHUNK_BYTES", 16):
                        out = io.StringIO()
                        rc = soak.report(c, m, out)
                    text = out.getvalue()
                    self.assertEqual(rc, 2, text)
                    self.assertIn("invalid input", text)
                    for forbidden in ("runs ", "availability", "UNPLANNED", "coverage"):
                        self.assertNotIn(forbidden, text)
                    self.assertTrue(opened[str(c)].closed)

    def test_late_invalid_maintenance_yields_no_partial_report_and_still_closes_the_file(self):
        maint = b"2026-11-04T21:00:00 2026-11-04T22:00:00 ok\nnope 2026-11-04T23:00:00 x\n"
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_bytes((HEAD + "2026-11-04T21:00:00," + GOOD).encode())
            m.write_bytes(maint)
            opened = {}
            with self.track_open(opened), mock.patch.object(soak, "READ_CHUNK_BYTES", 8):
                out = io.StringIO()
                rc = soak.report(c, m, out)
            text = out.getvalue()
            self.assertEqual(rc, 2, text)
            self.assertIn("bad maintenance time", text)
            self.assertNotIn("runs ", text)
            self.assertNotIn("availability", text)
            self.assertTrue(opened[str(m)].closed)

    def test_missing_file_error_during_read_is_invalid_maintenance_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_bytes((HEAD + "2026-11-04T21:00:00," + GOOD).encode())
            m.write_text("# planned windows\n")

            def open_for_read(path, *args, **kwargs):
                raw = open(path, *args, **kwargs)
                if path == m:
                    raw.readinto = mock.Mock(side_effect=FileNotFoundError("input became unavailable"))
                return raw

            out = io.StringIO()
            with mock.patch.object(soak, "open", open_for_read, create=True):
                rc = soak.report(c, m, out)
        self.assertEqual(rc, 2, out.getvalue())
        self.assertIn("m.txt", out.getvalue())
        self.assertNotIn("availability", out.getvalue())


class Coverage(unittest.TestCase):
    def test_gap_over_max_is_missing_coverage_not_an_outage(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:10:00," + GOOD], max_gap=360)
        self.assertEqual(rc, 2)
        self.assertIn("unplanned failures 0", text)
        self.assertIn("coverage gap", text)
        self.assertIn("600s > 360s", text)
        self.assertIn("within recorded endpoints", text)

    def test_interval_exactly_at_max_is_covered(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:06:00," + GOOD], max_gap=360)
        self.assertEqual(rc, 0)
        self.assertIn("coverage: OK", text)
        self.assertIn("360s <= 360s", text)

    def test_one_second_over_max_is_a_gap(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:06:01," + GOOD], max_gap=360)
        self.assertEqual(rc, 2)
        self.assertIn("361s > 360s", text)

    def test_fractional_gap_is_reported_at_its_observed_precision(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:06:00.000001," + GOOD], max_gap=360)
        self.assertEqual(rc, 2)
        self.assertIn("360.000001s > 360s", text)

    def test_one_round_does_not_claim_interval_coverage(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD], max_gap=360)
        self.assertEqual(rc, 0)
        self.assertIn("no adjacent rounds", text)
        self.assertNotIn("coverage: OK", text)

    def test_duplicate_timestamps_are_ambiguous_evidence(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:00:00," + GOOD], max_gap=360)
        self.assertEqual(rc, 2)
        self.assertIn("duplicate", text)

    def test_backwards_timestamps_are_ambiguous_evidence(self):
        rc, text = run(["2026-11-04T21:05:00," + GOOD, "2026-11-04T21:00:00," + GOOD], max_gap=360)
        self.assertEqual(rc, 2)
        self.assertIn("before the previous", text)

    def test_timestamps_are_normalized_to_utc_before_comparing(self):
        rc, text = run(["2026-11-04T22:00:00+01:00," + GOOD, "2026-11-04T21:05:00Z," + GOOD], max_gap=360)
        self.assertEqual(rc, 0)
        self.assertIn("coverage: OK", text)
        self.assertIn("300s <= 360s", text)

    def test_gap_and_unplanned_failure_returns_2_and_keeps_the_evidence(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:10:00," + DOWN], max_gap=360)
        self.assertEqual(rc, 2)
        self.assertIn("UNPLANNED 2026-11-04T21:10:00", text)
        self.assertIn("coverage gap", text)

    def test_a_gap_is_not_turned_into_observed_evidence_by_a_window(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:10:00," + DOWN],
                       "2026-11-04T21:09:00 2026-11-04T21:11:00 tests\n", max_gap=360)
        self.assertEqual(rc, 2)
        self.assertIn("failed in planned maintenance 1; unplanned failures 0", text)
        self.assertIn("coverage gap", text)

    def test_unplanned_failure_with_satisfied_coverage_returns_1(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:05:00," + DOWN], max_gap=360)
        self.assertEqual(rc, 1)
        self.assertIn("UNPLANNED", text)
        self.assertIn("coverage: OK", text)

    def test_without_the_option_a_long_interval_is_not_a_gap(self):
        rc, text = run(["2026-11-04T21:00:00," + GOOD, "2026-11-04T21:10:00," + GOOD])
        self.assertEqual(rc, 0)
        self.assertNotIn("coverage", text)

    def test_report_requires_a_positive_finite_max_gap(self):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_text(HEAD + "2026-11-04T21:00:00," + GOOD)
            m.write_text("")
            for threshold in (0, float("nan"), float("inf")):
                with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                    soak.report(c, m, io.StringIO(), max_gap_seconds=threshold)


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(REPORT), *args],
                              capture_output=True, text=True, timeout=30)

    def test_help_documents_the_option_and_exit_codes(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--max-gap-seconds", r.stdout)
        self.assertIn("Exit codes", r.stdout)

    def test_gap_on_the_command_line_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_text(HEAD + "2026-11-04T21:00:00," + GOOD + "2026-11-04T21:10:00," + GOOD)
            m.write_text("")
            r = self.run_cli("--max-gap-seconds", "360", str(c), str(m))
        self.assertEqual(r.returncode, 2)
        self.assertIn("coverage gap", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_cli_requires_a_positive_finite_max_gap(self):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_text(HEAD + "2026-11-04T21:00:00," + GOOD)
            m.write_text("")
            for threshold in ("0", "nan", "inf", "1e999"):
                with self.subTest(threshold=threshold):
                    r = self.run_cli("--max-gap-seconds", threshold, str(c), str(m))
                    self.assertEqual(r.returncode, 2)
                    self.assertIn("--max-gap-seconds", r.stderr)

    def test_header_only_on_the_command_line_exits_2_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "dns.csv"), pathlib.Path(d, "m.txt")
            c.write_text(HEAD)
            m.write_text("")
            r = self.run_cli(str(c), str(m))
        self.assertEqual(r.returncode, 2)
        self.assertIn("no observation", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_cli_escapes_control_characters_in_a_path_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            c, m = pathlib.Path(d, "evil\nname.csv"), pathlib.Path(d, "m.txt")
            m.write_text("")
            r = self.run_cli(str(c), str(m))
        self.assertEqual(r.returncode, 2)
        self.assertIn("evil\\nname.csv", r.stdout)
        self.assertNotIn("evil\nname.csv", r.stdout)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
