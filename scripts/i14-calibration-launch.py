#!/usr/bin/env python3
"""Build C13 offline; run only a separately reviewed, fresh gate on the leased I14."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

CAMPAIGN = 'C13-stock-full-20261113'
COORDINATOR = os.environ.get('I14_COORDINATOR_ID', 'i14-coordinator')
BASE = Path('/mnt/data/media/0/i14-linux')
CODE = BASE / ('battery-calibration-code-' + CAMPAIGN)
CONTROL = BASE / ('battery-calibration-' + CAMPAIGN + '-control')
MEASURE_SHA = 'be92a25370ab8f0f97f89ee36a303a60822bd97bfca9e2b26e25c98508b455a6'
RUN_SECONDS, RECOVERY_SECONDS, CAMPAIGN_SECONDS = 32*3600, 4*3600, 72*3600
MIB = 1024**2
MAX_RECORD_BYTES, MAX_INPUT_BYTES, MAX_ACKS = 8*MIB, 256*1024, 80
# Both traces (960), atomic state/result/failure pairs (96), code/control/logs (128).
C13_BYTES = 1184*MIB
C13_GUARD_LOG_BYTES = 2*3842*512
OBSERVER_BYTES, COMMISSION_BYTES, ALLOCATION_BYTES, RESERVE_BYTES = 1280*MIB, 32*MIB, 32*MIB, 512*MIB
SERVICES = ('i14-power', 'i14-ui', 'i14-netwatch', 'i14-cpufreq', 'i14-splash', 'unbound', 'sshd')
RUNTIME_PATHS = tuple('/usr/libexec/i14-rs/' + name for name in (
    'i14-ui', 'i14-ui.prev', 'i14-netwatch', 'i14-battery-guard', 'i14-battery-guard.prev',
    'i14-cpufreq', 'i14-splash', 'i14-reboot-bootloader', 'SHA256SUMS')) + tuple(
    '/usr/lib/i14ui/' + name for name in ('charging.py', 'bcct.py', 'config.py')) + tuple(
    '/etc/conf.d/' + name for name in ('i14-power', 'i14-ui', 'i14-netwatch', 'i14-cpufreq', 'i14-splash')) + (
    str(BASE / 'ui.json'),)
WRAPPER = '''#!/bin/sh
set -eu
umask 077
cd -- "$(dirname -- "$0")"
case "${1:-}" in gate-1.json|gate-2.json) ;; *) exit 2 ;; esac
exec python3 -B ./launcher.py --run "$1"
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def hash_value(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def read_json(path, *, limit=MAX_RECORD_BYTES):
    require(path.is_file() and not path.is_symlink(), 'missing or symlinked record: ' + str(path))
    require(path.stat().st_size<=limit, 'input byte cap exceeded: '+str(path))
    return json.loads(path.read_text())


def write_json(path, value, *, new=False):
    data = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n'
    require(len(data)<=MAX_RECORD_BYTES,'record byte cap exceeded')
    if new:
        with path.open('x') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        temp = path.with_suffix('.tmp')
        with temp.open('w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)


def build_bundle(directory):
    source = Path(__file__).resolve().parents[1]
    measure = source / 'scripts/i14-battery-calibrate.py'
    require(digest(measure) == MEASURE_SHA, 'measurement differs from frozen package pin')
    directory.mkdir(mode=0o700)
    (directory/'measure.py').write_bytes(measure.read_bytes())
    (directory/'launcher.py').write_bytes(Path(__file__).read_bytes())
    (directory/'wrapper.sh').write_text(WRAPPER)
    (directory/'wrapper.sh').chmod(0o700)
    write_json(directory/'package.json', dict(schema=1, campaign=CAMPAIGN,
        files={name:digest(directory/name) for name in ('measure.py','launcher.py','wrapper.sh')},
        module_pins={'/usr/lib/i14ui/'+name: digest(source/'rootfs/overlay/usr/lib/i14ui'/name)
                     for name in ('charging.py','bcct.py','config.py')},
        policy=dict(attempts=2, run_seconds=RUN_SECONDS, recovery_seconds=RECOVERY_SECONDS,
                    campaign_seconds=CAMPAIGN_SECONDS, kill_grace_seconds=30)), new=True)


def verify_bundle(directory):
    package = read_json(directory/'package.json')
    require(package['schema']==1 and package['campaign']==CAMPAIGN, 'wrong package')
    require(set(package['files'])=={'measure.py','launcher.py','wrapper.sh'}, 'package file set')
    for name, pin in package['files'].items():
        path=directory/name
        require(not path.is_symlink() and digest(path)==pin, 'package hash mismatch: '+name)
    require(package['files']['measure.py']==MEASURE_SHA, 'wrong measurement hash')
    require(sum((directory/name).stat().st_size for name in (*package['files'],'package.json'))<=16*MIB,
            'bundle byte cap exceeded')
    inputs=list(directory.glob('ack-*.json'))+list(directory.glob('gate-*.json'))
    require(len(list(directory.glob('ack-*.json')))<=MAX_ACKS and
            len(list(directory.glob('gate-*.json')))<=2,'input file count cap exceeded')
    require(all(not p.is_symlink() and p.stat().st_size<=MAX_INPUT_BYTES for p in inputs),
            'input byte cap exceeded')
    return package


def storage_required(gate):
    storage=gate['storage']
    require(all(hash_value(storage[key]) for key in ('quota_review_sha256','retention_review_sha256')),
            'missing reviewed quota/retention proof')
    extra=[storage[key] for key in ('observer_fixed_bytes','background_growth_bytes')]
    require(all(type(value) is int and value>=0 for value in extra), 'invalid storage ceiling')
    require(storage['background_growth_bytes']>=C13_GUARD_LOG_BYTES, 'insufficient C13 guard log allowance')
    return max(2*1024**3,C13_BYTES+OBSERVER_BYTES+COMMISSION_BYTES+ALLOCATION_BYTES+RESERVE_BYTES+sum(extra))


def validate_gate(gate, now):
    try:
        storage_required(gate)
        require(gate['schema']==1 and gate['campaign']==CAMPAIGN and
                type(gate['attempt']) is int and gate['attempt'] in (1,2), 'wrong campaign/attempt')
        require(str(uuid.UUID(gate['boot_id']))==gate['boot_id'], 'invalid boot pin')
        grant, facts, watch = gate['grant'], gate['facts'], gate['supervision']
        deadline=gate['not_after_epoch']
        for epoch in (grant['starts_epoch'],grant['ends_epoch'],facts['captured_epoch'],
                      watch['probe_epoch'],watch['coverage_until_epoch'],watch['next_check_epoch']):
            require(type(epoch) is int, 'invalid timestamp type')
        require(type(deadline) is int and deadline-now >= RUN_SECONDS+RECOVERY_SECONDS+30,
                'insufficient campaign time')
        require(type(grant['owner']) is int and grant['owner']==4 and
                grant['coordinator_thread_id']==COORDINATOR and isinstance(grant['id'],str) and
                bool(grant['id']) and grant['starts_epoch']<=now and grant['ends_epoch']>=deadline,
                'missing current item2 grant')
        for pin in (grant['receipt_sha256'],grant['package_sha256'],gate['launch_review_sha256'],
                    facts['K_sha256'],facts['usb_sha256'],facts['serial_recovery_sha256'],watch['receipt_sha256']):
            require(hash_value(pin), 'missing proof hash')
        require(facts['K_pass'] is True and facts['boot_id']==gate['boot_id'] and
                0<=now-facts['captured_epoch']<=150, 'stale or failed preflight K')
        require(set(facts['runtime_hashes'])==set(RUNTIME_PATHS) and
                all(hash_value(pin) for pin in facts['runtime_hashes'].values()), 'runtime pin set')
        a,b=facts['guard_readbacks']
        require(type(a['epoch']) is int and type(b['epoch']) is int and
                b['epoch']-a['epoch']>=600 and 0<=now-b['epoch']<=150 and
                all(r['valid'] is True and r['boot_id']==gate['boot_id'] for r in (a,b)),
                'two same-boot guard readings ten minutes apart required')
        require(0<=now-watch['probe_epoch']<=900 and watch['coverage_until_epoch']>=deadline and
                now<watch['next_check_epoch']<=now+3600 and isinstance(watch['continuation_id'],str) and
                bool(watch['continuation_id']), 'unproven collection/continuation coverage')
        previous=gate['previous_acceptance']
        if gate['attempt']==1:
            require(previous is None, 'first attempt cannot reuse old acceptance')
        else:
            require(previous['verdict']=='ACCEPTED_CYCLE_AND_RESTORATION' and
                    hash_value(previous['result_sha256']) and hash_value(previous['review_sha256']),
                    'missing independent first-cycle/restoration acceptance')
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('invalid C13 gate: '+str(exc)) from exc


def reserve_attempt(path, gate, now, package_hash):
    """Caller holds the campaign flock. Reserve before starting anything; never refund."""
    require(gate['grant']['package_sha256']==package_hash, 'grant package pin differs')
    attempt=gate['attempt']; mono=time.monotonic()
    if attempt==1:
        require(not path.exists(), 'campaign already exists; no cap reset')
        deadline=min(now+CAMPAIGN_SECONDS,gate['not_after_epoch'])
        record=dict(campaign=CAMPAIGN, started_epoch=now,started_monotonic=mono,
                    deadline_epoch=deadline,deadline_monotonic=mono+deadline-now,
                    attempts=0, state='new', package_sha256=package_hash, boot_id=gate['boot_id'],
                    frozen_runtime=gate['facts']['runtime_hashes'])
    else:
        record=read_json(path)
        require(record['state']!='stopped', 'campaign stopped after failure')
        require(record['state']=='awaiting_review' and record['attempts']==1 and
                record['result_sha256']==(gate['previous_acceptance'] or {}).get('result_sha256'),
                'first cycle has no matching independent acceptance')
        require(0<=now-record['ended_epoch']<=RECOVERY_SECONDS,'four-hour restoration/start window expired')
        require(0<=mono-record['ended_monotonic']<=RECOVERY_SECONDS,
                'four-hour monotonic restoration/start window expired')
        require(record['package_sha256']==package_hash and
                record['frozen_runtime']==gate['facts']['runtime_hashes'] and
                record['boot_id']==gate['boot_id'], 'frozen campaign inputs changed')
    require(now>=record.get('last_epoch',record['started_epoch']), 'clock rollback')
    require(clock_valid(record,now,mono),'clock rollback/monotonic discontinuity')
    require(gate['not_after_epoch']==record['deadline_epoch'] and
            record['deadline_epoch']-now>=RUN_SECONDS+RECOVERY_SECONDS+30, 'insufficient fixed campaign budget')
    require(record['deadline_monotonic']-mono>=RUN_SECONDS+RECOVERY_SECONDS+30,
            'insufficient monotonic campaign budget')
    record.update(attempts=attempt,state='running',last_epoch=now,last_monotonic=mono,
                  attempt_started_monotonic=mono,grant=gate['grant']['id'])
    write_json(path,record,new=attempt==1)
    return record


def finish_attempt(path, attempt, rc, result, restore_rc, ended_epoch, *, clock_floor=None, experiment_gone=True):
    record=read_json(path)
    require(record['state']=='running' and record['attempts']==attempt, 'wrong active attempt')
    mono=time.monotonic(); checkpoint=None; clock_ok=False
    if clock_floor:
        record['last_epoch']=max(record['last_epoch'],clock_floor[0])
        record['last_monotonic']=max(record['last_monotonic'],clock_floor[1])
    try:
        checkpoint=read_json(path.parent/'checkpoint.json') if (path.parent/'checkpoint.json').exists() else None
        clock_ok=type(ended_epoch) is int and clock_valid(record,ended_epoch,mono,checkpoint)
    except (OSError,ValueError,KeyError,TypeError):
        pass
    success=rc==0 and restore_rc==0 and clock_ok and experiment_gone and result is not None and result.is_file()
    if not clock_ok: rc=1
    if checkpoint:
        if type(checkpoint.get('acknowledged_epoch')) is int:
            record['last_epoch']=max(record['last_epoch'],checkpoint['acknowledged_epoch'])
        if type(checkpoint.get('acknowledged_monotonic')) in (int,float):
            record['last_monotonic']=max(record['last_monotonic'],checkpoint['acknowledged_monotonic'])
    record.update(state='awaiting_review' if success else ('stopped' if experiment_gone else 'stopping'), exit_code=rc,
                  production_start_rc=restore_rc, result_sha256=digest(result) if success else None,
                  ended_epoch=ended_epoch,ended_monotonic=mono,clock_error=not clock_ok,
                  last_epoch=max(record['last_epoch'],ended_epoch),last_monotonic=max(record['last_monotonic'],mono))
    write_json(path,record)
    return record


def acknowledge(control, receipt, now):
    """A fresh real scheduler receipt renews monitoring, never the attempt/campaign budget."""
    record=read_json(control/'campaign.json'); previous=read_json(control/'checkpoint.json')
    count=previous.get('ack_count',0)
    require(type(count) is int and 0<=count<MAX_ACKS, 'receipt count cap exceeded')
    mono=time.monotonic()
    require(clock_valid(record,now,mono,previous),'clock rollback at checkpoint ack')
    require(record['state']=='running' and receipt['campaign']==CAMPAIGN and
            receipt['attempt']==record['attempts'] and receipt['boot_id']==record['boot_id'] and
            receipt['grant_id']==record['grant'], 'ack does not match current owner/run')
    captured,following=receipt['captured_epoch'],receipt['next_check_epoch']
    require(type(captured) is int and type(following) is int and 0<=now-captured<=150 and
            now<=previous['next_check_epoch']+300 and now<following<=now+3600 and
            mono<=previous['deadline_monotonic']+300 and
            mono+following-now<=record['deadline_monotonic']-RECOVERY_SECONDS-30 and
            following<=record['deadline_epoch']-RECOVERY_SECONDS-30 and
            hash_value(receipt['scheduler_receipt_sha256']), 'late or unbounded checkpoint ack')
    write_json(control/'checkpoint.json',dict(receipt,next_check_epoch=following,acknowledged_epoch=now,
               acknowledged_monotonic=mono,deadline_monotonic=mono+following-now,ack_count=count+1))


def clock_valid(record, wall, mono, checkpoint=None):
    """Wall timestamps cannot add elapsed time; tolerate only integer-clock quantization."""
    floor_wall=record.get('last_epoch',record['started_epoch'])
    floor_mono=record.get('last_monotonic',record['started_monotonic'])
    if checkpoint:
        floor_wall=max(floor_wall,checkpoint['acknowledged_epoch'])
        floor_mono=max(floor_mono,checkpoint['acknowledged_monotonic'])
    return (wall>=floor_wall and mono>=floor_mono and
            wall-record['started_epoch']>=mono-record['started_monotonic']-2)


def supervision_valid(record, checkpoint, wall, mono, seen_wall, seen_mono):
    return (clock_valid(record,wall,mono,checkpoint) and wall>=seen_wall and mono>=seen_mono and
            mono<min(record['attempt_started_monotonic']+RUN_SECONDS,
                     record['deadline_monotonic']-RECOVERY_SECONDS-30) and
            wall<record['deadline_epoch']-RECOVERY_SECONDS-30 and
            wall<=checkpoint['next_check_epoch']+300 and mono<=checkpoint['deadline_monotonic']+300)


def service(action, name='i14-power'):
    try:
        return subprocess.run(['rc-service',name,action],capture_output=True,text=True,timeout=30).returncode
    except (OSError,subprocess.TimeoutExpired):
        return -1


def rust_selector(text):
    return re.findall(r'^\s*i14_impl\s*=([^\n]*)$',text,re.MULTILINE)==['"rust"']


def live_preflight(gate, package, directory):
    require(os.geteuid()==0 and os.uname().nodename=='i14' and os.uname().release=='3.18.19' and
            os.uname().machine=='aarch64', 'wrong native device')
    require(Path('/sys/firmware/devicetree/base/compatible').read_bytes()==b'mediatek,MT6735\0', 'wrong board')
    require(Path('/proc/sys/kernel/random/boot_id').read_text().strip()==gate['boot_id'], 'boot changed')
    require(not any(word in Path('/proc/cmdline').read_text() for word in ('i14.rescue','androidboot.mode=charger')),
            'non-normal boot')
    require(not Path('/run/i14-drain-request').exists(), 'drain request exists')
    require(not Path('/run/i14-drain-token').exists(), 'drain owner token exists')
    for name in ('i14-battery-calibration.lock','i14-drain.lock'):
        lock_path=Path('/run')/name
        if lock_path.exists():
            with lock_path.open('r') as lock:
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError as exc: raise ValueError('another controller owns '+name) from exc
    for proc in Path('/proc').iterdir():
        if proc.name.isdecimal() and proc.name!=str(os.getpid()):
            try: args=(proc/'cmdline').read_bytes()
            except FileNotFoundError: continue
            require(not any(b'/battery-calibration-code-' in arg and arg.endswith(b'/measure.py')
                            for arg in args.split(b'\0')) and b'i14-drain-load' not in args,
                    'another experiment process exists')
    for path,pin in gate['facts']['runtime_hashes'].items():
        require(digest(Path(path))==pin, 'runtime hash mismatch: '+path)
        if path.startswith('/etc/conf.d/'):
            require(rust_selector(Path(path).read_text()),'Rust selector not pinned: '+path)
    for path,pin in package['module_pins'].items():
        require(gate['facts']['runtime_hashes'][path]==pin, 'Python module differs from package: '+path)
    guard=read_json(Path('/run/i14-charge-status.json'))
    require(guard['boot_id']==gate['boot_id'] and 0<=time.monotonic()-guard['updated_monotonic']<=150 and
            guard['valid'] is True and guard['error'] is None and guard.get('drain') is False and
            guard.get('online') is True,
            'production guard not ready')
    require(all(service('status',name)==0 for name in SERVICES), 'service not started')
    require(os.statvfs(BASE).f_bavail*os.statvfs(BASE).f_frsize>=storage_required(gate),
            'insufficient combined C13/observer/closure storage (2GiB floor retained)')
    spec=importlib.util.spec_from_file_location('measure',directory/'measure.py')
    measure=importlib.util.module_from_spec(spec); spec.loader.exec_module(measure)
    row=measure.sample()  # Cached gauge getters and dmesg only; no active current acquisition.
    measure.sample_json(row)
    require(row['boot_id']==gate['boot_id'] and row['online'] is True and row['capacity']>=10 and
            row['voltage_mv']>=3700 and 0<=row['temp_deci_c']<=400, 'unsafe initial battery')
    return row


def measurement_pid(data, directory):
    try:
        pid=int((data/'python.pid').read_text())
        args=(Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
        return pid if str(directory/'measure.py').encode() in args and str(data).encode() in args else None
    except (OSError,ValueError):
        return None


def run_gate(path):
    directory=Path(__file__).resolve().parent
    require(directory==CODE and path.resolve().parent==CODE, 'use fixed reviewed code directory')
    package=verify_bundle(directory); gate=read_json(path,limit=MAX_INPUT_BYTES)
    require(path.name==f"gate-{gate['attempt']}.json", 'wrong gate filename')
    validate_gate(gate,int(time.time()))
    package_hash=digest(directory/'package.json')
    require(gate['grant']['package_sha256']==package_hash, 'package not granted')
    initial=live_preflight(gate,package,directory)
    require(not CONTROL.is_symlink(), 'symlinked campaign control directory')
    CONTROL.mkdir(mode=0o700,exist_ok=True)
    with (CONTROL/'campaign.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        now=int(time.time()); validate_gate(gate,now)
        record=reserve_attempt(CONTROL/'campaign.json',gate,now,package_hash)
        attempt=gate['attempt']; data=BASE/f'battery-calibration-{CAMPAIGN}-attempt-{attempt}'
        receipt=CONTROL/f'launch-{attempt}.json'
        rc,restore,child=1,None,None
        seen_wall,seen_mono=now,record['attempt_started_monotonic']
        supervisor_error=None; stop_since=None
        stop_requested=[False]
        def stop(*_): stop_requested[0]=True
        old_handlers={s:signal.signal(s,stop) for s in (signal.SIGTERM,signal.SIGINT)}
        try:
            require(not data.exists(), 'attempt data already exists')
            write_json(receipt,dict(campaign=record,gate=gate,gate_sha256=digest(path),initial=initial,
                                   wrapper_pid=os.getpid(),started_epoch=now,timeout_seconds=RUN_SECONDS),new=True)
            old_checkpoint=CONTROL/'checkpoint.json'
            count=read_json(old_checkpoint).get('ack_count',0) if old_checkpoint.exists() else 0
            require(type(count) is int and 0<=count<MAX_ACKS,'receipt count cap exceeded')
            write_json(CONTROL/'checkpoint.json',dict(ack_count=count,next_check_epoch=gate['supervision']['next_check_epoch'],
                       acknowledged_epoch=now,acknowledged_monotonic=seen_mono,
                       deadline_monotonic=seen_mono+gate['supervision']['next_check_epoch']-now,
                       scheduler_receipt_sha256=gate['supervision']['receipt_sha256']))
            with (CONTROL/f'wrapper-{attempt}.log').open('x') as log:
                child=subprocess.Popen(['timeout','-s','TERM','-k','30',str(RUN_SECONDS),
                    'python3','-B',str(directory/'measure.py'),'--run',str(data),
                    '--expected-boot',gate['boot_id']],stdout=log,stderr=subprocess.STDOUT)
                while True:
                    exited=False
                    try:
                        try:
                            rc=child.wait(timeout=30)
                            exited=True
                        except subprocess.TimeoutExpired:
                            pass
                        if os.statvfs(BASE).f_bavail*os.statvfs(BASE).f_frsize<=512*1024**2:
                            stop_requested[0]=True
                        wall,mono=int(time.time()),time.monotonic()
                        if not supervision_valid(record,read_json(CONTROL/'checkpoint.json'),wall,mono,
                                                 seen_wall,seen_mono): stop_requested[0]=True
                        seen_wall=max(seen_wall,wall); seen_mono=max(seen_mono,mono)
                    except Exception as exc:
                        supervisor_error=supervisor_error or exc
                        stop_requested[0]=True
                    if exited and measurement_pid(data,directory) is not None:
                        stop_requested[0]=True  # Reap an identified orphan before any restoration.
                    if stop_requested[0]:
                        mono=time.monotonic()
                        if stop_since is None: stop_since=mono
                        pid=measurement_pid(data,directory)
                        if pid is not None:
                            hard_kill=mono>=record['attempt_started_monotonic']+RUN_SECONDS+30
                            try: os.kill(pid,signal.SIGKILL if mono-stop_since>=30 or hard_kill else signal.SIGTERM)
                            except ProcessLookupError: pass  # Verified child exited between read and signal.
                            except OSError as exc: supervisor_error=supervisor_error or exc
                    if exited and measurement_pid(data,directory) is None: break
                    # Keep the owner alive after a supervision error until reaped or the hard run+grace bound.
                    if time.monotonic()>=record['attempt_started_monotonic']+RUN_SECONDS+35:
                        stop_requested[0]=True
                        break
                    if supervisor_error is not None or exited: time.sleep(0.25)
        finally:
            # A live or uncertain experiment keeps ownership. Never start a second BCCT writer.
            gone=child is None or child.poll() is not None
            pid=measurement_pid(data,directory)
            if gone and pid is None:
                restore=service('start')
            else:
                rc=1
            if stop_requested[0]: rc=1
            finished=finish_attempt(CONTROL/'campaign.json',attempt,rc,data/'result.json',restore,int(time.time()),
                                    clock_floor=(seen_wall,seen_mono),experiment_gone=gone and pid is None)
            rc=finished['exit_code']
            write_json(CONTROL/f'wrapper-result-{attempt}.json',dict(exit_code=rc,
                       production_start_rc=restore,experiment_gone=gone and pid is None,
                       supervisor_error=repr(supervisor_error) if supervisor_error else None,
                       restoration_accepted=False,finished_epoch=int(time.time())),new=True)
            for sig,handler in old_handlers.items(): signal.signal(sig,handler)
        if supervisor_error is not None: raise supervisor_error
        return rc


def checkpoint(stop=False):
    """Own-run status/stop only. Never stop the wrapper or start production here."""
    directory=Path(__file__).resolve().parent
    require(directory==CODE, 'use the fixed reviewed bundle')
    verify_bundle(directory)
    record=read_json(CONTROL/'campaign.json')
    attempt=record['attempts']
    data=BASE/f'battery-calibration-{CAMPAIGN}-attempt-{attempt}'
    pid=measurement_pid(data,directory)
    if stop:
        require(record['state'] in ('running','stopping') and pid is not None,'no verified own measurement PID')
        os.kill(pid,signal.SIGTERM)
    def optional(path): return read_json(path) if path.is_file() else None
    print(json.dumps(dict(campaign=record,measurement_pid=pid,stop_signal_sent=stop,
        state=optional(data/'state.json'),failure=optional(data/'failure.json'),
        result=optional(data/'result.json'),wrapper=optional(CONTROL/f'wrapper-result-{attempt}.json'),
        guard=optional(Path('/run/i14-charge-status.json')),epoch=int(time.time()),
        free_bytes=os.statvfs(BASE).f_bavail*os.statvfs(BASE).f_frsize),allow_nan=False))


def ack_file(path):
    require(Path(__file__).resolve().parent==CODE and path.resolve().parent==CODE,
            'use own immutable ack file in reviewed bundle directory')
    verify_bundle(CODE)
    receipt=read_json(path,limit=MAX_INPUT_BYTES)
    require(path.name==f"ack-{receipt['attempt']}-{receipt['captured_epoch']}.json",'ack filename mismatch')
    require(Path('/proc/sys/kernel/random/boot_id').read_text().strip()==receipt['boot_id'],'boot changed')
    with (CONTROL/'checkpoint.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        acknowledge(CONTROL,receipt,int(time.time()))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--build',type=Path); group.add_argument('--verify',type=Path)
    group.add_argument('--run',type=Path)
    group.add_argument('--checkpoint',action='store_true'); group.add_argument('--stop',action='store_true')
    group.add_argument('--ack-checkpoint',type=Path)
    args=parser.parse_args(); os.umask(0o077)
    if args.build: build_bundle(args.build)
    elif args.verify: verify_bundle(args.verify)
    elif args.checkpoint or args.stop: checkpoint(args.stop)
    elif args.ack_checkpoint: ack_file(args.ack_checkpoint)
    else: raise SystemExit(run_gate(args.run))


if __name__=='__main__': main()
