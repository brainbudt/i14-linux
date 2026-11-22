"""scripts/i14-touch offline: os/fcntl mocked, no uinput, no phone touched (runs on macOS)."""
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import signal
import socket
import threading
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("i14_touch", str(ROOT / "scripts/i14-touch"))
_spec = importlib.util.spec_from_loader("i14_touch", _loader)
t = importlib.util.module_from_spec(_spec)
_loader.exec_module(t)

EV = struct.Struct("<qqHHi")


def decode(data):
    assert len(data) % 24 == 0
    return [EV.unpack_from(data, i)[2:] for i in range(0, len(data), 24)]


class FakeDevice:
    def __init__(self):
        self.taps = []
        self.fail = None

    def create(self):
        pass

    def tap(self, x, y):
        if self.fail:
            raise self.fail
        self.taps.append((x, y))
        if getattr(self, "after", None):
            self.after()

    def destroy(self):
        pass


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Descriptor(unittest.TestCase):
    def test_layout_matches_uinput_user_dev(self):
        d = t.descriptor()
        self.assertEqual(len(d), 80 + 8 + 4 + 4 * 64 * 4)  # 1116
        self.assertEqual(d[:80].rstrip(b"\0"), b"I14 test tap injector")
        self.assertEqual(struct.unpack_from("<HHHHI", d, 80), (0x06, 0x1234, 0x5678, 1, 0))
        absmax = struct.unpack_from("<64i", d, 92)
        self.assertEqual(absmax[0x35], 719)
        self.assertEqual(absmax[0x36], 1599)
        self.assertEqual(absmax[0x00], 719)
        self.assertEqual(absmax[0x01], 1599)
        self.assertEqual(absmax[0x2F], 9)
        self.assertEqual(absmax[0x39], 65535)
        self.assertEqual(sum(1 for v in absmax if v), 6)
        self.assertEqual(d[92 + 256:], bytes(3 * 256))  # absmin, absfuzz, absflat all 0

    def test_ioctl_numbers(self):
        # _IOW('U', n, int) and _IO('U', n)
        iow = lambda n: (1 << 30) | (4 << 16) | (ord("U") << 8) | n
        self.assertEqual(t.UI_SET_EVBIT, iow(100))
        self.assertEqual(t.UI_SET_KEYBIT, iow(101))
        self.assertEqual(t.UI_SET_ABSBIT, iow(103))
        self.assertEqual(t.UI_DEV_CREATE, (ord("U") << 8) | 1)
        self.assertEqual(t.UI_DEV_DESTROY, (ord("U") << 8) | 2)


class Events(unittest.TestCase):
    def test_tap_bytes_protocol_b(self):
        down, up = t.tap_events(360, 375, 7)
        self.assertEqual(len(down), 8 * 24)
        self.assertEqual(decode(down), [
            (3, 0x2F, 0), (3, 0x39, 7), (3, 0x35, 360), (3, 0x36, 375),
            (3, 0x00, 360), (3, 0x01, 375), (1, 330, 1), (0, 0, 0)])
        self.assertEqual(decode(up), [(3, 0x2F, 0), (3, 0x39, -1), (1, 330, 0), (0, 0, 0)])
        # first event, byte for byte: zero timeval, type 3, code 0x2f, value 0 (little endian)
        self.assertEqual(down[:24], bytes(16) + b"\x03\x00\x2f\x00" + bytes(4))
        self.assertEqual(up[24 + 20:48], b"\xff\xff\xff\xff")

    def test_both_uis_would_read_this_tap(self):
        """Feed the bytes through the Python UI's own parser (rootfs i14ui/inputdev.py)."""
        spec = importlib.util.spec_from_file_location(
            "inputdev", ROOT / "rootfs/overlay/usr/lib/i14ui/inputdev.py")
        inputdev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inputdev)
        touch, gestures = inputdev.Touch(), []
        for n, xy in enumerate([(360, 375), (190, 1065), (360, 375)], 1):
            down, up = t.tap_events(*xy, n)
            for _, et, code, val in inputdev.events(down + up):
                g = touch.feed(et, code, val)
                if g:
                    gestures.append(g)
        self.assertEqual(gestures, [("down", 360, 375), ("up", 360, 375),
                                    ("down", 190, 1065), ("up", 190, 1065),
                                    ("down", 360, 375), ("up", 360, 375)])
        self.assertEqual(inputdev.EVENT.size, 24)


