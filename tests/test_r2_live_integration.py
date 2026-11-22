"""R2 host integration: temporary evidence, fake process and lock boundaries only."""
import contextlib
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHost:
    def __init__(self, case):
        self.case = case
        self.identity = {'pid': 8123, 'start': 'Sun Nov 1 23:15:08 2026',
                         'argv': [sys.executable, '-I', '-B',
                                  str(case.root / 'scripts/i14-window-supervisor.py'),
                                  str(case.old), str(case.root / 'artifacts/i14-autonomous-windows-20261101-r1/manifest.json')]}
        self.current = copy.deepcopy(self.identity)
        self.calls = []
        self.on_stop = None
        self.busy = None
        self.ready = True

    def identify(self, pid):
        self.calls.append(('identify', pid))
        return copy.deepcopy(self.current)

    def signal_and_wait(self, expected):
        self.calls.append(('stop', expected))
        self.case.assertTrue((self.case.handoff / 'intent.json').is_file())
        self.case.assertEqual(expected, self.current)
        state = json.loads((self.case.old / 'state.json').read_text())
        state.update(phase='STOPPED', event_seq=state['event_seq'] + 1)
        if self.on_stop:
            self.on_stop(state)
        (self.case.old / 'state.json').write_text(json.dumps(state))
        with (self.case.old / 'events.jsonl').open('a') as f:
            f.write(json.dumps({'seq': state['event_seq'], 'type': 'STOP',
                                'data': {'owned_worker_active': False, 'owned_pid': None, 'recovered_pid': None}}) + '\n')
        self.current = None

    @contextlib.contextmanager
    def lock(self, path):
        self.calls.append(('lock', str(path)))
        if path.name == self.busy:
            raise BlockingIOError('fixture busy')
        yield 44 if path.name == 'supervisor.lock' else 45
        self.calls.append(('unlock', str(path)))

    def start(self, argv, old_lock_fd, out):
        self.calls.append(('start', argv, old_lock_fd))
        self.case.assertIn(('unlock', str(self.case.root / 'artifacts/i14-native-watch.lock')), self.calls)
        self.case.assertNotIn(('unlock', str(self.case.old / 'supervisor.lock')), self.calls)
        return 9123

    def await_ready(self, pid, directory, lineage_sha256, package_sha256):
        self.calls.append(('ready', pid))
        if not self.ready:
            raise TimeoutError('fixture ready unknown')
        return {'pid': pid, 'lineage_sha256': lineage_sha256,
                'package_sha256': package_sha256, 'status': 'READY_BEFORE_FIRST_SCAN'}

    def verify_inherited_lock(self, fd, path):
        self.calls.append(('inherited', fd, str(path)))


