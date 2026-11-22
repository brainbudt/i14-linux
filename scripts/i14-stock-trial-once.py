#!/usr/bin/env python3
"""Fixed, globally once-only stock RAM trial. Shipped release is unqualified.

External, source-reviewed PMIC/power/custody decisions are prerequisites, not
conclusions manufactured by this worker. Cached stock reports never prove charge.
"""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import select
import socket
import stat
import subprocess
import sys
import termios
import threading
import time
import types

ROOT = Path(__file__).resolve().parents[1]
READY = 'artifacts/stock-ram-ready-20261102'
PACKAGE = 'artifacts/i14-stock-trial-deployment-20261102/manifest.json'
QUAL_DIR = 'artifacts/i14-stock-dispatch-qualification-20261102'
QUALIFICATION = QUAL_DIR + '/qualification.json'
ONCE = 'artifacts/i14-stock-trial-once'
RUNTIME = 'artifacts/i14-autonomous-runtime.json'
OLD_STATE = 'artifacts/i14-autonomous-state-r2/state.json'
OLD_JOURNAL = 'artifacts/i14-autonomous-state-r2/events.jsonl'
OLD_PID = 'artifacts/i14-autonomous-state-r2/pid'
STOPS = ['artifacts/i14-autonomous-state-r1/STOP', 'artifacts/i14-autonomous-state-r2/STOP', ONCE + '/STOP']
SOURCE_IP, PHONE_IP, ASSET_PORT = '10.77.14.1', '10.77.14.2', 18144
KERNEL = '7.0.10-i14-bringup'
NATIVE_USB = {'idVendor': 0x1d6b, 'idProduct': 0x0104, 'USB Serial Number': 'I14_NATIVE_INIT', 'locationID': 0x02110000}
STOCK_USB = {'idVendor': 0x0e8d, 'idProduct': 0x2007, 'USB Serial Number': 'I14-STOCK-TRIAL-20261101', 'locationID': 0x02110000}
NATIVE_MODEL = ('9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a', '867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a')
STOCK_MODEL = ('77a29783599bebf2f2e339bea3f14992c9870f6f08bb211cd1a62da9ebbfe5ae', '33d69af0dce2161bb2211a0bb3c91313905301bae14f4d474de0204a74a4b47a')
RO_NAMES = {'mmcblk0', 'mmcblk0boot0', 'mmcblk0boot1'} | {'mmcblk0p%d' % i for i in range(1, 24)}
OLD_BOOT = 'b5ab7aeb-674d-4b33-ab26-f0094a516145'
OLD_GLOBAL_BLOCK = 'log partial/STOP/uncertain: native completion UNKNOWN; model reconciliation required'
UNKNOWN_HISTORY = {'r2_wall': 'UNKNOWN', 'pmic_adc': 'UNKNOWN_NOT_CLEARED', 'mode': 'FAILED_TERMINAL', 'endlog': 'PARTIAL_NATIVE_COMPLETION_UNKNOWN'}
ASSETS = {
    'stock-entry.page': (4096, '12e9a3f01a90e891970a46a8c6daad93498d6521a4d7c610626063f4a80e5131'),
    'Image': (16446424, '71b449fdf2b61207a0c48ea2e9bb977ae6f704ffe44a63edb2e97e1fe438b4e9'),
    'stock-charge.cpio.gz': (208378, 'e6f3775a16ea95f48f65446bf66628a0800af805bb39e250a2e186219ba40e1f'),
    'candidate.dtb': (75744, 'c05bb4bdfbd0efc9e71723dfcd446a8cd0db8820bf5cf5bcabe278febb9c69e4'),
    'i14-stock-ram-kexec': (71704, '86afced43f8c5c78f634e9fa658ec8065e4824a34ff306fe5e8bf4fcf7af45fd'),
}
PROFILE_SHA256 = '0696809f9579603be397f71720669e9c989e32affd84ec681ddd7010670e2c37'
HISTORY_PINS = {'artifacts/i14-autonomous-state-r1/state.json': '5ccb86d2c85b3bf379a3b78f856127829b1e7ced15160e007c0897fc85eaf73c', 'artifacts/i14-autonomous-state-r1/events.jsonl': '4fd35a5aba1fafb2175eae2d9591a9b7f194525adbfba3f109e600061fc10121', 'artifacts/i14-autonomous-state-r2/lineage.json': '8f9463dc79895863ae65fe0680f0003360d73b1b2e5e12d2ab492e23ce4468f2', 'artifacts/i14-r2-handoff-once/result.json': 'be0e7f5f259ce780228c0939f3b5861d940f772967820eae271de880cd8fc4e1', 'artifacts/i14-window-log-capture-once/attempt.json': '066d9ee0ddd04ed65dcde3c3fa34a7e887a25947754438066b67b22ff408327e', 'artifacts/i14-window-log-capture-once/result.json': 'cec3864d2ba159a80c63afc22041a2b4fcbf268fac0f952a49fc16984f93203f', 'artifacts/i14-window-log-capture-once/wire.raw': '6a896848bb2f33c068ef4a518ba312664230da8b9e3fd2d657120cbb99ce92d6', 'artifacts/i14-window-log-capture-once/stream.raw': '5a9ac462488436fab3f9599eefc96857d32b5d4e627a730887b309d4f713d3e1', 'artifacts/i14-window-log-capture-supervisor-reservation.json': 'cd802d37c102011d54caed9ba25aee29f0fac62870d0f24dd50e9345a87d74a7', 'artifacts/adc-session-reconciliation-20261101/REPORT.md': 'e3df4b9a4590f040b6800911e37e0bd097dd3a70162cf3480e31393cace3015f', 'docs/CHARGING-MODE-READY-2026-11-01.md': '509e64b867ce9e3c2f641c595a6216c3bc204284cd50abb29e2fe96abceed86f'}
TELNET_SOURCE = 'scripts/i14-window-log-capture.py'
TELNET_SOURCE_SHA256 = '1f488633f0401640577368393f278d8791aa1ac380636a2d75441a70f4d8900b'
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
HEX = r'[0-9a-f]{64}'
CHECK_LINE = b'I14_STOCK_CHECKED: four exact assets and native guards; NOT LOADED; charging UNKNOWN'


