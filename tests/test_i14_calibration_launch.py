"""Host-only checks of C13 packaging, gates and irreversible attempt accounting."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('launch', ROOT / 'scripts/i14-calibration-launch.py')
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)
NOW = 1800000000
BOOT = '11111111-2222-4333-8444-555555555555'


def gate():
    return dict(schema=1, campaign=launch.CAMPAIGN, attempt=1, boot_id=BOOT,
                not_after_epoch=NOW + 72*3600,
                grant=dict(id='item4-c13-test', coordinator_thread_id=launch.COORDINATOR,
                           owner=4, starts_epoch=NOW-10, ends_epoch=NOW+72*3600,
                           receipt_sha256='a'*64, package_sha256='a'*64),
                facts=dict(captured_epoch=NOW, boot_id=BOOT, K_pass=True, K_sha256='b'*64,
                           usb_sha256='c'*64, serial_recovery_sha256='d'*64,
                           runtime_hashes={p: 'e'*64 for p in launch.RUNTIME_PATHS},
                           guard_readbacks=[dict(epoch=NOW-610, valid=True, boot_id=BOOT),
                                            dict(epoch=NOW-10, valid=True, boot_id=BOOT)]),
                supervision=dict(receipt_sha256='f'*64, probe_epoch=NOW-30,
                                 coverage_until_epoch=NOW+72*3600,
                                 next_check_epoch=NOW+1800, continuation_id='c13-one-shot-test'),
                storage=dict(quota_review_sha256='2'*64, retention_review_sha256='3'*64,
                             observer_fixed_bytes=16*1024**2, background_growth_bytes=64*1024**2),
                launch_review_sha256='1'*64, previous_acceptance=None)


class Launch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_combined_storage_reserves_both_cycles_observer_closure_and_fixed_sources(self):
        self.assertEqual(launch.storage_required(gate()), 3120*1024**2)
        for key in ('quota_review_sha256','retention_review_sha256'):
            value=gate(); value['storage'][key]=''
            with self.assertRaises(ValueError): launch.validate_gate(value,NOW)
        for bad in (-1, True, 1.5):
            value=gate(); value['storage']['background_growth_bytes']=bad
            with self.assertRaises(ValueError): launch.validate_gate(value,NOW)

    def test_background_allowance_covers_hard_c13_guard_log_bound_before_other_writers(self):
        value=gate(); value['storage']['background_growth_bytes']=2*3842*512
        launch.validate_gate(value,NOW)
        value['storage']['background_growth_bytes']-=1
        with self.assertRaisesRegex(ValueError,'C13 guard log allowance'):
            launch.validate_gate(value,NOW)

    def test_fixed_observer_overhead_must_be_funded(self):
        value = gate()
        value['storage']['observer_fixed_bytes'] = launch.MIB - 1
        with self.assertRaisesRegex(ValueError, 'observer'):
            launch.validate_gate(value, NOW)

    def test_free_space_must_come_from_the_userdata_backing_device(self):
        with mock.patch.object(launch, 'BASE', self.root), \
             mock.patch.object(Path, 'read_text', autospec=True) as uevent, \
             mock.patch.object(launch.os, 'fstatvfs', return_value=SimpleNamespace(f_bavail=3, f_frsize=4096)):
            uevent.return_value = 'PARTNAME=userdata\n'
            self.assertEqual(launch.userdata_free_bytes(), 12288)
            dev = self.root.stat().st_dev
            uevent.assert_called_once_with(Path(f'/sys/dev/block/{launch.os.major(dev)}:{launch.os.minor(dev)}/uevent'))
            uevent.return_value = 'PARTNAME=other\n'
            with self.assertRaisesRegex(ValueError, 'userdata'):
                launch.userdata_free_bytes()

    def test_record_and_gate_limits_refuse_before_replacing_or_reading(self):
        path=self.root/'record.json'; path.write_text('{}')
        with mock.patch.object(launch,'MAX_RECORD_BYTES',8):
            with self.assertRaisesRegex(ValueError,'record byte cap'):
                launch.write_json(path,{'padding':'x'*20})
        self.assertEqual(path.read_text(),'{}')
        self.assertFalse(path.with_suffix('.tmp').exists())
        path.write_text(' '*257*1024)
        with self.assertRaisesRegex(ValueError,'input byte cap'):
            launch.read_json(path,limit=256*1024)

    def test_checkpoint_receipt_count_cannot_extend_campaign_storage(self):
        value=gate(); state=self.root/'campaign.json'
        launch.reserve_attempt(state,value,NOW,'a'*64)
        start=json.loads(state.read_text())['started_monotonic']
        current=dict(next_check_epoch=NOW+60, acknowledged_epoch=NOW,
                     acknowledged_monotonic=start,deadline_monotonic=start+60,ack_count=80)
        (self.root/'checkpoint.json').write_text(json.dumps(current))
        receipt=dict(campaign=launch.CAMPAIGN,attempt=1,boot_id=BOOT,grant_id=value['grant']['id'],
                     captured_epoch=NOW+60,next_check_epoch=NOW+3600,scheduler_receipt_sha256='6'*64)
        with self.assertRaisesRegex(ValueError,'receipt count cap'):
            launch.acknowledge(self.root,receipt,NOW+60)

    def test_package_is_exact_and_refuses_reusing_output(self):
        bundle = self.root / 'bundle'
        launch.build_bundle(bundle)
        manifest = json.loads((bundle/'package.json').read_text())
        self.assertEqual((bundle/'measure.py').read_bytes(),
                         (ROOT/'scripts/i14-battery-calibrate.py').read_bytes())
        self.assertEqual(manifest['files']['measure.py'], launch.digest(bundle/'measure.py'))
        launch.verify_bundle(bundle)
        (bundle/'measure.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'hash'):
            launch.verify_bundle(bundle)
        with self.assertRaises(FileExistsError):
            launch.build_bundle(bundle)

    def test_rust_restore_selector_is_positive_and_unambiguous(self):
        self.assertTrue(launch.rust_selector('# retained fallback\ni14_impl="rust"\n'))
        for text in ['i14_impl="python"\n','# i14_impl="rust"\n',
                     'i14_impl="rust"\ni14_impl="python"\n','i14_impl=${choice}\n']:
            self.assertFalse(launch.rust_selector(text))

    def test_missing_stale_or_wrong_grant_and_supervision_are_not_launchable(self):
        launch.validate_gate(gate(), NOW)
        for section, field, bad in [('grant','owner',5), ('grant','ends_epoch',NOW+100),
                                    ('supervision','probe_epoch',NOW-901),
                                    ('supervision','next_check_epoch',NOW+3601),
                                    ('supervision','coverage_until_epoch',NOW+3600),
                                    ('facts','captured_epoch',NOW-151),
                                    ('facts','K_pass',False), ('facts','runtime_hashes',{})]:
            value=gate(); value[section][field]=bad
            with self.subTest(section=section,field=field), self.assertRaises(ValueError):
                launch.validate_gate(value, NOW)
        value=gate(); value['facts']['guard_readbacks'][0]['epoch']=NOW-100
        with self.assertRaises(ValueError): launch.validate_gate(value,NOW)
        value=gate(); value['previous_acceptance']={}
        with self.assertRaises(ValueError): launch.validate_gate(value,NOW)

    def test_failed_attempt_stops_campaign_without_replacement(self):
        state=self.root/'campaign.json'; value=gate()
        launch.reserve_attempt(state,value,NOW,'a'*64)
        launch.finish_attempt(state,1,1,None,0,NOW+1)
        value['attempt']=2
        value['previous_acceptance']=dict(verdict='ACCEPTED_CYCLE_AND_RESTORATION',
                                          result_sha256='2'*64, review_sha256='3'*64)
        with self.assertRaisesRegex(ValueError,'stopped'):
            launch.reserve_attempt(state,value,NOW+10,'a'*64)
        value['attempt']=1
        with self.assertRaises(ValueError): launch.reserve_attempt(state,value,NOW+10,'a'*64)
        self.assertEqual(json.loads(state.read_text())['attempts'],1)

    def test_checkpoint_ack_is_bounded_and_cannot_restart_or_reset_campaign(self):
        state=self.root/'campaign.json'; value=gate()
        launch.reserve_attempt(state,value,NOW,'a'*64)
        start=json.loads(state.read_text())['started_monotonic']
        current={'next_check_epoch':NOW+60,'acknowledged_epoch':NOW,
                 'acknowledged_monotonic':start,'deadline_monotonic':start+60}
        (self.root/'checkpoint.json').write_text(json.dumps(current))
        ack=dict(campaign=launch.CAMPAIGN,attempt=1,boot_id=BOOT,grant_id=value['grant']['id'],
                 captured_epoch=NOW+60,next_check_epoch=NOW+3600,scheduler_receipt_sha256='6'*64)
        launch.acknowledge(self.root,ack,NOW+60)
        self.assertEqual(json.loads((self.root/'checkpoint.json').read_text())['next_check_epoch'],NOW+3600)
        record=json.loads(state.read_text())
        self.assertEqual((record['attempts'],record['deadline_epoch']),(1,NOW+72*3600))
        rolled=dict(ack,captured_epoch=NOW+30,next_check_epoch=NOW+2000)
        with self.assertRaisesRegex(ValueError,'clock'):
            launch.acknowledge(self.root,rolled,NOW+30)
        for mutate in [('next_check_epoch',NOW+7200),('boot_id','wrong'),('grant_id','other')]:
            bad=copy.deepcopy(ack);bad[mutate[0]]=mutate[1]
            with self.assertRaises(ValueError): launch.acknowledge(self.root,bad,NOW+61)
        with self.assertRaises(ValueError): launch.acknowledge(self.root,ack,NOW+3901)
        launch.finish_attempt(state,1,1,None,0,NOW+1)
        with self.assertRaises(ValueError): launch.acknowledge(self.root,ack,NOW+62)

    def test_second_attempt_needs_review_frozen_runtime_and_remaining_budget(self):
        state=self.root/'campaign.json'; value=gate()
        launch.reserve_attempt(state,value,NOW,'a'*64)
        result=self.root/'result.json'; result.write_text('{"physical_soc_calibrated":false}')
        launch.finish_attempt(state,1,0,result,0,NOW+1)
        value['attempt']=2
        with self.assertRaises(ValueError): launch.reserve_attempt(state,value,NOW+100,'a'*64)
        value['previous_acceptance']=dict(verdict='ACCEPTED_CYCLE_AND_RESTORATION',
                                          result_sha256=launch.digest(result),review_sha256='3'*64)
        changed=copy.deepcopy(value); changed['facts']['runtime_hashes'][launch.RUNTIME_PATHS[0]]='0'*64
        with self.assertRaises(ValueError): launch.reserve_attempt(state,changed,NOW+100,'a'*64)
        with self.assertRaises(ValueError): launch.reserve_attempt(state,value,NOW+36*3600,'a'*64)
        with self.assertRaisesRegex(ValueError,'four-hour'):
            launch.reserve_attempt(state,value,NOW+5*3600,'a'*64)
        launch.reserve_attempt(state,value,NOW+3600,'a'*64)
        record=json.loads(state.read_text())
        self.assertEqual(record['attempts'],2)
        self.assertEqual(record['deadline_epoch'],NOW+72*3600)
        with self.assertRaises(ValueError): launch.reserve_attempt(state,value,NOW+3601,'a'*64)

    def test_clock_rollback_or_a_different_package_cannot_extend_campaign(self):
        state=self.root/'campaign.json'; value=gate()
        launch.reserve_attempt(state,value,NOW,'a'*64)
        result=self.root/'result.json'; result.write_text('{}')
        launch.finish_attempt(state,1,0,result,0,NOW+1)
        value.update(attempt=2,previous_acceptance=dict(verdict='ACCEPTED_CYCLE_AND_RESTORATION',
                     result_sha256=launch.digest(result),review_sha256='3'*64))
        for epoch,pin in [(NOW-1,'a'*64),(NOW+10,'b'*64)]:
            with self.assertRaises(ValueError): launch.reserve_attempt(state,value,epoch,pin)

    def test_wrapper_restoration_receipt_never_equals_full_restoration_acceptance(self):
        for child_rc,restore_rc,want in [(0,0,'awaiting_review'),(1,0,'stopped'),(0,1,'stopped')]:
            with self.subTest(child_rc=child_rc,restore_rc=restore_rc):
                control=self.root/f'control-{child_rc}-{restore_rc}'
                self.run_wrapper(control,child_rc,restore_rc)
                record=json.loads((control/'campaign.json').read_text())
                receipt=json.loads((control/'wrapper-result-1.json').read_text())
                self.assertEqual(record['state'],want)
                self.assertEqual(record['attempts'],1)
                self.assertFalse(receipt['restoration_accepted'])
                self.assertEqual(receipt['production_start_rc'],restore_rc)
                self.assertEqual(receipt['exit_code'] == 0, want == 'awaiting_review')

    def test_missing_result_cannot_exit_successfully(self):
        state = self.root / 'campaign.json'
        launch.reserve_attempt(state, gate(), NOW, 'a'*64)
        finished = launch.finish_attempt(state, 1, 0, self.root/'result.json', 0, NOW+1)
        self.assertEqual(finished['state'], 'stopped')
        self.assertNotEqual(finished['exit_code'], 0)

    def test_supervision_exception_terminates_verified_child_and_restores_after_reap(self):
        control=self.root/'held'
        with self.assertRaisesRegex(OSError,'wait failed'):
            self.run_wrapper(control,1,0,live=True)
        receipt=json.loads((control/'wrapper-result-1.json').read_text())
        self.assertEqual(self.last_signals,[(123456,launch.signal.SIGTERM)])
        self.assertTrue(receipt['experiment_gone'])
        self.assertEqual(receipt['production_start_rc'],0)
        self.assertEqual(json.loads((control/'campaign.json').read_text())['state'],'stopped')

    def test_missed_checkpoint_stops_measurement_and_cannot_be_a_success(self):
        control=self.root/'missed'
        signals=self.run_wrapper(control,0,0,missed=True)
        self.assertEqual(signals,[(123456,launch.signal.SIGTERM)])
        record=json.loads((control/'campaign.json').read_text())
        self.assertEqual(record['state'],'stopped')
        self.assertEqual(record['attempts'],1)
        self.assertEqual(record['exit_code'],1)

    def test_wall_rollback_after_ack_is_a_terminal_stop(self):
        control=self.root/'rollback'
        signals=self.run_wrapper(control,0,0,scenario='rollback')
        self.assertEqual(signals,[(123456,launch.signal.SIGTERM)])
        self.assertEqual(json.loads((control/'campaign.json').read_text())['state'],'stopped')

    def test_exit0_just_after_checkpoint_grace_is_not_success(self):
        control=self.root/'exit-late'
        self.run_wrapper(control,0,0,scenario='exit-late')
        record=json.loads((control/'campaign.json').read_text())
        self.assertEqual(record['state'],'stopped')
        self.assertEqual(record['exit_code'],1)

    def test_cleanup_clock_failure_is_not_an_outer_success(self):
        control=self.root/'cleanup-clock'
        self.run_wrapper(control,0,0,scenario='cleanup-rollback')
        self.assertEqual(json.loads((control/'campaign.json').read_text())['exit_code'],1)
        self.assertEqual(json.loads((control/'wrapper-result-1.json').read_text())['exit_code'],1)

    def test_exception_escalates_only_verified_pid_to_kill_and_reaps(self):
        control=self.root/'needs-kill'
        with self.assertRaisesRegex(OSError,'wait failed'):
            self.run_wrapper(control,1,0,scenario='needs-kill')
        self.assertEqual(self.last_signals,[(123456,launch.signal.SIGTERM),(123456,launch.signal.SIGKILL)])
        receipt=json.loads((control/'wrapper-result-1.json').read_text())
        self.assertTrue(receipt['experiment_gone'])
        self.assertEqual(receipt['production_start_rc'],0)

    def test_unreapable_child_is_an_explicit_owned_hold_not_restored_or_retryable(self):
        control=self.root/'unreapable'
        self.run_wrapper(control,1,0,scenario='unreapable')
        self.assertEqual(self.last_signals,[(123456,launch.signal.SIGKILL)])
        record=json.loads((control/'campaign.json').read_text())
        receipt=json.loads((control/'wrapper-result-1.json').read_text())
        self.assertEqual(record['state'],'stopping')
        self.assertEqual(record['attempts'],1)
        self.assertFalse(receipt['experiment_gone'])
        self.assertIsNone(receipt['production_start_rc'])

    def test_reaped_timeout_and_missing_pid_do_not_override_a_live_measurement_lock(self):
        control = self.root / 'lock-held'
        with (self.root/'measurement.lock').open('a') as lock:
            launch.fcntl.flock(lock, launch.fcntl.LOCK_EX)
            with self.assertRaises(BlockingIOError):
                self.run_wrapper(control, 0, 0)
        record = json.loads((control/'campaign.json').read_text())
        self.assertEqual(record['state'], 'stopping')
        self.assertIsNone(record['production_start_rc'])
        self.assertNotEqual(record['exit_code'], 0)

    def test_userdata_at_reserve_stops_even_if_child_returns_zero(self):
        control = self.root / 'space-floor'
        self.run_wrapper(control, 0, 0, scenario='space-floor')
        record = json.loads((control/'campaign.json').read_text())
        self.assertEqual(record['state'], 'stopped')
        self.assertNotEqual(record['exit_code'], 0)

    def run_wrapper(self,control,child_rc,restore_rc,live=False,missed=False,scenario=None):
        bundle=self.root/('bundle-'+control.name)
        launch.build_bundle(bundle)
        value=gate();value['grant']['package_sha256']=launch.digest(bundle/'package.json')
        if scenario=='exit-late': value['supervision']['next_check_epoch']=NOW+1
        (bundle/'gate-1.json').write_text(json.dumps(value))
        base=self.root/('data-'+control.name); base.mkdir()
        data=base/f'battery-calibration-{launch.CAMPAIGN}-attempt-1'
        waits=[0]; signals=[];done=[False];clock={'wall':NOW,'mono':1000.0}
        self.last_signals=signals
        # Only device/external process boundaries are replaced; real ledger/receipts run.
        class Child:
            def wait(self,timeout):
                waits[0]+=1
                if live and not signals: raise OSError('wait failed')
                if scenario=='unreapable':
                    clock.update(wall=NOW+launch.RUN_SECONDS+36,mono=1000+launch.RUN_SECONDS+36)
                    raise launch.subprocess.TimeoutExpired('fixture',timeout)
                if scenario=='needs-kill':
                    clock.update(wall=NOW+waits[0]*30,mono=1000+waits[0]*30)
                    if waits[0]==1: raise OSError('wait failed')
                    if not any(sig==launch.signal.SIGKILL for _,sig in signals):
                        raise launch.subprocess.TimeoutExpired('fixture',timeout)
                if scenario=='rollback':
                    clock.update(wall=NOW+{1:30,2:10,3:11}[waits[0]],mono=1000+waits[0]*30)
                    if waits[0]==1:
                        ack=dict(campaign=launch.CAMPAIGN,attempt=1,boot_id=BOOT,grant_id=value['grant']['id'],
                                 captured_epoch=NOW+30,next_check_epoch=NOW+3600,scheduler_receipt_sha256='6'*64)
                        launch.acknowledge(control,ack,NOW+30)
                    if waits[0]<3: raise launch.subprocess.TimeoutExpired('fixture',timeout)
                if scenario=='exit-late':
                    elapsed=min(waits[0]*30,302)
                    clock.update(wall=NOW+elapsed,mono=1000+elapsed)
                    if waits[0]<11: raise launch.subprocess.TimeoutExpired('fixture',timeout)
                if missed: clock.update(wall=NOW+4000,mono=5000)
                if missed and waits[0]==1: raise launch.subprocess.TimeoutExpired('fixture',timeout)
                done[0]=True
                return child_rc
            def poll(self): return child_rc if done[0] else None
        def spawn(*args,**kwargs):
            data.mkdir()
            (data/'result.json').write_text('{"physical_soc_calibrated":false}')
            return Child()
        def restore(*args):
            if scenario=='cleanup-rollback': clock['wall']=NOW-1
            return restore_rc
        with mock.patch.object(launch,'__file__',str(bundle/'launcher.py')), \
             mock.patch.object(launch,'CODE',bundle),mock.patch.object(launch,'CONTROL',control), \
             mock.patch.object(launch,'BASE',base), \
             mock.patch.object(launch,'MEASUREMENT_LOCK',self.root/'measurement.lock'), \
             mock.patch.object(launch,'live_preflight',return_value={'boot_id':BOOT}), \
             mock.patch.object(launch.time,'time',side_effect=lambda:clock['wall']), \
             mock.patch.object(launch.time,'monotonic',side_effect=lambda:clock['mono']), \
             mock.patch.object(launch.subprocess,'Popen',side_effect=spawn), \
             mock.patch.object(launch,'userdata_free_bytes',return_value=launch.RESERVE_BYTES +
                               (0 if scenario == 'space-floor' else 1)), \
             mock.patch.object(launch,'measurement_pid',side_effect=lambda *a:123456 if not done[0] else None), \
             mock.patch.object(launch.os,'kill',side_effect=lambda *args:signals.append(args)), \
             mock.patch.object(launch,'service',side_effect=restore):
            launch.run_gate(bundle/'gate-1.json')
        return signals


if __name__=='__main__': unittest.main()
