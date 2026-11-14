"""Offline method execution against synthetic observed scores; no real scorer."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode
from pablo_task import adapter, harness, methods, oracle
from pablo_task.literature import adapter as literature


@unittest.skipUnless(importlib.util.find_spec('rdkit'), 'Optional PABLO RDKit dependency required')
class ExecutableMethodsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name).resolve() / 'run')
        self.addCleanup(self.store.close)
        self.run = 'methods'
        self.controller = Controller(self.store)
        initial = self.store.put_blob({'candidates': ['CC', 'CCC', 'CCO', 'c1ccccc1'],
                                      'scores': [0.1, 0.2, 0.9, 0.3]})
        self.controller.initialize(self.run, max_external=1,
            budgets={k: {'limit': 10000, 'hard': k.endswith('calls')}
                     for k in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds', 'cpu_seconds')},
            config={'execution_mode': 'test', 'max_open_reviews': 0,
                    'pablo_run_root': str(self.store.root), 'pablo_initial_ref': initial,
                    'pablo_max_new_evaluations': 20, 'pablo_harness': harness.DEFAULT_CONFIG})
        self.runtime = Runtime(self.store, self.run, adapters=[methods.make_adapter()])
        content = {'provider': literature.PROVIDER, 'topic': 'sample_efficient_search',
                   'query': literature.TOPICS['sample_efficient_search'], 'http_status': 200,
                   'response_complete': True, 'response_sha256': 'a' * 64,
                   'records': [{'provider_source': 'SYNTHETIC', 'publication_id': 'method-fixture',
                                'abstract_text': 'Synthetic method evidence for offline software testing.'}]}
        self.controller.add_fact(self.run, 'paper', 'source_statement', content,
                                 {'adapter_id': literature.ADAPTER_ID, 'fixture': True}, 'paper')

    def inputs(self, **changes):
        return {'run_root': str(self.store.root), 'run_id': self.run, 'proposal_role': 'local',
                'anchor_parent': 'CCO', 'candidates': ['CCCC', 'CCCO', 'CCN', 'CCCl', 'CCOC'],
                'select_count': 2, 'method': 'knn_ucb_diversity',
                'parameters': {'neighbors': 2, 'exploration_weight': 0.2, 'diversity_weight': 0.1},
                'literature_evidence_ids': ['paper'], **changes}

    def execute(self, inputs=None, trigger='select'):
        with patch.object(oracle, 'score', side_effect=AssertionError('Method must not call hidden scorer')):
            return self.runtime.execute_adapter(methods.ADAPTER_ID, inputs or self.inputs(), trigger_id=trigger)

    def test_real_numeric_selection_replays_and_publishes_without_scoring(self):
        before = adapter.read_progress(self.store, self.run)
        result = self.execute()
        self.assertEqual(result['effect']['status'], 'succeeded', result)
        data = result['result']
        self.assertEqual(len(data['selected_candidates']), 2)
        self.assertEqual(len(data['candidate_predictions']), 2)
        self.assertEqual(sum(r['status'] == 'role_ineligible' for r in data['filters']), 3)
        self.assertTrue(all(0 <= r['predicted_mean'] <= 1 for r in data['candidate_predictions']))
        self.assertGreater(len({r['predicted_mean'] for r in data['candidate_predictions']}), 1)
        self.assertEqual(data['training_sha256'], before['trajectory_sha256'])
        self.assertEqual(data['literature_sources'][0]['fact_id'], 'paper')
        self.assertEqual(data['harness_binding']['proposal_role'], 'local')
        self.assertEqual(data['harness_binding']['turn'], 0)
        self.assertEqual(adapter.read_progress(self.store, self.run), before)
        replay = self.execute(trigger='select')
        self.assertEqual(replay, result)
        self.assertEqual(len(self.store.list(self.run, 'effect')), 1)
        self.assertEqual(len([f for f in self.store.list(self.run, 'fact')
                              if f['provenance'].get('adapter_id') == methods.ADAPTER_ID]), 1)

    def test_alias_order_is_deterministic_but_method_or_parameters_change_plan(self):
        first = methods.prepare(self.inputs())
        reordered = methods.prepare(self.inputs(candidates=['COCC', 'ClCC', 'NCC', 'OCCC', 'CCCC', 'CCCC']))
        self.assertEqual(first, reordered)
        result = methods.select(first)
        self.assertEqual(result, methods.select(reordered))
        alternative = methods.select(methods.prepare(self.inputs(method='fingerprint_diversity')))
        self.assertNotEqual(result['plan_id'], alternative['plan_id'])
        changed = methods.select(methods.prepare(self.inputs(parameters={
            'neighbors': 1, 'exploration_weight': 0, 'diversity_weight': 0})))
        self.assertNotEqual(result['plan_id'], changed['plan_id'])

    def test_knn_predictions_change_with_observed_scores_not_invented_scores(self):
        packet = methods.prepare(self.inputs(parameters={
            'neighbors': 1, 'exploration_weight': 0, 'diversity_weight': 0}))
        original = methods.select(packet)
        changed = copy.deepcopy(packet)
        changed['observations']['scores'] = [1 - s for s in packet['observations']['scores']]
        alternative = methods.select(changed)
        a = {r['candidate']: r['predicted_mean'] for r in original['candidate_predictions']}
        b = {r['candidate']: r['predicted_mean'] for r in alternative['candidate_predictions']}
        self.assertTrue(all(abs(a[k] + b[k] - 1) < 1e-12 for k in a))
        self.assertNotEqual(original['selected_candidates'], alternative['selected_candidates'])

    def test_diversity_is_actual_greedy_minimum_distance(self):
        data = methods.select(methods.prepare(self.inputs(method='fingerprint_diversity')))
        maximum = max(r['observed_novelty_distance'] for r in data['candidate_predictions'])
        self.assertEqual(data['selection_rows'][0]['acquisition_value'], maximum)
        for row in data['selection_rows']:
            self.assertLessEqual(row['selection_diversity_distance'], row['observed_novelty_distance'])

    def test_invalid_or_unsupported_inputs_and_literature_fail_before_work(self):
        changes = [{'method': 'execute arbitrary paper code'}, {'anchor_parent': 'CN'},
                   {'candidates': ['not molecule']}, {'select_count': 0},
                   {'proposal_role': 'both'}, {'literature_evidence_ids': []},
                   {'parameters': {'neighbors': 2, 'exploration_weight': float('nan'), 'diversity_weight': 0}},
                   {'parameters': {'neighbors': 33, 'exploration_weight': 0, 'diversity_weight': 0}},
                   {'run_id': 'other'}, {'extra_code': 'never execute'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((ValueError, KeyError)):
                self.execute(self.inputs(**change), trigger='bad')
        self.assertEqual(self.store.list(self.run, 'effect'), [])
        self.controller.add_fact(self.run, 'fake-paper', 'raw_fact', {'text': 'claimed source'},
                                 {'adapter_id': literature.ADAPTER_ID}, 'fake')
        with self.assertRaises(ValueError):
            self.execute(self.inputs(literature_evidence_ids=['fake-paper']))

    def test_empty_abstract_and_inapplicable_topic_do_not_count_as_method_evidence(self):
        original = json.loads(self.store.read_blob(self.store.get(self.run, 'fact', 'paper')['content_ref']))
        for index, content in enumerate([
            {**original, 'records': []},
            {**original, 'records': [{**original['records'][0], 'abstract_text': ''}]},
            {**original, 'topic': 'matched_molecular_pairs', 'query': literature.TOPICS['matched_molecular_pairs']} ]):
            fid = 'bad-paper-' + str(index)
            self.controller.add_fact(self.run, fid, 'source_statement', content,
                                     {'adapter_id': literature.ADAPTER_ID}, fid)
            with self.assertRaises(ValueError):
                methods.prepare(self.inputs(literature_evidence_ids=[fid]))

    def test_queued_candidates_are_excluded_before_selection_effect_exists(self):
        payload = {'capability_id': adapter.ADAPTER_ID, 'inputs_json': json.dumps({
            'run_root': str(self.store.root), 'run_id': self.run, 'candidates': ['CCCO']})}
        self.controller.enqueue(self.run, 'diagnostic', 'score-queued', 'queue', inputs=payload)
        packet = methods.prepare(self.inputs(candidates=['CCO', 'CCCO', 'CCCC', 'OCCO']))
        self.assertEqual({r['candidate']: r['status'] for r in packet['filters']},
                         {'CCO': 'already_observed', 'CCCO': 'pending', 'CCCC': 'eligible', 'OCCO': 'eligible'})
        result = methods.select(packet)
        self.assertEqual(set(result['selected_candidates']), {'CCCC', 'OCCO'})
        with self.assertRaisesRegex(ValueError, 'Insufficient'):
            methods.prepare(self.inputs(candidates=['CCO', 'CCCO', 'CCN'], select_count=2))

    def test_scoring_plan_requires_exact_set_role_anchor_and_current_trajectory(self):
        selected = self.execute()['result']
        args = {'selection_effect_id': selected['effect_id'], 'plan_id': selected['plan_id'],
                'candidates': selected['selected_candidates'], 'proposal_role': 'local', 'anchor_parent': 'OCC'}
        bound = methods.validate_evaluation_plan(self.store, self.run, **args)
        self.assertEqual(bound['plan_id'], selected['plan_id'])
        for change in [{'plan_id': 'invented'}, {'candidates': selected['selected_candidates'][:1]},
                       {'candidates': ['CN', selected['selected_candidates'][1]]},
                       {'proposal_role': 'challenge'}, {'anchor_parent': 'CCC'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                methods.validate_evaluation_plan(self.store, self.run, **{**args, **change})
        actual = adapter.read_progress(self.store, self.run)
        with patch.object(adapter, 'read_progress', return_value={**actual, 'trajectory_sha256': 'changed'}):
            with self.assertRaisesRegex(ValueError, 'stale'):
                methods.validate_evaluation_plan(self.store, self.run, **args)

    def test_forged_dispatch_and_changed_snapshot_do_not_execute(self):
        packet = methods.prepare(self.inputs())
        self.assertEqual(methods.execute({'id': 'fake'}, encode(packet))['status'], 'failed_definite')
        work = self.controller.enqueue(self.run, 'diagnostic', 'dispatch', 'dispatch', inputs={'fixture': True})
        effect = self.controller.prepare_effect(self.run, work['id'], methods.EFFECT_KIND, packet,
            methods.reservations(packet), 'prepared', metadata={'adapter_id': methods.ADAPTER_ID, 'adapter_version': methods.VERSION})
        current = adapter.read_progress(self.store, self.run)
        with patch.object(adapter, 'read_progress', return_value={**current, 'trajectory_sha256': 'changed'}):
            effect = self.controller.dispatch_effect(self.run, effect['id'], methods.execute)
        self.assertEqual(effect['status'], 'failed_definite')

    def test_contract_and_semantic_result_do_not_claim_new_scores(self):
        registered = methods.make_adapter()
        self.assertTrue({'admit_work', 'recommendation_normalize', 'recommendation_completion',
                         'decision_dependencies', 'progress', 'required_evidence'}.issubset(registered.descriptor()['callbacks']))
        contract = {'version': 1, 'kind': 'exact_inputs',
                    'inputs': methods.normalize_recommendation(self.store, self.run, self.inputs())}
        run = self.execute()
        pub = {'data': run['result']}
        complete = methods.recommendation_completion(contract, run['effect'], pub)
        self.assertEqual(complete['status'], 'complete')
        self.assertEqual(complete['causal_benefit'], 'not_established')
        semantic = methods.progress(run['effect'], pub)
        self.assertEqual(semantic['new_scores'], 0)
        self.assertEqual(semantic, methods.progress({**run['effect'], 'id': 'another'}, pub))
        broken = copy.deepcopy(pub)
        broken['data']['input_contract']['parameters']['neighbors'] = 3
        self.assertEqual(methods.recommendation_completion(contract, run['effect'], broken)['status'], 'unsupported')

    def test_harness_role_anchor_and_batch_limits_are_checked_before_selection(self):
        for changes in ({'proposal_role': 'challenge'}, {'anchor_parent': 'CCC'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                methods.prepare(self.inputs(**changes))
        with patch.object(harness, 'validate_proposal', return_value={'max_candidates': 1}):
            with self.assertRaisesRegex(ValueError, 'Harness batch'):
                methods.prepare(self.inputs())
        before = harness.context(self.store, self.run)['next_plan']
        self.execute()
        self.assertEqual(harness.context(self.store, self.run)['next_plan'], before)

    def test_imported_paper_provenance_and_tampered_selection_publication(self):
        original = self.store.get(self.run, 'fact', 'paper')
        self.controller.add_fact(self.run, 'imported-paper', 'source_statement',
            json.loads(self.store.read_blob(original['content_ref'])),
            {'scientific_memory_import': 'fixture', 'original_provenance': {
                'scientific_memory_import': 'previous-fixture',
                'original_provenance': original['provenance']}}, 'imported-paper')
        inputs = self.inputs(literature_evidence_ids=['imported-paper'])
        run = self.execute(inputs)
        contract = {'version': 1, 'kind': 'exact_inputs',
                    'inputs': methods.normalize_recommendation(self.store, self.run, inputs)}
        bad = copy.deepcopy(run['result'])
        bad['selection_rows'][0]['predicted_mean'] = 123
        with self.assertRaisesRegex(ValueError, 'immutable method plan'):
            methods.publication(run['effect'], {'status': 'succeeded', 'result': bad})
        self.assertEqual(methods.recommendation_completion(contract, run['effect'], {'data': bad})['status'], 'unsupported')

    def test_unpublished_selection_cannot_be_consumed_and_method_requires_its_sources(self):
        self.assertEqual(methods.required_evidence(self.store, self.run, self.inputs()), ['paper'])
        run = self.execute()
        data = run['result']
        actual_list = self.store.list
        def unpublished(run_id, entity_kind, **filters):
            if entity_kind == 'registration' and filters.get('kind') == 'adapter_publication':
                return []
            return actual_list(run_id, entity_kind, **filters)
        with patch.object(self.store, 'list', side_effect=unpublished):
            with self.assertRaisesRegex(ValueError, 'complete published result'):
                methods.validate_evaluation_plan(self.store, self.run, selection_effect_id=data['effect_id'],
                    plan_id=data['plan_id'], candidates=data['selected_candidates'])

    def test_semantic_progress_ignores_effect_and_literature_alias_bookkeeping(self):
        result = self.execute()
        original = methods.progress(result['effect'], {'data': result['result']})
        alias = copy.deepcopy(result['result'])
        alias.update(effect_id='different-effect', plan_id='different-plan')
        alias['input_contract']['literature_evidence_ids'] = ['different-local-alias']
        alias['literature_sources'][0].update(fact_id='different-local-alias', content_sha256='different-wrapper')
        self.assertEqual(methods.progress(result['effect'], {'data': alias}), original)


if __name__ == '__main__':
    unittest.main()
