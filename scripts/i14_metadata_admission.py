"""Pure offline contract check. Declared facts never grant execution rights.

No live evidence is collected or authenticated here. Future integration needs its
own reviewed collector, global reservation and ownership transition. This module
is deliberately disconnected from every worker and the active supervisor.
"""
import re


ACTION = 'post_log_metadata_once'
NAMESPACE = 'artifacts/i14-post-log-metadata-once'
PROFILE = 'ap_ram_metadata_v1'
MAX_AGE_NS = 5_000_000_000
MAX_NS = (1 << 63) - 1
TOP = frozenset(('schema', 'action', 'captured_ns', 'once', 'history', 'bindings',
                 'ownership', 'bootstrap', 'identity', 'profile'))
HISTORY_STATUS = {'log_completion': 'UNKNOWN', 'pmic_outcome': 'UNKNOWN',
                  'pmic_reset': 'UNKNOWN', 'global_block': 'INTEGRITY_BLOCK',
                  'legacy_once': 'CONSUMED'}
HISTORY_ROLES = frozenset(('r1_final_state', 'r1_final_journal', 'r2_lineage',
                          'log_attempt', 'log_result', 'log_wire', 'log_stream',
                          'legacy_pmic_receipts', 'legacy_pmic_stop'))
FACTS = frozenset(('history_qualified', 'sole_actor_verified', 'custody_held',
                   'worker_absent', 'pending_absent', 'recovery_absent',
                   'all_stops_absent', 'source_pins_match', 'snapshot_still_current',
                   'native_lock_original', 'fixed_payload_reviewed',
                   'transport_reviewed', 'integration_reviewed', 'source_bindable'))
HASH_BINDINGS = frozenset(('state_sha256', 'journal_sha256', 'package_sha256',
                           'process_expected', 'process_observed', 'command_sha256',
                           'worker_sha256', 'transport_sha256'))
BINDINGS = HASH_BINDINGS | {'epoch', 'cursor', 'lock_expected', 'lock_observed'}
BOOTSTRAP = frozenset(('kind', 'qualified', 'before_new_intent', 'epoch', 'cursor',
                       'state_sha256', 'old_session_sha256', 'producer_sha256'))
TARGET = {'idVendor': 7531, 'idProduct': 260, 'serial': 'I14_NATIVE_INIT',
          'location': 34668544, 'source': '10.77.14.1', 'target': '10.77.14.2'}


def _fields(value, names):
    return (type(value) is dict and all(type(k) is str for k in value)
            and value.keys() == names)


def _digest(value):
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _integer(value):
    return type(value) is int and 0 <= value <= MAX_NS


def _lock(value):
    return (type(value) is list and len(value) == 2
            and all(_integer(v) for v in value) and value[1] > 0)


def _result(missing):
    return {
        'schema': 1,
        'status': 'NO_GO' if missing else 'CONTRACT_DECLARATIONS_COMPLETE',
        'missing_prerequisites': missing,
        'evidence_scope': 'DECLARED_LOCAL_SNAPSHOT_NOT_ATTESTATION',
        'operational_blockers': ['OFFLINE_ONLY_NO_EXECUTOR', 'NO_LIVE_ATTESTATION',
                                 'ACTIVE_ALLOWLIST_NOT_CHANGED'],
        'execution_authorized': False, 'dispatch_allowed': False,
        'module_load_allowed': False, 'first_pmic_read_allowed': False,
        'ready_for_charging': False, 'hardware_go': False,
        'live_state_inspected': False, 'operational_admission': False,
        'charging_status': 'UNKNOWN', 'pmic_reset': 'UNKNOWN',
        'retained_history': dict(HISTORY_STATUS),
        'once_namespace': NAMESPACE,
        'once_policy': 'EXISTING_ATTEMPT_IS_NEVER_REPLENISHED',
    }


