"""Serial, receipt-backed molecular evaluation under a frozen host configuration."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import resource
import sqlite3
import time

from rsi_lab.adapters import Adapter
from rsi_lab.store import Store, digest, encode
from . import oracle

ADAPTER_ID = 'pablo.evaluate'
VERSION = '1.0'
EFFECT_KIND = 'pablo_evaluation'
MAX_CANDIDATES = 20
BASE_INPUT_FIELDS = {'run_root', 'run_id', 'candidates'}
METHOD_INPUT_FIELDS = {'selection_effect_id', 'selection_plan_id', 'proposal_role', 'anchor_parent'}
LIMITS = {'wall_seconds': 120, 'cpu_seconds': 120}


def input_fields(config):
    return BASE_INPUT_FIELDS | (METHOD_INPUT_FIELDS if config.get('pablo_harness') else set())


def selection_binding(store, run_id, inputs):
    """A new Harness scoring operation must execute a real method-selected set."""
    config = store.run(run_id)['config']
    if set(inputs) != input_fields(config):
        raise ValueError('Scoring inputs must include exactly the frozen Harness selection contract')
    if not config.get('pablo_harness'):
        return None, None
    from . import harness, methods
    for key in ('selection_effect_id', 'selection_plan_id', 'proposal_role', 'anchor_parent'):
        if not isinstance(inputs[key], str) or not inputs[key] or len(inputs[key]) > 4096:
            raise ValueError('Scoring selection references and role must be bounded nonempty strings')
    selected = methods.validate_evaluation_plan(store, run_id,
        selection_effect_id=inputs['selection_effect_id'], plan_id=inputs['selection_plan_id'],
        candidates=inputs['candidates'], proposal_role=inputs['proposal_role'], anchor_parent=inputs['anchor_parent'])
    binding = harness.validate_proposal(store, run_id, inputs['proposal_role'], inputs['anchor_parent'])
    harness.validate_binding(store, run_id, binding, inputs['candidates'])
    return selected, binding


def required_evidence(store, run_id, inputs):
    if not store.run(run_id)['config'].get('pablo_harness'):
        return []
    from rsi_lab.controller import identity
    selected, _ = selection_binding(store, run_id, inputs)
    publication = store.get(run_id, 'registration', identity('publication', selected['selection_effect_id']))
    data = json.loads(store.read_blob(publication['publication_ref']))
    return [row['id'] for row in data['facts']]


class _ReadStore(Store):
    """Use Store's verified read API without creating or migrating any files."""
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.blobs = self.root / 'objects'
        path = self.root / 'ledger.sqlite3'
        if not path.is_file() or not self.blobs.is_dir():
            raise ValueError('An existing initialized run ledger is required')
        self.db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
        self.db.row_factory = sqlite3.Row


def _binding(store, run_id):
    config = store.run(run_id)['config']
    if config.get('pablo_run_root') != str(store.root):
        raise ValueError('Run root differs from its frozen host binding')
    if config.get('max_external') != 1:
        raise ValueError('Molecular evaluation requires serial external execution')
    maximum = config.get('pablo_max_new_evaluations')
    if type(maximum) is not int or maximum < 1:
        raise ValueError('A positive frozen evaluation limit is required')
    initial = json.loads(store.read_blob(config['pablo_initial_ref']))
    if set(initial) != {'candidates', 'scores'} or not initial['candidates']:
        raise ValueError('Initial observations must contain only candidates and scores')
    if len(initial['candidates']) != len(initial['scores']):
        raise ValueError('Initial observation columns disagree')
    canonical = [oracle.canonicalize(value) for value in initial['candidates']]
    if None in canonical or canonical != initial['candidates'] or len(set(canonical)) != len(canonical):
        raise ValueError('Initial observations must be valid distinct canonical molecules')
    if any(type(value) not in (float, int) or not math.isfinite(value) for value in initial['scores']):
        raise ValueError('Initial scores must be finite numbers')
    expected = config.get('pablo_oracle_fingerprint')
    if expected is not None and expected != oracle.fingerprint():
        raise ValueError('Frozen evaluator implementation or dependencies changed')
    return config, initial, maximum