class R2IntegrationTests(unittest.TestCase):
    def setUp(self):
        paths = ['scripts/i14-window-supervisor-r2.py', 'scripts/i14-r2-host-handoff.py']
        for p in paths:
            self.assertTrue((ROOT / p).exists(), 'missing concrete r2 integration: ' + p)
        fixture_module = load(ROOT / 'tests/test_r2_handoff_preflight.py', 'r2_preflight_fixture')
        self.fixture = fixture_module.HandoffPreflightTests('test_eligible_snapshot_is_read_only_and_never_authorizes_execution')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.old = self.fixture.state_dir
        self.handoff = self.root / 'artifacts/i14-r2-handoff-once'
        self.new = self.root / 'artifacts/i14-autonomous-state-r2'
        self.m = load(ROOT / paths[1], 'r2_handoff_test')
        self.r2 = load(ROOT / paths[0], 'r2_integration_test')
        self.r2.ROOT = self.root
        for p in paths + ['scripts/i14-r2-handoff-preflight.py', 'scripts/i14-window-log-capture.py',
                          'artifacts/window-supervisor-r2-prep-20261101/i14-window-supervisor.py']:
            dst = self.root / p
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((ROOT / p).read_bytes())
        (self.old / 'supervisor.lock').touch()
        self.package_path = self.root / 'artifacts/i14-autonomous-windows-20261102-r2/manifest.json'
        self.package_path.parent.mkdir(parents=True)
        self.package = {'version': 2, 'reviewed': True,
                        'worker_sha256': self.fixture.package_pin['manifest']['worker_sha256'],
                        'log_worker_sha256': self.r2.LOG_WORKER_SHA256,
                        'actions': ['power_evidence', 'cached_baseline', 'window_log_capture'],
                        'source_sha256': {p: hashlib.sha256((self.root / p).read_bytes()).hexdigest() for p in paths + ['scripts/i14-r2-handoff-preflight.py']}}
        self.write_package()
        self.host = FakeHost(self)

    def write_package(self):
        self.package_path.write_text(json.dumps(self.package))

    def run_handoff(self):
        return self.m.execute(self.root, host=self.host)

    def test_separate_deployment_manifest_preserves_frozen_false_candidate(self):
        expected='artifacts/i14-autonomous-windows-20261102-r2/manifest.json'
        self.assertEqual(self.m.PACKAGE,expected)
        self.assertEqual(self.r2.PACKAGE,expected)
        frozen=self.root/'artifacts/window-supervisor-r2-integration-20261102/package.json'
        frozen.parent.mkdir(parents=True)
        frozen.write_bytes((ROOT/'artifacts/window-supervisor-r2-integration-20261102/package.json').read_bytes())
        before=frozen.read_bytes()
        self.assertFalse(json.loads(before)['reviewed'])
        self.assertEqual(self.run_handoff()['status'],'ACTOR_READY')
        self.assertEqual(frozen.read_bytes(),before)

    def test_closed_complete_epoch_stops_once_and_hands_held_old_lock_to_fixed_actor(self):
        result = self.run_handoff()
        self.assertEqual(result['status'], 'ACTOR_READY')
        self.assertEqual(result['charging_status'], 'UNKNOWN')
        self.assertFalse(result['charging_qualified'])
        lineage = json.loads((self.new / 'lineage.json').read_text())
        self.assertEqual(lineage['old_epoch'], str(self.old))
        self.assertEqual(lineage['pmic_adc_status'], 'UNKNOWN_NOT_CLEARED')
        self.assertEqual(lineage['old_package_sha256'], self.fixture.m.R1_PACKAGE_SHA256)
        start = [c for c in self.host.calls if c[0] == 'start']
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0][1][:2], ['/usr/bin/caffeinate', '-i'])
        self.assertEqual(start[0][1][-2:], ['--legacy-lock-fd', '44'])
        self.assertEqual(len([c for c in self.host.calls if c[0] == 'stop']), 1)
        self.assertFalse((self.old / 'STOP').exists())

    def test_unreviewed_package_refuses_before_intent_process_or_stop(self):
        self.package['reviewed'] = False
        self.write_package()
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertEqual(self.host.calls, [])
        self.assertFalse(self.handoff.exists())

    def test_changed_new_source_or_fixed_worker_blocks_before_process(self):
        for name in ['scripts/i14-window-supervisor-r2.py', 'scripts/i14-window-log-capture.py']:
            with self.subTest(name=name):
                path = self.root / name
                old = path.read_bytes()
                path.write_bytes(old + b'\n')
                with self.assertRaises(self.m.Refused): self.run_handoff()
                self.assertEqual(self.host.calls, [])
                path.write_bytes(old)

    def test_old_pending_unknown_open_stop_or_unprocessed_event_never_signals(self):
        for key, value in [('reserved', {'action': 'bad'}), ('recovery', {'status': 'UNKNOWN'}),
                           ('attachment_open', True), ('phase', 'STOPPED')]:
            with self.subTest(key=key):
                state = copy.deepcopy(self.fixture.state)
                state[key] = value
                (self.old / 'state.json').write_text(json.dumps(state))
                with self.assertRaises(self.m.Refused): self.run_handoff()
                self.assertEqual(self.host.calls, [])
        self.fixture.write_state()
        (self.old / 'STOP').touch()
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertEqual(self.host.calls, [])

    def test_wrong_command_start_or_pid_is_not_signalled(self):
        for key, value in [('argv', ['other']), ('start', ''), ('pid', 999)]:
            with self.subTest(key=key):
                self.host.current = copy.deepcopy(self.host.identity)
                self.host.current[key] = value
                with self.assertRaises(self.m.Refused): self.run_handoff()
                self.assertFalse(any(c[0] == 'stop' for c in self.host.calls))

    def test_same_arguments_with_a_different_python_binary_are_not_ownership(self):
        self.host.current['argv'][0] = '/other/Python'
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertFalse(any(c[0] == 'stop' for c in self.host.calls))

    def test_stop_appearing_after_native_lock_release_prevents_launch(self):
        original = self.host.lock
        @contextlib.contextmanager
        def stop_on_release(path):
            with original(path) as fd:
                yield fd
            if path.name == 'i14-native-watch.lock':
                (self.old / 'STOP').touch()
        self.host.lock = stop_on_release
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertFalse(any(c[0] == 'start' for c in self.host.calls))
        self.assertTrue((self.handoff / 'intent.json').exists())

    def test_pid_or_start_reuse_before_stop_consumes_intent_and_refuses(self):
        original = self.host.identify
        count = 0
        def reused(pid):
            nonlocal count
            count += 1
            info = original(pid)
            if count > 1: info['start'] = 'reused'
            return info
        self.host.identify = reused
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertTrue((self.handoff / 'intent.json').exists())
        self.assertFalse(any(c[0] == 'stop' for c in self.host.calls))
        with self.assertRaises(self.m.Refused): self.run_handoff()

    def test_new_window_or_pending_race_after_stop_is_terminal(self):
        self.host.on_stop = lambda s: s.update(inflight={'pid': 55})
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertTrue((self.handoff / 'intent.json').exists())
        self.assertFalse(any(c[0] == 'start' for c in self.host.calls))
        with self.assertRaises(self.m.Refused): self.run_handoff()

    def test_process_still_present_after_stop_never_acquires_lock_or_launches(self):
        stop = self.host.signal_and_wait
        def incomplete(expected):
            stop(expected)
            self.host.current = copy.deepcopy(expected)
        self.host.signal_and_wait = incomplete
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertFalse(any(c[0] in ('lock', 'start') for c in self.host.calls))

    def test_busy_old_or_native_lock_consumes_intent_without_launch(self):
        self.host.busy = 'supervisor.lock'
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertFalse(any(c[0] == 'start' for c in self.host.calls))
        self.assertTrue((self.handoff / 'intent.json').exists())

    def test_native_lock_busy_never_creates_new_epoch_or_launches(self):
        self.host.busy = 'i14-native-watch.lock'
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertFalse(self.new.exists())
        self.assertFalse(any(c[0] == 'start' for c in self.host.calls))

    def test_existing_epoch_or_guard_is_not_overwritten_or_used_as_latch_clear(self):
        self.new.mkdir()
        (self.new / 'witness').write_text('old unknown')
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertEqual((self.new / 'witness').read_text(), 'old unknown')
        self.assertEqual(self.host.calls, [])

    def test_crash_after_durable_intent_never_retries_even_with_fresh_state(self):
        def crash(expected): raise RuntimeError('simulated host crash')
        self.host.signal_and_wait = crash
        with self.assertRaises(RuntimeError): self.run_handoff()
        self.assertTrue((self.handoff / 'intent.json').exists())
        with self.assertRaises(self.m.Refused): self.run_handoff()

    def test_launch_timeout_is_unknown_and_never_starts_a_second_actor(self):
        self.host.ready = False
        with self.assertRaises(self.m.Refused): self.run_handoff()
        receipt = json.loads((self.handoff / 'launch.json').read_text())
        self.assertEqual(receipt['pid'], 9123)
        self.assertEqual(receipt['status'], 'LAUNCH_UNKNOWN')
        with self.assertRaises(self.m.Refused): self.run_handoff()
        self.assertEqual(len([c for c in self.host.calls if c[0] == 'start']), 1)

    def test_lineage_binds_final_old_evidence_and_immutable_new_package(self):
        self.run_handoff()
        self.m.verify_lineage(self.root, 44, self.host)
        p = self.old / 'state.json'
        p.write_bytes(p.read_bytes() + b' ')
        with self.assertRaises(self.m.Refused): self.m.verify_lineage(self.root, 44, self.host)

    def test_staged_code_pin_real_root_and_read_only_action_allowlist(self):
        base = self.r2.load_base(self.root)
        self.assertEqual(base.ROOT, self.root)
        self.assertEqual(base.ACTIONS, ['power_evidence', 'cached_baseline', 'window_log_capture'])
        staged = self.root / 'artifacts/window-supervisor-r2-prep-20261101/i14-window-supervisor.py'
        staged.write_bytes(staged.read_bytes() + b'\n')
        with self.assertRaises(self.r2.Refused): self.r2.load_base(self.root)

    def test_reading_a_file_may_update_atime_without_changing_evidence(self):
        p = self.root / 'atime-fixture'
        p.write_bytes(b'unchanged evidence')
        original = self.m.os.fstat
        count = 0
        def observed(fd):
            nonlocal count
            count += 1
            info = original(fd)
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            fields['st_atime_ns'] += count
            return SimpleNamespace(**fields)
        with patch.object(self.m.os, 'fstat', side_effect=observed):
            self.assertEqual(self.m.read(p), b'unchanged evidence')

    def test_r2_cli_refuses_missing_lease_or_unreviewed_package_before_constructor(self):
        self.assertEqual(self.r2.main([]), 5)
        self.package['reviewed'] = False
        self.write_package()
        with patch.object(self.r2, 'run', side_effect=AssertionError('must validate before run')):
            self.assertEqual(self.r2.main(['--legacy-lock-fd', '44']), 5)

    def test_real_process_adapter_uses_only_exact_metadata_then_one_signal(self):
        host = self.m.RealHost()
        ps = type('PS', (), {'returncode': 0, 'stdout': '8123 Sun Nov  1 23:15:08 2026 /own/Python -I -B /own/script\n'})()
        with patch.object(self.m.subprocess, 'run', return_value=ps) as probe:
            info = host.identify(8123)
        self.assertEqual(info, {'pid': 8123, 'start': 'Sun Nov 1 23:15:08 2026',
                                'argv': ['/own/Python', '-I', '-B', '/own/script']})
        self.assertEqual(probe.call_args.args[0], ['/bin/ps', '-p', '8123', '-o', 'pid=,lstart=,command='])
        with patch.object(host, 'identify', side_effect=[info, None]), \
                patch.object(self.m.os, 'kill') as kill:
            host.signal_and_wait(info)
        kill.assert_called_once_with(8123, self.m.signal.SIGTERM)
        with patch.object(host, 'identify', return_value={**info, 'start': 'new'}), \
                patch.object(self.m.os, 'kill', side_effect=AssertionError('no reused PID signal')):
            with self.assertRaises(self.m.Refused): host.signal_and_wait(info)

    def test_r2_real_loop_boundary_publishes_ready_before_first_scan_and_keeps_lineage(self):
        self.run_handoff()
        base = self.r2.load_base(self.root)
        package, package_sha = base.package_at(self.package_path)
        calls = []
        class FakeSupervisor:
            def __init__(self, state_dir, manifest):
                calls.append(('construct', state_dir, manifest))
                self.package_sha = package_sha
                self.state = {}
                self.check_pins()
            def check_pins(self): calls.append(('pins',))
            def save(self): calls.append(('save', copy.deepcopy(self.state)))
            def request_stop(self, *args): calls.append(('stop-request',))
            def tick(self):
                self_case.assertTrue((self_case.new / 'ready.json').exists())
                calls.append(('tick',))
                return False
            def close(self): calls.append(('close',))
        self_case = self
        base.Supervisor = FakeSupervisor
        real_close = self.r2.os.close
        def close(fd):
            if fd == 44: calls.append(('close-legacy',))
            else: real_close(fd)
        with patch.object(self.r2.signal, 'signal') as handlers, \
                patch.object(self.r2.os, 'getpid', return_value=9123), \
                patch.object(self.r2.os, 'close', side_effect=close):
            self.assertEqual(self.r2.run(self.root, 44, base, package, host=self.host), 0)
        self.assertEqual(handlers.call_count, 2)
        self.assertIn(('tick',), calls)
        self.assertEqual(calls[-2:], [('close',), ('close-legacy',)])
        ready = json.loads((self.new / 'ready.json').read_text())
        self.assertEqual(ready['status'], 'READY_BEFORE_FIRST_SCAN')
        self.assertEqual(ready['pid'], 9123)
        self.assertEqual(ready['package_sha256'], package_sha)


if __name__ == '__main__':
    unittest.main()
