"""Callback-only old-LK replay tests; no cryptographic or device proof."""
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "i14_hacc.py"
BASE = 0x10008000


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


class Registers:
    def __init__(self, *, ready=True):
        self.trace = []
        self.values = {}
        self.ready = ready

    def read(self, address):
        self.trace.append(("r", address))
        if address == BASE + 8:
            return 0x8000 if self.ready else 0
        if BASE + 0x50 <= address <= BASE + 0x5C:
            return {0x50: 0x04030201, 0x54: 0x08070605,
                    0x58: 0x0C0B0A09, 0x5C: 0x100F0E0D}[address - BASE]
        return self.values.get(address, 0)

    def write(self, address, value):
        self.trace.append(("w", address, value))
        self.values[address] = value
        return True


def cleanup_fixture():
    return [("w", BASE + 8, 2)] + [
        ("w", BASE + offset, 0)
        for offset in (*range(0x20, 0x40, 4), *range(0x60, 0x80, 4))]


def init_fixture(encrypt):
    # Independent fixture transcribed from the bound old LK register sequence.
    writes = lambda offsets, values: [("w", BASE + o, v)
                                      for o, v in zip(offsets, values)]
    trace = writes(range(0x60, 0x80, 4), [0] * 8)
    trace += writes(range(0x20, 0x40, 4), [0] * 8)
    trace += [("w", BASE + 4, 2), ("w", BASE + 12, 0x10),
              ("r", BASE + 12), ("w", BASE + 12, 0x110),
              ("w", BASE + 8, 2)]
    iv = [0x9ED40400, 0x00E884A1, 0xE3F083BD, 0x2F4E6D8A]
    trace += writes(range(0x40, 0x50, 4), iv)
    pattern = [(0x2D44BB70, 0xA744D227, 0xD0A9864B, 0x83FFC244),
               (0x7EC8266B, 0x43E80FB2, 0x01A6348A, 0x2067F9A0),
               (0x54536405, 0xD546A6B1, 0x1CC3EC3A, 0xDE377A83)]
    for block in pattern:
        trace += writes(range(0x10, 0x20, 4), block)
        trace += [("w", BASE + 8, 1), ("r", BASE + 8)]
    trace += [("w", BASE + 8, 2)]
    trace += writes(range(0x40, 0x50, 4), iv)
    trace += [("w", BASE + 4, 3 if encrypt else 2), ("w", BASE + 12, 0)]
    return trace


