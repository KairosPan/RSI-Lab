"""Exact, read-only candidate lookup against the complete authorized score history."""
import json
from pathlib import Path
import time

from rsi_lab.adapters import Adapter
from rsi_lab.store import digest
from . import adapter, oracle

ADAPTER_ID = 'pablo.lookup'
VERSION = '1.0'
EFFECT_KIND = 'pablo_identity_lookup'


def _prepare(inputs):
    if not isinstance(inputs, dict) or set(inputs) != {'run_root', 'run_id', 'candidates'}:
        raise ValueError('Lookup needs exactly run_root, run_id and candidates')
    root = inputs['run_root']
    if (not isinstance(root, str) or not Path(root).is_absolute() or str(Path(root).resolve()) != root
            or not isinstance(inputs['run_id'], str) or not inputs['run_id']):
        raise ValueError('Canonical absolute run root and explicit run ID required')
    values = inputs['candidates']
    if (not isinstance(values, list) or not 1 <= len(values) <= 100
            or any(not isinstance(s, str) or len(s) > 4096 for s in values)):
        raise ValueError('Lookup accepts one to 100 bounded SMILES strings')
    store = adapter._ReadStore(root)
    try:
        progress = adapter.read_progress(store, inputs['run_id'])
        known = {s: (i + 1, score) for i, (s, score) in enumerate(zip(progress['candidates'], progress['scores']))}
        pending = {}
        queued = {}
        for work in adapter.queued_intents(store, inputs['run_id']):
            for canonical in work['canonical_candidates']:
                queued.setdefault(canonical, []).append(work['work_id'])
        for effect in adapter._effects(store, inputs['run_id']):
            if effect['status'] in {'prepared', 'dispatching', 'unknown'}:
                packet = json.loads(store.read_blob(effect['packet_ref']))
                for row in packet['accepted']:
                    pending[row['canonical']] = effect['id']
        seen, rows = set(), []
        for index, value in enumerate(values):
            canonical = oracle.canonicalize(value)
            row = {'source_index': index, 'candidate': value, 'canonical': canonical}
            if canonical is None:
                row.update(status='invalid', score=None)
            elif canonical in known:
                ordinal, score = known[canonical]
                row.update(status='already_evaluated', score=score, observation_ordinal=ordinal,
                           origin='inherited' if ordinal <= progress['initial_count'] else 'current_segment')
            elif canonical in pending:
                row.update(status='pending_or_unknown', score=None, effect_id=pending[canonical])
            elif canonical in queued:
                row.update(status='pending_or_unknown', score=None, work_ids=queued[canonical],
                           planning_state='queued_before_effect_preparation')
            elif canonical in seen:
                row.update(status='duplicate_in_query', score=None)
            else:
                row.update(status='not_seen_at_snapshot', score=None)
            if canonical:
                seen.add(canonical)
            rows.append(row)
        return {'schema_version': 'pablo_lookup_v1', **inputs, 'rows': rows,
                'trajectory_sha256': progress['trajectory_sha256'],
                'as_of_observations': len(progress['candidates']),
                'initial_count': progress['initial_count'],
                'limitation': 'Exact identity at this snapshot; not_seen is not a promise of validity or novelty at later scoring.'}
    finally:
        store.close()


def _execute(effect, raw):
    started = time.monotonic()
    costs = {'model_calls': 0, 'tokens': 0, 'tool_calls': 1, 'wall_seconds': 0, 'cpu_seconds': 0}
    try:
        packet = json.loads(raw)
        store = adapter._ReadStore(packet['run_root'])
        try:
            adapter._binding(store, packet['run_id'])
            store.verify_history(packet['run_id'])
            if (store.get(packet['run_id'], 'effect', effect['id']) != effect
                    or effect['status'] != 'dispatching' or effect['kind'] != EFFECT_KIND
                    or effect['run_id'] != packet['run_id']
                    or effect.get('metadata', {}).get('adapter_id') != ADAPTER_ID
                    or effect['packet_hash'] != digest(raw)
                    or store.read_blob(effect['packet_ref']) != raw):
                raise ValueError('Lookup requires its exact persisted dispatching effect')
        finally:
            store.close()
        # The frozen query snapshot is a read-only answer; a later score adapter rechecks all identities.
        result = {k: v for k, v in packet.items() if k not in {'run_root', 'run_id', 'candidates'}}
        result.update(effect_id=effect['id'], packet_sha256=digest(raw))
        costs['wall_seconds'] = time.monotonic() - started
        return {'status': 'succeeded', 'actual_costs': costs, 'result': result}
    except (KeyError, ValueError, OSError, TypeError) as exc:
        costs['wall_seconds'] = time.monotonic() - started
        return {'status': 'failed_definite', 'actual_costs': costs, 'error': str(exc)}


def prepare(inputs):
    return _prepare(inputs)


def reservations(packet):
    return {'model_calls': 0, 'tokens': 0, 'tool_calls': 1, 'wall_seconds': 30, 'cpu_seconds': 30}


def execute(effect, raw):
    return _execute(effect, raw)


def publication(effect, receipt):
    return {'facts': [{'id': 'pablo_lookup_' + digest(effect['id'])[:24], 'kind': 'raw_fact',
                      'content': receipt['result'], 'provenance': {'effect_id': effect['id'],
                      'adapter_id': ADAPTER_ID, 'adapter_version': VERSION,
                      'receipt_ref': effect['result_ref']}}], 'conclusions': [], 'data': receipt['result']}


def make_adapter():
    return Adapter(ADAPTER_ID, VERSION, EFFECT_KIND,
        {'domain': 'molecular_optimization', 'scope': 'Exact canonical lookup over ALL inherited and current observations and pending scores; no oracle calls',
         'input_contract': {'run_root': 'frozen absolute run root', 'run_id': 'current run',
                            'candidates': '1 to 100 SMILES strings'},
         'output_contract': 'Per-input canonical identity, prior score and ordinal, pending identity or not_seen_at_snapshot',
         'limitations': ['Not a similarity or activity prediction', 'Scoring rechecks current state',
                        'Invalid and pending identities are never returned as novel']},
        prepare, reservations, execute, publication,
        source_paths=(str(Path(adapter.__file__).resolve()), str(Path(oracle.__file__).resolve())))
