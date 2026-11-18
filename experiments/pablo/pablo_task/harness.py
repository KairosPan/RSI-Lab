"""Receipt-derived local/challenge molecular search policy, owned by PABLO.

This module does not call a model or evaluator and has no mutable scheduler.
The kernel serializes Work; every scoring packet freezes the current policy
binding, and only its actual successful score rows earn role credit.

Two versions share it. In `pablo_local_challenge_v1` the batch index decides
each batch's role. In `pablo_lab_led_v1` the Lab chooses the role and states a
decision; only the challenge backstop can require a challenge batch.
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
LED_VERSION = 'pablo_lab_led_v1'
LED_DEFAULT_CONFIG = {
    'version': LED_VERSION,
    # After this many consecutive batches of this run that neither beat the best score nor were scored
    # challenge batches, the next batch must be a challenge batch; None switches the backstop off.
    'challenge_backstop_batches': 4,
    'stale_batch_threshold': 4,
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
ROLES = tuple(ROLE_INSTRUCTIONS)
DECISION_FIELDS = ('challenge_dispatched', 'challenge_adopted', 'reason')
BACKSTOP_REFUSAL = 'The backstop requires a challenge batch proposed by the Challenge sub-agent'


def configuration(config):
    """None preserves old frozen runs; new runs must freeze every policy field of their version."""
    value = config.get('pablo_harness')
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) not in (set(DEFAULT_CONFIG), set(LED_DEFAULT_CONFIG)):
        raise ValueError('PABLO harness requires its complete frozen configuration')
    if value['version'] not in (VERSION, LED_VERSION):
        raise ValueError('Unsupported PABLO harness version')
    if set(value) != set(LED_DEFAULT_CONFIG if value['version'] == LED_VERSION else DEFAULT_CONFIG):
        raise ValueError('PABLO harness requires its complete frozen configuration')
    limits = [('stale_batch_threshold', 1, 100), ('stale_score_threshold', 1, 10000), ('max_candidates', 1, 20)]
    if value['version'] == VERSION:
        limits.insert(0, ('challenge_every_batches', 2, 20))
    elif value['challenge_backstop_batches'] is not None:  # None switches the backstop off.
        limits.insert(0, ('challenge_backstop_batches', 1, 100))
    for key, low, high in limits:
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


def validate_decision(decision, proposal_role):
    """Normalize the Lab's statement about one led batch; its shape is checked, not its truth."""
    if proposal_role not in ROLES:
        raise ValueError('A batch decision requires the local/challenge role of its proposal')
    if not isinstance(decision, dict) or set(decision) != set(DECISION_FIELDS):
        raise ValueError('A batch decision holds exactly challenge_dispatched, challenge_adopted and reason')
    if type(decision['challenge_dispatched']) is not bool or type(decision['challenge_adopted']) is not bool:
        raise ValueError('challenge_dispatched and challenge_adopted must be true or false')
    if decision['challenge_adopted'] and not (decision['challenge_dispatched'] and proposal_role == 'challenge'):
        raise ValueError('Adopting a Challenge proposal requires a dispatched Challenge and a challenge batch')
    reason = decision['reason'].strip() if isinstance(decision['reason'], str) else ''
    if not 1 <= len(reason) <= 300 or '\n' in reason or '\r' in reason:
        raise ValueError('A batch decision reason must be one line of 1 to 300 characters')
    return {'challenge_dispatched': decision['challenge_dispatched'],
            'challenge_adopted': decision['challenge_adopted'], 'reason': reason}


def check_dispatch(decision, child_threads):
    """challenge_dispatched must match the record of the Lab call that made the request.

    A sub-agent counts when its recorded role is challenge, whether the call
    started it or addressed it again; one whose role could not be read does not.
    """
    if not isinstance(decision, dict) or type(decision.get('challenge_dispatched')) is not bool:
        raise ValueError('The dispatch check requires a decision whose challenge_dispatched is true or false')
    if child_threads is not None and not isinstance(child_threads, list):
        raise ValueError("The call record's child_threads must be a list or absent")
    roles = [child.get('role') for child in child_threads or [] if isinstance(child, dict)]
    dispatched = 'challenge' in roles
    if decision['challenge_dispatched'] and not dispatched:
        raise ValueError('The decision says Challenge was dispatched, but the call record of the requesting Lab '
                         'call ' + ('has no sub-agent with role challenge' if roles else 'shows no sub-agent')
                         + ('; a sub-agent whose role could not be read does not count' if None in roles else ''))
    if dispatched and not decision['challenge_dispatched']:
        raise ValueError('The decision says Challenge was not dispatched, but the call record of the requesting '
                         'Lab call has a sub-agent with role challenge')