def _effects(store, run_id):
    return sorted((effect for effect in store.list(run_id, 'effect')
                   if effect.get('metadata', {}).get('adapter_id') == ADAPTER_ID),
                  key=lambda effect: effect['created_seq'])


def read_progress(store, run_id):
    """Reconstruct scientific state from immutable successful receipts only."""
    store.verify_history(run_id)
    _, initial, maximum = _binding(store, run_id)
    candidates, scores = list(initial['candidates']), list(initial['scores'])
    seen, observations = set(candidates), []
    for effect in _effects(store, run_id):
        if effect['status'] != 'succeeded':
            continue
        if effect.get('accounting_unknown'):
            raise ValueError('Successful evaluation has unresolved accounting; reconcile before continuing')
        packet = json.loads(store.read_blob(effect['packet_ref']))
        receipt = json.loads(store.read_blob(effect['result_ref']))
        result = receipt['result']
        if (receipt['status'] != 'succeeded' or result['effect_id'] != effect['id']
                or result['packet_sha256'] != effect['packet_hash']
                or result['evaluation_start'] != len(observations)
                or packet['evaluation_start'] != len(observations)):
            raise ValueError('Evaluation receipt does not extend the recorded trajectory')
        rows = result['evaluations']
        if len(rows) != len(packet['accepted']):
            raise ValueError('Evaluation receipt differs from its admitted candidates')
        for row, admitted in zip(rows, packet['accepted']):
            ordinal = len(observations) + 1
            if (row['slot_id'] != f'evaluation-{ordinal:03d}'
                    or row['evaluation'] != ordinal or row['candidate'] != admitted['canonical']
                    or row['source_index'] != admitted['source_index'] or row['candidate'] in seen
                    or type(row['score']) not in (int, float) or not math.isfinite(row['score'])):
                raise ValueError('Invalid, duplicate or reordered evaluation receipt')
            seen.add(row['candidate'])
            candidates.append(row['candidate']); scores.append(row['score']); observations.append(row)
    if len(observations) > maximum:
        raise ValueError('Recorded evaluations exceed the frozen limit')
    top20 = [{'candidate': candidates[index], 'score': scores[index]}
             for index in sorted(range(len(scores)), key=lambda index: (-scores[index], index))[:20]]
    return {'initial_count': len(initial['candidates']), 'new_evaluations': len(observations),
            'remaining_evaluations': maximum - len(observations), 'top20': top20,
            'candidates': candidates, 'scores': scores, 'observations': observations,
            'trajectory_sha256': digest({'candidates': candidates, 'scores': scores})}


def queued_intents(store, run_id):
    """All accepted but unfinished scoring intents, including Work without Effect.

    This is planning state, not evidence that any molecule was scored or even
    admitted to the scorer. Never truncate the set used for admission checks.
    """
    rows = []
    for work in store.list(run_id, 'work'):
        if (work['kind'] != 'diagnostic' or work['status'] not in {'ready', 'leased'}
                or work['inputs'].get('capability_id') != ADAPTER_ID):
            continue
        inputs = json.loads(work['inputs']['inputs_json'])
        if inputs.get('run_id') != run_id or inputs.get('run_root') != str(store.root):
            raise ValueError('Queued scoring intent has a different run binding')
        candidates = sorted({canonical for value in inputs['candidates']
                             if (canonical := oracle.canonicalize(value)) is not None})
        rows.append({'work_id': work['id'], 'work_version': work['version'],
                     'status': work['status'], 'created_seq': work['created_seq'],
                     'effect_id': work.get('effect_id'),
                     'source_proposal_id': work['inputs'].get('source_proposal_id'),
                     'canonical_candidates': candidates})
    return sorted(rows, key=lambda row: (row['created_seq'], row['work_id']))


