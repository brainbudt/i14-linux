"""Offline state/receipt tests. All USB, local bind and child boundaries are fake."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/i14-window-supervisor.py"


class CommandLineTests(unittest.TestCase):
    def test_missing_arguments_refuse_without_starting_detection(self):
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT)], capture_output=True)
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn(b"STATE_DIR PACKAGE_MANIFEST", result.stderr)


def load_module():
    spec = importlib.util.spec_from_file_location("i14_window_supervisor_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds=2):
        self.now += seconds


class FakeProcess:
    def __init__(self):
        self.pid = 8123
        self.returncode = None
        self.terminated = False
        self.ignore_term = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.ignore_term:
            self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake collector", timeout)
        return self.returncode


def complete_result(action="power_evidence"):
    result = {"attempted": True, "capture_complete": True, "identity": {"ok": True},
              "boot": {"boot_id": "00000000-0000-0000-0000-000000000001",
                       "end_boot_id": "00000000-0000-0000-0000-000000000001", "same_boot": True},
              "storage": {"capacity_ok": True, "names_ok": True, "all_ro": True},
              "cached_diagnostics": {"historical_only": True, "charger_state": {"status": "ABSENT"},
                                     "battery_sample": {"status": "ABSENT"}},
              "charging_blocked": True, "ready_for_charging": False,
              "exit_code": 0, "verdict": "COMPLETE_POWER_EVIDENCE" if action == "power_evidence"
              else "COMPLETE_CACHED_BASELINE"}
    if action == "power_evidence":
        result["power_evidence"] = {"kernel_log": {"status": "ABSENT"}}
    return result


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = load_module()
        self.module.ROOT = self.root
        self.worker_path = self.root / "scripts/wait-native-cached-once.py"
        self.worker_path.parent.mkdir()
        self.worker_path.write_text("# fixture; never executed\n")
        self.source = self.root / "source.txt"
        self.source.write_text("pinned source\n")
        self.manifest = self.root / "manifest.json"
        self.package = {"version": 1, "reviewed": True,
                        "worker_sha256": hashlib.sha256(self.worker_path.read_bytes()).hexdigest(),
                        "actions": ["power_evidence", "cached_baseline"],
                        "source_sha256": {"source.txt": hashlib.sha256(self.source.read_bytes()).hexdigest()}}
        self.write_manifest()
        self.state_dir = self.root / "state"
        self.clock = FakeClock()
        self.observation = "exact"
        self.bindable = True
        self.scans = 0
        self.processes = []
        self.launched = []
        self.worker = SimpleNamespace(scan=self.scan, classify=lambda devices: devices,
                                      source_ready=lambda: self.bindable)
        self.sup = None
        self.addCleanup(self.cleanup_supervisor)

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.package))

    def scan(self):
        self.scans += 1
        return self.observation

    def launch(self, args, *, stdout, stderr, **kwargs):
        # The real durable reservation must precede this external boundary.
        state = json.loads((self.state_dir / "state.json").read_text())
        reservation = state["reserved"]
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation["action"], "power_evidence" if args[-1] == "--power-evidence"
                         else "cached_baseline")
        self.assertEqual(args[:4], [sys.executable, "-I", "-B", str(self.worker_path)])
        self.assertEqual(args[5], "5")
        self.assertFalse(kwargs.get("shell", False))
        self.assertTrue(kwargs.get("close_fds"))
        out = Path(args[4])
        self.assertFalse(out.exists(), "worker must create its own NEW output directory")
        out.mkdir()
        process = FakeProcess()
        self.processes.append(process)
        self.launched.append((args, out))
        return process

    def start(self, **kwargs):
        self.sup = self.module.Supervisor(self.state_dir, self.manifest, worker=self.worker,
                                          clock=self.clock, popen=kwargs.pop("popen", self.launch), **kwargs)
        return self.sup

    def test_successful_start_publishes_own_pid_for_event_waiter(self):
        self.start()
        pid_path = self.state_dir / "pid"
        self.assertTrue(pid_path.is_file())
        self.assertEqual(pid_path.read_text(), str(os.getpid()) + "\n")
        self.assertEqual(self.launched, [])

    def cleanup_supervisor(self):
        if self.sup is not None:
            self.sup.close()

    def abandon(self):
        # Model a host process crash: do not run its orderly stop path.
        if self.sup.lock_fd is not None:
            os.close(self.sup.lock_fd)
            self.sup.lock_fd = None
        for stream in self.sup.streams:
            stream.close()
        self.sup = None

    def tick(self):
        self.clock.advance()
        return self.sup.tick()

    def finish(self, result, rc=0, attempted=True, raw=None):
        out = self.launched[-1][1]
        if attempted:
            (out / "attempt.json").write_text('{"mode":"read-only"}')
        (out / "result.json").write_text(json.dumps(result) if raw is None else raw)
        self.processes[-1].returncode = rc
        self.tick()

    def events(self):
        return [json.loads(line) for line in (self.state_dir / "events.jsonl").read_text().splitlines()]

    def test_exact_target_waits_for_bindable_source_before_durable_reservation(self):
        self.bindable = False
        self.start().tick()
        self.assertTrue(self.sup.state["attachment_open"])
        self.assertIsNone(self.sup.state["reserved"])
        self.assertEqual(self.launched, [])
        first = len(self.events())
        self.tick()
        self.assertEqual(len(self.events()), first, "unchanged wait must not grow the journal")
        self.bindable = True
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["inflight"]["pid"], 8123)

    def test_mismatch_is_never_reserved_and_does_not_close_an_open_window(self):
        self.bindable = False
        self.start().tick()
        self.observation = "mismatch"
        for _ in range(4):
            self.tick()
        self.assertTrue(self.sup.state["attachment_open"])
        self.assertEqual(self.sup.state["window_seq"], 1)
        self.assertEqual(self.launched, [])
        self.assertNotIn("USB Serial Number", (self.state_dir / "state.json").read_text())

    def test_poll_interval_and_three_absences_debounce_window_loss(self):
        self.bindable = False
        self.start().tick()
        self.observation = "absent"
        self.sup.tick()
        self.assertEqual(self.scans, 1)
        self.tick()
        self.tick()
        self.assertTrue(self.sup.state["attachment_open"])
        self.tick()
        self.assertFalse(self.sup.state["attachment_open"])
        self.assertIn("WINDOW_LOST", [event["type"] for event in self.events()])
        self.assertTrue((self.state_dir / "NEXT-WINDOW.md").exists())

    def test_single_absent_blip_is_forgotten_on_exact_metadata(self):
        self.bindable = False
        self.start().tick()
        self.observation = "absent"
        self.tick()
        self.observation = "exact"
        self.tick()
        self.observation = "absent"
        self.tick()
        self.tick()
        self.assertTrue(self.sup.state["attachment_open"])

    def test_complete_power_capture_finishes_both_goals_without_second_call(self):
        self.start().tick()
        self.finish(complete_result())
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "COMPLETE")
        self.assertEqual(self.sup.state["last_boot"], "00000000-0000-0000-0000-000000000001")
        self.assertEqual(self.sup.state["results"][-1]["status"], "COMPLETE")
        plan = (self.state_dir / "NEXT-WINDOW.md").read_text()
        self.assertIn("dmesg", plan)
        self.assertIn("ADC", plan)
        self.assertIn("BLOCKED", plan)

    def test_power_missing_cached_data_cannot_claim_both_goals_complete(self):
        self.start().tick()
        result = complete_result()
        del result["cached_diagnostics"]
        self.finish(result)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")

    def test_success_receipt_without_attempt_receipt_remains_blocked(self):
        self.start().tick()
        self.finish(complete_result(), attempted=False)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")

    def test_power_boot_end_must_match_before_success(self):
        self.start().tick()
        result = complete_result()
        result["boot"]["end_boot_id"] = "00000000-0000-0000-0000-000000000002"
        self.finish(result)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")

    def test_network_loss_does_not_make_new_window_or_retry(self):
        self.start().tick()
        self.finish(complete_result())
        self.bindable = False
        for _ in range(5):
            self.tick()
        self.assertEqual(self.sup.state["window_seq"], 1)
        self.assertEqual(len(self.launched), 1)

    def test_new_physical_window_can_capture_new_read_only_context(self):
        self.start().tick()
        self.finish(complete_result())
        self.observation = "absent"
        for _ in range(3):
            self.tick()
        self.observation = "exact"
        self.tick()
        self.assertEqual(self.sup.state["window_seq"], 2)
        self.assertEqual(len(self.launched), 2)
        self.assertNotEqual(self.launched[0][1], self.launched[1][1])

    def test_only_explicit_no_command_timeout_allows_one_cached_fallback(self):
        self.start().tick()
        result = {"attempted": False, "capture_complete": False, "exit_code": 3,
                  "verdict": "TIMEOUT_NO_COMMAND", "charging_blocked": True,
                  "ready_for_charging": False}
        self.finish(result, rc=3, attempted=False)
        self.assertEqual(len(self.launched), 2)
        self.assertEqual(self.launched[-1][0][-1], "5")
        self.finish(complete_result("cached_baseline"))
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.launched), 2)
        self.assertEqual(self.sup.state["phase"], "COMPLETE")

    def test_attempt_receipt_prohibits_even_an_explicit_no_command_fallback(self):
        self.start().tick()
        result = {"attempted": False, "capture_complete": False, "exit_code": 3,
                  "verdict": "TIMEOUT_NO_COMMAND", "charging_blocked": True,
                  "ready_for_charging": False}
        self.finish(result, rc=3, attempted=True)
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")

    def test_transport_error_never_dispatches_fallback_or_same_window_retry(self):
        self.start().tick()
        result = complete_result()
        result.update(capture_complete=False, exit_code=2, verdict="STOP_INCOMPLETE")
        self.finish(result, rc=2)
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")

    def test_worker_lock_refusal_does_not_fallback(self):
        self.start().tick()
        self.processes[-1].returncode = 5
        self.tick()
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")

    def test_incomplete_and_malformed_results_are_not_success(self):
        for label, raw in (("truncated", "{"), ("missing fields", "{}"),
                           ("false complete", json.dumps({"capture_complete": True})),
                           ("boolean exit", json.dumps({**complete_result(), "exit_code": False})),
                           ("partial", json.dumps({**complete_result(), "capture_complete": False}))):
            with self.subTest(label=label):
                if self.sup is not None:
                    self.sup.close()
                self.state_dir = self.root / label
                self.start().tick()
                self.finish(None, raw=raw)
                self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")
                self.assertNotEqual(self.sup.state["results"][-1]["status"], "COMPLETE")

    def test_identity_refusal_blocks_every_future_physical_window(self):
        self.start().tick()
        result = complete_result()
        result.update(identity={"ok": False}, verdict="STOP_IDENTITY", exit_code=6)
        self.finish(result, rc=6)
        self.observation = "absent"
        for _ in range(3):
            self.tick()
        self.observation = "exact"
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "INTEGRITY_BLOCK")
        self.assertGreater(self.scans, 4, "host detection should continue while action is blocked")

    def test_worker_integrity_receipt_is_global_block(self):
        self.start().tick()
        result = {"attempted": False, "capture_complete": False, "exit_code": 2,
                  "verdict": "STOP_ERROR", "failure_kind": "integrity",
                  "charging_blocked": True, "ready_for_charging": False}
        self.finish(result, rc=2, attempted=False)
        self.assertEqual(self.sup.state["phase"], "INTEGRITY_BLOCK")

    def test_native_identity_contradiction_without_identity_dict_blocks_next_window(self):
        self.start().tick()
        result = complete_result()
        result.update(identity=None, verdict="STOP_CONTRADICTION", exit_code=6,
                      capture_complete=False,
                      framing={"I14_IDENTITY_OK": 1, "I14_STOP_IDENTITY": 1})
        self.finish(result, rc=6)
        self.observation = "absent"
        for _ in range(3):
            self.tick()
        self.observation = "exact"
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "INTEGRITY_BLOCK")

    def test_crash_during_identity_classification_retains_dispatch_fence(self):
        self.start().tick()
        result = complete_result()
        result.update(identity={"ok": False}, verdict="STOP_IDENTITY", exit_code=6)
        def crash(_result):
            raise RuntimeError("simulated crash during classification")
        self.sup.inspect_failure = crash
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            self.finish(result, rc=6)
        self.abandon()
        self.start()
        self.observation = "absent"
        for _ in range(3):
            self.tick()
        self.observation = "exact"
        self.tick()
        self.assertEqual(len(self.launched), 1)

    def test_worker_pin_mutation_before_launch_executes_nothing(self):
        self.bindable = False
        self.start().tick()
        self.worker_path.write_text("# changed worker\n")
        self.bindable = True
        self.tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.sup.state["phase"], "INTEGRITY_BLOCK")

    def test_second_pin_check_after_reservation_prevents_exec(self):
        self.start()
        original = self.sup.save

        def mutate_after_reserve():
            original()
            if self.sup.state["reserved"]:
                self.worker_path.write_text("# changed at reservation\n")

        self.sup.save = mutate_after_reserve
        self.sup.tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.sup.state["phase"], "INTEGRITY_BLOCK")

    def test_manifest_change_and_source_mutation_fail_closed(self):
        self.start().tick()
        self.finish(complete_result())
        self.package["worker_sha256"] = "0" * 64
        self.write_manifest()
        self.tick()
        self.assertEqual(self.sup.state["phase"], "INTEGRITY_BLOCK")
        self.sup.close()
        with self.assertRaises(self.module.IntegrityError):
            self.start()

    def test_source_pin_mutation_prevents_scan_and_dispatch(self):
        self.source.write_text("mutated source")
        with self.assertRaises(self.module.IntegrityError):
            self.start()
        self.assertEqual(self.scans, 0)
        self.assertEqual(self.launched, [])

    def test_corrupt_pending_window_state_is_refused_before_detection(self):
        self.start().tick()
        self.finish(complete_result())
        self.abandon()
        state = json.loads((self.state_dir / "state.json").read_text())
        state["current_window"]["actions"]["power_evidence"] = "COMPLETE"
        (self.state_dir / "state.json").write_text(json.dumps(state))
        with self.assertRaises(self.module.IntegrityError):
            self.start()

    def test_orderly_restart_restores_operational_phase_from_durable_actions(self):
        self.start().tick()
        self.finish(complete_result())
        self.sup.close()
        self.start().tick()
        self.assertEqual(self.sup.state["phase"], "COMPLETE")
        self.assertEqual(len(self.launched), 1)

    def test_missing_state_with_existing_journal_cannot_be_treated_as_fresh_epoch(self):
        self.start().tick()
        self.finish(complete_result())
        self.abandon()
        (self.state_dir / "state.json").unlink()
        with self.assertRaises(self.module.IntegrityError):
            self.start()
        self.assertEqual(len(self.launched), 1)

    def test_partial_journal_is_preserved_and_refuses_restart(self):
        self.start().tick()
        self.finish(complete_result())
        self.abandon()
        with (self.state_dir / "events.jsonl").open("ab") as stream:
            stream.write(b'{"seq":')
        before = (self.state_dir / "events.jsonl").read_bytes()
        with self.assertRaises(self.module.IntegrityError):
            self.start()
        self.assertEqual((self.state_dir / "events.jsonl").read_bytes(), before)

    def test_unreviewed_manifest_or_dynamic_worker_path_is_refused(self):
        for change in ({"reviewed": False}, {"actions": ["charger"]},
                       {"worker_path": "/tmp/else.py"}, {"version": True},
                       {"source_sha256": {"../else": "0" * 64}}):
            with self.subTest(change=change):
                original = dict(self.package)
                self.package.update(change)
                self.write_manifest()
                with self.assertRaises(self.module.IntegrityError):
                    self.start()
                self.package = original
        self.assertEqual(self.launched, [])

    def test_singleton_contention_is_nonblocking_and_never_releases_owner(self):
        self.start()
        with self.assertRaises(BlockingIOError):
            self.module.Supervisor(self.state_dir, self.manifest, worker=self.worker, clock=self.clock,
                                   popen=self.launch)
        self.sup.tick()
        self.assertEqual(len(self.launched), 1)

    def test_crash_after_durable_reserve_does_not_dispatch_on_restart(self):
        class Crash(BaseException):
            pass

        def crash(args, **kwargs):
            state = json.loads((self.state_dir / "state.json").read_text())
            self.assertEqual(state["reserved"]["action"], "power_evidence")
            raise Crash()

        self.start(popen=crash)
        with self.assertRaises(Crash):
            self.sup.tick()
        self.abandon()
        self.start().tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")
        self.assertIn("RECOVER_UNCERTAIN", [event["type"] for event in self.events()])
        self.assertTrue(self.sup.state["attachment_open"])

    def test_recovery_keeps_completed_receipt_as_data_without_reentry(self):
        self.start().tick()
        (self.launched[-1][1] / "result.json").write_text(json.dumps(complete_result()))
        self.abandon()
        self.start().tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")
        self.assertEqual(self.sup.state["results"][-1]["receipt"]["verdict"], "COMPLETE_POWER_EVIDENCE")
        self.assertEqual(self.sup.state["results"][-1]["status"], "UNCERTAIN")

    def test_restart_on_same_attachment_requires_three_absences_before_new_window(self):
        self.start().tick()
        self.finish(complete_result())
        self.abandon()
        self.start().tick()
        self.assertEqual(len(self.launched), 1)
        self.observation = "absent"
        self.tick()
        self.tick()
        self.observation = "exact"
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.observation = "absent"
        for _ in range(3):
            self.tick()
        self.observation = "exact"
        self.tick()
        self.assertEqual(len(self.launched), 2)

    def test_restart_does_not_count_stale_precrash_absence_streak(self):
        self.bindable = False
        self.start().tick()
        self.observation = "absent"
        self.tick()
        self.tick()
        self.abandon()
        self.start().tick()
        self.assertTrue(self.sup.state["attachment_open"])
        self.tick()
        self.assertTrue(self.sup.state["attachment_open"])
        self.tick()
        self.assertFalse(self.sup.state["attachment_open"])

    def test_stop_terminates_and_reaps_only_owned_child(self):
        self.start().tick()
        (self.state_dir / "STOP").write_text("stop host supervisor\n")
        self.assertFalse(self.tick())
        self.assertTrue(self.processes[-1].terminated)
        self.assertIsNotNone(self.processes[-1].returncode)
        self.assertEqual(self.sup.state["phase"], "STOPPED")
        self.assertEqual(self.events()[-1]["type"], "STOP")

    def test_owned_child_exit_between_poll_and_sigterm_is_reaped(self):
        self.start().tick()
        child = self.processes[-1]

        def already_exited():
            child.returncode = 0
            raise ProcessLookupError("child exited during stop")

        child.terminate = already_exited
        self.sup.close()
        self.assertIsNone(self.sup.process)
        self.assertEqual(self.sup.state["phase"], "STOPPED")

    def test_recovered_pid_is_never_signalled_on_stop(self):
        self.start().tick()
        child = self.processes[-1]
        self.abandon()
        self.start()
        self.sup.close()
        self.assertFalse(child.terminated)
        self.assertEqual(self.sup.state["recovery"]["pid"], 8123)

    def test_unresolved_recovered_process_also_guards_later_windows(self):
        self.start().tick()
        self.abandon()
        self.start().tick()
        self.observation = "absent"
        for _ in range(3):
            self.tick()
        self.observation = "exact"
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")

    def test_stop_arriving_during_source_check_cannot_launch_worker(self):
        def stop_during_bind():
            (self.state_dir / "STOP").write_text("stop\n")
            return True

        self.worker.source_ready = stop_during_bind
        self.start().tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.sup.state["phase"], "STOPPED")

    def test_sigterm_arriving_at_reservation_does_not_launch_worker(self):
        self.start()
        original = self.sup.save

        def stop_after_reserve():
            original()
            if self.sup.state["reserved"]:
                self.sup.request_stop(15)

        self.sup.save = stop_after_reserve
        self.sup.tick()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.sup.state["phase"], "STOPPED")

    def test_unresponsive_owned_worker_is_documented_without_kill_or_relaunch(self):
        self.start().tick()
        self.processes[-1].ignore_term = True
        self.sup.close()
        self.assertTrue(self.sup.state["worker_active_at_stop"])
        self.assertEqual(self.sup.state["inflight"]["pid"], 8123)

    def test_child_watchdog_blocks_window_without_retry(self):
        self.start().tick()
        self.clock.advance(40)
        self.sup.tick()
        self.assertTrue(self.processes[-1].terminated)
        self.tick()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.sup.state["phase"], "BLOCKED_WINDOW")

    def test_real_host_child_creates_new_output_and_is_reaped_without_hardware(self):
        result = complete_result()
        fixture = ("import json, pathlib, sys\n"
                   "out=pathlib.Path(sys.argv[1]); out.mkdir()\n"
                   "(out/'attempt.json').write_text('{}')\n"
                   "(out/'result.json').write_text(" + repr(json.dumps(result)) + ")\n"
                   "print('offline child fixture', flush=True)\n")
        self.worker_path.write_text(fixture)
        self.package["worker_sha256"] = hashlib.sha256(self.worker_path.read_bytes()).hexdigest()
        self.write_manifest()
        self.start(popen=subprocess.Popen).tick()
        child = self.sup.process
        deadline = time.monotonic() + 3
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.tick()
        self.assertEqual(child.returncode, 0)
        self.assertIsNone(self.sup.process)
        self.assertEqual(self.sup.state["phase"], "COMPLETE")
        stdout = self.state_dir / "windows/window-000001/power_evidence.stdout"
        self.assertEqual(stdout.read_text(), "offline child fixture\n")

    def test_real_host_child_stop_is_terminated_and_reaped_without_signalling_other_processes(self):
        self.worker_path.write_text("import time\ntime.sleep(30)\n")
        self.package["worker_sha256"] = hashlib.sha256(self.worker_path.read_bytes()).hexdigest()
        self.write_manifest()
        self.start(popen=subprocess.Popen).tick()
        child = self.sup.process
        self.sup.close()
        self.assertEqual(child.returncode, -15)
        self.assertFalse(self.sup.state["worker_active_at_stop"])
        self.assertEqual(self.sup.state["phase"], "STOPPED")

    def test_journal_is_ordered_and_state_tracks_final_sequence(self):
        self.start().tick()
        self.finish(complete_result())
        events = self.events()
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(self.sup.state["event_seq"], len(events))
        self.assertTrue(all(e["utc"] and isinstance(e["data"], dict) for e in events))


if __name__ == "__main__":
    unittest.main()
