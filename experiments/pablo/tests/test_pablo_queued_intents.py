"""Real routing transactions and packets; model choices and scorer are fixtures."""
import json
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller, identity
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from rsi_lab.decision_dependencies import StaleDecision
from pablo_task import adapter, lookup, oracle
from pablo_task.research import registered_adapters
_spec = importlib.util.spec_from_file_location('queued_intent_model_fixture',
    Path(__file__).resolve().parents[3] / 'tests/v2/test_improvements.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
FakeModel = _fixture.FakeModel


class QueuedIntentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'run'
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.run = 'queued'
        self.c = Controller(self.store)
        initial = self.store.put_blob({'candidates': ['CCO'], 'scores': [0.1]})
        self.c.initialize(self.run, max_external=1,
            budgets={name: {'limit': 10000, 'hard': name.endswith('calls')}
                     for name in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds', 'cpu_seconds')},
            config={'execution_mode': 'replay', 'research_features': {'meta_lab': True},
                    'max_input_bytes': 500000, 'pablo_run_root': str(self.root),
                    'pablo_initial_ref': initial, 'pablo_max_new_evaluations': 20})
        self.rt = Runtime(self.store, self.run, adapters=[a for a in registered_adapters()
                          if a.adapter_id in {adapter.ADAPTER_ID, lookup.ADAPTER_ID}])
        self.c.add_fact(self.run, 'original', 'raw_fact', {'observed': 'fixture'},
                        {'source': 'test fixture'}, 'original')
        self.res = {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1}
        self.serial = 0

    def inputs(self, candidates):
        return {'run_root': str(self.root), 'run_id': self.run, 'candidates': candidates}

    def lab(self, candidates, *, decision=None):
        self.serial += 1
        work = self.rt.enqueue_lab('lab-' + str(self.serial), ['original'])
        model = FakeModel('request_diagnostic', {'capability_id': adapter.ADAPTER_ID,
                          'capability_version': adapter.VERSION,
                          'inputs_json': json.dumps(self.inputs(candidates))}, decision=decision)
        return self.rt.run_lab(work['id'], model, self.res), model

    def diagnostics(self):
        return self.store.list(self.run, 'work', kind='diagnostic')

    def test_lab_barrier_waits_for_original_work_and_lookup_exposes_alias(self):
        first, _ = self.lab(['CCC'])
        self.assertEqual(first['proposal']['status'], 'applied')
        work = self.diagnostics()[0]
        effects_before = len(self.store.list(self.run, 'effect'))
        with self.assertRaisesRegex(ValueError, 'Lab awaits'):
            self.lab(['C(C)C', 'CC'])
        self.assertEqual(effects_before, len(self.store.list(self.run, 'effect')))
        self.assertEqual(len(self.diagnostics()), 1)
        queued = adapter.context(self.store, self.run)
        self.assertTrue(queued['complete'])
        self.assertEqual(queued['queued_work'][0]['canonical_candidates'], ['CCC'])
        answer = lookup.prepare(self.inputs(['C(C)C', 'CC']))
        self.assertEqual(answer['rows'][0]['status'], 'pending_or_unknown')
        self.assertEqual(answer['rows'][0]['work_ids'], [work['id']])
        self.assertEqual(answer['rows'][1]['status'], 'not_seen_at_snapshot')
        with patch.object(oracle, 'score', return_value=0.25) as score:
            result = self.rt.execute_adapter(adapter.ADAPTER_ID, self.inputs(['CCC']),
                                             trigger_id=work['id'], work_id=work['id'])
        self.assertEqual(result['effect']['status'], 'succeeded')
        self.assertEqual(score.call_count, 1)
        self.assertEqual(adapter.read_progress(self.store, self.run)['new_evaluations'], 1)
        self.assertEqual(adapter.context(self.store, self.run)['queued_work_count'], 0)

    def test_stale_adoption_rolls_back_recommendation_and_dependency(self):
        # First response was admitted but crashed before its Work was queued.
        with patch.object(self.rt, '_route', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.lab(['CCC'])
        original = self.store.list(self.run, 'proposal', status='admitted')[0]
        work = self.c.enqueue(self.run, 'meta_discovery', 'recommend', 'recommend-work',
                              inputs={'evidence_ids': ['original']})
        model = FakeModel('recommend_change', {'change_kind': 'candidate_selection',
            'instruction': 'Score the screened candidate', 'capability_id': adapter.ADAPTER_ID,
            'capability_version': adapter.VERSION, 'expected_outcome': 'One novel score',
            'execution_contract_json': json.dumps({'version': 1, 'kind': 'exact_inputs',
                                                  'inputs': self.inputs(['CCC'])}),
            'measurement': 'Actual finite evaluation rows', 'falsifier': 'No new score'})
        result = self.rt.run_meta(work['id'], model, self.res)
        self.assertEqual(result['proposal']['status'], 'applied')
        self.assertEqual(model.inputs[0]['task']['constraints']['adapter_context'][adapter.ADAPTER_ID]
                         ['queued_work_count'], 0)
        recommendation = self.rt.improvements.rows()[0]
        route = self.rt._route
        def interleave(proposal):
            route(original)
            return route(proposal)
        # Existing operation commits after the adoption packet was admitted.
        # The adoption transaction first detects its now-stale queue read set.
        with patch.object(self.rt, '_route', side_effect=interleave):
            second, _ = self.lab(['C(C)C'], decision='adopt')
        self.assertEqual(second['proposal']['status'], 'blocked_stale')
        self.assertIn('Decision dependency changed', second['proposal']['error'])
        self.assertNotIn('queue_disposition', second['proposal'])
        current = self.store.get(self.run, 'registration', recommendation['id'])
        self.assertEqual(current['status'], 'proposed')
        self.assertEqual(current['response_fact_ids'], [])
        self.assertEqual(len(self.diagnostics()), 1)
        self.assertEqual(self.store.list(self.run, 'dependency',
                         id=identity('object', second['proposal']['id'])), [])

    def test_same_successful_route_replay_does_not_conflict_with_its_own_work(self):
        first, _ = self.lab(['CCC'])
        before = self.store.verify_history(self.run)
        original = self.diagnostics()[0]
        self.assertEqual(self.rt._route(first['proposal'])['id'], original['id'])
        self.assertEqual(len(self.diagnostics()), 1)
        self.assertEqual(self.store.verify_history(self.run), before)

    def test_two_previously_admitted_proposals_recheck_read_set_at_atomic_routing(self):
        # Crash after each model response was admitted but before its route.
        # Neither initial admission can observe the other operation's intent.
        with patch.object(self.rt, '_route', side_effect=KeyboardInterrupt):
            for candidates in (['CCC'], ['C(C)C']):
                with self.assertRaises(KeyboardInterrupt):
                    self.lab(candidates)
        proposals = self.store.list(self.run, 'proposal', status='admitted')
        self.assertEqual(len(proposals), 2)
        self.assertEqual(self.diagnostics(), [])
        first = self.rt._route(proposals[0])
        with self.assertRaisesRegex(StaleDecision, 'Decision dependency changed'):
            self.rt._route(proposals[1])
        self.assertEqual(len(self.diagnostics()), 1)
        self.assertEqual(self.rt._route(proposals[0])['id'], first['id'])
        self.assertEqual(len(self.diagnostics()), 1)
        self.assertEqual(self.store.list(self.run, 'dependency',
                         id=identity('object', proposals[1]['id'])), [])

    def test_completed_only_batch_rejected_while_partial_novel_batch_is_allowed(self):
        rejected, _ = self.lab(['OCC'])
        self.assertEqual(rejected['proposal']['status'], 'blocked_action')
        self.assertIn('already evaluated', rejected['proposal']['error'])
        self.assertEqual(self.diagnostics(), [])
        accepted, _ = self.lab(['OCC', 'CCC'])
        self.assertEqual(accepted['proposal']['status'], 'applied')
        self.assertEqual(len(self.diagnostics()), 1)

    def test_published_score_is_covered_without_new_work_or_effect_attribution(self):
        first, _ = self.lab(['CCC'])
        work = self.diagnostics()[0]
        with patch.object(oracle, 'score', return_value=0.25):
            result = self.rt.execute_adapter(adapter.ADAPTER_ID, self.inputs(['CCC']),
                                             trigger_id=work['id'], work_id=work['id'])
        rows = self.store.list(self.run, 'registration', kind='work_identity_claim')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'covered')
        self.assertEqual(rows[0]['effect_id'], result['effect']['id'])
        later, _ = self.lab(['C(C)C'])
        self.assertEqual(later['proposal']['status'], 'blocked_action')
        disposition = later['proposal']['queue_disposition']
        self.assertEqual(disposition['status'], 'covered')
        self.assertEqual(disposition['owner_work_ids'], [work['id']])
        self.assertEqual(len(self.diagnostics()), 1)

    def test_domain_coverage_uses_only_actual_finite_rows(self):
        effect = {'id': 'actual'}
        self.assertEqual(adapter.completed_identities(effect, {'data': {
            'effect_id': 'actual', 'evaluations': [{'candidate': 'CCC', 'score': 0.5}],
            'filters': [{'canonical': 'CC', 'status': 'budget_clipped'}]}}), ['CCC'])
        self.assertEqual(adapter.completed_identities(effect, {'data': {
            'effect_id': 'actual', 'evaluations': []}}), [])
        with self.assertRaises(ValueError):
            adapter.completed_identities(effect, {'data': {
                'effect_id': 'other', 'evaluations': [{'candidate': 'CCC', 'score': 0.5}]}})

    def test_driver_cuts_historical_duplicate_ordering_then_covers_late_adoption(self):
        """Cut the historical 943→954→965→976→986 order at queued execution.

        The real scheduler must execute the original five-candidate operation
        before older Meta/Lab wakeups. Even if those models still recommend and
        adopt the same pool, covered identities must prevent a second operation.
        """
        values = ['C', 'CC', 'CCC', 'CCCC', 'CCCCC']
        original_lab = self.rt.enqueue_lab('original-driver-lab', ['original'])
        meta = self.c.enqueue(self.run, 'meta_discovery', 'older-meta', 'older-meta-work',
                              inputs={'evidence_ids': ['original']})
        later_lab = self.c.enqueue(self.run, 'lab', 'older-lab', 'older-lab-work',
                                   inputs={'evidence_ids': ['original']})
        payload = {'capability_id': adapter.ADAPTER_ID, 'capability_version': adapter.VERSION,
                   'inputs_json': json.dumps(self.inputs(values))}
        queued = self.rt.run_lab(original_lab['id'], FakeModel('request_diagnostic', payload), self.res)
        self.assertEqual(queued['proposal']['status'], 'applied')
        original_tool = self.diagnostics()[0]
        self.assertLess(meta['created_seq'], original_tool['created_seq'])
        self.assertLess(later_lab['created_seq'], original_tool['created_seq'])
        with patch.object(oracle, 'score', return_value=0.25) as scorer:
            with patch.object(FakeModel, '__call__', side_effect=AssertionError('Queued tool must run first')):
                step = self.rt.run_until_wait(FakeModel('no_action', {'reason': 'unused'}),
                                               self.res, max_steps=1)
        self.assertEqual(step['steps'][0]['work_id'], original_tool['id'])
        self.assertEqual(scorer.call_count, 5)
        original_effect = self.store.get(self.run, 'effect', step['steps'][0]['effect_id'])
        self.assertEqual(original_effect['status'], 'succeeded')
        self.assertEqual(adapter.read_progress(self.store, self.run)['new_evaluations'], 5)
        advice = {'change_kind': 'candidate_selection', 'instruction': 'Evaluate the same five-candidate pool',
                  'capability_id': adapter.ADAPTER_ID, 'capability_version': adapter.VERSION,
                  'execution_contract_json': json.dumps({'version': 1, 'kind': 'exact_inputs',
                                                        'inputs': self.inputs(values)}),
                  'expected_outcome': 'Five more scores', 'measurement': 'Actual new rows',
                  'falsifier': 'Already completed identities add no rows'}
        recommended = self.rt.run_meta(meta['id'], FakeModel('recommend_change', advice), self.res)
        self.assertEqual(recommended['proposal']['status'], 'applied')
        adoption = self.rt.run_lab(later_lab['id'], FakeModel('request_diagnostic', payload, decision='adopt'), self.res)
        self.assertEqual(adoption['proposal']['status'], 'blocked_action')
        disposition = adoption['proposal']['queue_disposition']
        self.assertEqual(disposition['status'], 'covered')
        self.assertEqual(disposition['owner_work_ids'], [original_tool['id']])
        self.assertEqual({row['effect_id'] for row in disposition['conflicts']}, {original_effect['id']})
        self.assertEqual(len(self.diagnostics()), 1)
        self.assertEqual(len(self.store.list(self.run, 'effect', kind=adapter.EFFECT_KIND)), 1)
        self.assertEqual(adapter.read_progress(self.store, self.run)['new_evaluations'], 5)
        recommendation = self.rt.improvements.rows()[0]
        self.assertEqual(recommendation['status'], 'proposed')
        self.assertNotIn('executed_effect_id', recommendation)
        self.assertNotIn('tool_work_id', recommendation)
        self.assertEqual(recommendation['response_fact_ids'], [])

    def test_context_truncation_never_truncates_atomic_conflict_check(self):
        self.lab(['CCC'])
        original = adapter.queued_intents(self.store, self.run)[0]
        rows = [{**original, 'work_id': 'long-' + str(i),
                 'canonical_candidates': ['C' * 3000]} for i in range(20)]
        rows.append(original)
        with patch.object(adapter, 'queued_intents', return_value=rows):
            context = adapter.context(self.store, self.run)
            self.assertFalse(context['complete'])
            self.assertGreater(context['omitted_work_count'], 0)
            with self.assertRaisesRegex(ValueError, original['work_id']):
                adapter.admit_work(self.store, self.run, self.inputs(['C(C)C']))
