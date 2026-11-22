"""Offline capture tests. No USB scan, address bind or phone connection occurs.

Real files, fsync, flock, parser and orchestration are exercised. Only the
external scan/bind/socket boundary and clock are replaced.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("window_log_capture", ROOT / "scripts/i14-window-log-capture.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
BOOT = "5ea5b847-d0c3-46a2-b77d-49a7776404c0"
OTHER_BOOT = "00000000-0000-0000-0000-000000000002"
HEADER = (b"@@I14_LOG_BEGIN 7.0.10-i14-bringup aarch64 "
          b"9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a "
          b"867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a "
          + BOOT.encode() + b"\r\n")
TARGET = {"idVendor": 0x1d6b, "idProduct": 0x0104, "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}


def snapshot(index, data=b"<6>new log\r\n", boot=BOOT, rc=0):
    return (f"@@I14_LOG_SNAPSHOT {index}\r\n".encode() + data
            + f"\r\n@@I14_LOG_STATUS {index} {rc} {boot}\r\n".encode())


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Socket:
    def __init__(self, chunks, clock, before_recv=None):
        self.chunks, self.clock = list(chunks), clock
        self.before_recv, self.sent, self.closed, self.calls = before_recv, [], False, 0

    def settimeout(self, seconds):
        self.timeout = seconds

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        self.calls += 1
        if self.before_recv:
            self.before_recv(self.calls)
        self.clock.now += 0.01
        chunk = self.chunks.pop(0) if self.chunks else b""
        if isinstance(chunk, BaseException):
            raise chunk
        if len(chunk) > count:
            self.chunks.insert(0, chunk[count:])
        return chunk[:count]

    def close(self):
        self.closed = True


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out, self.lock = self.root / "capture", self.root / "native.lock"
        self.lock.touch()
        self.clock = Clock()
        self.enterContext(patch.object(m, "STOP_REQUESTED", False, create=True))
        self.enterContext(patch.object(m, "OUT", self.out))
        self.enterContext(patch.object(m, "LOCK", self.lock))
        self.enterContext(patch.object(m, "scan", return_value=[dict(TARGET)]))
        self.enterContext(patch.object(m, "source_ready", return_value=True))
        self.enterContext(patch.object(m.time, "monotonic", self.clock))
        self.connections = []
        self.sock = Socket([], self.clock)

        def connect(address, **kwargs):
            self.connections.append((address, kwargs))
            self.assertTrue((self.out / "attempt.json").exists(), "reserve durably before opening TCP")
            self.assertEqual(json.loads((self.out / "attempt.json").read_text())["command_sha256"],
                             hashlib.sha256(m.COMMAND).hexdigest())
            return self.sock

        self.enterContext(patch.object(m.socket, "create_connection", side_effect=connect))

    def result(self):
        return json.loads((self.out / "result.json").read_text())

    def test_loss_keeps_partial_nonnewline_bytes_before_next_recv_and_never_reconnects(self):
        first = HEADER + b"@@I14_LOG_SNAPSHOT 0\r\n<0>panic partial\x80"
        self.sock.chunks = [first, ConnectionResetError("lost")]

        def check_durable(n):
            if n == 2:
                self.assertEqual((self.out / "stream.raw").read_bytes(), first)
                self.assertEqual((self.out / "wire.raw").read_bytes(), first)
                with self.lock.open("r+") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.sock.before_recv = check_durable
        self.assertEqual(m.run(), 2)
        result = self.result()
        self.assertEqual(result["verdict"], "PARTIAL_TRANSPORT_LOSS")
        self.assertEqual(result["boot_id"], BOOT)
        self.assertFalse(result["capture_complete"])
        self.assertEqual(result["stream_sha256"], hashlib.sha256(first).hexdigest())
        self.assertEqual(len(self.connections), 1)
        self.assertTrue(self.sock.closed)
        self.assertEqual(len(self.sock.sent), 1)
        self.assertEqual(self.connections[0], (("10.77.14.2", 23),
                         {"timeout": 5, "source_address": ("10.77.14.1", 0)}))
        self.assertEqual(m.run(), 5, "existing reservation must block a second attempt")
        self.assertEqual(len(self.connections), 1)

    def test_host_stop_retains_verified_boot_and_partial_bytes_without_device_stopwrite(self):
        first = HEADER + b"@@I14_LOG_SNAPSHOT 0\r\n<0>last partial"
        self.sock.chunks = [first, m.Stopped("host stop")]
        self.assertEqual(m.run(), 2)
        self.assertEqual(self.result()["verdict"], "STOP_SIGNAL")
        self.assertEqual(self.result()["boot_id"], BOOT)
        self.assertEqual((self.out / "stream.raw").read_bytes(), first)
        self.assertEqual(self.sock.sent, [m.COMMAND])
        self.assertTrue(self.sock.closed)

    def test_normal_completion_requires_240_ordered_same_boot_snapshots(self):
        data = HEADER + b"".join(snapshot(i) for i in range(240)) + f"@@I14_LOG_DONE {BOOT}\r\n".encode()
        self.sock.chunks = [data[:17], data[17:201], data[201:]]
        self.assertEqual(m.run(), 0)
        self.assertEqual(self.result()["snapshots_complete"], 240)
        self.assertTrue(self.result()["capture_complete"])
        self.assertEqual(self.result()["verdict"], "COMPLETE_BOUNDED_CAPTURE")

    def test_wrong_native_identity_aborts_before_accepting_log(self):
        self.sock.chunks = [HEADER.replace(b"7.0.10-i14-bringup", b"wrong-kernel"), b"must not read"]
        self.assertEqual(m.run(), 2)
        self.assertEqual(self.result()["verdict"], "STOP_PROTOCOL")
        self.assertIsNone(self.result()["boot_id"])
        self.assertEqual(self.sock.calls, 1)

    def test_changed_boot_is_terminal_and_never_accepted_complete(self):
        self.sock.chunks = [HEADER + snapshot(0, boot=OTHER_BOOT)]
        self.assertEqual(m.run(), 2)
        self.assertEqual(self.result()["verdict"], "STOP_PROTOCOL")
        self.assertFalse(self.result()["capture_complete"])

    def test_premature_completion_and_duplicate_snapshot_are_terminal(self):
        for tail in (f"@@I14_LOG_DONE {BOOT}\r\n".encode(), snapshot(0) + snapshot(0)):
            with self.subTest(tail=tail[:60]):
                parser = m.Parser()
                with self.assertRaises(m.ProtocolError):
                    parser.feed(HEADER + tail)

    def test_read_error_preserves_bytes_and_is_not_complete(self):
        self.sock.chunks = [HEADER + snapshot(0, data=b"dmesg: read failed\r\n", rc=1)]
        self.assertEqual(m.run(), 2)
        self.assertEqual(self.result()["verdict"], "STOP_DMESG")
        self.assertIn(b"dmesg: read failed", (self.out / "stream.raw").read_bytes())

    def test_byte_cap_counts_wire_and_stops_without_reading_extra(self):
        self.sock.chunks = [b"x" * (8388608 + 1000)]
        self.assertEqual(m.run(), 2)
        self.assertEqual(self.result()["verdict"], "PARTIAL_BYTE_BOUND")
        self.assertEqual((self.out / "wire.raw").stat().st_size, 8388608)
        self.assertEqual((self.out / "stream.raw").stat().st_size, 8388608)
        self.assertEqual(sum(map(len, self.sock.chunks)), 1000)

    def test_timeout_has_fixed_deadline_and_never_reconnects(self):
        def advance(n):
            self.clock.now += 100

        self.sock.before_recv = advance
        self.sock.chunks = [socket.timeout(), socket.timeout(), socket.timeout()]
        self.assertEqual(m.run(), 2)
        self.assertEqual(self.result()["verdict"], "PARTIAL_TIME_BOUND")
        self.assertEqual(len(self.connections), 1)

    def test_split_telnet_controls_are_removed_and_refused_without_corrupting_bytes(self):
        self.sock.chunks = [b"\xff", b"\xfd", b"\x01" + HEADER + b"@@I14_LOG_SNAPSHOT 0\r\nhi\xff", b"\xfftail", b""]
        self.assertEqual(m.run(), 2)
        self.assertEqual(self.sock.sent[1], b"\xff\xfc\x01")
        self.assertIn(b"hi\xfftail", (self.out / "stream.raw").read_bytes())
        self.assertIn(b"hi\xff\xfftail", (self.out / "wire.raw").read_bytes())

    def test_related_usb_mismatch_refuses_without_socket_or_attempt(self):
        with patch.object(m, "scan", return_value=[{**TARGET, "locationID": 123}]):
            self.assertEqual(m.run(), 4)
        self.assertFalse((self.out / "attempt.json").exists())
        self.assertEqual(self.connections, [])

    def test_absent_or_unbindable_source_refuses_without_attempt(self):
        with patch.object(m, "source_ready", return_value=False):
            self.assertEqual(m.run(), 4)
        self.assertEqual(self.result()["verdict"], "NO_SOURCE")
        self.assertFalse((self.out / "attempt.json").exists())
        self.assertEqual(self.connections, [])

    def test_missing_or_owned_shared_lock_never_scans_or_touches_output(self):
        with self.lock.open("r+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(m, "scan") as scan:
                self.assertEqual(m.run(), 5)
                scan.assert_not_called()
            self.assertFalse(self.out.exists())
            # A refusal must not release the real owner's lock.
            with self.lock.open("r+") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock.unlink()
        self.assertEqual(m.run(), 5)
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.connections, [])

    def test_failed_durable_reservation_never_connects(self):
        original = m.write_json

        def fail_attempt(path, value):
            if path.name == "attempt.json":
                raise OSError("fsync failed")
            original(path, value)

        with patch.object(m, "write_json", side_effect=fail_attempt):
            self.assertEqual(m.run(), 2)
        self.assertEqual(self.connections, [])

    def test_short_host_writes_are_retried_until_all_bytes_are_fsynced(self):
        path = self.root / "short-write.raw"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        actual_write, synced = os.write, []
        try:
            with patch.object(m.os, "write", side_effect=lambda handle, data: actual_write(handle, data[:2])), \
                    patch.object(m.os, "fsync", side_effect=lambda handle: synced.append(handle)):
                m.write_all(fd, b"abc\x00\xffpartial")
            self.assertEqual(path.read_bytes(), b"abc\x00\xffpartial")
            self.assertEqual(synced, [fd])
        finally:
            os.close(fd)

    def test_signal_during_short_write_persists_received_chunk_before_stop(self):
        clean = HEADER + b"@@I14_LOG_SNAPSHOT 0\r\n<0>last partial"
        wire = b"\xff\xfd\x01" + clean
        self.sock.chunks = [wire, b"must never be received"]
        actual_write, actual_fsync = os.write, os.fsync
        stopped, synced = False, set()

        def short_write(handle, data):
            nonlocal stopped
            count = actual_write(handle, data[:3])
            path = self.out / "wire.raw"
            if not stopped and path.exists() and os.fstat(handle).st_ino == path.stat().st_ino:
                stopped = True
                m.on_term(15, None)
            return count

        def fsync(handle):
            actual_fsync(handle)
            synced.add(os.fstat(handle).st_ino)

        with patch.object(m.os, "write", side_effect=short_write), patch.object(m.os, "fsync", side_effect=fsync):
            self.assertEqual(m.run(), 2)
        self.assertTrue(stopped)
        self.assertEqual(self.result()["verdict"], "STOP_SIGNAL")
        self.assertEqual(self.result()["boot_id"], BOOT)
        self.assertEqual((self.out / "wire.raw").read_bytes(), wire)
        self.assertEqual((self.out / "stream.raw").read_bytes(), clean)
        self.assertTrue({(self.out / name).stat().st_ino for name in ("wire.raw", "stream.raw")} <= synced)
        self.assertEqual(self.sock.calls, 1)
        self.assertEqual(self.sock.sent, [m.COMMAND])
        self.assertTrue(self.sock.closed)

    def test_connect_failure_consumes_reservation_and_does_not_retry(self):
        with patch.object(m.socket, "create_connection", side_effect=OSError("down")) as connect:
            self.assertEqual(m.run(), 2)
            self.assertEqual(m.run(), 5)
            self.assertEqual(connect.call_count, 1)
        self.assertTrue((self.out / "attempt.json").exists())

    def test_cli_requires_explicit_activation_and_rejects_command_input(self):
        with patch.object(m, "run") as run, patch.object(m.sys, "stderr"):
            for args in ([], ["--command", "reboot"], ["--capture-once", "extra"]):
                self.assertEqual(m.main(args), 5)
            run.assert_not_called()

    def test_fixed_device_program_emits_240_bounded_readonly_snapshots(self):
        # All shell command boundaries are functions; the fixed program never
        # reads this host's /proc, USB, dmesg or device tree.
        calls = self.root / "dmesg-calls"
        harness = r'''
uname() { case "$1" in -r) printf '7.0.10-i14-bringup\n';; -m) printf 'aarch64\n';; *) return 90;; esac; }
sha256sum() { case "$1" in
/proc/device-tree/model) printf '9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a  %s\n' "$1";;
/proc/device-tree/compatible) printf '867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a  %s\n' "$1";;
*) return 91;; esac; }
grep() { return 0; }
cat() { [ "$1" = /proc/sys/kernel/random/boot_id ] || return 92; printf '5ea5b847-d0c3-46a2-b77d-49a7776404c0\n'; }
dmesg() { [ "$*" = '-r -s 16384' ] || return 93; printf '%s\n' "$*" >> "$CAPTURE_TEST_CALLS"; printf '<6>bounded tail\n'; }
sleep() { [ "$*" = 1 ] || return 94; }
'''
        completed = subprocess.run(["/bin/sh"], input=harness.encode() + m.COMMAND,
                                   capture_output=True, timeout=10,
                                   env={"PATH": "/nonexistent", "CAPTURE_TEST_CALLS": str(calls)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls.read_text().splitlines(), ["-r -s 16384"] * 240)
        parser = m.Parser()
        parser.feed(completed.stdout)
        self.assertTrue(parser.done)
        self.assertEqual(parser.completed, 240)
        self.assertEqual(parser.boot, BOOT)


if __name__ == "__main__":
    unittest.main()
