"""Verified continuation lineage and role-owned memory transfer; no model calls."""
import copy
import json
import math
from pathlib import Path
import sqlite3
import time
from rsi_lab.controller import identity
from rsi_lab.store import Store, digest
import host_support as host

class ReadOnlySnapshot(Store):
    """Read a closed WAL database without creating a journal or shared memory."""
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.blobs = self.root / 'objects'
        path = self.root / 'ledger.sqlite3'
        if not path.is_file() or not self.blobs.is_dir():
            raise ValueError('A complete existing parent ledger is required')
        if Path(str(path) + '-wal').exists():
            raise ValueError('Parent WAL is still present; close/reconcile its writer before continuing')
        self.db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row

def _sum_costs(rows):
    total = {}
    for row in rows:
        for name, value in row.items():
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Historical costs must be explicit finite nonnegative values')
            total[name] = total.get(name, 0) + value
    return total

def make_lineage(parent, target):
    inherited = parent['manifest'].get('continuation_lineage', {})
    segments = copy.deepcopy(inherited.get('cost_segments', []))
    segment = {'run_root': parent['root'], 'run_id': parent['run_id'],
               'history': parent['history'], 'actual_costs': {b['id']: b['spent'] for b in parent['budgets']},
               'new_evaluations': parent['progress']['new_evaluations']}
    if any((row['run_root'], row['run_id']) == (segment['run_root'], segment['run_id']) for row in segments):
        raise ValueError('Parent segment is already present in its own cost ancestry')
    segments.append(segment)
    prior_attempts = copy.deepcopy(parent['manifest'].get('prior_attempts', []))
    original_count = inherited.get('original_initial_count', parent['progress']['initial_count'])
    total_history = len(parent['progress']['candidates'])
    return {'schema_version': 'pablo_continuation_lineage_v2', 'parent_root': parent['root'],
            'parent_run_id': parent['run_id'], 'parent_config_sha256': parent['config_hash'],
            'parent_history': parent['history'], 'parent_manifest_sha256': parent['manifest_sha256'],
            'parent_result_sha256': parent['result_sha256'],
            'inherited_trajectory_sha256': parent['progress']['trajectory_sha256'],
            'original_initial_count': original_count,
            'historical_generated_evaluations': total_history - original_count,
            'inherited_observations': total_history, 'segment_new_evaluations': target,
            'cost_segments': segments, 'prior_attempts': prior_attempts,
            'prior_attempt_costs': _sum_costs(p.get('actual_costs', {}) for p in prior_attempts),
            'inherited_segment_costs': _sum_costs(s['actual_costs'] for s in segments),
            'cost_identity': 'run_root plus run_id; prior attempts retain their full summary path and effect IDs',
            'source_receipts': parent['scoring_sources'],
            'rejection_dispositions': copy.deepcopy(inherited.get('rejection_dispositions', [])) + parent['rejection_dispositions'],
            'private_memory_policy': 'Role-preserving unverified import; historical IDs grant no current read authority',
            'native_session_policy': 'Fresh owned sessions; no parent provider thread or queued work copied'}

def import_memory(runtime, role, source, parent):
    binding = {'model': runtime.config['model'], 'effort': runtime.config['effort'], 'transport': 'codex_exec_text_v1'}
    session = runtime.agents.ensure_session(role, runtime_binding=binding)
    memory = source['memory']
    content = {} if memory is None else runtime.agents._memory_delta(memory['content'], {})
    provenance = {'parent_root': parent['root'], 'parent_run_id': parent['run_id'],
                  'parent_history': parent['history'], 'parent_owner_id': source['owner']['id'],
                  'parent_session_id': source['session']['id'], 'parent_checkpoint_id': source['checkpoint']['id'],
                  'parent_memory_head_ref': source['owner']['memory_head_ref'],
                  'parent_memory_revision': source['owner']['memory_revision'],
                  'parent_memory_sha256': None if memory is None else digest(memory),
                  'parent_memory_blob_ref': None if memory is None else runtime.store.put_blob(memory),
                  'verification': 'unverified_working_state',
                  'reference_scope': 'Historical only; current task and delivered facts override old counts and IDs'}
    token = 'continuation-memory:' + role
    def apply(tx):
        owner = tx.get('agent_owner', session['owner_id'])
        if owner['memory_revision'] or owner['memory_head_ref'] or session['committed_turn'] or session['provider_thread_id']:
            raise ValueError('Memory import requires a fresh unused target owner')
        registration = tx.insert('registration', token, {'kind': 'private_memory_import',
            'status': 'imported' if memory is not None else 'empty_parent_memory', 'role': role,
            'target_owner_id': owner['id'], 'provenance': provenance})
        if memory is not None:
            rid = identity('memory_import', runtime.run_id, role, provenance['parent_memory_sha256'])
            tx.insert('working_memory', rid, {'owner_id': owner['id'], 'owner': owner['scope'],
                'revision': 1, 'parent_ref': None, 'content': content,
                'source_turn_id': None, 'source_call_id': None, 'source_effect_id': None,
                'source_import_id': registration['id'], 'ledger_snapshot_seq': tx.seq,
                'context_manifest_hash': None, 'schema_version': 'agent_working_memory_v1',
                'verification': 'unverified_working_state', 'import_provenance': provenance})
            tx.update('agent_owner', owner['id'], {'memory_revision': 1, 'memory_head_ref': rid})
        return registration
    return runtime.store.mutate(runtime.run_id, token, 'private_memory_imported', apply,
                                payload={'role': role, 'target_session_id': session['id'], 'provenance': provenance})

def write_cumulative(root, run_id):
    root = Path(root).resolve()
    lineage = json.loads((root / 'lineage.json').read_text())
    store = ReadOnlySnapshot(root)
    try:
        config = store.run(run_id)['config']
        if digest(lineage) != config['pablo_continuation_lineage_ref']:
            raise ValueError('Continuation lineage file changed')
        store.verify_history(run_id)
        current = {b['id']: b['spent'] for b in store.list(run_id, 'budget')}
        report = {'updated_at_unix': time.time(), 'run_id': run_id,
            'current_segment_costs': current,
            'prior_segment_costs': lineage['inherited_segment_costs'],
            'prior_attempt_costs': lineage['prior_attempt_costs'],
            'cumulative_known_costs': _sum_costs([current, lineage['inherited_segment_costs'], lineage['prior_attempt_costs']]),
            'prior_attempts': lineage['prior_attempts'], 'cost_segments': lineage['cost_segments'],
            'scope': 'Observed ledger costs plus explicitly archived prior attempts; host overhead excluded, not zero',
            'updates': 'At preparation and host exit; live segment budgets remain in progress.json'}
        host.write_json(root / 'cumulative-costs.json', report)
        return report
    finally:
        store.close()
