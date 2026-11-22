#!/usr/bin/env python3
"""One consumed host handoff: reconcile r1, stop its exact actor, launch fixed r2.

Never a phone command. Unknown outcomes preserve all receipts and cannot be retried.
The shipped package is unreviewed and therefore refuses before process inspection.
"""
import contextlib
import copy
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
OLD = 'artifacts/i14-autonomous-state-r1'
NEW = 'artifacts/i14-autonomous-state-r2'
ONCE = 'artifacts/i14-r2-handoff-once'
PACKAGE = 'artifacts/i14-autonomous-windows-20261102-r2/manifest.json'
PREFLIGHT_SHA256 = 'b8578b17fab5b967423e1c34cad40ab7833be82a91e9e1a82a1194542cba1b43'
R1_PACKAGE_SHA256 = '581e48c4e4a9b7b5761341190f68820594638f279b7b4b2b73bcc0ac2c6b6908'
R2_STAGED_SHA256 = '21299b244b5ee73dbc7aab7797fe22be6a6970fadb68264ce1acbdd932630ecf'
R1_EXECUTABLE = os.environ.get('I14_R1_PYTHON', sys.executable)


class Refused(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise Refused(reason)


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    require(path.resolve() == path, 'symlinked input: ' + str(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 64 * 1024 * 1024, 'invalid local file')
        value = stream.read(64 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    def identity(info):
        # Reading may itself change atime; inode/content timestamps may not move.
        return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)
    require(len(value) <= 64 * 1024 * 1024 and identity(info) == identity(after), 'local input changed')
    require(identity(path.stat(follow_symlinks=False)) == identity(after), 'local path replaced')
    return value


def data(path):
    return json.loads(read(path))


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exclusive(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    sync(path.parent)


def module_at(path, pin, name):
    code = read(path)
    require(sha(code) == pin, 'source pin mismatch: ' + str(path))
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(code, str(path), 'exec'), module.__dict__)
    return module


def package_at(root):
    base = module_at(root / 'artifacts/window-supervisor-r2-prep-20261101/i14-window-supervisor.py',
                     R2_STAGED_SHA256, 'r2_package_helpers')
    base.ROOT = root
    try:
        package, package_sha = base.package_at(root / PACKAGE)
    except base.IntegrityError as error:
        raise Refused(str(error)) from error
    required_pins = {'scripts/i14-window-supervisor-r2.py', 'scripts/i14-r2-host-handoff.py',
                     'scripts/i14-r2-handoff-preflight.py'}
    require(required_pins <= set(package.get('source_sha256', {})), 'missing integration source pins')
    pins = {**package['source_sha256'], 'scripts/wait-native-cached-once.py': package['worker_sha256'],
            'scripts/i14-window-log-capture.py': base.LOG_WORKER_SHA256}
    for name, pin in pins.items():
        require(sha(read(root / name)) == pin, 'new package source changed: ' + name)
    return package, package_sha


def guards(root):
    for rel in [OLD + '/STOP', NEW + '/STOP', 'artifacts/i14-window-log-capture-once',
                'artifacts/i14-window-log-capture-supervisor-reservation.json']:
        require(not os.path.lexists(root / rel), 'STOP or consumed global action: ' + rel)


def validate_process(info, pid, root):
    tail = ['-I', '-B', str(root / 'scripts/i14-window-supervisor.py'), str(root / OLD),
            str(root / 'artifacts/i14-autonomous-windows-20261101-r1/manifest.json')]
    require(isinstance(info, dict) and info.get('pid') == pid and type(pid) is int and pid > 0
            and isinstance(info.get('start'), str) and bool(info['start'])
            and isinstance(info.get('argv'), list) and len(info['argv']) == 6
            and info['argv'][1:] == tail and info['argv'][0] == R1_EXECUTABLE,
            'old process identity/start metadata mismatch')


def final_snapshot(root, before_state, before_journal):
    state = data(root / OLD / 'state.json')
    expected = copy.deepcopy(before_state)
    expected.update(phase='STOPPED', event_seq=before_state['event_seq'] + 1)
    require(state == expected, 'state changed or native custody raced with host stop')
    journal = read(root / OLD / 'events.jsonl')
    require(journal.startswith(before_journal) and journal.endswith(b'\n'), 'old journal changed')
    tail = journal[len(before_journal):].splitlines()
    require(len(tail) == 1, 'unexpected events during old stop')
    event = json.loads(tail[0])
    require(event.get('seq') == expected['event_seq'] and event.get('type') == 'STOP'
            and event.get('data') == {'owned_worker_active': False, 'owned_pid': None, 'recovered_pid': None},
            'old stop does not prove no owned/recovered worker')
    return state, journal


class RealHost:
    def identify(self, pid):
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'pid=,lstart=,command='],
                                capture_output=True, text=True, timeout=5, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
        if result.returncode == 1 and not result.stdout.strip():
            return None
        require(result.returncode == 0, 'process metadata unavailable')
        parts = result.stdout.strip().split(None, 6)
        require(len(parts) == 7 and parts[0].isdigit(), 'incomplete process metadata')
        return {'pid': int(parts[0]), 'start': ' '.join(parts[1:6]), 'argv': shlex.split(parts[6])}

    def signal_and_wait(self, expected):
        require(self.identify(expected['pid']) == expected, 'process changed before SIGTERM')
        os.kill(expected['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = self.identify(expected['pid'])
            if current is None:
                return
            require(current == expected, 'PID reused after stop; no further signals')
            time.sleep(0.1)
        raise Refused('old process exit UNKNOWN; no second signal or restart')

    @contextlib.contextmanager
    def lock(self, path):
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode), 'invalid lockfile')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield fd
        finally:
            os.close(fd)

    def verify_inherited_lock(self, fd, path):
        expected = path.stat(follow_symlinks=False)
        actual = os.fstat(fd)
        require(stat.S_ISREG(actual.st_mode) and (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino),
                'inherited r1 singleton lock mismatch')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def start(self, argv, old_lock_fd, out):
        with (out / 'actor.stdout').open('xb') as stdout, (out / 'actor.stderr').open('xb') as stderr:
            process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, close_fds=True,
                                       pass_fds=(old_lock_fd,), start_new_session=True, shell=False)
        return process.pid

    def await_ready(self, pid, directory, lineage_sha256, package_sha256):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            path = directory / 'ready.json'
            if path.exists():
                value = data(path)
                require(value.get('pid') == pid and value.get('lineage_sha256') == lineage_sha256
                        and value.get('package_sha256') == package_sha256
                        and value.get('status') == 'READY_BEFORE_FIRST_SCAN', 'new actor readiness mismatch')
                return value
            time.sleep(0.1)
        raise TimeoutError('new actor readiness UNKNOWN; preserve launch, no retry')


