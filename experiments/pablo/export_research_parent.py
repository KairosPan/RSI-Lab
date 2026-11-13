"""Read-only research continuation export using the parent's frozen interpreter code.

This helper is new host code. Domain receipt and scientific-memory interpretation
come exclusively from the hash-verified frozen source tree in a fresh process.
It neither constructs a Runtime nor invokes a model, scorer or network adapter.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _source_tree(source_root, manifest):
    values = manifest.get('source_hashes')
    required = {'rsi_lab/store.py', 'rsi_lab/scientific_memory.py',
                'experiments/pablo/pablo_task/adapter.py', 'experiments/pablo/pablo_task/oracle.py'}
    if not isinstance(values, dict) or not required.issubset(values):
        raise ValueError('Research parent lacks its frozen source contract')
    for name, expected in values.items():
        path = (source_root / name).resolve()
        if not path.is_relative_to(source_root) or not path.is_file() or _hash(path.read_bytes()) != expected:
            raise ValueError('Historical source differs from frozen parent: ' + name)


def _zero_wal(root):
    wal = root / 'ledger.sqlite3-wal'
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Parent has a nonempty WAL; close and reconcile its writer before export')


def _memories(store, run_id, history, digest):
    result = {}
    for owner in store.list(run_id, 'agent_owner'):
        scope = owner['scope']
        role = scope['role']
        if role not in {'lab', 'meta'} or role in result or scope['branch_id'] != 'main':
            raise ValueError('Parent requires one main-branch private owner per role')
        session = store.get(run_id, 'agent_session', owner['session_id'])
        if (session['owner_id'] != owner['id'] or session['owner'] != scope
                or session['status'] != 'idle' or session.get('active_turn_id')):
            raise ValueError('Parent private memory owner has an active or inconsistent current session')
        memory = store.get(run_id, 'working_memory', owner['memory_head_ref']) if owner['memory_head_ref'] else None
        if memory is not None:
            if memory['owner_id'] != owner['id'] or memory['owner'] != scope or memory['revision'] != owner['memory_revision']:
                raise ValueError('Parent private memory head differs from its role owner')
            if memory.get('source_turn_id'):
                turn = store.get(run_id, 'agent_turn', memory['source_turn_id'])
                effect = store.get(run_id, 'effect', memory['source_effect_id'])
                if (turn['status'] != 'committed' or turn['owner_id'] != owner['id']
                        or effect['id'] not in turn['effect_ids'] or effect['status'] != 'succeeded'
                        or turn['terminal_ref'] != effect['result_ref']):
                    raise ValueError('Parent private memory lacks its committed role-owned source turn')
            elif not memory.get('source_import_id'):
                raise ValueError('Parent private memory lacks a committed source or explicit import')
        elif owner['memory_revision']:
            raise ValueError('Parent private owner has a revision without a memory head')
        checkpoint = None
        for cid in (session.get('checkpoint_ref'), session.get('reconstruction_checkpoint')):
            if cid:
                candidate = store.get(run_id, 'agent_checkpoint', cid)
                if candidate['owner_id'] != owner['id']:
                    raise ValueError('Parent checkpoint crosses private role ownership')
                if candidate['memory_head_ref'] == owner['memory_head_ref']:
                    checkpoint = candidate
                    break
        proof = {'kind': 'readonly_verified_memory_head', 'owner_id': owner['id'],
                 'session_id': session['id'], 'memory_head_ref': owner['memory_head_ref'],
                 'memory_revision': owner['memory_revision'], 'history': history,
                 'memory_sha256': None if memory is None else digest(memory)}
        result[role] = {'owner': owner, 'session': session, 'memory': memory, 'checkpoint': checkpoint,
                        'memory_head_proof': {**proof, 'sha256': digest(proof)}}
    if set(result) != {'lab', 'meta'}:
        raise ValueError('Both parent Lab and Meta private owners are required')
    return result


def export_parent(root, run_id, source_root):
    root, source_root = Path(root).resolve(strict=True), Path(source_root).resolve(strict=True)
    with (root / 'host.lock').open('rb') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Parent host is still running') from exc
        manifest_raw = (root / 'manifest.json').read_bytes()
        manifest = json.loads(manifest_raw)
        if not str(manifest.get('research_architecture', '')).startswith('pablo_research_'):
            raise ValueError('Only a frozen research parent uses this exporter')
        _source_tree(source_root, manifest)
        # Execute only the verified parent modules, never the modified live tree.
        sys.path[:0] = [str(source_root), str(source_root / 'experiments/pablo')]
        from rsi_lab.store import Store, digest, encode
        from rsi_lab.scientific_memory import ScientificMemory
        from pablo_task import adapter, oracle
        for module in (sys.modules['rsi_lab.store'], sys.modules['rsi_lab.scientific_memory'], adapter, oracle):
            if not Path(module.__file__).resolve().is_relative_to(source_root):
                raise ValueError('Parent interpretation did not load from its verified source tree')

        class ParentStore(Store):
            def __init__(self):
                self.root, self.blobs = root, root / 'objects'
                _zero_wal(root)
                self.db = sqlite3.connect((root / 'ledger.sqlite3').as_uri() + '?mode=ro&immutable=1',
                                          uri=True, isolation_level=None)
                self.db.row_factory = sqlite3.Row
            def mutate(self, *args, **kwargs):
                raise AssertionError('Research parent export is read-only')
            def put_blob(self, *args, **kwargs):
                raise AssertionError('Research parent export cannot write blobs')

        ledger_hash = _hash((root / 'ledger.sqlite3').read_bytes())
        store = ParentStore()
        try:
            history, run = store.verify_history(run_id), store.run(run_id)
            config = run['config']
            if config.get('research_features', {}).get('scientific_memory') is not True:
                raise ValueError('Research parent requires its structured scientific-memory graph')
            if config.get('pablo_oracle_fingerprint') != oracle.fingerprint():
                raise ValueError('Parent frozen evaluator or dependencies changed')
            effects, budgets = store.list(run_id, 'effect'), store.list(run_id, 'budget')
            if any(e['status'] not in {'succeeded', 'failed_definite', 'cancelled'}
                   or e.get('accounting_unknown') or any(e.get('held_reservations', {}).values()) for e in effects):
                raise ValueError('Parent has active/unknown execution or unsettled accounting')
            if (any(b['reserved'] != 0 for b in budgets)
                    or store.list(run_id, 'work', status='leased')
                    or any(s.get('active_turn_id') for s in store.list(run_id, 'agent_session'))
                    or any(t['status'] not in {'committed', 'rejected', 'cancelled'} for t in store.list(run_id, 'agent_turn'))):
                raise ValueError('Parent has active reservations, Work or private turns')
            for budget in budgets:
                values = [e.get('actual_costs', {}).get(budget['id'], 0) for e in effects]
                if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values + [budget['spent']]):
                    raise ValueError('Parent costs must be finite and nonnegative')
                if not math.isclose(sum(values), budget['spent'], rel_tol=1e-9, abs_tol=1e-6):
                    raise ValueError('Parent budget spent differs from settled effect costs: ' + budget['id'])
            artifact_name = 'result.json' if (root / 'result.json').exists() else 'progress.json'
            artifact_raw = (root / artifact_name).read_bytes()
            boundary_report = json.loads(artifact_raw)
            status = boundary_report.get('status')
            if status not in {'paused', 'completed'} or boundary_report.get('run_id') != run_id:
                raise ValueError('Parent must explicitly report a paused or completed research boundary')
            if (status == 'paused') != (artifact_name == 'progress.json'):
                raise ValueError('Parent boundary artifact does not match its paused/completed status')
            progress = adapter.read_progress(store, run_id)
            if any(boundary_report.get(key) != progress[key] for key in ('initial_count', 'new_evaluations', 'remaining_evaluations')):
                raise ValueError('Parent boundary report differs from authenticated observations')
            maximum = config['pablo_max_new_evaluations']
            if (boundary_report.get('target_new_evaluations') != maximum
                    or manifest.get('target_new_evaluations') != maximum
                    or (status == 'completed' and progress['remaining_evaluations'] != 0)):
                raise ValueError('Parent boundary quota differs from its frozen task')
            values = progress['candidates']
            if (len(values) != len(set(values)) or any(oracle.canonicalize(value) != value for value in values)
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in progress['scores'])):
                raise ValueError('Inherited observations must be canonical, unique and finite')
            evidence = {config['pablo_initial_ref']: store.read_blob(config['pablo_initial_ref'])}
            for item in manifest.get('continuation_lineage', {}).get('rejection_dispositions', []):
                for key in ('repair_chain_sha256', 'archive_sha256'):
                    if item.get(key):
                        evidence[item[key]] = store.read_blob(item[key])
            scoring = []
            for effect in effects:
                if effect.get('metadata', {}).get('adapter_id') != adapter.ADAPTER_ID:
                    continue
                if effect['status'] != 'succeeded':
                    raise ValueError('Unsuccessful scoring requires an explicit reconciliation before continuation')
                for ref in (effect['packet_ref'], effect['result_ref'], effect['receipt_ref']):
                    evidence[ref] = store.read_blob(ref)
                receipt = json.loads(evidence[effect['result_ref']])
                expected = receipt.get('execution', {}).get('journal_sha256')
                if expected:
                    journal = (root / 'pablo_evaluations' / effect['id'] / 'evaluations.jsonl').read_bytes()
                    if digest(journal) != expected:
                        raise ValueError('Parent scoring journal differs from its certified receipt')
                    evidence[expected] = journal
                scoring.append({'effect_id': effect['id'], 'packet_ref': effect['packet_ref'],
                                'result_ref': effect['result_ref'], 'run_root': str(root)})
            pending = {'work': [w for w in store.list(run_id, 'work') if w['status'] not in {'succeeded', 'failed', 'cancelled', 'superseded'}],
                       'proposals': [p for p in store.list(run_id, 'proposal') if p['status'] != 'applied'],
                       'recommendations': store.list(run_id, 'registration', kind='research_recommendation'),
                       'issues': store.list(run_id, 'issue'), 'waits': store.list(run_id, 'wait'),
                       'questions': store.list(run_id, 'question'),
                       'transfer_policy': 'Historical state only; no queued Work, recommendation authority, lease or provider thread is imported'}
            pending_ref = digest(pending)
            evidence[pending_ref] = encode(pending)
            dispositions = [{'run_root': str(root), 'run_id': run_id, 'proposal_id': row['id'],
                             'original_status': row['status'], 'disposition': 'archived_without_retry_or_success_reclassification',
                             'archive_sha256': pending_ref} for row in pending['proposals']]
            bundle = ScientificMemory(store, run_id).export_bundle()
            boundary = {'status': status, 'source_kind': 'paused_research_progress' if status == 'paused' else 'completed_research_result',
                        'artifact_name': artifact_name, 'artifact_sha256': digest(artifact_raw), 'history': history,
                        'new_evaluations': progress['new_evaluations'], 'initial_count': progress['initial_count'],
                        'target_new_evaluations': maximum, 'inherited_observations': len(values),
                        'ledger_sha256': ledger_hash, 'pending_state_sha256': pending_ref,
                        'exporter_sha256': _hash(Path(__file__).read_bytes()), 'scientific_memory_sha256': bundle['sha256']}
            memories = _memories(store, run_id, history, digest)
            if (store.verify_history(run_id) != history or _hash((root / 'ledger.sqlite3').read_bytes()) != ledger_hash
                    or (root / 'manifest.json').read_bytes() != manifest_raw
                    or (root / artifact_name).read_bytes() != artifact_raw):
                raise ValueError('Parent changed during its read-only export')
            _zero_wal(root)
            return {'root': str(root), 'run_id': run_id, 'config': config, 'config_hash': run['config_hash'],
                    'history': history, 'manifest': manifest, 'manifest_sha256': digest(manifest_raw),
                    'result': boundary_report, 'result_sha256': digest(artifact_raw), 'progress': progress,
                    'budgets': budgets, 'memories': memories, 'scoring_sources': scoring,
                    'evidence_objects': {key: base64.b64encode(raw).decode('ascii') for key, raw in evidence.items()},
                    'rejection_dispositions': dispositions, 'boundary': boundary,
                    'pending_parent_state': pending, 'scientific_memory_bundle': bundle,
                    'validated_source_root': str(source_root)}
        finally:
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    exported = export_parent(args.root, args.run_id, args.source_root)
    args.output.write_text(json.dumps(exported, ensure_ascii=False))
