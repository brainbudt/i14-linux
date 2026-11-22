#!/usr/bin/env python3
"""Fixed r2 entrypoint. Requires a reviewed package and inherited r1 singleton lock."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
STAGED = 'artifacts/window-supervisor-r2-prep-20261101/i14-window-supervisor.py'
STAGED_SHA256 = '21299b244b5ee73dbc7aab7797fe22be6a6970fadb68264ce1acbdd932630ecf'
LOG_WORKER_SHA256 = '1f488633f0401640577368393f278d8791aa1ac380636a2d75441a70f4d8900b'
PACKAGE = 'artifacts/i14-autonomous-windows-20261102-r2/manifest.json'
STATE = 'artifacts/i14-autonomous-state-r2'


class Refused(ValueError):
    pass


def load_base(root=ROOT):
    path = Path(root) / STAGED
    code = path.read_bytes()
    if hashlib.sha256(code).hexdigest() != STAGED_SHA256:
        raise Refused('frozen staged r2 source changed')
    base = types.ModuleType('pinned_r2_supervisor')
    base.__file__ = str(path)
    exec(compile(code, str(path), 'exec'), base.__dict__)
    base.ROOT = Path(root)
    return base


def load_handoff(root, package):
    path = root / 'scripts/i14-r2-host-handoff.py'
    code = path.read_bytes()
    if hashlib.sha256(code).hexdigest() != package['source_sha256'].get('scripts/i14-r2-host-handoff.py'):
        raise Refused('handoff source pin mismatch')
    module = types.ModuleType('pinned_r2_handoff')
    module.__file__ = str(path)
    exec(compile(code, str(path), 'exec'), module.__dict__)
    return module


def run(root, legacy_fd, base, package, *, host=None):
    handoff = load_handoff(root, package)
    host = handoff.RealHost() if host is None else host
    lineage = handoff.verify_lineage(root, legacy_fd, host)
    lineage_hash = hashlib.sha256((root / STATE / 'lineage.json').read_bytes()).hexdigest()

    class BoundSupervisor(base.Supervisor):
        def check_pins(self):
            super().check_pins()
            handoff.verify_lineage(root, legacy_fd, host, check_process=False)

    supervisor = None
    try:
        supervisor = BoundSupervisor(root / STATE, root / PACKAGE)
        supervisor.state['old_epoch_lineage_sha256'] = lineage_hash
        supervisor.save()
        base.atomic_write(root / STATE / 'ready.json', base.encoded({
            'status': 'READY_BEFORE_FIRST_SCAN', 'pid': os.getpid(),
            'lineage_sha256': lineage_hash, 'package_sha256': supervisor.package_sha,
            'old_epoch': lineage['old_epoch'], 'charging_status': 'UNKNOWN'}))
        signal.signal(signal.SIGTERM, supervisor.request_stop)
        signal.signal(signal.SIGINT, supervisor.request_stop)
        while supervisor.tick():
            time.sleep(base.POLL_SECONDS)
        return 0
    finally:
        if supervisor is not None:
            supervisor.close()
        os.close(legacy_fd)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) != 2 or argv[0] != '--legacy-lock-fd' or not argv[1].isdigit() or int(argv[1]) < 3:
            raise Refused('requires --legacy-lock-fd from the one-shot host handoff')
        base = load_base(ROOT)
        package, _ = base.package_at(ROOT / PACKAGE)
        # Every source pin is checked before the handoff module's top-level code.
        pins = {**package.get('source_sha256', {}),
                'scripts/wait-native-cached-once.py': package['worker_sha256'],
                'scripts/i14-window-log-capture.py': LOG_WORKER_SHA256}
        for name, pin in pins.items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != pin:
                raise Refused('source changed: ' + name)
        return run(ROOT, int(argv[1]), base, package)
    except (Refused, OSError, ValueError, KeyError) as error:
        print('REFUSED: ' + str(error), file=sys.stderr)
        return 5
    except Exception as error:
        # The pinned staged IntegrityError is defined in its isolated module.
        print('REFUSED: ' + type(error).__name__ + ': ' + str(error), file=sys.stderr)
        return 5


if __name__ == '__main__':
    sys.exit(main())
