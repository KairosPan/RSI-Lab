"""Prospective coverage contract for the live exploratory docking application.

This host boundary freezes the complete arm before Vina dispatch. Dynamic
1–8-item batches partition that arm; they cannot replace failures or repeat an
identity. The metric measures known execution dispositions, not docking energy,
valid poses, biological benefit, or formal screening qualification.
"""
from __future__ import annotations

import json
from pathlib import Path

from rsi_lab import plans
from rsi_lab.controller import identity
from rsi_lab.runtime import Runtime
from rsi_lab.store import digest
from . import live_vina

TASK_ID = 'live-docking-exploratory-coverage-v1'
ARM = 'exploratory'
POLICY_VERSION = 'live_task_contract_v1'
METRIC = 'execution_disposition_terminal'


def _read(path):
    return json.loads(Path(path).read_text())


def _is_vina(effect):
    return (effect['kind'] == 'docking_vina_batch'
            or effect.get('metadata', {}).get('adapter_id') == live_vina.ADAPTER_ID)


def _source_sha():
    return digest(Path(__file__).read_bytes())


def _make_plan(runtime, case, task_id):
    data, protocol_path = Path(case['data_dir']).resolve(), Path(case['protocol_path']).resolve()
    protocol = _read(protocol_path)
    protocol_sha = digest(protocol_path.read_bytes())
    rows = _read(data / 'candidates.json')['candidates']
    by_id = {row['candidate_id']: row for row in rows}
    if len(by_id) != len(rows) or case['planned_ids'] != [row['candidate_id'] for row in rows]:
        raise ValueError('The exploratory arm must preserve the exact complete frozen candidate order')
    if case['planned_seed_jobs'] != len(rows) * 2 or protocol['seeds'] != [17, 43]:
        raise ValueError('Planned worker coverage differs from the supported two-seed protocol')
    if any(digest((data / name).read_bytes()) != sha for name, sha in protocol['input_sha256'].items()):
        raise ValueError('Current data does not match the prospective protocol inputs')
    sources = {source['source_id']: source for source in case['sources']}
    if sources['protocol.json']['sha256'] != protocol_sha:
        raise ValueError('Current protocol differs from the original experiment source')
    for name, sha in protocol['input_sha256'].items():
        if name in sources and sources[name]['sha256'] != sha:
            raise ValueError('Protocol input differs from the original experiment source')
    output_root = (runtime.store.root / 'screen').resolve()
    policy_sha = _source_sha()
    parameters = {'data_dir': str(data), 'protocol_path': str(protocol_path),
        'protocol_sha256': protocol_sha, 'vina_protocol': protocol, 'output_root': str(output_root),
        'batch_policy': 'one_to_eight_disjoint_items_per_direct_child_directory_no_retry_or_replacement',
        'metric_semantics': ('One means the planned seed has a known terminal execution disposition; '
            'a definite batch failure is a recorded terminal failure even if its workers did not start. '
            'Unknown and unassigned work do not count. All scientific outcome statuses are retained.')}
    return plans.validate_task_plan({'schema_version': plans.SCHEMA, 'task_id': task_id, 'goal': case['goal'],
        'inputs': [{'input_id': name, 'sha256': sha} for name, sha in sorted(protocol['input_sha256'].items())]
                  + [{'input_id': 'protocol.json', 'sha256': protocol_sha}],
        'items': [{'item_id': row['candidate_id'],
                   'identity': row.get('canonical_parent_identity') or row['canonical_smiles']} for row in rows],
        'protocol': {'version': protocol['protocol_id'], 'parameters': parameters},
        'capability_versions': {live_vina.ADAPTER_ID: live_vina.VERSION},
        'metrics': [{'name': METRIC, 'units': 'known_terminal_disposition', 'direction': 'higher_or_equal',
                     'threshold': 1, 'aggregation': 'min', 'replicates': ['17', '43']}],
        'success': {'metric': METRIC, 'min_qualified_items': len(rows)},
        'arms': [{'arm_id': ARM, 'policy_version': POLICY_VERSION, 'policy_sha256': policy_sha,
                  'shards': [{'shard_id': 'all', 'item_ids': case['planned_ids'], 'output_path': str(output_root),
                              'adapter_id': live_vina.ADAPTER_ID, 'protocol_sha256': protocol_sha}]}],
        'budget': {name: row['limit'] for name, row in case['budgets'].items()},
        'stop_rule': plans.STOP_RULE, 'missing_rule': plans.MISSING_RULE})