def claim_identities(store, run_id, inputs):
    """Canonical identity provider; the generic kernel owns all claims."""
    if inputs.get('run_id') != run_id or inputs.get('run_root') != str(store.root):
        raise ValueError('Scoring identities must target the current run')
    values = inputs.get('candidates')
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_CANDIDATES
            or any(not isinstance(value, str) or len(value) > 4096 for value in values)):
        raise ValueError('Scoring identities require a bounded candidate list')
    return sorted({canonical for value in values
                   if (canonical := oracle.canonicalize(value)) is not None})


def completed_identities(effect, publication):
    """Only actual finite scoring rows cover identity claims, never duplicates."""
    data = publication.get('data', {})
    rows = data.get('evaluations')
    if (not isinstance(rows, list) or data.get('effect_id') != effect['id']
            or any(not isinstance(row, dict) or not isinstance(row.get('candidate'), str)
                   or type(row.get('score')) not in (int, float) or not math.isfinite(row['score'])
                   for row in rows)):
        raise ValueError('Identity coverage requires actual finite bound evaluation rows')
    return sorted({row['candidate'] for row in rows})


def admit_work(store, run_id, inputs):
    """Atomic routing guard, called on the controller's current transaction.

    Refuse redundant work explicitly instead of attributing an older operation
    to a later recommendation. Final scoring still performs its own guard.
    """
    if inputs.get('run_id') != run_id or inputs.get('run_root') != str(store.root):
        raise ValueError('Scoring intent must target the current run')
    progress = read_progress(store, run_id)
    selection_binding(store, run_id, inputs)
    requested = {canonical for value in inputs['candidates']
                 if (canonical := oracle.canonicalize(value)) is not None}
    conflicts = [row for row in queued_intents(store, run_id)
                 if requested.intersection(row['canonical_candidates'])]
    if conflicts:
        raise ValueError('Scoring candidates already scheduled; do not duplicate pending work: '
                         + ', '.join(row['work_id'] for row in conflicts))
    pending = {}
    for effect in _effects(store, run_id):
        if effect['status'] in {'prepared', 'dispatching', 'unknown'}:
            packet = json.loads(store.read_blob(effect['packet_ref']))
            for row in packet['accepted']:
                pending[row['canonical']] = effect['id']
    if requested.intersection(pending):
        raise ValueError('Scoring candidates already reserved by pending effects: '
                         + ', '.join(sorted({pending[s] for s in requested.intersection(pending)})))
    if not requested.difference(progress['candidates']):
        raise ValueError('Scoring intent has no new valid identity; all candidates are already evaluated or invalid')


def context(store, run_id):
    """Bounded authoritative planning snapshot for both Lab and Meta."""
    progress = read_progress(store, run_id)
    all_rows = queued_intents(store, run_id)
    rows = []
    for row in all_rows:
        # Bound bytes, not merely record count: a legal SMILES can be long.
        if len(encode(rows + [row])) > 48000:
            break
        rows.append(row)
    result = {'schema_version': 'pablo_queued_intents_v1',
            'authority': 'controller_current_planning_state_not_scientific_evidence',
            'trajectory_sha256': progress['trajectory_sha256'],
            'as_of_observations': len(progress['candidates']),
            'new_evaluations': progress['new_evaluations'],
            'remaining_evaluations': progress['remaining_evaluations'],
            'queued_work': rows, 'queued_work_count': len(all_rows),
            'complete': len(rows) == len(all_rows), 'omitted_work_count': len(all_rows) - len(rows),
            'guidance': 'Do not propose or adopt scoring of identities already queued. Await their existing work/result. '
                        'If this summary is incomplete, lookup proposed identities before choosing them. '
                        'Routing checks all queued identities atomically; execution checks novelty again. '
                        'Existing work is not evidence of recommendation-caused progress.'}
    if store.run(run_id)['config'].get('pablo_harness'):
        from . import harness
        result['harness'] = harness.context(store, run_id, progress)
    return result


