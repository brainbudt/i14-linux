#!/usr/bin/env python3
"""Read-only phone collector inventory; no environment, secrets or log bodies."""
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time


BASE = Path('/mnt/data/media/0/i14-linux')
SAMPLER = '/mnt/data/media/0/i14-linux/soak/i14-soak-sample'


def check_identity(uid, hostname, kernel, compatible):
    if (uid != 0 or hostname != 'i14' or kernel != '3.18.19'
            or 'mediatek,MT6735' not in compatible.split('\0')):
        raise ValueError('unexpected device identity; stop without retry')


def cron_summary(raw):
    lines = [line for line in raw.decode().splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    commands = {f"{quote}{path}{quote}" for path in (SAMPLER, SAMPLER + "-rs")
                for quote in ("", "'", '"')}
    commands.add(f"/bin/sh '{SAMPLER}'")
    expected = {f"* * * * * {cmd} # i14-soak-sample" for cmd in commands}
    sampler = [line for line in lines if line in expected]
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
            'sampler_lines': len(sampler), 'other_command_count': len(lines) - len(sampler),
            'other_line_sha256': [hashlib.sha256(line.encode()).hexdigest()
                                  for line in lines if line not in sampler]}


def read_snapshot(path):
    """One bounded descriptor snapshot; every directory and file refuses symlinks."""
    path = path.absolute()
    if path.anchor != '/' or '..' in path.parts:
        raise ValueError('unexpected input path')
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    descriptor = None
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('inventory path is not regular')
        if before.st_size > 64 * 1024 * 1024:
            raise ValueError('unexpected inventory input size')
        blocks = []
        size = 0
        while size <= before.st_size:
            block = os.read(descriptor, min(65536, before.st_size + 1 - size))
            if not block:
                break
            blocks.append(block)
            size += len(block)
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (size != before.st_size or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or (current.st_size, current.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                or not stat.S_ISREG(current.st_mode)):
            raise ValueError('input changed during bounded snapshot')
        raw = b''.join(blocks)
        return raw, {'present': True, 'bytes': size, 'bytes_read': size,
                     'sha256': hashlib.sha256(raw).hexdigest(), 'inode': before.st_ino,
                     'dev': before.st_dev, 'mode': oct(stat.S_IMODE(before.st_mode)),
                     'uid': before.st_uid, 'mtime_ns': before.st_mtime_ns, 'symlink': False}
    except FileNotFoundError:
        if descriptor is not None:
            raise ValueError('input disappeared during bounded snapshot') from None
        return None, {'present': False}
    except OSError as error:
        if error.errno in [errno.ELOOP, errno.ENOTDIR]:
            raise ValueError('unexpected symlink or ancestor type; no target read') from None
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def file_info(path, log=False):
    raw, info = read_snapshot(path)
    if raw is None:
        return info
    if log:
        first = last = None
        lines = raw.splitlines()
        for line in lines:
            match = re.match(rb'^(?:[A-Z][a-z]{2}\s+\d+ \d\d:\d\d:\d\d|'
                             rb'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)', line)
            if match:
                last = match[0].decode('ascii')
                if first is None:
                    first = last
        info.update(first_stamp=first, last_stamp=last, lines=len(lines),
                    newline_terminated=not raw or raw.endswith(b'\n'))
    return info


def main():
    compatible = Path('/proc/device-tree/compatible').read_bytes().decode()
    identity = dict(uid=os.geteuid(), hostname=os.uname().nodename,
                    kernel=os.uname().release, compatible=compatible)
    check_identity(identity['uid'], identity['hostname'], identity['kernel'], compatible)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch(r'[0-9a-f-]{36}', boot):
        raise ValueError('invalid boot identity')
    cron_raw, cron_info = read_snapshot(Path('/etc/crontabs/root'))
    syslog_raw, syslog_info = read_snapshot(Path('/etc/conf.d/syslog'))
    if cron_raw is None or syslog_raw is None:
        raise ValueError('required cron/syslog configuration missing')
    sources = ['/etc/periodic/daily/i14-logtrim', SAMPLER, SAMPLER + '-rs']
    logs = ['/var/log/messages' + suffix for suffix in ['', '.0', '.1', '.2', '.3', '.4']]
    logs.append('/var/log/i14-battery.log')
    files = {name: file_info(Path(name), log=name in logs) for name in sources + logs}
    files.update({'/etc/crontabs/root': cron_info, '/etc/conf.d/syslog': syslog_info})
    processes = []
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            command = (proc / 'comm').read_text().strip()
            if command in ['crond', 'syslogd']:
                processes.append({'pid': int(proc.name), 'comm': command,
                                  'stat': (proc / 'stat').read_text().strip(),
                                  'cmdline_sha256': hashlib.sha256((proc / 'cmdline').read_bytes()).hexdigest()})
        except (FileNotFoundError, ProcessLookupError):
            continue
    storage = os.statvfs(BASE)
    samples = {str(BASE / 'soak' / name): file_info(BASE / 'soak' / name)
               for name in ['samples.jsonl.1', 'samples.jsonl']}
    print(json.dumps({'utc': datetime.now(timezone.utc).isoformat(),
                      'epoch': time.time(), 'monotonic': time.monotonic(),
                      'uptime': float(Path('/proc/uptime').read_text().split()[0]),
                      'identity': identity, 'boot_id': boot, 'files': files,
                      'sample_files': samples, 'cron': cron_summary(cron_raw),
                      'processes': processes, 'userdata_available_bytes': storage.f_bavail * storage.f_frsize,
                      'syslog_expected_options': any(line.strip() == 'SYSLOGD_OPTS="-t -s 2048 -b 4"'
                                                     for line in syslog_raw.decode().splitlines()),
                      'scope': 'Inventory only; no write, environment dump, secret or log-body output'}, indent=2))


if __name__ == '__main__':
    main()
