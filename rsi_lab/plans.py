"""Task-independent prospective plans and deterministic selection contracts.

Adapters authenticate raw outputs and map them into the observation contract.
This module freezes task intent and checks identity, coverage and numerical
criteria; it does not infer whether a domain method is scientifically valid.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import statistics

SCHEMA = 'rsi_task_plan_v1'
FIELDS = {'schema_version', 'task_id', 'goal', 'inputs', 'items', 'protocol',
          'capability_versions', 'metrics', 'success', 'arms', 'budget', 'stop_rule', 'missing_rule'}
DIRECTIONS = {'lower_or_equal', 'higher_or_equal'}
AGGREGATIONS = {'median', 'mean', 'min', 'max'}
STOP_RULE = 'all_planned_shards_terminal_or_explicit_stop'
MISSING_RULE = 'retain_all_missing_no_imputation_no_replacement'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(label + ' requires a nonempty exact string')


def _id(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}', value):
        raise ValueError('Invalid ' + label)


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError('Input/version identity requires an exact SHA256')


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _exact(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(label + ' requires exactly ' + str(sorted(fields)))


def validate_metric(metric):
    _exact(metric, {'name', 'units', 'direction', 'threshold', 'aggregation', 'replicates'}, 'Metric')
    _id(metric['name'], 'metric name'); _text(metric['units'], 'metric units')
    if (not isinstance(metric['direction'], str) or metric['direction'] not in DIRECTIONS or not _finite(metric['threshold'])
            or not isinstance(metric['aggregation'], str) or metric['aggregation'] not in AGGREGATIONS):
        raise ValueError('Metric needs a finite threshold, explicit direction and supported aggregation')
    repeats = metric['replicates']
    if (not isinstance(repeats, list) or not repeats or any(not isinstance(x, str) for x in repeats)
            or len(repeats) != len(set(repeats))):
        raise ValueError('A metric requires a complete unique list of replicate identities')
    for repeat in repeats:
        _id(repeat, 'replicate identity')
    return metric


def validate_task_plan(plan, *, check_future_paths=False):
    _exact(plan, FIELDS, 'TaskPlan')
    if plan['schema_version'] != SCHEMA:
        raise ValueError('Unsupported prospective TaskPlan schema')
    _id(plan['task_id'], 'task identity'); _text(plan['goal'], 'task goal')
    if not isinstance(plan['inputs'], list) or not plan['inputs']:
        raise ValueError('TaskPlan must bind its original input identities')
    names = set()
    for row in plan['inputs']:
        _exact(row, {'input_id', 'sha256'}, 'Task input')
        _id(row['input_id'], 'input identity'); _sha(row['sha256'])
        if row['input_id'] in names:
            raise ValueError('Duplicate input identity')
        names.add(row['input_id'])
    if not isinstance(plan['items'], list) or not plan['items']:
        raise ValueError('A complete planned item registry is required')
    item_ids, identities = set(), set()
    for row in plan['items']:
        _exact(row, {'item_id', 'identity'}, 'Planned item')
        _id(row['item_id'], 'item identity'); _text(row['identity'], 'original object identity')
        if row['item_id'] in item_ids or row['identity'] in identities:
            raise ValueError('Items and original identities must each be unique')
        item_ids.add(row['item_id']); identities.add(row['identity'])
    _exact(plan['protocol'], {'version', 'parameters'}, 'Task protocol')
    _text(plan['protocol']['version'], 'protocol version')
    if not isinstance(plan['protocol']['parameters'], dict):
        raise ValueError('Task protocol parameters must be explicit JSON object data')
    encoded(plan['protocol']['parameters'])
    if not isinstance(plan['capability_versions'], dict) or not plan['capability_versions']:
        raise ValueError('TaskPlan must bind its capability versions')
    for name, version in plan['capability_versions'].items():
        _text(name, 'capability identity'); _text(version, 'capability version')
    if not isinstance(plan['metrics'], list) or not plan['metrics']:
        raise ValueError('TaskPlan needs frozen metric semantics')
    metric_names = set()
    for metric in plan['metrics']:
        validate_metric(metric)
        if metric['name'] in metric_names:
            raise ValueError('Duplicate metric identity')
        metric_names.add(metric['name'])
    _exact(plan['success'], {'metric', 'min_qualified_items'}, 'Success criterion')
    if (not isinstance(plan['success']['metric'], str) or plan['success']['metric'] not in metric_names
            or type(plan['success']['min_qualified_items']) is not int
            or plan['success']['min_qualified_items'] < 1):
        raise ValueError('Success requires a registered metric and a positive task-specific item minimum')
    if not isinstance(plan['arms'], list) or not plan['arms']:
        raise ValueError('All planned experimental arms must be registered')
    arms, paths = set(), []
    for arm in plan['arms']:
        _exact(arm, {'arm_id', 'policy_version', 'policy_sha256', 'shards'}, 'Experimental arm')
        _id(arm['arm_id'], 'arm identity'); _text(arm['policy_version'], 'policy version'); _sha(arm['policy_sha256'])
        if arm['arm_id'] in arms or not isinstance(arm['shards'], list) or not arm['shards']:
            raise ValueError('Duplicate or empty experimental arm')
        arms.add(arm['arm_id']); assigned, shard_ids = set(), set()
        for shard in arm['shards']:
            _exact(shard, {'shard_id', 'item_ids', 'output_path', 'adapter_id', 'protocol_sha256'}, 'Planned shard')
            _id(shard['shard_id'], 'shard identity'); _text(shard['adapter_id'], 'adapter identity'); _sha(shard['protocol_sha256'])
            if shard['shard_id'] in shard_ids:
                raise ValueError('Repeated shard identity')
            shard_ids.add(shard['shard_id'])
            members = shard['item_ids']
            if (not isinstance(members, list) or not members or any(not isinstance(x, str) for x in members)
                    or len(members) != len(set(members)) or not set(members) <= item_ids or assigned & set(members)):
                raise ValueError('Shards must form a disjoint partition of registered items')
            assigned.update(members)
            _text(shard['output_path'], 'output path')
            path = Path(shard['output_path'])
            if not path.is_absolute() or str(path.resolve()) != str(path):
                raise ValueError('Output path must be absolute and canonical')
            if check_future_paths and path.exists():
                raise ValueError('Plan must precede output materialization')
            if any(path == old or path.is_relative_to(old) or old.is_relative_to(path) for old in paths):
                raise ValueError('Every planned shard requires a disjoint output path')
            paths.append(path)
        if assigned != item_ids:
            raise ValueError('Every arm must retain every planned item')
    if not isinstance(plan['budget'], dict) or not plan['budget']:
        raise ValueError('A finite prospective resource budget is required')
    for resource, value in plan['budget'].items():
        _text(resource, 'resource unit')
        if not _finite(value) or value < 0:
            raise ValueError('Budget limits must be finite and nonnegative')
    if plan['stop_rule'] != STOP_RULE or plan['missing_rule'] != MISSING_RULE:
        raise ValueError('Failed, unknown and unstarted planned work cannot be removed or imputed')
    return json.loads(encoded(plan))


def freeze_task_plan(tx, plan):
    """Freeze within a caller's transaction, before related Effects or outputs."""
    plan = validate_task_plan(plan, check_future_paths=True)
    task_id = plan['task_id']; registration_id = 'task_plan:' + task_id
    if tx.find('registration', id=registration_id):
        raise ValueError('TaskPlan is immutable; a changed task requires a new identity')
    for effect in tx.find('effect'):
        metadata = effect.get('metadata', {})
        if (metadata.get('task_plan_id') == task_id or metadata.get('task_id') == task_id
                or metadata.get('task_binding', {}).get('task_id') == task_id):
            raise ValueError('TaskPlan registration cannot follow a related execution Effect')
    return tx.insert('registration', registration_id, {'kind': 'task_plan', 'task_id': task_id,
        'status': 'frozen', 'plan_ref': tx.blob(plan), 'plan_hash': digest(plan)})


