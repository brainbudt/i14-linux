"""Host-only owner gates: no phone, services or settings touched."""
import importlib.util
import stat
from pathlib import Path
import unittest
import json
import io
from contextlib import redirect_stdout
import tempfile
from types import SimpleNamespace
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'scripts/i14-h1-owner.py'
if SOURCE.is_file():
    spec=importlib.util.spec_from_file_location('h1_owner',SOURCE)
    owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(owner)
else:
    owner=None

class OwnerTests(unittest.TestCase):
    def test_binding_rejects_duplicate_child_wrong_parent_and_exe(self):
        self.assertIsNotNone(owner)
        sup=dict(pid=40,ppid=1,exe='/sbin/supervise-daemon',argv=['supervise-daemon','i14-ui','/usr/libexec/i14-rs/i14-ui'])
        kid=dict(pid=42,ppid=40,exe='/usr/libexec/i14-rs/i14-ui',argv=['/usr/libexec/i14-rs/i14-ui'],start_ticks=10)
        self.assertEqual(owner.bind([sup,kid],'ui','rust')['child'],kid)
        for rows in ([sup,kid,kid],[sup,dict(kid,ppid=99)],[sup,dict(kid,exe='/other')]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):owner.bind(rows,'ui','rust')

    def test_new_UI_readiness_precedes_idle_and_same_identity_is_required(self):
        self.assertIsNotNone(owner)
        row=dict(child=dict(pid=42,start_ticks=9))
        calls=[]
        self.assertEqual(owner.ui_ready(lambda: row,lambda: calls.append('on')),row)
        self.assertEqual(calls,['on'])
        with self.assertRaises(ValueError):
            owner.ui_ready(mock.Mock(side_effect=[row,dict(child=dict(pid=43,start_ticks=10))]),lambda:None)

    def test_BCCT_release_allows_disabled_slot_value_and_checks_state(self):
        self.assertTrue(owner.bcct_matches('curr_limit 65535\nmtk-cl-bcct00 -1 mA, state 0\n',False))
        self.assertTrue(owner.bcct_matches('curr_limit 1\nmtk-cl-bcct00 1 mA, state 1\n',True))
        self.assertFalse(owner.bcct_matches('curr_limit 1\nmtk-cl-bcct00 1 mA, state 0\n',True))
        self.assertFalse(owner.bcct_matches('',False))

    def test_real_select_calls_on_readiness_before_returning_new_UI(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);config=tmp/'conf';config.write_text('i14_impl="rust"\n')
            binary=tmp/'ui';binary.write_bytes(b'fixture')
            freeze=tmp/'freeze';freeze.write_text('{}')
            before=dict(child=dict(pid=42),supervisor=dict(pid=40))
            after=dict(child=dict(pid=43),supervisor=dict(pid=41),entrypoint='/usr/sbin/i14-ui')
            instance=owner.Owner.__new__(owner.Owner)
            instance.ready=mock.Mock()
            instance.identify=mock.Mock(side_effect=[before,after,after,after])
            instance.h1=SimpleNamespace(light=lambda:1)
            instance.ctl=SimpleNamespace(processes=lambda:[],await_backlight_on=mock.Mock())
            instance.freeze={'sha256':{}}
            def path(value):
                return config if str(value)=='/etc/conf.d/i14-ui' else binary
            with mock.patch.object(owner,'Path',side_effect=path),mock.patch.object(owner,'FREEZE',freeze),mock.patch.object(owner,'command'):
                actual=instance.select('ui','python')
            instance.ctl.await_backlight_on.assert_called_once_with(instance.h1.light)
            self.assertEqual(actual['child']['pid'],43)
            self.assertEqual(config.read_text(),'i14_impl="python"\n')

    def test_permit_renewal_rechecks_readiness_and_changes_only_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            permit=Path(tmp)/'permit';original={'boot_id':'a','e_closed':True,'expires_uptime_s':10}
            permit.write_text(json.dumps(original))
            instance=owner.Owner.__new__(owner.Owner);instance.ready=mock.Mock()
            instance.h1=SimpleNamespace(PERMIT=permit,uptime=lambda:100)
            instance.renew()
            instance.ready.assert_called_once()
            self.assertEqual(json.loads(permit.read_text()),dict(original,expires_uptime_s=1000))

    def test_real_power_select_warms_new_guard_before_final_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);config=tmp/'conf';config.write_text('i14_impl="rust"\n')
            binary=tmp/'guard';binary.write_bytes(b'fixture')
            status=tmp/'status';status.write_text(json.dumps(dict(boot_id='B',stopped=True,valid=False,level=None,enabled=False,error=None)))
            before=dict(child=dict(pid=42),supervisor=dict(pid=40))
            after=dict(child=dict(pid=43,start_ticks=1000),supervisor=dict(pid=41),entrypoint='/usr/sbin/i14-battery-guard')
            instance=owner.Owner.__new__(owner.Owner);events=[]
            instance.ready=lambda:events.append('ready')
            instance.identify=mock.Mock(side_effect=[before,after,after])
            instance.ctl=SimpleNamespace(processes=lambda:[])
            instance.checked_guard=mock.Mock();instance.freeze={'boot_id':'B'}
            def warmed(*args):events.append('warmup');return {'valid':True}
            def path(value):return {'/etc/conf.d/i14-power':config,'/run/i14-charge-status.json':status}.get(str(value),binary)
            def published(*args):events.append('publication');return {'drain':False}
            with mock.patch.object(owner,'Path',side_effect=path),mock.patch.object(owner,'command'),mock.patch.object(owner,'await_guard',side_effect=warmed),mock.patch.object(owner,'await_publication',side_effect=published):
                instance.select('power','python')
            self.assertEqual(events,['ready','publication','warmup','ready'])

    def test_real_preflight_shutdown_to_first_publication_to_valid_tick(self):
        spec=importlib.util.spec_from_file_location('h1_owner_shutdown',ROOT/'scripts/i14-h1.py')
        h1=importlib.util.module_from_spec(spec);spec.loader.exec_module(h1)
        now=[0.0]
        shutdown=dict(boot_id='B',updated_monotonic=9,stopped=True,valid=False,level=None,enabled=False,error=None)
        empty=dict(boot_id='B',updated_monotonic=11,valid=False,level=None,raw_level=None,enabled=True,limit=80,error=None,drain=False)
        valid=dict(empty,updated_monotonic=71,valid=True,level=74,raw_level=74)
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);proc=tmp/'proc';proc.mkdir();data=tmp/'data';data.mkdir()
            status=tmp/'status';boot=tmp/'boot';boot.write_text('B')
            def read_raw():
                value=shutdown if now[0]<1 else (empty if now[0]<61 else valid)
                raw=json.dumps(value);status.write_text(raw);return raw
            def path(value):
                return {'/proc':proc,'/run/i14-charge-status.json':status,'/proc/sys/kernel/random/boot_id':boot,'/run/i14-battery-calibration.lock':tmp/'absent-lock'}.get(str(value),Path(value))
            permit=SimpleNamespace(lstat=lambda:SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_uid=0),read_text=lambda:json.dumps(dict(boot_id='B',expires_uptime_s=900,e_closed=True,r_closed=True,drain_active=False,soak_active=False)))
            with mock.patch.object(h1,'Path',side_effect=path),mock.patch.object(h1,'PERMIT',permit),mock.patch.object(h1,'DATA',data),mock.patch.object(h1,'uptime',side_effect=lambda:now[0]+10),mock.patch.object(h1.platform,'system',return_value='Linux'),mock.patch.object(h1.platform,'machine',return_value='aarch64'),mock.patch.object(h1.platform,'release',return_value='3.18.19'),mock.patch.object(h1.os,'geteuid',return_value=0),mock.patch.object(h1.os.path,'lexists',return_value=False):
                previous=read_raw()
                with self.assertRaises(ValueError):h1.preflight()
                owner.await_publication(read_raw,previous,10,'B',lambda:now[0],lambda dt:now.__setitem__(0,now[0]+dt))
                self.assertGreaterEqual(now[0],1)
                def checked():
                    raw=read_raw();self.assertEqual(h1.preflight(),'B');return json.loads(raw)
                result=owner.await_guard(checked,10,lambda:now[0],lambda dt:now.__setitem__(0,now[0]+dt))
                self.assertTrue(result['valid']);self.assertGreaterEqual(now[0],61)

    def test_publication_barrier_refuses_timeout_or_changed_unsafe_status(self):
        old=json.dumps(dict(boot_id='B',updated_monotonic=9,stopped=True,valid=False,level=None,enabled=False,error=None))
        now=[0.0]
        with self.assertRaises(ValueError):
            owner.await_publication(lambda:old,old,10,'B',lambda:now[0],lambda dt:now.__setitem__(0,now[0]+dt))
        self.assertGreaterEqual(now[0],15)
        bad=json.dumps(dict(boot_id='B',updated_monotonic=11,valid=False,error='failure',drain=False))
        with self.assertRaises(ValueError):owner.await_publication(lambda:bad,old,10,'B')

    def test_guard_warmup_accepts_only_new_valid_publication_without_relaxing_errors(self):
        now=[0.0]
        first=dict(updated_monotonic=10,valid=False,level=None,raw_level=None,enabled=True,limit=80)
        final=dict(first,updated_monotonic=70,valid=True,level=74,raw_level=74)
        def read():return first if now[0]<60 else final
        result=owner.await_guard(read,10,lambda:now[0],lambda s:now.__setitem__(0,now[0]+s))
        self.assertTrue(result['valid']);self.assertEqual(now[0],60)
        with self.assertRaisesRegex(ValueError,'genuine error'):
            owner.await_guard(mock.Mock(side_effect=ValueError('genuine error')),10)
        now[0]=0
        with self.assertRaises(ValueError):
            owner.await_guard(lambda:first,10,lambda:now[0],lambda s:now.__setitem__(0,now[0]+s))

    def test_timeout_preserves_child_frames_and_existing_result_before_raising(self):
        error=owner.subprocess.TimeoutExpired(['python3','payload'],1,output=b'partial',stderr=b'{"frames":[{"function":"idle_wait","line":77}]}')
        for operation in ('payload','command'):
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'result';path.write_text('{"partial":true}')
                output=io.StringIO()
                with mock.patch.object(owner.subprocess,'run',side_effect=error),redirect_stdout(output),self.assertRaises(owner.subprocess.TimeoutExpired):
                    if operation=='payload':owner.payload(['python3','payload'],path,{})
                    else:owner.command(['rc-service','status'])
                trace=json.loads(output.getvalue());self.assertTrue(trace['timeout']);self.assertIn('idle_wait',trace['stderr']);self.assertEqual(trace['stdout'],'partial')
                if operation=='payload':self.assertEqual(trace['result'],{'partial':True})

    def test_idle_always_requests_fresh_process_for_both_implementations(self):
        instance=owner.Owner.__new__(owner.Owner);instance.h1=SimpleNamespace(light=lambda:0)
        instance.ready=mock.Mock();instance.select=mock.Mock(return_value={'child':{'pid':42,'start_ticks':912}})
        with mock.patch.object(owner,'payload') as measure:
            instance.idle('ui','rust',Path('/run/h1-idle.json'))
        instance.select.assert_called_once_with('ui','rust',restart=True)
        argv = measure.call_args.args[0]
        self.assertEqual(argv[argv.index('--start-ticks') + 1], '912')

    def test_measurement_failure_stops_after_logging_payload_status(self):
        self.assertIsNotNone(owner)
        with mock.patch.object(owner.subprocess,'run',return_value=mock.Mock(returncode=2,stdout=b'',stderr=b'ValueError')):
            with self.assertRaises(ValueError):
                owner.payload(['python3','payload'],None,{})

if __name__=='__main__':unittest.main()
