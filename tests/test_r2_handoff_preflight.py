"""Host files only; no real supervisor, scan, socket, lock or process is used."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/i14-r2-handoff-preflight.py'


class HandoffPreflightTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'missing host-only preflight')
        spec = importlib.util.spec_from_file_location('r2_preflight', SCRIPT)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state_dir = self.root / 'artifacts/i14-autonomous-state-r1'
        self.state_dir.mkdir(parents=True)
        (self.root / 'artifacts/i14-native-watch.lock').touch()
        package = json.loads((ROOT / 'artifacts/i14-autonomous-windows-20261101-r1/manifest.json').read_text())
        pins = dict(package['source_sha256'])
        pins['scripts/wait-native-cached-once.py'] = package['worker_sha256']
        for name in pins:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        self.manifest = self.root / 'artifacts/i14-autonomous-windows-20261101-r1/manifest.json'
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(json.dumps(package))
        self.package_pin = {'version': 1, 'package_sha256': self.m.R1_PACKAGE_SHA256, 'manifest': package}
        (self.state_dir / 'package-pin.json').write_text(json.dumps(self.package_pin))
        self.out = self.state_dir / 'windows/window-000001/power_evidence'
        self.out.mkdir(parents=True)
        fixture = ROOT / 'artifacts/i14-autonomous-state-r1/windows/window-000001/power_evidence'
        for name in ('result.json', 'attempt.json', 'kernel-ringbuffer.raw'):
            (self.out / name).write_bytes((fixture / name).read_bytes())
        self.result = json.loads((self.out / 'result.json').read_text())
        self.state = {'version': 1, 'poll_seconds': 2, 'window_seq': 1,
                      'current_window': {'id': 1, 'opened_utc': 'fixture', 'blocked': False,
                          'actions': {'power_evidence': {'status': 'COMPLETE', 'out': str(self.out)}}},
                      'attachment_open': False, 'absent_streak': 3, 'reserved': None, 'inflight': None,
                      'results': [{'window': 1, 'action': 'power_evidence', 'status': 'COMPLETE',
                                   'returncode': 0, 'out': str(self.out), 'receipt': self.result}],
                      'last_boot': self.result['boot']['boot_id'], 'phase': 'WAIT_DEVICE', 'event_seq': 6,
                      'package_sha256': self.m.R1_PACKAGE_SHA256, 'global_block': None,
                      'wait_reason': None, 'recovery': None, 'worker_active_at_stop': False}
        self.write_state()
        (self.state_dir / 'coordinator.json').write_text(json.dumps({'last_seq': 6}))
        self.events = [
            {'seq': 1, 'type': 'WINDOW_OPEN', 'data': {'window': 1}},
            {'seq': 2, 'type': 'ACTION_RESERVED', 'data': {'window': 1, 'action': 'power_evidence', 'out': str(self.out)}},
            {'seq': 3, 'type': 'ACTION_FINISHED', 'data': {'window': 1, 'action': 'power_evidence', 'out': str(self.out), 'status': 'COMPLETE'}},
            {'seq': 4, 'type': 'PLAN_UPDATED', 'data': {}},
            {'seq': 5, 'type': 'WINDOW_LOST', 'data': {'window': 1}},
            {'seq': 6, 'type': 'PLAN_UPDATED', 'data': {}},
        ]
        self.write_journal()
        (self.state_dir / 'pid').write_text('8123\n')

    def write_state(self):
        (self.state_dir / 'state.json').write_text(json.dumps(self.state))

    def write_journal(self):
        (self.state_dir / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in self.events))

    def test_eligible_snapshot_is_read_only_and_never_authorizes_execution(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(socket, 'socket', side_effect=AssertionError('no socket')), \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('no process')):
            result = self.m.check(self.root)
        self.assertEqual(result['status'], 'PREFLIGHT_SNAPSHOT_ELIGIBLE')
        self.assertIs(result['execution_authorized'], False)
        self.assertEqual(result['process_liveness'], 'NOT_CHECKED')
        self.assertEqual(result['native_lock'], 'NOT_ACQUIRED')
        self.assertEqual(result['charging_status'], 'UNKNOWN')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_stop_pending_unknown_or_open_window_blocks(self):
        for key, value in [('phase', 'STOPPED'), ('attachment_open', True), ('global_block', 'identity'),
                           ('reserved', {'action': 'unknown'}), ('inflight', {'action': 'unknown'}),
                           ('recovery', {'reason': 'UNKNOWN'}), ('worker_active_at_stop', True)]:
            with self.subTest(key=key):
                original = copy.deepcopy(self.state)
                self.state[key] = value
                self.write_state()
                with self.assertRaises(self.m.Refused): self.m.check(self.root)
                self.state = original
        self.write_state()
        (self.state_dir / 'STOP').touch()
        with self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_consumed_global_guard_or_output_is_never_cleared(self):
        for name in ('i14-window-log-capture-once', 'i14-window-log-capture-supervisor-reservation.json'):
            path = self.root / 'artifacts' / name
            path.symlink_to(self.root / 'absent')
            with self.assertRaises(self.m.Refused): self.m.check(self.root)
            self.assertTrue(path.is_symlink())
            path.unlink()

    def test_changed_source_is_refused_before_loading_it(self):
        path = self.root / 'scripts/i14-window-supervisor.py'
        path.write_text("raise AssertionError('must never execute')\n")
        with self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_incomplete_or_changed_receipt_blocks(self):
        self.state['results'][0]['status'] = 'UNCERTAIN'
        self.write_state()
        with self.assertRaises(self.m.Refused): self.m.check(self.root)
        self.state['results'][0]['status'] = 'COMPLETE'
        self.write_state()
        altered = copy.deepcopy(self.result)
        altered['storage']['all_ro'] = False
        (self.out / 'result.json').write_text(json.dumps(altered))
        with self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_raw_hash_or_result_path_mismatch_blocks(self):
        (self.out / 'kernel-ringbuffer.raw').write_bytes(b'changed')
        with self.assertRaises(self.m.Refused): self.m.check(self.root)
        self.state['results'][0]['out'] = str(self.root / 'outside')
        self.write_state()
        with self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_unprocessed_partial_or_reordered_journal_blocks(self):
        (self.state_dir / 'coordinator.json').write_text('{"last_seq":5}')
        with self.assertRaises(self.m.Refused): self.m.check(self.root)
        (self.state_dir / 'coordinator.json').write_text('{"last_seq":6}')
        valid = (self.state_dir / 'events.jsonl').read_bytes()
        for raw in (valid[:-1], b''.join(reversed(valid.splitlines(keepends=True)))):
            (self.state_dir / 'events.jsonl').write_bytes(raw)
            with self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_state_change_during_read_is_not_a_stable_snapshot(self):
        original = self.m.read_input
        count = 0
        path = self.state_dir / 'state.json'
        def changing(p):
            nonlocal count
            data, identity = original(p)
            if p == path:
                count += 1
                if count > 1: return data + b' ', identity
            return data, identity
        with patch.object(self.m, 'read_input', changing):
            with self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_report_output_is_exclusive(self):
        path = self.root / 'artifacts/r2-handoff-preflight-reports/report.json'
        self.m.write_report(path, {'execution_authorized': False}, root=self.root)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError): self.m.write_report(path, {'changed': True}, root=self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_or_conflicting_epoch_pin_blocks(self):
        path = self.state_dir / 'package-pin.json'
        path.unlink()
        with self.assertRaises(self.m.Refused): self.m.check(self.root)
        path.write_text('{"version":1,"package_sha256":"wrong","manifest":{}}')
        with self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_current_actions_require_exactly_one_canonical_receipt(self):
        original = copy.deepcopy(self.state)
        variants = []
        altered = copy.deepcopy(original)
        altered['current_window']['actions']['cached_baseline'] = {'status': 'COMPLETE', 'out': str(self.out.parent / 'cached_baseline')}
        variants.append(altered)
        altered = copy.deepcopy(original)
        altered['current_window']['actions']['power_evidence']['out'] = str(self.root / 'wrong')
        variants.append(altered)
        altered = copy.deepcopy(original)
        altered['results'].append(copy.deepcopy(altered['results'][0]))
        variants.append(altered)
        for state in variants:
            with self.subTest(state=state):
                self.state = state
                self.write_state()
                with self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_journal_requires_event_shape_and_complete_reservation_history(self):
        for change in ('type', 'data', 'orphan', 'window_type'):
            events = copy.deepcopy(self.events)
            if change == 'orphan':
                events[1]['data']['action'] = 'cached_baseline'
            elif change == 'window_type':
                events[1]['data']['window'] = True
            else:
                events[0].pop(change)
            (self.state_dir / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
            with self.subTest(change=change), self.assertRaises(self.m.Refused): self.m.check(self.root)

    def test_guards_or_paths_replaced_during_read_block(self):
        for variant in ('STOP', 'global_guard', 'state_symlink', 'lock_symlink'):
            with self.subTest(variant=variant):
                original = self.m.read_input
                pid_path = self.state_dir / 'pid'
                injected = False
                created = None
                replaced = None
                def changing(path):
                    nonlocal injected, created, replaced
                    result = original(path)
                    if path == pid_path and not injected:
                        injected = True
                        if variant in ('STOP', 'global_guard'):
                            created = self.state_dir / 'STOP' if variant == 'STOP' else self.root / 'artifacts/i14-window-log-capture-supervisor-reservation.json'
                            created.touch()
                        else:
                            replaced = self.state_dir / 'state.json' if variant == 'state_symlink' else self.root / 'artifacts/i14-native-watch.lock'
                            created = self.root / 'same-bytes'
                            created.write_bytes(replaced.read_bytes())
                            replaced.unlink()
                            replaced.symlink_to(created)
                    return result
                try:
                    with patch.object(self.m, 'read_input', changing):
                        with self.assertRaises(self.m.Refused): self.m.check(self.root)
                finally:
                    if replaced is not None:
                        data = created.read_bytes()
                        replaced.unlink()
                        replaced.write_bytes(data)
                    if created is not None: created.unlink()

    def test_report_cannot_create_control_namespace_or_follow_directory_symlink(self):
        for path in (self.state_dir / 'STOP', self.root / 'artifacts/i14-window-log-capture-supervisor-reservation.json'):
            with self.subTest(path=path), self.assertRaises(self.m.Refused):
                self.m.write_report(path, {'execution_authorized': False}, root=self.root)
            self.assertFalse(path.exists())
        folder = self.root / 'artifacts/r2-handoff-preflight-reports'
        folder.symlink_to(self.state_dir, target_is_directory=True)
        with self.assertRaises((self.m.Refused, OSError)):
            self.m.write_report(folder / 'evil.json', {}, root=self.root)
        self.assertFalse((self.state_dir / 'evil.json').exists())

    def test_cli_rejects_stop_output_without_creating_marker(self):
        stop = self.state_dir / 'STOP'
        with patch.object(self.m, 'ROOT', self.root), \
                patch.object(self.m, 'check', return_value={'status': 'PREFLIGHT_SNAPSHOT_ELIGIBLE', 'execution_authorized': False}), \
                patch.object(self.m.sys, 'argv', ['preflight', '--output', str(stop)]), \
                patch.object(self.m.sys, 'stderr'):
            self.assertEqual(self.m.main(), 5)
        self.assertFalse(stop.exists())


if __name__ == '__main__': unittest.main()
