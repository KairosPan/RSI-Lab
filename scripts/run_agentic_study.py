#!/usr/bin/env python3
"""Operate one real Owner/Inspector -> Lab -> Meta -> validation -> Lab cycle."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from lab_harness import controller as c, intake, meta, meta_cycle, versions
from lab_harness.autonomous import run_campaign
from lab_harness.meta_cycle import execute_cycle
import run_vina_batch as worker_adapter

COHORTS = ('development_01', 'validation_01', 'validation_02', 'development_02')
CHECKPOINTS = {
    'before_docking': 'controller.init/assert_frozen',
    'before_validation': 'meta.plan/workspace',
    'before_promotion': 'meta.gate/completed_run',
    'before_next_development': 'selected_policy.verify',
}


def write_new(path, value):
    with Path(path).open('x') as stream:
        stream.write(c.encoded(value).decode())


def runtime_hash():
    return c.canonical_hash({name: c.sha(path) for name, path in c.protected_sources().items()})


def verify_development_identity(path, *, data, protocol, harness, model, effort, expected_runtime):
    """A valid old archive is not necessarily the campaign this study requested."""
    state = c.read(Path(path) / 'state.json')
    c.assert_frozen(Path(path), state)
    expected = {'harness_hash': versions.load_harness(harness)['harness_hash'],
                'protocol_hash': c.sha(protocol), 'model': model, 'effort': effort,
                'phase': 'development', 'execution_mode': 'live', 'runtime_hash': expected_runtime}
    for key, value in expected.items():
        if state.get(key) != value:
            raise ValueError(f'Development campaign differs from requested {key}')
    if Path(state['source_data_path']).resolve() != Path(data).resolve():
        raise ValueError('Development campaign belongs to a different cohort data path')
    return state


def verify_checkpoint(root, readiness, artifacts, stage, **context):
    """Execute a real boundary check, then bind deferred findings to its evidence."""
    if stage not in CHECKPOINTS:
        raise ValueError('Unimplemented study checkpoint')
    intake.assert_guard_implementations(readiness, artifacts=artifacts,
                                       checkpoints=set(CHECKPOINTS.values()), stage=stage)
    evidence = {}
    if stage == 'before_docking':
        run = Path(context['run'])
        c.assert_frozen(run, c.read(run / 'state.json'))
        evidence[str(run / 'frozen/campaign.json')] = c.sha(run / 'frozen/campaign.json')
    elif stage == 'before_next_development':
        workspace, identity, _ = meta.workspace(context['workspace'])
        pointer = c.read(workspace / 'selected_policy.json')
        cycle = c.read(workspace / 'cycle.json')
        if (cycle.get('status') not in ('completed', 'no_change') or cycle.get('selected_policy') != pointer
                or meta_cycle._actual_meta_usage(workspace) != cycle.get('meta_usage')):
            raise ValueError('Handoff requires a completed, audited Meta cycle and its exact pointer')
        if versions.load_harness(pointer['harness'])['harness_hash'] != pointer['harness_hash']:
            raise ValueError('Handoff policy differs from frozen selected policy')
        if cycle['decision'] in ('accepted', 'rejected'):
            gate_path = Path(pointer['gate'])
            gate = c.read(gate_path)
            accepted = cycle['decision'] == 'accepted'
            if (c.sha(gate_path) != pointer['gate_sha256'] or c.sha(gate_path) != cycle['gate_sha256']
                    or gate['decision'] != cycle['decision'] or gate.get('synthetic') is not False
                    or gate['eligible_for_next_development_run'] is not accepted
                    or pointer.get('candidate_promoted') is not accepted
                    or pointer['harness_hash'] != identity['candidate_harness_hash' if accepted else 'parent_harness_hash']):
                raise ValueError('Handoff differs from actual live gate decision')
            evidence[str(gate_path)] = c.sha(gate_path)
        elif (cycle['decision'] != 'no_change' or cycle['status'] != 'no_change'
                or c.read(workspace / 'proposal.json').get('decision') != 'no_change'
                or pointer.get('candidate_promoted') is not False or pointer.get('gate') is not None
                or pointer['harness_hash'] != identity['parent_harness_hash']):
            raise ValueError('Parent handoff requires actual no-change decision or rejected gate')
        evidence[str(workspace / 'cycle.json')] = c.sha(workspace / 'cycle.json')
        evidence[str(workspace / 'selected_policy.json')] = c.sha(workspace / 'selected_policy.json')
        evidence[str(workspace / 'call_receipt.json')] = c.sha(workspace / 'call_receipt.json')
    else:
        workspace, identity, _ = meta.workspace(context['workspace'])
        registration = c.read(workspace / 'validation_plan.json')
        if (identity['validation_plan_sha256'] != c.sha(workspace / 'validation_plan.json')
                or len(registration['pairs']) < meta.DEFAULT_RULES['min_pairs']):
            raise ValueError('Checkpoint requires complete prospectively registered validation')
        evidence[str(workspace / 'validation_plan.json')] = c.sha(workspace / 'validation_plan.json')
        if stage == 'before_validation':
            if registration != context['registration']:
                raise ValueError('Validation checkpoint differs from actual registration')
            for policy in ('parent', 'candidate'):
                key = policy + '_harness_hash'
                if (registration.get(key) != identity[key]
                        or versions.load_harness(workspace / policy)['harness_hash'] != identity[key]):
                    raise ValueError('Validation policy differs from frozen registration')
            if registration.get('execution_mode') != 'live' or identity.get('execution_mode') != 'live':
                raise ValueError('Validation admission requires a live registration')
            if any(Path(row[arm]).exists() for row in registration['pairs'] for arm in ('baseline', 'candidate')):
                raise ValueError('Validation admission must precede every registered arm')
        else:
            assessment, gate_path = context['assessment'], Path(context['gate_path'])
            if (c.read(gate_path) != assessment or assessment.get('synthetic') is not False
                    or assessment['validation_plan_sha256'] != identity['validation_plan_sha256']
                    or assessment['decision'] not in ('accepted', 'rejected')
                    or assessment['eligible_for_next_development_run'] is not (assessment['decision'] == 'accepted')):
                raise ValueError('Promotion checkpoint differs from actual live gate')
            expected = {(row['baseline'], row['candidate']) for row in registration['pairs']}
            if (len(assessment['pairs']) != len(expected)
                    or {(row['baseline'], row['candidate']) for row in assessment['pairs']} != expected):
                raise ValueError('Promotion checkpoint requires every registered pair')
            for row in registration['pairs']:
                for arm in ('baseline', 'candidate'):
                    run, _, evaluation = meta.completed_run(row[arm], 'validation')
                    if evaluation.get('synthetic') is not False or not evaluation['llm_usage_accounting_complete']:
                        raise ValueError('Promotion checkpoint needs audited live validation')
                    evidence[str(run / 'artifact_hashes.json')] = c.sha(run / 'artifact_hashes.json')
            evidence[str(gate_path)] = c.sha(gate_path)
    receipts = []
    required_ids = {row['guard_id'] for row in readiness.get('deferred_obligations', []) if row['stage'] == stage}
    for gid, guard in readiness.get('guard_registry', {}).items():
        if gid in required_ids:
            if guard['checkpoint'] != CHECKPOINTS[stage]:
                raise ValueError('Registered guard uses the wrong implemented checkpoint')
            receipts.append({'guard_id': gid, 'stage': stage, 'checkpoint': CHECKPOINTS[stage],
                'status': 'passed', 'guard_registry_sha256': readiness['guard_registry_sha256'],
                'implementation': guard['implementation'], 'evidence': evidence})
    intake.assert_stage_ready(readiness, stage=stage, guard_receipts=receipts)
    folder = Path(root) / 'stage_receipts'
    folder.mkdir(exist_ok=True)
    write_new(folder / f'{len(list(folder.glob("*.json"))) + 1:03d}_{stage}.json',
              {'stage': stage, 'checked_at': c.now(), 'evidence': evidence, 'guard_receipts': receipts})


def run(root):
    root = Path(root).resolve()
    config = c.read(root / 'study_config.json')
    state_path = root / 'study_state.json'
    identity = {'config_sha256': c.sha(root / 'study_config.json'), 'driver_sha256': c.sha(__file__),
                'runtime_hash': runtime_hash()}
    if state_path.exists():
        state = c.read(state_path)
        if state['identity'] != identity:
            raise ValueError('Study configuration or driver changed; do not continue a frozen run')
        raise ValueError('Existing study state requires explicit reconciliation; summary-only automatic restart is disabled')
    else:
        state = {'schema_version': 'agentic_study_state_v1', 'created_at': c.now(),
                 'status': 'prepared', 'identity': identity, 'events': [], 'global_wall_time_limit_seconds': None}

    def record(status, **fields):
        state.update(status=status, **fields)
        state['events'].append({'at': c.now(), 'status': status})
        c.dump(state_path, state)
        print(json.dumps({'study_status': status, **fields}, ensure_ascii=False), flush=True)

    def campaign(name, harness):
        path = root / 'campaigns' / name
        data, protocol = root / 'data' / name, root / 'protocols' / (name + '.json')
        if runtime_hash() != identity['runtime_hash']:
            raise ValueError('Protected implementation changed since study startup')
        if not path.exists():
            c.init(SimpleNamespace(data=data, protocol=protocol,
                                  harness=harness, out=path, phase='development', execution_mode='live',
                                  model=config['model'], effort=config['effort']))
        verify_development_identity(path, data=data, protocol=protocol, harness=harness,
                                    model=config['model'], effort=config['effort'],
                                    expected_runtime=identity['runtime_hash'])
        verify_checkpoint(root, readiness, config['artifacts'], 'before_docking', run=path)
        result = run_campaign(path, vina=config['vina'], worker_python=config['worker_python'])
        if result['status'] != 'completed':
            raise RuntimeError(f'{name} halted: {result}')
        meta.completed_run(path, 'development')
        return path

    def check_plan(plan):
        if (plan['runtime_hash'] != identity['runtime_hash'] or runtime_hash() != identity['runtime_hash']
                or plan['initial_harness_hash'] != versions.load_harness(config['harness'])['harness_hash']
                or plan['intake_result_sha256'] != c.sha(root / 'intake/result.json')):
            raise ValueError('Study implementation, initial policy, or intake changed after registration')
        for name, expected in plan['cohorts'].items():
            protocol_path = root / 'protocols' / (name + '.json')
            if c.sha(protocol_path) != expected['protocol_sha256']:
                raise ValueError('Registered study protocol changed')
            protocol = c.read(protocol_path)
            if protocol['input_sha256'] != worker_adapter.input_hashes(root / 'data' / name):
                raise ValueError('Registered study data changed')

    try:
        intake_path = root / 'intake/result.json'
        if (root / 'intake').exists() or intake_path.exists():
            raise RuntimeError('Existing intake requires receipt reconciliation; summary-only reuse and implicit retry are disabled')
        elif (root / 'meta_01').exists() or (root / 'study_plan.json').exists():
            raise RuntimeError('Preexisting Meta or study plan requires reconciliation before new model calls')
        else:
            record('intake_running')
            readiness = intake.run_intake(goal=config['goal'], artifacts=config['artifacts'], out=root,
                                           model=config['model'], effort=config['effort'],
                                           max_calls=config['intake_max_calls'], guards=config.get('guards', {}),
                                           required_before_launch=config.get('required_before_launch', {}))
        if readiness['status'] != 'ready_for_exploratory':
            record(readiness['status'], intake_result=str(intake_path), questions=readiness.get('questions', []))
            return state
        if runtime_hash() != identity['runtime_hash']:
            raise ValueError('Protected implementation changed during intake')

        plan_path = root / 'study_plan.json'
        if not plan_path.exists():
            record('freezing_study')
            (root / 'protocols').mkdir(exist_ok=True)
            (root / 'campaigns').mkdir(exist_ok=True)
            cohorts = {}
            for name in COHORTS:
                data = root / 'data' / name
                context = {'scope': 'Exploratory computational agentic study; not formal biological validation',
                    'intake_status': readiness['status'], 'intake_result_sha256': c.sha(intake_path),
                    'formal_readiness': False, 'formal_min_distinct_compounds': 100,
                    'formal_threshold': None, 'findings': readiness['findings'],
                    'scientific_limitations': readiness['scientific_limitations'],
                    'source_assay_context': c.read(data / 'assay_details.json'),
                    'capabilities': c.read(root / 'intake_artifacts/capabilities.json'),
                    'scope_rule': 'Per-assay conditions remain local to their named assay; do not generalize them to this whole cohort.'}
                write_new(data / 'task_context.json', context)
                protocol_path = root / 'protocols' / (name + '.json')
                worker_adapter.freeze_protocol(SimpleNamespace(data=data, vina=config['vina'], out=protocol_path,
                    pose_config=ROOT / 'scripts/vina_pose_checks.json', protocol_id='agentic_cdk2_' + name))
                cohorts[name] = {'data': str(data), 'protocol': str(protocol_path),
                                 'protocol_sha256': c.sha(protocol_path), 'candidate_count': 20}
            plan = {'schema_version': 'agentic_study_plan_v1', 'registered_at': c.now(),
                    'model': config['model'], 'effort': config['effort'], 'global_wall_time_limit_seconds': None,
                    'max_meta_cycles': 1, 'max_lab_campaigns': 6, 'max_lab_model_attempts': 36,
                    'max_meta_model_attempts': 1, 'max_intake_model_attempts': config['intake_max_calls'],
                    'max_candidate_evaluations': 6 * (c.LIMITS['online_candidates'] + c.TERMINAL_LIMIT),
                    'max_seed_jobs': 6 * (c.LIMITS['online_candidates'] + c.TERMINAL_LIMIT) * 2,
                    'per_campaign_limits': c.LIMITS, 'min_meta_validation_pairs': 2,
                    'gate_rules': meta.DEFAULT_RULES, 'threshold': None, 'formal_min_compounds': 100,
                    'formal_qualification_assessed': False, 'cohorts': cohorts,
                    'initial_harness_hash': versions.load_harness(config['harness'])['harness_hash'],
                    'runtime_hash': identity['runtime_hash'],
                    'intake_result_sha256': c.sha(intake_path),
                    'stages': ['owner_inspector_intake', 'development_01', 'real_meta_proposal',
                               'two_independent_validation_pairs_if_changed', 'gate', 'selected_policy_development_02'],
                    'no_change_rule': 'Retain parent and continue independent development_02; do not manufacture a modification',
                    'failure_rule': 'Preserve failed or unknown calls; do not replay, replace validation pairs, or promote without evidence'}
            write_new(plan_path, plan)
        plan = c.read(plan_path)
        check_plan(plan)
        record('development_01_running')
        dev = campaign('development_01', config['harness'])
        record('development_01_completed')

        workspace = root / 'meta_01'
        if workspace.exists():
            raise RuntimeError('Existing Meta workspace requires receipt reconciliation; summary-only reuse is disabled')
        else:
            check_plan(plan)
            record('meta_cycle_running')
            pairs = [(root / 'data' / name, root / 'protocols' / (name + '.json'),
                      root / 'campaigns' / (name + '_parent'), root / 'campaigns' / (name + '_candidate'))
                     for name in ('validation_01', 'validation_02')]
            cycle = execute_cycle(development_run=dev, workspace=workspace, planned_pairs=pairs,
                                  gate_out=root / 'gate_01.json', vina=config['vina'],
                                  worker_python=config['worker_python'],
                                  checkpoint=lambda stage, **context: verify_checkpoint(
                                      root, readiness, config['artifacts'], stage, **context))
        if cycle['status'] not in {'completed', 'no_change'}:
            raise RuntimeError(f'Meta cycle requires audit: {cycle.get("error", cycle["status"])}')
        pointer = c.read(workspace / 'selected_policy.json')
        if versions.load_harness(pointer['harness'])['harness_hash'] != pointer['harness_hash']:
            raise ValueError('Selected policy pointer no longer matches frozen policy')
        verify_checkpoint(root, readiness, config['artifacts'], 'before_next_development', workspace=workspace)
        check_plan(plan)
        record('development_02_running', meta_decision=cycle['decision'], selected_harness_hash=pointer['harness_hash'])
        campaign('development_02', pointer['harness'])

        evaluations = []
        for path in sorted((root / 'campaigns').glob('*/evaluation.json')):
            _, _, evaluated = meta.completed_run(path.parent)
            evaluations.append({'campaign': path.parent.name, 'phase': evaluated['phase'],
                'harness_hash': evaluated['harness_hash'], 'shortlist': evaluated['shortlist'],
                'paired_pose_valid_candidates': evaluated['paired_pose_valid_candidates'],
                'mean_qualified_docking_energy_kcal_mol': evaluated['mean_qualified_docking_energy_kcal_mol'],
                'observed_input_plus_output_tokens': evaluated['observed_input_plus_output_tokens'],
                'model_call_count': len(evaluated['model_calls']),
                'archive_sha256': c.sha(path.parent / 'artifact_hashes.json')})
        record('completed', finished_at=c.now(), meta_decision=cycle['decision'],
               selected_harness_hash=pointer['harness_hash'], evaluations=evaluations,
               formal_qualification_assessed=False, intake_result=str(intake_path))
        return state
    except Exception as exc:
        record('needs_audit', error=f'{type(exc).__name__}: {exc}')
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    with (Path(args.root) / '.study.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(args.root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
