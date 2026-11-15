"""Run the original PABLO black-box molecular task on persistent RSI-Lab.

This is an explicit domain host, not the original three-role PABLO optimizer.
All scientific evaluations are requested by Lab and dispatched by Runtime.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rsi_lab.controller import Controller
from rsi_lab.plans import register_task_plan
from rsi_lab.roles import CodexExecutor
from rsi_lab.runtime import Runtime
from rsi_lab.session_context import read_native_snapshot
from rsi_lab.store import Store, digest, encode
from rsi_domains.docking.live_sources import freeze_sources, source_contracts, deliver_pending
from rsi_domains.pablo.adapter import ADAPTER_ID, VERSION, make_adapter, read_progress
from rsi_domains.pablo.oracle import canonicalize, score, fingerprint


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def log(root, event, **details):
    row = {'unix_time': time.time(), 'event': event, **details}
    line = json.dumps(row, ensure_ascii=False, allow_nan=False)
    with (root / 'host-events.jsonl').open('a') as stream:
        stream.write(line + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    print(line, flush=True)


def task_plan(root, run_id, initial_ref, target, goal):
    items = [{'item_id': f'evaluation-{i:03d}',
              'identity': f'new-evaluation-slot:{i:03d}'} for i in range(1, target + 1)]
    protocol = {'version': 'blackbox-molecule-search-v1', 'parameters': {
        'initial_count': 100, 'new_unique_finite_scores_required': target,
        'identity': 'RDKit canonical isomeric SMILES',
        'slot_semantics': 'Evaluation positions, not predetermined molecular identities. Each admitted packet binds its positions to novel canonical SMILES before scoring.',
        'duplicate_policy': 'exclude initial, successful and pending canonical molecules',
        'invalid_policy': 'retain filter record; do not count or score',
        'excess_policy': 'clip in proposal order at remaining evaluation capacity',
        'unknown_policy': 'retain obligation; never reissue or replace unknown evaluation',
        'objective_disclosure': 'intentionally hidden from Lab and Meta',
        'task_completion_is_not_optimization_improvement': True}}
    return {'schema_version': 'rsi_task_plan_v1', 'task_id': run_id, 'goal': goal,
        'inputs': [{'input_id': 'initial-observations', 'sha256': initial_ref}],
        'items': items, 'protocol': protocol, 'capability_versions': {ADAPTER_ID: VERSION},
        'metrics': [{'name': 'new_unique_finite_score', 'units': 'completed-evaluation',
            'direction': 'higher_or_equal', 'threshold': 1, 'aggregation': 'min', 'replicates': ['oracle']}],
        'success': {'metric': 'new_unique_finite_score', 'min_qualified_items': target},
        'arms': [{'arm_id': 'persistent-lab-meta', 'policy_version': 'rsi-lab-blackbox-v1',
            'policy_sha256': digest(goal), 'shards': [{'shard_id': 'all-new-evaluations',
                'item_ids': [i['item_id'] for i in items],
                'output_path': str(root / 'observations.json'), 'adapter_id': ADAPTER_ID,
                'protocol_sha256': digest(protocol)}]}],
        'budget': {'new_oracle_evaluations': target, 'model_calls': 600, 'tool_calls': 1000},
        'stop_rule': 'all_planned_shards_terminal_or_explicit_stop',
        'missing_rule': 'retain_all_missing_no_imputation_no_replacement'}


def prepare(args):
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = args.initial_data.resolve(strict=True)
    with source.open() as stream:
        rows = list(csv.DictReader(stream))[:100]
    smiles_key = next(k for k in ('smile', 'smiles', 'SMILES', 'Smiles') if k in rows[0])
    initial = {'candidates': [canonicalize(row[smiles_key]) for row in rows],
               'scores': [float(row['adip']) for row in rows]}
    if len(rows) != 100 or None in initial['candidates'] or len(set(initial['candidates'])) != 100:
        raise ValueError('The first 100 initial candidates must be valid and canonically unique')
    recomputed = [score(s) for s in initial['candidates']]
    delta = max(abs(a-b) for a,b in zip(recomputed, initial['scores']))
    if delta > 1e-7:
        raise ValueError('Original scores disagree with the installed oracle')
    initial_path = root / 'initial-observations.json'
    write_json(initial_path, initial)
    call_example = json.dumps({'run_root': str(root), 'run_id': args.run_id,
                              'candidates': ['YOUR_NEW_VALID_SMILES']})
    goal = (
        f'Optimize a fixed hidden molecular score in [0,1], higher is better. Start from the supplied '
        f'100 original observations and obtain exactly {args.new_evaluations} NEW valid, canonically '
        'unique molecular evaluations. Initial observations, duplicates and invalid proposals do not '
        'count toward this target. This is a black-box search: the objective identity and formula '
        'are deliberately unavailable, not a missing prerequisite. Learn from the observed structure-score '
        'pairs. Lab should propose informative novel candidates, adapting exploration and local improvements '
        'from actual feedback. Submit batches of up to 10 (absolute tool limit 20) through '
        f'request_diagnostic capability {ADAPTER_ID} version {VERSION}; inputs_json must encode '
        f'an object shaped like {call_example}. The run_root and run_id are fixed. '
        'Only Lab may request molecular scoring. Keep your search plan and learned patterns in private '
        'working memory. Retain failed hypotheses; no score improvement does not mean the computation '
        'target is finished. Meta independently reviews raw results, execution, evidence and mechanism '
        'problems. Meta does not submit molecules or infer that the intentionally hidden objective is a '
        'missing permission. If review finds no supported actionable issue, no_action is legitimate. '
        'The host can deliver the explicitly listed frozen sources; it does not automatically implement '
        'arbitrary capability builds or policy trials. Such requests remain explicit unresolved work. '
        'Report completion only from the actual remaining evaluation count. Completion of the evaluation '
        'budget alone is not a claim of biological activity or causal Meta benefit.'
    )
    protocol_path = root / 'task-contract.txt'
    protocol_path.write_text(goal + '\n', encoding='utf-8')
    store = Store(root)
    try:
        initial_ref = store.put_blob(initial)
        sources = freeze_sources(store, {'initial_observations': initial_path,
                                        'task_contract': protocol_path})
        config = {'execution_mode': 'live', 'task_id': args.run_id, 'task_plan_id': args.run_id,
            'model': args.model, 'effort': args.effort, 'goal': goal,
            'pablo_run_root': str(root), 'pablo_initial_ref': initial_ref,
            'pablo_oracle_fingerprint': fingerprint(),
            'pablo_max_new_evaluations': args.new_evaluations,
            'model_reservation': {'model_calls': 1, 'tokens': 45000, 'wall_seconds': 600},
            'max_input_bytes': 262144, 'max_effective_context_bytes': 2097152,
            'max_working_memory_bytes': 32768,
            'prospective_rebuild_history_bytes': 1200000,
            'authorized_sources': [s['source_id'] for s in sources],
            'authorized_source_contracts': source_contracts(sources),
            'frozen_sources': sources, 'open_review_interval_batches': 10,
            'max_open_reviews': 20, 'wait_timeout_seconds': 7200}
        budgets = {key: {'limit': limit, 'hard': key.endswith('_calls')} for key, limit in {
            'model_calls': 600, 'tool_calls': 1000, 'tokens': 100000000,
            'wall_seconds': 432000, 'cpu_seconds': 86400}.items()}
        Controller(store).initialize(args.run_id, budgets=budgets, max_external=1, config=config)
        plan = task_plan(root, args.run_id, initial_ref, args.new_evaluations, goal)
        register_task_plan(store, args.run_id, plan, key='freeze-task-plan')
        runtime = Runtime(store, args.run_id, adapters=[make_adapter()])
        runtime.controller.add_fact(args.run_id, 'initial-observations', 'raw_fact', initial,
            {'source': 'frozen initial observations', 'sha256': initial_ref,
             'scope': '100 preexisting observations, excluded from the new evaluation count'}, 'initial-fact')
        runtime.controller.add_fact(args.run_id, 'task-contract', 'source_statement',
            {'contract': goal, 'new_evaluations_required': args.new_evaluations},
            {'source': 'host task contract', 'sha256': digest(goal)}, 'contract-fact')
        runtime.enqueue_lab('initial-step', ['initial-observations', 'task-contract'])
        runtime.observe('initial-input', 'input_changed', 0, ['initial-observations', 'task-contract'])
        write_json(root / 'run-config.json', store.run(args.run_id)['config'])
        write_json(root / 'task-plan.json', plan)
        tracked = subprocess.check_output(['git', 'ls-files', '*.py'], cwd=REPO, text=True).splitlines()
        additions = list((REPO / 'rsi_domains/pablo').glob('*.py')) + [Path(__file__)]
        source_hashes = {str(p.relative_to(REPO)): digest(p.read_bytes())
                         for p in sorted(set([REPO / name for name in tracked] + additions))}
        manifest = {'base_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
            'branch': subprocess.check_output(['git','branch','--show-current'],cwd=REPO,text=True).strip(),
            'source_hashes': source_hashes, 'initial_csv_sha256': digest(source.read_bytes()),
            'initial_csv_path': str(source), 'initial_scores_max_abs_delta': delta,
            'task_aware': False, 'algorithm': 'RSI-Lab persistent Lab/Meta on the original PABLO black-box task; not the original three-role optimizer',
            'model': args.model, 'effort': args.effort, 'target_new_evaluations': args.new_evaluations,
            'python': sys.version, 'dependencies': {name: importlib.metadata.version(name)
                for name in ['guacamol','rdkit','numpy','scipy','pandas']}}
        write_json(root / 'manifest.json', manifest)
        log(root, 'prepared', target=args.new_evaluations, initial_count=100,
            oracle_max_abs_delta=delta, base_commit=manifest['base_commit'])
    finally:
        store.close()


def snapshot(runtime, status, reason=None):
    store, run = runtime.store, runtime.run_id
    progress = read_progress(store, run)
    effects = store.list(run, 'effect')
    runtime_state = runtime.status()
    def attempts(kind):
        return sum(e.get('actual_costs', {}).get('model_calls',
            int(e['status'] in {'dispatching', 'unknown'})) for e in effects if e['kind'] == kind)
    result = {'status': status, 'reason': reason, 'run_id': run, 'updated_at_unix': time.time(),
        'new_evaluations': progress['new_evaluations'], 'target_new_evaluations': runtime.config['pablo_max_new_evaluations'],
        'remaining_evaluations': progress['remaining_evaluations'], 'initial_count': progress['initial_count'],
        'top20': progress['top20'],
        'lab_calls': attempts('lab'), 'meta_calls': attempts('meta'),
        'tool_effects': sum(e.get('metadata',{}).get('adapter_id')==ADAPTER_ID for e in effects),
        'unresolved_effects': [e['id'] for e in effects if e['status'] in {'dispatching','unknown'} or e.get('accounting_unknown')],
        'issues': store.list(run, 'issue'), 'questions': store.list(run, 'question', status='pending'),
        'blocked_work': runtime_state['blocked_work'],
        'blocked_proposals': runtime_state['blocked_proposals'],
        'agent_sessions': runtime_state['agent_sessions'],
        'prepared_effects': runtime_state['prepared_effects'],
        'pending_work': [{'id': w['id'], 'kind': w['kind'], 'status': w['status']}
                         for w in runtime_state['ready']],
        'completion_scope': 'requested evaluation count; unresolved research issues remain explicit',
        'meta_review_pending': any(w['kind'] in {'meta_discovery', 'meta_disposition', 'disposition'}
                                   for w in runtime_state['ready']),
        'budgets': store.list(run, 'budget')}
    write_json(store.root / 'progress.json', result)
    return result


def attention_reason(state):
    """A one-step driver result does not certify that its effect succeeded."""
    if state['unresolved_effects']:
        return 'Unresolved external execution or accounting requires reconciliation'
    for session in state['agent_sessions']:
        if session['status'] in {'recovery_required', 'in_turn'}:
            return 'Agent session requires reconciliation: ' + session['id']
    if state['blocked_work']:
        return 'Blocked work requires explicit replanning: ' + state['blocked_work'][0]['id']
    if state['questions']:
        return 'Meta requested user input; see progress.json questions'
    return None


def resume_prepared_diagnostic(runtime):
    """Dispatch the exact already-admitted packet; never prepare or score twice."""
    unresolved = [e for e in runtime.store.list(runtime.run_id, 'effect')
                  if e['status'] in {'dispatching', 'unknown'} or e.get('accounting_unknown')]
    if unresolved:
        raise RuntimeError('Cannot dispatch while an external effect requires reconciliation')
    for effect in runtime.store.list(runtime.run_id, 'effect', status='prepared'):
        work = runtime.store.get(runtime.run_id, 'work', effect['work_id'])
        if work['kind'] != 'diagnostic':
            continue
        adapter = runtime.adapters.resolve(effect)
        settled = runtime.controller.dispatch_effect(runtime.run_id, effect['id'], adapter.execute)
        if settled['status'] == 'succeeded' and not settled.get('accounting_unknown'):
            runtime.publish_adapter_effect(settled)
        return {'work_id': work['id'], 'effect_id': settled['id'], 'effect_status': settled['status']}
    return None


def review_final_batch(runtime, executor):
    """Review only the final scoring publication, without an unbounded tail."""
    scored = [e for e in runtime.store.list(runtime.run_id, 'effect', status='succeeded')
              if e.get('metadata', {}).get('adapter_id') == ADAPTER_ID]
    if not scored:
        return None
    final = max(scored, key=lambda e: e['created_seq'])
    trigger = 'event:adapter:' + final['id']
    work = next((w for w in runtime.controller.ready(runtime.run_id)
                 if w['kind'] == 'meta_discovery' and w['trigger'] == trigger), None)
    if work is None:
        return None
    return runtime.run_meta(work['id'], executor, runtime.config['model_reservation'])


def record_attention(runtime, reason):
    """Report failures even when scientific progress cannot be reconstructed."""
    try:
        result = snapshot(runtime, 'needs_attention', reason)
    except Exception as exc:
        result = {'status': 'needs_attention', 'run_id': runtime.run_id, 'reason': reason,
                  'updated_at_unix': time.time(), 'new_evaluations': None,
                  'progress_verified': False, 'progress_error': f'{type(exc).__name__}: {exc}'}
        try:
            result['state'] = runtime.status()
        except Exception as status_error:
            result['state_error'] = f'{type(status_error).__name__}: {status_error}'
        write_json(runtime.store.root / 'progress.json', result)
    write_json(runtime.store.root / 'attention.json', result)
    return result


def rebuild_at_boundary(runtime):
    threshold = runtime.config['prospective_rebuild_history_bytes']
    if any(e['status'] in {'dispatching', 'unknown'} or e.get('accounting_unknown')
           for e in runtime.store.list(runtime.run_id, 'effect')):
        raise RuntimeError('Cannot rebuild around an unresolved external effect')
    for session in runtime.store.list(runtime.run_id, 'agent_session', status='idle'):
        manifest = session.get('context_manifest') or {}
        path = manifest.get('native_session_ref')
        if not isinstance(path, str) or not Path(path).is_file() or Path(path).stat().st_size < threshold:
            continue
        if session['active_turn_id']:
            continue
        checked = read_native_snapshot(path,
            expected_thread_id=session['provider_thread_id'],
            expected_cwd=str(runtime.store.root / 'agents' / session['id'] / 'workspace'),
            expected_prefix=manifest.get('native_prefix'))
        if checked['prefix_sha256'] != (manifest.get('native_prefix') or {}).get('prefix_sha256'):
            raise RuntimeError('Native session advanced outside its committed turn; reconcile before rebuilding')
        checkpoint = runtime.agents.seal_checkpoint(session['id'],
            'capacity-checkpoint:' + session['id'] + ':' + str(session['committed_turn']))
        rebuilt = runtime.agents.rebuild_session(session['id'],
            'Prospectively configured observable-history capacity boundary',
            'capacity-rebuild:' + session['id'], checkpoint_id=checkpoint['id'])
        log(runtime.store.root, 'session_rebuilt', old_session=session['id'], new_session=rebuilt['id'])


def drive(args):
    root = args.root.resolve(strict=True)
    lock = (root / 'host.lock').open('a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('Another host owns this run')
    write_json(root / 'host-process.json', {'pid': os.getpid(), 'run_id': args.run_id,
                                          'started_at_unix': time.time(), 'python': sys.executable})
    store = Store(root)
    runtime = Runtime(store, args.run_id, adapters=[make_adapter()])
    try:
        manifest = json.loads((root / 'manifest.json').read_text())
        if any(digest((REPO / p).read_bytes()) != expected for p, expected in manifest['source_hashes'].items()):
            raise ValueError('Source differs from the prepared run; do not silently change a live experiment')
        runtime.recover()
        log(root, 'started', pid=os.getpid(), run_id=args.run_id)
        executor = CodexExecutor(root / 'calls', timeout_seconds=600)
        while True:
            state = snapshot(runtime, 'running')
            if attention_reason(state):
                raise RuntimeError(attention_reason(state))
            if state['new_evaluations'] == state['target_new_evaluations']:
                review = review_final_batch(runtime, executor)
                if review:
                    state = snapshot(runtime, 'running')
                    if attention_reason(state):
                        raise RuntimeError(attention_reason(state))
                    log(root, 'final_batch_meta_review', effect_id=review.get('effect', {}).get('id'),
                        proposal_status=(review.get('proposal') or {}).get('status'))
                runtime.finalize('requested_new_evaluations_completed')
                result = snapshot(runtime, 'completed')
                write_json(root / 'observations.json', read_progress(store, args.run_id))
                write_json(root / 'result.json', result)
                log(root, 'completed', new_evaluations=state['new_evaluations'],
                    lab_calls=state['lab_calls'], meta_calls=state['meta_calls'])
                return 0
            deliveries = deliver_pending(runtime, runtime.config['frozen_sources'])
            if deliveries:
                log(root, 'source_requests', outcomes=[{'request_id': d['request_id'],
                    'status': d['status']} for d in deliveries])
            state = snapshot(runtime, 'running')
            if attention_reason(state):
                raise RuntimeError(attention_reason(state))
            rebuild_at_boundary(runtime)
            resumed = resume_prepared_diagnostic(runtime)
            result = ({'status': 'step_limit_reached', 'steps': [resumed]} if resumed else
                      runtime.run_until_wait(executor, runtime.config['model_reservation'], max_steps=1))
            state = snapshot(runtime, 'running')
            if result['steps']:
                log(root, 'step', steps=result['steps'], new_evaluations=state['new_evaluations'],
                    lab_calls=state['lab_calls'], meta_calls=state['meta_calls'])
            if attention_reason(state):
                raise RuntimeError(attention_reason(state))
            if result['status'] != 'step_limit_reached' or not result['steps']:
                reason = result.get('reason') or result['status']
                result = snapshot(runtime, 'needs_attention', reason)
                write_json(root / 'attention.json', result)
                log(root, 'needs_attention', reason=reason, new_evaluations=state['new_evaluations'])
                return 2
    except BaseException as exc:
        reason = f'{type(exc).__name__}: {exc}'
        try:
            record_attention(runtime, reason)
            log(root, 'needs_attention', reason=reason)
        finally:
            traceback.print_exc()
        return 2
    finally:
        store.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--run-id', default='pablo-200')
    parser.add_argument('--initial-data', type=Path)
    parser.add_argument('--new-evaluations', type=int, default=200)
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--effort', default='medium')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.new_evaluations <= 10000:
        parser.error('--new-evaluations must be between 1 and 10000')
    if not args.resume:
        if args.initial_data is None:
            parser.error('--initial-data is required for a new run')
        prepare(args)
    if args.prepare_only:
        return 0
    return drive(args)


if __name__ == '__main__':
    raise SystemExit(main())
