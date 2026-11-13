"""Trusted PABLO projections for structured scientific memory; no hidden objective text."""
import copy
from dataclasses import replace
import json
import math
from . import adapter


def _recommendation_binding(store, run_id, inputs, fields, root_key):
    """Validate a static input contract without preparing or reserving execution."""
    if not isinstance(inputs, dict) or set(inputs) != set(fields) | {'run_root', 'run_id'}:
        raise ValueError('Recommendation inputs must contain exactly the capability input fields')
    if (inputs['run_id'] != run_id or inputs['run_root'] != str(store.root)
            or store.run(run_id)['config'].get(root_key) != str(store.root)):
        raise ValueError('Recommendation inputs must target the current frozen run binding')
    return {'run_id': run_id, 'run_root': str(store.root)}


def _normalize_candidates(store, run_id, inputs, maximum):
    from . import oracle
    binding = _recommendation_binding(store, run_id, inputs, {'candidates'}, 'pablo_run_root')
    values = inputs['candidates']
    if (not isinstance(values, list) or not 1 <= len(values) <= maximum
            or any(not isinstance(value, str) or not value or len(value) > 4096 for value in values)):
        raise ValueError('Recommendation requires a bounded nonempty candidate list')
    canonical = [oracle.canonicalize(value) for value in values]
    if any(value is None for value in canonical):
        raise ValueError('Recommendation candidates must all have valid canonical SMILES identities')
    return {**binding, 'identity_rule': 'canonical_isomeric_SMILES',
            'candidates': sorted(set(canonical))}


def normalize_evaluation_recommendation(store, run_id, inputs):
    """Bind the complete molecular set; aliases/order do not change its meaning."""
    return _normalize_candidates(store, run_id, inputs, adapter.MAX_CANDIDATES)


def normalize_lookup_recommendation(store, run_id, inputs):
    return _normalize_candidates(store, run_id, inputs, 100)


def normalize_literature_recommendation(store, run_id, inputs):
    from .literature import adapter as literature
    binding = _recommendation_binding(store, run_id, inputs, {'topic', 'max_results'}, 'literature_run_root')
    if (not isinstance(inputs['topic'], str) or inputs['topic'] not in literature.TOPICS
            or type(inputs['max_results']) is not int
            or not 1 <= inputs['max_results'] <= literature.LIMITS['max_results']):
        raise ValueError('Recommendation requires a known literature topic and bounded result count')
    return {**binding, 'topic': inputs['topic'], 'max_results': inputs['max_results'],
            'provider': literature.PROVIDER, 'query': literature.TOPICS[inputs['topic']]}


def scoring_decision_dependencies(store, run_id):
    """Read sets for trajectory/queued identity context, scoped to this scorer.

    The runtime captures these selectors in the same SQLite snapshot as the
    packet. Scoring identity and trajectory are global within this serial run,
    including other branches; unrelated domains and model bookkeeping are not.
    Run configuration and initial observations are immutable hash-bound inputs.
    """
    store.run(run_id)
    return [
        {'kind': 'effect', 'where': {'metadata.adapter_id': adapter.ADAPTER_ID},
         'fields': ['status', 'accounting_unknown', 'result_ref', 'packet_ref', 'packet_hash']},
        {'kind': 'work', 'where': {'kind': 'diagnostic', 'inputs.capability_id': adapter.ADAPTER_ID,
                                  'status': {'$in': ['ready', 'leased']}},
         'fields': ['status', 'inputs', 'effect_id']},
        {'kind': 'registration', 'where': {'kind': 'work_identity_claim', 'adapter_id': adapter.ADAPTER_ID,
                                          'status': {'$in': ['held', 'covered']}},
         'fields': ['identity', 'owner_work_id', 'status', 'generation', 'effect_id', 'result_ref', 'publication_ref']},
    ]


def literature_decision_dependencies(store, run_id):
    from .literature import adapter as literature
    store.run(run_id)
    return [{'kind': 'effect', 'where': {'metadata.adapter_id': literature.ADAPTER_ID},
             'fields': ['status', 'accounting_unknown', 'held_reservations', 'result_ref', 'packet_ref', 'packet_hash']}]


def _completion_data(contract, effect, publication, capability_id):
    if contract.get('version') != 1 or contract.get('kind') != 'exact_inputs':
        return None
    expected, data = contract.get('inputs', {}), publication.get('data', {})
    if (not isinstance(expected, dict) or not isinstance(data, dict)
            or effect.get('status') != 'succeeded' or effect.get('run_id') != expected.get('run_id')
            or effect.get('metadata', {}).get('adapter_id') != capability_id
            or data.get('effect_id') != effect.get('id')
            or not effect.get('packet_hash') or data.get('packet_sha256') != effect['packet_hash']):
        return None
    return data


