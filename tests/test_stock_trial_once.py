"""Fixed stock trial: only temporary files and fake hardware/network boundaries."""
import contextlib
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/i14-stock-trial-once.py'
BOOT = '12345678-1234-4234-8234-123456789abc'
STOCK_BOOT = '22345678-1234-4234-8234-123456789abc'
RETURN_BOOT = '32345678-1234-4234-8234-123456789abc'
NOW = datetime.datetime(2026, 11, 2, 2, tzinfo=datetime.timezone.utc)


class FakeChannel:
    def __init__(self, ops, kind):
        self.ops, self.kind = ops, kind

    def command(self, body, phase, deadline):
        self.ops.calls.append(('command', phase, body))
        if phase == 'baseline':
            return self.ops.snapshot(False, BOOT)
        if phase.startswith('upload_') or phase in ('mkdir', 'chmod'):
            if self.ops.mode=='upload_deadline' and phase=='upload_0':self.ops.clock+=91
            return b''
        if phase == 'readback':
            return ''.join(pin + '  /run/i14-stock-trial/' + name + '\n'
                           for name, (_, pin) in self.ops.module.ASSETS.items()).encode() if self.ops.mode != 'readback' else b'bad\n'
        if phase == 'check':
            return b'I14_STOCK_CHECKED: four exact assets and native guards; NOT LOADED; charging UNKNOWN\n' if self.ops.mode != 'check' else b'bad\n'
        if phase == 'stock':
            self.ops.stock_captured = True
            return self.ops.snapshot(True, STOCK_BOOT)
        if phase == 'return':
            return self.ops.snapshot(False, BOOT if self.ops.mode == 'return_boot' else RETURN_BOOT)
        raise AssertionError(phase)

    def load_receipt(self, deadline, boot):
        self.ops.calls.append(('load', deadline))
        if boot!=BOOT:raise AssertionError('unbound native load boot')
        if self.ops.mode == 'late':
            self.ops.clock += 10
        if self.ops.mode == 'load_failure':
            raise OSError('fixture failed load')
        line = ('I14_STOCK_LOADED boot=%s token=%s entry=40000000 segments=4 phase=WAIT_EXECUTE charging=UNKNOWN\n' %
                (RETURN_BOOT if self.ops.mode == 'load_boot' else BOOT, 'a' * 32)).encode()
        return b'echo ' + line if self.ops.mode == 'echo' else line

    def execute(self, token, deadline):
        self.ops.calls.append(('execute', token))
        self.ops.executed = True
        if self.ops.mode == 'execute_drop':
            raise OSError('fixture send uncertain')

    def close(self):
        self.ops.calls.append(('close', self.kind))