def register_contract(runtime, case, *, task_id=TASK_ID):
    """Freeze before any Vina Effect/output; investigation Effects may precede it."""
    config_case = json.loads(runtime.store.read_blob(runtime.config['case_ref']))
    if case != config_case:
        raise ValueError('Contract case differs from the original immutable experiment')
    plan = _make_plan(runtime, case, task_id)
    existing = runtime.store.list(runtime.run_id, 'registration', id='task_plan:' + task_id)
    if existing:
        if plans.load_task_plan(runtime.store, runtime.run_id, task_id) != plan:
            raise ValueError('Existing exploratory TaskPlan differs; it cannot be replaced')
        return existing[0]
    if any(_is_vina(effect) for effect in runtime.store.list(runtime.run_id, 'effect')):
        raise ValueError('Exploratory TaskPlan must precede every Vina execution Effect')
    # Source bytes are archived before the policy hash becomes an execution rule.
    runtime.store.put_blob(Path(__file__).read_bytes())
    result = plans.register_task_plan(runtime.store, runtime.run_id, plan, key='live-task-contract:' + task_id)
    runtime.controller.add_fact(runtime.run_id, 'task-contract:' + task_id, 'raw_fact',
        {'task_plan_id': task_id, 'task_plan_hash': digest(plan), 'plan': plan},
        {'source': 'prospective host execution contract', 'registration_id': result['id'],
         'policy_source_sha256': _source_sha()}, 'publish-task-contract:' + task_id)
    return result


def _load(runtime, task_id):
    runtime.store.verify_history(runtime.run_id)
    plan = plans.load_task_plan(runtime.store, runtime.run_id, task_id)
    if (len(plan['arms']) != 1 or plan['arms'][0]['arm_id'] != ARM
            or plan['arms'][0]['policy_version'] != POLICY_VERSION
            or plan['arms'][0]['policy_sha256'] != _source_sha()
            or plan['capability_versions'] != {live_vina.ADAPTER_ID: live_vina.VERSION}):
        raise ValueError('Unsupported or changed host task contract implementation')
    return plan


def _validate_inputs(plan, inputs):
    if not isinstance(inputs, dict) or set(inputs) != {'data_dir', 'protocol_path', 'selected_ids', 'output_dir'}:
        raise ValueError('Unsupported Vina task inputs')
    p = plan['protocol']['parameters']
    if inputs['data_dir'] != p['data_dir'] or inputs['protocol_path'] != p['protocol_path']:
        raise ValueError('Cannot switch frozen task data or protocol paths')
    protocol_path = Path(p['protocol_path'])
    if digest(protocol_path.read_bytes()) != p['protocol_sha256']:
        raise ValueError('Frozen task protocol bytes changed')
    data = Path(p['data_dir'])
    for row in plan['inputs']:
        path = protocol_path if row['input_id'] == 'protocol.json' else data / row['input_id']
        if digest(path.read_bytes()) != row['sha256']:
            raise ValueError('Frozen task input bytes changed: ' + row['input_id'])
    ids = inputs['selected_ids']
    planned = {row['item_id'] for row in plan['items']}
    if (not isinstance(ids, list) or not 1 <= len(ids) <= live_vina.MAX_CANDIDATES
            or any(not isinstance(cid, str) for cid in ids) or len(ids) != len(set(ids))
            or not set(ids) <= planned):
        raise ValueError('Batch must contain one to eight distinct planned identities')
    output = Path(inputs['output_dir'])
    if (not output.is_absolute() or str(output.resolve()) != str(output)
            or output.parent != Path(p['output_root'])):
        raise ValueError('Batch output must be a canonical direct child of the frozen output root')


