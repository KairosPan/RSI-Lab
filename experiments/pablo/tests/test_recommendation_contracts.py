"""PABLO machine contracts bind actual parameters, not the free-text rationale."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from rsi_lab import decision_dependencies, recommendation_contracts
from pablo_task import adapter, lookup, oracle, research
from pablo_task.literature import adapter as literature

_spec = importlib.util.spec_from_file_location('contract_model_fixture',
    Path(__file__).resolve().parents[3] / 'tests/v2/test_improvements.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
FakeModel = _fixture.FakeModel


class RecommendationContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name).resolve() / 'run')
        self.addCleanup(self.store.close)
        self.run = 'contracts'
        self.c = Controller(self.store)
        initial = self.store.put_blob({'candidates': ['CCO'], 'scores': [0.1]})
        self.c.initialize(self.run, max_external=1,
            budgets={key: {'limit': 10000, 'hard': key.endswith('calls')}
                     for key in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds', 'cpu_seconds')},
            config={'execution_mode': 'replay', 'research_features': {'meta_lab': True, 'literature': True},
                    'research_policy': research.research_policy(), 'max_input_bytes': 500000,
                    'pablo_run_root': str(self.store.root), 'pablo_initial_ref': initial,
                    'pablo_max_new_evaluations': 1, 'literature_run_root': str(self.store.root),
                    'literature_max_calls': 3})
        self.adapters = {a.adapter_id: a for a in research.registered_adapters()}
        self.rt = Runtime(self.store, self.run, adapters=self.adapters.values())
        self.c.add_fact(self.run, 'original', 'raw_fact', {'observation': 'test fixture'},
                        {'source': 'fixed test data'}, 'original')
        self.res = {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1}
        self.serial = 0

    def inputs(self, candidates):
        return {'run_root': str(self.store.root), 'run_id': self.run, 'candidates': candidates}

    def literature_inputs(self, **changes):
        return {'run_root': str(self.store.root), 'run_id': self.run,
                'topic': 'molecular_diversity', 'max_results': 2, **changes}

    def contract(self, aid, inputs):
        return {'version': 1, 'kind': 'exact_inputs',
                'inputs': self.adapters[aid].recommendation_normalize(self.store, self.run, inputs)}

    def recommend(self, aid, inputs):
        self.serial += 1
        work = self.c.enqueue(self.run, 'meta_discovery', 'contract-' + str(self.serial),
                              'meta-' + str(self.serial), inputs={'evidence_ids': ['original']})
        payload = {'change_kind': 'evidence_gathering' if aid == literature.ADAPTER_ID else 'candidate_selection',
                   'instruction': 'Execute precisely the machine-readable contract',
                   'capability_id': aid, 'capability_version': self.adapters[aid].version,
                   'execution_contract_json': json.dumps({'version': 1, 'kind': 'exact_inputs', 'inputs': inputs}),
                   'expected_outcome': 'A bounded observation', 'measurement': 'Actual publication coverage',
                   'falsifier': 'Requested operation is absent or incomplete'}
        result = self.rt.run_meta(work['id'], FakeModel('recommend_change', payload), self.res)
        self.assertEqual(result['proposal']['status'], 'applied', result['proposal'])
        return self.rt.improvements.rows()[0]

    def adopt(self, aid, inputs):
        self.serial += 1
        work = self.rt.enqueue_lab('adopt-' + str(self.serial), ['original'])
        payload = {'capability_id': aid, 'capability_version': self.adapters[aid].version,
                   'inputs_json': json.dumps(inputs)}
        return self.rt.run_lab(work['id'], FakeModel('request_diagnostic', payload, decision='adopt'), self.res)

    def publication_fixture(self, aid, **data):
        effect = {'id': 'executed', 'run_id': self.run, 'status': 'succeeded', 'packet_hash': 'packet',
                  'metadata': {'adapter_id': aid}}
        return effect, {'data': {'effect_id': effect['id'], 'packet_sha256': 'packet', **data}}

    def test_normalizers_are_source_pinned_and_canonical_sets_are_read_only(self):
        before = self.store.verify_history(self.run)
        for aid in (adapter.ADAPTER_ID, lookup.ADAPTER_ID):
            registered = self.adapters[aid]
            original = self.contract(aid, self.inputs(['CCC', 'CCO']))
            aliases = self.contract(aid, self.inputs(['OCC', 'C(C)C', 'CCC']))
            self.assertEqual(original, aliases)
            self.assertNotEqual(original, self.contract(aid, self.inputs(['CCO'])))
            descriptor = registered.descriptor()
            for hook in ('recommendation_normalize', 'recommendation_completion'):
                self.assertIn(hook, descriptor['callbacks'])
        self.assertEqual(self.store.verify_history(self.run), before)
        self.assertEqual(self.store.list(self.run, 'effect'), [])

    def test_invalid_molecules_unknown_fields_and_run_changes_never_normalize_away(self):
        for aid in (adapter.ADAPTER_ID, lookup.ADAPTER_ID):
            for inputs in (self.inputs(['CCC', 'not a molecule']), self.inputs([]),
                           {**self.inputs(['CCC']), 'query': 'free text'},
                           {**self.inputs(['CCC']), 'run_id': 'another-run'},
                           {**self.inputs(['CCC']), 'run_root': str(self.store.root.parent)}):
                with self.subTest(aid=aid, inputs=inputs):
                    with self.assertRaises(ValueError):
                        self.contract(aid, inputs)

    def test_literature_contract_freezes_query_limit_and_run_binding(self):
        original = self.contract(literature.ADAPTER_ID, self.literature_inputs())
        self.assertEqual(original['inputs']['query'], literature.TOPICS['molecular_diversity'])
        for changes in ({'topic': 'sample_efficient_search'}, {'max_results': 1}):
            self.assertNotEqual(original, self.contract(literature.ADAPTER_ID, self.literature_inputs(**changes)))
        for changes in ({'query': 'unrestricted query'}, {'run_id': 'elsewhere'}, {'max_results': True},
                        {'max_results': 6}, {'topic': 'unknown'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.contract(literature.ADAPTER_ID, self.literature_inputs(**changes))

    def test_weak_constraints_cannot_bypass_complete_pablo_input_contract(self):
        for aid, registered in self.adapters.items():
            self.assertEqual(registered.manifest['recommendation_contract_kinds'], ['exact_inputs'])
            payload = {'capability_id': aid, 'capability_version': registered.version,
                       'execution_contract_json': json.dumps({'version': 1, 'kind': 'constraints',
                           'requirements': [{'path': ['run_id'], 'operator': 'equals', 'value': self.run}]})}
            with self.subTest(aid=aid), self.assertRaisesRegex(ValueError, 'not permitted'):
                recommendation_contracts.prepare(self.rt, payload)

    def test_actual_alias_adoption_executes_once_and_certifies_only_request_coverage(self):
        self.recommend(adapter.ADAPTER_ID, self.inputs(['CCC']))
        adopted = self.adopt(adapter.ADAPTER_ID, self.inputs(['C(C)C']))
        self.assertEqual(adopted['proposal']['status'], 'applied', adopted['proposal'])
        row = self.rt.improvements.rows()[0]
        with patch.object(oracle, 'score', return_value=0.25) as score:
            execution = self.rt.execute_adapter(adapter.ADAPTER_ID, self.inputs(['C(C)C']),
                trigger_id=row['tool_work_id'], work_id=row['tool_work_id'])
        self.assertEqual(score.call_count, 1)
        self.assertEqual(execution['effect']['status'], 'succeeded')
        measurement = self.rt.improvements.rows()[0]['measurement']
        self.assertEqual(measurement['contract_completion']['status'], 'complete')
        self.assertEqual(measurement['contract_completion']['details']['scientific_status'], 'unverified')
        self.assertEqual(measurement['causal_benefit'], 'not_established')

    def test_same_capability_cannot_adopt_replaced_omitted_or_added_candidates(self):
        self.recommend(adapter.ADAPTER_ID, self.inputs(['CCC', 'CC']))
        for actual in (['CCC'], ['CCC', 'CCCC'], ['CCC', 'CC', 'CCCC']):
            with self.subTest(actual=actual):
                result = self.adopt(adapter.ADAPTER_ID, self.inputs(actual))
                self.assertEqual(result['proposal']['status'], 'rejected', result['proposal'])
                self.assertEqual(self.store.list(self.run, 'work', kind='diagnostic'), [])
                self.assertEqual(self.rt.improvements.rows()[0]['status'], 'proposed')
                self.assertNotIn('tool_work_id', self.rt.improvements.rows()[0])

    def test_same_literature_tool_cannot_adopt_changed_topic_or_result_limit(self):
        self.recommend(literature.ADAPTER_ID, self.literature_inputs())
        with patch.object(literature, '_fetch', side_effect=AssertionError('No HTTP in contract tests')):
            for changes in ({'topic': 'sample_efficient_search'}, {'max_results': 1}):
                with self.subTest(changes=changes):
                    result = self.adopt(literature.ADAPTER_ID, self.literature_inputs(**changes))
                    self.assertEqual(result['proposal']['status'], 'rejected', result['proposal'])
        self.assertEqual(self.store.list(self.run, 'work', kind='diagnostic'), [])
        self.assertNotIn('executed_effect_id', self.rt.improvements.rows()[0])

    def test_legacy_free_text_recommendation_cannot_adopt_same_tool(self):
        row = self.recommend(adapter.ADAPTER_ID, self.inputs(['CCC']))
        advice = {key: value for key, value in row['recommendation'].items() if key != 'execution_contract_json'}
        # A legacy row has prose and a capability, but no frozen machine contract.
        self.store.mutate(self.run, 'legacy-row-fixture', 'fixture', lambda tx: tx.update('registration', row['id'],
            {'recommendation': advice, 'execution_contract': None, 'contract_adapter_descriptor_ref': None,
             'contract_sha256': None, 'contract_adapter_descriptor_sha256': None}))
        result = self.adopt(adapter.ADAPTER_ID, self.inputs(['CCC']))
        self.assertEqual(result['proposal']['status'], 'rejected', result['proposal'])
        self.assertIn('no frozen execution contract', result['proposal']['error'])
        self.assertEqual(self.store.list(self.run, 'work', kind='diagnostic'), [])
        current = self.rt.improvements.rows()[0]
        self.assertEqual(current['status'], 'proposed')
        self.assertNotIn('tool_work_id', current)
        self.assertNotIn('executed_effect_id', current)

    def test_budget_clipping_records_partial_even_with_verified_inputs_and_successful_effect(self):
        values = ['CCC', 'CCCC']
        self.recommend(adapter.ADAPTER_ID, self.inputs(values))
        adopted = self.adopt(adapter.ADAPTER_ID, self.inputs(values))
        self.assertEqual(adopted['proposal']['status'], 'applied', adopted['proposal'])
        row = self.rt.improvements.rows()[0]
        with patch.object(oracle, 'score', return_value=0.25) as score:
            executed = self.rt.execute_adapter(adapter.ADAPTER_ID, self.inputs(values),
                trigger_id=row['tool_work_id'], work_id=row['tool_work_id'])
        self.assertEqual(executed['effect']['status'], 'succeeded')
        self.assertEqual(score.call_count, 1)
        completion = self.rt.improvements.rows()[0]['measurement']['contract_completion']
        self.assertEqual(completion['status'], 'partial')
        self.assertEqual(completion['details']['completed_count'], 1)
        self.assertEqual(completion['details']['missing'], ['CCCC'])
        self.assertEqual(completion['details']['scientific_status'], 'unverified')

    def test_empty_score_rows_and_legacy_advice_cannot_certify_completion(self):
        contract = self.contract(adapter.ADAPTER_ID, self.inputs(['CCC']))
        effect, pub = self.publication_fixture(adapter.ADAPTER_ID, evaluations=[])
        completion = research.evaluation_recommendation_completion(contract, effect, pub)
        self.assertEqual(completion['status'], 'partial')
        self.assertEqual(completion['missing'], ['CCC'])
        self.assertEqual(research.evaluation_recommendation_completion({}, effect, pub)['status'], 'unsupported')
        pub['data']['evaluations'] = [{'candidate': 'CCC', 'score': 0.2}]
        for change in ({'effect_id': 'other'}, {'packet_sha256': 'other'},
                       {'evaluations': [{'candidate': 'CCC', 'score': float('nan')}]},
                       {'evaluations': [{'candidate': 'CCCC', 'score': 0.2}]}):
            altered = copy.deepcopy(pub)
            altered['data'].update(change)
            self.assertEqual(research.evaluation_recommendation_completion(contract, effect, altered)['status'],
                             'unsupported')

    def test_lookup_coverage_is_not_scoring_and_literature_short_cache_is_partial(self):
        contract = self.contract(lookup.ADAPTER_ID, self.inputs(['CCC', 'CC']))
        effect, pub = self.publication_fixture(lookup.ADAPTER_ID, rows=[
            {'canonical': 'CCC', 'status': 'pending_or_unknown'},
            {'canonical': 'CC', 'status': 'not_seen_at_snapshot'}])
        complete = research.lookup_recommendation_completion(contract, effect, pub)
        self.assertEqual(complete['status'], 'complete')
        self.assertEqual(complete['scientific_status'], 'unverified')
        pub['data']['rows'].pop()
        self.assertEqual(research.lookup_recommendation_completion(contract, effect, pub)['status'], 'partial')
        contract = self.contract(literature.ADAPTER_ID, self.literature_inputs())
        effect, pub = self.publication_fixture(literature.ADAPTER_ID,
            provider=literature.PROVIDER, topic='molecular_diversity', query=literature.TOPICS['molecular_diversity'],
            requested_max_results=2, response_complete=True, http_status=200, provider_total_hits=10,
            returned_records=1, records=[{'id': 'one'}], cache_hit=True)
        self.assertEqual(research.literature_recommendation_completion(contract, effect, pub)['status'], 'partial')
        pub['data'].update(provider_total_hits=0, returned_records=0, records=[])
        self.assertEqual(research.literature_recommendation_completion(contract, effect, pub)['status'], 'complete')
        pub['data']['query'] = 'another query'
        self.assertEqual(research.literature_recommendation_completion(contract, effect, pub)['status'], 'unsupported')

    def test_scoring_read_set_detects_queue_effect_claim_changes_but_ignores_unrelated_state(self):
        selectors = research.scoring_decision_dependencies(self.store, self.run)
        def capture():
            with self.store.read_snapshot():
                return decision_dependencies.capture(self.store, self.run, {'run_id': self.run}, [], selectors)
        before = capture()
        def unrelated(tx):
            tx.insert('effect', 'other-tool', {'kind': 'other', 'status': 'succeeded',
                'metadata': {'adapter_id': 'unrelated.domain'}, 'result_ref': tx.blob({'ok': True})})
            tx.insert('effect', 'model', {'kind': 'lab', 'status': 'dispatching', 'metadata': {}})
            tx.update('budget', 'tokens', {'spent': 5})
            tx.insert('agent_session', 'bookkeeping', {'kind': 'lab', 'status': 'active'})
        self.store.mutate(self.run, 'unrelated-deps', 'fixture', unrelated)
        decision_dependencies.validate(self.store, self.run, before)
        self.c.enqueue(self.run, 'diagnostic', 'score-work', 'score-work',
            inputs={'capability_id': adapter.ADAPTER_ID, 'capability_version': adapter.VERSION,
                    'inputs_json': json.dumps(self.inputs(['CCC']))})
        with self.assertRaises(decision_dependencies.StaleDecision):
            decision_dependencies.validate(self.store, self.run, before)
        before = capture()
        self.store.mutate(self.run, 'score-deps', 'fixture', lambda tx: tx.insert('effect', 'score-effect',
            {'kind': adapter.EFFECT_KIND, 'status': 'prepared', 'metadata': {'adapter_id': adapter.ADAPTER_ID},
             'packet_ref': 'one', 'packet_hash': 'one', 'result_ref': None, 'accounting_unknown': False}))
        with self.assertRaises(decision_dependencies.StaleDecision):
            decision_dependencies.validate(self.store, self.run, before)
        before = capture()
        self.store.mutate(self.run, 'claim-deps', 'fixture', lambda tx: tx.insert('registration', 'claim',
            {'kind': 'work_identity_claim', 'adapter_id': adapter.ADAPTER_ID, 'identity': 'CCC',
             'owner_work_id': 'score-work', 'status': 'held', 'generation': 1, 'effect_id': None,
             'result_ref': None, 'publication_ref': None}))
        with self.assertRaises(decision_dependencies.StaleDecision):
            decision_dependencies.validate(self.store, self.run, before)
        before = capture()
        self.store.mutate(self.run, 'claim-covered', 'fixture', lambda tx: tx.update('registration', 'claim',
            {'status': 'covered', 'effect_id': 'score-effect', 'result_ref': 'receipt'}))
        with self.assertRaises(decision_dependencies.StaleDecision):
            decision_dependencies.validate(self.store, self.run, before)

    def test_literature_read_set_is_bound_to_its_own_query_frontier(self):
        with self.store.read_snapshot():
            before = decision_dependencies.capture(self.store, self.run, {'run_id': self.run}, [],
                research.literature_decision_dependencies(self.store, self.run))
        self.store.mutate(self.run, 'other-frontier', 'fixture', lambda tx: tx.insert('effect', 'unrelated',
            {'kind': 'other', 'status': 'succeeded', 'metadata': {'adapter_id': adapter.ADAPTER_ID}}))
        decision_dependencies.validate(self.store, self.run, before)
        self.store.mutate(self.run, 'literature-frontier', 'fixture', lambda tx: tx.insert('effect', 'literature',
            {'kind': literature.EFFECT_KIND, 'status': 'prepared', 'metadata': {'adapter_id': literature.ADAPTER_ID},
             'packet_ref': 'query', 'result_ref': None, 'accounting_unknown': False, 'held_reservations': {}}))
        with self.assertRaises(decision_dependencies.StaleDecision):
            decision_dependencies.validate(self.store, self.run, before)


if __name__ == '__main__':
    unittest.main()