class FakeOps:
    def __init__(self, module, mode='normal'):
        self.module, self.mode, self.clock = module, mode, 0.0
        self.calls = []
        self.executed = self.stock_captured = False

    def utcnow(self):
        return NOW + datetime.timedelta(seconds=self.clock)

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def old_actor_gone(self, pid):
        self.calls.append(('old_actor_gone', pid))
        return self.mode != 'old_actor'

    @contextlib.contextmanager
    def locks(self, root):
        self.calls.append(('locks',))
        if self.mode == 'lock':
            raise BlockingIOError('fixture busy')
        try:
            yield
        finally:
            self.calls.append(('unlock',))

    def inventory(self):
        self.calls.append(('inventory',))
        if self.executed and not self.stock_captured:
            if self.mode == 'no_stock':
                return []
            record = dict(self.module.STOCK_USB, serial_path='/dev/cu.usbmodemTEST')
            return [record, dict(record)] if self.mode == 'ambiguous_stock' else [record]
        if self.stock_captured and self.mode == 'no_return':
            return []
        record = dict(self.module.NATIVE_USB)
        if self.mode == 'usb':
            record['locationID'] += 1
        return [record]

    def source_ready(self):
        self.calls.append(('source_ready',))
        return self.mode != 'source'

    def connect(self, root, out, phase, serial_path=None):
        self.calls.append(('connect', phase, serial_path))
        return FakeChannel(self, phase)

    @contextlib.contextmanager
    def asset_server(self, assets, token, out):
        self.calls.append(('server',))
        try:
            yield
        finally:
            self.calls.append(('server_closed',))

    def snapshot(self, stock, boot):
        m = self.module
        model, compat = m.STOCK_MODEL if stock else m.NATIVE_MODEL
        if self.mode == 'model' and not stock:
            model = '0' * 64
        lines = ['KERNEL ' + ('3.18.19' if stock else m.KERNEL), 'ARCH aarch64',
                 'ROOT ' + ('1' if self.mode == 'root' and not stock else '0'),
                 'MODEL ' + model, 'COMPAT ' + compat,
                 'BOOT ' + (RETURN_BOOT if self.mode == 'boot' and not stock else boot),
                 'CAPACITY ' + ('1' if self.mode == 'capacity' and not stock else '30535680'),
                 'RAM_ROOT rootfs', 'RUN_RAM inherited', 'MODULES ' + hashlib.sha256(b'').hexdigest()]
        for name in sorted(m.RO_NAMES):
            if self.mode == 'missing_ro' and name == 'mmcblk0p23' and not stock:
                continue
            lines.append('RO %s %s' % (name, '0' if self.mode == 'ro' and name == 'mmcblk0' and not stock else '1'))
        lines += ['WRITTEN 0', 'BOOT_END ' + boot]
        if stock:
            lines += ['CACHE battery/batt_vol 3400', 'CACHE battery/batt_temp 250',
                      'CACHE battery/status Charging', 'CACHE usb/online 1', 'CACHE ac/online 0']
        return ('\n'.join(lines) + '\n').encode()


class StockTrialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.exists():
            return
        spec = importlib.util.spec_from_file_location('stock_trial', SCRIPT)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)
        cls.blobs = {str(path): path.read_bytes() for path in cls.m.input_paths(ROOT)}

    def setUp(self):
        self.assertTrue(SCRIPT.exists(), 'missing concrete fixed stock-trial worker')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for source, blob in self.blobs.items():
            p = self.root / Path(source).relative_to(ROOT)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(blob)
        self.ops = FakeOps(self.m)
        self.make_release()

    def write(self, rel, value):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))
        return {'path': rel, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}

    def make_release(self):
        m = self.m
        state = {'phase': 'STOPPED', 'inflight': None, 'reserved': None, 'worker_active_at_stop': False,
                 'global_block': m.OLD_GLOBAL_BLOCK, 'log_capture': {'status': 'BLOCKED'}, 'event_seq': 99}
        self.write(m.OLD_STATE, state)
        self.write(m.OLD_JOURNAL, {'seq':99, 'type':'STOP', 'data':{'owned_worker_active':False,'owned_pid':None,'recovered_pid':None}})
        self.write(m.RUNTIME, {'phase': 'STOCK_TRIAL_QUALIFIED', 'active_actor': 'i14-stock-trial-once',
                               'source_sha256': hashlib.sha256(SCRIPT.read_bytes()).hexdigest()})
        evidence = self.write(m.QUAL_DIR + '/support.json', {'fixture': 'fake evidence; never production authorization'})
        review = self.write(m.QUAL_DIR + '/review.json', {'verdict': 'APPROVE', 'action': 'stock_ram_trial_once',
                                'source_sha256': hashlib.sha256(SCRIPT.read_bytes()).hexdigest(), 'evidence': [evidence]})
        pmic = self.write(m.QUAL_DIR + '/pmic.json', {'decision': 'QUALIFIED_FOR_STOCK_VENDOR_PROBES',
                         'boot_id': BOOT, 'history_retained': True, 'evidence': [evidence], 'review': review})
        power = self.write(m.QUAL_DIR + '/power.json', {'decision': 'QUALIFIED_FOR_LINUX_RAM_ROUTE',
                         'boot_id': BOOT, 'route': 'linux_legacy_kexec', 'evidence': [evidence], 'review': review})
        custody = self.write(m.QUAL_DIR + '/custody.json', {'decision': 'QUALIFIED_HOST_CUSTODY_TRANSFER',
                             'boot_id': BOOT, 'old_pid': 57010, 'old_state_sha256': hashlib.sha256((self.root/m.OLD_STATE).read_bytes()).hexdigest(),
                             'evidence': [evidence], 'review': review})
        q = {'version': 1, 'action': 'stock_ram_trial_once', 'decision': 'QUALIFIED_FOR_ONE_STOCK_RAM_TRIAL',
             'boot_id': BOOT, 'issued_utc': (NOW-datetime.timedelta(seconds=5)).isoformat(),
             'expires_utc': (NOW+datetime.timedelta(seconds=110)).isoformat(),
             'profile_sha256': m.PROFILE_SHA256, 'historical_unknowns': m.UNKNOWN_HISTORY,
             'pmic_handoff': pmic, 'power': power, 'custody': custody}
        qref = self.write(m.QUALIFICATION, q)
        self.write(m.RUNTIME, {'phase': 'STOCK_TRIAL_QUALIFIED', 'active_actor': 'i14-stock-trial-once',
                               'source_sha256': hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                               'boot_id': BOOT, 'qualification_sha256': qref['sha256']})
        self.package = {'version': 1, 'reviewed': True, 'action': 'stock_ram_trial_once',
                        'source_sha256': hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                        'profile_sha256': m.PROFILE_SHA256, 'qualification_sha256': qref['sha256']}
        self.write(m.PACKAGE, self.package)

    def run_worker(self, mode='normal'):
        self.ops.mode = mode
        return self.m.execute(self.root, ops=self.ops)

    def test_success_keeps_charging_unknown_and_returns_with_new_boot(self):
        result = self.run_worker()
        self.assertEqual(result['status'], 'STOCK_CAPTURE_AND_NORMAL_RETURN_VERIFIED')
        self.assertEqual(result['charging_status'], 'UNKNOWN')
        self.assertFalse(result['persistent_installation_complete'])
        self.assertEqual(result['native_boot'], BOOT)
        self.assertEqual(result['stock_boot'], STOCK_BOOT)
        self.assertEqual(result['return_boot'], RETURN_BOOT)
        self.assertEqual(len([x for x in self.ops.calls if x[0]=='load']), 1)
        self.assertEqual(len([x for x in self.ops.calls if x[0]=='execute']), 1)
        self.assertTrue((self.root/self.m.ONCE/'intent.json').exists())
        self.assertIn(('server_closed',),self.ops.calls)

    def test_unreviewed_missing_or_boolean_only_qualification_has_no_boundaries(self):
        for variant in ('unreviewed', 'missing', 'boolean'):
            with self.subTest(variant=variant):
                if variant=='unreviewed': self.package['reviewed']=False;self.write(self.m.PACKAGE,self.package)
                elif variant=='missing': (self.root/self.m.QUALIFICATION).unlink()
                else:
                    self.write(self.m.QUALIFICATION, {'qualified': True})
                    self.package['qualification_sha256']=hashlib.sha256((self.root/self.m.QUALIFICATION).read_bytes()).hexdigest();self.write(self.m.PACKAGE,self.package)
                with self.assertRaises(self.m.Refused):self.run_worker()
                self.assertEqual(self.ops.calls,[])
                self.make_release()

    def test_changed_source_asset_or_old_unknown_guard_refuses_before_boundary(self):
        for rel in ['scripts/i14-stock-trial-once.py', self.m.READY+'/assets/Image', *list(self.m.HISTORY_PINS)[:1]]:
            with self.subTest(rel=rel):
                p=self.root/rel;before=p.read_bytes();p.write_bytes(before+b'x')
                with self.assertRaises(self.m.Refused):self.run_worker()
                self.assertEqual(self.ops.calls,[])
                p.write_bytes(before)

    def test_existing_attempt_and_stop_refuse_without_clearing_anything(self):
        p=self.root/self.m.ONCE;p.mkdir(parents=True)
        with self.assertRaises(self.m.Refused):self.run_worker()
        self.assertTrue(p.exists());self.assertEqual(self.ops.calls,[])
        p.rmdir();stop=self.root/self.m.STOPS[0];stop.parent.mkdir(parents=True,exist_ok=True);stop.touch()
        with self.assertRaises(self.m.Refused):self.run_worker()
        self.assertTrue(stop.exists());self.assertEqual(self.ops.calls,[])

    def test_old_actor_or_busy_lock_never_connects(self):
        for mode in ('old_actor','lock'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                self.ops.calls=[]
                with self.assertRaises(self.m.Refused):self.run_worker(mode)
                self.assertFalse(any(x[0] in ('connect','server') for x in self.ops.calls))

    def test_wrong_usb_source_identity_boot_ro_capacity_never_stages_or_loads(self):
        for mode in ('usb','source','root','model','boot','ro','missing_ro','capacity'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                # Each fixture is a different hypothetical host, not a retry of real evidence.
                oldroot=self.root;self.root=Path(tmp).resolve()
                for source,blob in self.blobs.items():
                    p=self.root/Path(source).relative_to(ROOT);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(blob)
                self.make_release();self.ops=FakeOps(self.m,mode)
                with self.assertRaises(self.m.Refused):self.run_worker(mode)
                self.assertFalse(any(x[0] in ('server','load','execute') for x in self.ops.calls))
                self.root=oldroot

    def test_readback_check_echo_failure_late_or_boot_change_never_executes(self):
        for mode in ('readback','check','echo','load_failure','late','load_boot'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                # This test calls the pure phase pipeline; global once semantics are separate.
                self.ops=FakeOps(self.m,mode)
                with self.assertRaises(self.m.Refused):
                    self.m.pipeline(self.root,self.ops,Path(tmp),BOOT,self.m.load_assets(self.root),lambda:None)
                self.assertFalse(any(x[0]=='execute' for x in self.ops.calls))
                self.assertIn(('server_closed',),self.ops.calls)

    def test_post_execute_loss_stock_ambiguity_or_missing_return_stays_unknown(self):
        for mode in ('execute_drop','no_stock','ambiguous_stock','no_return','return_boot'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                self.ops=FakeOps(self.m,mode)
                with self.assertRaises(self.m.Refused):
                    self.m.pipeline(self.root,self.ops,Path(tmp),BOOT,self.m.load_assets(self.root),lambda:None)
                self.assertEqual(len([x for x in self.ops.calls if x[0]=='execute']),1)

    def test_completed_attempt_cannot_run_again_even_with_other_boot(self):
        self.run_worker();self.ops.calls=[]
        with self.assertRaises(self.m.Refused):self.run_worker()
        self.assertEqual(self.ops.calls,[])

    def test_frames_and_loader_receipts_reject_echo_duplicate_and_wrong_nonce(self):
        text=b"echo @@BEGIN token\n@@I14_TRIAL_BEGIN "+b'1'*32+b" baseline\nhello\n\n@@I14_TRIAL_END "+b'1'*32+b" baseline 0\n"
        self.assertEqual(self.m.frame_payload(text,'1'*32,'baseline'),b'hello\n')
        for raw in (b'echo '+text,text+text):
            if raw==b'echo '+text:continue
            with self.assertRaises(self.m.Refused):self.m.frame_payload(raw,'1'*32,'baseline')
        with self.assertRaises(self.m.Refused):self.m.frame_payload(text,'2'*32,'baseline')
        receipt=('I14_STOCK_LOADED boot=%s token=%s entry=40000000 segments=4 phase=WAIT_EXECUTE charging=UNKNOWN\n'%(BOOT,'a'*32)).encode()
        self.assertEqual(self.m.loaded_token(receipt,BOOT),'a'*32)
        for raw in (b'echo '+receipt,receipt+receipt,receipt.replace(BOOT.encode(),RETURN_BOOT.encode())):
            with self.assertRaises(self.m.Refused):self.m.loaded_token(raw,BOOT)

    def test_raw_asset_request_is_usb_peer_token_allowlist_and_once_bound(self):
        served=set();assets={'Image':b'payload'};token='b'*32
        self.assertEqual(self.m.asset_request('10.77.14.2',('GET '+token+' Image\n').encode(),token,assets,served),b'payload')
        for peer,line in [('192.0.2.1','GET '+token+' Image\n'),('10.77.14.2','GET '+token+' ../Image\n'),('10.77.14.2','GET '+'c'*32+' Image\n'),('10.77.14.2','GET '+token+' Image\n')]:
            with self.assertRaises(self.m.Refused):self.m.asset_request(peer,line.encode(),token,assets,served)

    def test_shell_commands_use_available_nc_not_base64_wget_or_huge_payload(self):
        command=self.m.upload_command('Image','b'*32,BOOT)
        self.assertIn(' nc -w ',command)
        self.assertIn('10.77.14.1',command)
        self.assertLess(len(command),1000)
        self.assertNotIn('base64',command);self.assertNotIn('wget',command)
        stock=self.m.snapshot_command(True)
        for forbidden in ('voltage_now','current_now','charger_state','battery_sample','insmod','; mount ','reboot','devmem'):
            self.assertNotIn(forbidden,stock)

    def test_usb_serial_mapping_requires_exact_ancestor_and_unambiguous_callout(self):
        node={**self.m.STOCK_USB,'IORegistryEntryChildren':[{'IOCalloutDevice':'/dev/cu.usbmodem123'}]}
        self.assertEqual(self.m.usb_records([node])[0]['serial_path'],'/dev/cu.usbmodem123')
        node['IORegistryEntryChildren'].append({'IOCalloutDevice':'/dev/cu.usbmodem456'})
        with self.assertRaises(self.m.Refused):self.m.classify_usb(self.m.usb_records([node]),stock=True)

    def test_pinned_real_profile_assets_and_history_remain_exact(self):
        assets=self.m.load_assets(ROOT)
        self.assertEqual(sum(map(len,assets.values())),16806346)
        self.m.check_history(ROOT)

    def test_parent_directory_fsync_failure_prevents_first_transport(self):
        artifacts_inode=(self.root/'artifacts').stat().st_ino
        original=self.m.os.fsync
        def fail_parent(fd):
            if self.m.os.fstat(fd).st_ino==artifacts_inode:
                raise OSError('fixture parent directory fsync failed')
            return original(fd)
        with patch.object(self.m.os,'fsync',side_effect=fail_parent):
            with self.assertRaises(self.m.Refused):self.run_worker()
        self.assertFalse(any(x[0] in ('connect','server','load') for x in self.ops.calls))

    def test_duplicate_registry_root_deduplicates_only_same_registry_entry(self):
        node={**self.m.STOCK_USB,'IORegistryEntryID':77,
              'IORegistryEntryChildren':[{'IOCalloutDevice':'/dev/cu.usbmodem123'}]}
        self.assertEqual(len(self.m.usb_records([node,dict(node)])),1)
        second=dict(node,IORegistryEntryID=78)
        with self.assertRaises(self.m.Refused):self.m.classify_usb(self.m.usb_records([node,second]),True)

    def test_asset_server_stop_during_request_stops_reading(self):
        server=self.m.AssetServer({'Image':b'x'},'a'*32)
        class Connection:
            reads=0
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,*args):pass
            def recv(self,n):
                self.reads+=1
                server.stop.set()
                return b'G' if self.reads==1 else b'\n'
        connection=Connection()
        class Listener:
            def accept(self):return connection,('10.77.14.2',123)
        server.listener=Listener();server.serve()
        self.assertEqual(connection.reads,1)

    def test_malformed_json_shape_or_empty_exit_journal_refuses_cleanly(self):
        for name,value in [(self.m.PACKAGE,[]),(self.m.QUALIFICATION,[]),(self.m.OLD_JOURNAL,None)]:
            with self.subTest(name=name):
                path=self.root/name;before=path.read_bytes()
                path.write_text('' if value is None else json.dumps(value))
                if name==self.m.QUALIFICATION:
                    self.package['qualification_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();self.write(self.m.PACKAGE,self.package)
                with self.assertRaises(self.m.Refused):self.run_worker()
                self.assertEqual(self.ops.calls,[])
                path.write_bytes(before);self.make_release()

    def test_empty_or_unexpected_stock_cache_output_is_not_a_complete_capture(self):
        body=self.ops.snapshot(True,STOCK_BOOT)
        for changed in [body.replace(b'CACHE battery/batt_vol 3400',b'CACHE battery/batt_vol '),
                        body+b'UNEXPECTED payload\n',body.replace(b'RUN_RAM inherited\n',b'')]:
            with self.subTest(body=changed[-100:]):
                with self.assertRaises(self.m.Refused):self.m.parse_snapshot(changed,True)

    def test_stock_high_cached_voltage_is_preserved_but_never_accepts_charge(self):
        body=self.ops.snapshot(True,STOCK_BOOT).replace(b'batt_vol 3400',b'batt_vol 5399')
        value=self.m.parse_snapshot(body,True)
        self.assertEqual(value['cached_status']['battery/batt_vol'],'5399')
        self.assertEqual(value['charging_status'],'UNKNOWN')

    def real_wrapper_payload(self, payload, phase):
        """Run only builtin printf locally; the production transport is a fake."""
        m = self.m
        class Socket:
            def send(self, raw):
                completed = subprocess.run(
                    ['/bin/sh'], input=bytes(raw), capture_output=True,
                    check=True, timeout=2, env={'PATH': '/nonexistent', 'LC_ALL': 'C'})
                self.response = completed.stdout.replace(b'\n', b'\r\n')
                return len(raw)
            def recv(self, count):
                value, self.response = self.response[:count], self.response[count:]
                return value
            def close(self):
                pass
        body = "printf '%s' '" + payload.decode('ascii').replace("'", "'\\''") + "'"
        with tempfile.TemporaryDirectory() as tmp:
            channel = m.Channel(111, Path(tmp), phase, sock=Socket())
            try:
                with patch.object(m.select, 'select', side_effect=lambda reads, writes, errors, timeout: (reads, writes, [])):
                    return channel.command(body, phase, m.time.monotonic() + 3)
            finally:
                channel.close()

    def test_real_wrapper_removes_only_its_separator_newline(self):
        for payload in (b'', b'hello', b'hello\n', b'hello\n\n', b'\n\n'):
            with self.subTest(payload=payload):
                self.assertEqual(self.real_wrapper_payload(payload, 'fixture'), payload)
        malformed = b'@@I14_TRIAL_BEGIN ' + b'1' * 32 + b' fixture\n@@I14_TRIAL_END ' + b'1' * 32 + b' fixture 0\n'
        with self.assertRaises(self.m.Refused):
            self.m.frame_payload(malformed, '1' * 32, 'fixture')

    def test_real_wrapper_snapshot_and_sha_readback_reach_exact_consumers(self):
        baseline = self.ops.snapshot(False, BOOT)
        captured = self.real_wrapper_payload(baseline, 'baseline')
        self.assertEqual(captured, baseline)
        self.assertEqual(self.m.parse_snapshot(captured)['boot_id'], BOOT)
        readback = ''.join(pin + '  /run/i14-stock-trial/' + name + '\n'
                           for name, (_, pin) in self.m.ASSETS.items()).encode()
        self.assertEqual(self.real_wrapper_payload(readback, 'readback'), readback)

    def test_channel_real_framing_uses_bounded_fake_socket_and_persists_wire(self):
        m=self.m
        class Socket:
            def __init__(self):self.response=b'';self.closed=False
            def send(self,raw):
                text=bytes(raw)
                self.response=(text+b'\r\n@@I14_TRIAL_BEGIN '+channel.token.encode()+b' baseline\r\nhello\r\n\r\n@@I14_TRIAL_END '+channel.token.encode()+b' baseline 0\r\n')
                return len(raw)
            def recv(self,count):
                value,self.response=self.response,b'';return value
            def close(self):self.closed=True
        with tempfile.TemporaryDirectory() as tmp:
            sock=Socket();channel=m.Channel(111,Path(tmp),'test',sock=sock)
            with patch.object(m.select,'select',side_effect=lambda reads,writes,errors,timeout:(reads,writes,[])):
                self.assertEqual(channel.command('true','baseline',m.time.monotonic()+2),b'hello\n')
            channel.close()
            self.assertTrue(sock.closed)
            self.assertIn(b'@@I14_TRIAL_BEGIN',(Path(tmp)/'wire-test.raw').read_bytes())

    def test_asset_server_binding_is_fixed_usb_address_with_fake_socket_thread(self):
        m=self.m
        class Socket:
            bound=None;closed=False
            def bind(self,address):self.bound=address
            def listen(self,*args):pass
            def settimeout(self,*args):pass
            def close(self):self.closed=True
        class Thread:
            def __init__(self,*args,**kwargs):pass
            def start(self):pass
            def join(self,*args,**kwargs):pass
            def is_alive(self):return False
        sock=Socket()
        with patch.object(m.socket,'socket',return_value=sock),patch.object(m.threading,'Thread',Thread):
            with m.AssetServer({'Image':b'x'},'a'*32) as server:server.served.add('Image')
        self.assertEqual(sock.bound,('10.77.14.1',18144));self.assertTrue(sock.closed)

    def test_serial_backend_refuses_non_character_file_before_terminal_io(self):
        m=self.m
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'regular';f.touch();fd=m.os.open(f,m.os.O_RDONLY)
            with patch.object(m.os,'open',return_value=fd),patch.object(m.termios,'tcgetattr',side_effect=AssertionError('no terminal access')):
                with self.assertRaises(m.Refused):m.RealOps().connect(self.root,Path(tmp),'stock','/dev/cu.usbmodemTEST')

    def test_all_asset_uploads_share_one_short_window_deadline(self):
        self.ops=FakeOps(self.m,'upload_deadline')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.m.Refused):self.m.pipeline(self.root,self.ops,Path(tmp),BOOT,self.m.load_assets(self.root),lambda:None)
        self.assertFalse(any(x[0]=='load' for x in self.ops.calls))

    def test_asset_server_finishes_partial_socket_writes_without_a_real_socket(self):
        m=self.m;payload=b'x'*100000;server=m.AssetServer({'Image':payload},'a'*32)
        class Connection:
            def __init__(self):self.request=bytearray(('GET '+'a'*32+' Image\n').encode());self.sent=bytearray()
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,*args):pass
            def recv(self,n):
                result=bytes(self.request[:n]);del self.request[:n];return result
            def send(self,view):
                n=min(17001,len(view));self.sent.extend(view[:n]);return n
        c=Connection()
        class Listener:
            def accept(self):return c,('10.77.14.2',123)
        server.listener=Listener();server.serve()
        self.assertIsNone(server.failure)
        self.assertEqual(c.sent,payload)

    def test_expired_qualification_or_wrong_pmic_proof_never_reaches_boundary(self):
        qpath=self.root/self.m.QUALIFICATION
        for field in ('expiry','pmic'):
            with self.subTest(field=field):
                q=json.loads(qpath.read_text())
                if field=='expiry':q['expires_utc']=(NOW-datetime.timedelta(seconds=1)).isoformat()
                else:
                    proof=self.root/q['pmic_handoff']['path'];value=json.loads(proof.read_text());value['decision']='OBSERVATION_ONLY'
                    q['pmic_handoff']=self.write(q['pmic_handoff']['path'],value)
                ref=self.write(self.m.QUALIFICATION,q);self.package['qualification_sha256']=ref['sha256'];self.write(self.m.PACKAGE,self.package)
                with self.assertRaises(self.m.Refused):self.run_worker()
                self.assertEqual(self.ops.calls,[])
                self.make_release()

    def test_native_shell_guards_storage_before_cached_callbacks(self):
        for variant in ('valid','ro','capacity','missing'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                for p in ['proc','run','sys/class/block']:(root/p).mkdir(parents=True,exist_ok=True)
                (root/'proc/mounts').write_text('rootfs / rootfs rw 0 0\n')
                for name in self.m.RO_NAMES:
                    if variant=='missing' and name=='mmcblk0p23':continue
                    (root/'sys/class/block'/name).mkdir()
                command=self.m.snapshot_command(True)
                for name in ('proc','sys','run'):command=command.replace('/'+name+'/',str(root/name)+'/').replace(' /'+name+' ', ' '+str(root/name)+' ')
                event=root/'cache-callbacks'
                fakes='''
uname() { case "$1" in -r) printf '3.18.19\\n';; -m) printf 'aarch64\\n';; *) return 98;; esac; }
grep() { [ "$1" = -qE ] || return 98; }
sha256sum() { case "$1" in */model) printf 'MODEL  file\\n';; */compatible) printf 'COMPAT  file\\n';; */modules) printf 'EMPTY  file\\n';; *) return 98;; esac; }
cat() {
    case "$1" in
        */size) printf 'CAPACITY\\n';;
        */mmcblk0/ro) printf 'RO\\n';;
        */ro) printf '1\\n';;
        */stat) printf '0 0 0 0 0 0 0 0 0 0 0\\n';;
        */boot_id) printf 'BOOT\\n';;
        */power_supply/*) printf 'called\\n' >>'EVENT'; printf '123\\n';;
        *) return 98;;
    esac
}
'''.replace('MODEL',self.m.STOCK_MODEL[0]).replace('COMPAT',self.m.STOCK_MODEL[1]).replace('EMPTY',hashlib.sha256(b'').hexdigest()).replace('CAPACITY','1' if variant=='capacity' else '30535680').replace("'RO", "'0" if variant=='ro' else "'1").replace('BOOT',STOCK_BOOT).replace('EVENT',str(event))
                result=subprocess.run(['/bin/sh','-c','set -eu;\n'+fakes+'\n'+command],env={'PATH':'/nonexistent'},capture_output=True,text=True)
                if variant=='valid':
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertEqual(len(event.read_text().splitlines()),5)
                else:
                    self.assertNotEqual(result.returncode,0,result.stdout)
                    self.assertFalse(event.exists(),result.stdout)


    def test_custody_and_runtime_grants_are_bound_to_this_boot_and_qualification(self):
        m=self.m
        for variant in ('custody_boot','runtime_boot','runtime_qualification'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                self.root=Path(tmp).resolve()
                for source,blob in self.blobs.items():
                    p=self.root/Path(source).relative_to(ROOT);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(blob)
                self.ops=FakeOps(m);self.make_release()
                if variant=='custody_boot':
                    q=json.loads((self.root/m.QUALIFICATION).read_text())
                    ref=q['custody'];proof=json.loads((self.root/ref['path']).read_text())
                    proof['boot_id']=RETURN_BOOT;q['custody']=self.write(ref['path'],proof)
                    qref=self.write(m.QUALIFICATION,q)
                    self.package['qualification_sha256']=qref['sha256'];self.write(m.PACKAGE,self.package)
                    runtime=json.loads((self.root/m.RUNTIME).read_text());runtime['qualification_sha256']=qref['sha256']
                    self.write(m.RUNTIME,runtime)
                else:
                    runtime=json.loads((self.root/m.RUNTIME).read_text())
                    runtime['boot_id' if variant=='runtime_boot' else 'qualification_sha256']=RETURN_BOOT if variant=='runtime_boot' else '0'*64
                    self.write(m.RUNTIME,runtime)
                with self.assertRaises(m.Refused):self.run_worker()
                self.assertEqual(self.ops.calls,[])
                self.make_release()