def assess(snapshot, *, now_ns):
    """Check declarations only; every operational capability stays false."""
    missing = []
    if not _fields(snapshot, TOP):
        return _result(['INVALID_SNAPSHOT_SCHEMA'])
    if type(snapshot['schema']) is not int or snapshot['schema'] != 1:
        missing.append('INVALID_SNAPSHOT_SCHEMA')
    if type(snapshot['action']) is not str or snapshot['action'] != ACTION:
        missing.append('ACTION_NOT_QUALIFIED')
    captured = snapshot['captured_ns']
    if not (_integer(captured) and _integer(now_ns)
            and 0 <= now_ns - captured <= MAX_AGE_NS):
        missing.append('SNAPSHOT_TIME_INVALID_OR_STALE')

    once = snapshot['once']
    if not _fields(once, {'namespace', 'status'}):
        missing.append('METADATA_ONCE_NOT_FRESH')
    else:
        if type(once['namespace']) is not str or once['namespace'] != NAMESPACE:
            missing.append('METADATA_ONCE_NAMESPACE_MISMATCH')
        if type(once['status']) is not str or once['status'] != 'FRESH':
            missing.append('METADATA_ONCE_NOT_FRESH')

    history = snapshot['history']
    hshape = _fields(history, HISTORY_STATUS.keys() | {'before', 'after'})
    retained = hshape and all(type(history[k]) is str and history[k] == v
                              for k, v in HISTORY_STATUS.items())
    if not retained:
        missing.append('HISTORY_NOT_RETAINED')
    hpins = (hshape and _fields(history['before'], HISTORY_ROLES)
             and _fields(history['after'], HISTORY_ROLES)
             and all(_digest(v) for pins in (history['before'], history['after'])
                     for v in pins.values())
             and history['before'] == history['after'])
    if not hpins:
        missing.append('HISTORY_BINDINGS_CHANGED')

    bindings = snapshot['bindings']
    bound = (_fields(bindings, BINDINGS)
             and type(bindings['epoch']) is str
             and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', bindings['epoch']) is not None
             and _integer(bindings['cursor'])
             and all(_digest(bindings[k]) for k in HASH_BINDINGS)
             and _lock(bindings['lock_expected']) and _lock(bindings['lock_observed']))
    if not bound:
        missing.append('BINDINGS_INVALID')
    elif (bindings['process_expected'] != bindings['process_observed']
          or bindings['lock_expected'] != bindings['lock_observed']):
        missing.append('OWNERSHIP_BINDING_MISMATCH')

    ownership = snapshot['ownership']
    if not _fields(ownership, FACTS):
        missing.append('OWNERSHIP_SCHEMA_INVALID')
    else:
        for name in sorted(FACTS):
            if type(ownership[name]) is not bool or ownership[name] is not True:
                missing.append(name.upper() + '_UNPROVEN')

    witness = snapshot['bootstrap']
    # No policy exception or evidence made by the proposed transaction can fill
    # the prior native-exclusion requirement of the current contract.
    excluded = (bound and retained and hpins and _fields(witness, BOOTSTRAP)
                and type(witness['kind']) is str
                and witness['kind'] in ('independent_preexisting_ap_exclusion',
                                         'native_single_owner_lease')
                and witness['qualified'] is True and witness['before_new_intent'] is True
                and type(witness['epoch']) is str and witness['epoch'] == bindings['epoch']
                and _integer(witness['cursor']) and witness['cursor'] == bindings['cursor']
                and _digest(witness['state_sha256'])
                and witness['state_sha256'] == bindings['state_sha256']
                and _digest(witness['old_session_sha256'])
                and witness['old_session_sha256'] == history['before']['log_attempt']
                and _digest(witness['producer_sha256']))
    if not excluded:
        missing.append('AP_EXCLUSION_UNPROVEN')

    identity = snapshot['identity']
    if not (_fields(identity, TARGET.keys())
            and all(type(identity[k]) is type(v) and identity[k] == v
                    for k, v in TARGET.items())):
        missing.append('TARGET_OR_SOURCE_MISMATCH')
    profile = snapshot['profile']
    if not (_fields(profile, {'name', 'cache_getters'})
            and type(profile['name']) is str and profile['name'] == PROFILE
            and type(profile['cache_getters']) is list and not profile['cache_getters']):
        missing.append('PAYLOAD_PROFILE_NOT_QUALIFIED')
    return _result(missing)