def register_task_plan(store, run_id, plan, *, key):
    plan = validate_task_plan(plan)
    return store.mutate(run_id, key, 'task_plan.registered', lambda tx: freeze_task_plan(tx, plan), payload={'plan': plan})


def load_task_plan(store, run_id, task_id):
    """Read a plan anchored to its original immutable registration event."""
    row = store.get(run_id, 'registration', 'task_plan:' + task_id)
    if row.get('kind') != 'task_plan' or row.get('task_id') != task_id or row.get('status') != 'frozen':
        raise ValueError('TaskPlan registration identity changed')
    event = store.db.execute('SELECT changes_ref FROM events WHERE run_id=? AND seq=?', (run_id, row['created_seq'])).fetchone()
    if event is None:
        raise ValueError('TaskPlan has no prospective registration event')
    originals = [change['document'] for change in json.loads(store.read_blob(event['changes_ref']))
                 if change['kind'] == 'registration' and change['document']['id'] == row['id']]
    if (len(originals) != 1 or originals[0]['version'] != 1 or originals[0]['created_seq'] != row['created_seq']
            or any(row[key] != originals[0][key] for key in ('plan_ref', 'plan_hash', 'task_id', 'kind'))):
        raise ValueError('Frozen TaskPlan cannot be replaced after registration')
    plan = validate_task_plan(json.loads(store.read_blob(row['plan_ref'])))
    if plan['task_id'] != task_id or digest(plan) != row['plan_hash']:
        raise ValueError('Frozen TaskPlan content identity changed')
    return plan


