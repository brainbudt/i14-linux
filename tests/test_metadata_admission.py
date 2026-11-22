import copy
import importlib.util
from pathlib import Path
import sys
import unittest


PATH = Path(__file__).resolve().parents[1] / 'scripts/i14_metadata_admission.py'
if PATH.exists():
    spec = importlib.util.spec_from_file_location('i14_metadata_admission', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
else:
    module = None


def complete_declarations():
    # Synthetic declarations only. No fixture is authenticated runtime evidence.
    pins = {name: 'a' * 64 for name in (
        'r1_final_state', 'r1_final_journal', 'r2_lineage', 'log_attempt',
        'log_result', 'log_wire', 'log_stream', 'legacy_pmic_receipts', 'legacy_pmic_stop')}
    facts = {name: True for name in (
        'history_qualified', 'sole_actor_verified', 'custody_held', 'worker_absent',
        'pending_absent', 'recovery_absent', 'all_stops_absent', 'source_pins_match',
        'snapshot_still_current', 'native_lock_original', 'fixed_payload_reviewed',
        'transport_reviewed', 'integration_reviewed', 'source_bindable')}
    return {
        'schema': 1, 'action': 'post_log_metadata_once', 'captured_ns': 100000000000,
        'once': {'namespace': 'artifacts/i14-post-log-metadata-once', 'status': 'FRESH'},
        'history': {'log_completion': 'UNKNOWN', 'pmic_outcome': 'UNKNOWN',
                    'pmic_reset': 'UNKNOWN', 'global_block': 'INTEGRITY_BLOCK',
                    'legacy_once': 'CONSUMED', 'before': pins, 'after': copy.deepcopy(pins)},
        'bindings': {'epoch': 'r3', 'cursor': 76, 'state_sha256': 'b' * 64,
                     'journal_sha256': 'c' * 64, 'package_sha256': 'd' * 64,
                     'process_expected': 'e' * 64, 'process_observed': 'e' * 64,
                     'lock_expected': [2, 123], 'lock_observed': [2, 123],
                     'command_sha256': 'f' * 64, 'worker_sha256': '1' * 64,
                     'transport_sha256': '2' * 64},
        'ownership': facts,
        'bootstrap': {'kind': 'independent_preexisting_ap_exclusion', 'qualified': True,
                      'before_new_intent': True, 'epoch': 'r3', 'cursor': 76,
                      'state_sha256': 'b' * 64, 'old_session_sha256': 'a' * 64,
                      'producer_sha256': '3' * 64},
        'identity': {'idVendor': 7531, 'idProduct': 260, 'serial': 'I14_NATIVE_INIT',
                     'location': 34668544, 'source': '10.77.14.1', 'target': '10.77.14.2'},
        'profile': {'name': 'ap_ram_metadata_v1', 'cache_getters': []},
    }


class MetadataAdmissionTests(unittest.TestCase):
    def assess(self, snapshot, now_ns=101000000000):
        self.assertIsNotNone(module, 'pure metadata admission module is not implemented')
        return module.assess(snapshot, now_ns=now_ns)

    def assert_inert(self, result):
        for name in ('execution_authorized', 'dispatch_allowed', 'module_load_allowed',
                     'first_pmic_read_allowed', 'ready_for_charging', 'hardware_go',
                     'live_state_inspected', 'operational_admission'):
            self.assertIs(result[name], False, name)
        self.assertEqual(result['charging_status'], 'UNKNOWN')
        self.assertEqual(result['pmic_reset'], 'UNKNOWN')

    def test_complete_declarations_never_authorize_execution(self):
        result = self.assess(complete_declarations())
        self.assertEqual(result['status'], 'CONTRACT_DECLARATIONS_COMPLETE')
        self.assertEqual(result['missing_prerequisites'], [])
        self.assertEqual(result['evidence_scope'], 'DECLARED_LOCAL_SNAPSHOT_NOT_ATTESTATION')
        self.assert_inert(result)

    def test_modeled_actual_partial_log_case_is_no_go(self):
        data = complete_declarations()
        data['bootstrap'].update(kind='UNPROVEN', qualified=False, before_new_intent=False)
        for key in ('history_qualified', 'custody_held', 'integration_reviewed', 'source_bindable'):
            data['ownership'][key] = False
        result = self.assess(data)
        self.assertEqual(result['status'], 'NO_GO')
        self.assertIn('AP_EXCLUSION_UNPROVEN', result['missing_prerequisites'])
        self.assertIn('CUSTODY_HELD_UNPROVEN', result['missing_prerequisites'])
        self.assert_inert(result)

    def test_proposed_transaction_boot_and_host_only_witnesses_are_rejected(self):
        for kind in ('proposed_transaction', 'new_usb_window', 'host_pid_absent',
                     'host_flock_free', 'elapsed_deadline', 'safe_observation_exception'):
            with self.subTest(kind=kind):
                data = complete_declarations(); data['bootstrap']['kind'] = kind
                result = self.assess(data)
                self.assertEqual(result['status'], 'NO_GO')
                self.assertIn('AP_EXCLUSION_UNPROVEN', result['missing_prerequisites'])

    def test_prior_native_single_owner_lease_is_only_a_declared_contract_case(self):
        data = complete_declarations(); data['bootstrap']['kind'] = 'native_single_owner_lease'
        result = self.assess(data)
        self.assertEqual(result['status'], 'CONTRACT_DECLARATIONS_COMPLETE')
        self.assert_inert(result)

    def test_witness_must_precede_new_intent(self):
        data = complete_declarations(); data['bootstrap']['before_new_intent'] = False
        self.assertIn('AP_EXCLUSION_UNPROVEN', self.assess(data)['missing_prerequisites'])

    def test_witness_must_match_epoch_cursor_state_and_old_session(self):
        for field, value in [('epoch', 'r4'), ('cursor', 77), ('state_sha256', '4' * 64),
                             ('old_session_sha256', '5' * 64), ('producer_sha256', '')]:
            with self.subTest(field=field):
                data = complete_declarations(); data['bootstrap'][field] = value
                self.assertIn('AP_EXCLUSION_UNPROVEN', self.assess(data)['missing_prerequisites'])

    def test_consumed_or_uncertain_once_cannot_be_replenished_by_new_epoch(self):
        for status in ('RESERVED', 'SENT', 'UNKNOWN', 'PARTIAL', 'COMPLETE', 'CONSUMED', ''):
            for epoch in ('r3', 'r4'):
                with self.subTest(status=status, epoch=epoch):
                    data = complete_declarations(); data['once']['status'] = status
                    data['bindings']['epoch'] = data['bootstrap']['epoch'] = epoch
                    result = self.assess(data)
                    self.assertIn('METADATA_ONCE_NOT_FRESH', result['missing_prerequisites'])
                    self.assert_inert(result)

    def test_action_or_reservation_alias_is_not_a_new_allowance(self):
        for section, key, value in [('once', 'namespace', 'new-epoch/once'),
                                    (None, 'action', 'cached_baseline'),
                                    (None, 'action', 'window_log_capture')]:
            with self.subTest(value=value):
                data = complete_declarations()
                (data if section is None else data[section])[key] = value
                self.assertEqual(self.assess(data)['status'], 'NO_GO')

    def test_no_history_status_can_be_cleared(self):
        for key, value in [('log_completion', 'COMPLETE'), ('pmic_outcome', 'SAFE'),
                           ('pmic_reset', 'COLD_RESET'), ('global_block', ''),
                           ('legacy_once', 'FRESH')]:
            with self.subTest(key=key):
                data = complete_declarations(); data['history'][key] = value
                result = self.assess(data)
                self.assertIn('HISTORY_NOT_RETAINED', result['missing_prerequisites'])
                self.assert_inert(result)

    def test_changed_missing_or_renamed_historical_pin_refuses(self):
        for mode in ('changed', 'missing', 'renamed', 'invalid'):
            with self.subTest(mode=mode):
                data = complete_declarations(); after = data['history']['after']
                if mode == 'changed': after['log_wire'] = '6' * 64
                elif mode == 'missing': del after['legacy_pmic_stop']
                elif mode == 'renamed': after['alias_stop'] = after.pop('legacy_pmic_stop')
                else: after['log_wire'] = 'not-a-digest'
                self.assertIn('HISTORY_BINDINGS_CHANGED', self.assess(data)['missing_prerequisites'])

    def test_each_missing_ownership_precondition_refuses(self):
        for key in complete_declarations()['ownership']:
            with self.subTest(key=key):
                data = complete_declarations(); data['ownership'][key] = False
                self.assertIn(key.upper() + '_UNPROVEN', self.assess(data)['missing_prerequisites'])

    def test_owner_process_or_lock_inode_substitution_refuses(self):
        for key, value in [('process_observed', '7' * 64), ('lock_observed', [2, 124])]:
            with self.subTest(key=key):
                data = complete_declarations(); data['bindings'][key] = value
                self.assertIn('OWNERSHIP_BINDING_MISMATCH', self.assess(data)['missing_prerequisites'])

    def test_binding_hashes_and_cursor_types_are_strict(self):
        for key, value in [('cursor', True), ('cursor', -1), ('state_sha256', 'A' * 64),
                           ('worker_sha256', ''), ('lock_expected', [True, 123])]:
            with self.subTest(key=key):
                data = complete_declarations(); data['bindings'][key] = value
                self.assertEqual(self.assess(data)['status'], 'NO_GO')

    def test_snapshot_time_is_exact_integer_fresh_and_not_future(self):
        for captured, now in [(100000000000, 105000000001), (102000000000, 101000000000),
                              (True, 101000000000), (1.0, 101000000000),
                              (100000000000, float('inf')), (100000000000, float('nan')),
                              (100000000000, 1 << 80)]:
            with self.subTest(captured=captured, now=now):
                data = complete_declarations(); data['captured_ns'] = captured
                self.assertIn('SNAPSHOT_TIME_INVALID_OR_STALE', self.assess(data, now)['missing_prerequisites'])

    def test_exact_freshness_boundary_is_accepted_only_as_declarations(self):
        result = self.assess(complete_declarations(), 105000000000)
        self.assertEqual(result['status'], 'CONTRACT_DECLARATIONS_COMPLETE')
        self.assert_inert(result)

    def test_usb_target_source_identity_mismatch_refuses(self):
        for key, value in [('serial', 'OTHER'), ('location', 34668545), ('idVendor', True),
                           ('source', '10.77.14.2'), ('target', '192.0.2.1')]:
            with self.subTest(key=key):
                data = complete_declarations(); data['identity'][key] = value
                self.assertIn('TARGET_OR_SOURCE_MISMATCH', self.assess(data)['missing_prerequisites'])

    def test_unsafe_getter_or_profile_cannot_be_selected(self):
        for getters in (['charger_state'], ['battery'], ['battery_sample'], ['/dev/mem']):
            with self.subTest(getters=getters):
                data = complete_declarations(); data['profile']['cache_getters'] = getters
                self.assertIn('PAYLOAD_PROFILE_NOT_QUALIFIED', self.assess(data)['missing_prerequisites'])
        data = complete_declarations(); data['profile']['name'] = 'arbitrary_shell'
        self.assertIn('PAYLOAD_PROFILE_NOT_QUALIFIED', self.assess(data)['missing_prerequisites'])

    def test_schema_extra_fields_and_truthy_values_refuse(self):
        cases = []
        data = complete_declarations(); data['output_dir'] = 'new-epoch'; cases.append(data)
        data = complete_declarations(); del data['once']; cases.append(data)
        data = complete_declarations(); data['schema'] = True; cases.append(data)
        data = complete_declarations(); data['ownership']['custody_held'] = 1; cases.append(data)
        data = complete_declarations(); data['bootstrap']['qualified'] = 'yes'; cases.append(data)
        cases += [None, [], 'command']
        for data in cases:
            with self.subTest(data_type=type(data).__name__):
                result = self.assess(data)
                self.assertEqual(result['status'], 'NO_GO'); self.assert_inert(result)

    def test_function_does_not_mutate_declared_history_or_once(self):
        data = complete_declarations(); before = copy.deepcopy(data)
        self.assess(data)
        self.assertEqual(data, before)

    def test_real_assessment_has_no_io_process_or_network_boundary(self):
        active = [False]
        def audit(event, args):
            if active[0] and (event == 'open' or event.startswith(('os.', 'socket.', 'subprocess.', 'shutil.'))):
                raise AssertionError('unexpected side effect: ' + event)
        sys.addaudithook(audit)
        data = complete_declarations()
        active[0] = True
        try:
            result = self.assess(data)
        finally:
            active[0] = False
        self.assert_inert(result)


if __name__ == '__main__':
    unittest.main()
