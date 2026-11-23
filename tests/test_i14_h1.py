"""Host-only checks: no Linux device opens, transports or subprocess workloads."""
import importlib.util
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "i14-h1.py"
spec = importlib.util.spec_from_file_location("h1_phone", SOURCE)
if SOURCE.exists():
    h1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h1)
else:
    h1 = None


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(h1, "phone payload must exist before H1 can run")

    def test_proc_comm_parentheses_do_not_shift_cpu_or_identity(self):
        fields = "S 1 0 0 0 0 0 0 0 0 0 7 3 0 0 0 0 0 0 900".split()
        self.assertEqual(h1.parse_stat("42 (name (with) spaces) " + " ".join(fields)),
                         {"pid": 42, "ppid": 1, "ticks": 10, "start_ticks": 900})

    def test_idle_cpu_uses_measured_interval_and_one_core(self):
        before = dict(pid=42, start_ticks=900, ticks=10, uptime=100.0, rss_kib=200)
        after = dict(pid=42, start_ticks=900, ticks=13, uptime=160.0, rss_kib=204)
        result = h1.idle_result(before, after, 100)
        self.assertEqual(result["cpu_percent_one_core"], 0.05)
        self.assertEqual(result["rss_after_kib"], 204)

    def test_reused_pid_and_reversed_cpu_are_rejected(self):
        before = dict(pid=42, start_ticks=900, ticks=10, uptime=100.0, rss_kib=200)
        for delta in ({"start_ticks": 901}, {"ticks": 9}, {"uptime": 159.99}):
            after = dict(before, uptime=160.0, ticks=11)
            after.update(delta)
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                h1.idle_result(before, after, 100)

    def test_idle_rejects_pid_reused_since_owner_selected_child(self):
        with mock.patch.object(h1, "snapshot", return_value={"start_ticks": 901}), \
                mock.patch.object(h1, "idle_wait") as wait, self.assertRaises(ValueError):
            h1.idle_sample(42, 900, lambda: 0)
        wait.assert_not_called()

    def test_idle_waits_sixty_seconds_and_refuses_screen_on(self):
        clock = Clock()
        h1.idle_wait(lambda: 0, clock.now, clock.sleep)
        self.assertEqual(clock.now(), 60)
        clock = Clock()
        with self.assertRaises(ValueError):
            h1.idle_wait(lambda: int(clock.now() >= 2), clock.now, clock.sleep)
        self.assertEqual(clock.now(), 2)

    def test_pixels_timestamp_precedes_late_transport_ack(self):
        clock = Clock()
        sent = []
        # Fake transport returns from write at 4 ms; ACK is only ready at 220 ms.
        def send():
            sent.append(clock.now())
            clock.sleep(0.004)
        result = h1.observe_change(lambda: int(clock.now() >= 0.060), send,
                                   clock.now, clock.sleep)
        self.assertEqual(sent, [0])
        self.assertAlmostEqual(result["observed_s"], 0.060)
        self.assertAlmostEqual(result["latency_ms"], 60)
        self.assertLess(result["observed_s"], 0.220)

    def test_no_frame_change_is_timeout_without_invented_latency(self):
        clock = Clock()
        result = h1.observe_change(lambda: 123, lambda: None, clock.now, clock.sleep)
        self.assertTrue(result["timeout"])
        self.assertIsNone(result["latency_ms"])
        self.assertGreaterEqual(result["end_s"], 3)

    def test_start_observes_backlight_and_retains_uptime_stamps(self):
        clock = Clock()
        result = h1.observe_start(lambda: None, lambda: int(clock.now() >= 0.54),
                                  lambda: 100 + clock.now(), clock.now, clock.sleep)
        self.assertAlmostEqual(result["start_uptime_s"], 100)
        self.assertAlmostEqual(result["on_uptime_s"], 100.54)
        self.assertAlmostEqual(result["elapsed_uptime_s"], 0.54)

    def test_start_requires_screen_off_and_missing_on_is_timeout(self):
        clock = Clock()
        with self.assertRaises(ValueError):
            h1.observe_start(lambda: self.fail("must not launch"), lambda: 1,
                             clock.now, clock.now, clock.sleep)
        result = h1.observe_start(lambda: None, lambda: 0, clock.now,
                                  clock.now, clock.sleep)
        self.assertTrue(result["timeout"])
        self.assertIsNone(result["on_uptime_s"])

    def test_results_never_overwrite_existing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            h1.save_result(path, {"test": True})
            with self.assertRaises(FileExistsError):
                h1.save_result(path, {"test": False})
            self.assertIn('"test": true', path.read_text())

    def test_owner_permit_blocks_experiments_stale_or_wrong_boot(self):
        permit = dict(boot_id="boot-a", expires_uptime_s=200,
                      e_closed=True, r_closed=True, drain_active=False, soak_active=False)
        h1.check_permit(permit, "boot-a", 100, [])
        for changed, blockers in (({"boot_id": "boot-b"}, []),
                                  ({"expires_uptime_s": 99}, []),
                                  ({"expires_uptime_s": 1001}, []),
                                  ({"e_closed": False}, []),
                                  ({"soak_active": True}, []),
                                  ({}, ["calibration lock held"])):
            with self.subTest(changed=changed, blockers=blockers), self.assertRaises(ValueError):
                h1.check_permit(dict(permit, **changed), "boot-a", 100, blockers)

    def test_lost_second_ack_retains_prior_and_observed_partial_row(self):
        class Transport:
            def __init__(self, fail=False):
                self.fail = fail
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def settimeout(self, value):
                pass
            def connect(self, path):
                pass
            def recv(self, size):
                if self.fail:
                    raise TimeoutError("injected ACK loss")
                return b"ok\n"
        process = dict(pid=42, start_ticks=900, ticks=10, uptime=100, rss_kib=200)
        observed = dict(observed_s=1.1, latency_ms=100, timeout=False)
        with mock.patch.object(h1, "preflight"), mock.patch.object(h1, "snapshot", return_value=process), \
                mock.patch.object(h1, "light", return_value=1), mock.patch.object(h1, "Framebuffer"), \
                mock.patch.object(h1.subprocess, "run"), mock.patch.object(h1.time, "sleep"), \
                mock.patch.object(h1, "observe_change", return_value=observed), \
                mock.patch.object(h1.socket, "socket", side_effect=[Transport(), Transport(True)]):
            try:
                result = h1.taps(42, [10, 10], [20, 20])
            except OSError:
                self.fail("lost ACK must retain partial measurements instead of escaping")
        self.assertEqual(len(result["rows"]), 2)
        self.assertTrue(result["rows"][0]["ack_ok"])
        self.assertEqual(result["rows"][1]["observed_s"], 1.1)
        self.assertEqual(result["rows"][1]["transport_error"], "TimeoutError")
        self.assertEqual(result["completed"], 1)

    def test_actual_startup_binding_checks_parent_executable_and_hash(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);proc=tmp/'proc'/'4242';proc.mkdir(parents=True)
            (proc/'stat').write_text('4242 (ui) S 100 0 0 0 0 0 0 0 0 0 7 3 0 0 0 0 0 0 123')
            (proc/'cmdline').write_bytes(b'/entry\0');entry=tmp/'entry';entry.write_bytes(b'fixture')
            def path(value):return tmp/'proc' if str(value)=='/proc' else entry
            with mock.patch.object(h1,'Path',side_effect=path),mock.patch.object(h1,'UI',{'rust':'/entry'}),mock.patch.object(h1.os,'readlink',return_value='/entry'),mock.patch.object(h1.os,'getpid',return_value=100):
                result=h1.startup_binding(mock.Mock(pid=4242),'rust')
                self.assertEqual(result['start_ticks'],123)
                self.assertEqual(result['entrypoint_sha256'],hashlib.sha256(b'fixture').hexdigest())
                with mock.patch.object(h1.os,'readlink',return_value='/other'),self.assertRaises(ValueError):
                    h1.startup_binding(mock.Mock(pid=4242),'rust')

    def test_startup_binding_is_recorded_after_observation_before_cleanup(self):
        events=[]
        child=mock.Mock(pid=4242);child.poll.return_value=None
        child.terminate.side_effect=lambda:events.append('cleanup')
        def observed(*a):events.append('observed');return dict(timeout=False)
        def binding(*a):events.append('binding');return dict(pid=4242,ppid=100,start_ticks=123,exe='ui',entrypoint_sha256='a'*64)
        with mock.patch.object(h1.subprocess,'Popen',return_value=child),mock.patch.object(h1,'observe_start',side_effect=observed),mock.patch.object(h1,'startup_binding',side_effect=binding):
            # observe_start normally performs launch; this fake does it explicitly.
            def run_observed(launch,*a):launch();return observed()
            with mock.patch.object(h1,'observe_start',side_effect=run_observed):result=h1.startup('rust')
        self.assertEqual(events,['observed','binding','cleanup'])
        self.assertTrue(result['binding_valid']);self.assertEqual(result['child_binding']['pid'],4242)

    def test_failure_trace_names_exact_source_site_without_exception_payload(self):
        clock = Clock()
        try:
            h1.idle_wait(lambda: 1, clock.now, clock.sleep)
        except ValueError as error:
            output = io.StringIO()
            h1.report_failure(error, output)
        record = json.loads(output.getvalue())
        self.assertEqual(record["exception"], "ValueError")
        self.assertEqual(record["frames"][-1]["function"], "idle_wait")
        self.assertGreater(record["frames"][-1]["line"], 0)
        self.assertNotIn("message", record)
        output = io.StringIO()
        h1.report_failure(ValueError("SECRET_CONFIG_OR_PIXEL"), output)
        self.assertNotIn("SECRET", output.getvalue())

    def test_terminal_entry_taps_hit_real_keyboard_keys(self):
        project = SOURCE.parents[1]
        widgets_source = project / "rootfs/overlay/usr/lib/i14ui/widgets.py"
        widgets_spec = importlib.util.spec_from_file_location("h1_entry_widgets", widgets_source)
        widgets = importlib.util.module_from_spec(widgets_spec)
        widgets_spec.loader.exec_module(widgets)
        plan = json.loads((project / "tests/fixtures/h1-terminal-entry.json").read_text())
        keyboard = widgets.Keyboard(plan["width"], plan["bottom"], lambda *a: None,
                                    terminal_row=plan["terminal_row"])
        outputs = []
        for expected, x, y in plan["keys"]:
            key = keyboard.key_at(x, y)
            self.assertIsNotNone(key)
            self.assertEqual(key[2], expected)
            outputs.append("\n" if expected == "enter" else expected)
        self.assertEqual("".join(outputs), plan["command"])
        self.assertLessEqual(len(plan["keys"]) + 1, 30)


if __name__ == "__main__":
    unittest.main()
