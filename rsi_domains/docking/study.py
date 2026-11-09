"""Prospectively registered formal screening, independent of the v1 pilot schema.

The immutable plan is committed before any bound evaluation Effect. Selection
accepts only a study/arm identity, never a caller-supplied threshold or run list.
Native Vina evidence is re-read when settling and when exporting a list.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from rsi_lab import plans as task_plans


SCHEMA = 'docking_study_plan_v2'
# Historical archive-reader capability limits, never generic task requirements.
SUPPORTED_ARCHIVE_PROFILES = {
    'native_vina_jobs_v2': {'seeds': (17, 43), 'pose_rank': 1, 'requires_all_valid': True},
    'lab_vina_archive_v1': {'seeds': (17, 43), 'pose_rank': 1, 'requires_all_valid': True},
}
SEEDS = SUPPORTED_ARCHIVE_PROFILES['native_vina_jobs_v2']['seeds']  # legacy API alias
PLAN_FIELDS = {'schema_version', 'study_id', 'phase', 'compound_registry', 'identity_rule',
               'scientific_protocol', 'tool_versions', 'arms', 'threshold', 'direction',
               'min_compounds', 'score', 'budget', 'stop_rule', 'missing_rule'}
IDENTITY_FIELDS = {'compound_id', 'parent_identity', 'prepared_smiles', 'prepared_state_id'}
SHARD_FIELDS = {'shard_id', 'compound_ids', 'archive_path', 'archive_adapter', 'protocol_sha256'}
ARM_FIELDS = {'arm_id', 'policy_version', 'policy_sha256', 'shards'}
ADAPTERS = set(SUPPORTED_ARCHIVE_PROFILES)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f'{name} requires a nonempty exact string')


def _id(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}', value):
        raise ValueError(f'Invalid {name}')


def _sha(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError(f'Invalid {name} SHA256')


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f'{name} requires exactly {sorted(fields)}')


def validate_plan(plan, *, check_future_paths=False):
    """Validate a complete partition; no 20-candidate or shortlist restriction."""
    _exact(plan, PLAN_FIELDS, 'StudyPlan')
    if plan['schema_version'] != SCHEMA or plan['phase'] != 'formal':
        raise ValueError('Expected new formal StudyPlan schema; legacy threshold + runs is not admission')
    _id(plan['study_id'], 'study_id')
    if (type(plan['threshold']) not in (float, int) or not math.isfinite(plan['threshold'])
            or not isinstance(plan['direction'], str) or plan['direction'] not in task_plans.DIRECTIONS
            or type(plan['min_compounds']) is not int
            or plan['min_compounds'] < 1):
        raise ValueError('Formal selection requires a finite threshold, explicit direction and positive task-specific minimum')
    if (not isinstance(plan['score'], dict) or set(plan['score']) != {'name', 'units', 'seeds', 'pose_rank', 'requires_both_valid'}
            or plan['score']['name'] != 'qualified_median_vina_affinity' or plan['score']['units'] != 'kcal/mol'
            or not isinstance(plan['score']['seeds'], list) or not plan['score']['seeds']
            or type(plan['score'].get('pose_rank')) is not int
            or plan['score'].get('requires_both_valid') is not True
            or any(type(seed) is not int for seed in plan['score']['seeds'])):
        raise ValueError('Unsupported score/pose/seed qualification contract')
    registry = plan['compound_registry']
    if not isinstance(registry, list) or not registry:
        raise ValueError('A frozen compound library is required')
    ids, parents, prepared = set(), set(), set()
    for row in registry:
        _exact(row, IDENTITY_FIELDS, 'Compound identity')
        _id(row['compound_id'], 'compound_id')
        for key in IDENTITY_FIELDS - {'compound_id'}:
            _text(row[key], key)
        if row['compound_id'] in ids or row['parent_identity'] in parents or row['prepared_smiles'] in prepared:
            raise ValueError('Compound IDs, registered parents and prepared structures must each be unique')
        ids.add(row['compound_id']); parents.add(row['parent_identity']); prepared.add(row['prepared_smiles'])
    rule = plan['identity_rule']
    _exact(rule, {'name', 'version', 'registry_sha256'}, 'Identity rule')
    if rule['name'] != 'registered_parent_identity':
        raise ValueError('Unsupported compound identity rule')
    _text(rule['version'], 'identity version')
    if rule['registry_sha256'] != digest(registry):
        raise ValueError('Identity rule does not bind the complete registry')
    scientific = plan['scientific_protocol']
    _exact(scientific, {'version', 'profile_sha256', 'preparation_sha256', 'evaluation_sha256'}, 'Scientific protocol')
    _text(scientific['version'], 'scientific protocol version')
    for key in ('profile_sha256', 'preparation_sha256', 'evaluation_sha256'):
        _sha(scientific[key], key)
    if not isinstance(plan['tool_versions'], dict) or not plan['tool_versions']:
        raise ValueError('Tool capability versions must be frozen separately from policy/science')
    for name, value in plan['tool_versions'].items():
        _text(name, 'tool name'); _text(value, 'tool version')
    if not isinstance(plan['arms'], list) or not plan['arms']:
        raise ValueError('All experimental arms must be registered')
    arm_ids, paths = set(), []
    for arm in plan['arms']:
        _exact(arm, ARM_FIELDS, 'Experimental arm')
        _id(arm['arm_id'], 'arm_id'); _text(arm['policy_version'], 'policy_version')
        _sha(arm['policy_sha256'], 'policy')
        if arm['arm_id'] in arm_ids or not isinstance(arm['shards'], list) or not arm['shards']:
            raise ValueError('Duplicate or empty arm')
        arm_ids.add(arm['arm_id'])
        shard_ids, assigned = set(), set()
        for shard in arm['shards']:
            _exact(shard, SHARD_FIELDS, 'Planned shard')
            _id(shard['shard_id'], 'shard_id')
            if shard['shard_id'] in shard_ids or shard['archive_adapter'] not in ADAPTERS:
                raise ValueError('Duplicate shard or unsupported raw archive adapter')
            supported = SUPPORTED_ARCHIVE_PROFILES[shard['archive_adapter']]
            if tuple(plan['score']['seeds']) != supported['seeds'] or plan['score']['pose_rank'] != supported['pose_rank']:
                raise ValueError('Selected historical archive adapter cannot verify the requested seed/pose protocol; register another capability')
            shard_ids.add(shard['shard_id'])
            _sha(shard['protocol_sha256'], 'shard protocol')
            members = shard['compound_ids']
            if (not isinstance(members, list) or not members or len(set(members)) != len(members)
                    or not set(members) <= ids or assigned & set(members)):
                raise ValueError('Shard identities overlap, duplicate, or leave the frozen library')
            assigned.update(members)
            _text(shard['archive_path'], 'archive_path')
            path = Path(shard['archive_path'])
            if not path.is_absolute() or str(path.resolve()) != str(path):
                raise ValueError('Future archive paths must be absolute and canonical')
            if check_future_paths and path.exists():
                raise ValueError('Formal plan must precede every output archive; retrospective import is forbidden')
            if any(path == old or path.is_relative_to(old) or old.is_relative_to(path) for old in paths):
                raise ValueError('Experimental arms/shards must have disjoint output paths')
            paths.append(path)
        if assigned != ids:
            raise ValueError('Every arm must explicitly partition the entire frozen compound library')
    if not isinstance(plan['budget'], dict) or not plan['budget']:
        raise ValueError('A prospective finite resource budget is required')
    for name, amount in plan['budget'].items():
        _text(name, 'resource unit')
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
            raise ValueError('Invalid resource budget')
    expected_jobs = len(ids) * len(plan['score']['seeds']) * len(plan['arms'])
    if type(plan['budget'].get('seed_jobs')) is not int or plan['budget']['seed_jobs'] < expected_jobs:
        raise ValueError('Registered seed-job budget cannot cover every planned arm/shard')
    if plan['stop_rule'] != 'all_planned_shards_terminal_or_explicit_stop':
        raise ValueError('Unsupported stopping rule')
    if plan['missing_rule'] != 'retain_all_missing_no_imputation_no_replacement':
        raise ValueError('Missing/failed/unstarted shards must remain in the plan')
    task_plans.validate_task_plan(_task_plan(plan), check_future_paths=check_future_paths)
    return json.loads(encoded(plan))


def _task_plan(plan, goal=None):
    """Translate domain identities and metric semantics into the common contract."""
    return {'schema_version': task_plans.SCHEMA, 'task_id': plan['study_id'],
        'goal': goal or 'Evaluate the frozen library under the registered selection and completion criteria',
        'inputs': [{'input_id': 'registered_library', 'sha256': plan['identity_rule']['registry_sha256']}] +
            [{'input_id': 'protocol:' + arm['arm_id'] + ':' + shard['shard_id'], 'sha256': shard['protocol_sha256']}
             for arm in plan['arms'] for shard in arm['shards']],
        'items': [{'item_id': row['compound_id'], 'identity': row['parent_identity']} for row in plan['compound_registry']],
        'protocol': {'version': plan['scientific_protocol']['version'],
            'parameters': {'scientific_contract': plan['scientific_protocol'], 'identity_rule': plan['identity_rule'],
                           'qualification': plan['score']}},
        'capability_versions': plan['tool_versions'],
        'metrics': [{'name': plan['score']['name'], 'units': plan['score']['units'], 'direction': plan['direction'],
            'threshold': plan['threshold'], 'aggregation': 'median', 'replicates': [str(seed) for seed in plan['score']['seeds']]}],
        'success': {'metric': plan['score']['name'], 'min_qualified_items': plan['min_compounds']},
        'arms': [{'arm_id': arm['arm_id'], 'policy_version': arm['policy_version'], 'policy_sha256': arm['policy_sha256'],
            'shards': [{'shard_id': shard['shard_id'], 'item_ids': shard['compound_ids'], 'output_path': shard['archive_path'],
                       'adapter_id': shard['archive_adapter'], 'protocol_sha256': shard['protocol_sha256']} for shard in arm['shards']]}
            for arm in plan['arms']], 'budget': plan['budget'], 'stop_rule': plan['stop_rule'], 'missing_rule': plan['missing_rule']}


def to_task_plan(plan, *, goal=None):
    return task_plans.validate_task_plan(_task_plan(validate_plan(plan), goal=goal))


def _shard_key(study_id, arm_id, shard_id):
    return f'{study_id}/{arm_id}/{shard_id}'


def register_study(store, run_id, plan, *, key):
    """Commit the immutable plan and *all* planned shard projections atomically."""
    plan = validate_plan(plan)
    plan_hash = digest(plan)
    def apply(tx):
        validate_plan(plan, check_future_paths=True)
        # Repeat commands are handled by Store's operation key, never by replacing a plan.
        if tx.find('study', study_id=plan['study_id']):
            raise ValueError('Study already registered; scientific changes require a new study')
        if any(e.get('metadata', {}).get('study_binding', {}).get('study_id') == plan['study_id']
               or e.get('metadata', {}).get('study_id') == plan['study_id'] for e in tx.find('effect')):
            raise ValueError('Study registration cannot follow one of its evaluation Effects')
        task = task_plans.freeze_task_plan(tx, _task_plan(plan, goal=store.run(run_id)['config'].get('goal')))
        blob = tx.blob(plan)
        study = tx.insert('study', plan['study_id'], {'study_id': plan['study_id'], 'status': 'registered',
            'plan_hash': plan_hash, 'plan_ref': blob, 'task_plan_ref': task['plan_ref'], 'phase': 'formal', 'arm_ids': [a['arm_id'] for a in plan['arms']]})
        for arm in plan['arms']:
            for shard in arm['shards']:
                tx.insert('shard', _shard_key(plan['study_id'], arm['arm_id'], shard['shard_id']), {
                    'study_id': plan['study_id'], 'arm_id': arm['arm_id'], 'shard_id': shard['shard_id'],
                    'status': 'planned', 'plan_hash': plan_hash, 'effect_id': None, 'archive_receipt': None})
        return study
    return store.mutate(run_id, key, 'study.registered', apply, payload={'plan': plan})


def _load(store, run_id, study_id):
    study = store.get(run_id, 'study', study_id)
    plan = json.loads(store.read_blob(study['plan_ref']))
    validate_plan(plan)
    if digest(plan) != study['plan_hash'] or plan['study_id'] != study_id:
        raise ValueError('Immutable StudyPlan identity changed')
    if study.get('task_plan_ref'):
        common = task_plans.load_task_plan(store, run_id, study_id)
        expected_common = _task_plan(plan, goal=store.run(run_id)['config'].get('goal'))
        if common != expected_common or task_plans.digest(common) != study['task_plan_ref']:
            raise ValueError('Domain plan differs from its frozen common TaskPlan')
    expected = {_shard_key(study_id, arm['arm_id'], shard['shard_id']) for arm in plan['arms'] for shard in arm['shards']}
    actual = store.list(run_id, 'shard', study_id=study_id)
    if {row['id'] for row in actual} != expected or len(actual) != len(expected):
        raise ValueError('Registered shard ledger omits or adds planned runs')
    return study, plan


def _locate(plan, arm_id, shard_id):
    arms = [a for a in plan['arms'] if a['arm_id'] == arm_id]
    if not arms:
        raise ValueError('Arm is outside the frozen study')
    shards = [s for s in arms[0]['shards'] if s['shard_id'] == shard_id]
    if not shards:
        raise ValueError('Shard is outside the frozen arm')
    return arms[0], shards[0]


def _dispatch_spec(study, plan, arm_id, shard_id):
    arm, shard = _locate(plan, arm_id, shard_id)
    return {'study_id': plan['study_id'], 'plan_hash': study['plan_hash'], 'arm_id': arm_id,
            'shard_id': shard_id, 'phase': 'formal', 'archive_path': shard['archive_path'],
            'archive_adapter': shard['archive_adapter'], 'compound_ids': shard['compound_ids'],
            'scientific_protocol': plan['scientific_protocol'], 'protocol_sha256': shard['protocol_sha256'],
            'policy_version': arm['policy_version'], 'policy_sha256': arm['policy_sha256'],
            'tool_versions': plan['tool_versions']}


def dispatch_spec(store, run_id, study_id, arm_id, shard_id):
    study, plan = _load(store, run_id, study_id)
    if study['status'] != 'registered':
        raise ValueError('Stopped studies cannot schedule new formal execution')
    return _dispatch_spec(study, plan, arm_id, shard_id)


def _verify_effect_binding(study, plan, state, effect):
    expected = _dispatch_spec(study, plan, state['arm_id'], state['shard_id'])
    if (effect['id'] != state['effect_id'] or effect['created_seq'] <= study['created_seq']
            or state.get('dispatch_binding') != expected
            or effect.get('metadata', {}).get('study_binding') != expected
            or effect.get('packet_hash') != state.get('effect_packet_hash')):
        raise ValueError('Effect packet or prospective study binding changed after reservation')


def bind_shard(store, run_id, study_id, arm_id, shard_id, effect_id, *, key):
    """Link a persisted, not-yet-dispatched Effect to its prospective shard."""
    study, plan = _load(store, run_id, study_id)
    _, planned = _locate(plan, arm_id, shard_id)
    spec = dispatch_spec(store, run_id, study_id, arm_id, shard_id)
    def apply(tx):
        current = tx.get('shard', _shard_key(study_id, arm_id, shard_id))
        effect = tx.get('effect', effect_id)
        if tx.get('study', study_id)['status'] != 'registered':
            raise ValueError('Study stopped before execution admission')
        if current['status'] != 'planned' or current['effect_id'] is not None:
            raise ValueError('Shard already bound; no retry/replacement after an attempted evaluation')
        if (effect['status'] != 'prepared' or effect.get('metadata', {}).get('study_binding') != spec
                or effect['created_seq'] <= study['created_seq']):
            raise ValueError('Effect must be prepared after registration and bind the exact study/arm/shard')
        if Path(planned['archive_path']).exists():
            raise ValueError('Shard output already exists before execution admission')
        if any(row.get('effect_id') == effect_id for row in tx.find('shard')):
            raise ValueError('One Effect cannot be attributed to multiple shards or arms')
        return tx.update('shard', current['id'], {'status': 'reserved', 'effect_id': effect_id,
                         'effect_packet_hash': effect['packet_hash'], 'dispatch_binding': spec}, expected_version=current['version'])
    return store.mutate(run_id, key, 'study.shard_bound', apply, payload={
        'study_id': study_id, 'arm_id': arm_id, 'shard_id': shard_id, 'effect_id': effect_id, 'binding': spec})


def protocol_contract(protocol, manifest):
    """Compute semantic hashes from actual inputs, not from a claimed version label."""
    from lab_harness.meta import scientific_protocol
    scientific = scientific_protocol(protocol)
    receptor = scientific['receptor_input_sha256']
    evaluation = {key: protocol.get(key) for key in ('engine', 'vina_sha256', 'vina_version', 'scoring',
                  'seeds', 'center', 'size', 'exhaustiveness', 'num_modes', 'cpu', 'posebusters_configs',
                  'versions', 'score_semantics', 'runner_sha256', 'adapter_sha256', 'contract_sha256')}
    preparation = {'receptor': receptor, 'chemical_preparation': protocol.get('chemical_preparation'),
                   'geometry_preparation': protocol.get('preparation')}
    return {'profile_sha256': digest({'scientific': scientific, 'target': manifest.get('target_chembl_id'),
                                      'construct': manifest.get('construct_id')}),
            'preparation_sha256': digest(preparation), 'evaluation_sha256': digest(evaluation)}


def execution_binding(store, run_id, study_id, arm_id, shard_id):
    """Executor persists this exact envelope alongside its native output files."""
    study, plan = _load(store, run_id, study_id)
    if study['status'] != 'registered':
        raise ValueError('Stopped studies cannot authorize new native execution')
    shard = store.get(run_id, 'shard', _shard_key(study_id, arm_id, shard_id))
    if shard['status'] != 'reserved' or not shard['effect_id']:
        raise ValueError('Native execution requires a registered, reserved shard')
    effect = store.get(run_id, 'effect', shard['effect_id'])
    if effect['status'] not in ('prepared', 'dispatching'):
        raise ValueError('No new execution is authorized for settled or unknown Effects')
    _verify_effect_binding(study, plan, shard, effect)
    return {'effect_id': effect['id'], 'effect_packet_hash': effect['packet_hash'],
            'binding': shard['dispatch_binding']}


def _verify_manifest(root):
    root = Path(root).resolve()
    path = root / 'artifact_hashes.json'
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('Native archive requires a nonempty file hash manifest')
    actual = {str(p.relative_to(root)) for p in root.rglob('*')
              if p.is_file() and p.name not in ('artifact_hashes.json', '.lock')}
    if set(manifest) != actual:
        raise ValueError('Archive manifest does not cover all raw artifact files')
    for relative, expected in manifest.items():
        _sha(expected, 'artifact')
        item = root / relative
        if (not isinstance(relative, str) or Path(relative).is_absolute() or item.is_symlink()
                or not item.resolve().is_relative_to(root) or not item.is_file() or file_hash(item) != expected):
            raise ValueError(f'Raw archive artifact changed or escaped: {relative}')
    return manifest, file_hash(path)


def _identity_rows(plan, shard):
    by_id = {row['compound_id']: row for row in plan['compound_registry']}
    return {cid: by_id[cid] for cid in shard['compound_ids']}


def _read_native_jobs(root, plan, shard):
    from scripts.report_vina_screen import inspect_job, check_frozen_sources
    from scripts.vina_contract import validate_vina_protocol
    from scripts.run_vina_batch import load_chemical_states, input_hashes
    protocol = json.loads((root / 'protocol.json').read_text())
    validate_vina_protocol(protocol, live=True)
    if protocol.get('phase') != 'formal':
        raise ValueError('Native formal execution requires a prospectively frozen formal phase')
    if file_hash(root / 'protocol.json') != shard['protocol_sha256']:
        raise ValueError('Raw shard protocol differs from prospective registration')
    if not (root / 'frozen').is_dir():
        raise ValueError('Native formal archive requires frozen worker source evidence')
    check_frozen_sources(root, shard, protocol)
    data = root / 'data'
    manifest = json.loads((data / 'manifest.json').read_text())
    if manifest.get('synthetic') is True or manifest.get('is_mock') is not False:
        raise ValueError('Synthetic input cannot enter formal screening')
    if protocol_contract(protocol, manifest) != {k: plan['scientific_protocol'][k]
                                                for k in ('profile_sha256', 'preparation_sha256', 'evaluation_sha256')}:
        raise ValueError('Raw shard uses a different scientific protocol')
    if protocol['input_sha256'] != input_hashes(data):
        raise ValueError('Native shard inputs differ from complete frozen protocol')
    rows = json.loads((data / 'candidates.json').read_text())['candidates']
    method, _ = load_chemical_states(data, candidates=rows, manifest=manifest)
    if protocol.get('chemical_preparation') != {'method': method} or method is None:
        raise ValueError('Native chemical preparation differs from the frozen scientific protocol')
    expected = _identity_rows(plan, shard)
    if len(rows) != len(expected) or {row['candidate_id'] for row in rows} != set(expected):
        raise ValueError('Native shard input pool differs from exact prospective partition')
    seeds = plan['score']['seeds']
    if protocol['seeds'] != seeds:
        raise ValueError('Native evaluation replicates differ from the frozen task')
    expected_folders = {f'{cid}_s{seed}' for cid in expected for seed in seeds}
    if (root / 'jobs').exists() and any(p.name not in expected_folders for p in (root / 'jobs').iterdir()):
        raise ValueError('Native shard contains unregistered executions')
    result = []
    for row in rows:
        identity = expected[row['candidate_id']]
        if (row.get('canonical_parent_identity', row.get('canonical_smiles')) != identity['parent_identity']
                or row['smiles'] != identity['prepared_smiles']
                or row.get('prepared_state_id') != identity['prepared_state_id']):
            raise ValueError('Native shard parent/prepared identity differs from registered library')
        jobs = [inspect_job(root, row, seed, protocol) for seed in seeds]
        result.append({'compound_id': row['candidate_id'], 'parent_identity': identity['parent_identity'],
                       'prepared_smiles': identity['prepared_smiles'], 'prepared_state_id': identity['prepared_state_id'],
                       'seed_results': [{'seed': seed, 'status': job['status'], 'pose_valid': job.get('pose_valid'),
                                         'identity_preserved': job.get('identity_preserved'),
                                         'energy': job.get('energy')} for seed, job in zip(seeds, jobs)]})
    return result


def _read_lab_archive(root, plan, shard):
    """Compatibility reader only: legacy pilots are not retroactively registered."""
    from lab_harness.docking_archive import read_docking_run
    observations, source = read_docking_run(root)
    state = json.loads((root / 'state.json').read_text())
    protocol = json.loads((root / 'frozen/protocol.json').read_text())
    manifest = json.loads((root / 'frozen/manifest.json').read_text())
    if state['phase'] != 'final' or source['protocol_sha256'] != shard['protocol_sha256']:
        raise ValueError('A development or validation pilot cannot become a formal archive')
    if protocol['seeds'] != plan['score']['seeds']:
        raise ValueError('Legacy evaluation replicates differ from the frozen task')
    if protocol_contract(protocol, manifest) != {k: plan['scientific_protocol'][k]
                                                for k in ('profile_sha256', 'preparation_sha256', 'evaluation_sha256')}:
        raise ValueError('Legacy archive scientific protocol differs from registered study')
    expected = _identity_rows(plan, shard)
    if {r['candidate_id'] for r in state['candidates']} != set(expected):
        raise ValueError('Legacy archive pool differs from the full planned partition')
    out = []
    for row in observations:
        cid = row['compound_id']; identity = expected[cid]
        if (row.get('canonical_parent_identity', row['canonical_smiles']) != identity['parent_identity']
                or row['canonical_smiles'] != identity['prepared_smiles']
                or row.get('prepared_state_id') != identity['prepared_state_id']):
            raise ValueError('Legacy prepared/parent identity differs from formal registry')
        out.append({'compound_id': cid, **{k: identity[k] for k in ('parent_identity', 'prepared_smiles', 'prepared_state_id')},
                    'seed_results': [{'seed': seed, 'status': status, 'pose_valid': valid,
                                      'identity_preserved': valid is True, 'energy': energy}
                                     for seed, status, valid, energy in row['seed_results']]})
    return out


def _read_archive(root, plan, arm, shard, binding):
    """This is the only production source of observations; score-table ingestion is absent."""
    root = Path(root)
    manifest, archive_hash = _verify_manifest(root)
    if 'study_binding.json' not in manifest or json.loads((root / 'study_binding.json').read_text()) != binding:
        raise ValueError('Archive lacks its exact prospective Effect/packet/study binding')
    if binding['binding']['policy_sha256'] != arm['policy_sha256']:
        raise ValueError('Archive was assigned to a different experimental policy arm')
    if shard['archive_adapter'] == 'lab_vina_archive_v1':
        state = json.loads((root / 'state.json').read_text())
        if state.get('harness_hash') != arm['policy_sha256']:
            raise ValueError('Legacy executed policy differs from the prospective experimental arm')
    reader = _read_native_jobs if shard['archive_adapter'] == 'native_vina_jobs_v2' else _read_lab_archive
    rows = reader(root, plan, shard)
    return rows, {'archive_path': str(root), 'archive_sha256': archive_hash,
                  'adapter': shard['archive_adapter'], 'observations_sha256': digest(rows)}


def _normalize(rows, expected, *, seeds=SEEDS):
    observed = {}
    for row in rows:
        cid = row['compound_id']
        if cid not in expected or cid in observed:
            raise ValueError('Archive has an extra or duplicate compound observation')
        identity = expected[cid]
        if any(row.get(k) != identity[k] for k in ('parent_identity', 'prepared_smiles', 'prepared_state_id')):
            raise ValueError('Observed compound identity differs from registered library')
        jobs = row['seed_results']
        if (not isinstance(jobs, list) or [j.get('seed') for j in jobs] != list(seeds)
                or any(type(j.get('seed')) is not int for j in jobs)):
            raise ValueError('Every observation must explicitly account for both frozen seeds')
        for job in jobs:
            if job.get('energy') is not None and (type(job['energy']) not in (int, float) or not math.isfinite(job['energy'])):
                raise ValueError('Invalid native energy')
        qualified = all(j.get('status') == 'completed' and j.get('pose_valid') is True
                        and j.get('identity_preserved') is True and j.get('energy') is not None for j in jobs)
        unresolved = any(j.get('status') in ('unrun', 'pending_or_interrupted', 'unknown') for j in jobs)
        metric = {'name': 'qualified_median_vina_affinity', 'units': 'kcal/mol', 'direction': 'lower_or_equal',
                  'threshold': 0, 'aggregation': 'median', 'replicates': [str(seed) for seed in seeds]}
        score = task_plans.aggregate_metric(metric, {str(j['seed']): j['energy'] for j in jobs}) if qualified else None
        observed[cid] = {**identity, 'score': score,
                         'status': 'qualified_measurement' if qualified else 'unresolved' if unresolved else 'failed_or_invalid',
                         'seed_results': jobs}
    return [observed.get(cid, {**identity, 'score': None, 'status': 'unrun', 'seed_results': []})
            for cid, identity in expected.items()]


def _execution_receipt(effect):
    # Cost reconciliation may replace receipt_ref; result_ref retains the
    # original execution result. Older effects only have receipt_ref.
    return effect.get('result_ref') or effect.get('receipt_ref')


def reconcile_shard(store, run_id, study_id, arm_id, shard_id, *, key):
    study, plan = _load(store, run_id, study_id)
    arm, planned = _locate(plan, arm_id, shard_id)
    state = store.get(run_id, 'shard', _shard_key(study_id, arm_id, shard_id))
    if state['status'] not in ('reserved', 'completed', 'incomplete', 'failed'):
        raise ValueError('Only a reserved shard can be reconciled; no replacement or duplicate settlement')
    effect = store.get(run_id, 'effect', state['effect_id'])
    _verify_effect_binding(study, plan, state, effect)
    if effect['status'] in ('prepared', 'dispatching', 'unknown'):
        return {'status': 'unresolved', 'effect_status': effect['status'], 'shard_id': state['id']}
    receipt_ref = _execution_receipt(effect)
    if effect['status'] not in ('succeeded', 'failed_definite') or not receipt_ref:
        raise ValueError('Shard requires an authenticated terminal Effect receipt')
    receipt = json.loads(store.read_blob(receipt_ref))
    result = receipt.get('result', receipt)
    binding = {'effect_id': effect['id'], 'effect_packet_hash': state['effect_packet_hash'],
               'binding': state['dispatch_binding']}
    if effect['status'] == 'succeeded':
        observations, archive = _read_archive(planned['archive_path'], plan, arm, planned, binding)
        if (not isinstance(result, dict) or result.get('archive_path') != planned['archive_path']
                or result.get('archive_sha256') != archive['archive_sha256']
                or result.get('study_binding') != state['dispatch_binding']):
            raise ValueError('Native archive is not the one returned by the bound Effect')
        rows = _normalize(observations, _identity_rows(plan, planned), seeds=plan['score']['seeds'])
        status = 'incomplete' if any(r['status'] in ('unresolved', 'unrun') for r in rows) else 'completed'
    else:
        rows = _normalize([], _identity_rows(plan, planned), seeds=plan['score']['seeds'])
        for row in rows:
            row['status'] = 'execution_failed'
        archive = None
        status = 'failed'
    def apply(tx):
        current = tx.get('shard', state['id'])
        current_effect = tx.get('effect', effect['id'])
        if (current['status'] != 'reserved' or current['version'] != state['version'] or current_effect['version'] != effect['version']
                or _execution_receipt(current_effect) != receipt_ref):
            raise ValueError('Shard or Effect changed while evidence was being verified')
        return tx.update('shard', state['id'], {'status': status, 'archive_receipt': archive,
            'effect_receipt_ref': receipt_ref, 'observations_ref': tx.blob(rows)}, expected_version=state['version'])
    return store.mutate(run_id, key, 'study.shard_reconciled', apply,
                        payload={'shard_id': state['id'], 'effect_id': effect['id'], 'receipt_ref': receipt_ref,
                                 'archive': archive, 'observations_sha256': digest(rows)})


def select_study(store, run_id, study_id, arm_id):
    """Export every threshold-qualified parent from one immutable planned arm.

    Missing runs are represented explicitly. Reaching the task's item minimum
    cannot turn an incomplete arm into an accepted formal list.
    """
    history = store.verify_history(run_id)
    if store.run(run_id)['config'].get('execution_mode') != 'live':
        raise ValueError('Replay or unspecified execution mode cannot export formal screening results')
    study, plan = _load(store, run_id, study_id)
    arms = [arm for arm in plan['arms'] if arm['arm_id'] == arm_id]
    if not arms:
        raise ValueError('Cannot combine or substitute study arms')
    arm = arms[0]
    observations, ledger = [], []
    for planned in arm['shards']:
        state = store.get(run_id, 'shard', _shard_key(study_id, arm_id, planned['shard_id']))
        if state['plan_hash'] != study['plan_hash']:
            raise ValueError('Shard is linked to a different immutable StudyPlan')
        expected = _identity_rows(plan, planned)
        if state['status'] in ('completed', 'incomplete'):
            effect = store.get(run_id, 'effect', state['effect_id'])
            _verify_effect_binding(study, plan, state, effect)
            if effect['status'] != 'succeeded' or _execution_receipt(effect) != state['effect_receipt_ref']:
                raise ValueError('Previously settled Effect no longer matches its raw receipt')
            raw, archive = _read_archive(planned['archive_path'], plan, arm, planned,
                {'effect_id': effect['id'], 'effect_packet_hash': state['effect_packet_hash'], 'binding': state['dispatch_binding']})
            rows = _normalize(raw, expected, seeds=plan['score']['seeds'])
            if archive != state['archive_receipt'] or rows != json.loads(store.read_blob(state['observations_ref'])):
                raise ValueError('Settled native observations changed; no post-hoc replacement')
        elif state['status'] == 'failed':
            effect = store.get(run_id, 'effect', state['effect_id'])
            _verify_effect_binding(study, plan, state, effect)
            if effect['status'] != 'failed_definite' or _execution_receipt(effect) != state['effect_receipt_ref']:
                raise ValueError('Failed shard lacks its retained failure receipt')
            rows = json.loads(store.read_blob(state['observations_ref']))
        elif state['status'] in ('planned', 'reserved'):
            rows = _normalize([], expected, seeds=plan['score']['seeds'])
        else:
            raise ValueError('Unknown shard lifecycle state')
        observations.extend({**row, 'shard_id': planned['shard_id']} for row in rows)
        ledger.append({'shard_id': planned['shard_id'], 'status': state['status'], 'effect_id': state['effect_id'],
                       'expected_compounds': len(expected), 'archive_receipt': state['archive_receipt']})
    if len(observations) != len(plan['compound_registry']):
        raise ValueError('Formal arm no longer accounts for the complete registered library')
    common = _task_plan(plan, goal=store.run(run_id)['config'].get('goal'))
    evidence = {row['shard_id']: row['archive_receipt']['archive_sha256'] for row in ledger if row['archive_receipt']}
    generic_rows = []
    for row in observations:
        status_map = {'qualified_measurement': 'completed', 'unresolved': 'unknown', 'unrun': 'unrun',
                      'failed_or_invalid': 'failed', 'execution_failed': 'failed'}
        values = {str(seed): None for seed in plan['score']['seeds']}
        if row['score'] is not None:
            values = {str(job['seed']): job['energy'] for job in row['seed_results']}
        generic_rows.append({'item_id': row['compound_id'], 'identity': row['parent_identity'], 'arm_id': arm_id,
            'status': status_map[row['status']], 'values': {plan['score']['name']: values},
            'evidence_refs': [evidence[row['shard_id']]] if row['shard_id'] in evidence else []})
    selection = task_plans.select_items(common, generic_rows, arm_id)
    by_id = {row['compound_id']: row for row in observations}
    selected = [by_id[row['item_id']] for row in selection['items']]
    unresolved = any(row['status'] in ('planned', 'reserved', 'incomplete') for row in ledger)
    count = len(selected)
    status = 'incomplete' if unresolved else {'insufficient_items': 'insufficient_compounds'}.get(selection['status'], selection['status'])
    if store.verify_history(run_id) != history:
        raise ValueError('Ledger changed during selection; repeat the read-only snapshot')
    return {'schema_version': 'docking_study_selection_v2', 'study_id': study_id, 'run_id': run_id,
            'arm_id': arm_id, 'phase': 'formal', 'plan_hash': study['plan_hash'], 'threshold': plan['threshold'],
            'direction': plan['direction'], 'score': plan['score'], 'min_compounds': plan['min_compounds'],
            'status': status, 'formal_contract_satisfied': status == 'accepted', 'count': count,
            'shortfall': max(0, plan['min_compounds'] - count), 'missing_count': sum(r['score'] is None for r in observations),
            'shards': ledger, 'compounds': selected, 'all_observations': observations,
            'scope': 'Prospective computational threshold contract; no biological efficacy or method calibration claim',
            'historical_compatibility': 'Existing v1 artifacts are read-only history and cannot be registered retrospectively'}


def stop_study(store, run_id, study_id, reason, *, key):
    """Stop future dispatch without deleting planned work or unresolved Effects."""
    _text(reason, 'stop reason')
    _load(store, run_id, study_id)
    def apply(tx):
        study = tx.get('study', study_id)
        if study['status'] != 'registered':
            raise ValueError('Study is already stopped')
        return tx.update('study', study_id, {'status': 'stopped', 'stop_reason': reason},
                         expected_version=study['version'])
    return store.mutate(run_id, key, 'study.stopped', apply, payload={'study_id': study_id, 'reason': reason})