class Refused(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise Refused(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def local_read(path, cap=32 * 1024 * 1024):
    require(path.resolve() == path, 'symlinked input')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        a = os.fstat(stream.fileno())
        require(stat.S_ISREG(a.st_mode) and not a.st_mode & 0o022 and a.st_size <= cap, 'invalid local input')
        result = stream.read(cap + 1)
        b = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mode, s.st_mtime_ns, s.st_ctime_ns)
    require(len(result) <= cap and identity(a) == identity(b) == identity(path.stat(follow_symlinks=False)), 'local input changed')
    return result


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def data(path):
    return json.loads(local_read(path), object_pairs_hook=unique)


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def input_paths(root):
    return [root / 'scripts/i14-stock-trial-once.py', root / READY / 'manifest.json', root / TELNET_SOURCE,
            root / OLD_STATE, root / OLD_JOURNAL, root / OLD_PID] + [root / READY / 'assets' / name for name in ASSETS] + [root / name for name in HISTORY_PINS]


def check_history(root):
    for name, pin in HISTORY_PINS.items():
        require(digest(local_read(root / name)) == pin, 'old history/guard changed: ' + name)


def load_assets(root):
    require(digest(local_read(root / READY / 'manifest.json')) == PROFILE_SHA256, 'ready profile changed')
    result = {}
    for name, (size, pin) in ASSETS.items():
        blob = local_read(root / READY / 'assets' / name)
        require(len(blob) == size and digest(blob) == pin, 'asset changed: ' + name)
        result[name] = blob
    return result


def ref_data(root, reference):
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'}, 'missing evidence reference')
    name, pin = reference['path'], reference['sha256']
    require(isinstance(name, str) and name.startswith('artifacts/') and '..' not in Path(name).parts
            and not Path(name).is_absolute() and isinstance(pin, str) and re.fullmatch(HEX, pin), 'unsafe evidence reference')
    raw = local_read(root / name)
    require(digest(raw) == pin, 'qualification evidence changed')
    return json.loads(raw, object_pairs_hook=unique)


def guards(root):
    require(all(not os.path.lexists(root / name) for name in STOPS), 'STOP present')


def authorize(root, now):
    package = data(root / PACKAGE)
    require(isinstance(package, dict) and package.get('version') == 1 and package.get('reviewed') is True
            and package.get('action') == 'stock_ram_trial_once' and package.get('profile_sha256') == PROFILE_SHA256,
            'no reviewed fixed deployment release')
    require(package.get('source_sha256') == digest(local_read(root / 'scripts/i14-stock-trial-once.py')), 'worker source changed')
    require(digest(local_read(root / TELNET_SOURCE)) == TELNET_SOURCE_SHA256, 'transport source changed')
    qraw = local_read(root / QUALIFICATION)
    require(re.fullmatch(HEX, str(package.get('qualification_sha256', ''))) and digest(qraw) == package['qualification_sha256'], 'qualification pin missing/mismatch')
    q = json.loads(qraw, object_pairs_hook=unique)
    require(isinstance(q, dict) and q.get('version') == 1 and q.get('action') == 'stock_ram_trial_once'
            and q.get('decision') == 'QUALIFIED_FOR_ONE_STOCK_RAM_TRIAL' and q.get('profile_sha256') == PROFILE_SHA256
            and q.get('historical_unknowns') == UNKNOWN_HISTORY and re.fullmatch(UUID, str(q.get('boot_id', '')))
            and q['boot_id'] != OLD_BOOT, 'not a current reviewed stock qualification')
    issued, expiry = (datetime.datetime.fromisoformat(q[name]) for name in ('issued_utc', 'expires_utc'))
    require(issued.tzinfo is not None and expiry.tzinfo is not None and issued <= now < expiry
            and 0 < (expiry-issued).total_seconds() <= 120, 'qualification expired/unbounded')
    for role, decision in [('pmic_handoff', 'QUALIFIED_FOR_STOCK_VENDOR_PROBES'), ('power', 'QUALIFIED_FOR_LINUX_RAM_ROUTE'),
                           ('custody', 'QUALIFIED_HOST_CUSTODY_TRANSFER')]:
        proof = ref_data(root, q.get(role))
        require(isinstance(proof, dict) and proof.get('decision') == decision and isinstance(proof.get('evidence'), list) and proof['evidence'], 'missing substantive qualification evidence')
        for ref in proof['evidence']:
            require(isinstance(ref_data(root, ref), dict), 'invalid qualification evidence')
        review = ref_data(root, proof.get('review'))
        require(isinstance(review, dict) and review.get('verdict') == 'APPROVE' and review.get('action') == 'stock_ram_trial_once'
                and review.get('source_sha256') == package['source_sha256'] and review.get('evidence') == proof['evidence'], 'unbound qualification review')
        if role == 'pmic_handoff':
            require(proof.get('boot_id') == q['boot_id'] and proof.get('history_retained') is True, 'PMIC handoff wrong boot/history')
        elif role == 'power':
            require(proof.get('boot_id') == q['boot_id'] and proof.get('route') == 'linux_legacy_kexec', 'power contract wrong route/boot')
        else:
            require(proof.get('boot_id') == q['boot_id'] and proof.get('old_pid') == 57010 and proof.get('old_state_sha256') == digest(local_read(root / OLD_STATE)), 'unbound old custody')
    runtime, old = data(root / RUNTIME), data(root / OLD_STATE)
    require(isinstance(runtime, dict) and runtime.get('phase') == 'STOCK_TRIAL_QUALIFIED' and runtime.get('active_actor') == 'i14-stock-trial-once'
            and runtime.get('source_sha256') == package['source_sha256'] and runtime.get('boot_id') == q['boot_id']
            and runtime.get('qualification_sha256') == digest(qraw), 'current runtime does not grant stock custody')
    require(isinstance(old, dict) and old.get('phase') == 'STOPPED' and old.get('inflight') is None and old.get('reserved') is None
            and old.get('worker_active_at_stop') is False and old.get('global_block') == OLD_GLOBAL_BLOCK
            and old.get('log_capture', {}).get('status') == 'BLOCKED', 'old actor not reconciled/unknown was cleared')
    journal = local_read(root / OLD_JOURNAL).splitlines()
    require(journal, 'empty old exit journal')
    last = json.loads(journal[-1])
    require(isinstance(last, dict) and last.get('type') == 'STOP' and last.get('seq') == old.get('event_seq')
            and last.get('data') == {'owned_worker_active': False, 'owned_pid': None, 'recovered_pid': None}, 'old exit receipt missing')
    require(int(local_read(root / OLD_PID)) == 57010, 'old PID metadata changed')
    guards(root)
    check_history(root)
    return q


def snapshot_command(stock=False):
    kernel = '3.18.19' if stock else KERNEL
    model, compat = STOCK_MODEL if stock else NATIVE_MODEL
    body = "[ \"$(uname -r)\" = %s ] && [ \"$(uname -m)\" = aarch64 ] || exit 7; " % kernel
    body += "grep -qE '^Uid:[[:space:]]+0[[:space:]]+0[[:space:]]+0[[:space:]]+0$' /proc/self/status || exit 7; "
    body += 'h=$(sha256sum /proc/device-tree/model); [ "${h%% *}" = ' + model + ' ] || exit 7; '
    body += 'h=$(sha256sum /proc/device-tree/compatible); [ "${h%% *}" = ' + compat + ' ] || exit 7; '
    body += 'b=$(cat /proc/sys/kernel/random/boot_id); printf "KERNEL %s\\nARCH aarch64\\nROOT 0\\nMODEL %s\\nCOMPAT %s\\nBOOT %s\\n" ' + kernel + ' ' + model + ' ' + compat + ' "$b"; '
    body += 's=$(cat /sys/class/block/mmcblk0/size) || exit 7; [ "$s" = 30535680 ] || exit 7; printf "CAPACITY %s\\n" "$s"; '
    body += 'while read -r dev mount type rest; do case "$mount" in /) printf "RAM_ROOT %s\\n" "$type";; /run) printf "RUN_RAM %s\\n" "$type";; esac; done </proc/mounts; '
    body += '[ -d /run ] && [ ! -L /run ] || exit 7; printf "RUN_RAM inherited\\n"; '
    body += 'h=$(sha256sum /proc/modules); printf "MODULES %s\\n" "${h%% *}"; '
    body += 'c=0; for p in /sys/class/block/mmcblk*; do n=${p##*/}; case "$n" in mmcblk0|mmcblk0boot0|mmcblk0boot1|mmcblk0p[1-9]|mmcblk0p1[0-9]|mmcblk0p2[0-3]) ;; *) exit 7;; esac; '
    body += 'v=$(cat "$p/ro") || exit 7; [ "$v" = 1 ] || exit 7; c=$((c+1)); printf "RO %s %s\\n" "$n" "$v"; done; [ "$c" = 26 ] || exit 7; '
    body += 'set -- $(cat /sys/class/block/mmcblk0/stat); [ "$7" = 0 ] || exit 7; printf "WRITTEN %s\\n" "$7"; '
    if stock:
        for name in ('battery/batt_vol', 'battery/batt_temp', 'battery/status', 'usb/online', 'ac/online'):
            body += 'v=$(cat /sys/class/power_supply/' + name + ') || exit 7; printf "CACHE ' + name + ' %s\\n" "$v"; '
    body += 'printf "BOOT_END %s\\n" "$(cat /proc/sys/kernel/random/boot_id)"'
    return body


def parse_snapshot(raw, stock=False, expected_boot=None):
    fields, nodes, cache, run_seen = {}, {}, {}, False
    for line in raw.decode('ascii').splitlines():
        if line.startswith('RO '):
            parts = line.split()
            require(len(parts) == 3 and parts[1] not in nodes, 'bad/duplicate RO node')
            nodes[parts[1]] = parts[2]
        elif line.startswith('CACHE '):
            _, key, value = line.split(' ', 2)
            require(key not in cache, 'duplicate cache field')
            cache[key] = value
        else:
            key, value = line.split(' ', 1)
            if key == 'RUN_RAM':
                require(value in ('rootfs', 'ramfs', 'tmpfs', 'inherited'), 'non-RAM run mount')
                run_seen = True
                continue
            require(key not in fields, 'duplicate snapshot field')
            fields[key] = value
    model, compat = STOCK_MODEL if stock else NATIVE_MODEL
    expected = {'KERNEL': '3.18.19' if stock else KERNEL, 'ARCH': 'aarch64', 'ROOT': '0', 'MODEL': model,
                'COMPAT': compat, 'CAPACITY': '30535680', 'WRITTEN': '0'}
    require(set(fields) == set(expected) | {'BOOT', 'BOOT_END', 'RAM_ROOT', 'MODULES'} and run_seen,
            'missing/unexpected snapshot fields')
    require(all(fields.get(k) == v for k, v in expected.items()) and fields.get('RAM_ROOT') in ('rootfs', 'ramfs', 'tmpfs'), 'native identity/storage/root mismatch')
    require(re.fullmatch(UUID, fields.get('BOOT', '')) and fields.get('BOOT_END') == fields['BOOT']
            and (expected_boot is None or fields['BOOT'] == expected_boot), 'snapshot boot mismatch')
    require(set(nodes) == RO_NAMES and set(nodes.values()) == {'1'}, '26 exact RO nodes required')
    if not stock:
        require(fields.get('MODULES') == digest(b''), 'unexpected mainline module owner')
    else:
        require(set(cache) == {'battery/batt_vol', 'battery/batt_temp', 'battery/status', 'usb/online', 'ac/online'}, 'stock cache fields missing')
        require(all(value and len(value) <= 128 for value in cache.values()), 'unreadable stock cache field')
    return {'boot_id': fields['BOOT'], 'cached_status': cache, 'charging_status': 'UNKNOWN'}


def upload_command(name, token, boot):
    require(name in ASSETS and re.fullmatch(r'[0-9a-f]{32}', token) and re.fullmatch(UUID, boot), 'bad fixed upload parameters')
    path = '/run/i14-stock-trial/' + name
    return ('[ "$(cat /proc/sys/kernel/random/boot_id)" = ' + boot + ' ] || exit 7; '
            'test ! -e ' + path + '; printf "GET ' + token + ' ' + name + '\\n" | /bin/busybox nc -w 45 ' + SOURCE_IP + ' ' + str(ASSET_PORT) + ' >' + path + '; '
            '/bin/busybox chmod 600 ' + path + '; [ "$(cat /proc/sys/kernel/random/boot_id)" = ' + boot + ' ]')


def frame_payload(raw, token, phase):
    begin = ('@@I14_TRIAL_BEGIN ' + token + ' ' + phase).encode()
    end = ('@@I14_TRIAL_END ' + token + ' ' + phase + ' 0').encode()
    lines = raw.replace(b'\r\n', b'\n').splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip(b'\n') == begin]
    ends = [i for i, line in enumerate(lines) if line.rstrip(b'\n') == end]
    require(len(starts) == len(ends) == 1 and starts[0] < ends[0], 'no unique successful native frame')
    payload = b''.join(lines[starts[0]+1:ends[0]])
    require(payload.endswith(b'\n'), 'missing native frame separator')
    # Channel.command writes exactly one separator before END, even for empty output.
    return payload[:-1]


