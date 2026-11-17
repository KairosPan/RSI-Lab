"""Experiment host helpers adapted from the previously frozen PABLO runner."""
import json
import os
from pathlib import Path
import time
from rsi_lab.session_context import read_native_snapshot
from rsi_lab.store import digest
from pablo_task.adapter import ADAPTER_ID, VERSION, read_progress

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
    campaign = runtime.config.get('research_campaign')
    if campaign:
        completed = campaign['completed_before_segment'] + progress['new_evaluations']
        result['research_campaign'] = {**campaign, 'completed_new_evaluations': completed,
            'remaining_to_checkpoint': campaign['checkpoint_new_evaluations'] - completed,
            'remaining_to_full_target': campaign['target_new_evaluations'] - completed,
            'total_observations': progress['initial_count'] + progress['new_evaluations']}
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