def execute(root=ROOT, *, host=None):
    root = Path(root).resolve()
    host = RealHost() if host is None else host
    try:
        package, package_sha = package_at(root)  # Refuses unreviewed before any process boundary.
        guards(root)
        require(not os.path.lexists(root / NEW) and not os.path.lexists(root / ONCE), 'handoff/epoch already consumed')
        preflight = module_at(root / 'scripts/i14-r2-handoff-preflight.py', PREFLIGHT_SHA256, 'pinned_host_preflight')
        try:
            snapshot = preflight.check(root)
        except preflight.Refused as error:
            raise Refused(str(error)) from error
        old_state = data(root / OLD / 'state.json')
        old_journal = read(root / OLD / 'events.jsonl')
        require(sha(encoded(old_state)) == sha(encoded(data(root / OLD / 'state.json'))), 'old state unstable')
        pid = snapshot['old_supervisor_pid_metadata']
        identity = host.identify(pid)
        validate_process(identity, pid, root)
        (root / ONCE).mkdir(mode=0o700, exist_ok=False)
        sync(root / 'artifacts')
        intent = {'version': 1, 'status': 'CONSUMED_BEFORE_STOP', 'old_epoch': str(root / OLD),
                  'new_epoch': str(root / NEW), 'old_process': identity, 'snapshot': snapshot,
                  'new_package_sha256': package_sha, 'pmic_adc_status': 'UNKNOWN_NOT_CLEARED'}
        exclusive(root / ONCE / 'intent.json', intent)
        # Fresh complete snapshot and process binding after durable intent.
        require(preflight.check(root) == snapshot and host.identify(pid) == identity,
                'state/process changed after intent; handoff consumed')
        guards(root)
        host.signal_and_wait(identity)
        require(host.identify(pid) is None, 'old process exit not proven')
        with host.lock(root / OLD / 'supervisor.lock') as old_fd:
            with host.lock(root / 'artifacts/i14-native-watch.lock'):
                guards(root)
                final_state, final_journal = final_snapshot(root, old_state, old_journal)
                # Every immutable evidence input from the snapshot remains byte-identical.
                for name, pin in snapshot['snapshot_sha256'].items():
                    if name in (OLD + '/state.json', OLD + '/events.jsonl'):
                        continue
                    require(sha(read(root / name)) == pin, 'old evidence changed: ' + name)
                require(package_at(root)[1] == package_sha, 'new package changed after stop')
                (root / NEW).mkdir(mode=0o700, exist_ok=False)
                sync(root / 'artifacts')
                lineage = {'version': 1, 'old_epoch': str(root / OLD), 'old_package_sha256': R1_PACKAGE_SHA256,
                           'old_final_event_seq': final_state['event_seq'], 'old_final_state_sha256': sha(read(root / OLD / 'state.json')),
                           'old_final_journal_sha256': sha(final_journal), 'old_snapshot': snapshot,
                           'new_package_sha256': package_sha, 'pmic_adc_status': 'UNKNOWN_NOT_CLEARED',
                           'intent_sha256': sha(read(root / ONCE / 'intent.json'))}
                exclusive(root / NEW / 'lineage.json', lineage)
                lineage_sha = sha(read(root / NEW / 'lineage.json'))
                exclusive(root / ONCE / 'launch-reserved.json', {'status': 'LAUNCH_RESERVED',
                    'lineage_sha256': lineage_sha, 'package_sha256': package_sha})
            # Native lock is now released for the fixed worker. The old singleton
            # stays held and is inherited by r2, preventing a second r1 actor.
            # Same task-bound idle-sleep prevention as the existing r1 launch.
            argv = ['/usr/bin/caffeinate', '-i', sys.executable, '-I', '-B', str(root / 'scripts/i14-window-supervisor-r2.py'),
                    '--legacy-lock-fd', str(old_fd)]
            guards(root)
            require(package_at(root)[1] == package_sha, 'new package changed before launch')
            new_pid = host.start(argv, old_fd, root / ONCE)
            exclusive(root / ONCE / 'launch.json', {'status': 'LAUNCH_UNKNOWN', 'pid': new_pid, 'argv': argv})
            ready = host.await_ready(new_pid, root / NEW, lineage_sha, package_sha)
            result = {'status': 'ACTOR_READY', 'pid': new_pid, 'ready': ready,
                      'charging_status': 'UNKNOWN', 'charging_qualified': False,
                      'persistent_installation_complete': False, 'old_epoch_preserved': True}
            exclusive(root / ONCE / 'result.json', result)
            return result
    except Refused:
        raise
    except (OSError, ValueError, KeyError, TypeError, TimeoutError) as error:
        raise Refused('handoff incomplete; preserve consumed intent/launch, no retry: ' + str(error)) from error


