"""Prospective, code-settled trials. Model opinions never settle a version.

The external-effect controller owns dispatch and reservations. This module owns
the immutable paired plan, certified measurement admission, single trial lock,
three-axis outcome, and any remaining restoration responsibility.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

VERSION_AXES = frozenset({'policy', 'capability', 'scientific_protocol'})
MODES = frozenset({'replay', 'role_recall', 'isolated_branch'})
ACTIVE = frozenset({'active', 'awaiting_settlement', 'restoring'})
SCHEMA_VERSION = 'rsi_trial_v2'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f'{field} requires a bounded nonempty string')


def _resources(value, field, *, positive=False):
    if not isinstance(value, dict) or not value or any(not isinstance(k, str) or not k or not finite(v) or v < 0 for k, v in value.items()):
        raise ValueError(f'{field} requires explicit finite nonnegative resource limits')
    if positive and not any(value.values()):
        raise ValueError(f'{field} must reserve a nonzero settlement allowance')


def validate_trial_spec(spec, *, schema_version=SCHEMA_VERSION):
    required = {'schema_version', 'intervention_kind', 'baseline_versions', 'candidate_versions',
                'study_id', 'new_study_id', 'applicability', 'execution_mode', 'pairs', 'minimum_pairs',
                'metrics', 'release_rule', 'budget', 'settlement_reserve', 'recovery_owner'}
    if not isinstance(spec, dict) or set(spec) != required or spec['schema_version'] != schema_version:
        raise ValueError('Expected the exact prospective ' + schema_version + ' contract')
    if spec['intervention_kind'] not in VERSION_AXES or spec['execution_mode'] not in MODES:
        raise ValueError('Unsupported intervention kind or execution mode')
    for key in ('baseline_versions', 'candidate_versions'):
        if not isinstance(spec[key], dict) or set(spec[key]) != VERSION_AXES:
            raise ValueError('Policy, capability and scientific protocol versions are separate required axes')
        for value in spec[key].values():
            _text(value, key)
    changed = {axis for axis in VERSION_AXES if spec['baseline_versions'][axis] != spec['candidate_versions'][axis]}
    if spec['intervention_kind'] not in changed:
        raise ValueError('The proposed intervention must change its declared version axis')
    if spec['intervention_kind'] == 'policy' and changed != {'policy'}:
        raise ValueError('An orchestration trial cannot quietly change tools or scientific protocol')
    if spec['intervention_kind'] != 'policy' and 'policy' in changed:
        raise ValueError('A method or capability trial must not conflate policy intervention')
    _text(spec['study_id'], 'study_id')
    if 'scientific_protocol' in changed:
        _text(spec['new_study_id'], 'new_study_id')
        if spec['new_study_id'] == spec['study_id'] or spec['execution_mode'] != 'isolated_branch':
            raise ValueError('Scientific changes require a distinct study and isolated execution')
    elif spec['new_study_id'] is not None:
        raise ValueError('Do not fabricate a new scientific study for an unchanged scientific protocol')
    if spec['execution_mode'] == 'role_recall' and changed != {'policy'}:
        raise ValueError('Role re-calls cannot establish capability or scientific-method performance')
    if not isinstance(spec['applicability'], dict) or not spec['applicability']:
        raise ValueError('Explicit applicability is required')
    encoded(spec['applicability'])
    _resources(spec['budget'], 'budget')
    _resources(spec['settlement_reserve'], 'settlement_reserve', positive=True)
    if not set(spec['settlement_reserve']).issubset(spec['budget']) or any(spec['settlement_reserve'][k] > spec['budget'][k] for k in spec['settlement_reserve']):
        raise ValueError('Settlement allowance must fit inside the registered trial budget')
    _text(spec['recovery_owner'], 'recovery_owner')
    if spec['release_rule'] not in ('any_primary', 'all_primary'):
        raise ValueError('Unsupported release rule')
    pairs = spec['pairs']
    if not isinstance(pairs, list) or not pairs or len(pairs) > 256:
        raise ValueError('Trial needs a bounded complete list of prospective pairs')
    if type(spec['minimum_pairs']) is not int or not 1 <= spec['minimum_pairs'] <= len(pairs):
        raise ValueError('Minimum information cannot exceed the full prospective design')
    seen_pairs, seen_runs, seen_inputs = set(), set(), set()
    for pair in pairs:
        if not isinstance(pair, dict) or set(pair) != {'pair_id', 'input_hash', 'baseline_run_id', 'candidate_run_id'}:
            raise ValueError('Each pair requires exact input identity and both future run IDs')
        for key, value in pair.items():
            _text(value, key)
        if (pair['pair_id'] in seen_pairs or pair['input_hash'] in seen_inputs
                or pair['baseline_run_id'] == pair['candidate_run_id']
                or {pair['baseline_run_id'], pair['candidate_run_id']} & seen_runs):
            raise ValueError('Pairs, inputs and all arm runs must be distinct')
        seen_pairs.add(pair['pair_id']); seen_inputs.add(pair['input_hash'])
        seen_runs.update((pair['baseline_run_id'], pair['candidate_run_id']))
    metrics = spec['metrics']
    if not isinstance(metrics, list) or not metrics or len(metrics) > 32:
        raise ValueError('A bounded frozen metric specification is required')
    names = set()
    for metric in metrics:
        if not isinstance(metric, dict) or set(metric) != {'name', 'direction', 'role', 'min_gain', 'max_loss', 'per_pair_no_regression'}:
            raise ValueError('Each metric must freeze direction, evidence threshold and protection rule')
        _text(metric['name'], 'metric name')
        if (metric['name'] in names or metric['direction'] not in ('lower', 'higher')
                or metric['role'] not in ('primary', 'guard') or type(metric['per_pair_no_regression']) is not bool
                or not finite(metric['min_gain']) or metric['min_gain'] < 0
                or not finite(metric['max_loss']) or metric['max_loss'] < 0):
            raise ValueError('Invalid or duplicate metric rule')
        names.add(metric['name'])
    if not any(metric['role'] == 'primary' for metric in metrics):
        raise ValueError('At least one primary evidence criterion is required')
    return json.loads(encoded(spec))


def validate_measurement(spec, measurement):
    required = {'schema_version', 'trial_id', 'pair_id', 'arm', 'run_id', 'input_hash', 'versions',
                'execution_status', 'metrics', 'costs', 'evidence_refs'}
    if not isinstance(measurement, dict) or set(measurement) != required or measurement['schema_version'] != 'trial_measurement_v2':
        raise ValueError('Expected an exact certified trial_measurement_v2 receipt')
    pair = next((p for p in spec['pairs'] if p['pair_id'] == measurement['pair_id']), None)
    arm = measurement['arm']
    if pair is None or arm not in ('baseline', 'candidate'):
        raise ValueError('Unregistered measurement pair or arm')
    if (measurement['run_id'] != pair[arm + '_run_id'] or measurement['input_hash'] != pair['input_hash']
            or measurement['versions'] != spec[arm + '_versions']):
        raise ValueError('Measurement changed the prospective input, branch or version identity')
    if measurement['execution_status'] not in ('complete', 'incomplete', 'invalid'):
        raise ValueError('Unknown measurement execution state')
    if not isinstance(measurement['metrics'], dict) or set(measurement['metrics']) != {m['name'] for m in spec['metrics']}:
        raise ValueError('Missing metric keys must be explicit null, never silently omitted or zeroed')
    if any(value is not None and not finite(value) for value in measurement['metrics'].values()):
        raise ValueError('Metrics must be finite numbers or explicit missing values')
    if not isinstance(measurement['costs'], dict) or set(measurement['costs']) != set(spec['budget']):
        raise ValueError('Every budget resource needs an observed cost or explicit unknown value')
    if any(v is not None and (not finite(v) or v < 0) for v in measurement['costs'].values()):
        raise ValueError('Resource cost must be finite nonnegative or unknown')
    refs = measurement['evidence_refs']
    if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or len(ref) != 64
            or any(ch not in '0123456789abcdef' for ch in ref) for ref in refs) or len(set(refs)) != len(refs)):
        raise ValueError('Measurement requires distinct immutable evidence references')
    return copy.deepcopy(measurement)


def settle_trial(spec, measurements, *, candidate_applied=False, restoration_confirmed=False,
                 additional_costs=(), recovery_costs=(), schema_version=SCHEMA_VERSION):
    """Pure three-axis settlement. Missing observations never count as zero."""
    spec = validate_trial_spec(spec, schema_version=schema_version)
    rows = [validate_measurement(spec, row) for row in measurements]
    keys = [(row['pair_id'], row['arm']) for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate trial arm measurements cannot be selected or averaged')
    expected = {(pair['pair_id'], arm) for pair in spec['pairs'] for arm in ('baseline', 'candidate')}
    if not set(keys).issubset(expected):
        raise ValueError('Unregistered measurements')
    extra, recovery = list(additional_costs), list(recovery_costs)
    for costs in extra + recovery:
        if (not isinstance(costs, dict) or set(costs) != set(spec['budget'])
                or any(v is not None and (not finite(v) or v < 0) for v in costs.values())):
            raise ValueError('Additional effects require complete observed or explicitly unknown resource costs')
    research_cost_rows = [row['costs'] for row in rows] + extra
    all_cost_rows = research_cost_rows + recovery
    known_costs = {r: sum(costs[r] for costs in all_cost_rows if costs[r] is not None) for r in spec['budget']}
    research_costs = {r: sum(costs[r] for costs in research_cost_rows if costs[r] is not None) for r in spec['budget']}
    unknown_costs = {r: sum(costs[r] is None for costs in all_cost_rows) for r in spec['budget']}
    invalid = any(row['execution_status'] == 'invalid' for row in rows)
    exceeded = [r for r, cost in known_costs.items() if cost > spec['budget'][r]
                or research_costs[r] > spec['budget'][r] - spec['settlement_reserve'].get(r, 0)]
    invalid |= bool(exceeded)
    incomplete = (set(keys) != expected or len(rows) < 2 * spec['minimum_pairs']
                  or any(row['execution_status'] != 'complete' or any(v is None for v in row['metrics'].values()) for row in rows)
                  or any(unknown_costs.values()))
    execution = 'invalid' if invalid else ('incomplete' if incomplete else 'valid')
    metric_results = []
    evidence = 'insufficient'
    if execution == 'valid':
        by_key = {(row['pair_id'], row['arm']): row for row in rows}
        for metric in spec['metrics']:
            gains = [(by_key[(pair['pair_id'], 'candidate')]['metrics'][metric['name']]
                      - by_key[(pair['pair_id'], 'baseline')]['metrics'][metric['name']])
                     * (1 if metric['direction'] == 'higher' else -1) for pair in spec['pairs']]
            mean = sum(gains) / len(gains)
            guard = mean >= -metric['max_loss'] - 1e-12 and (not metric['per_pair_no_regression'] or all(g >= -1e-12 for g in gains))
            metric_results.append({'name': metric['name'], 'role': metric['role'], 'pair_gains': gains,
                                   'mean_gain': mean, 'protection_passed': guard,
                                   'criterion_met': mean >= metric['min_gain'] - 1e-12})
        primary = [row['criterion_met'] for row in metric_results if row['role'] == 'primary']
        criteria = all(primary) if spec['release_rule'] == 'all_primary' else any(primary)
        evidence = 'met' if criteria and all(row['protection_passed'] for row in metric_results) else 'not_met'
    eligible = evidence == 'met' and spec['execution_mode'] != 'replay'
    if eligible:
        action = 'retain' if candidate_applied else 'approve_next'
    elif candidate_applied:
        action = 'restore_baseline' if restoration_confirmed else 'recovery_required'
    else:
        action = 'unchanged'
    return {'schema_version': 'trial_outcome_v2', 'execution': execution, 'evidence': evidence,
            'version_action': action, 'eligible_for_next_use': eligible,
            'execution_mode': spec['execution_mode'], 'expected_arm_count': len(expected),
            'observed_arm_count': len(rows), 'missing_arms': sorted([list(k) for k in expected - set(keys)]),
            'metric_results': metric_results, 'known_costs': known_costs, 'unknown_cost_counts': unknown_costs,
            'research_known_costs': research_costs, 'additional_effect_count': len(extra), 'recovery_effect_count': len(recovery),
            'budget_exceeded': exceeded, 'settlement_reserve': spec['settlement_reserve'],
            'recovery_owner': spec['recovery_owner'],
            'scope': ('Mechanism replay only; no new judgment or task-effect benefit established' if spec['execution_mode'] == 'replay'
                      else 'Evidence applies only to the frozen applicability, metrics and registered branches'),
            'interpretation': 'Not met is not proof of no effect; restoration is not proof of harm; retaining a version is not proof of task benefit'}


class TrialEngine:
    def __init__(self, store, run_id, *, schema_version=SCHEMA_VERSION):
        self.store, self.run_id = store, run_id
        self.schema_version = schema_version

    def _spec(self, trial):
        spec = json.loads(self.store.read_blob(trial['spec_ref']))
        if digest(spec) != trial['spec_sha256']:
            raise ValueError('Frozen trial specification changed')
        return validate_trial_spec(spec, schema_version=self.schema_version)

    def register(self, trial_id, spec, *, key):
        spec = validate_trial_spec(spec, schema_version=self.schema_version)
        def commit(tx):
            if any(row.get('status') in ACTIVE for row in tx.find('trial')):
                raise ValueError('An unsettled trial already owns the run trial lock')
            reference = tx.blob(spec)
            return tx.insert('trial', trial_id, {'status': 'proposed', 'spec_ref': reference,
                'spec_sha256': digest(spec), 'candidate_applied': False, 'restoration_confirmed': False,
                'outcome_ref': None, 'recovery_owner': spec['recovery_owner']})
        return self.store.mutate(self.run_id, key, 'trial_registered', commit, payload={'trial_id': trial_id, 'spec': spec})

    def start(self, trial_id, *, key):
        def commit(tx):
            trial = tx.get('trial', trial_id)
            if trial['status'] != 'proposed' or any(row['id'] != trial_id and row.get('status') in ACTIVE for row in tx.find('trial')):
                raise ValueError('Only one prospective trial may become active')
            spec = self._spec(trial)
            planned_runs = {pair[arm + '_run_id'] for pair in spec['pairs'] for arm in ('baseline', 'candidate')}
            if any(effect.get('metadata', {}).get('run_id') in planned_runs or effect.get('metadata', {}).get('trial_id') == trial_id for effect in tx.find('effect')):
                raise ValueError('Trial admission must precede all registered arm effects')
            return tx.update('trial', trial_id, {'status': 'active', 'admitted_seq': tx.seq})
        return self.store.mutate(self.run_id, key, 'trial_started', commit, payload={'trial_id': trial_id})

    def record_measurement(self, trial_id, effect_id, *, key):
        def commit(tx):
            trial, effect = tx.get('trial', trial_id), tx.get('effect', effect_id)
            if trial['status'] != 'active' or effect.get('status') != 'succeeded':
                raise ValueError('Measurements require an active trial and a succeeded certified Effect')
            spec = self._spec(trial)
            receipt_ref = effect.get('result_ref') or effect.get('receipt_ref')
            receipt = json.loads(self.store.read_blob(receipt_ref))
            row = validate_measurement(spec, receipt.get('trial_measurement'))
            metadata = effect.get('metadata', {})
            if row['trial_id'] != trial_id or any(metadata.get(k) != row[k] for k in ('trial_id', 'pair_id', 'arm', 'run_id')):
                raise ValueError('Measurement is not bound to the intended trial Effect')
            if effect.get('created_seq', 0) <= trial['admitted_seq']:
                raise ValueError('Measurements cannot predate prospective registration')
            if row['costs'] != {resource: receipt.get('actual_costs', {}).get(resource) for resource in spec['budget']}:
                raise ValueError('Measurement costs differ from the actual Effect receipt')
            original_costs = copy.deepcopy(row['costs'])
            settled_costs = {resource: effect.get('actual_costs', {}).get(resource) for resource in spec['budget']}
            if any(value is not None and settled_costs[resource] != value for resource, value in original_costs.items()):
                raise ValueError('Immutable measurement costs contradict the settled Effect ledger')
            # The result receipt remains immutable. Later authenticated cost
            # reconciliation may fill a missing value without rewriting metrics.
            row['costs'] = settled_costs
            for reference in row['evidence_refs']:
                self.store.read_blob(reference)
            measurement_id = f'{trial_id}:{row["pair_id"]}:{row["arm"]}'
            return tx.insert('measurement', measurement_id, {'trial_id': trial_id, 'effect_id': effect_id,
                'receipt_ref': receipt_ref, 'measurement': row, 'original_costs': original_costs,
                'cost_effect_version': effect['version']})
        return self.store.mutate(self.run_id, key, 'trial_measurement_recorded', commit,
                                 payload={'trial_id': trial_id, 'effect_id': effect_id})

    def mark_candidate_applied(self, trial_id, effect_id, *, key):
        """Track temporary application only with an actual controller Effect receipt."""
        def commit(tx):
            trial, effect = tx.get('trial', trial_id), tx.get('effect', effect_id)
            receipt = json.loads(self.store.read_blob(effect.get('result_ref') or effect.get('receipt_ref')))
            expected = {'trial_id': trial_id, 'candidate_versions': self._spec(trial)['candidate_versions']}
            if (trial['status'] != 'active' or effect.get('status') != 'succeeded' or receipt.get('version_application') != expected
                    or effect.get('metadata', {}).get('trial_id') != trial_id or effect['created_seq'] <= trial['admitted_seq']):
                raise ValueError('Temporary application requires its exact certified version receipt')
            return tx.update('trial', trial_id, {'candidate_applied': True, 'application_effect_id': effect_id})
        return self.store.mutate(self.run_id, key, 'trial_candidate_applied', commit, payload={'trial_id': trial_id, 'effect_id': effect_id})

    def settle(self, trial_id, *, key):
        def commit(tx):
            trial = tx.get('trial', trial_id)
            if trial['status'] not in ACTIVE:
                raise ValueError('Trial is already settled or was never active')
            effects = [effect for effect in tx.find('effect') if effect.get('metadata', {}).get('trial_id') == trial_id]
            unresolved = [effect['id'] for effect in effects
                          if effect.get('status') in ('prepared', 'dispatching', 'unknown', 'accounting_unknown')
                          or effect.get('accounting_unknown')]
            if unresolved:
                raise ValueError('Trial retains unknown or pending external-effect responsibilities: ' + ', '.join(unresolved))
            spec = self._spec(trial)
            measurements = tx.find('measurement', trial_id=trial_id)
            measured_effect_ids = {row['effect_id'] for row in measurements}
            extra, recovery, reconciled_measurements = [], [], []
            for measurement in measurements:
                effect = tx.get('effect', measurement['effect_id'])
                row = copy.deepcopy(measurement['measurement'])
                row['costs'] = {resource: effect.get('actual_costs', {}).get(resource) for resource in spec['budget']}
                reconciled_measurements.append(row)
            for effect in effects:
                if effect['id'] in measured_effect_ids:
                    continue
                costs = {resource: effect.get('actual_costs', {}).get(resource) for resource in spec['budget']}
                destination = recovery if effect.get('metadata', {}).get('trial_operation') == 'restore_baseline' else extra
                destination.append(costs)
            outcome = settle_trial(spec, reconciled_measurements, candidate_applied=trial['candidate_applied'],
                                   restoration_confirmed=trial['restoration_confirmed'], additional_costs=extra,
                                   recovery_costs=recovery, schema_version=self.schema_version)
            status = 'restoring' if outcome['version_action'] == 'recovery_required' else 'settled'
            patch = {'status': status, 'outcome_ref': tx.blob(outcome), 'outcome': outcome}
            if status == 'restoring' and trial['status'] != 'restoring':
                patch['restoration_started_seq'] = tx.seq
            return tx.update('trial', trial_id, patch)
        return self.store.mutate(self.run_id, key, 'trial_settled', commit, payload={'trial_id': trial_id})

    def confirm_restoration(self, trial_id, effect_id, *, key):
        def commit(tx):
            trial, effect = tx.get('trial', trial_id), tx.get('effect', effect_id)
            receipt = json.loads(self.store.read_blob(effect.get('result_ref') or effect.get('receipt_ref')))
            expected = {'trial_id': trial_id, 'baseline_versions': self._spec(trial)['baseline_versions']}
            metadata = effect.get('metadata', {})
            if (trial['status'] != 'restoring' or effect.get('status') != 'succeeded' or receipt.get('version_restoration') != expected
                    or metadata.get('trial_id') != trial_id or metadata.get('trial_operation') != 'restore_baseline'
                    or metadata.get('baseline_versions') != expected['baseline_versions']
                    or effect['created_seq'] <= trial['restoration_started_seq']):
                raise ValueError('Restoration is incomplete or lacks its exact certified baseline receipt')
            return tx.update('trial', trial_id, {'restoration_confirmed': True, 'restoration_effect_id': effect_id,
                                                'status': 'awaiting_settlement'})
        return self.store.mutate(self.run_id, key, 'trial_restoration_confirmed', commit, payload={'trial_id': trial_id, 'effect_id': effect_id})
