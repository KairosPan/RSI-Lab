"""Receipt-derived local/challenge molecular search policy, owned by PABLO.

This module does not call a model or evaluator and has no mutable scheduler.
The kernel serializes Work; every scoring packet freezes the current policy
binding, and only its actual successful score rows earn role credit.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import json
import math

from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.Scaffolds import MurckoScaffold

from rsi_lab.store import digest


VERSION = 'pablo_local_challenge_v1'
DEFAULT_CONFIG = {
    'version': VERSION,
    'challenge_every_batches': 4,
    'stale_batch_threshold': 4,
    # New explicit host policy, not a claim about the original implementation.
    'stale_score_threshold': 40,
    'max_candidates': 10,
    'local_min_anchor_similarity': .35,
    'challenge_min_outside_incumbent_fraction': 1.0,
}
SOURCE_ORIGIN = {
    'original_cadence': {
        'path': 'variants/search3/a/entrypoint.py',
        'sha256': '2bfba65a2d0cbed835a60938705239a2a149223bf2641449d3b130be027d55c0',
        'lines': [60, 75],
    },
    'adaptive_policy': {
        'path': 'variants/continuity11/qualification/science_policy.py',
        'sha256': 'e43de615f92e0b86e38914bb549f371303b407c2fa26538e422e464bc206374c',
        'lines': [95, 156, 220, 240],
    },
    'source_root': '/Users/pan/Documents/ChatGPT/RSI-PABLO/pablo-meta-experiment',
    'migration': 'Ported policy semantics, not imported execution loop. Source paths are provenance only.',
}
ROLE_INSTRUCTIONS = {
    'local': (
        'Refine the best observed structural series using a small interpretable edit supported by an observed '
        'contrast. Keep distinct plausible edit axes, consider negative analogues and interactions, and '
        'reserve an alternative explanation. State the hypothesis, observed contrast, and what outcome '
        'would falsify it. Identify a candidate whose evaluation can discriminate the explanations; do '
        'not predict its numerical score or guess the hidden objective.'),
    'challenge': (
        'Challenge the leading series using an eligible, structurally different, strong observed anchor. '
        'Contrast that anchor with unsuccessful observations and test an alternative edit direction or '
        'recombination supported by outcomes. Explain how it challenges the leading-series hypothesis, '
        'name competing explanations, and identify a discriminating candidate. Diversity alone is not '
        'success; do not predict numerical scores or guess the hidden objective.'),
}


def configuration(config):
    """None preserves old frozen runs; new runs must freeze every policy field."""
    value = config.get('pablo_harness')
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(DEFAULT_CONFIG):
        raise ValueError('PABLO harness requires its complete frozen configuration')
    if value['version'] != VERSION:
        raise ValueError('Unsupported PABLO harness version')
    for key, low, high in [('challenge_every_batches', 2, 20), ('stale_batch_threshold', 1, 100),
                           ('stale_score_threshold', 1, 10000), ('max_candidates', 1, 20)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('Invalid PABLO harness limit: ' + key)
    for key in ('local_min_anchor_similarity', 'challenge_min_outside_incumbent_fraction'):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or not 0 < value[key] <= 1:
            raise ValueError('Invalid PABLO harness candidate constraint: ' + key)
    return copy.deepcopy(value)


def _canonical(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError('Harness candidates and anchors require bounded SMILES strings')
    return _canonical_text(value)


@lru_cache(maxsize=32768)
def _canonical_text(value):
    molecule = Chem.MolFromSmiles(value)
    if molecule is None:
        raise ValueError('Harness candidates and anchors require valid SMILES')
    return Chem.MolToSmiles(molecule, isomericSmiles=True)


@lru_cache(maxsize=32768)
def _family(value):
    molecule = Chem.MolFromSmiles(value)
    scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=molecule, includeChirality=False)
    # Empty Murcko strings do not make all acyclic chemistry one series.
    return ('murcko:' + scaffold if scaffold else
            'acyclic_connectivity:' + Chem.MolToSmiles(molecule, isomericSmiles=False))


_MORGAN = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, includeChirality=False)


@lru_cache(maxsize=32768)
def _fingerprint(value):
    return _MORGAN.GetFingerprint(Chem.MolFromSmiles(value))


def validate_candidates(candidates, binding, incumbent_candidate, config):
    """Validate actual selected structures, not just the proposal's role label.

    These explicit host constraints operationalize the old textual roles. They
    do not certify a scientifically useful or causally effective search policy.
    """
    config = configuration({'pablo_harness': config})
    if not isinstance(candidates, list) or not candidates:
        raise ValueError('Role validation requires a nonempty selected candidate list')
    canonical = [_canonical(value) for value in candidates]
    role = binding.get('proposal_role') if isinstance(binding, dict) else None
    if role == 'local':
        anchor = _canonical(binding.get('anchor_parent'))
        similarities = [DataStructs.TanimotoSimilarity(_fingerprint(value), _fingerprint(anchor))
                        for value in canonical]
        if any(value < config['local_min_anchor_similarity'] for value in similarities):
            raise ValueError('Local selected candidates exceed the frozen anchor-similarity boundary')
        return {'proposal_role': role, 'minimum_anchor_similarity': min(similarities)}
    if role == 'challenge':
        incumbent_family = _family(_canonical(incumbent_candidate))
        different = sum(_family(value) != incumbent_family for value in canonical)
        if different / len(canonical) < config['challenge_min_outside_incumbent_fraction']:
            raise ValueError('Challenge selected candidates do not leave the incumbent structural series')
        return {'proposal_role': role, 'outside_incumbent_series': different,
                'selected_count': len(canonical)}
    raise ValueError('Selected candidates require the current local/challenge role')


def _validated_initial(initial):
    if (not isinstance(initial, dict) or set(initial) != {'candidates', 'scores'}
            or not isinstance(initial['candidates'], list) or not initial['candidates']
            or not isinstance(initial['scores'], list)
            or len(initial['scores']) != len(initial['candidates'])):
        raise ValueError('Harness requires nonempty paired initial observations')
    candidates = [_canonical(v) for v in initial['candidates']]
    if candidates != initial['candidates'] or len(candidates) != len(set(candidates)):
        raise ValueError('Harness initial identities must be distinct canonical SMILES')
    scores = initial['scores']
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in scores):
        raise ValueError('Harness scores must be finite')
    return candidates, list(scores)


def _anchor_options(candidates, scores, role):
    best_index = max(range(len(scores)), key=lambda i: (scores[i], -i))
    best_family = _family(candidates[best_index])
    cutoff = sorted(scores)[max(0, math.ceil(.75 * len(scores)) - 1)]
    rows = [{'candidate': c, 'score': s, 'family': _family(c), 'observation': i + 1}
            for i, (c, s) in enumerate(zip(candidates, scores))]
    if role == 'local':
        eligible = [r for r in rows if r['family'] == best_family and r['score'] >= cutoff]
        rule = 'best_series_at_or_above_observed_75th_percentile'
    else:
        alternatives = [r for r in rows if r['family'] != best_family]
        eligible = [r for r in alternatives if r['score'] >= cutoff]
        rule = 'different_observed_series_at_or_above_observed_75th_percentile'
        if not eligible and alternatives:
            # A deliberate, visible fallback; never silently reuse the incumbent series.
            strongest = max(r['score'] for r in alternatives)
            eligible = [r for r in alternatives if r['score'] == strongest]
            rule = 'strongest_observed_different_series_below_percentile_fallback'
    return sorted(eligible, key=lambda r: (-r['score'], r['observation'])), rule, best_family


def _next_state(candidates, scores, target, completed, turn, stale_batches, stale_scores,
                credits, completed_effects, config):
    plateau = stale_scores >= config['stale_score_threshold']
    stale = stale_batches >= config['stale_batch_threshold']
    scheduled = turn % config['challenge_every_batches'] == config['challenge_every_batches'] - 1
    adaptive = (plateau or stale) and turn % 2 == 1
    role = 'challenge' if scheduled or adaptive else 'local'
    regime = 'alternating_local_challenge' if plateau or stale else 'scheduled_local_challenge'
    reasons = []
    if scheduled:
        reasons.append('scheduled_challenge')
    if adaptive:
        reasons.append('score_plateau_challenge' if plateau else 'stale_batches_challenge')
    if not reasons:
        reasons.append('local_refinement_in_adaptive_regime' if plateau or stale else 'local_refinement')
    remaining = target - completed
    nominal = 3
    if completed >= 50 and stale_batches >= 3:
        nominal = 5 if completed < 120 else 8
    state = {'version': VERSION, 'proposal_role': role, 'turn': turn,
             'stale_batches': stale_batches, 'stale_scores': stale_scores,
             'regime': regime, 'reason': reasons, 'completed_evaluations': completed,
             'remaining_evaluations': remaining,
             'max_candidates': min(config['max_candidates'], remaining),
             'requested_candidates': min(nominal, config['max_candidates'], remaining),
             'trajectory_sha256': digest({'candidates': candidates, 'scores': scores}),
             'policy_sha256': digest(config), 'completed_effect_ids': list(completed_effects),
             'role_credits': copy.deepcopy(credits)}
    state['state_sha256'] = digest(state)
    return state


def project(initial, batches, target, config=None, *, verify_bindings=True):
    """Pure replay of settled score batches; lookup/model calls never advance it.

    Each batch contains effect_id, evaluations and harness_binding. This accepts
    only actual accepted finite rows, never proposed candidate counts or top20.
    """
    config = configuration({'pablo_harness': DEFAULT_CONFIG if config is None else config})
    if type(target) is not int or target < 1:
        raise ValueError('Harness target must be a positive score count')
    candidates, scores = _validated_initial(initial)
    incumbent, stale_scores = -math.inf, 0
    for score in scores:
        if score > incumbent:
            incumbent, stale_scores = score, 0
        else:
            stale_scores += 1
    completed = turn = stale_batches = 0
    effects, transitions = [], []
    credits = {role: {'batches': 0, 'accepted_scores': 0, 'improving_batches': 0,
                     'best_observed_score': None} for role in ROLE_INSTRUCTIONS}
    previous_regime = None
    for batch in batches:
        state = _next_state(candidates, scores, target, completed, turn, stale_batches,
                            stale_scores, credits, effects, config)
        binding, rows = batch.get('harness_binding'), batch.get('evaluations')
        effect_id = batch.get('effect_id')
        if not isinstance(effect_id, str) or not effect_id or effect_id in effects:
            raise ValueError('Harness score batches require distinct execution identities')
        if (not isinstance(rows, list) or any(not isinstance(r, dict) or not {'candidate', 'score'}.issubset(r)
                or type(r['score']) not in (int, float) or not math.isfinite(r['score']) for r in rows)):
            raise ValueError('Harness credit requires actual finite score rows')
        if verify_bindings:
            if (not isinstance(binding, dict) or binding.get('state_sha256') != state['state_sha256']
                    or binding.get('proposal_role') != state['proposal_role']
                    or binding.get('turn') != turn or binding.get('policy_sha256') != state['policy_sha256']
                    or binding.get('trajectory_sha256') != state['trajectory_sha256']):
                raise ValueError('Scoring batch lacks its exact admitted harness state')
            eligible, _, _ = _anchor_options(candidates, scores, state['proposal_role'])
            if binding.get('anchor_parent') not in {r['candidate'] for r in eligible}:
                raise ValueError('Scoring batch anchor does not implement its local/challenge role')
            if rows:
                incumbent_candidate = candidates[max(range(len(scores)), key=lambda i: (scores[i], -i))]
                validate_candidates([r['candidate'] for r in rows], binding, incumbent_candidate, config)
        role = state['proposal_role']
        before = incumbent
        if previous_regime != state['regime']:
            transitions.append({'at_turn': turn, 'at_evaluation': completed,
                                'from': previous_regime, 'to': state['regime'],
                                'stale_scores': stale_scores, 'stale_batches': stale_batches,
                                'effect_id': effect_id, 'state_sha256': state['state_sha256']})
            previous_regime = state['regime']
        for row in rows:
            candidate = _canonical(row['candidate'])
            if candidate != row['candidate'] or candidate in candidates:
                raise ValueError('Harness credit cannot contain invalid or duplicate scored identities')
            if completed >= target:
                raise ValueError('Harness score credits exceed the frozen evaluation budget')
            candidates.append(candidate); scores.append(row['score']); completed += 1
            if row['score'] > incumbent:
                incumbent, stale_scores = row['score'], 0
            else:
                stale_scores += 1
        if len(rows) > state['max_candidates']:
            raise ValueError('Harness batch exceeds its admitted per-batch capacity')
        improved = incumbent > before
        stale_batches = 0 if improved else stale_batches + 1
        credit = credits[role]
        credit['batches'] += 1
        credit['accepted_scores'] += len(rows)
        credit['improving_batches'] += int(improved)
        if rows:
            credit['best_observed_score'] = max([row['score'] for row in rows] +
                                               ([] if credit['best_observed_score'] is None else
                                                [credit['best_observed_score']]))
        effects.append(effect_id); turn += 1
    state = _next_state(candidates, scores, target, completed, turn, stale_batches,
                        stale_scores, credits, effects, config)
    anchors, anchor_rule, best_family = _anchor_options(candidates, scores, state['proposal_role'])
    return {'schema_version': VERSION, 'authority': 'receipt_derived_host_policy_not_scientific_evidence',
            'next_plan': state, 'instructions': ROLE_INSTRUCTIONS[state['proposal_role']],
            'anchor_rule': anchor_rule, 'incumbent_family': best_family,
            'eligible_anchors': anchors[:20], 'eligible_anchor_count': len(anchors),
            'omitted_anchor_count': max(0, len(anchors) - 20),
            'blocked_reason': ('score_budget_complete' if state['remaining_evaluations'] == 0 else
                               'no_observed_distinct_challenge_anchor' if not anchors else None),
            'strategy_transitions': transitions, 'source_origin': SOURCE_ORIGIN,
            'current_policy_configuration': config,
            'candidate_constraints': {'local_min_anchor_similarity': config['local_min_anchor_similarity'],
                'fingerprint': 'Morgan radius 2, 2048 bits, chirality disabled',
                'challenge_min_outside_incumbent_fraction': config['challenge_min_outside_incumbent_fraction']},
            'interpretation_limit': 'Role credit records actual scoring allocation, not causal scientific benefit.'}


def context(store, run_id, progress=None):
    """Host adapter context; reconstruct only settled scoring publications."""
    from . import adapter
    config = store.run(run_id)['config']
    policy = configuration(config)
    if policy is None:
        return None
    if progress is None:
        progress = adapter.read_progress(store, run_id)
    initial = json.loads(store.read_blob(config['pablo_initial_ref']))
    batches = []
    for effect in adapter._effects(store, run_id):
        if effect['status'] != 'succeeded':
            continue
        if effect.get('accounting_unknown'):
            raise ValueError('Harness cannot advance across unresolved scoring accounting')
        packet = json.loads(store.read_blob(effect['packet_ref']))
        receipt = json.loads(store.read_blob(effect['result_ref']))
        bound = packet.get('harness_binding')
        if (not isinstance(bound, dict) or bound.get('run_id') != run_id
                or bound.get('run_root') != str(store.root)):
            raise ValueError('Harness scoring binding belongs to a different frozen run')
        batches.append({'effect_id': effect['id'], 'evaluations': receipt['result']['evaluations'],
                        'harness_binding': bound})
    result = project(initial, batches, config['pablo_max_new_evaluations'], policy)
    if (result['next_plan']['trajectory_sha256'] != progress['trajectory_sha256']
            or result['next_plan']['completed_evaluations'] != progress['new_evaluations']):
        raise ValueError('Harness trajectory differs from authenticated scoring progress')
    result['pending_scoring_effect_ids'] = [e['id'] for e in adapter._effects(store, run_id)
                                            if e['status'] in {'prepared', 'dispatching', 'unknown'}]
    return result


def validate_proposal(store, run_id, proposal_role, anchor_parent, progress=None):
    """Bind selection and later scoring to the role currently due at this prefix."""
    from . import adapter
    policy = configuration(store.run(run_id)['config'])
    if policy is None:
        raise ValueError('Local/challenge proposal requires its frozen enabled harness')
    progress = progress or adapter.read_progress(store, run_id)
    current = context(store, run_id, progress)
    state = current['next_plan']
    if current['blocked_reason']:
        raise ValueError('Harness cannot submit another batch: ' + current['blocked_reason'])
    if proposal_role != state['proposal_role']:
        raise ValueError('Proposal role differs from the currently due local/challenge turn')
    canonical = _canonical(anchor_parent)
    anchors, _, _ = _anchor_options(progress['candidates'], progress['scores'], proposal_role)
    if canonical not in {r['candidate'] for r in anchors}:
        raise ValueError('Anchor is not an eligible observed parent for the current role')
    return {'version': VERSION, 'run_id': run_id, 'run_root': str(store.root),
            'proposal_role': proposal_role, 'anchor_parent': canonical, 'turn': state['turn'],
            'state_sha256': state['state_sha256'], 'policy_sha256': state['policy_sha256'],
            'trajectory_sha256': state['trajectory_sha256'], 'regime': state['regime'],
            'reason': state['reason'], 'max_candidates': state['max_candidates']}


def validate_binding(store, run_id, binding, candidates=None, progress=None):
    """Revalidate an executable selection plan before scoring admission/dispatch."""
    if not isinstance(binding, dict):
        raise ValueError('Scoring requires an explicit local/challenge harness binding')
    expected = validate_proposal(store, run_id, binding.get('proposal_role'),
                                 binding.get('anchor_parent'), progress)
    if binding != expected:
        raise ValueError('Local/challenge harness binding is stale or altered')
    if candidates is not None:
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= expected['max_candidates']:
            raise ValueError('Scoring candidates exceed the harness batch/remaining capacity')
        canonical = [_canonical(value) for value in candidates]
        if len(canonical) != len(set(canonical)):
            raise ValueError('Harness selection has duplicate canonical identities')
        from . import adapter
        progress = progress or adapter.read_progress(store, run_id)
        index = max(range(len(progress['scores'])), key=lambda i: (progress['scores'][i], -i))
        validate_candidates(candidates, expected, progress['candidates'][index],
                            configuration(store.run(run_id)['config']))
    return expected


def publication_credit(effect, publication, binding):
    """Return per-role actual coverage; never use requested pool sizes as credit."""
    data = publication.get('data', {})
    rows = data.get('evaluations')
    if (effect.get('status') != 'succeeded' or effect.get('accounting_unknown')
            or effect.get('metadata', {}).get('adapter_id') != 'pablo.evaluate'
            or data.get('effect_id') != effect.get('id')
            or data.get('packet_sha256') != effect.get('packet_hash')
            or not isinstance(binding, dict) or binding.get('proposal_role') not in ROLE_INSTRUCTIONS
            or not isinstance(rows, list)
            or any(not isinstance(row, dict) or type(row.get('score')) not in (int, float)
                   or not math.isfinite(row['score']) for row in rows)
            or len({row.get('candidate') for row in rows}) != len(rows)):
        raise ValueError('Harness credit requires bound successful unique finite scoring rows')
    return {'version': VERSION, 'proposal_role': binding['proposal_role'],
            'anchor_parent': binding['anchor_parent'], 'turn': binding['turn'],
            'state_sha256': binding['state_sha256'], 'effect_id': effect['id'],
            'scored_candidates': [row['candidate'] for row in rows],
            'accepted_scores': len(rows), 'best_in_batch': max((r['score'] for r in rows), default=None),
            'scientific_benefit': 'not_established'}