def loaded_token(raw, boot):
    pattern = rb'I14_STOCK_LOADED boot=(' + UUID.encode() + rb') token=([0-9a-f]{32}) entry=40000000 segments=4 phase=WAIT_EXECUTE charging=UNKNOWN\n'
    found = re.fullmatch(pattern, raw.replace(b'\r\n', b'\n'))
    require(found is not None and found[1].decode() == boot, 'no exact own load receipt')
    return found[2].decode()


def usb_records(tree):
    records, seen = [], {}
    def serial_paths(node):
        result = []
        if isinstance(node, dict):
            if 'idVendor' in node:
                return result
            if 'IOCalloutDevice' in node:
                result.append(node['IOCalloutDevice'])
            for child in node.get('IORegistryEntryChildren', []): result += serial_paths(child)
        return result
    def walk(node):
        if isinstance(node, list):
            for child in node: walk(child)
        elif isinstance(node, dict):
            if 'idVendor' in node:
                record = {key: node.get(key) for key in NATIVE_USB}
                paths = []
                for child in node.get('IORegistryEntryChildren', []): paths += serial_paths(child)
                if paths:
                    record['serial_paths'] = paths
                    if len(paths) == 1: record['serial_path'] = paths[0]
                registry_id = node.get('IORegistryEntryID')
                if type(registry_id) is int and registry_id in seen:
                    require(seen[registry_id] == record, 'conflicting USB registry entry')
                else:
                    records.append(record)
                    if type(registry_id) is int: seen[registry_id] = record
            for child in node.get('IORegistryEntryChildren', []): walk(child)
    walk(tree)
    return records


