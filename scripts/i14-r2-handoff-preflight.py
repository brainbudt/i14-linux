#!/usr/bin/env python3
"""Read-only r1 snapshot check. Never stops, starts, locks or migrates anything."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
R1_SOURCE_SHA256 = '86b57d49a161265db74a05215b2b3e996a6aa626cfca666cbdae7b405c6c633a'
R1_PACKAGE_SHA256 = '581e48c4e4a9b7b5761341190f68820594638f279b7b4b2b73bcc0ac2c6b6908'


class Refused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_input(path):
    require(path.resolve() == path, 'symlinked input path: ' + str(path))
    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= 64 * 1024 * 1024, 'invalid input type/size')
        data = stream.read(64 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    require(len(data) <= 64 * 1024 * 1024 and identity(before) == identity(after), 'input changed during read')
    require(path.resolve() == path and identity(path.stat(follow_symlinks=False)) == identity(after),
            'input path replaced during read: ' + str(path))
    return data, identity(after)


def check(root=ROOT):
    try:
        return _check(Path(root).resolve())
    except Refused:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Refused('unusable local metadata: ' + str(error)) from error


def _check(root):
    state_dir = root / 'artifacts/i14-autonomous-state-r1'
    tracked = {}

    def read(path):
        value = read_input(path)
        require(path not in tracked or tracked[path] == value, 'input changed between reads: ' + str(path))
        tracked[path] = value
        data = value[0]
        return data

    def check_guards():
        require(not os.path.lexists(state_dir / 'STOP'), 'explicit STOP present')
        for name in ('i14-window-log-capture-once', 'i14-window-log-capture-supervisor-reservation.json'):
            require(not os.path.lexists(root / 'artifacts' / name), 'global log attempt already consumed')
    check_guards()
    source_path = root / 'scripts/i14-window-supervisor.py'
    code = read(source_path)
    require(digest(code) == R1_SOURCE_SHA256, 'r1 source pin mismatch before loading helper functions')
    base = types.ModuleType('pinned_r1_readonly_helpers')
    base.__file__ = str(source_path)
    # Definitions only; no Supervisor constructor/main or bytecode-cache writes.
    exec(compile(code, str(source_path), 'exec'), base.__dict__)
    manifest = root / 'artifacts/i14-autonomous-windows-20261101-r1/manifest.json'
    read(manifest)
    try:
        package, package_sha = base.package_at(manifest)
    except base.IntegrityError as error:
        raise Refused(str(error)) from error
    require(package_sha == R1_PACKAGE_SHA256, 'unexpected r1 package')
    epoch_pin = json.loads(read(state_dir / 'package-pin.json'))
    require(epoch_pin == {'version': 1, 'package_sha256': package_sha, 'manifest': package}, 'epoch package pin mismatch')
    pins = dict(package['source_sha256'])
    pins['scripts/wait-native-cached-once.py'] = package['worker_sha256']
    for name, expected in pins.items():
        require(digest(read(root / name)) == expected, 'active source pin mismatch: ' + name)
    state = json.loads(read(state_dir / 'state.json'))
    coordinator = json.loads(read(state_dir / 'coordinator.json'))
    holder = types.SimpleNamespace(state=state, package_sha=package_sha)
    holder.action_path = lambda window, action: state_dir / 'windows' / ('window-%06d' % window) / action
    try:
        base.Supervisor.validate_state(holder)
    except base.IntegrityError as error:
        raise Refused(str(error)) from error
    require(state['phase'] == 'WAIT_DEVICE' and state['attachment_open'] is False and state['absent_streak'] == 3,
            'r1 must be idle after a closed physical window')
    require(state['reserved'] is None and state['inflight'] is None and state['recovery'] is None
            and state['global_block'] is None and state['worker_active_at_stop'] is False,
            'pending, uncertain or blocked r1 custody')
    require(state['current_window'] and state['current_window']['blocked'] is False, 'blocked or absent current window')
    require(state['results'] and all(a.get('status') == 'COMPLETE' for a in state['current_window']['actions'].values()),
            'unfinished current-window action')
    journal = read(state_dir / 'events.jsonl').splitlines(keepends=True)
    require(journal and all(line.endswith(b'\n') for line in journal), 'partial or empty journal')
    events = [json.loads(line) for line in journal]
    require(all(type(e.get('seq')) is int for e in events)
            and [e['seq'] for e in events] == list(range(1, state['event_seq'] + 1)), 'journal sequence mismatch')
    require(all(isinstance(e.get('type'), str) and isinstance(e.get('data'), dict) for e in events), 'invalid journal event shape')
    require(type(coordinator.get('last_seq')) is int and coordinator['last_seq'] == state['event_seq'],
            'coordinator has unprocessed events')
    records = {}
    for record in state['results']:
        action, window = record['action'], record['window']
        require(action in ('power_evidence', 'cached_baseline') and type(window) is int and 0 < window <= state['window_seq'],
                'unknown result action/window')
        out = holder.action_path(window, action)
        require(record.get('out') == str(out) and record.get('status') == 'COMPLETE'
                and type(record.get('returncode')) is int, 'unknown or misbound action outcome')
        require((window, action) not in records, 'duplicate action result')
        records[window, action] = record
        actual = json.loads(read(out / 'result.json'))
        require(actual == record['receipt'] and base.complete_receipt(actual, action, record['returncode']),
                'receipt mismatch or incomplete capture')
        require(isinstance(json.loads(read(out / 'attempt.json')), dict), 'missing attempt receipt')
        if action == 'power_evidence':
            log = actual['power_evidence']['kernel_log']
            if log['status'] == 'PRESENT':
                require(digest(read(out / 'kernel-ringbuffer.raw')) == log.get('sha256'), 'raw kernel-log hash mismatch')
    window = state['current_window']
    require(set(window['actions']) == {a for w, a in records if w == window['id']}, 'current actions/results mismatch')
    for action, info in window['actions'].items():
        expected_out = str(holder.action_path(window['id'], action))
        require(info.get('out') == expected_out and records[window['id'], action]['out'] == expected_out,
                'current action output binding mismatch')
    reserved, finished = {}, {}
    for event in events:
        if event['type'] not in ('ACTION_RESERVED', 'ACTION_FINISHED'):
            continue
        data = event['data']
        require(type(data.get('window')) is int and data.get('action') in ('power_evidence', 'cached_baseline'),
                'invalid journal action identity')
        key = (data.get('window'), data.get('action'))
        collection = reserved if event['type'] == 'ACTION_RESERVED' else finished
        require(key not in collection, 'duplicate journal action')
        collection[key] = data
    require(set(reserved) == set(finished) == set(records), 'journal reservation/result history mismatch')
    for key, record in records.items():
        require(reserved[key].get('out') == record['out'] and finished[key].get('out') == record['out']
                and finished[key].get('status') == 'COMPLETE', 'journal action outcome binding mismatch')
    pid = int(read(state_dir / 'pid'))
    require(pid > 0, 'invalid old supervisor PID metadata')
    lock_path = root / 'artifacts/i14-native-watch.lock'
    read(lock_path)  # Metadata/bytes only; no flock is acquired.
    # The running supervisor can change after this check. This is no execution lease.
    for path, value in tracked.items():
        require(read_input(path) == value, 'snapshot changed while reading: ' + str(path))
    check_guards()
    return {'status': 'PREFLIGHT_SNAPSHOT_ELIGIBLE', 'execution_authorized': False,
            'handoff_completed': False, 'old_supervisor_pid_metadata': pid,
            'process_liveness': 'NOT_CHECKED', 'native_lock': 'NOT_ACQUIRED',
            'charging_status': 'UNKNOWN', 'pmic_actions_allowed': False,
            'event_seq': state['event_seq'], 'source_package_sha256': package_sha,
            'snapshot_sha256': {str(p.relative_to(root)): digest(value[0]) for p, value in tracked.items()},
            'required_before_handoff': ['exclusive native custody held by a reviewed host executor',
                'fresh STOP/source/state/process recheck', 'durable intent before stopping the matching old supervisor',
                'observed old supervisor/worker exit and reconciled final receipts',
                'new epoch linked to all old evidence; no PMIC latch clear',
                'separately reviewed r2 entrypoint and migration receipt']}


def write_report(path, value, *, root=ROOT):
    root, path = Path(root).resolve(), Path(path).absolute()
    folder_name = 'r2-handoff-preflight-reports'
    require(path.parent == root / 'artifacts' / folder_name
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.json', path.name), 'report must use dedicated report namespace')
    artifact_fd = os.open(root / 'artifacts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(folder_name, 0o700, dir_fd=artifact_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(folder_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=artifact_fd)
        try:
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(artifact_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = check()
        rc = 0
    except Refused as error:
        result = {'status': 'BLOCKED', 'execution_authorized': False, 'reason': str(error)}
        rc = 2
    try:
        write_report(args.output, result, root=ROOT)
    except (OSError, Refused) as error:
        print('REFUSED: ' + str(error), file=sys.stderr)
        return 5
    print(json.dumps({'status': result['status'], 'execution_authorized': False}))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