def _led_choice(state, proposal_role, decision):
    """The Lab's role and decision, held to the backstop of the state they are admitted under.

    A required challenge batch must be the Challenge sub-agent's proposal: a
    challenge batch whose decision says Challenge was dispatched and adopted.
    """
    decision = validate_decision(decision, proposal_role)
    if state['required_role'] == 'challenge' and not (proposal_role == 'challenge'
                                                      and decision['challenge_dispatched']
                                                      and decision['challenge_adopted']):
        raise ValueError(BACKSTOP_REFUSAL)
    return decision


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
                credits, completed_effects, config, unchallenged=0):
    plateau = stale_scores >= config['stale_score_threshold']
    stale = stale_batches >= config['stale_batch_threshold']
    if config['version'] == LED_VERSION:
        # The Lab chooses the role; the two signals are plain facts, and only the backstop can require one.
        backstop = config['challenge_backstop_batches']
        state = {'version': LED_VERSION,
                 'required_role': 'challenge' if backstop is not None and unchallenged >= backstop else None,
                 'unchallenged_stale_batches': unchallenged, 'challenge_backstop_batches': backstop,
                 'turn': turn, 'stale_batches': stale_batches, 'stale_scores': stale_scores,
                 'signals': {'score_plateau': plateau, 'stale_batches': stale}}
    else:
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
        state = {'version': VERSION, 'proposal_role': role, 'turn': turn,
                 'stale_batches': stale_batches, 'stale_scores': stale_scores,
                 'regime': regime, 'reason': reasons}
    remaining = target - completed
    nominal = 3
    if completed >= 50 and stale_batches >= 3:
        nominal = 5 if completed < 120 else 8
    state.update({'completed_evaluations': completed, 'remaining_evaluations': remaining,
                  'max_candidates': min(config['max_candidates'], remaining),
                  'requested_candidates': min(nominal, config['max_candidates'], remaining),
                  'trajectory_sha256': digest({'candidates': candidates, 'scores': scores}),
                  'policy_sha256': digest(config), 'completed_effect_ids': list(completed_effects),
                  'role_credits': copy.deepcopy(credits)})
    state['state_sha256'] = digest(state)
    return state