def _build_packet(store, inputs, *, exclude_effect=None):
    config, _, maximum = _binding(store, inputs['run_id'])
    selected, harness_binding = selection_binding(store, inputs['run_id'], inputs)
    progress = read_progress(store, inputs['run_id'])
    seen = set(progress['candidates'])
    held, pending = 0, []
    for effect in _effects(store, inputs['run_id']):
        if effect['id'] == exclude_effect or effect['status'] not in {'prepared', 'dispatching', 'unknown'}:
            continue
        packet = json.loads(store.read_blob(effect['packet_ref']))
        pending.append(effect['id'])
        for row in packet['accepted']:
            seen.add(row['canonical']); held += 1
    remaining = max(0, maximum - progress['new_evaluations'] - held)
    filtered, accepted = [], []
    for index, candidate in enumerate(inputs['candidates']):
        canonical = oracle.canonicalize(candidate)
        row = {'source_index': index, 'candidate': candidate, 'canonical': canonical}
        if canonical is None:
            row['status'] = 'invalid'
        elif canonical in seen:
            row['status'] = 'duplicate'
        else:
            seen.add(canonical)
            if len(accepted) >= remaining:
                row['status'] = 'budget_clipped'
            else:
                row['status'] = 'accepted'
                accepted.append({'source_index': index, 'canonical': canonical})
        filtered.append(row)
    if selected and (len(accepted) != len(selected['selected_candidates'])
                     or any(row['status'] != 'accepted' for row in filtered)):
        raise ValueError('The complete method-selected batch must remain admissible without clipping or replacement')
    return {'schema_version': 'pablo_evaluation_packet_v2' if selected else 'pablo_evaluation_packet_v1', **inputs,
            **({'selection_binding': selected, 'harness_binding': harness_binding} if selected else {}),
            'initial_ref': config['pablo_initial_ref'], 'max_new_evaluations': maximum,
            'evaluation_start': progress['new_evaluations'], 'trajectory_sha256': progress['trajectory_sha256'],
            'pending_effect_ids': pending, 'held_evaluations': held,
            'accepted': accepted, 'filters': filtered,
            'oracle_fingerprint': oracle.fingerprint(), 'limits': LIMITS}


def _prepare(inputs):
    if (not isinstance(inputs, dict) or not BASE_INPUT_FIELDS.issubset(inputs)
            or set(inputs) - (BASE_INPUT_FIELDS | METHOD_INPUT_FIELDS)):
        raise ValueError('Evaluation needs the complete declared run and candidate contract')
    if (not isinstance(inputs['run_root'], str) or not Path(inputs['run_root']).is_absolute()
            or str(Path(inputs['run_root']).resolve()) != inputs['run_root']
            or not isinstance(inputs['run_id'], str) or not inputs['run_id']):
        raise ValueError('Canonical absolute run root and explicit run identity are required')
    values = inputs['candidates']
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_CANDIDATES
            or any(not isinstance(value, str) or len(value) > 4096 for value in values)):
        raise ValueError('Supply one to twenty bounded candidate strings')
    store = _ReadStore(inputs['run_root'])
    try:
        return _build_packet(store, inputs)
    finally:
        store.close()


def _reservations(packet):
    return {'model_calls': 0, 'tokens': 0, 'tool_calls': 1, **LIMITS}


