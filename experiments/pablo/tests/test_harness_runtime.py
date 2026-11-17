"""Whole local/challenge path with synthetic labels; never calls a real scorer.

The runtime, tool numerical selection, packets, Work queue, source gates,
publication, memory interpretation and scorer receipt path are real. Only model
responses and oracle.score are fixtures, and literature is a labeled fixture.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from experiments.pablo.tests.test_research_domain_boundary import Decision
from pablo_task import adapter, harness, methods, oracle, research
from pablo_task.literature import adapter as literature


class FakeModel(Decision):
    def __init__(self, action, payload, *, mutate=None):
        super().__init__(action, payload, update=mutate)


class HarnessRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name).resolve() / 'run'); self.addCleanup(self.store.close)
        self.run = 'harness-runtime'
        self.controller = Controller(self.store)
        initial = self.store.put_blob({'candidates': ['CCCO', 'Cc1ccncc1', 'Cc1ccccc1'],
                                      'scores': [.2, .7, .8]})
        self.controller.initialize(self.run, max_external=1,
            budgets={key: {'limit': 10000, 'hard': key.endswith('calls')}
                     for key in ('model_calls', 'tokens', 'tool_calls', 'cpu_seconds', 'wall_seconds')},
            config={'execution_mode': 'replay', 'max_open_reviews': 0,
                    'max_input_bytes': 500000, 'scientific_memory_context_bytes': 64000,
                    'research_features': {'scientific_memory': True, 'meta_lab': True, 'literature': True},
                    'research_policy': research.research_policy(),
                    'batch_interpretation_capabilities': ['pablo.evaluate'],
                    'pablo_run_root': str(self.store.root), 'pablo_initial_ref': initial,
                    'pablo_max_new_evaluations': 6,
                    'pablo_harness': {**harness.DEFAULT_CONFIG, 'stale_score_threshold': 2},
                    'literature_run_root': str(self.store.root), 'literature_max_calls': 2})
        self.rt = Runtime(self.store, self.run,
            adapters=research.registered_adapters(executable_methods=True),
            scientific_identity_provider=research.project_publication)
        self.paper = {'provider': literature.PROVIDER, 'topic': 'sample_efficient_search',
            'query': literature.TOPICS['sample_efficient_search'], 'http_status': 200,
            'response_complete': True, 'response_sha256': 'a' * 64,
            'records': [{'provider_source': 'SYNTHETIC', 'publication_id': 'offline-method-fixture',
                         'abstract_text': 'Synthetic abstract motivating a numerical selection regression fixture.'}]}
        self.controller.add_fact(self.run, 'paper', 'source_statement', self.paper,
                                 {'adapter_id': literature.ADAPTER_ID, 'fixture': True}, 'paper')
        self.rt.scientific_memory.observe_fact('paper')
        self.reservation = {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1}
        # Fail closed outside the explicit scoring helper, including accidental
        # access from candidate selection or packet construction.
        self.block_score = patch.object(oracle, 'score', side_effect=AssertionError('Real scorer forbidden'))
        self.block_score.start(); self.addCleanup(self.block_score.stop)

    def method_inputs(self, role='local'):
        return {'run_root': str(self.store.root), 'run_id': self.run,
            'proposal_role': role, 'anchor_parent': 'Cc1ccccc1' if role == 'local' else 'Cc1ccncc1',
            'candidates': (['CCc1ccccc1', 'CCCc1ccccc1', 'COc1ccccc1', 'Clc1ccccc1'] if role == 'local' else
                           ['CCc1ccncc1', 'CCCc1ccncc1', 'COc1ccncc1', 'Clc1ccncc1']),
            'select_count': 2, 'method': 'knn_ucb_diversity',
            'parameters': {'neighbors': 2, 'exploration_weight': .2, 'diversity_weight': .1},
            'literature_evidence_ids': ['paper']}

    def ready_lab(self):
        return next(w for w in self.controller.ready(self.run) if w['kind'] == 'lab')

    def propose(self, aid, inputs, *, work=None, mutate=None):
        if work is None:
            work = self.rt.enqueue_lab('request-' + str(len(self.store.list(self.run, 'effect'))), ['paper'])
        model = FakeModel('request_diagnostic', {'capability_id': aid, 'capability_version': '1.0',
                          'inputs_json': json.dumps(inputs)}, mutate=mutate)
        outcome = self.rt.run_lab(work['id'], model, self.reservation)
        return outcome, model

    def execute_proposed(self, aid, inputs):
        work = next(w for w in self.controller.ready(self.run)
                    if w['kind'] == 'diagnostic' and w['inputs']['capability_id'] == aid)
        if aid == adapter.ADAPTER_ID:
            with patch.object(oracle, 'score', return_value=.6) as synthetic_score:
                result = self.rt.execute_adapter(aid, inputs, trigger_id=work['id'], work_id=work['id'])
            self.assertEqual(synthetic_score.call_count, len(result['result']['evaluations']))
        else:
            result = self.rt.execute_adapter(aid, inputs, trigger_id=work['id'], work_id=work['id'])
        self.assertEqual(result['effect']['status'], 'succeeded', result)
        return result

    def select(self, role='local', *, work=None):
        inputs = self.method_inputs(role)
        proposed, model = self.propose(methods.ADAPTER_ID, inputs, work=work)
        self.assertEqual(proposed['proposal']['status'], 'applied', proposed['proposal'])
        return self.execute_proposed(methods.ADAPTER_ID, inputs), model

    def score_inputs(self, selection):
        result = selection['result']
        return {'run_root': str(self.store.root), 'run_id': self.run,
            'candidates': result['selected_candidates'], 'selection_effect_id': selection['effect']['id'],
            'selection_plan_id': result['plan_id'], 'proposal_role': result['proposal_role'],
            'anchor_parent': result['anchor_parent']}

    def score(self, selection):
        inputs = self.score_inputs(selection)
        proposed, model = self.propose(adapter.ADAPTER_ID, inputs, work=self.ready_lab())
        self.assertEqual(proposed['proposal']['status'], 'applied', proposed['proposal'])
        return self.execute_proposed(adapter.ADAPTER_ID, inputs), model

    @staticmethod
    def interpret(response, data):
        task = data['task']; barrier = task['constraints']['batch_interpretation']
        objects = {row['id']: row for row in task['scientific_memory']['objects']}
        response['scientific_memory_updates'] = json.dumps([{
            'kind': 'decision', 'id': 'interpret-' + ref['id'], 'expected_revision': 0,
            'question_ids': objects[ref['id']]['question_ids'],
            'content': {'result_interpretation': 'Both fixture scores are 0.6, below the inherited 0.8 best.',
                        'decision': 'Preserve the negative result and test the next Harness role and anchor.',
                        'scope': {'source_effect_id': barrier['source_effect_id']}},
            'source_evidence_ids': barrier['required_evidence_ids'],
            'object_refs': [{**ref, 'relation': 'derived_from'}]} for ref in barrier['investigations']])

    def finish_interpretation(self):
        work = next(w for w in self.controller.ready(self.run) if w['inputs'].get('batch_interpretation_id'))
        model = FakeModel('no_action', {'reason': 'Interpret actual fixture score outcomes'}, mutate=self.interpret)
        result = self.rt.run_lab(work['id'], model, self.reservation)
        self.assertEqual(result['proposal']['status'], 'applied', result['proposal'])
        return result, model

    def assert_delivered(self, model, fact_id):
        pages = [p for p in model.inputs[0]['delivered_pages'] if p['evidence_id'] == fact_id]
        self.assertTrue(pages and all(p['complete_source'] for p in pages))

    def test_selected_scored_interpreted_then_challenge_with_real_role_credit(self):
        selection, chooser = self.select()
        self.assert_delivered(chooser, 'paper')
        self.assertEqual(selection['result']['literature_sources'][0]['content_sha256'],
                         self.store.get(self.run, 'fact', 'paper')['content_ref'])
        before = adapter.read_progress(self.store, self.run)
        self.assertEqual(selection['result']['training_sha256'], before['trajectory_sha256'])
        score, scorer = self.score(selection)
        method_fact = next(f['id'] for f in self.store.list(self.run, 'fact')
                           if f['provenance'].get('effect_id') == selection['effect']['id'])
        self.assert_delivered(scorer, method_fact)
        self.assertEqual(score['result']['harness_binding']['proposal_role'], 'local')
        barrier = self.rt.batch_interpretations.rows()[0]
        self.assertEqual(barrier['status'], 'awaiting_interpretation')
        older = self.rt.enqueue_lab('older-pending-wakeup', ['paper'])
        self.assertNotIn(older['id'], {w['id'] for w in self.controller.ready(self.run)})
        with self.assertRaisesRegex(ValueError, 'before interpreting'):
            self.rt.execute_adapter(adapter.ADAPTER_ID, self.score_inputs(selection), trigger_id='bypass')
        reviewed, review_model = self.finish_interpretation()
        self.assertTrue(review_model.inputs[0]['task']['constraints']['interpretation_only'])
        next_selection, next_model = self.select('challenge', work=self.ready_lab())
        task = next_model.inputs[0]['task']
        self.assertEqual(task['constraints']['batch_interpretation']['phase'], 'decision')
        barrier = self.rt.batch_interpretations.rows()[0]
        objects = {(r['id'], r['revision']) for r in task['scientific_memory']['objects']}
        self.assertTrue(all((r['id'], r['revision']) in objects for r in barrier['decision_refs']))
        self.assertEqual(next_selection['result']['harness_binding']['proposal_role'], 'challenge')
        self.assertIn('score_plateau_challenge', next_selection['result']['harness_binding']['reason'])
        self.assertTrue(all(harness._family(c) != harness._family('Cc1ccccc1')
                            for c in next_selection['result']['selected_candidates']))
        self.score(next_selection)
        context = harness.context(self.store, self.run)
        self.assertEqual(context['next_plan']['role_credits']['local']['accepted_scores'], 2)
        self.assertEqual(context['next_plan']['role_credits']['challenge']['accepted_scores'], 2)
        self.assertEqual(context['next_plan']['completed_evaluations'], 4)
        self.assertTrue(any(t['to'] == 'alternating_local_challenge' for t in context['strategy_transitions']))
        with self.assertRaisesRegex(ValueError, 'stale'):
            methods.validate_evaluation_plan(self.store, self.run,
                selection_effect_id=selection['effect']['id'], plan_id=selection['result']['plan_id'],
                candidates=selection['result']['selected_candidates'])
        self.store.verify_history(self.run)

    def test_score_without_plan_or_with_changed_selected_set_never_executes(self):
        missing = {'run_root': str(self.store.root), 'run_id': self.run, 'candidates': ['CCc1ccccc1']}
        proposed, _ = self.propose(adapter.ADAPTER_ID, missing)
        self.assertNotEqual(proposed['proposal']['status'], 'applied')
        selection, _ = self.select()
        changed = self.score_inputs(selection)
        changed['candidates'] = changed['candidates'][:1]
        proposed, _ = self.propose(adapter.ADAPTER_ID, changed, work=self.ready_lab())
        self.assertNotEqual(proposed['proposal']['status'], 'applied')
        self.assertEqual(adapter.read_progress(self.store, self.run)['new_evaluations'], 0)
        self.assertFalse(adapter._effects(self.store, self.run))

    def test_method_requires_literature_in_actual_command_citations(self):
        proposed, model = self.propose(methods.ADAPTER_ID, self.method_inputs(),
            mutate=lambda response, data: response['command'].update(evidence_ids=[]))
        self.assert_delivered(model, 'paper')
        self.assertNotEqual(proposed['proposal']['status'], 'applied')
        self.assertFalse(any(e['metadata'].get('adapter_id') == methods.ADAPTER_ID
                             for e in self.store.list(self.run, 'effect')))

    def test_score_requires_method_result_in_actual_command_citations(self):
        selection, _ = self.select()
        proposed, _ = self.propose(adapter.ADAPTER_ID, self.score_inputs(selection), work=self.ready_lab(),
            mutate=lambda response, data: response['command'].update(evidence_ids=[]))
        self.assertNotEqual(proposed['proposal']['status'], 'applied')
        self.assertFalse(adapter._effects(self.store, self.run))


if __name__ == '__main__':
    unittest.main()