def classify_usb(records, stock=False):
    expected = STOCK_USB if stock else NATIVE_USB
    related = [x for x in records if x.get('locationID') == expected['locationID']
               or x.get('USB Serial Number') in (NATIVE_USB['USB Serial Number'], STOCK_USB['USB Serial Number'])
               or (x.get('idVendor'),x.get('idProduct')) in ((NATIVE_USB['idVendor'],NATIVE_USB['idProduct']),
                                                          (STOCK_USB['idVendor'],STOCK_USB['idProduct']))]
    if not related:
        return None
    require(len(related) == 1 and all(related[0].get(k) == v for k, v in expected.items()), 'ambiguous/wrong target USB')
    if stock:
        p = related[0].get('serial_path', '')
        require(re.fullmatch(r'/dev/cu\.usbmodem[A-Za-z0-9._-]+', p) is not None
                and len(related[0].get('serial_paths', [p])) == 1, 'stock serial path not bound')
        return p
    return True


def wait_target(ops, stock, deadline):
    while ops.monotonic() < deadline:
        records = ops.inventory()
        # The old interface can still be present before the planned transition.
        expected, previous = (STOCK_USB, NATIVE_USB) if stock else (NATIVE_USB, STOCK_USB)
        old = [x for x in records if all(x.get(k) == v for k, v in previous.items())]
        if len(old) == 1 and len([x for x in records if x.get('locationID') == expected['locationID']]) == 1:
            ops.sleep(1)
            continue
        result = classify_usb(records, stock)
        if result:
            require(ops.monotonic() < deadline, 'USB observation late')
            return result
        ops.sleep(1)
    raise Refused('target transition deadline; no reconnect/retry')


