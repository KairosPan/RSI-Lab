"""A recommendation is adopted only when its actual request implements a contract."""
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from rsi_lab import recommendation_contracts as contracts
from rsi_lab.runtime import Runtime
from rsi_lab.store import digest
from tests.v2 import test_improvements as fixtures


def normalize_candidates(store, run_id, inputs):
    if set(inputs) != {'candidates'} or not isinstance(inputs['candidates'], list):
        raise ValueError('Candidate inputs required')
    return {'candidates': sorted(set(value.lower() for value in inputs['candidates']))}


def changed_normalizer(store, run_id, inputs):
    return dict(inputs)


def incomplete_publication(effect, receipt):
    data = {**receipt['result'], 'evaluations': receipt['result']['evaluations'][:1]}
    return {'facts': [{'id': 'result_' + effect['id'], 'kind': 'raw_fact', 'content': data,
                      'provenance': {'effect_id': effect['id']}}], 'conclusions': [], 'data': data}


def coverage(contract, effect, publication):
    wanted = set(contract['inputs']['candidates'])
    actual = {row['candidate'].lower() for row in publication['data']['evaluations']}
    missing = sorted(wanted - actual)
    return {'status': 'partial' if missing else 'complete', 'missing': missing}


class RecommendationContractTests(unittest.TestCase):
    setUp = fixtures.ImprovementsTests.setUp
    recommend = fixtures.ImprovementsTests.recommend
    lab = fixtures.ImprovementsTests.lab

    def actual(self, candidates):
        work = next(w for w in self.c.ready('r') if w['kind'] == 'lab')
        return self.rt.run_lab(work['id'], fixtures.FakeModel('request_diagnostic', {
            'capability_id': 'candidate_test', 'capability_version': 'v1',
            'inputs_json': json.dumps({'candidates': candidates})}, decision='adopt'), self.res)

    def test_same_capability_with_wrong_candidates_is_not_adoption(self):
        row, _ = self.recommend()
        outcome = self.actual(['unrelated'])
        self.assertEqual(outcome['proposal']['status'], 'rejected')
        self.assertIn('do not satisfy', outcome['proposal']['error'])
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])
        current = self.store.get('r', 'registration', row['id'])
        self.assertEqual(current['status'], 'proposed')
        self.assertEqual(current['response_fact_ids'], [])

    def test_actual_inputs_are_rechecked_before_atomic_outbox_creation(self):
        row, _ = self.recommend()
        route = self.rt._route
        def alter_after_validation(proposal):
            proposal = copy.deepcopy(proposal)
            proposal['command']['payload']['inputs_json'] = '{"candidates":["unrelated"]}'
            return route(proposal)
        with patch.object(self.rt, '_route', side_effect=alter_after_validation):
            outcome = self.actual(['new'])
        self.assertEqual(outcome['proposal']['status'], 'blocked_action')
        self.assertIn('do not satisfy', outcome['proposal']['error'])
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])
        self.assertEqual(self.store.list('r', 'dependency', producer='candidate_test'), [])
        self.assertEqual(self.store.get('r', 'registration', row['id'])['status'], 'proposed')

    def test_exact_adoption_records_request_proof_without_claiming_completion(self):
        row, _ = self.recommend()
        outcome = self.actual(['new'])
        self.assertEqual(outcome['proposal']['status'], 'applied')
        row = self.store.get('r', 'registration', row['id'])
        verification = row['adoption_verification']
        self.assertEqual(verification['verification_scope'], 'request_inputs')
        self.assertEqual(verification['inputs_sha256'], digest({'candidates': ['new']}))
        self.assertEqual(verification['contract_sha256'], row['contract_sha256'])
        self.assertEqual(verification['execution_completion'], 'not_established')
        work = self.store.get('r', 'work', row['tool_work_id'])
        self.rt.execute_adapter('candidate_test', {'candidates': ['new']}, trigger_id=work['id'], work_id=work['id'])
        measurement = self.store.get('r', 'registration', row['id'])['measurement']
        self.assertEqual(measurement['execution_status'], 'succeeded')
        self.assertEqual(measurement['contract_completion']['status'], 'unsupported')
        self.assertEqual(measurement['causal_benefit'], 'not_established')

    def test_source_pinned_normalization_accepts_declared_semantic_equivalence(self):
        adapter = replace(self.rt.adapters.by_id['candidate_test'], recommendation_normalize=normalize_candidates)
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        self.advice['execution_contract_json'] = json.dumps({'version': 1, 'kind': 'exact_inputs',
                                                           'inputs': {'candidates': ['NEW', 'second']}})
        row, _ = self.recommend()
        self.assertEqual(row['execution_contract']['inputs'], {'candidates': ['new', 'second']})
        outcome = self.actual(['SECOND', 'new'])
        self.assertEqual(outcome['proposal']['status'], 'applied')
        self.assertIn('recommendation_normalize', adapter.descriptor()['callbacks'])

    def test_changed_normalizer_cannot_verify_a_previous_contract(self):
        adapter = replace(self.rt.adapters.by_id['candidate_test'], recommendation_normalize=normalize_candidates)
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        self.recommend()
        self.rt = Runtime(self.store, 'r', adapters=[replace(adapter, recommendation_normalize=changed_normalizer)])
        outcome = self.actual(['new'])
        self.assertEqual(outcome['proposal']['status'], 'rejected')
        self.assertIn('implementation changed', outcome['proposal']['error'])
        self.assertFalse(self.store.list('r', 'work', kind='diagnostic'))

    def test_legacy_recommendation_cannot_gain_verified_adoption(self):
        row, _ = self.recommend()
        self.store.mutate('r', 'legacy-contract', 'fixture_legacy_recommendation', lambda tx:
            tx.update('registration', row['id'], {'execution_contract': None,
                'contract_adapter_descriptor_ref': None, 'contract_sha256': None}))
        outcome = self.actual(['new'])
        self.assertEqual(outcome['proposal']['status'], 'rejected')
        self.assertIn('no frozen execution contract', outcome['proposal']['error'])
        self.assertNotIn('adoption_verification', self.store.get('r', 'registration', row['id']))
        # Explicit decline remains possible; old evidence is never rewritten.
        result, model = self.lab('decline')
        self.assertEqual(result['proposal']['status'], 'applied')
        self.assertEqual(model.inputs[0]['task']['constraints']['research_loop']['recommendations'][0]
                         ['execution_contract_status'], 'legacy_missing_contract')

    def test_successful_tool_with_partial_coverage_still_requires_honest_review(self):
        adapter = replace(self.rt.adapters.by_id['candidate_test'], publication=incomplete_publication,
            recommendation_normalize=normalize_candidates, recommendation_completion=coverage)
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        self.advice['execution_contract_json'] = json.dumps({'version': 1, 'kind': 'exact_inputs',
                                                           'inputs': {'candidates': ['new', 'second']}})
        row, _ = self.recommend()
        self.assertEqual(self.actual(['new', 'second'])['proposal']['status'], 'applied')
        work = self.store.get('r', 'work', self.rt.improvements.rows()[0]['tool_work_id'])
        self.rt.execute_adapter('candidate_test', {'candidates': ['new', 'second']}, trigger_id=work['id'], work_id=work['id'])
        row = self.store.get('r', 'registration', row['id'])
        self.assertEqual(row['status'], 'executed')
        self.assertEqual(row['measurement']['execution_status'], 'succeeded')
        self.assertEqual(row['measurement']['contract_completion']['status'], 'partial')
        self.assertEqual(row['measurement']['contract_completion']['details']['missing'], ['second'])
        review = next(w for w in self.c.ready('r') if w['inputs'].get('improvement_review_ids'))
        outcome = self.rt.run_meta(review['id'], fixtures.FakeModel('no_action', {'reason': 'Review partial result'}), self.res)
        self.assertEqual(outcome['proposal']['status'], 'applied')
        before = self.store.verify_history('r')
        self.rt.recover(); self.rt.recover()
        self.assertEqual(self.store.verify_history('r'), before)

    def test_invalid_or_missing_contract_is_rejected_before_recommendation_creation(self):
        self.advice['execution_contract_json'] = '{"version":1,"kind":"constraints","requirements":[]}'
        work = self.rt.observe('invalid', 'input_changed', 0, ['original'])
        result = self.rt.run_meta(work['work_id'], fixtures.FakeModel('recommend_change', self.advice), self.res)
        self.assertEqual(result['proposal']['status'], 'rejected')
        self.assertEqual(self.rt.improvements.rows(), [])

    def test_adapter_can_require_a_complete_exact_inputs_contract(self):
        adapter = self.rt.adapters.by_id['candidate_test']
        adapter = replace(adapter, manifest={**adapter.manifest,
            'recommendation_contract_kinds': ['exact_inputs']})
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        self.advice['execution_contract_json'] = json.dumps({'version': 1, 'kind': 'constraints',
            'requirements': [{'path': ['candidates'], 'operator': 'contains_all', 'value': ['new']}]})
        work = self.rt.observe('incomplete-contract', 'input_changed', 0, ['original'])
        result = self.rt.run_meta(work['work_id'], fixtures.FakeModel('recommend_change', self.advice), self.res)
        self.assertEqual(result['proposal']['status'], 'rejected')
        self.assertIn('not permitted', result['proposal']['error'])
        self.assertEqual(self.rt.improvements.rows(), [])

    def test_exact_contract_creation_checks_registered_input_shape(self):
        self.advice['execution_contract_json'] = json.dumps({'version': 1, 'kind': 'exact_inputs',
                                                           'inputs': {'wrong_parameter': ['new']}})
        work = self.rt.observe('bad-contract-inputs', 'input_changed', 0, ['original'])
        result = self.rt.run_meta(work['work_id'], fixtures.FakeModel('recommend_change', self.advice), self.res)
        self.assertEqual(result['proposal']['status'], 'rejected')
        self.assertIn('Candidate input required', result['proposal']['error'])
        self.assertEqual(self.rt.improvements.rows(), [])

    def test_atomic_work_output_cannot_substitute_other_parameters(self):
        row, _ = self.recommend()
        create = self.rt.controller._work
        def substitute(tx, issue_id, kind, trigger, **kwargs):
            if kind == 'diagnostic':
                kwargs['inputs'] = {**kwargs['inputs'], 'inputs_json': '{"candidates":["unrelated"]}'}
            return create(tx, issue_id, kind, trigger, **kwargs)
        with patch.object(self.rt.controller, '_work', side_effect=substitute):
            result = self.actual(['new'])
        self.assertEqual(result['proposal']['status'], 'blocked_action')
        self.assertIn('do not satisfy', result['proposal']['error'])
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])
        self.assertEqual(self.store.list('r', 'dependency', producer='candidate_test'), [])
        self.assertEqual(self.store.get('r', 'registration', row['id'])['status'], 'proposed')