def _authorize(store, effect, packet, raw):
    store.verify_history(packet['run_id'])
    _binding(store, packet['run_id'])
    if (effect['status'] != 'dispatching' or effect['run_id'] != packet['run_id']
            or effect['kind'] != EFFECT_KIND or store.get(packet['run_id'], 'effect', effect['id']) != effect
            or effect.get('metadata', {}).get('adapter_id') != ADAPTER_ID
            or effect.get('metadata', {}).get('adapter_version') != VERSION
            or digest(raw) != effect['packet_ref'] or digest(raw) != effect['packet_hash']
            or store.read_blob(effect['packet_ref']) != raw):
        raise ValueError('Evaluation requires its exact persisted dispatching effect')
    work = store.get(packet['run_id'], 'work', effect['work_id'])
    proposal_id = work['inputs'].get('source_proposal_id')
    if not proposal_id or not work['inputs'].get('lab_continuation'):
        raise ValueError('Only an authenticated Lab proposal may request evaluation')
    proposal = store.get(packet['run_id'], 'proposal', proposal_id)
    command = proposal.get('command') or {}
    payload = command.get('payload') or {}
    origin = store.get(packet['run_id'], 'effect', proposal['effect_id'])
    origin_receipt = json.loads(store.read_blob(origin['result_ref']))
    original = {key: packet[key] for key in input_fields(store.run(packet['run_id'])['config'])}
    if (work['kind'] != 'diagnostic' or proposal['status'] != 'applied'
            or command.get('mode') != 'lab' or command.get('action') != 'request_diagnostic'
            or origin['kind'] != 'lab' or origin['status'] != 'succeeded' or origin.get('accounting_unknown')
            or origin.get('metadata', {}).get('mode') != 'lab'
            or origin_receipt.get('response', {}).get('command') != command
            or payload.get('capability_id') != ADAPTER_ID or payload.get('capability_version') != VERSION
            or json.loads(payload.get('inputs_json', '{}')) != original
            or work['inputs'].get('capability_id') != ADAPTER_ID
            or work['inputs'].get('capability_version') != VERSION
            or json.loads(work['inputs'].get('inputs_json', '{}')) != original):
        raise ValueError('Evaluation is not bound to an applied Lab diagnostic proposal')
    if _build_packet(store, original, exclude_effect=effect['id']) != packet:
        raise ValueError('Evaluation state or admitted input changed before scoring')
    if packet['pending_effect_ids']:
        raise ValueError('Unresolved earlier evaluations must be reconciled first')


def _cpu():
    row = resource.getrusage(resource.RUSAGE_SELF)
    return row.ru_utime + row.ru_stime