def _claims(runtime, task_id):
    return [row for row in runtime.store.list(runtime.run_id, 'registration')
            if row.get('kind') == 'live_vina_batch_claim' and row.get('task_plan_id') == task_id]


def _validate_effects(runtime, plan):
    claims = {row['work_id']: row for row in _claims(runtime, plan['task_id'])}
    for effect in runtime.store.list(runtime.run_id, 'effect'):
        if not _is_vina(effect):
            continue
        meta = effect['metadata']
        claim = claims.get(effect['work_id'])
        if (meta.get('task_plan_id') != plan['task_id'] or meta.get('task_plan_sha256') != digest(plan)
                or claim is None):
            raise ValueError('Found a Vina Effect outside the frozen host task contract')
        packet = json.loads(runtime.store.read_blob(effect['packet_ref']))
        for name in ('selected_ids', 'data_dir', 'protocol_path', 'output_dir'):
            if packet[name] != claim['inputs'][name]:
                raise ValueError('Vina Effect changed its reserved task batch identity')
        if packet['protocol_sha256'] != plan['protocol']['parameters']['protocol_sha256']:
            raise ValueError('Vina Effect changed its frozen task protocol')


def checked_execute(runtime, work, *, task_id=TASK_ID):
    """Check/claim a dynamic partition, then use unchanged generic dispatch.

    A local Runtime view adds the already registered plan to Effect metadata.
    It does not mutate the run configuration or change the Meta role context.
    The claim transaction prevents parallel hosts from allocating the same item.
    """
    plan = _load(runtime, task_id)
    work_id = work if isinstance(work, str) else work['id']
    work = runtime.store.get(runtime.run_id, 'work', work_id)
    if (work['kind'] != 'diagnostic' or work['inputs'].get('capability_id') != live_vina.ADAPTER_ID
            or work['inputs'].get('capability_version') != live_vina.VERSION):
        raise ValueError('Execution needs the admitted Vina diagnostic Work')
    inputs = json.loads(work['inputs']['inputs_json'])
    _validate_inputs(plan, inputs)
    _validate_effects(runtime, plan)
    claim_id = identity('live_batch_claim', task_id, work_id)
    policy = {'task_plan_id': task_id, 'task_plan_sha256': digest(plan), 'work_id': work_id, 'inputs': inputs}

    def claim(tx):
        for previous in tx.find('registration', kind='live_vina_batch_claim', task_plan_id=task_id):
            if previous['work_id'] == work_id:
                if previous['inputs'] != inputs or previous['task_plan_sha256'] != digest(plan):
                    raise ValueError('Cannot change an already reserved batch')
                return previous
            if set(previous['inputs']['selected_ids']) & set(inputs['selected_ids']):
                raise ValueError('Planned identities already reserved by another batch; no retry or replacement')
            if previous['inputs']['output_dir'] == inputs['output_dir']:
                raise ValueError('Output path already belongs to another batch')
        if Path(inputs['output_dir']).exists():
            raise ValueError('Unclaimed batch output already exists')
        return tx.insert('registration', claim_id, {**policy, 'kind': 'live_vina_batch_claim', 'status': 'reserved',
                          'host_validation_ref': tx.blob({**policy, 'validated': True,
                                                        'policy_source_sha256': _source_sha()})})

    runtime.store.mutate(runtime.run_id, 'claim-live-task-batch:' + work_id, 'live_task_batch_reserved', claim, payload=policy)
    bound = Runtime(runtime.store, runtime.run_id, adapters=list(runtime.adapters.by_id.values()))
    bound.config = {**bound.config, 'task_plan_id': task_id}
    existing = [e for e in runtime.store.list(runtime.run_id, 'effect') if e['work_id'] == work_id]
    if existing:
        if len(existing) != 1:
            raise ValueError('One admitted Work cannot own multiple Vina executions')
        effect = existing[0]
        _validate_effects(runtime, plan)
        if effect['status'] == 'prepared':
            adapter = bound.adapters.resolve(effect)
            effect = bound.controller.dispatch_effect(runtime.run_id, effect['id'], adapter.execute)
        return {'effect': effect, 'result': bound.publish_adapter_effect(effect)
                if effect['status'] == 'succeeded' and not effect.get('accounting_unknown') else None}
    return bound.execute_adapter(live_vina.ADAPTER_ID, inputs, trigger_id=work_id, work_id=work_id)


