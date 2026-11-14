"""Auditable, bounded candidate selection from observed data and literature.

This host capability implements explicit method primitives, not a reproduction
of an arbitrary cited paper. It never evaluates the hidden objective. A plan is
an immutable selection receipt; scoring must consume its exact selected set.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import time

from rsi_lab.adapters import Adapter
from rsi_lab.store import digest, encode
from . import adapter
from .literature import adapter as literature

ADAPTER_ID = 'pablo.select_candidates'
VERSION = '1.0'
EFFECT_KIND = 'pablo_method_selection'
MAX_POOL = 256
MAX_TRAINING = 10000
METHODS = {
    'fingerprint_diversity': {
        'topics': ['molecular_diversity', 'representation_bias', 'budgeted_search', 'sample_efficient_search'],
        'algorithm': 'Greedy maximum minimum Morgan distance to observed and already-selected molecules.'},
    'knn_ucb_diversity': {
        'topics': ['sample_efficient_search', 'surrogate_uncertainty', 'black_box_optimization', 'budgeted_search'],
        'algorithm': 'Similarity-weighted kNN observed-score mean plus an uncertainty proxy, with greedy diversity.'},
}
FIELDS = {'run_root', 'run_id', 'proposal_role', 'anchor_parent', 'candidates', 'select_count',
          'method', 'parameters', 'literature_evidence_ids'}
PARAMETERS = {'neighbors', 'exploration_weight', 'diversity_weight'}
FINGERPRINT = {'kind': 'Morgan_bit_vector', 'radius': 2, 'bits': 2048, 'include_chirality': False}


def _chemistry():
    # RDKit is a public representation library. Do not import or invoke oracle.score.
    from rdkit import Chem, DataStructs, rdBase
    from rdkit.Chem import rdFingerprintGenerator
    return Chem, DataStructs, rdFingerprintGenerator, rdBase.rdkitVersion


def _canonical(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError('Method candidates require nonempty bounded SMILES')
    Chem, _, _, _ = _chemistry()
    mol = Chem.MolFromSmiles(value)
    if mol is None:
        raise ValueError('Method candidates must be valid molecules')
    return Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)


def _inputs(inputs):
    if not isinstance(inputs, dict) or set(inputs) != FIELDS:
        raise ValueError('Selection requires the complete fixed input contract')
    if (not isinstance(inputs['run_root'], str) or not Path(inputs['run_root']).is_absolute()
            or str(Path(inputs['run_root']).resolve()) != inputs['run_root']
            or not isinstance(inputs['run_id'], str) or not inputs['run_id']):
        raise ValueError('Selection requires the current absolute run binding')
    if (not isinstance(inputs['proposal_role'], str) or inputs['proposal_role'] not in {'local', 'challenge'}
            or not isinstance(inputs['method'], str) or inputs['method'] not in METHODS):
        raise ValueError('Selection requires one local/challenge role and an implemented method')
    candidates = inputs['candidates']
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_POOL:
        raise ValueError('Selection pool must contain 1..256 molecules')
    canonical = sorted({_canonical(value) for value in candidates})
    count = inputs['select_count']
    if type(count) is not int or not 1 <= count <= min(20, len(canonical)):
        raise ValueError('Selection count must be 1..20 and fit the distinct pool')
    p = inputs['parameters']
    if (not isinstance(p, dict) or set(p) != PARAMETERS
            or type(p['neighbors']) is not int or not 1 <= p['neighbors'] <= 32
            or any(type(p[k]) not in (int, float) or not math.isfinite(p[k]) or not 0 <= p[k] <= 2
                   for k in ('exploration_weight', 'diversity_weight'))):
        raise ValueError('Bound neighbors to 1..32 and finite acquisition weights to 0..2')
    ids = inputs['literature_evidence_ids']
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 4
            or any(not isinstance(fid, str) or not fid or len(fid) > 200 for fid in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Selection requires 1..4 distinct actual literature fact IDs')
    return {**inputs, 'candidates': canonical, 'anchor_parent': _canonical(inputs['anchor_parent']),
            'parameters': dict(p), 'literature_evidence_ids': sorted(ids)}


def _literature_sources(store, run_id, inputs):
    sources = []
    for fid in inputs['literature_evidence_ids']:
        fact = store.get(run_id, 'fact', fid)
        provenance = fact.get('provenance', {})
        # Trusted imports preserve the complete original provenance, sometimes
        # through several continuation segments. They do not upgrade truth.
        while 'original_provenance' in provenance:
            provenance = provenance['original_provenance']
        content = json.loads(store.read_blob(fact['content_ref']))
        if (fact['kind'] != 'source_statement' or provenance.get('adapter_id') != literature.ADAPTER_ID
                or content.get('provider') != literature.PROVIDER
                or content.get('topic') not in METHODS[inputs['method']]['topics']
                or content.get('query') != literature.TOPICS.get(content.get('topic'))
                or content.get('http_status') != 200 or content.get('response_complete') is not True
                or not isinstance(content.get('records'), list) or not content['records']):
            raise ValueError('Method needs an actual successful, applicable literature publication')
        records = [row for row in content['records'] if isinstance(row, dict)
                   and isinstance(row.get('abstract_text'), str) and row['abstract_text'].strip()]
        if not records:
            raise ValueError('Metadata-only or empty search cannot ground an executable method')
        sources.append({'fact_id': fid, 'content_sha256': fact['content_ref'],
                        'response_sha256': content['response_sha256'], 'topic': content['topic'],
                        'records': [{'provider_source': row['provider_source'], 'publication_id': row['publication_id'],
                                     'abstract_sha256': digest(row['abstract_text'].encode())} for row in records],
                        'relationship': 'method_motivation_not_paper_reproduction_or_validated_transfer'})
    return sources


def normalize_recommendation(store, run_id, inputs):
    normalized = _inputs(inputs)
    if (normalized['run_id'] != run_id or normalized['run_root'] != str(store.root)
            or store.run(run_id)['config'].get('pablo_run_root') != str(store.root)):
        raise ValueError('Selection contract targets a different run')
    return {**normalized, 'literature_sources': _literature_sources(store, run_id, normalized),
            'implementation': VERSION, 'fingerprint': dict(FINGERPRINT)}


def required_evidence(store, run_id, inputs):
    """The generic request gate verifies complete delivery and explicit citation."""
    return normalize_recommendation(store, run_id, inputs)['literature_evidence_ids']


def _packet(store, run_id, inputs):
    normalized = normalize_recommendation(store, run_id, inputs)
    progress = adapter.read_progress(store, run_id)
    if len(progress['candidates']) > MAX_TRAINING:
        raise ValueError('Observed training history exceeds the explicit 10000-observation capacity')
    if normalized['anchor_parent'] not in progress['candidates']:
        raise ValueError('Anchor parent must be an actually observed molecule')
    if normalized['select_count'] > progress['remaining_evaluations']:
        raise ValueError('Selection cannot exceed remaining scoring slots')
    harness_binding = None
    if store.run(run_id)['config'].get('pablo_harness'):
        from . import harness
        harness_binding = harness.validate_proposal(store, run_id, normalized['proposal_role'],
                                                    normalized['anchor_parent'], progress=progress)
        if normalized['select_count'] > harness_binding['max_candidates']:
            raise ValueError('Method selection exceeds the current Harness batch allowance')
    known = set(progress['candidates'])
    pending = {value for work in adapter.queued_intents(store, run_id) for value in work['canonical_candidates']}
    for effect in adapter._effects(store, run_id):
        if effect['status'] in {'prepared', 'dispatching', 'unknown'}:
            old = json.loads(store.read_blob(effect['packet_ref']))
            pending.update(row['canonical'] for row in old['accepted'])
    filters = []
    incumbent = progress['candidates'][max(range(len(progress['scores'])),
                                         key=lambda i: (progress['scores'][i], -i))]
    for value in normalized['candidates']:
        status = 'already_observed' if value in known else 'pending' if value in pending else 'eligible'
        reason = None
        if status == 'eligible' and harness_binding is not None:
            try:
                harness.validate_candidates([value], harness_binding, incumbent,
                                            harness.configuration(store.run(run_id)['config']))
            except ValueError as exc:
                status, reason = 'role_ineligible', str(exc)
        filters.append({'candidate': value, 'status': status, **({'reason': reason} if reason else {})})
    eligible = [row['candidate'] for row in filters if row['status'] == 'eligible']
    if len(eligible) < normalized['select_count']:
        raise ValueError('Insufficient novel nonpending candidates for the requested selection')
    _, _, _, version = _chemistry()
    return {'schema_version': 'pablo_method_packet_v1', 'inputs': normalized,
            'observations': {'candidates': progress['candidates'], 'scores': progress['scores']},
            'trajectory_sha256': progress['trajectory_sha256'], 'as_of_observations': len(progress['candidates']),
            'eligible': eligible, 'filters': filters, 'rdkit_version': version,
            'harness_binding': harness_binding,
            'implementation_sha256': digest(Path(__file__).read_bytes())}


def prepare(inputs):
    normalized = _inputs(inputs)
    store = adapter._ReadStore(normalized['run_root'])
    try:
        with store.read_snapshot():
            return _packet(store, normalized['run_id'], inputs)
    finally:
        store.close()


def admit_work(store, run_id, inputs):
    _packet(store, run_id, inputs)


def select(packet):
    """Deterministic actual numerical method; consumes only the frozen dataset."""
    Chem, DataStructs, generators, version = _chemistry()
    if version != packet['rdkit_version']:
        raise ValueError('RDKit version differs from the method plan')
    generator = generators.GetMorganGenerator(radius=2, fpSize=2048, includeChirality=False)
    fingerprint = lambda value: generator.GetFingerprint(Chem.MolFromSmiles(value))
    observations = packet['observations']
    training = [fingerprint(value) for value in observations['candidates']]
    fps = {value: fingerprint(value) for value in packet['eligible']}
    p, inputs = packet['inputs']['parameters'], packet['inputs']
    rows = {}
    for value in packet['eligible']:
        similarities = list(DataStructs.BulkTanimotoSimilarity(fps[value], training))
        closest = sorted(range(len(similarities)), key=lambda i: (-similarities[i], i))[:p['neighbors']]
        weights = [max(similarities[i], 1e-6) for i in closest]
        total = sum(weights)
        mean = sum(weight * observations['scores'][i] for weight, i in zip(weights, closest)) / total
        variance = sum(weight * (observations['scores'][i] - mean) ** 2
                       for weight, i in zip(weights, closest)) / total
        distance = 1 - max(similarities)
        uncertainty = math.sqrt(variance) + distance
        rows[value] = {'candidate': value, 'predicted_mean': mean,
                       'uncertainty_proxy': uncertainty, 'nearest_observed_similarity': max(similarities),
                       'nearest_observed_ordinal': closest[0] + 1,
                       'nearest_observed_score': observations['scores'][closest[0]],
                       'neighbor_count': len(closest), 'observed_novelty_distance': distance}
    selected, choices = [], []
    while len(selected) < inputs['select_count']:
        evaluated = []
        for value, row in rows.items():
            if value in selected:
                continue
            within = min((1 - DataStructs.TanimotoSimilarity(fps[value], fps[previous]) for previous in selected), default=1.0)
            diversity = min(row['observed_novelty_distance'], within)
            acquisition = (diversity if inputs['method'] == 'fingerprint_diversity' else
                           row['predicted_mean'] + p['exploration_weight'] * row['uncertainty_proxy']
                           + p['diversity_weight'] * diversity)
            evaluated.append((acquisition, value, diversity))
        acquisition, value, diversity = min(evaluated, key=lambda item: (-item[0], item[1]))
        selected.append(value)
        choices.append({**rows[value], 'selection_rank': len(selected), 'acquisition_value': acquisition,
                        'selection_diversity_distance': diversity})
    body = {'schema_version': 'pablo_method_plan_v1', 'input_contract': inputs,
            'trajectory_sha256': packet['trajectory_sha256'], 'as_of_observations': packet['as_of_observations'],
            'training_sha256': digest(observations), 'pool_sha256': digest(inputs['candidates']),
            'implementation_sha256': packet['implementation_sha256'], 'rdkit_version': version,
            'method': inputs['method'], 'proposal_role': inputs['proposal_role'], 'anchor_parent': inputs['anchor_parent'],
            'harness_binding': packet['harness_binding'],
            'selected_candidates': selected, 'selection_rows': choices,
            'candidate_predictions': list(rows.values()), 'filters': packet['filters'],
            'literature_sources': inputs['literature_sources'],
            'new_scores': 0, 'limitations': [
                'Method primitives are this reviewed implementation; the cited papers were not reproduced.',
                'kNN uncertainty is an uncalibrated proxy, not a confidence interval or proof of improvement.',
                'Only existing observations were used; no hidden objective was queried.',
                'Pool coverage limits selection; a more diverse pool is not guaranteed a higher score.',
                'Scoring must recheck state and execute precisely this selected set.']}
    return {**body, 'plan_id': 'method_plan_' + digest(body)}


def reservations(packet):
    return {'model_calls': 0, 'tokens': 0, 'tool_calls': 1, 'wall_seconds': 120, 'cpu_seconds': 120}


def execute(effect, raw):
    start, cpu = time.monotonic(), time.process_time()
    costs = {'model_calls': 0, 'tokens': 0, 'tool_calls': 1, 'wall_seconds': 0, 'cpu_seconds': 0}
    try:
        packet = json.loads(raw)
        inputs = packet['inputs']
        store = adapter._ReadStore(inputs['run_root'])
        try:
            with store.read_snapshot():
                if (effect['status'] != 'dispatching' or effect['kind'] != EFFECT_KIND
                        or effect['run_id'] != inputs['run_id']
                        or effect.get('metadata', {}).get('adapter_id') != ADAPTER_ID
                        or effect.get('metadata', {}).get('adapter_version') != VERSION
                        or store.get(effect['run_id'], 'effect', effect['id']) != effect
                        or effect['packet_hash'] != digest(raw)
                        or store.read_blob(effect['packet_ref']) != raw):
                    raise ValueError('Method execution requires its exact persisted dispatching effect')
                original = {key: inputs[key] for key in FIELDS}
                if _packet(store, effect['run_id'], original) != packet:
                    raise ValueError('Method observations, candidates, literature or implementation changed before execution')
        finally:
            store.close()
        result = select(packet)
        result.update(effect_id=effect['id'], packet_sha256=digest(raw))
        return {'status': 'succeeded', 'actual_costs': costs, 'result': result}
    except (KeyError, ValueError, OSError, TypeError) as exc:
        return {'status': 'failed_definite', 'actual_costs': costs, 'error': str(exc)}
    finally:
        costs.update(wall_seconds=time.monotonic() - start, cpu_seconds=time.process_time() - cpu)


def _validated_result(effect, receipt):
    result = receipt['result']
    if (receipt.get('status') != 'succeeded' or result.get('effect_id') != effect['id']
            or result.get('packet_sha256') != effect['packet_hash']
            or result.get('plan_id') != 'method_plan_' + digest({k: v for k, v in result.items()
                if k not in {'plan_id', 'effect_id', 'packet_sha256'}})):
        raise ValueError('Selection result is not bound to its immutable method plan')
    selected = result['selected_candidates']
    if (len(selected) != result['input_contract']['select_count'] or len(set(selected)) != len(selected)
            or selected != [r['candidate'] for r in result['selection_rows']]):
        raise ValueError('Selection receipt has inconsistent candidate coverage')
    return result


def publication(effect, receipt):
    result = _validated_result(effect, receipt)
    return {'facts': [{'id': 'pablo_selection_' + digest(effect['id'])[:24], 'kind': 'raw_fact',
            'content': result, 'provenance': {'effect_id': effect['id'], 'adapter_id': ADAPTER_ID,
             'adapter_version': VERSION, 'receipt_ref': effect['result_ref'],
             'scope': 'actual candidate selection computation, not actual scores or paper reproduction'}}],
            'conclusions': [], 'data': result}


def validate_evaluation_plan(store, run_id, *, selection_effect_id, plan_id, candidates,
                             proposal_role=None, anchor_parent=None):
    """Scoring admission hook: no invented, stale, changed or partial plan use.

    The host calls this before reserving scoring Work; its normal identity
    claims then guard the time between admission and scoring execution.
    """
    effect = store.get(run_id, 'effect', selection_effect_id)
    if (effect['status'] != 'succeeded' or effect.get('accounting_unknown')
            or effect.get('metadata', {}).get('adapter_id') != ADAPTER_ID
            or effect.get('metadata', {}).get('adapter_version') != VERSION):
        raise ValueError('Scoring needs a settled actual method selection Effect')
    result = _validated_result(effect, json.loads(store.read_blob(effect['result_ref'])))
    publications = store.list(run_id, 'registration', kind='adapter_publication', effect_id=effect['id'])
    if (len(publications) != 1 or publications[0].get('status') != 'complete'
            or publications[0].get('adapter_id') != ADAPTER_ID
            or json.loads(store.read_blob(publications[0]['publication_ref'])).get('data') != result):
        raise ValueError('Selection plan must have its complete published result before scoring admission')
    if result['input_contract']['run_id'] != run_id or result['input_contract']['run_root'] != str(store.root):
        raise ValueError('Selection plan targets a different run')
    canonical = [_canonical(value) for value in candidates]
    if (plan_id != result['plan_id'] or len(canonical) != len(set(canonical))
            or set(canonical) != set(result['selected_candidates'])):
        raise ValueError('Scoring must use the exact complete method-selected candidate set')
    if (proposal_role is not None and proposal_role != result['proposal_role']) or (
            anchor_parent is not None and _canonical(anchor_parent) != result['anchor_parent']):
        raise ValueError('Scoring changes the method plan role or anchor')
    progress = adapter.read_progress(store, run_id)
    if progress['trajectory_sha256'] != result['trajectory_sha256']:
        raise ValueError('Selection plan is stale after new scoring observations')
    if store.run(run_id)['config'].get('pablo_harness'):
        from . import harness
        harness.validate_binding(store, run_id, result.get('harness_binding'),
                                 result['selected_candidates'], progress=progress)
    return {'selection_effect_id': effect['id'], 'plan_id': result['plan_id'],
            'selection_receipt_ref': effect['result_ref'], 'selected_candidates': result['selected_candidates'],
            'proposal_role': result['proposal_role'], 'anchor_parent': result['anchor_parent'],
            'method': result['method'], 'trajectory_sha256': result['trajectory_sha256']}


def recommendation_completion(contract, effect, publication):
    data = publication.get('data', {})
    if (contract.get('kind') != 'exact_inputs' or contract.get('version') != 1
            or effect.get('status') != 'succeeded' or effect.get('metadata', {}).get('adapter_id') != ADAPTER_ID
            or data.get('effect_id') != effect['id'] or data.get('packet_sha256') != effect.get('packet_hash')
            or data.get('input_contract') != contract.get('inputs')):
        return {'status': 'unsupported', 'detail': 'Actual selection publication does not match the frozen method contract'}
    try:
        _validated_result(effect, {'status': 'succeeded', 'result': data})
    except (KeyError, TypeError, ValueError):
        return {'status': 'unsupported', 'detail': 'Selection result or selected set has an invalid immutable plan binding'}
    selected = data.get('selected_candidates', [])
    complete = (len(selected) == contract['inputs']['select_count'] and len(set(selected)) == len(selected)
                and set(selected).issubset(contract['inputs']['candidates']))
    return {'status': 'complete' if complete else 'partial', 'completed_count': len(selected),
            'expected_count': contract['inputs']['select_count'], 'scientific_status': 'unverified',
            'causal_benefit': 'not_established', 'detail': 'Method execution and selected set only; no new score or algorithm benefit established'}


def decision_dependencies(store, run_id):
    from .research import scoring_decision_dependencies, literature_decision_dependencies
    return scoring_decision_dependencies(store, run_id) + literature_decision_dependencies(store, run_id) + [
        {'kind': 'effect', 'where': {'metadata.adapter_id': ADAPTER_ID},
         'fields': ['status', 'accounting_unknown', 'packet_ref', 'result_ref']},
        {'kind': 'fact', 'where': {'kind': 'source_statement'}, 'fields': ['content_ref', 'provenance']}]


def progress(effect, publication):
    data = publication['data']
    inputs = data['input_contract']
    sources = [{'topic': row['topic'], 'response_sha256': row['response_sha256'], 'records': row['records']}
               for row in data['literature_sources']]
    query = {key: inputs[key] for key in ('method', 'proposal_role', 'anchor_parent', 'candidates',
                                         'select_count', 'parameters', 'fingerprint', 'implementation')}
    query['literature_sources'] = [json.loads(value) for value in sorted({encode(row).decode() for row in sources})]
    return {'query': query, 'state': {'trajectory_sha256': data['trajectory_sha256']},
            'result': {'selected_candidates': data['selected_candidates'], 'selection_rows': data['selection_rows']},
            'new_scores': 0, 'information_units': len(data['selected_candidates'])}


def make_adapter():
    return Adapter(ADAPTER_ID, VERSION, EFFECT_KIND,
        {'domain': 'molecular_optimization', 'scope': 'Execute bounded public method primitives on observed data; no scoring',
         'input_contract': {'fields': sorted(FIELDS), 'proposal_role': ['local', 'challenge'],
             'candidates': '1..256 valid SMILES; canonical aliases collapse deterministically',
             'select_count': '1..20 eligible candidates', 'anchor_parent': 'previously observed molecule',
             'method': METHODS, 'parameters': {'neighbors': '1..32', 'exploration_weight': '0..2', 'diversity_weight': '0..2'},
             'literature_evidence_ids': '1..4 actual applicable literature publications containing abstracts'},
         'output_contract': 'Reproducible selection plan, predictions and source hashes; not new observations',
         'recommendation_contract_kinds': ['exact_inputs'], 'limits': {'pool': MAX_POOL, 'training': MAX_TRAINING},
         'method_fidelity': 'Reviewed method primitives; no claim to reproduce an arbitrary cited paper'},
        prepare, reservations, execute, publication,
        source_paths=(str(Path(adapter.__file__).resolve()), str(Path(literature.__file__).resolve()),
                      str(Path(__file__).with_name('harness.py').resolve())),
        admit_work=admit_work, recommendation_normalize=normalize_recommendation,
        recommendation_completion=recommendation_completion, decision_dependencies=decision_dependencies,
        progress=progress, required_evidence=required_evidence)
