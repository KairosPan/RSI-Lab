"""Trusted PABLO projections for structured scientific memory; no hidden objective text."""
import copy
from dataclasses import replace
import json
import math
from . import adapter


def research_policy():
    """Task policy consumed by the generic recommendation/schema machinery."""
    return {
        'recommendation_kinds': ['candidate_selection', 'search_strategy', 'novelty_screening', 'evidence_gathering'],
        'recommendation_guidance': (
            'PABLO recommendations are bounded candidate-selection, molecular-search, exact novelty-screening '
            'or evidence-gathering advice, never code execution, scoring-function changes or permission changes. '
            'Use a registered capability and delivered evidence; do not infer the hidden objective identity. '
            'Literature advice concerns generic search methods. A batch score increase is an observed outcome, '
            'not evidence that Meta caused improvement or that a molecule has biological efficacy.'),
        'final_review_guidance': (
            'Only when final_review_only is true, the PABLO host has confirmed that this segment has completed '
            'its requested additional valid, novel, finite scoring quota. Historical observations do not count '
            'toward that quota. In that final review, interpret the molecular-search results and explicitly '
            'dispose of pending recommendations; no more scoring or other tool execution is authorized. '
            'When final_review_only is false, this policy text makes no completion claim. '
            'Scoring completion is not biological efficacy.')}


def project_measurement(effect, publication):
    """PABLO-only measurements; the core stores this dictionary without interpreting it.

    Lookup can report an existing score without scoring a molecule. Only an
    evaluate result's actual evaluation rows contribute to batch score metrics.
    Unknown adapters and missing result fields do not acquire invented metrics.
    """
    from .lookup import ADAPTER_ID as LOOKUP_ID
    aid = effect.get('metadata', {}).get('adapter_id')
    if aid not in {adapter.ADAPTER_ID, LOOKUP_ID}:
        return {}
    data = publication.get('data')
    if not isinstance(data, dict):
        return {}
    metrics = {}
    for name in ('filter_counts', 'new_evaluations', 'remaining_evaluations'):
        if name not in data:
            continue
        value = data[name]
        counts = value.values() if name == 'filter_counts' and isinstance(value, dict) else [value]
        if (name == 'filter_counts' and not isinstance(value, dict)) or any(type(v) is not int or v < 0 for v in counts):
            raise ValueError('PABLO measurement counts must be nonnegative integers')
        metrics[name] = copy.deepcopy(value)
    if aid == adapter.ADAPTER_ID and 'evaluations' in data:
        rows = data['evaluations']
        if not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get('score')) not in (int, float)
                                            or not math.isfinite(row['score']) for row in rows):
            raise ValueError('PABLO batch measurements require actual finite score rows')
        scores = [row['score'] for row in rows]
        metrics['scored_count'] = len(scores)
        if scores:
            metrics.update(batch_best=max(scores), batch_mean=sum(scores) / len(scores))
    if metrics:
        metrics.update(score_direction='higher',
            measurement_scope='current_evaluation_batch' if aid == adapter.ADAPTER_ID else 'exact_lookup_snapshot',
            interpretation_limit='PABLO observations only; no causal benefit or biological efficacy established')
    return metrics


def project_publication(store, run_id, effect, publication):
    if effect is None or effect.get('metadata', {}).get('adapter_id') != adapter.ADAPTER_ID:
        return {'identities': []}
    data = publication['data']
    facts = publication['facts']
    if not facts or not isinstance(data.get('evaluations'), list):
        return {'identities': []}
    config = store.run(run_id)['config']
    initial = json.loads(store.read_blob(config['pablo_initial_ref']))
    initial_count = len(initial['candidates'])
    prior_scores = list(initial['scores'])
    for previous in adapter._effects(store, run_id):
        if previous['created_seq'] >= effect['created_seq']:
            break
        if previous['status'] != 'succeeded' or previous.get('accounting_unknown'):
            raise ValueError('Scientific memory cannot interpret scores across an unsettled predecessor')
        result = json.loads(store.read_blob(previous['result_ref']))['result']
        prior_scores.extend(row['score'] for row in result['evaluations'])
    if len(prior_scores) - initial_count != data['evaluation_start']:
        raise ValueError('Memory projection does not match the authenticated evaluation prefix')
    best_before = max(prior_scores)
    rows = data['evaluations']
    best_after = max([best_before] + [r['score'] for r in rows])
    filters = data.get('filter_counts', {})
    observation = ('no_new_evaluations' if not rows else
                   'improved_observed_best' if best_after > best_before else 'no_observed_best_improvement')
    outcome = 'inconclusive' if not rows else 'positive' if best_after > best_before else 'negative'
    conditions = {'evaluator_fingerprint': config.get('pablo_oracle_fingerprint'),
                  'identity_rule': 'canonical_isomeric_SMILES', 'score_direction': 'higher'}
    return {'identities': [{'identity': r['candidate'], 'conditions': conditions,
                           'fact_id': facts[0]['id'], 'locator': 'evaluations/' + str(i)}
                          for i, r in enumerate(rows)],
            'experience': {'summary': f'This batch accepted {len(rows)} novel scores; observed best {best_before:.12g} -> {best_after:.12g}.',
                           'outcome': outcome,
                           'measured_outcome': observation,
                           'scope': {'run_id': run_id, 'effect_id': effect['id'], 'conditions': conditions,
                                     'candidates': [r['candidate'] for r in rows]},
                           'best_before': best_before, 'best_after': best_after,
                           'best_delta': best_after - best_before, 'filter_counts': filters,
                           'interpretation_limit': 'Observed batch outcome only; no claim that this method is generally ineffective or that Meta caused improvement.'}}


def registered_adapters():
    from .lookup import make_adapter as make_lookup
    from rsi_domains.literature import make_adapter as make_literature
    return [replace(adapter.make_adapter(), measure=project_measurement),
            replace(make_lookup(), measure=project_measurement), make_literature()]