def asset_request(peer, request, token, assets, served):
    require(peer == PHONE_IP and len(request) < 128, 'asset peer/request refused')
    match = re.fullmatch(rb'GET ([0-9a-f]{32}) ([A-Za-z0-9._-]+)\n', request)
    require(match is not None and match[1].decode() == token, 'asset token refused')
    name = match[2].decode()
    require(name in assets and name not in served, 'asset name/reuse refused')
    served.add(name)
    return assets[name]


def pipeline(root, ops, out, boot, assets, refresh):
    channel = serial = None
    try:
        require(classify_usb(ops.inventory()) is True and ops.source_ready(), 'native USB/source unavailable')
        channel = ops.connect(root, out, 'mainline')
        baseline = parse_snapshot(channel.command(snapshot_command(), 'baseline', ops.monotonic()+12), expected_boot=boot)
        save(out / 'baseline.json', baseline)
        refresh()
        host_token = secrets.token_hex(16)
        stage_deadline = ops.monotonic()+90
        with ops.asset_server(assets, host_token, out):
            channel.command('test ! -e /run/i14-stock-trial; umask 077; /bin/busybox mkdir -m 700 /run/i14-stock-trial', 'mkdir', ops.monotonic()+5)
            for i, name in enumerate(ASSETS):
                refresh()
                require(ops.monotonic() < stage_deadline, 'asset stage exceeded short window budget')
                channel.command(upload_command(name, host_token, boot), 'upload_'+str(i), min(stage_deadline,ops.monotonic()+50))
                require(ops.monotonic() < stage_deadline, 'asset stage exceeded short window budget')
        boot_guard = '[ "$(cat /proc/sys/kernel/random/boot_id)" = ' + boot + ' ] || exit 7; '
        hashes = channel.command(boot_guard+'sha256sum ' + ' '.join('/run/i14-stock-trial/'+name for name in ASSETS)+'; '+boot_guard+'true', 'readback', ops.monotonic()+15)
        expected = ''.join(pin+'  /run/i14-stock-trial/'+name+'\n' for name, (_, pin) in ASSETS.items()).encode()
        require(hashes.replace(b'\r\n',b'\n') == expected, 'native asset readback mismatch')
        save(out / 'native-asset-hashes.json', {'sha256': {name: pin for name, (_, pin) in ASSETS.items()}, 'boot_id':boot})
        channel.command('/bin/busybox chmod 700 /run/i14-stock-trial/i14-stock-ram-kexec', 'chmod', ops.monotonic()+5)
        checked = channel.command(boot_guard+'/run/i14-stock-trial/i14-stock-ram-kexec --check; '+boot_guard+'true', 'check', ops.monotonic()+15)
        require(checked.strip() == CHECK_LINE, 'native check did not pass')
        refresh()
        save(out / 'load-intent.json', {'boot_id':boot,'assets_sha256':digest(expected),'status':'CONSUMED_BEFORE_LOAD'})
        deadline = ops.monotonic()+4
        token = loaded_token(channel.load_receipt(deadline, boot), boot)
        require(ops.monotonic() < deadline, 'load receipt late')
        save(out / 'loaded.json', {'boot_id':boot,'token_sha256':digest(token.encode()),'entry':'40000000','segments':4})
        refresh()
        save(out / 'execute-intent.json', {'boot_id':boot,'status':'CONSUMED_BEFORE_EXECUTE_TOKEN'})
        require(ops.monotonic() < deadline, 'execute token deadline')
        channel.execute(token, deadline)
        channel.close(); channel = None
        executed = ops.monotonic()
        serial_path = wait_target(ops, True, executed+45)
        serial = ops.connect(root, out, 'stock', serial_path)
        stock = parse_snapshot(serial.command(snapshot_command(True), 'stock', ops.monotonic()+15), stock=True)
        require(stock['boot_id'] != boot, 'stock boot did not change')
        save(out / 'stock-cached.json', stock)
        serial.close(); serial = None
        wait_target(ops, False, executed+180)
        require(ops.source_ready(), 'normal-return source unavailable')
        channel = ops.connect(root, out, 'return')
        returned = parse_snapshot(channel.command(snapshot_command(), 'return', ops.monotonic()+12))
        require(returned['boot_id'] not in (boot, stock['boot_id']), 'normal return has no new boot')
        save(out / 'normal-return.json', returned)
        return {'status':'STOCK_CAPTURE_AND_NORMAL_RETURN_VERIFIED','native_boot':boot,'stock_boot':stock['boot_id'],
                'return_boot':returned['boot_id'],'charging_status':'UNKNOWN','persistent_installation_complete':False,
                'normal_return_evidence':'Expected installed kernel/USB and a new boot; no independent reboot-cause receipt.',
                'reboot_cause':'UNKNOWN',
                'cached_status_note':'Historical kernel reports only; no physical charging or SOC acceptance.'}
    except (OSError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, Refused): raise
        raise Refused('trial outcome UNKNOWN; preserve attempt: '+str(error)) from error
    finally:
        if channel is not None: channel.close()
        if serial is not None: serial.close()