def coverage_report(runtime, *, task_id=TASK_ID):
    """Keep every planned item and separate terminal accounting from science."""
    plan = _load(runtime, task_id)
    _validate_effects(runtime, plan)
    effects = {e['work_id']: e for e in runtime.store.list(runtime.run_id, 'effect') if _is_vina(e)}
    mapping = {cid: claim for claim in _claims(runtime, task_id) for cid in claim['inputs']['selected_ids']}
    observations, details = [], []
    for item in plan['items']:
        claim = mapping.get(item['item_id'])
        effect = effects.get(claim['work_id']) if claim else None
        status = 'unrun' if effect is None else effect['status']
        known_terminal = effect is not None and status in {'succeeded', 'failed_definite'} and not effect.get('accounting_unknown')
        receipt = json.loads(runtime.store.read_blob(effect['result_ref'])) if effect and effect.get('result_ref') else {}
        result = receipt.get('result', {})
        jobs = [job for job in result.get('jobs', []) if job['candidate_id'] == item['item_id']]
        scientific_completed = (known_terminal and status == 'succeeded'
                                and sorted(job['seed'] for job in jobs) == [17, 43]
                                and all(job['status'] == 'completed' for job in jobs))
        valid_pair = scientific_completed and all(job.get('pose_valid') is True for job in jobs)
        # Observation.status refers to completion of the coverage assessment;
        # scientific job failures remain unaltered in the per-item details.
        observations.append({'item_id': item['item_id'], 'identity': item['identity'], 'arm_id': ARM,
            'status': 'completed' if known_terminal else 'unrun' if effect is None else 'unknown',
            'values': {METRIC: {seed: 1 if known_terminal else None for seed in ('17', '43')}},
            'evidence_refs': [effect['result_ref']] if known_terminal else []})
        details.append({'item_id': item['item_id'], 'identity': item['identity'], 'effect_status': status,
            'effect_id': effect['id'] if effect else None, 'execution_disposition_terminal': known_terminal,
            'scientific_jobs_completed': scientific_completed, 'two_valid_poses': valid_pair,
            'jobs': jobs, 'batch_failure': result.get('error') if status == 'failed_definite' else None,
            'seed_dispositions': {str(seed): next((job['status'] for job in jobs if job['seed'] == seed),
                'not_started_or_unverified_due_to_definite_batch_failure' if known_terminal and status == 'failed_definite'
                else 'unrun' if effect is None else 'unknown') for seed in (17, 43)}})
    selection = plans.select_items(plan, observations, ARM)
    return {'schema_version': 'live_docking_coverage_report_v1', 'task_plan_id': task_id,
        'task_plan_hash': digest(plan), 'coverage': selection, 'items': details,
        'planned_items': len(details), 'terminal_items': sum(row['execution_disposition_terminal'] for row in details),
        'scientifically_completed_items': sum(row['scientific_jobs_completed'] for row in details),
        'two_valid_pose_items': sum(row['two_valid_poses'] for row in details),
        'formal_qualification': False, 'screening_benefit_evaluated': False,
        'interpretation': 'Coverage acceptance is only complete known execution disposition of the frozen arm; failures and missing work remain visible.'}