def _sync_json(path, value):
    with path.open('xb') as stream:
        stream.write(encode(value)); stream.flush(); os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _execute(effect, packet_bytes):
    started, cpu_before = time.monotonic(), _cpu()
    store = None
    try:
        packet = json.loads(packet_bytes)
        store = _ReadStore(packet['run_root'])
        _authorize(store, effect, packet, packet_bytes)
        before = read_progress(store, packet['run_id'])
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        return {'status': 'failed_definite', 'actual_costs': {'model_calls': 0, 'tokens': 0, 'tool_calls': 1,
                'wall_seconds': time.monotonic() - started, 'cpu_seconds': _cpu() - cpu_before},
                'result': {'rejected_before_scoring': True},
                'execution': {'reason': str(error), 'scored_candidates': 0}}
    finally:
        if store is not None:
            store.close()
    folder = Path(packet['run_root']) / 'pablo_evaluations' / effect['id']
    folder.parent.mkdir(exist_ok=True)
    try:
        folder.mkdir()
    except FileExistsError:
        terminal = folder / 'receipt.json'
        if terminal.is_file():
            saved = json.loads(terminal.read_bytes())
            if (saved['result']['effect_id'] != effect['id']
                    or saved['result']['packet_sha256'] != digest(packet_bytes)):
                raise ValueError('Archived evaluation receipt belongs to another attempt')
            return saved
        return {'status': 'unknown', 'actual_costs': {},
                'reason': 'An interrupted evaluation journal exists; reconcile without rescoring'}
    descriptor = os.open(folder.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_json(folder / 'binding.json', {'effect_id': effect['id'], 'packet_sha256': digest(packet_bytes)})
    rows = []
    try:
        with (folder / 'evaluations.jsonl').open('xb') as journal:
            for admitted in packet['accepted']:
                value = oracle.score(admitted['canonical'])
                if not math.isfinite(value):
                    raise ValueError('Evaluator returned a nonfinite score')
                ordinal = packet['evaluation_start'] + len(rows) + 1
                row = {'slot_id': f'evaluation-{ordinal:03d}', 'evaluation': ordinal,
                       'candidate': admitted['canonical'], 'source_index': admitted['source_index'], 'score': value}
                journal.write(encode({'effect_id': effect['id'], 'packet_sha256': digest(packet_bytes), **row}) + b'\n')
                journal.flush(); os.fsync(journal.fileno()); rows.append(row)
    except Exception:
        return {'status': 'unknown', 'actual_costs': {'model_calls': 0, 'tokens': 0, 'tool_calls': 1,
                'wall_seconds': time.monotonic() - started, 'cpu_seconds': _cpu() - cpu_before},
                'result': {'effect_id': effect['id'], 'packet_sha256': digest(packet_bytes),
                           'durable_partial_evaluations': rows},
                'reason': 'Evaluation interrupted; preserve journal and reconcile without rescoring'}
    candidates = before['candidates'] + [row['candidate'] for row in rows]
    scores = before['scores'] + [row['score'] for row in rows]
    count = before['new_evaluations'] + len(rows)
    result = {'effect_id': effect['id'], 'packet_sha256': digest(packet_bytes),
              **({key: packet[key] for key in ('selection_binding', 'harness_binding')} if 'selection_binding' in packet else {}),
              'evaluation_start': packet['evaluation_start'], 'evaluations': rows, 'filters': packet['filters'],
              'filter_counts': {status: sum(row['status'] == status for row in packet['filters'])
                                for status in ('accepted', 'invalid', 'duplicate', 'budget_clipped')},
              'initial_count': before['initial_count'], 'new_evaluations': count,
              'remaining_evaluations': packet['max_new_evaluations'] - count,
              'top20': [{'candidate': candidates[index], 'score': scores[index]}
                        for index in sorted(range(len(scores)), key=lambda index: (-scores[index], index))[:20]]}
    receipt = {'status': 'succeeded', 'actual_costs': {'model_calls': 0, 'tokens': 0, 'tool_calls': 1,
               'wall_seconds': time.monotonic() - started, 'cpu_seconds': _cpu() - cpu_before},
               'result': result, 'execution': {'evaluator_fingerprint': packet['oracle_fingerprint'],
               'journal_sha256': digest((folder / 'evaluations.jsonl').read_bytes()), 'scored_candidates': len(rows)}}
    _sync_json(folder / 'receipt.json', receipt)
    return receipt


def _publication(effect, receipt):
    result = dict(receipt['result'])
    if 'harness_binding' in result:
        from . import harness
        result['harness_credit'] = harness.publication_credit(effect, {'data': result}, result['harness_binding'])
    return {'facts': [{'id': 'pablo_result_' + digest(effect['id'])[:24], 'kind': 'raw_fact',
             'content': result, 'provenance': {'effect_id': effect['id'], 'adapter_id': ADAPTER_ID,
             'adapter_version': VERSION, 'execution_ref': effect['result_ref']}}],
            'conclusions': [], 'data': result}


def prepare(inputs):
    return _prepare(inputs)


def reservations(packet):
    return _reservations(packet)


def execute(effect, packet_bytes):
    return _execute(effect, packet_bytes)


def publication(effect, receipt):
    return _publication(effect, receipt)


def make_adapter():
    return Adapter(adapter_id=ADAPTER_ID, version=VERSION, effect_kind=EFFECT_KIND,
        manifest={'domain': 'molecular_optimization', 'scope': 'Evaluate up to twenty candidate SMILES against the frozen hidden objective',
                  'input_contract': {'run_root': 'exact frozen absolute run directory', 'run_id': 'exact run identity',
                                     'candidates': 'one to twenty SMILES strings',
                                     'harness_enabled_required_fields': sorted(METHOD_INPUT_FIELDS)},
                  'output_contract': 'Measured scores, exact candidate dispositions, evaluation slots and current best observations',
                  'limitations': ['Only authenticated Lab proposals may score', 'Serial execution',
                                  'Invalid and duplicate molecules do not consume evaluation slots',
                                  'Unknown evaluations require reconciliation, never automatic retry']},
        prepare=prepare, reservations=reservations, execute=execute, publication=publication,
        admit_work=admit_work, context=context, claim_identities=claim_identities,
        completed_identities=completed_identities,
        required_evidence=required_evidence,
        source_paths=(str(Path(oracle.__file__).resolve()),
                      str(Path(__file__).with_name('harness.py')), str(Path(__file__).with_name('methods.py'))))