class ContractSyntaxTests(unittest.TestCase):
    def test_explicit_constraints_bind_paths_types_and_all_requirements(self):
        contract = contracts.parse(json.dumps({'version': 1, 'kind': 'constraints', 'requirements': [
            {'path': ['candidates'], 'operator': 'contains_all', 'value': ['a', 'b']},
            {'path': ['settings', 'count'], 'operator': 'min', 'value': 2},
            {'path': ['settings', 'count'], 'operator': 'max', 'value': 4},
            {'path': ['mode'], 'operator': 'one_of', 'value': ['screen', 'score']},
            {'path': ['flags', 0], 'operator': 'equals', 'value': True}]}))
        inputs = {'candidates': ['b', 'a'], 'settings': {'count': 3}, 'mode': 'score', 'flags': [True]}
        self.assertTrue(contracts._matches(contract, inputs))
        for change in ({'candidates': ['a']}, {'settings': {'count': True}}, {'settings': {'count': 5}},
                       {'flags': [1]}, {'mode': 'other'}, {'flags': []}):
            with self.subTest(change=change):
                self.assertFalse(contracts._matches(contract, {**inputs, **change}))

    def test_malformed_contracts_fail_closed(self):
        cases = [None, '{}', '{"version":1,"version":1,"kind":"exact_inputs","inputs":{}}',
                 '{"version":true,"kind":"exact_inputs","inputs":{}}',
                 '{"version":1,"kind":"exact_inputs","inputs":{"x":NaN}}',
                 '{"version":1,"kind":"exact_inputs","inputs":{"x":' + '[' * 2000 + '0' + ']' * 2000 + '}}']
        for requirement in ({'path': [], 'operator': 'equals', 'value': 1},
                            {'path': [True], 'operator': 'equals', 'value': 1},
                            {'path': ['x'], 'operator': 'min', 'value': True},
                            {'path': ['x'], 'operator': 'exec', 'value': 'code'},
                            {'path': ['x'], 'operator': 'one_of', 'value': []}):
            cases.append(json.dumps({'version': 1, 'kind': 'constraints', 'requirements': [requirement]}))
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                contracts.parse(raw)
