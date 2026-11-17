"""V3 host admission and finalization boundaries, entirely offline."""
import copy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from experiments.pablo import run as host
from experiments.pablo.tests import test_host_preflight as fixtures
from experiments.pablo.tests.test_harness_runtime import FakeModel
from pablo_task import adapter, harness, methods, oracle
from pablo_task.literature import adapter as literature
from pablo_task.research import registered_adapters
from rsi_lab import input_view
from rsi_lab.store import Store, digest


class HostV3PreflightTests(unittest.TestCase):
    setUp = fixtures.HostPreflightTests.setUp
    open_run = fixtures.HostPreflightTests.open_run
    prepared_child = fixtures.HostPreflightTests.prepared_child
    assert_no_execution = fixtures.HostPreflightTests.assert_no_execution

    def v3_store(self, overrides=None):
        return self.open_run({'research_architecture': host.ARCHITECTURE,
            'pablo_harness': copy.deepcopy(harness.DEFAULT_CONFIG),
            'batch_interpretation_capabilities': [adapter.ADAPTER_ID],
            'effort': 'high', 'max_open_reviews': 0, **(overrides or {})})

    def scored_runtime(self):
        """Real method/receipt/publication path; model output and score labels are fixtures."""
        store = self.v3_store()
        runtime = host.make_runtime(store, 'run')
        paper = {'provider': literature.PROVIDER, 'topic': 'sample_efficient_search',
            'query': literature.TOPICS['sample_efficient_search'], 'http_status': 200,
            'response_complete': True, 'response_sha256': 'b' * 64,
            'records': [{'provider_source': 'SYNTHETIC', 'publication_id': 'host-offline-fixture',
                         'abstract_text': 'Synthetic evidence motivating a software test of candidate selection.'}]}
        runtime.controller.add_fact('run', 'paper', 'source_statement', paper,
            {'adapter_id': literature.ADAPTER_ID, 'fixture': True}, 'paper')
        runtime.scientific_memory.observe_fact('paper')
        selected = runtime.execute_adapter(methods.ADAPTER_ID, {
            'run_root': str(store.root), 'run_id': 'run', 'proposal_role': 'local',
            'anchor_parent': 'CCO', 'candidates': ['CCCC', 'CCCO'], 'select_count': 2,
            'method': 'knn_ucb_diversity',
            'parameters': {'neighbors': 1, 'exploration_weight': .2, 'diversity_weight': .1},
            'literature_evidence_ids': ['paper']}, trigger_id='offline-selection')
        self.assertEqual(selected['effect']['status'], 'succeeded')
        result = selected['result']
        scoring = {'run_root': str(store.root), 'run_id': 'run',
            'candidates': result['selected_candidates'], 'selection_effect_id': selected['effect']['id'],
            'selection_plan_id': result['plan_id'], 'proposal_role': 'local', 'anchor_parent': 'CCO'}
        method_fact = next(f['id'] for f in store.list('run', 'fact')
                           if f['provenance'].get('effect_id') == selected['effect']['id'])
        work = runtime.enqueue_lab('offline-score-decision', [method_fact])
        proposal = runtime.run_lab(work['id'], FakeModel('request_diagnostic', {
            'capability_id': adapter.ADAPTER_ID, 'capability_version': adapter.VERSION,
            'inputs_json': json.dumps(scoring)}), {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1})
        self.assertEqual(proposal['proposal']['status'], 'applied', proposal['proposal'])
        tool = next(w for w in runtime.controller.ready('run')
                    if w['kind'] == 'diagnostic' and w['inputs']['capability_id'] == adapter.ADAPTER_ID)
        with patch.object(oracle, 'score', return_value=.05) as synthetic_score:
            completed = runtime.execute_adapter(adapter.ADAPTER_ID, scoring, trigger_id=tool['id'], work_id=tool['id'])
        self.assertEqual(completed['effect']['status'], 'succeeded', completed)
        self.assertEqual(synthetic_score.call_count, 2)
        self.assertEqual(adapter.read_progress(store, 'run')['remaining_evaluations'], 0)
        # Scripted model fixtures intentionally have no native provider thread.
        # Use the real supported reconstruction boundary, preserving memory, so
        # host native-context validation still runs rather than being mocked out.
        for session in store.list('run', 'agent_session', status='idle'):
            if session['committed_turn']:
                checkpoint = runtime.agents.seal_checkpoint(session['id'], 'offline-checkpoint:' + session['id'])
                runtime.agents.rebuild_session(session['id'], 'Offline fixture has no native provider thread',
                    'offline-rebuild:' + session['id'], checkpoint_id=checkpoint['id'])
        return store, runtime

    def test_v3_rejects_missing_harness_or_exact_scoring_interpretation_barrier(self):
        for changes in ({'pablo_harness': None}, {'batch_interpretation_capabilities': []},
                        {'batch_interpretation_capabilities': None},
                        {'batch_interpretation_capabilities': [methods.ADAPTER_ID]}):
            with self.subTest(changes=changes):
                store = self.v3_store(changes)
                with self.assertRaisesRegex(ValueError, 'v3 requires.*Harness.*interpretation barrier'):
                    host.make_runtime(store, 'run')
                self.assert_no_execution(store)

    def test_v3_rejects_missing_selector_and_required_original_evidence_hooks(self):
        for omitted in host.expectations({'pablo_harness': harness.DEFAULT_CONFIG})['capabilities']:
            store = self.v3_store()
            supplied = [a for a in registered_adapters(executable_methods=True) if a.adapter_id != omitted]
            with self.subTest(omitted=omitted), patch.object(host, 'registered_adapters', return_value=supplied):
                message = 'loaded adapters|' + omitted if omitted == adapter.ADAPTER_ID else omitted
                with self.assertRaisesRegex(ValueError, message):
                    host.make_runtime(store, 'run')
            self.assert_no_execution(store)
        for aid in (adapter.ADAPTER_ID, methods.ADAPTER_ID):
            store = self.v3_store()
            supplied = [replace(a, required_evidence=None) if a.adapter_id == aid else a
                        for a in registered_adapters(executable_methods=True)]
            with self.subTest(hook=aid), patch.object(host, 'registered_adapters', return_value=supplied):
                with self.assertRaisesRegex(ValueError, 'required_evidence: ' + aid):
                    host.make_runtime(store, 'run')
            self.assert_no_execution(store)

    def test_new_preparation_freezes_v3_harness_barrier_and_all_four_capabilities(self):
        args = self.prepared_child(effort='high')
        store = Store(args.root)
        self.addCleanup(store.close)
        config = store.run('child')['config']
        saved = json.loads((args.root / 'run-config.json').read_text())
        plan = json.loads((args.root / 'task-plan.json').read_text())
        manifest = json.loads((args.root / 'manifest.json').read_text())
        preflight = json.loads((args.root / 'preflight.json').read_text())
        expected = {adapter.ADAPTER_ID, methods.ADAPTER_ID, 'pablo.lookup', 'literature.search'}
        self.assertEqual(saved, config)
        self.assertEqual(config['research_architecture'], host.ARCHITECTURE)
        self.assertEqual(config['pablo_harness'], harness.DEFAULT_CONFIG)
        self.assertEqual(config['batch_interpretation_capabilities'], [adapter.ADAPTER_ID])
        self.assertEqual(plan['protocol']['parameters']['pablo_harness'], config['pablo_harness'])
        self.assertEqual(set(plan['capability_versions']), expected)
        self.assertEqual(set(preflight['expectations']['capabilities']), expected)
        self.assertEqual({r['adapter_id'] for r in preflight['feedback_adapters']}, expected)
        for report in preflight['requests']:
            self.assertEqual(set(report['request']['capabilities']), expected)
            self.assertTrue(report['feedback_contract']['request_bytes_verified'])
        for path in ('rsi_lab/batch_interpretations.py', 'experiments/pablo/pablo_task/harness.py',
                     'experiments/pablo/pablo_task/methods.py'):
            self.assertEqual(manifest['source_hashes'][path], digest((host.REPO / path).read_bytes()))
        self.assert_no_execution(store, 'child')

    def test_interpretation_preflight_accepts_zero_tools_and_checks_actual_no_action_schema(self):
        store, runtime = self.scored_runtime()
        review = next(w for w in runtime.controller.ready('run') if w['inputs'].get('batch_interpretation_id'))
        before = store.verify_history('run')
        report = host.startup_preflight(runtime)
        self.assertEqual(store.verify_history('run'), before)
        row = next(r for r in report['requests'] if r['request']['work_id'] == review['id'])
        self.assertTrue(row['request']['interpretation_only'])
        self.assertFalse(row['request']['final_review_only'])
        self.assertEqual(row['request']['capabilities'], [])
        self.assertEqual(row['request']['actions'], ['no_action'])
        self.assertFalse(row['feedback_contract']['recommendation_available'])
        agent, mode, task, options = runtime._context(review, read_only=True)
        planned = agent.prepare(mode, task, **options)
        packet = agent.for_dispatch(planned, mode, task, **options)
        envelope = json.loads(packet['envelope_bytes'])
        sent = input_view.decode(envelope['prompt'].rsplit('INPUT_DATA_JSON:\n', 1)[1],
                                 envelope['input_view_mode'])['task']
        self.assertEqual(sent['capability_catalog'], {})
        self.assertTrue(sent['constraints']['interpretation_only'])
        variants = envelope['response_schema']['properties']['command']['anyOf']
        self.assertEqual([v['properties']['action']['enum'] for v in variants], [['no_action']])
        self.assertIn('scientific_memory_updates', envelope['response_schema']['properties'])
        self.assertEqual(packet['packet_sha256'], row['request']['packet_sha256'])

    def test_final_review_conversion_keeps_pending_batch_interpretation_and_zero_tool_schema(self):
        store, runtime = self.scored_runtime()
        barrier = runtime.batch_interpretations.rows()[0]
        host.mark_final_reviews(runtime)
        review = store.get('run', 'work', barrier['review_work_id'])
        self.assertEqual(review['inputs']['batch_interpretation_id'], barrier['id'])
        self.assertTrue(review['inputs']['final_review_only'])
        report = host.startup_preflight(runtime)
        row = next(r for r in report['requests'] if r['request']['work_id'] == review['id'])
        self.assertTrue(row['request']['interpretation_only'])
        self.assertTrue(row['request']['final_review_only'])
        self.assertEqual(row['request']['actions'], ['no_action'])
        self.assertEqual(row['request']['capabilities'], [])
        self.assertIn('required interpretation', host.finalization_problem(runtime))

    def test_missing_or_deferred_interpretation_cannot_complete_even_with_score_quota_met(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                store, runtime = self.scored_runtime()
                review = next(w for w in runtime.controller.ready('run') if w['inputs'].get('batch_interpretation_id'))
                def update(response, data):
                    if not deferred:
                        return
                    task = data['task']
                    context = task['constraints']['batch_interpretation']
                    objects = {r['id']: r for r in task['scientific_memory']['objects']}
                    response['scientific_memory_updates'] = json.dumps([{
                        'kind': 'decision', 'id': 'deferred-' + ref['id'], 'expected_revision': 0,
                        'question_ids': objects[ref['id']]['question_ids'],
                        'content': {'decision': 'Defer this interpretation', 'result_interpretation': 'Fixture review incomplete',
                                    'result_disposition': 'deferred', 'scope': {'source_effect_id': context['source_effect_id']}},
                        'source_evidence_ids': context['required_evidence_ids'],
                        'object_refs': [{**ref, 'relation': 'derived_from'}]} for ref in context['investigations']])
                outcome = runtime.run_lab(review['id'], FakeModel('no_action', {'reason': 'Incomplete fixture review'}, mutate=update),
                    {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1})
                self.assertEqual(outcome['proposal']['status'], 'blocked_action')
                self.assertEqual(runtime.batch_interpretations.rows()[0]['status'], 'awaiting_interpretation')
                self.assertEqual(adapter.read_progress(store, 'run')['remaining_evaluations'], 0)
                # An operator-cancelled/absent ready queue must not turn an
                # unresolved interpretation into a completed experiment.
                def empty_queue(tx):
                    for work in tx.find('work', status='ready'):
                        tx.update('work', work['id'], {'status': 'cancelled'})
                store.mutate('run', 'fixture-empty-queue', 'fixture', empty_queue)
                self.assertEqual(runtime.controller.ready('run'), [])
                self.assertIn('required interpretation', host.finalization_problem(runtime))
                self.assertFalse((store.root / 'result.json').exists())


if __name__ == '__main__':
    unittest.main()