class HaccTests(unittest.TestCase):
    def load(self):
        self.assertTrue(SCRIPT.exists(), "HACC replay helper is missing")
        spec = importlib.util.spec_from_file_location("i14_hacc", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_exact_initialization_data_order_and_cleanup_for_both_modes(self):
        h = self.load()
        for encrypt in (False, True):
            with self.subTest(encrypt=encrypt):
                io, clock = Registers(), Clock()
                data = bytes(range(32))
                with patch.object(h.time, "monotonic", clock.now), \
                     patch.object(h.time, "sleep", clock.sleep):
                    output = h.transform(data, encrypt=encrypt,
                                         read32=io.read, write32=io.write)
                prefix = init_fixture(encrypt)
                self.assertEqual(io.trace[:len(prefix)], prefix)
                expected = prefix[:]
                for start in (0, 16):
                    expected += [("w", BASE + offset, word) for offset, word in
                                 zip(range(0x10, 0x20, 4), struct.unpack("<4I", data[start:start+16]))]
                    expected += [("w", BASE + 8, 1), ("r", BASE + 8)]
                    expected += [("r", BASE + offset) for offset in range(0x50, 0x60, 4)]
                expected += cleanup_fixture()
                self.assertEqual(io.trace, expected)
                self.assertEqual(output, bytes(range(1, 17)) * 2)

    def test_invalid_input_has_no_io(self):
        h = self.load()
        for data in (b"", bytes(15), bytes(0x1840), bytearray(16)):
            io = Registers()
            with self.subTest(data_type=type(data), length=len(data)):
                with self.assertRaises((ValueError, TypeError)):
                    h.transform(data, encrypt=True, read32=io.read, write32=io.write)
                self.assertEqual(io.trace, [])
        for kwargs in ({"timeout": 0}, {"timeout": float("inf")},
                       {"ready_timeout": -1}, {"ready_timeout": float("nan")},
                       {"encrypt": 1}):
            io = Registers()
            args = dict(encrypt=True, read32=io.read, write32=io.write)
            args.update(kwargs)
            with self.assertRaises((ValueError, TypeError)):
                h.transform(bytes(16), **args)
            self.assertEqual(io.trace, [])

    def test_responsive_ready_timeout_cleans_up(self):
        h, io, clock = self.load(), Registers(ready=False), Clock()
        with patch.object(h.time, "monotonic", clock.now), \
             patch.object(h.time, "sleep", clock.sleep):
            with self.assertRaisesRegex(h.HaccTimeout, "READY"):
                h.transform(bytes(16), encrypt=True, read32=io.read,
                            write32=io.write, ready_timeout=0.003)
        self.assertEqual(io.trace[-17:], cleanup_fixture())
        self.assertFalse(any(op[0] == "r" and BASE+0x50 <= op[1] <= BASE+0x5C
                             for op in io.trace))
        self.assertLess(len(io.trace), 60)

    def test_late_ready_response_is_still_a_ready_timeout(self):
        h, io, clock = self.load(), Registers(), Clock()
        def read(address):
            result = io.read(address)
            if address == BASE + 8:
                clock.value += 0.6
            return result
        with patch.object(h.time, "monotonic", clock.now):
            with self.assertRaisesRegex(h.HaccTimeout, "READY"):
                h.transform(bytes(16), encrypt=True, read32=read, write32=io.write)
        self.assertEqual(io.trace[-17:], cleanup_fixture())

    def test_ready_poll_cap_terminates_even_with_a_frozen_clock(self):
        h, io, clock = self.load(), Registers(ready=False), Clock()
        with patch.object(h.time, "monotonic", clock.now), \
             patch.object(h.time, "sleep", lambda duration: None):
            with self.assertRaisesRegex(h.HaccTimeout, "poll limit"):
                h.transform(bytes(16), encrypt=True, read32=io.read, write32=io.write)
        self.assertEqual(sum(op == ("r", BASE + 8) for op in io.trace), 4096)
        self.assertEqual(io.trace[-17:], cleanup_fixture())

    def test_read_transport_exception_stops_without_cleanup(self):
        h, io = self.load(), Registers()
        def read(address):
            io.trace.append(("r", address))
            raise OSError("lost reply")
        with self.assertRaisesRegex(h.HaccError, "channel untrusted"):
            h.transform(bytes(16), encrypt=True, read32=read, write32=io.write)
        self.assertEqual(io.trace[-1], ("r", BASE + 12))
        self.assertEqual(len(io.trace), 19)

    def test_maximum_body_is_finite_and_keeps_output_length(self):
        h, io = self.load(), Registers()
        output = h.transform(bytes(0x1830), encrypt=True, read32=io.read, write32=io.write)
        self.assertEqual(len(output), 0x1830)
        self.assertEqual(io.trace[-17:], cleanup_fixture())

    def test_transport_or_ack_failure_stops_all_later_io(self):
        h = self.load()
        for failure in ("raise", None, False, 1):
            io = Registers()
            def write(address, value):
                io.trace.append(("w", address, value))
                if failure == "raise":
                    raise OSError("transport lost")
                return failure
            with self.subTest(failure=failure):
                with self.assertRaises(h.HaccError):
                    h.transform(bytes(16), encrypt=True, read32=io.read, write32=write)
                self.assertEqual(len(io.trace), 1)

    def test_invalid_read_stops_without_cleanup(self):
        h = self.load()
        for value in (-1, 2**32, True, None):
            io = Registers()
            def read(address):
                io.trace.append(("r", address))
                return value
            with self.subTest(value=value):
                with self.assertRaises(h.HaccError):
                    h.transform(bytes(16), encrypt=True, read32=read, write32=io.write)
                self.assertEqual(io.trace[-1], ("r", BASE + 12))
                self.assertEqual(len(io.trace), 19)

    def test_total_deadline_stops_io_and_reports_incomplete_cleanup(self):
        h, io, clock = self.load(), Registers(), Clock()
        def write(address, value):
            result = io.write(address, value)
            clock.value += 0.06
            return result
        with patch.object(h.time, "monotonic", clock.now):
            with self.assertRaisesRegex(h.HaccTimeout, "cleanup incomplete"):
                h.transform(bytes(16), encrypt=True, read32=io.read,
                            write32=write, timeout=0.1)
        self.assertEqual(len(io.trace), 2)

    def test_cleanup_transport_failure_stops_its_remaining_writes(self):
        h, io = self.load(), Registers()
        prefix_length = len(init_fixture(True)) + 10  # one data block and outputs
        def write(address, value):
            result = io.write(address, value)
            if len(io.trace) == prefix_length + 1:
                return False
            return result
        with self.assertRaisesRegex(h.HaccError, "cleanup incomplete"):
            h.transform(bytes(16), encrypt=True, read32=io.read, write32=write)
        self.assertEqual(len(io.trace), prefix_length + 1)


if __name__ == "__main__":
    unittest.main()