def _candidate_completion(expected, completed, detail):
    missing = sorted(set(expected).difference(completed))
    return {'status': 'partial' if missing else 'complete', 'expected_count': len(expected),
            'completed_count': len(completed), 'missing': missing, 'detail': detail,
            'scientific_status': 'unverified', 'causal_benefit': 'not_established'}


def evaluation_recommendation_completion(contract, effect, publication):
    data = _completion_data(contract, effect, publication, adapter.ADAPTER_ID)
    if data is None:
        return {'status': 'unsupported', 'detail': 'No supported contract and bound successful scoring publication'}
    expected, rows = contract['inputs']['candidates'], data.get('evaluations')
    if (not isinstance(rows, list) or any(not isinstance(row, dict)
            or row.get('candidate') not in expected or type(row.get('score')) not in (int, float)
            or not math.isfinite(row['score']) for row in rows)):
        return {'status': 'unsupported', 'detail': 'Publication lacks valid finite evaluation rows for this contract'}
    completed = {row['candidate'] for row in rows}
    if len(completed) != len(rows):
        return {'status': 'unsupported', 'detail': 'Repeated evaluation rows cannot certify molecular coverage'}
    return _candidate_completion(expected, completed,
        'Only actual new finite scoring rows count; filtered or budget-clipped candidates remain incomplete. '
        'Complete coverage is execution of the requested experiment, not scientific success or causal improvement.')


def lookup_recommendation_completion(contract, effect, publication):
    from .lookup import ADAPTER_ID as LOOKUP_ID
    data = _completion_data(contract, effect, publication, LOOKUP_ID)
    if data is None:
        return {'status': 'unsupported', 'detail': 'No supported contract and bound successful lookup publication'}
    expected, rows = contract['inputs']['candidates'], data.get('rows')
    statuses = {'already_evaluated', 'pending_or_unknown', 'not_seen_at_snapshot', 'duplicate_in_query'}
    if (not isinstance(rows, list) or any(not isinstance(row, dict)
            or row.get('canonical') not in expected or row.get('status') not in statuses for row in rows)):
        return {'status': 'unsupported', 'detail': 'Publication lacks valid lookup answers for this contract'}
    return _candidate_completion(expected, {row['canonical'] for row in rows},
        'Coverage means every requested identity received a lookup answer at this snapshot. '
        'It adds no scores and does not establish novelty at later execution or scientific success.')


def literature_recommendation_completion(contract, effect, publication):
    from .literature import adapter as literature
    data = _completion_data(contract, effect, publication, literature.ADAPTER_ID)
    if data is None:
        return {'status': 'unsupported', 'detail': 'No supported contract and bound successful literature publication'}
    expected = contract['inputs']
    if (any(data.get(key) != expected[key] for key in ('provider', 'topic', 'query'))
            or data.get('requested_max_results') != expected['max_results']
            or data.get('response_complete') is not True or data.get('http_status') != 200
            or type(data.get('provider_total_hits')) is not int or data['provider_total_hits'] < 0
            or not isinstance(data.get('records'), list)
            or data.get('returned_records') != len(data['records'])):
        return {'status': 'unsupported', 'detail': 'Publication does not verify the requested literature query and result bound'}
    target = min(expected['max_results'], data['provider_total_hits'])
    completed = len(data['records'])
    if completed > target:
        return {'status': 'unsupported', 'detail': 'Published literature records exceed the requested result bound'}
    return {'status': 'complete' if completed == target else 'partial',
            'expected_count': target, 'completed_count': completed,
            'requested_max_results': expected['max_results'], 'cache_hit': data.get('cache_hit') is True,
            'detail': 'Coverage concerns only the bounded metadata/available-abstract search. A shorter cached '
                      'response is partial when more results were requested and provider hits remain. Empty '
                      'searches can complete the request; retrieved papers do not establish scientific success.',
            'scientific_status': 'unverified', 'causal_benefit': 'not_established'}