def execute(root=ROOT, *, ops=None):
    root = Path(root).resolve()
    ops = RealOps() if ops is None else ops
    out = root / ONCE
    try:
        q = authorize(root, ops.utcnow())
        assets = load_assets(root)
        require(not os.path.lexists(out), 'global stock trial already consumed')
        require(ops.old_actor_gone(57010), 'old actor still present; no takeover')
        with ops.locks(root):
            require(authorize(root, ops.utcnow()) == q and not os.path.lexists(out), 'qualification changed before reservation')
            out.mkdir(mode=0o700, exist_ok=False)
            sync_directory(out.parent)
            save(out / 'intent.json', {'version':1,'action':'stock_ram_trial_once','boot_id':q['boot_id'],
                 'profile_sha256':PROFILE_SHA256,'qualification_sha256':digest(local_read(root/QUALIFICATION)),
                 'status':'CONSUMED_BEFORE_FIRST_TRANSPORT','historical_unknowns':UNKNOWN_HISTORY,'pid':os.getpid()})
            def refresh():
                require(authorize(root, ops.utcnow()) == q, 'dispatch qualification changed/expired')
            try:
                result = pipeline(root, ops, out, q['boot_id'], assets, refresh)
                save(out / 'result.json', result)
                return result
            except Exception as error:
                save(out / 'result.json', {'status':'UNKNOWN_NO_RETRY','reason':str(error),'charging_status':'UNKNOWN',
                                         'persistent_installation_complete':False,'historical_unknowns':UNKNOWN_HISTORY})
                raise
    except Refused:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Refused('stock trial refused/unknown; no retry: '+str(error)) from error


