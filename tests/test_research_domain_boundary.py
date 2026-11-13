"""Cross-domain execution tests: lower loss and shared untrusted sources.

Model decisions and external source text are fixtures. The controller, packets, adapter
computation, publication, memory, recommendation transitions and recovery are real.
No model provider or external service is called by these tests.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab import input_view
from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest


CAPABILITY = 'regression.evaluate'
POLICY = {'recommendation_kinds': ['parameter_trial', 'method_evidence'],
          'recommendation_guidance': 'For this regression task, lower mean squared loss is better; test a bounded slope change.',
          'final_review_guidance': 'Interpret measured prediction loss without requesting another regression trial.'}


def _prepare_loss(inputs):
    if (set(inputs) != {'slopes'} or not isinstance(inputs['slopes'], list)
            or not 1 <= len(inputs['slopes']) <= 5
            or any(type(v) not in (float, int) or abs(v) > 10 for v in inputs['slopes'])):
        raise ValueError('A bounded numeric slope batch is required')
    return {'slopes': inputs['slopes'], 'observations': [[x, 2 * x] for x in [-2, -1, 0, 1, 2]]}


def prepare_loss(inputs):
    return _prepare_loss(inputs)


def reserve_loss(packet):
    return {'tool_calls': 1}


def _execute_loss(effect, packet):
    data = json.loads(packet)
    rows = [{'slope': slope, 'loss': sum((slope * x - y) ** 2 for x, y in data['observations']) / len(data['observations'])}
            for slope in data['slopes']]
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
            'result': {'trials': rows, 'observations': data['observations'], 'criterion': 'minimize_mse',
                       # A legacy-looking field is deliberately non-authoritative: core must not interpret it.
                       'evaluations': [{'score': 9999}], 'filter_counts': {'debug_only': 1}}}


def execute_loss(effect, packet):
    return _execute_loss(effect, packet)


def _publish_loss(effect, receipt):
    return {'facts': [{'id': 'loss_' + effect['id'], 'kind': 'raw_fact', 'content': receipt['result'],
            'provenance': {'effect_id': effect['id'], 'adapter_id': CAPABILITY,
                           'source': 'Executed squared prediction error on fixed observations'}}],
            'conclusions': [], 'data': receipt['result']}


def publish_loss(effect, receipt):
    return _publish_loss(effect, receipt)


def _measure_loss(effect, publication):
    best = min(publication['data']['trials'], key=lambda row: row['loss'])
    return {'metric': 'mean_squared_error', 'direction': 'minimize', 'best_loss': best['loss'],
            'best_slope': best['slope'], 'trial_count': len(publication['data']['trials'])}


def measure_loss(effect, publication):
    return _measure_loss(effect, publication)


def alternative_measure(effect, publication):
    return {'different_rule': True}


def namespace_measure(effect, publication):
    return {'execution_status': 'not_controller_status', 'causal_benefit': 'not_controller_evidence'}


def invalid_list_measure(effect, publication):
    return []


def invalid_nan_measure(effect, publication):
    return {'loss': float('nan')}


def project_loss(store, run_id, effect, publication):
    if effect is None or effect.get('metadata', {}).get('adapter_id') != CAPABILITY:
        return {'identities': []}
    rows = publication['data']['trials']
    return {'identities': [{'identity': 'slope:' + str(row['slope']),
        'conditions': {'fixed_observations': publication['data']['observations'], 'metric': 'mse'},
        'fact_id': publication['facts'][0]['id'], 'locator': 'trials/' + str(index)} for index, row in enumerate(rows)],
        'experience': {'summary': 'The bounded slope trial measured squared error on the fixed data.',
            'scope': {'observations': publication['data']['observations'], 'slopes': [r['slope'] for r in rows]},
            'outcome': 'positive' if min(r['loss'] for r in rows) < 8 else 'negative',
            'best_loss': min(r['loss'] for r in rows), 'generalization': 'Only these fixed observations'}}


def loss_adapter(measure=measure_loss):
    return Adapter(CAPABILITY, '1.0', 'regression_measurement', {'scope': 'Bounded numerical regression experiment'},
                   prepare_loss, reserve_loss, execute_loss, publish_loss, measure=measure)


SOURCE_CAPABILITY = 'source.reference_fixture'


def prepare_source(inputs):
    if inputs != {'topic': 'least_squares'}:
        raise ValueError('Only the fixed source topic is available')
    return dict(inputs)


def execute_source(effect, packet):
    query = json.loads(packet)
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1}, 'result': {
        'source_group_id': 'reference:' + digest(query),
        'title': 'Least squares on fixed observations',
        'abstract': 'A numerical method was described; applicability to this task remains unverified.',
        'full_text_retrieved': False}}


def publish_source(effect, receipt):
    data = receipt['result']
    return {'facts': [{'id': 'source_' + effect['id'], 'kind': 'source_statement', 'content': data,
        'provenance': {'effect_id': effect['id'], 'source_group_id': data['source_group_id'],
                       'source': 'Fixed external-source fixture', 'independent_evidence': False,
                       'current_task_validation': False}}], 'conclusions': [], 'data': data}


def source_adapter():
    return Adapter(SOURCE_CAPABILITY, '1.0', 'source_reference_fixture',
                   {'scope': 'Fixed untrusted source text; no network or task validation'},
                   prepare_source, reserve_loss, execute_source, publish_source)


class Decision:
    def __init__(self, action, payload, *, adoption='decline', update=None):
        self.action, self.payload, self.adoption, self.update = action, payload, adoption, update
        self.inputs, self.envelopes = [], []

    def __call__(self, effect, raw):
        envelope = json.loads(raw)
        data = input_view.decode(envelope['prompt'].split('INPUT_DATA_JSON:\n', 1)[1], envelope['input_view_mode'])
        self.inputs.append(data); self.envelopes.append(envelope)
        task, mode = data['task'], envelope['mode']
        refs = [p['evidence_id'] for p in data['delivered_pages'] if p['complete_source']]
        response = {'command': {'mode': mode, 'action': self.action, 'issue_id': task.get('issue_id'),
            'expected_versions': task['expected_versions'], 'evidence_ids': refs, 'payload': self.payload},
            'working_memory_delta': '{}'}
        if 'scientific_memory' in task:
            response['scientific_memory_updates'] = '[]'
        loop = task['constraints'].get('research_loop')
        if loop is not None:
            if mode == 'lab':
                response['recommendation_responses'] = [{'recommendation_id': row['id'], 'expected_version': row['version'],
                    'decision': self.adoption, 'reason': 'A bounded regression trial can measure the proposed loss change.',
                    'evidence_ids': refs} for row in loop['recommendations']]
            else:
                response['improvement_reviews'] = [{'recommendation_id': row['id'], 'expected_version': row['version'],
                    'outcome': 'supported_observation', 'reason': 'The executed trial measured loss; no causal comparison was run.',
                    'evidence_ids': refs, 'scientific_status': 'unverified', 'causal_benefit': 'not_established'}
                    for row in loop['improvement_reviews']]
        if self.update:
            self.update(response, data)
        return {'status': 'succeeded', 'terminal_received': True, 'protocol_valid': True,
            'actual_costs': {'model_calls': 1, 'tokens': 1, 'wall_seconds': 0},
            'response': response, 'sent_packet_sha256': digest(raw),
            'sent_prompt_sha256': digest(envelope['prompt'].encode()), 'usage_complete': True,
            'usage': {'input_tokens': 1, 'output_tokens': 0}, 'synthetic': True}


class ResearchDomainBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.reservation = {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1}

    def runtime(self, name='regression', *, measure=measure_loss, policy=POLICY, external=False):
        store = Store(self.base / name)
        self.addCleanup(store.close)
        config = {'execution_mode': 'replay', 'goal': 'Fit y = 2x by minimizing measured mean squared error.',
            'research_features': {'meta_lab': True, 'scientific_memory': True},
            'max_input_bytes': 500000, 'scientific_memory_context_bytes': 64000}
        if policy is not None:
            config['research_policy'] = policy
        Controller(store).initialize(name, budgets={k: {'limit': v, 'hard': k.endswith('_calls')}
            for k, v in {'model_calls': 20, 'tokens': 1000, 'tool_calls': 10, 'wall_seconds': 200, 'cpu_seconds': 100}.items()},
            config=config)
        rt = Runtime(store, name, adapters=[loss_adapter(measure)] + ([source_adapter()] if external else []),
                     scientific_identity_provider=project_loss)
        rt.controller.add_fact(name, 'baseline', 'raw_fact', {'slope': 0, 'mean_squared_error': 8, 'direction': 'minimize'},
                               {'source': 'Fixed-data baseline calculation'}, 'baseline')
        rt.scientific_memory.observe_fact('baseline', projection={})
        return rt

    def recommend_and_execute(self, rt, category='parameter_trial'):
        advice = {'change_kind': category, 'instruction': 'Measure slopes 1, 2 and 3 on the fixed regression observations.',
            'capability_id': CAPABILITY, 'capability_version': '1.0', 'expected_outcome': 'A lower measured squared loss',
            'execution_contract_json': json.dumps({'version': 1, 'kind': 'exact_inputs', 'inputs': {'slopes': [1, 2, 3]}}),
            'measurement': 'Minimum mean squared loss among the executed slopes', 'falsifier': 'Every tested loss is at least eight'}
        trigger = rt.observe('baseline-review', 'input_changed', 0, ['baseline'])
        meta = Decision('recommend_change', advice)
        proposed = rt.run_meta(trigger['work_id'], meta, self.reservation)
        self.assertEqual(proposed['proposal']['status'], 'applied', proposed['proposal'])
        lab_work = next(w for w in rt.controller.ready(rt.run_id) if w['kind'] == 'lab')
        inputs = {'slopes': [1, 2, 3]}
        lab = Decision('request_diagnostic', {'capability_id': CAPABILITY, 'capability_version': '1.0',
                       'inputs_json': json.dumps(inputs)}, adoption='adopt')
        adopted = rt.run_lab(lab_work['id'], lab, self.reservation)
        self.assertEqual(adopted['proposal']['status'], 'applied', adopted['proposal'])
        recommendation = rt.improvements.rows()[0]
        wid = recommendation['tool_work_id']
        executed = rt.execute_adapter(CAPABILITY, inputs, trigger_id=wid, work_id=wid)
        self.assertEqual(executed['effect']['status'], 'succeeded')
        return rt.improvements.rows()[0], executed, meta, lab

    def test_minimization_review_and_next_lab_source_bound_memory(self):
        rt = self.runtime()
        rec, execution, meta, lab = self.recommend_and_execute(rt)
        measured = rec['measurement']
        self.assertEqual(measured['domain_metrics'], {'metric': 'mean_squared_error', 'direction': 'minimize',
            'best_loss': 0.0, 'best_slope': 2, 'trial_count': 3})
        self.assertEqual([r['loss'] for r in execution['result']['trials']], [2.0, 0.0, 2.0])
        for forbidden in ['batch_best', 'batch_mean', 'scored_count', 'filter_counts']:
            self.assertNotIn(forbidden, measured)
        self.assertIn(POLICY['recommendation_guidance'], meta.envelopes[0]['prompt'])
        self.assertIn(rec['recommendation_fact_id'], lab.inputs[0]['required_evidence_ids'])
        result_id = rec['result_evidence_ids'][0]
        def write_claim(response, data):
            question = next(r for r in data['task']['scientific_memory']['objects'] if r['kind'] == 'question')
            response['scientific_memory_updates'] = json.dumps([{'kind': 'claim', 'id': 'observed-zero-loss',
                'expected_revision': 0, 'question_ids': [question['id']],
                'content': {'statement': 'Slope two has zero measured squared loss on these five observations.',
                            'scope': {'observations': [-2, -1, 0, 1, 2], 'metric': 'mean_squared_error'}},
                'source_evidence_ids': [result_id], 'object_refs': []}])
        review_work = next(w for w in rt.controller.ready(rt.run_id) if w['inputs'].get('improvement_review_ids'))
        reviewer = Decision('no_action', {'reason': 'Measured lower loss is local evidence, not causal attribution.'}, update=write_claim)
        reviewed = rt.run_meta(review_work['id'], reviewer, self.reservation)
        self.assertEqual(reviewed['proposal']['status'], 'applied', reviewed['proposal'])
        self.assertEqual(rt.pending_improvements(), [])
        record = rt.scientific_memory.get('observed-zero-loss')
        self.assertEqual(record['support_check']['status'], 'pending')
        self.assertIsNone(record['validated_scope'])
        self.assertEqual(record['source_refs'][0]['content_sha256'], rt.store.get(rt.run_id, 'fact', result_id)['content_ref'])
        next_work = next(w for w in rt.controller.ready(rt.run_id) if w['kind'] == 'lab')
        next_lab = Decision('no_action', {'reason': 'Read the measured regression result and prior bounded claim.'})
        out = rt.run_lab(next_work['id'], next_lab, self.reservation)
        self.assertEqual(out['proposal']['status'], 'applied')
        data = next_lab.inputs[0]
        self.assertIn('observed-zero-loss', [r['id'] for r in data['task']['scientific_memory']['objects']])
        self.assertIn(result_id, data['required_evidence_ids'])
        self.assertTrue(next(p for p in data['delivered_pages'] if p['evidence_id'] == result_id)['complete_source'])
        counts = (len(rt.store.list(rt.run_id, 'effect')), len(rt.improvements.rows()))
        rt.recover(); rt.recover()
        self.assertEqual(counts, (len(rt.store.list(rt.run_id, 'effect')), len(rt.improvements.rows())))
        self.assertEqual(rt.store.get(rt.run_id, 'budget', 'tool_calls')['spent'], 1)
        rt.store.verify_history(rt.run_id)

    def test_without_measure_core_does_not_guess_score_semantics(self):
        rt = self.runtime(measure=None)
        rec, execution, _, _ = self.recommend_and_execute(rt)
        self.assertEqual(execution['result']['evaluations'][0]['score'], 9999)
        self.assertEqual(rec['measurement'].get('domain_metrics', {}), {})
        self.assertNotIn('batch_best', rec['measurement'])
        self.assertNotIn('filter_counts', rec['measurement'])

    def test_changed_measure_callback_blocks_unmeasured_recovery_without_reexecution(self):
        rt = self.runtime()
        with patch.object(rt.improvements, 'publication', side_effect=KeyboardInterrupt('Before measurement')):
            with self.assertRaises(KeyboardInterrupt):
                self.recommend_and_execute(rt)
        effect = rt.store.list(rt.run_id, 'effect', kind='regression_measurement')[0]
        self.assertEqual(effect['status'], 'succeeded')
        self.assertEqual(rt.improvements.rows()[0]['status'], 'adopted')
        first, changed = loss_adapter(), loss_adapter(alternative_measure)
        self.assertNotEqual(digest(first.descriptor()), digest(changed.descriptor()))
        self.assertIn('measure', first.descriptor()['callbacks'])
        altered = Runtime(rt.store, rt.run_id, adapters=[changed], scientific_identity_provider=project_loss)
        recovery = altered.recover_adapters()
        self.assertEqual(recovery[0]['status'], 'blocked_adapter')
        self.assertIn('exact original adapter', recovery[0]['reason'])
        self.assertEqual(len(rt.store.list(rt.run_id, 'effect', kind='regression_measurement')), 1)
        self.assertEqual(rt.store.get(rt.run_id, 'budget', 'tool_calls')['spent'], 1)
        self.assertNotIn('measurement', rt.improvements.rows()[0])

    def test_completed_measurement_replay_reuses_recorded_rule_and_credentials(self):
        rt = self.runtime()
        rec, execution, _, _ = self.recommend_and_execute(rt)
        original = rec['measurement']
        altered = Runtime(rt.store, rt.run_id, adapters=[loss_adapter(alternative_measure)], scientific_identity_provider=project_loss)
        altered.recover_adapters(); altered.recover_adapters()
        self.assertEqual(altered.improvements.rows()[0]['measurement'], original)
        self.assertNotIn('different_rule', altered.improvements.rows()[0]['measurement']['domain_metrics'])
        reviews = [w for w in rt.store.list(rt.run_id, 'work') if w['inputs'].get('improvement_review_ids')]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(len(rt.store.list(rt.run_id, 'effect', kind='regression_measurement')), 1)

    def test_domain_metric_names_cannot_overwrite_controller_credentials(self):
        rt = self.runtime(measure=namespace_measure)
        rec, execution, _, _ = self.recommend_and_execute(rt)
        self.assertEqual(rec['measurement']['execution_status'], 'succeeded')
        self.assertEqual(rec['measurement']['causal_benefit'], 'not_established')
        self.assertEqual(rec['measurement']['domain_metrics']['execution_status'], 'not_controller_status')
        self.assertEqual(rec['measurement']['executed_effect_id'], execution['effect']['id'])

    def test_invalid_domain_measurements_reject_publication_without_rerunning_tool(self):
        for name, measure in [('list', invalid_list_measure), ('nan', invalid_nan_measure)]:
            with self.subTest(name=name):
                rt = self.runtime(name=name, measure=measure)
                with self.assertRaises(ValueError):
                    self.recommend_and_execute(rt)
                effects = rt.store.list(rt.run_id, 'effect', kind='regression_measurement')
                self.assertEqual(len(effects), 1)
                self.assertEqual(effects[0]['status'], 'succeeded')
                self.assertEqual(rt.improvements.rows()[0]['status'], 'adopted')
                self.assertNotIn('measurement', rt.improvements.rows()[0])
                recovery = rt.recover_adapters()
                self.assertEqual(recovery[0]['status'], 'blocked_adapter')
                self.assertEqual(len(rt.store.list(rt.run_id, 'effect', kind='regression_measurement')), 1)
                self.assertEqual(rt.store.get(rt.run_id, 'budget', 'tool_calls')['spent'], 1)

    def test_policy_rejects_unsupported_category_before_lab_or_tool_work(self):
        rt = self.runtime()
        trigger = rt.observe('policy-test', 'input_changed', 0, ['baseline'])
        model = Decision('recommend_change', {'change_kind': 'unsupported_strategy',
            'instruction': 'This domain policy does not authorize this category.', 'capability_id': CAPABILITY,
            'execution_contract_json': json.dumps({'version': 1, 'kind': 'exact_inputs', 'inputs': {'slopes': [1, 2, 3]}}),
            'capability_version': '1.0', 'expected_outcome': 'A test', 'measurement': 'Loss', 'falsifier': 'No change'})
        out = rt.run_meta(trigger['work_id'], model, self.reservation)
        self.assertEqual(out['proposal']['status'], 'rejected')
        self.assertEqual(rt.improvements.rows(), [])
        self.assertFalse(rt.store.list(rt.run_id, 'work', kind='diagnostic'))

    def test_no_policy_allows_nonempty_domain_category(self):
        rt = self.runtime(policy=None)
        rec, _, _, _ = self.recommend_and_execute(rt, category='numerical_parameter_schedule')
        self.assertEqual(rec['recommendation']['change_kind'], 'numerical_parameter_schedule')
        self.assertEqual(rec['measurement']['domain_metrics']['direction'], 'minimize')

    def test_external_adapter_fixture_delivers_grouped_untrusted_sources_to_next_lab(self):
        rt = self.runtime(external=True)
        query = {'topic': 'least_squares'}
        meta_work = rt.observe('source-review', 'input_changed', 0, ['baseline'])
        lab_work = rt.enqueue_lab('source-owner', ['baseline'])
        payload = {'capability_id': SOURCE_CAPABILITY, 'capability_version': '1.0', 'inputs_json': json.dumps(query)}
        proposals = [rt.run_meta(meta_work['work_id'], Decision('request_diagnostic', payload), self.reservation),
                     rt.run_lab(lab_work['id'], Decision('request_diagnostic', payload), self.reservation)]
        outputs = []
        for out in proposals:
            self.assertEqual(out['proposal']['status'], 'applied')
            work = json.loads(rt.store.read_blob(out['proposal']['result_ref']))
            outputs.append(rt.execute_adapter(SOURCE_CAPABILITY, query, trigger_id=work['id'], work_id=work['id']))
        self.assertEqual(outputs[0]['result']['source_group_id'], outputs[1]['result']['source_group_id'])
        source_work = next(w for w in rt.controller.ready(rt.run_id) if w['kind'] == 'lab'
                           and w['inputs'].get('source_effect_id') == outputs[1]['effect']['id'])
        observer = Decision('no_action', {'reason': 'The delivered abstract is external source text, not validation of this task.'})
        rt.run_lab(source_work['id'], observer, self.reservation)
        required = source_work['inputs']['required_evidence_ids']
        page = next(p for p in observer.inputs[0]['delivered_pages'] if p['evidence_id'] in required)
        self.assertTrue(page['complete_source'])
        fact = rt.store.get(rt.run_id, 'fact', page['evidence_id'])
        self.assertEqual(fact['kind'], 'source_statement')
        self.assertFalse(fact['provenance']['independent_evidence'])
        self.assertFalse(fact['provenance']['current_task_validation'])
        self.assertFalse(json.loads(page['content'])['full_text_retrieved'])
        self.assertEqual(rt.store.get(rt.run_id, 'budget', 'tool_calls')['spent'], 2)


if __name__ == '__main__':
    unittest.main()