def project(initial, batches, target, config=None, *, verify_bindings=True):
    """Pure replay of settled score batches; lookup/model calls never advance it.

    Each batch contains effect_id, evaluations and harness_binding. This accepts
    only actual accepted finite rows, never proposed candidate counts or top20.
    A fixed-schedule run computes each batch's role from its turn; a Lab-led run
    takes it from the batch's binding and checks it against the backstop.
    """
    config = configuration({'pablo_harness': DEFAULT_CONFIG if config is None else config})
    led = config['version'] == LED_VERSION
    if type(target) is not int or target < 1:
        raise ValueError('Harness target must be a positive score count')
    candidates, scores = _validated_initial(initial)
    incumbent, stale_scores = -math.inf, 0
    for score in scores:
        if score > incumbent:
            incumbent, stale_scores = score, 0
        else:
            stale_scores += 1
    # The backstop counter starts at 0 whatever the initial observations say.
    completed = turn = stale_batches = unchallenged = 0
    effects, transitions, decisions = [], [], []
    totals = dict.fromkeys(('lab_chosen_challenge_batches', 'backstop_challenge_batches',
                            'dispatched_not_adopted_batches'), 0)
    credits = {role: {'batches': 0, 'accepted_scores': 0, 'improving_batches': 0,
                     'best_observed_score': None} for role in ROLE_INSTRUCTIONS}
    previous_regime = None
    for batch in batches:
        state = _next_state(candidates, scores, target, completed, turn, stale_batches,
                            stale_scores, credits, effects, config, unchallenged)
        binding, rows = batch.get('harness_binding'), batch.get('evaluations')
        effect_id = batch.get('effect_id')
        if not isinstance(effect_id, str) or not effect_id or effect_id in effects:
            raise ValueError('Harness score batches require distinct execution identities')
        if (not isinstance(rows, list) or any(not isinstance(r, dict) or not {'candidate', 'score'}.issubset(r)
                or type(r['score']) not in (int, float) or not math.isfinite(r['score']) for r in rows)):
            raise ValueError('Harness credit requires actual finite score rows')
        if led:
            role = binding.get('proposal_role') if isinstance(binding, dict) else None
            if role not in ROLES:
                raise ValueError('Lab-led scoring batch lacks the local/challenge role it was admitted with')
            decision = copy.deepcopy(binding.get('decision'))
        else:
            role = state['proposal_role']
        if verify_bindings:
            if (not isinstance(binding, dict) or binding.get('state_sha256') != state['state_sha256']
                    or binding.get('proposal_role') != role
                    or binding.get('turn') != turn or binding.get('policy_sha256') != state['policy_sha256']
                    or binding.get('trajectory_sha256') != state['trajectory_sha256']):
                raise ValueError('Scoring batch lacks its exact admitted harness state')
            if led:
                decision = _led_choice(state, role, binding.get('decision'))
            eligible, _, _ = _anchor_options(candidates, scores, role)
            if binding.get('anchor_parent') not in {r['candidate'] for r in eligible}:
                raise ValueError('Scoring batch anchor does not implement its local/challenge role')
            if rows:
                incumbent_candidate = candidates[max(range(len(scores)), key=lambda i: (scores[i], -i))]
                validate_candidates([r['candidate'] for r in rows], binding, incumbent_candidate, config)
        before = incumbent
        if not led and previous_regime != state['regime']:
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
        # A challenge batch disarms the backstop only when at least one of its candidates was scored.
        unchallenged = 0 if improved or (role == 'challenge' and rows) else unchallenged + 1
        credit = credits[role]
        credit['batches'] += 1
        credit['accepted_scores'] += len(rows)
        credit['improving_batches'] += int(improved)
        if rows:
            credit['best_observed_score'] = max([row['score'] for row in rows] +
                                               ([] if credit['best_observed_score'] is None else
                                                [credit['best_observed_score']]))
        if led:
            required = state['required_role'] == 'challenge'
            if role == 'challenge':
                totals['backstop_challenge_batches' if required else 'lab_chosen_challenge_batches'] += 1
            if (isinstance(decision, dict) and decision.get('challenge_dispatched') is True
                    and decision.get('challenge_adopted') is False):
                totals['dispatched_not_adopted_batches'] += 1
            decisions.append({'turn': turn, 'proposal_role': role, 'required': required, 'decision': decision,
                              'improved': improved, 'accepted_scores': len(rows)})
        effects.append(effect_id); turn += 1
    state = _next_state(candidates, scores, target, completed, turn, stale_batches,
                        stale_scores, credits, effects, config, unchallenged)
    authority = 'receipt_derived_host_policy_not_scientific_evidence'
    common = {'source_origin': SOURCE_ORIGIN, 'current_policy_configuration': config,
              'candidate_constraints': {'local_min_anchor_similarity': config['local_min_anchor_similarity'],
                  'fingerprint': 'Morgan radius 2, 2048 bits, chirality disabled',
                  'challenge_min_outside_incumbent_fraction': config['challenge_min_outside_incumbent_fraction']},
              'interpretation_limit': 'Role credit records actual scoring allocation, not causal scientific benefit.'}
    if not led:
        anchors, anchor_rule, best_family = _anchor_options(candidates, scores, state['proposal_role'])
        return {'schema_version': VERSION, 'authority': authority,
                'next_plan': state, 'instructions': ROLE_INSTRUCTIONS[state['proposal_role']],
                'anchor_rule': anchor_rule, 'incumbent_family': best_family,
                'eligible_anchors': anchors[:20], 'eligible_anchor_count': len(anchors),
                'omitted_anchor_count': max(0, len(anchors) - 20),
                'blocked_reason': ('score_budget_complete' if state['remaining_evaluations'] == 0 else
                                   'no_observed_distinct_challenge_anchor' if not anchors else None),
                'strategy_transitions': transitions, **common}
    # The Lab chooses, so it sees both roles; a missing challenge anchor blocks only a required challenge.
    options = {role: _anchor_options(candidates, scores, role) for role in ROLES}
    anchors = {role: found for role, (found, _, _) in options.items()}
    return {'schema_version': LED_VERSION, 'authority': authority,
            'next_plan': state, 'instructions': copy.deepcopy(ROLE_INSTRUCTIONS),
            'anchor_rule': {role: rule for role, (_, rule, _) in options.items()},
            'incumbent_family': options['local'][2],
            'eligible_anchors': {role: found[:20] for role, found in anchors.items()},
            'eligible_anchor_count': {role: len(found) for role, found in anchors.items()},
            'omitted_anchor_count': {role: max(0, len(found) - 20) for role, found in anchors.items()},
            'blocked_reason': ('score_budget_complete' if state['remaining_evaluations'] == 0 else
                               'no_observed_distinct_challenge_anchor'
                               if state['required_role'] == 'challenge' and not anchors['challenge'] else None),
            'decisions': decisions[-8:], 'totals': totals, **common}


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