def verify_lineage(root, legacy_fd, host, *, check_process=True):
    root = Path(root).resolve()
    package, package_sha = package_at(root)
    lineage = data(root / NEW / 'lineage.json')
    require(lineage.get('old_epoch') == str(root / OLD) and lineage.get('old_package_sha256') == R1_PACKAGE_SHA256
            and lineage.get('new_package_sha256') == package_sha
            and lineage.get('pmic_adc_status') == 'UNKNOWN_NOT_CLEARED', 'lineage identity/package mismatch')
    require(not os.path.lexists(root / OLD / 'STOP') and not os.path.lexists(root / NEW / 'STOP'), 'explicit STOP remains binding')
    require(sha(read(root / OLD / 'state.json')) == lineage.get('old_final_state_sha256')
            and sha(read(root / OLD / 'events.jsonl')) == lineage.get('old_final_journal_sha256')
            and sha(read(root / ONCE / 'intent.json')) == lineage.get('intent_sha256'), 'old evidence/intent changed')
    launch = data(root / ONCE / 'launch-reserved.json')
    require(launch == {'status': 'LAUNCH_RESERVED', 'lineage_sha256': sha(read(root / NEW / 'lineage.json')),
                       'package_sha256': package_sha}, 'launch/lineage binding mismatch')
    intent = data(root / ONCE / 'intent.json')
    if check_process:
        require(host.identify(intent['old_process']['pid']) is None, 'old process still present or PID reused')
    host.verify_inherited_lock(legacy_fd, root / OLD / 'supervisor.lock')
    return lineage


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv != ['--execute-once']:
        print('usage: i14-r2-host-handoff.py --execute-once', file=sys.stderr)
        return 5
    try:
        print(json.dumps(execute()))
        return 0
    except Refused as error:
        print('REFUSED: ' + str(error), file=sys.stderr)
        return 5


if __name__ == '__main__':
    sys.exit(main())
