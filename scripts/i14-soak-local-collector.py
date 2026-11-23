#!/usr/bin/env python3
"""Bounded local raw observer. Manual calls never establish cron origin."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import subprocess
import time

_spec = importlib.util.spec_from_file_location(
    'observer_inventory', Path(__file__).with_name('i14-soak-observer-inventory.py'))
_inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_inventory)

LOG_NAMES = ['messages' + suffix for suffix in ['', '.0', '.1', '.2', '.3', '.4']] + ['i14-battery.log']
REQUIRED = {'messages', 'i14-battery.log'}
BASE = Path('/mnt/data/media/0/i14-linux/soak-c-observer-20261115')
LOG_ROOT = Path('/var/log')


class CaptureInterrupted(Exception):
    """Deadline/cancellation must never be treated as a recoverable log read error."""


def assert_source_location(root):
    if (Path(__file__).resolve() != root/'i14-soak-local-collector.py'
            or Path(_inventory.__file__).resolve() != root/'i14-soak-observer-inventory.py'):
        raise ValueError('executing collector/helper do not match pinned run directory')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def copy_logs(source, target, budget):
    result = {'ok': [], 'absent': [], 'failed': [], 'files': {}}
    for name in LOG_NAMES:
        try:
            raw, metadata = _inventory.read_snapshot(source / name)
            if raw is None:
                result['failed' if name in REQUIRED else 'absent'].append(
                    name + '(absent)' if name in REQUIRED else name)
                continue
            if len(raw) > budget:
                result['failed'].append(name + '(budget)')
                continue
            with (target / name).open('xb') as output:
                output.write(raw)
            budget -= len(raw)
            result['ok'].append(name)
            result['files'][name] = {'sha256': metadata['sha256'], 'bytes': len(raw)}
        except (OSError, ValueError) as error:
            result['failed'].append(name + '(read)')
            with (target / (name + '.err')).open('xb') as output:
                output.write((type(error).__name__ + '\n').encode())
    return result


def cron_origin(rows):
    return any(row['uid'] == 0 and row['comm'] in ['crond', 'cron'] for row in rows)


def ancestry(pid):
    rows = []
    seen = set()
    while pid:
        if pid in seen or len(rows) >= 32:
            raise ValueError('invalid process ancestry')
        seen.add(pid)
        directory = Path('/proc') / str(pid)
        fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        uid = next(line for line in (directory / 'status').read_text().splitlines()
                   if line.startswith('Uid:')).split()[1]
        row = {'pid': pid, 'ppid': int(fields[1]), 'uid': int(uid),
               'comm': (directory / 'comm').read_text().strip(), 'start_ticks': int(fields[19])}
        rows.append(row)
        if pid == 1:
            break
        pid = row['ppid']
    return rows


def check_limits(config, now, count, used, free):
    if now >= config['expires_epoch']:
        raise ValueError('observer expired')
    if count >= config['max_captures']:
        raise ValueError('observer capture count reached')
    if used + config['capture_bytes'] > config['max_bytes']:
        raise ValueError('observer storage quota reached')
    if free < config['reserve_bytes'] + config['capture_bytes']:
        raise ValueError('observer free-space reserve reached')


def usage(root):
    used = 0
    for path in root.rglob('*'):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError('unexpected output symlink')
        if stat.S_ISREG(metadata.st_mode):
            used += max(metadata.st_size, metadata.st_blocks * 512)
    count = sum(1 for path in (root/'logs').iterdir() if re.fullmatch(r'\d{8}T\d{6}Z', path.name))
    space = os.statvfs(root)
    return count, used, space.f_bavail * space.f_frsize


def validate_procs(raw, boot):
    lines = raw.decode().splitlines()
    if len(lines) < 12 or lines[1] != boot:
        raise ValueError('process capture incomplete or wrong boot')
    datetime.strptime(lines[0], '%Y-%m-%dT%H:%M:%SZ')
    seen = set()
    for line in lines[2:]:
        pid, ticks, command = line.split(None, 2)
        pid, ticks = int(pid), int(ticks)
        if pid <= 0 or ticks < 0 or pid in seen or not command:
            raise ValueError('process row invalid')
        seen.add(pid)


def append_index(path, stamp, ok, absent, failed):
    datetime.strptime(stamp, '%Y%m%dT%H%M%SZ')
    line = (stamp + ' ok: ' + ' '.join(ok) + ' absent: ' + (' '.join(absent) or 'none')
            + ' failed: ' + (' '.join(failed) or 'none') + '\n')
    if len(line.encode()) > 1024:
        raise ValueError('index row exceeds metadata budget')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'ab') as output:
        if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
            raise ValueError('index is not regular')
        output.write(line.encode())
        output.flush()
        os.fsync(output.fileno())


def owned_file(path, mode):
    raw, metadata = _inventory.read_snapshot(path)
    if raw is None or metadata['uid'] != os.geteuid() or int(metadata['mode'], 8) != mode:
        raise ValueError('observer source/config ownership differs')
    return raw


def load_config(root):
    assert_source_location(root)
    raw = owned_file(root / 'observer.json', 0o600)
    config = json.loads(raw)
    keys = {'schema', 'grant_id', 'expires_epoch', 'max_captures', 'max_bytes',
            'reserve_bytes', 'capture_bytes', 'source_sha256'}
    phase = root.name.startswith('phase-')
    if phase:
        keys.add('boot_id')
    if set(config) != keys or config['schema'] != (2 if phase else 1):
        raise ValueError('observer configuration schema differs')
    for name in ['expires_epoch', 'max_captures', 'max_bytes', 'reserve_bytes', 'capture_bytes']:
        if type(config[name]) is not int or config[name] <= 0:
            raise ValueError('observer limit is not a positive integer')
    if phase and (config['max_captures'] > 80 or config['max_bytes'] > 1281 << 20
                  or config['capture_bytes'] != 16 << 20 or config['reserve_bytes'] != 512 << 20):
        raise ValueError('phase limits differ from the closed-phase exporter')
    if not isinstance(config['grant_id'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', config['grant_id']):
        raise ValueError('observer grant identifier malformed')
    if config['max_captures'] > 1000 or not 131072 <= config['capture_bytes'] <= 16777216:
        raise ValueError('observer capture bounds differ')
    expected = {'i14-soak-local-collector.py', 'i14-soak-observer-inventory.py', 'procs.sh'}
    if set(config['source_sha256']) != expected:
        raise ValueError('observer source pin set differs')
    for name, want in config['source_sha256'].items():
        if not isinstance(want, str) or not re.fullmatch('[0-9a-f]{64}', want):
            raise ValueError('observer source pin is malformed')
        if digest(owned_file(root / name, 0o700)) != want:
            raise ValueError('observer source hash differs')
    return config, digest(raw)


def write_json(path, value):
    raw = (json.dumps(value, indent=2) + '\n').encode()
    if len(raw) > 32768:
        raise ValueError('receipt exceeds metadata budget')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def capture_procs(script, capture, timeout=10):
    """Bound output in the child, and close the entire owned group on every exit."""
    numbers = {signal.SIGINT, signal.SIGTERM, signal.SIGALRM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    child = None
    result = {'rc': None, 'group_closed': False}
    def child_limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (32768, 32768))
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    try:
        with (capture/'procs.txt').open('xb') as stdout, (capture/'procs.err').open('xb') as stderr:
            child = subprocess.Popen(['/bin/sh', str(script)], stdout=stdout, stderr=stderr,
                                     start_new_session=True, preexec_fn=child_limits)
            result['pid'] = child.pid
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            result['rc'] = child.wait(timeout=timeout)
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
        try:
            if child is not None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=3)
                # The trusted helper has no background children. A surviving group is UNKNOWN.
                try:
                    os.killpg(child.pid, 0)
                except ProcessLookupError:
                    result['group_closed'] = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    return result


def runtime_identity():
    _inventory.check_identity(os.geteuid(), os.uname().nodename, os.uname().release,
                              Path('/proc/device-tree/compatible').read_bytes().decode())
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', boot):
        raise ValueError('invalid boot identity')
    return boot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--origin', choices=['manual', 'cron'], required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    root = args.run_dir.absolute()
    if root.resolve() != root or not root.is_relative_to(BASE) or root == BASE:
        raise ValueError('observer directory is not a dedicated userdata child')
    info = root.stat()
    if os.geteuid() != 0 or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('observer must run as root in own0700 directory')
    boot = runtime_identity()
    config, config_sha = load_config(root)
    if 'boot_id' in config and config['boot_id'] != boot:
        raise ValueError('observer boot differs from frozen phase')
    if os.path.lexists(root / 'STOPPED.json'):
        return 3
    rows = ancestry(os.getppid())
    cron = cron_origin(rows)
    if args.origin == 'cron' and not cron:
        raise ValueError('actual root-cron ancestry not proven')
    logs = root / 'logs'
    receipts = root / 'receipts'
    for directory in [logs, receipts]:
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError('observer output directory differs')
    count, used, free = usage(root)
    try:
        check_limits(config, time.time(), count, used, free)
    except ValueError as error:
        try:
            write_json(root / 'STOPPED.json', {'utc': datetime.now(timezone.utc).isoformat(),
                                              'reason': str(error), 'count': count, 'used_bytes': used})
        except FileExistsError:
            pass
        return 3
    lock = root / '.capture.lock'
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGINT, signal.SIGTERM})
    lock_fd = None
    started = datetime.now(timezone.utc)
    report = {'start_utc': started.isoformat(), 'monotonic_start': time.monotonic(),
              'boot_id': boot, 'pid': os.getpid(), 'origin': args.origin,
              'cron_origin_proven': args.origin == 'cron' and cron, 'ancestry': rows,
              'source_pins': config['source_sha256'], 'config_sha256': config_sha,
              'grant_id': config['grant_id'], 'result': 'FAIL', 'rc': 1}
    previous_handlers = {}
    def interrupted(signum, frame):
        report['interruption_signal'] = signum
        raise CaptureInterrupted('observer interrupted or timed out')
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        lock_stat = os.fstat(lock_fd)
        os.write(lock_fd, str(os.getpid()).encode())
        for number in [signal.SIGALRM, signal.SIGINT, signal.SIGTERM]:
            previous_handlers[number] = signal.signal(number, interrupted)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        signal.alarm(40)
        # Existing minute probes may take5s; collect after their initial write burst.
        time.sleep(7)
        # Refuse stale pre-lock count/space/expiry after a competing capture finishes.
        if os.path.lexists(root / 'STOPPED.json'):
            raise ValueError('observer phase already stopped')
        check_limits(config, time.time(), *usage(root))
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        capture = logs / stamp
        capture.mkdir(mode=0o700)
        report['capture_directory'] = str(capture)
        result = copy_logs(LOG_ROOT, capture, config['capture_bytes'] - 131072)
        try:
            process = capture_procs(root / 'procs.sh', capture)
            report['process_actor'] = process
            if process['rc'] != 0 or not process['group_closed']:
                raise ValueError('process command returned nonzero')
            raw = (capture / 'procs.txt').read_bytes()
            validate_procs(raw, boot)
            result['ok'].append('procs')
            result['files']['procs.txt'] = {'sha256': digest(raw), 'bytes': len(raw)}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result['failed'].append('procs(read)')
        append_index(logs / 'index.txt', stamp, result['ok'], result['absent'], result['failed'])
        report.update(result)
        if load_config(root)[1] != config_sha:
            raise ValueError('observer configuration changed during capture')
        if runtime_identity() != boot:
            raise ValueError('boot changed during capture')
        report['result'] = 'PASS' if not result['failed'] else 'FAIL'
        report['rc'] = 0 if report['result'] == 'PASS' else 1
    except BaseException as error:
        report['error_type'] = type(error).__name__
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGINT, signal.SIGTERM})
        signal.alarm(0)
        try:
            if lock_fd is not None:
                current = lock.lstat()
                if (current.st_dev, current.st_ino) != (lock_stat.st_dev, lock_stat.st_ino):
                    raise ValueError('observer lock identity changed; retain for reconciliation')
                lock.unlink()
                report['end_utc'] = datetime.now(timezone.utc).isoformat()
                report['monotonic_end'] = time.monotonic()
                path = receipts / (started.strftime('%Y%m%dT%H%M%S.%fZ') + '-' + str(os.getpid()) + '.json')
                write_json(path, report)
                print(json.dumps({'result': report['result'], 'rc': report['rc'], 'receipt': str(path)}))
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    return report['rc']


if __name__ == '__main__':
    raise SystemExit(main())
