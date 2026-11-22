"""H1 owner input block: fake time/processes only, no phone or service access."""
import importlib.util
import importlib.machinery
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("h1_input", ROOT / "scripts/i14-h1-input.py")
# New block stays absent until the regression tests have run RED.
if spec.origin and Path(spec.origin).is_file():
    block = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(block)
else:
    block = None

class InputBlockTests(unittest.TestCase):
    def test_quiet_restart_and_first_input_stay_inside_original_idle_bound(self):
        self.assertIsNotNone(block, "bounded input block is missing")
        now = [0.0]
        statuses = []
        def status():
            statuses.append(now[0])
            if now[0] >= 120:
                raise RuntimeError("idle expired")
            return {"pid": 42, "taps": 0, "age": int(now[0])}
        def restart():
            now[0] += 4
            return {"pid": 43, "start_ticks": 7}
        result = block.quiet_restart(status, restart, lambda: now[0], lambda s: now.__setitem__(0, now[0] + s))
        self.assertEqual(result["quiet_s"], 60)
        self.assertEqual(result["ui"]["pid"], 43)
        self.assertEqual(statuses, [0, 60, 64])
        self.assertLess(now[0], 120)

    def test_quiet_helper_replacement_taps_or_late_restart_refuses_before_input(self):
        self.assertIsNotNone(block)
        for changed in ({"pid": 43}, {"taps": 1}, {"age": 100}):
            records = [{"pid": 42, "taps": 0, "age": 0}, dict(pid=42, taps=0, age=60)]
            records[1].update(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                block.quiet_restart(mock.Mock(side_effect=records), mock.Mock(), lambda: 0, lambda _: None)

    def test_fresh_ui_must_wake_before_waiting_for_natural_idle(self):
        source = ROOT / "scripts/i14-h1.py"
        spec = importlib.util.spec_from_file_location("h1_idle_entry", source)
        h1 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(h1)
        now = [0.0]
        def clock():
            return now[0]
        def sleep(dt):
            now[0] += dt
        def light():
            return int(.9 <= now[0] < 60.9)
        # Actual old payload rejects a late wake after the owner's initial zero.
        with self.assertRaisesRegex(ValueError, "screen is not off"):
            h1.idle_wait(light, clock, sleep)
        now[0] = 0
        block.await_backlight_on(light, clock, sleep)
        self.assertGreaterEqual(now[0], .9)
        while light() != 0:
            sleep(1)
        h1.idle_wait(light, clock, sleep)
        self.assertGreaterEqual(now[0], 120.9)

    def test_backlight_never_on_or_late_on_refuses(self):
        now = [0.0]
        def sleep(dt):
            now[0] += dt
        with self.assertRaises(ValueError):
            block.await_backlight_on(lambda: 0, lambda: now[0], sleep)
        now[0] = 0
        def slow_read():
            now[0] = 8.1
            return 1
        with self.assertRaises(ValueError):
            block.await_backlight_on(slow_read, lambda: now[0], sleep)

    def test_real_helper_ping_does_not_extend_idle_and_old_delay_expires(self):
        loader = importlib.machinery.SourceFileLoader("h1_touch", str(ROOT / "scripts/i14-touch"))
        spec = importlib.util.spec_from_loader("h1_touch", loader)
        touch = importlib.util.module_from_spec(spec)
        loader.exec_module(touch)
        now = [0.0]
        daemon = touch.Daemon(900, 120, device=mock.Mock(), clock=lambda: now[0])
        now[0] = 11
        daemon.handle(b"ping\n")
        now[0] = 60
        self.assertIsNone(daemon.expired())
        now[0] = 128.8
        self.assertEqual(daemon.expired(), "idle")
        self.assertEqual(daemon.taps, 0)

    def test_shell_wait_accepts_delayed_single_shell_and_keeps_identity(self):
        self.assertIsNotNone(block)
        now = [0.0]
        wanted = {"pid": 90, "ppid": 43, "start_ticks": 8, "session": 90, "tty_nr": 34816,
                  "argv": ["/bin/sh", "-l"], "exe": "/bin/busybox"}
        read = mock.Mock(side_effect=[[], [], [wanted]])
        got = block.await_shell(read, 43, 7, "/bin/busybox", lambda: now[0], lambda s: now.__setitem__(0, now[0] + s))
        self.assertEqual(got, wanted)
        self.assertGreater(now[0], 0)

    def test_ambiguous_or_wrong_child_refuses_without_waiting_or_keys(self):
        wanted = {"pid": 90, "ppid": 43, "start_ticks": 8, "session": 90, "tty_nr": 34816,
                  "argv": ["/bin/sh", "-l"], "exe": "/bin/busybox"}
        for children in ([wanted, wanted], [dict(wanted, tty_nr=0)],
                         [dict(wanted, argv=["/bin/sh"])], [dict(wanted, start_ticks=6)]):
            keys = mock.Mock()
            with self.subTest(children=children), self.assertRaises(ValueError):
                block.terminal_entry(lambda x,y: None, lambda: children, 43, 7, "/bin/busybox", {}, keys=keys)
            keys.assert_not_called()

    def test_rejected_stdio_keeps_child_and_descriptor_metadata(self):
        child = {"pid": 90, "ppid": 43, "start_ticks": 8, "session": 90, "tty_nr": 34816,
                 "argv": ["/bin/sh", "-l"], "exe": "/bin/busybox"}
        trace = {}
        with mock.patch.object(block, "processes", return_value=[child]), mock.patch.object(
                block.os, "readlink", side_effect=["/dev/pts/2", "/dev/null", "/dev/pts/2"]):
            with self.assertRaises(ValueError):
                block.terminal_children(43, trace)
        self.assertEqual(trace["terminal_children"][0]["pid"], 90)
        self.assertEqual(trace["terminal_children"][0]["stdio"],
                         ["/dev/pts/2", "/dev/null", "/dev/pts/2"])

    def test_valid_shell_after_deadline_never_types(self):
        now = [0.0]
        child = {"pid": 90, "ppid": 43, "start_ticks": 8, "session": 90, "tty_nr": 34816,
                 "argv": ["/bin/sh", "-l"], "exe": "/bin/busybox"}
        def read():
            now[0] = 3.2
            return [child]
        keys = mock.Mock()
        with self.assertRaises(ValueError):
            block.terminal_entry(lambda x,y: None, read, 43, 7, "/bin/busybox", {}, keys=keys,
                                 clock=lambda: now[0], sleep=lambda _: None)
        keys.assert_not_called()

    def test_failed_or_ambiguous_shell_keeps_last_metadata_and_never_types(self):
        self.assertIsNotNone(block)
        now = [0.0]
        trace = {}
        keys = mock.Mock()
        with self.assertRaises(ValueError):
            block.terminal_entry(lambda x,y: None, lambda: [], 43, 7, "/bin/busybox", trace,
                                 keys=keys, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
        keys.assert_not_called()
        self.assertEqual(trace["terminal_children"], [])
        self.assertFalse(trace["terminal_shell_proven"])
        self.assertGreaterEqual(now[0], 3)

if __name__ == "__main__":
    unittest.main()
