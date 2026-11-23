"""Stream a closed phase to stdout after validating its owner-supplied bundle."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import types

BASE = Path('/mnt/data/media/0/i14-linux/soak-c-observer-20261115')
CRON = Path('/etc/crontabs/root')
OWNER = 0
DEPS = {
    'i14-soak-observer-export.py': '1382cd4e2e450f7c43c3e4b85e6203e1673ab08195309df7e1b7b7dc1051f660',
    'i14-soak-observer-inventory.py': '4a7973e96c1e3063e45def3e0efb2ef69c919f3384aad19caa9a2d00e6f5891c',
}
SOURCES = {
    'i14-soak-local-collector.py': 'f2aed0de2044aa041a4af4bc9dbd598d45a77e5b052b7362e9848532d495e231',
    'i14-soak-observer-inventory.py': DEPS['i14-soak-observer-inventory.py'],
    'procs.sh': '32bc698ce56a958045bf6b63ff0588a0c1287e9f47007c6789d1314aa80def9d',
}

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def dependencies(raws):
    if set(raws) != set(DEPS):
        raise ValueError('exact approved dependencies required')
    result = {}
    for name, raw in raws.items():
        if len(raw) > 65536 or sha(raw) != DEPS[name]:
            raise ValueError('approved export/inventory dependency differs')
        module = types.ModuleType('pinned_closed_phase_dependency')
        module.__file__ = name
        exec(compile(raw, name, 'exec'), module.__dict__)
        result[name] = module
    return result['i14-soak-observer-export.py'], result['i14-soak-observer-inventory.py']

def fixed(path, limit):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('fixed identity/proc input exceeds bound')
    return raw

def native(expected_boot):
    u = os.uname()
    if (os.geteuid(), u.nodename, u.release, u.machine) != (0, 'i14', '3.18.19', 'aarch64'):
        raise ValueError('native phone identity differs')
    if b'mediatek,MT6735' not in fixed('/proc/device-tree/compatible', 4096).split(b'\0'):
        raise ValueError('native board differs')
    actual = fixed('/proc/sys/kernel/random/boot_id', 64).decode().strip()
    if actual != expected_boot:
        raise ValueError('expected closed-phase boot differs')
    return actual

def actors(root):
    script = 'i14-soak-local-collector.py'
    expected = str(root/script)
    found = []
    with os.scandir('/proc') as entries:
        for i, entry in enumerate(entries):
            if i >= 4096:
                raise ValueError('process enumeration exceeds4096')
            if entry.name.isdigit():
                try:
                    parts = fixed(Path(entry.path)/'cmdline', 65536).split(b'\0')
                    if parts[-1] == b'':
                        parts.pop()  # Only the final separator; empty arguments retain their positions.
                    argv = [os.fsdecode(a) for a in parts]
                    run_dirs = []
                    run_option = False
                    for j,argument in enumerate(argv):
                        option,separator,value = argument.partition('=')
                        # The unchanged collector accepts argparse's unambiguous --r... prefixes.
                        if len(option)>=3 and '--run-dir'.startswith(option):
                            run_option = True
                            if separator:
                                run_dirs.append(value)
                            elif j+1<len(argv):
                                run_dirs.append(argv[j+1])
                    if not run_option and not any(script in a or str(root) in a for a in argv):
                        continue
                    cwd = None
                    def normalized(argument):
                        nonlocal cwd
                        if os.path.isabs(argument):
                            return os.path.normpath(argument)
                        if cwd is None:
                            try:
                                cwd = os.readlink(Path(entry.path)/'cwd')
                            except (FileNotFoundError, ProcessLookupError):
                                raise
                            except OSError as error:
                                raise ValueError('relevant collector cwd cannot be established') from error
                            if not isinstance(cwd,str) or len(cwd)>4096 or not os.path.isabs(cwd) or cwd.endswith(' (deleted)'):
                                raise ValueError('ambiguous relevant collector cwd')
                        return os.path.normpath(os.path.join(cwd,argument))
                    candidates = [a for a in argv if os.path.basename(a)==script]
                    if any(normalized(a)==expected for a in candidates):
                        found.append(int(entry.name))
                        continue
                    if (not candidates and not run_dirs) or any(normalized(a)==str(root) for a in run_dirs):
                        raise ValueError('unresolved relevant collector invocation')
                except (FileNotFoundError, ProcessLookupError):
                    pass
    return found

def stream_phase(bundle, dependency_sources, stream, clock):
    """Require caller/grant proof, stream bounded tar, and return ONLY after final checks.

    The future host caller must require this function's normal remote exit as well
    as B3a tar verification; an earlier tar completion member alone is insufficient.
    """
    keys = {'phase_id', 'grant_id', 'close_grant_id', 'run_dir', 'boot_id',
            'config_sha256', 'source_sha256', 'cronline_sha256', 'original_cron_sha256',
            'close_receipt_sha256', 'start_epoch', 'deadline_epoch'}
    if set(bundle) != keys or bundle['source_sha256'] != SOURCES:
        raise ValueError('unexpected closed-phase binding/source set')
    for name in ['phase_id', 'grant_id', 'close_grant_id']:
        if not isinstance(bundle[name], str) or not re.fullmatch('[A-Za-z0-9_.-]{1,120}', bundle[name]):
            raise ValueError('invalid phase/grant identifier')
    for name in ['config_sha256', 'cronline_sha256', 'original_cron_sha256', 'close_receipt_sha256']:
        if not re.fullmatch('[0-9a-f]{64}', str(bundle[name])):
            raise ValueError('missing exact provenance pin')
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(bundle['boot_id'])):
        raise ValueError('invalid expected boot')
    start, end = bundle['start_epoch'], bundle['deadline_epoch']
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in [start, end]) or not 0 < end-start <= 900:
        raise ValueError('export grant exceeds0..900s')
    def time_guard():
        now = clock()
        if not math.isfinite(now) or not start <= now < end:
            raise ValueError('export grant not current; no retry')
    time_guard()
    root = Path(bundle['run_dir'])
    if root.parent != BASE or not re.fullmatch('phase-[0-9]{8}T[0-9]{6}Z', root.name):
        raise ValueError('not the exact owned phase destination')
    export, inventory = dependencies(dependency_sources)
    binding = {k:bundle[k] for k in ['phase_id', 'grant_id', 'boot_id', 'config_sha256']}
    export.binding_bytes(binding)
    def checked(path, digest, mode):
        raw, meta = inventory.read_snapshot(path)
        if raw is None or len(raw) > 65536 or sha(raw) != digest or meta['uid'] != OWNER or int(meta['mode'],8) != mode:
            raise ValueError('closed-phase pinned file/metadata differs: '+path.name)
        return raw
    def invariants():
        time_guard(); native(bundle['boot_id'])
        checked(CRON, bundle['original_cron_sha256'], 0o600)
        checked(root/'root-crontab.before', bundle['original_cron_sha256'], 0o600)
        config = json.loads(checked(root/'observer.json', bundle['config_sha256'], 0o600))
        if (set(config) != {'schema','boot_id','grant_id','expires_epoch','max_captures','max_bytes','capture_bytes','reserve_bytes','source_sha256'}
                or config['schema'] != 2 or config['boot_id'] != bundle['boot_id']
                or config['grant_id'] != bundle['phase_id'] or config['source_sha256'] != SOURCES
                or type(config['max_captures']) is not int or not 1 <= config['max_captures'] <= 80
                or type(config['max_bytes']) is not int or not 0 < config['max_bytes'] <= 1281 << 20
                or config['capture_bytes'] != 16 << 20 or config['reserve_bytes'] != 512 << 20):
            raise ValueError('closed-phase config outside reviewed export scope')
        for name, digest in SOURCES.items():
            checked(root/name, digest, 0o700)
        report = json.loads(checked(root/'close-receipt.json', bundle['close_receipt_sha256'], 0o600))
        expected = dict(result='CRON_RESTORED_NO_ACTOR',grant_id=bundle['close_grant_id'],run_dir=str(root),
                        boot_id=bundle['boot_id'],source_sha256=SOURCES,config_sha256=bundle['config_sha256'],
                        cronline_sha256=bundle['cronline_sha256'],restored_cron_sha256=bundle['original_cron_sha256'])
        if any(report.get(k) != v for k,v in expected.items()):
            raise ValueError('close receipt belongs to a different phase/grant/provenance')
        try:
            os.stat('.capture.lock', dir_fd=fd, follow_symlinks=False)
            locked = True
        except FileNotFoundError:
            locked = False
        if actors(root) or locked:
            raise ValueError('collector actor/lock still present')
    with export.directory(root) as fd:
        before = os.fstat(fd)
        if before.st_uid != OWNER or stat.S_IMODE(before.st_mode) != 0o700:
            raise ValueError('phase directory is not private and owned')
        invariants()
        manifest = export.inventory(fd)
        if any(v['uid'] != OWNER for v in manifest['files'].values()):
            raise ValueError('foreign owner in phase evidence')
        required = {name:(digest,0o700) for name,digest in SOURCES.items()}
        required.update({'observer.json':(bundle['config_sha256'],0o600),
                         'root-crontab.before':(bundle['original_cron_sha256'],0o600),
                         'close-receipt.json':(bundle['close_receipt_sha256'],0o600)})
        for name,(digest,mode) in required.items():
            row = manifest['files'].get(name,{})
            if (row.get('sha256'),row.get('uid'),row.get('mode'),row.get('nlink')) != (digest,OWNER,mode,1):
                raise ValueError('export manifest differs from validated provenance: '+name)
        time_guard()
        class DeadlineSink:
            written = 0
            def write(self, raw):
                time_guard()
                self.written += len(raw)
                if self.written > export.MAX_BYTES + 2*export.MAX_META + (export.MAX_FILES+4)*1024 + 10240:
                    raise ValueError('stream envelope exceeds bounded tar overhead')
                count = stream.write(raw)
                if count != len(raw):
                    raise ValueError('short export stream write')
                return count
        sink = DeadlineSink()
        export.export_tar(fd, manifest, binding, sink)
        invariants()
        with export.directory(root) as current:
            after = os.fstat(current)
            if (before.st_dev,before.st_ino) != (after.st_dev,after.st_ino):
                raise ValueError('phase namespace changed')
        time_guard()
        return dict(result='CLOSED_PHASE_STREAMED_NOT_CLEANUP_AUTHORIZED',binding=binding,
                    files=len(manifest['files']),bytes=manifest['total_bytes'],tar_bytes=sink.written,
                    cleanup_authorized=False)


if __name__ == '__main__':
    if sys.argv[1:] != ['--stream']:
        raise SystemExit('usage: i14-soak-phase-export.py --stream < closed-phase.json')
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise SystemExit('phase bundle too large')
    sources = {name: fixed(Path(__file__).with_name(name), 65536) for name in DEPS}
    stream_phase(json.loads(raw), sources, sys.stdout.buffer, time.time)
    sys.stdout.buffer.flush()