class Bounds(unittest.TestCase):
    def test_accepts_corners(self):
        for xy in ((0, 0), (719, 0), (0, 1599), (719, 1599)):
            self.assertEqual(t.check_xy(*xy), xy)

    def test_rejects_out_of_range_and_non_int(self):
        for xy in ((-1, 0), (0, -1), (720, 0), (0, 1600), (True, 5), (1.0, 2), ("1", 2)):
            with self.assertRaises(t.Refused, msg=xy):
                t.check_xy(*xy)
        with self.assertRaises(t.Refused):
            t.tap_events(720, 0, 1)

    def test_cli_parse(self):
        self.assertEqual(t.parse_xy("0", "1599"), (0, 1599))
        for sx, sy in (("720", "1"), ("-1", "1"), ("01", "1"), ("1.5", "1"), (" 1", "1"),
                       ("1e2", "1"), ("0x10", "1"), ("", "1"), ("1", "99999")):
            with self.assertRaises(t.Refused, msg=(sx, sy)):
                t.parse_xy(sx, sy)


class Protocol(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(t.parse_request(b"tap 360 375\n"), (360, 375))
        self.assertEqual(t.parse_request(b"tap 0 0\n"), (0, 0))
        self.assertEqual(t.parse_request(b"tap 719 1599\n"), (719, 1599))

    def test_rejected(self):
        bad = [b"tap 720 0\n", b"tap 0 1600\n", b"tap -1 0\n", b"tap 01 0\n", b"tap 1 2",
               b"tap 1 2\n\n", b"tap  1 2\n", b"TAP 1 2\n", b"tap 1 2 3\n", b"tap 1\n",
               b"tap 1 2\r\n", b"ping\n", b"", b"tap 1 2\n" + b" " * 40, "tap 1 2\n",
               b"tap \xd9\xa1 2\n", b"stop\n"]
        for req in bad:
            with self.assertRaises(t.Refused, msg=req):
                t.parse_request(req)


class DeviceIO(unittest.TestCase):
    def setUp(self):
        self.ioctl = mock.patch.object(t.fcntl, "ioctl").start()
        self.open = mock.patch.object(t.os, "open", return_value=42).start()
        self.written = []
        self.write_result = None  # None: whole write; int: short; Exception: raise

        def write(fd, b):
            r = self.write_result(b) if callable(self.write_result) else None
            if isinstance(r, BaseException):
                raise r
            self.written.append((fd, b))
            return len(b) if r is None else r

        mock.patch.object(t.os, "write", side_effect=write).start()
        self.close = mock.patch.object(t.os, "close").start()
        self.addCleanup(mock.patch.stopall)

    def destroys(self):
        return [c for c in self.ioctl.call_args_list if c[0][1] == t.UI_DEV_DESTROY]

    def test_create_tap_destroy(self):
        d = t.Device()
        d.create()
        self.assertEqual(self.open.call_args[0][0], "/dev/uinput")
        calls = [c[0][1:] for c in self.ioctl.call_args_list]
        self.assertEqual(calls[:3], [(t.UI_SET_EVBIT, 0), (t.UI_SET_EVBIT, 1), (t.UI_SET_EVBIT, 3)])
        self.assertIn((t.UI_SET_KEYBIT, 330), calls)
        for axis in (0, 1, 0x2F, 0x35, 0x36, 0x39):
            self.assertIn((t.UI_SET_ABSBIT, axis), calls)
        self.assertEqual(calls[-1], (t.UI_DEV_CREATE,))
        self.assertEqual(self.written, [(42, t.descriptor())])
        self.written.clear()
        sleeps = []
        d.tap(10, 20, sleep=sleeps.append)
        self.assertEqual(sleeps, [t.HOLD, t.SETTLE])
        self.assertEqual([w[1] for w in self.written], list(t.tap_events(10, 20, 1)))
        d.tap(11, 21, sleep=lambda s: None)
        self.assertEqual(decode(self.written[2][1])[1], (3, 0x39, 2))  # new tracking id
        d.destroy()
        d.destroy()  # idempotent
        self.assertEqual(len(self.destroys()), 1)
        self.close.assert_called_once_with(42)

    def test_lift_is_written_even_if_sleep_is_interrupted(self):
        d = t.Device()
        d.create()
        self.written.clear()

        def boom(_):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            d.tap(1, 2, sleep=boom)
        self.assertEqual(len(self.written), 2)
        self.assertEqual(decode(self.written[1][1])[1], (3, 0x39, -1))

    def test_failed_down_write_destroys_the_device(self):
        d = t.Device()
        d.create()
        self.written.clear()
        _, up = t.tap_events(1, 2, 1)
        self.write_result = lambda b: OSError(5, "EIO") if b != up else None
        with self.assertRaises(t.DeviceLost):
            d.tap(1, 2, sleep=lambda s: None)
        self.assertEqual([w[1] for w in self.written], [up])  # best-effort lift first
        self.assertEqual(len(self.destroys()), 1)
        self.close.assert_called_once_with(42)
        self.assertFalse(d.created)
        with self.assertRaises(t.DeviceLost):
            d.tap(1, 2, sleep=lambda s: None)

    def test_short_down_write_destroys_the_device(self):
        d = t.Device()
        d.create()
        self.write_result = lambda b: 24  # one event of eight
        with self.assertRaises(t.DeviceLost):
            d.tap(1, 2, sleep=lambda s: None)
        self.assertEqual(len(self.destroys()), 1)

    def test_failed_lift_write_destroys_the_device(self):
        d = t.Device()
        d.create()
        _, up = t.tap_events(3, 4, 1)
        self.write_result = lambda b: OSError(19, "ENODEV") if b == up else None
        sleeps = []
        with self.assertRaises(t.DeviceLost):
            d.tap(3, 4, sleep=sleeps.append)
        self.assertEqual(sleeps, [t.HOLD])  # no settle, no ack
        self.assertEqual(len(self.destroys()), 1)
        self.close.assert_called_once_with(42)

    def test_failed_setup_closes_without_destroy(self):
        self.ioctl.side_effect = lambda fd, req, *a: (_ for _ in ()).throw(OSError(22, "bad")) \
            if req == t.UI_DEV_CREATE else None
        d = t.Device()
        with self.assertRaises(OSError):
            d.create()
        self.assertEqual(self.destroys(), [])
        self.close.assert_called_once_with(42)

    def test_bounds_checked_before_write(self):
        d = t.Device()
        d.create()
        self.written.clear()
        with self.assertRaises(t.Refused):
            d.tap(720, 0, sleep=lambda s: None)
        self.assertEqual(self.written, [])


class DaemonLogic(unittest.TestCase):
    def setUp(self):
        mock.patch.object(t, "log").start()
        self.addCleanup(mock.patch.stopall)
        self.clock = Clock()
        self.dev = FakeDevice()
        self.sleeps = []

        def sleep(s):
            self.sleeps.append(s)
            self.clock.now += s

        self.d = t.Daemon(lifetime=10800, idle=3600, device=self.dev, clock=self.clock,
                          sleep=sleep)

    def test_tap_ok_and_bad(self):
        self.assertEqual(self.d.handle(b"tap 360 375\n"), b"ok\n")
        self.assertEqual(self.dev.taps, [(360, 375)])
        self.clock.now += 1
        self.assertTrue(self.d.handle(b"tap 720 1\n").startswith(b"err "))
        self.assertTrue(self.d.handle(b"rm -rf /\n").startswith(b"err "))
        self.assertEqual(self.dev.taps, [(360, 375)])
        self.assertTrue(self.d.handle(b"ping\n").startswith(b"pong "))

    def test_device_lost_is_fatal(self):
        self.dev.fail = t.DeviceLost("lift write failed: ENODEV")
        reply = self.d.handle(b"tap 1 1\n")
        self.assertTrue(reply.startswith(b"err lift write failed"), reply)
        self.assertTrue(self.d.stop)
        self.d.sock = mock.Mock()
        self.assertTrue(self.d.run().startswith("device lost"))
        self.d.sock.accept.assert_not_called()
        self.dev.fail = None
        self.assertTrue(self.d.handle(b"tap 1 1\n").startswith(b"err daemon is stopping"))
        self.assertEqual(self.dev.taps, [])

    def test_back_to_back_taps_wait_instead_of_failing(self):
        self.assertEqual(self.d.handle(b"tap 1 1\n"), b"ok\n")
        self.assertEqual(self.d.handle(b"tap 2 2\n"), b"ok\n")
        self.assertEqual(self.sleeps, [t.MIN_INTERVAL])
        self.clock.now += 0.1
        self.assertEqual(self.d.handle(b"tap 3 3\n"), b"ok\n")
        self.assertAlmostEqual(self.sleeps[-1], t.MIN_INTERVAL - 0.1)
        self.assertEqual(self.dev.taps, [(1, 1), (2, 2), (3, 3)])

    def test_minute_budget(self):
        replies = [self.d.handle(b"tap 1 1\n") for _ in range(t.MAX_PER_MINUTE + 3)]
        self.assertEqual(replies.count(b"ok\n"), t.MAX_PER_MINUTE)
        self.assertTrue(replies[-1].startswith(b"err rate limit"))
        self.clock.now += 61
        self.assertEqual(self.d.handle(b"tap 1 1\n"), b"ok\n")

    def test_idle_and_lifetime(self):
        self.assertIsNone(self.d.expired())
        self.clock.now += 3599
        self.assertEqual(self.d.handle(b"tap 1 1\n"), b"ok\n")  # resets idle
        self.clock.now += 3599
        self.assertIsNone(self.d.expired())
        self.clock.now += 1
        self.assertEqual(self.d.expired(), "idle")
        self.assertTrue(self.d.handle(b"tap 1 1\n").startswith(b"err daemon is stopping (idle)"))
        self.assertEqual(len(self.dev.taps), 1)
        d = t.Daemon(lifetime=100, idle=3600, device=self.dev, clock=self.clock)
        self.clock.now += 100
        self.assertEqual(d.expired(), "lifetime")

    def test_no_tap_after_stop_even_mid_request(self):
        self.d.stop = True
        self.assertTrue(self.d.handle(b"tap 1 1\n").startswith(b"err daemon is stopping (signal)"))
        self.assertEqual(self.dev.taps, [])

    def test_expiry_during_rate_wait_is_rechecked(self):
        self.assertEqual(self.d.handle(b"tap 1 1\n"), b"ok\n")
        self.d.idle = 0.1  # the rate wait (0.25 s) crosses the idle limit
        self.assertTrue(self.d.handle(b"tap 1 1\n").startswith(b"err daemon is stopping"))
        self.assertEqual(len(self.dev.taps), 1)

    def test_serve_one_over_a_socket(self):
        a, b = socket.socketpair()
        a.sendall(b"tap 5 6\n")
        self.d.serve_one(b)
        self.assertEqual(a.recv(64), b"ok\n")
        a.close()

    def test_serve_one_cuts_long_requests(self):
        a, b = socket.socketpair()
        a.sendall(b"x" * 200)
        self.d.serve_one(b)
        self.assertEqual(a.recv(64), b"err bad request\n")
        self.assertEqual(self.dev.taps, [])
        a.close()

    def test_slow_client_is_bounded_by_an_absolute_deadline(self):
        a, b = socket.socketpair()
        stop = threading.Event()

        def drip():  # one byte every 50 ms: each recv is fast, the whole request is not
            for ch in b"tap 1 1":
                if stop.wait(0.05):
                    return
                try:
                    a.send(bytes([ch]))
                except OSError:
                    return

        th = threading.Thread(target=drip)
        th.start()
        with mock.patch.object(t, "REQUEST_TIMEOUT", 0.2):
            t0 = t.time.monotonic()
            self.d.serve_one(b)
            took = t.time.monotonic() - t0
        stop.set()
        th.join(2)
        self.assertLess(took, 1.0)
        self.assertEqual(a.recv(64), b"err request timeout\n")
        self.assertEqual(self.dev.taps, [])
        a.close()

    def test_signal_stops_the_loop(self):
        self.d.sock = mock.Mock()
        self.d.sock.accept.side_effect = socket.timeout
        self.d._signal(signal.SIGTERM, None)
        self.assertEqual(self.d.run(), "signal")


class ProcStat(unittest.TestCase):
    def test_start_time_is_field_22_even_with_odd_comm(self):
        fields = " ".join(str(n) for n in range(3, 53)).replace("3 ", "S ", 1)
        stat = f"4242 (py (x) y) {fields}\n".encode()
        with mock.patch("builtins.open", mock.mock_open(read_data=stat)):
            self.assertEqual(t.proc_start(4242), 22)

    def test_missing_process(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertIsNone(t.proc_start(4242))
        with mock.patch("builtins.open", mock.mock_open(read_data=b"garbage")):
            self.assertIsNone(t.proc_start(4242))


class PinGate(unittest.TestCase):
    SECRET = "c0ffee1234deadbeef"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "ui.json")

    def gate(self, raw):
        with open(self.path, "wb") as f:
            f.write(raw if isinstance(raw, bytes) else raw.encode())
        ok, msg = t.pin_gate(self.path)
        self.assertNotIn(self.SECRET, msg)
        self.assertNotIn("salt", msg)
        return ok

    def test_missing_file_is_no_pin(self):
        self.assertEqual(t.pin_gate(self.path), (True, "no settings file: no PIN"))

    def test_accepted_like_rust(self):
        for raw in ('{}', '{"pin": null}', ' {"pin":null}\n',
                    '{"brightness": 120, "timeout": 60, "pin": null, "charge_limit_enabled": false, '
                    '"charge_limit": 80}',
                    '{"pin": null, "timeout": "x", "brightness": [1], "unknown": {"a": [1, 2.5e3]}}',
                    '{"name": "\\ud83d\\ude00", "n": 1e300, "big": 123456789012345678901234567890}'):
            self.assertTrue(self.gate(raw), raw)

    def test_pin_or_bad_pin_entry_stops(self):
        for pin in (f'{{"salt": "{self.SECRET}", "hash": "{self.SECRET}", "rounds": 1}}',
                    '{"salt": "00"}', '5', '"1234"', 'false', '[]', '{}'):
            self.assertFalse(self.gate('{"pin": %s}' % pin), pin)

    def test_rejected_by_serde_json_stops(self):
        for raw in ('{"pin": null, "brightness": NaN}', '{"pin": null, "x": Infinity}',
                    '{"pin": null, "x": -Infinity}', '{"pin": null, "x": 1e400}',
                    '{"pin": null, "x": "\\ud800"}', '{"pin": null, "\\udc00": 1}',
                    '{"pin": null, "x": "\\ude00\\ud83d"}',
                    '{"pin": null, "pin": null}', '{"pin": null} x', '{"pin": null,}',
                    "{'pin': null}", '', '   ', '{"pin": null, "x": "a\tb"}',
                    '\ufeff{"pin": null}', '[' * 200 + ']' * 200,
                    '{"pin": null, "x": ' + '[' * 3000 + ']' * 3000 + '}'):
            self.assertFalse(self.gate(raw), raw[:60])

    def test_not_an_object_stops(self):
        for raw in ('null', '[]', '"x"', '1', 'true'):
            self.assertFalse(self.gate(raw), raw)

    def test_bad_utf8_stops(self):
        self.assertFalse(self.gate(b'{"pin": null, "x": "\xff"}'))
        self.assertFalse(self.gate(b'{"pin": null, "x": "\xed\xa0\x80"}'))  # UTF-8 surrogate

    def test_unreadable_stops(self):
        os.mkdir(self.path)  # read() fails with something other than NotFound
        ok, msg = t.pin_gate(self.path)
        self.assertFalse(ok)
        self.assertIn("unreadable", msg)

    def test_cli_prints_no_pin_data(self):
        with open(self.path, "w") as f:
            f.write(f'{{"pin": {{"salt": "{self.SECRET}", "hash": "{self.SECRET}", "rounds": 9}}}}')
        out = io.StringIO()
        with mock.patch.object(t, "SETTINGS", self.path), redirect_stdout(out):
            self.assertEqual(t.main(["pin-gate"]), 1)
        self.assertEqual(out.getvalue(), "STOP: a PIN is configured\n")
        os.unlink(self.path)
        out = io.StringIO()
        with mock.patch.object(t, "SETTINGS", self.path), redirect_stdout(out):
            self.assertEqual(t.main(["pin-gate"]), 0)
        self.assertTrue(out.getvalue().startswith("OK: "))


class FakeDaemonLock:
    """Plays the daemon's side of the pid-file lock in this process."""

    def __init__(self, path, pid=4242):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        t.fcntl.flock(self.fd, t.fcntl.LOCK_EX | t.fcntl.LOCK_NB)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"{pid}\n".encode())

    def release(self):
        if self.fd is not None:
            os.ftruncate(self.fd, 0)
            os.close(self.fd)
            self.fd = None


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = pathlib.Path(self.tmp.name)
        self.pid, self.sock, self.lock = p / "i14-touch.pid", p / "i14-touch.sock", p / "i14-touch.lock"
        mock.patch.object(t, "PIDFILE", str(self.pid)).start()
        mock.patch.object(t, "SOCK", str(self.sock)).start()
        mock.patch.object(t, "LOCKFILE", str(self.lock)).start()
        mock.patch.object(t, "log").start()
        mock.patch.object(t, "EXIT_WAIT", 0.5).start()
        mock.patch.object(t.os, "geteuid", return_value=0).start()
        mock.patch.object(t.os, "uname", return_value=mock.Mock(release="3.18.19")).start()
        mock.patch.object(t.platform, "machine", return_value="aarch64").start()
        self.daemons = []
        self.addCleanup(lambda: [d.release() for d in self.daemons])
        self.kills = []

        def kill(pid, sig):  # SIGTERM to our fake daemon: it exits (releases its lock)
            self.kills.append((pid, sig))
            for d in self.daemons:
                d.release()

        mock.patch.object(t.os, "kill", side_effect=kill).start()
        self.is_ours = mock.patch.object(t, "is_ours", return_value=True).start()
        self.proc_start = mock.patch.object(t, "proc_start", return_value=777).start()
        self.addCleanup(mock.patch.stopall)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = t.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def fake_spawn(self, gate=None, entered=None, result=b"ok"):
        def spawn(lifetime, idle):
            self.daemons.append(FakeDaemonLock(str(self.pid)))
            if entered:
                entered.set()
            if gate:
                gate.wait(5)
            return result
        return spawn

    def test_start_then_start_refuses(self):
        with mock.patch.object(t, "spawn", side_effect=self.fake_spawn()) as sp:
            self.assertEqual(self.run_main(["start"])[0], 0)
            rc, _, err = self.run_main(["start"])
        self.assertEqual(rc, 1)
        self.assertIn("already running (pid 4242)", err)
        self.assertEqual(sp.call_count, 1)

    def test_concurrent_start_waits_then_refuses(self):
        gate, entered = threading.Event(), threading.Event()
        results = {}
        with mock.patch.object(t, "spawn", side_effect=self.fake_spawn(gate, entered)) as sp:
            a = threading.Thread(target=lambda: results.setdefault("a", t.main(["start"])))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                a.start()
                self.assertTrue(entered.wait(5))
                with mock.patch.object(t, "LIFECYCLE_WAIT", 0.2):
                    results["busy"] = t.main(["start"])  # first still starting: lock busy
                b = threading.Thread(target=lambda: results.setdefault("b", t.main(["start"])))
                b.start()
                gate.set()
                a.join(5)
                b.join(5)
        self.assertEqual((results["a"], results["busy"], results["b"]), (0, 1, 1))
        self.assertEqual(sp.call_count, 1)

    def test_stop_waits_for_a_start_in_progress_and_kills_it(self):
        gate, entered = threading.Event(), threading.Event()
        order = []
        with mock.patch.object(t, "spawn", side_effect=self.fake_spawn(gate, entered)):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                a = threading.Thread(target=lambda: order.append(("start", t.main(["start"]))))
                a.start()
                self.assertTrue(entered.wait(5))
                # daemon locked the pid file; start has not finished: stop must not slip in
                b = threading.Thread(target=lambda: order.append(("stop", t.main(["stop"]))))
                b.start()
                b.join(0.3)
                self.assertTrue(b.is_alive(), "stop did not wait for the start")
                self.assertEqual(self.kills, [])
                gate.set()
                a.join(5)
                b.join(5)
        self.assertEqual(order, [("start", 0), ("stop", 0)])
        self.assertEqual(self.kills, [(4242, signal.SIGTERM)])
        self.assertFalse(t.alive())

    def test_failed_start_kills_its_half_started_daemon(self):
        with mock.patch.object(t, "spawn", side_effect=self.fake_spawn(result=b"")):
            rc, _, err = self.run_main(["start"])
        self.assertEqual(rc, 1)
        self.assertIn("did not start", err)
        self.assertEqual(self.kills, [(4242, signal.SIGTERM)])
        self.assertFalse(t.alive())

    def test_stop_is_idempotent_and_never_removes_the_lock_files(self):
        self.sock.write_text("")
        for _ in range(2):
            rc, out, _ = self.run_main(["stop"])
            self.assertEqual((rc, out.strip()), (0, "not running"))
        self.assertEqual(self.kills, [])
        self.assertFalse(self.sock.exists())
        self.assertTrue(self.lock.exists())

    def test_stop_ignores_a_stale_pid_file(self):
        self.pid.write_text("1\n")  # nobody holds the lock: not running, pid 1 untouched
        self.assertEqual(self.run_main(["stop"])[:2], (0, "not running\n"))
        self.assertEqual(self.kills, [])

    def test_stop_running_daemon(self):
        self.daemons.append(FakeDaemonLock(str(self.pid)))
        self.sock.write_text("")
        rc, out, _ = self.run_main(["stop"])
        self.assertEqual((rc, out.strip()), (0, "stopped"))
        self.assertEqual(self.kills, [(4242, signal.SIGTERM)])
        self.assertFalse(self.sock.exists())
        self.assertTrue(self.pid.exists())  # kept: its inode is the lock
        self.assertEqual(self.run_main(["stop"])[:2], (0, "not running\n"))

    def test_stop_escalates_to_sigkill(self):
        self.daemons.append(FakeDaemonLock(str(self.pid)))
        sigs = []

        def kill(pid, sig):
            sigs.append(sig)
            if sig == signal.SIGKILL:
                self.daemons[0].release()

        with mock.patch.object(t.os, "kill", side_effect=kill):
            self.assertEqual(self.run_main(["stop"])[0], 0)
        self.assertEqual(sigs, [signal.SIGTERM, signal.SIGKILL])

    def test_no_sigkill_if_the_pid_was_reused(self):
        self.daemons.append(FakeDaemonLock(str(self.pid)))
        sigs = []
        with mock.patch.object(t.os, "kill", side_effect=lambda pid, sig: sigs.append(sig)):
            self.proc_start.side_effect = [777, 999]  # same pid, other process after SIGTERM
            rc, _, err = self.run_main(["stop"])
        self.assertEqual(rc, 1)
        self.assertIn("not escalating", err)
        self.assertEqual(sigs, [signal.SIGTERM])

    def test_no_sigkill_if_the_pid_file_changed(self):
        d = FakeDaemonLock(str(self.pid))
        self.daemons.append(d)
        sigs = []

        def kill(pid, sig):
            sigs.append(sig)
            os.ftruncate(d.fd, 0)
            os.pwrite(d.fd, b"5555\n", 0)

        with mock.patch.object(t.os, "kill", side_effect=kill):
            rc, _, err = self.run_main(["stop"])
        self.assertEqual(rc, 1)
        self.assertEqual(sigs, [signal.SIGTERM])

    def test_no_sigkill_if_identity_cannot_be_read(self):
        self.daemons.append(FakeDaemonLock(str(self.pid)))
        sigs = []
        with mock.patch.object(t.os, "kill", side_effect=lambda pid, sig: sigs.append(sig)):
            self.proc_start.side_effect = [777, None]
            self.assertEqual(self.run_main(["stop"])[0], 1)
        self.assertEqual(sigs, [signal.SIGTERM])

    def test_already_exited_is_success(self):
        d = FakeDaemonLock(str(self.pid))
        self.daemons.append(d)

        def gone(pid, sig):
            d.release()
            raise ProcessLookupError(3, "No such process")

        with mock.patch.object(t.os, "kill", side_effect=gone):
            rc, out, _ = self.run_main(["stop"])
        self.assertEqual((rc, out.strip()), (0, "stopped"))

    def test_esrch_on_sigkill_is_success(self):
        d = FakeDaemonLock(str(self.pid))
        self.daemons.append(d)
        sigs = []

        def kill(pid, sig):
            sigs.append(sig)
            if sig == signal.SIGKILL:
                d.release()
                raise ProcessLookupError(3, "No such process")

        with mock.patch.object(t.os, "kill", side_effect=kill):
            self.assertEqual(self.run_main(["stop"])[0], 0)
        self.assertEqual(sigs, [signal.SIGTERM, signal.SIGKILL])

    def test_stop_without_identity_never_signals(self):
        self.daemons.append(FakeDaemonLock(str(self.pid)))
        self.proc_start.return_value = None
        self.assertEqual(self.run_main(["stop"])[0], 1)
        self.assertEqual(self.kills, [])

    def test_stop_never_kills_a_stranger(self):
        self.daemons.append(FakeDaemonLock(str(self.pid)))
        self.is_ours.return_value = False
        rc, _, err = self.run_main(["stop"])
        self.assertEqual(rc, 1)
        self.assertIn("not an i14-touch daemon", err)
        self.assertEqual(self.kills, [])

    def test_garbage_pidfile_is_not_a_pid(self):
        self.pid.write_text("-1\n")
        self.assertIsNone(t.read_pid())

    def test_requires_root_and_kernel(self):
        with mock.patch.object(t.os, "geteuid", return_value=1000):
            rc, _, err = self.run_main(["tap", "1", "1"])
        self.assertEqual(rc, 1)
        self.assertIn("root", err)
        with mock.patch.object(t.os, "uname", return_value=mock.Mock(release="7.0.0")):
            rc, _, err = self.run_main(["start"])
        self.assertEqual(rc, 1)
        self.assertIn("3.18.19", err)

    def test_start_refuses_wrong_machine_and_limits(self):
        with mock.patch.object(t, "spawn") as sp:
            with mock.patch.object(t.platform, "machine", return_value="x86_64"):
                self.assertEqual(self.run_main(["start"])[0], 1)
            self.assertEqual(self.run_main(["start", "--lifetime", "99999"])[0], 1)
            self.assertEqual(self.run_main(["start", "--idle", "5"])[0], 1)
            self.assertEqual(self.run_main(["start", "--bogus", "5"])[0], 2)
        sp.assert_not_called()

    def test_tap_without_daemon_fails(self):
        rc, _, err = self.run_main(["tap", "1", "1"])
        self.assertEqual(rc, 1)
        self.assertIn("start first", err)

    def test_tap_bad_args_never_connect(self):
        with mock.patch.object(t, "request") as req:
            for argv in (["tap", "720", "1"], ["tap", "1"], ["tap", "1", "2", "3"], ["tap", "a", "b"]):
                self.assertNotEqual(self.run_main(argv)[0], 0, argv)
        req.assert_not_called()

    def test_tap_nonzero_on_err_reply(self):
        with mock.patch.object(t, "request", return_value="err rate limit: 30 taps per minute"):
            self.assertEqual(self.run_main(["tap", "1", "1"])[0], 1)
        with mock.patch.object(t, "request", return_value="ok") as req:
            self.assertEqual(self.run_main(["tap", "360", "375"])[0], 0)
        req.assert_called_once_with(b"tap 360 375\n")

    def test_tap_against_a_live_socket(self):
        # AF_UNIX paths are short on macOS: use a short temp dir
        short = tempfile.mkdtemp(prefix="t", dir="/tmp" if os.path.isdir("/tmp") else None)
        path = os.path.join(short, "s")
        self.addCleanup(lambda: (os.path.exists(path) and os.unlink(path), os.rmdir(short)))
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.addCleanup(srv.close)
        dev = FakeDevice()
        d = t.Daemon(10800, 3600, device=dev)
        th = threading.Thread(target=lambda: d.serve_one(srv.accept()[0]))
        th.start()
        with mock.patch.object(t, "SOCK", path):
            rc, _, _ = self.run_main(["tap", "100", "200"])
        th.join(5)
        self.assertEqual(rc, 0)
        self.assertEqual(dev.taps, [(100, 200)])

    def test_client_deadline_is_absolute(self):
        short = tempfile.mkdtemp(prefix="t", dir="/tmp" if os.path.isdir("/tmp") else None)
        path = os.path.join(short, "s")
        self.addCleanup(lambda: (os.path.exists(path) and os.unlink(path), os.rmdir(short)))
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.addCleanup(srv.close)
        done = threading.Event()

        def dribble():  # a byte every 50 ms for 3 s, never a newline: per-recv timeouts never fire
            c = srv.accept()[0]
            for _ in range(60):
                if done.wait(0.05):
                    break
                try:
                    c.send(b"o")
                except OSError:
                    break
            c.close()

        th = threading.Thread(target=dribble)
        th.start()
        with mock.patch.object(t, "SOCK", path):
            t0 = t.time.monotonic()
            with self.assertRaises(OSError):
                t.request(b"tap 1 1\n", timeout=0.3)
            took = t.time.monotonic() - t0
        done.set()
        th.join(3)
        self.assertLess(took, 1.0)

    def test_usage(self):
        for argv in ([], ["bogus"], ["status", "x"], ["_daemon"]):
            self.assertNotEqual(self.run_main(argv)[0], 0, argv)


if __name__ == "__main__":
    unittest.main()