def research_policy():
    """Task policy consumed by the generic recommendation/schema machinery."""
    return {
        'recommendation_kinds': ['candidate_selection', 'search_strategy', 'novelty_screening', 'evidence_gathering'],
        'recommendation_guidance': (
            'PABLO recommendations are bounded candidate-selection, molecular-search, exact novelty-screening '
            'or evidence-gathering advice, never code execution, scoring-function changes or permission changes. '
            'Use a registered capability and delivered evidence; do not infer the hidden objective identity. '
            'Every recommendation needs execution_contract_json with version 1, kind exact_inputs and an inputs '
            'object containing the complete capability parameters, including current run_root and run_id. '
            'For scoring and lookup specify the entire valid candidate set; canonical isomeric SMILES aliases '
            'and order are equivalent, but replacing, adding or omitting identities changes the contract. '
            'For literature specify the exact public topic and max_results. Free-text advice without this '
            'machine contract cannot be certified as implemented. Matching request inputs establishes only '
            'request compliance; actual publication coverage determines whether all requested work completed. '
            'Check constraints.adapter_context for authoritative current queued scoring identities. '
            'Do not recommend or adopt an operation that is already queued or completed; refer to the existing '
            'work ID in the rationale and decline or defer redundant advice. Operational work IDs are not '
            'evidence IDs: evidence_ids must contain only delivered original fact IDs. A lookup of unseen identities is '
            'only a snapshot; pending Work can reserve the intention before an execution Effect exists. '
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
    from pablo_task.literature import make_adapter as make_literature
    score, lookup, literature = adapter.make_adapter(), make_lookup(), make_literature()
    score = replace(score, manifest={**score.manifest, 'recommendation_contract_kinds': ['exact_inputs']})
    lookup = replace(lookup, manifest={**lookup.manifest, 'recommendation_contract_kinds': ['exact_inputs']})
    literature = replace(literature, manifest={**literature.manifest, 'recommendation_contract_kinds': ['exact_inputs']})
    return [replace(score, measure=project_measurement, progress=project_progress,
                    recommendation_normalize=normalize_evaluation_recommendation,
                    recommendation_completion=evaluation_recommendation_completion,
                    decision_dependencies=scoring_decision_dependencies),
            replace(lookup, measure=project_measurement, progress=project_progress,
                    recommendation_normalize=normalize_lookup_recommendation,
                    recommendation_completion=lookup_recommendation_completion,
                    decision_dependencies=scoring_decision_dependencies),
            replace(literature, progress=project_progress,
                    recommendation_normalize=normalize_literature_recommendation,
                    recommendation_completion=literature_recommendation_completion,
                    decision_dependencies=literature_decision_dependencies)]


def project_progress(effect, publication):
    """Stable scientific/operational content, excluding receipt and effect identities."""
    from .lookup import ADAPTER_ID as LOOKUP_ID
    from .literature.adapter import ADAPTER_ID as LITERATURE_ID
    aid, data = effect['metadata']['adapter_id'], publication['data']
    if aid == adapter.ADAPTER_ID:
        rows, filters = data['evaluations'], data['filters']
        return {'query': sorted({r.get('canonical') or r['candidate'] for r in filters}),
            'state': {'initial_count': data['initial_count'], 'evaluation_start': data['evaluation_start']},
            'result': {'evaluations': [{k: r[k] for k in ('candidate', 'score', 'evaluation')} for r in rows],
                'filters': [{k: r.get(k) for k in ('canonical', 'status')} for r in filters]},
            'new_scores': len(rows), 'information_units': 0}
    if aid == LOOKUP_ID:
        rows = data['rows']
        # Work/effect identities may change while a candidate remains pending.
        answers = [{k: r.get(k) for k in ('canonical', 'status', 'score', 'observation_ordinal')}
                   for r in rows if r['status'] != 'duplicate_in_query']
        answers = [json.loads(raw) for raw in sorted({json.dumps(row, sort_keys=True) for row in answers})]
        return {'query': sorted({r.get('canonical') or r['candidate'] for r in rows}),
            'state': {'trajectory_sha256': data['trajectory_sha256'],
                      'as_of_observations': data['as_of_observations']},
            'result': answers, 'new_scores': 0, 'information_units': len(answers)}
    if aid == LITERATURE_ID:
        return {'query': {'provider': data['provider'], 'topic': data['topic'], 'query': data['query']},
            'state': {'response_sha256': data['response_sha256']},
            # A different cache slice of the same raw response is not another
            # external observation. The raw hash authenticates all its content.
            'result': {'response_sha256': data['response_sha256'],
                       'provider_total_hits': data['provider_total_hits']},
            'new_scores': 0, 'information_units': 0 if data['cache_hit'] else 1}
    raise ValueError('No PABLO semantic progress rule for adapter ' + aid)