def metric_passes(metric, value):
    validate_metric(metric)
    if value is None:
        return False
    if not _finite(value):
        raise ValueError('A measured value must be finite or explicitly missing')
    return value <= metric['threshold'] if metric['direction'] == 'lower_or_equal' else value >= metric['threshold']


def aggregate_metric(metric, values):
    validate_metric(metric)
    if not isinstance(values, dict) or set(values) != set(metric['replicates']):
        raise ValueError('Measurement must retain every frozen replicate identity')
    if any(value is not None and not _finite(value) for value in values.values()):
        raise ValueError('Replicate values must be finite or explicitly missing')
    if any(value is None for value in values.values()):
        return None
    reducer = {'median': statistics.median, 'mean': statistics.mean, 'min': min, 'max': max}[metric['aggregation']]
    return reducer(values.values())


def select_items(plan, observations, arm_id):
    """Evaluate authenticated adapter observations under a frozen task contract.

    This pure function does not authenticate a provider or source archive. The
    registered domain adapter must verify raw execution/identity evidence first.
    Every item remains in the result, including omitted, failed or unknown work.
    """
    plan = validate_task_plan(plan)
    if arm_id not in {arm['arm_id'] for arm in plan['arms']}:
        raise ValueError('Cannot substitute or combine task arms')
    expected = {item['item_id']: item for item in plan['items']}
    metrics = {metric['name']: metric for metric in plan['metrics']}
    observed = {}
    if not isinstance(observations, list):
        raise ValueError('Observations must be the complete adapter output list')
    for row in observations:
        _exact(row, {'item_id', 'identity', 'arm_id', 'status', 'values', 'evidence_refs'}, 'Adapter observation')
        row = json.loads(encoded(row))
        item_id = row['item_id']
        _id(item_id, 'observed item identity')
        if item_id not in expected or item_id in observed or row['identity'] != expected[item_id]['identity'] or row['arm_id'] != arm_id:
            raise ValueError('Observation has a duplicate, foreign or changed item/arm identity')
        if (not isinstance(row['status'], str) or row['status'] not in {'completed', 'failed', 'unrun', 'unknown'}
                or not isinstance(row['values'], dict) or set(row['values']) != set(metrics)):
            raise ValueError('Observation must retain its execution state and every planned metric')
        if (not isinstance(row['evidence_refs'], list) or any(not isinstance(x,str) for x in row['evidence_refs'])
                or len(row['evidence_refs']) != len(set(row['evidence_refs']))):
            raise ValueError('Observation evidence references must be a unique list')
        for reference in row['evidence_refs']:
            _sha(reference)
        if row['status'] == 'completed' and not row['evidence_refs']:
            raise ValueError('Completed observations require authenticated adapter evidence references')
        scores = {name: aggregate_metric(metric, row['values'][name]) for name, metric in metrics.items()}
        if row['status'] != 'completed':
            scores = dict.fromkeys(metrics)
        observed[item_id] = {**row, 'scores': scores}
    all_rows = [observed.get(item['item_id'], {**item, 'arm_id': arm_id, 'status': 'unrun',
        'values': {name: dict.fromkeys(metric['replicates']) for name,metric in metrics.items()},
        'scores': dict.fromkeys(metrics), 'evidence_refs': []}) for item in plan['items']]
    criterion = metrics[plan['success']['metric']]
    score_name = criterion['name']
    selected = [row for row in all_rows if metric_passes(criterion, row['scores'][score_name])]
    sign = 1 if criterion['direction'] == 'lower_or_equal' else -1
    selected.sort(key=lambda row: (sign * row['scores'][score_name], row['item_id']))
    unresolved = any(row['status'] in {'unrun', 'unknown'} or
                     (row['status'] == 'completed' and any(value is None for value in row['scores'].values())) for row in all_rows)
    minimum = plan['success']['min_qualified_items']; count = len(selected)
    status = 'incomplete' if unresolved else 'accepted' if count >= minimum else 'insufficient_items'
    return {'schema_version': 'rsi_task_selection_v1', 'task_id': plan['task_id'], 'arm_id': arm_id,
        'plan_hash': digest(plan), 'status': status, 'task_contract_satisfied': status == 'accepted',
        'count': count, 'shortfall': max(0, minimum-count), 'success': plan['success'],
        'items': selected, 'all_observations': all_rows, 'missing_count': sum(row['scores'][score_name] is None for row in all_rows)}