class AssetServer:
    """Five raw, token-bound transfers; only the direct USB pair can participate."""
    def __init__(self, assets, token):
        self.assets, self.token, self.served = assets, token, set()
        self.stop = threading.Event()
        self.listener = self.connection = self.thread = None
        self.failure = None

    def __enter__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.bind((SOURCE_IP, ASSET_PORT))
            self.listener.listen(1); self.listener.settimeout(0.2)
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()
            return self
        except Exception:
            self.listener.close()
            raise

    def serve(self):
        try:
            while not self.stop.is_set() and len(self.served) < len(self.assets):
                try: connection, peer = self.listener.accept()
                except socket.timeout: continue
                self.connection = connection
                with connection:
                    connection.settimeout(5)
                    request = bytearray()
                    deadline = time.monotonic()+5
                    while len(request) < 128 and not request.endswith(b'\n') and not self.stop.is_set():
                        if time.monotonic() >= deadline: break
                        connection.settimeout(min(0.5, deadline-time.monotonic()))
                        try: b = connection.recv(1)
                        except socket.timeout: continue
                        if not b: break
                        request.extend(b)
                    try: payload = asset_request(peer[0], bytes(request), self.token, self.assets, self.served)
                    except Refused: continue
                    pending, deadline = memoryview(payload), time.monotonic()+40
                    while pending:
                        require(not self.stop.is_set() and time.monotonic() < deadline, 'asset send deadline/stop')
                        connection.settimeout(min(0.5,deadline-time.monotonic()))
                        try: sent=connection.send(pending[:65536])
                        except socket.timeout: continue
                        require(sent > 0, 'asset send incomplete')
                        pending=pending[sent:]
                self.connection = None
        except Exception as error:
            if not self.stop.is_set(): self.failure = error

    def __exit__(self, kind, value, trace):
        self.stop.set()
        if self.connection is not None:
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        self.listener.close()
        if self.thread is not None: self.thread.join(timeout=6)
        if kind is None:
            require(self.failure is None and self.served == set(self.assets) and not self.thread.is_alive(), 'asset transfer not complete')


class Channel:
    def __init__(self, fd, out, phase, *, sock=None, telnet=None):
        self.fd, self.sock, self.telnet = fd, sock, telnet
        self.token = secrets.token_hex(16)
        self.buffer = bytearray()
        self.wire = (out / ('wire-'+phase+'.raw')).open('xb')
        self.bytes = 0

    def send(self, raw, deadline):
        require(time.monotonic() < deadline, 'late native send')
        pending = memoryview(raw)
        while pending:
            require(time.monotonic() < deadline, 'native send deadline')
            _, ready, _ = select.select([], [self.fd], [], min(1, max(0, deadline-time.monotonic())))
            if not ready: continue
            count = self.sock.send(pending) if self.sock is not None else os.write(self.fd, pending)
            require(count > 0, 'native send incomplete')
            pending = pending[count:]

    def receive(self, deadline):
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], min(1,max(0,deadline-time.monotonic())))
            if not ready: continue
            raw = self.sock.recv(65536) if self.sock is not None else os.read(self.fd,65536)
            require(raw, 'native transport EOF')
            self.bytes += len(raw); require(self.bytes <= 512*1024, 'native wire cap')
            self.wire.write(raw); self.wire.flush(); os.fsync(self.wire.fileno())
            if self.telnet is not None:
                raw, replies = self.telnet.feed(raw)
                for reply in replies: self.send(reply, deadline)
            self.buffer.extend(raw)
            return
        raise Refused('native receive deadline; no retry')

    def command(self, body, phase, deadline):
        require(re.fullmatch(r'[a-z_0-9]+',phase), 'invalid fixed phase')
        token = self.token
        wrapper = "printf '\\n@@I14_TRIAL_BEGIN %s %s\\n'; (set -euC; %s); r=$?; printf '\\n@@I14_TRIAL_END %s %s %%s\\n' \"$r\"\n" % (token,phase,body,token,phase)
        self.buffer.clear()
        self.send(wrapper.encode('ascii'),deadline)
        terminal = ('\n@@I14_TRIAL_END '+token+' '+phase+' ').encode()
        while True:
            normalized = bytes(self.buffer).replace(b'\r\n',b'\n')
            if terminal in normalized and normalized[normalized.index(terminal)+len(terminal):].find(b'\n') >= 0:
                return frame_payload(normalized, token, phase)
            self.receive(deadline)

    def load_receipt(self, deadline, boot):
        require(re.fullmatch(UUID,boot), 'invalid native load boot')
        self.buffer.clear()
        self.send(('[ "$(cat /proc/sys/kernel/random/boot_id)" = '+boot+' ] || exit 7; exec /run/i14-stock-trial/i14-stock-ram-kexec --load-and-wait\n').encode(),deadline)
        while True:
            normalized=bytes(self.buffer).replace(b'\r\n',b'\n')
            matches=[line for line in normalized.splitlines(keepends=True) if line.startswith(b'I14_STOCK_LOADED ') and line.endswith(b'\n')]
            if matches:
                require(len(matches)==1,'duplicate load receipt')
                return matches[0]
            self.receive(deadline)

    def execute(self, token, deadline):
        require(re.fullmatch(r'[0-9a-f]{32}',token), 'bad execute token')
        self.send(('EXECUTE '+token+'\n').encode(),deadline)

    def close(self):
        self.wire.close()
        if self.sock is not None: self.sock.close()
        else: os.close(self.fd)