def validate_proposal(store, run_id, proposal_role, anchor_parent, progress=None, decision=None):
    """Bind selection and later scoring to the role due at this prefix, or chosen by the Lab in a led run."""
    from . import adapter
    policy = configuration(store.run(run_id)['config'])
    if policy is None:
        raise ValueError('Local/challenge proposal requires its frozen enabled harness')
    led = policy['version'] == LED_VERSION
    if led and decision is None:
        raise ValueError("A Lab-led batch proposal requires the Lab's decision")
    if not led and decision is not None:
        raise ValueError('A fixed-schedule batch proposal takes no Lab decision')
    progress = progress or adapter.read_progress(store, run_id)
    current = context(store, run_id, progress)
    state = current['next_plan']
    if current['blocked_reason']:
        raise ValueError('Harness cannot submit another batch: ' + current['blocked_reason'])
    if led:
        decision = _led_choice(state, proposal_role, decision)
    elif proposal_role != state['proposal_role']:
        raise ValueError('Proposal role differs from the currently due local/challenge turn')
    canonical = _canonical(anchor_parent)
    anchors, _, _ = _anchor_options(progress['candidates'], progress['scores'], proposal_role)
    if canonical not in {r['candidate'] for r in anchors}:
        raise ValueError('Anchor is not an eligible observed parent for the current role')
    binding = {'version': policy['version'], 'run_id': run_id, 'run_root': str(store.root),
               'proposal_role': proposal_role, 'anchor_parent': canonical, 'turn': state['turn'],
               'state_sha256': state['state_sha256'], 'policy_sha256': state['policy_sha256'],
               'trajectory_sha256': state['trajectory_sha256']}
    if led:
        return {**binding, 'required_role': state['required_role'], 'max_candidates': state['max_candidates'],
                'decision': decision}
    return {**binding, 'regime': state['regime'], 'reason': state['reason'],
            'max_candidates': state['max_candidates']}


def validate_binding(store, run_id, binding, candidates=None, progress=None):
    """Revalidate an executable selection plan before scoring admission/dispatch.

    A led binding's own decision is checked again against the current state.
    """
    if not isinstance(binding, dict):
        raise ValueError('Scoring requires an explicit local/challenge harness binding')
    expected = validate_proposal(store, run_id, binding.get('proposal_role'),
                                 binding.get('anchor_parent'), progress, binding.get('decision'))
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
    return {'version': LED_VERSION if binding.get('version') == LED_VERSION else VERSION,
            'proposal_role': binding['proposal_role'],
            'anchor_parent': binding['anchor_parent'], 'turn': binding['turn'],
            'state_sha256': binding['state_sha256'], 'effect_id': effect['id'],
            'scored_candidates': [row['candidate'] for row in rows],
            'accepted_scores': len(rows), 'best_in_batch': max((r['score'] for r in rows), default=None),
            'scientific_benefit': 'not_established'}