class RealOps:
    def utcnow(self): return datetime.datetime.now(datetime.timezone.utc)
    def monotonic(self): return time.monotonic()
    def sleep(self, seconds): time.sleep(seconds)

    def old_actor_gone(self, pid):
        try: os.kill(pid,0)
        except ProcessLookupError: return True
        return False

    @contextlib.contextmanager
    def locks(self, root):
        fds=[]
        try:
            for name in ['artifacts/i14-autonomous-state-r1/supervisor.lock','artifacts/i14-autonomous-state-r2/supervisor.lock','artifacts/i14-native-watch.lock']:
                path=root/name
                require(path.resolve()==path,'symlinked custody lock')
                fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK);fds.append(fd)
                require(stat.S_ISREG(os.fstat(fd).st_mode),'invalid custody lock')
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally:
            for fd in reversed(fds):os.close(fd)

    def inventory(self):
        result=subprocess.run(['ioreg','-p','IOService','-r','-c','IOUSBHostDevice','-a','-l'],capture_output=True,check=True,timeout=5)
        require(len(result.stdout)<=32*1024*1024,'USB registry cap')
        return usb_records(plistlib.loads(result.stdout))

    def source_ready(self):
        s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:s.bind((SOURCE_IP,0));return True
        except OSError:return False
        finally:s.close()

    def connect(self, root, out, phase, serial_path=None):
        if phase in ('mainline','return'):
            source=root/TELNET_SOURCE;code=local_read(source);require(digest(code)==TELNET_SOURCE_SHA256,'transport changed')
            m=types.ModuleType('pinned_telnet');m.__file__=str(source);exec(compile(code,str(source),'exec'),m.__dict__)
            s=socket.create_connection((PHONE_IP,23),timeout=5,source_address=(SOURCE_IP,0));s.setblocking(False)
            return Channel(s.fileno(),out,phase,sock=s,telnet=m.Telnet())
        require(phase=='stock' and re.fullmatch(r'/dev/cu\.usbmodem[A-Za-z0-9._-]+',str(serial_path)),'unbound serial endpoint')
        fd=os.open(serial_path,os.O_RDWR|os.O_NOCTTY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd);require(stat.S_ISCHR(before.st_mode),'not a character serial endpoint')
            attrs=termios.tcgetattr(fd);attrs[0]=0;attrs[1]=0;attrs[2]=termios.CLOCAL|termios.CREAD|termios.CS8;attrs[3]=0
            attrs[4]=attrs[5]=termios.B115200;attrs[6][termios.VMIN]=0;attrs[6][termios.VTIME]=0
            termios.tcsetattr(fd,termios.TCSANOW,attrs)
            after=os.stat(serial_path,follow_symlinks=False);require((before.st_dev,before.st_ino)==(after.st_dev,after.st_ino),'serial path replaced')
            return Channel(fd,out,phase)
        except Exception:os.close(fd);raise

    def asset_server(self, assets, token, out):
        return AssetServer(assets,token)


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv) != ['--execute-once']:
        print('usage: i14-stock-trial-once.py --execute-once',file=sys.stderr);return 5
    try:
        print(json.dumps(execute()));return 0
    except Exception as error:
        print('REFUSED/UNKNOWN: '+str(error)+'; no retry',file=sys.stderr);return 5


if __name__ == '__main__':
    sys.exit(main())
