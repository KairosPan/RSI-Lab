"""Real scheduler/packet/memory regression for interpretation before decisions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.roles import CodexExecutor
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from tests.v2 import test_improvements as f


class BatchInterpretationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'run'); self.addCleanup(self.store.close)
        self.c = Controller(self.store)
        self.c.initialize('r', budgets={r: {'limit': 1000, 'hard': r.endswith('calls')}
            for r in ['model_calls', 'tool_calls', 'tokens', 'wall_seconds']}, config={
                'execution_mode': 'replay', 'research_features': {'scientific_memory': True},
                'batch_interpretation_capabilities': ['candidate_test'], 'max_input_bytes': 500000})
        adapter = Adapter('candidate_test', 'v1', 'candidate_test', {'scope': 'fixture'},
                          f.prepare, f.reservations, f.execute, f.publication)
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        self.res = {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1}

    def score(self):
        work = self.rt.enqueue_lab('start', [])
        out = self.rt.run_lab(work['id'], f.FakeModel('request_diagnostic', {
            'capability_id': 'candidate_test', 'capability_version': 'v1',
            'inputs_json': json.dumps({'candidates': ['new']})}), self.res)
        self.assertEqual(out['proposal']['status'], 'applied')
        tool = self.store.list('r', 'work', kind='diagnostic')[0]
        return self.rt.execute_adapter('candidate_test', {'candidates': ['new']},
                                      trigger_id=tool['id'], work_id=tool['id'])

    def review(self):
        return next(w for w in self.c.ready('r') if w['inputs'].get('batch_interpretation_id'))

    def finish_review(self):
        return self.rt.run_lab(self.review()['id'],
            f.FakeModel('no_action', {'reason': 'Review the measured batch'}, mutate=self.interpret), self.res)

    def meta_interpret(self, decision_id):
        barrier = self.rt.batch_interpretations.rows()[0]
        work = self.rt.observe(decision_id, 'input_changed', 0, barrier['source_evidence_ids'])
        def update(response, data):
            # The Meta packet has the original investigation and sources, but
            # is not itself the Lab-owned batch review.
            context = dict(data['task']['constraints'])
            data['task']['constraints']['batch_interpretation'] = {
                'source_effect_id': barrier['effect_id'], 'investigations': barrier['investigations'],
                'required_evidence_ids': barrier['source_evidence_ids']}
            self.interpret(response, data)
            data['task']['constraints'] = context
            values = json.loads(response['scientific_memory_updates'])
            for value in values:
                value['id'] = decision_id
            response['scientific_memory_updates'] = json.dumps(values)
        return self.rt.run_meta(work['work_id'],
            f.FakeModel('no_action', {'reason': 'Record an independent interpretation'}, mutate=update), self.res)

    @staticmethod
    def interpret(response, data):
        task = data['task']; context = task['constraints']['batch_interpretation']
        objects = {r['id']: r for r in task['scientific_memory']['objects']}
        response['scientific_memory_updates'] = json.dumps([{
            'kind': 'decision', 'id': 'interpret_' + ref['id'], 'expected_revision': 0,
            'question_ids': objects[ref['id']]['question_ids'],
            'content': {'decision': 'Use the measured result to select the next experiment',
                        'result_interpretation': 'The actual measured score is 0.75; superiority is untested',
                        'scope': {'effect_id': context['source_effect_id']}},
            'source_evidence_ids': context['required_evidence_ids'],
            'object_refs': [{**ref, 'relation': 'derived_from'}]}
            for ref in context['investigations']])

    def test_result_interpretation_precedes_next_decision_even_with_older_wakeup(self):
        result = self.score()
        older = self.rt.enqueue_lab('another-notification', [])
        self.assertNotIn(older['id'], {w['id'] for w in self.c.ready('r')})
        with self.assertRaisesRegex(ValueError, 'Lab awaits'):
            self.rt.prepare_meta(older['id'], self.res)
        with self.assertRaisesRegex(ValueError, 'before interpreting'):
            self.rt.execute_adapter('candidate_test', {'candidates': ['next']}, trigger_id='bypass')
        review_model = f.FakeModel('no_action', {'reason': 'Interpret the completed batch'}, mutate=self.interpret)
        reviewed = self.rt.run_lab(self.review()['id'], review_model, self.res)
        self.assertEqual(reviewed['proposal']['status'], 'applied', reviewed['proposal'])
        actual = review_model.inputs[0]['task']
        self.assertTrue(actual['constraints']['interpretation_only'])
        self.assertEqual(actual['capability_catalog'], {})
        barrier = self.rt.batch_interpretations.rows()[0]
        self.assertEqual(barrier['status'], 'interpreted')
        ready = [w for w in self.c.ready('r') if w['kind'] == 'lab']
        self.assertEqual(len(ready), 1)
        model = f.FakeModel('no_action', {'reason': 'The delivered interpretation informed this decision'})
        self.rt.run_lab(ready[0]['id'], model, self.res)
        task = model.inputs[0]['task']
        self.assertEqual(task['constraints']['batch_interpretation']['phase'], 'decision')
        refs = {(r['id'], r['revision']) for r in task['scientific_memory']['objects']}
        self.assertTrue(all((r['id'], r['revision']) in refs for r in barrier['decision_refs']))
        self.assertIn('result_' + result['effect']['id'],
                      [p['evidence_id'] for p in model.inputs[0]['delivered_pages']])

    def test_missing_or_deferred_interpretation_keeps_barrier_and_repairs_once(self):
        self.score()
        first = self.rt.run_lab(self.review()['id'], f.FakeModel('no_action', {'reason': 'No update'}), self.res)
        self.assertEqual(first['proposal']['status'], 'blocked_action')
        repair = self.review()
        self.assertIn('repair_of', repair['inputs'])
        def defer(response, data):
            self.interpret(response, data)
            updates = json.loads(response['scientific_memory_updates'])
            updates[0]['content']['result_disposition'] = 'deferred'
            response['scientific_memory_updates'] = json.dumps(updates)
        second = self.rt.run_lab(repair['id'], f.FakeModel('no_action', {'reason': 'Unsupported'}, mutate=defer), self.res)
        self.assertEqual(second['proposal']['status'], 'blocked_action')
        self.assertEqual(self.rt.batch_interpretations.rows()[0]['status'], 'awaiting_interpretation')
        self.assertFalse(any(w['kind'] == 'lab' for w in self.c.ready('r')))

    def test_review_schema_and_router_reject_tool_bypass(self):
        self.score()
        out = self.rt.run_lab(self.review()['id'], f.FakeModel('request_diagnostic', {
            'capability_id': 'candidate_test', 'capability_version': 'v1',
            'inputs_json': json.dumps({'candidates': ['another']})}), self.res)
        self.assertEqual(out['proposal']['status'], 'rejected')
        self.assertEqual(len(self.store.list('r', 'work', kind='diagnostic')), 1)

    def test_crash_after_memory_commit_recovers_one_release_without_new_model_or_tool(self):
        result = self.score(); work = self.review()
        with patch.object(self.rt.batch_interpretations, 'settle_review', side_effect=RuntimeError('crash after memory')):
            with self.assertRaisesRegex(RuntimeError, 'crash after memory'):
                self.rt.run_lab(work['id'], f.FakeModel('no_action', {'reason': 'Review'}, mutate=self.interpret), self.res)
        model_count = len(self.store.list('r', 'effect'))
        recovered = self.rt.recover()
        self.assertEqual(recovered['errors'], [])
        self.assertEqual(self.rt.batch_interpretations.rows()[0]['status'], 'interpreted')
        self.assertEqual(len(self.store.list('r', 'effect')), model_count)
        self.rt.recover(); self.rt.publish_adapter_effect(result['effect'])
        self.assertEqual(len(self.rt.batch_interpretations.rows()), 1)
        self.assertEqual(len([w for w in self.c.ready('r') if w['kind'] == 'lab']), 1)

    def test_other_branch_is_not_blocked_by_current_batch(self):
        self.score()
        other = self.c.enqueue('r', 'lab', 'other', 'other',
                               inputs={'branch_id': 'independent', 'evidence_ids': []})
        self.assertIn(other['id'], {w['id'] for w in self.c.ready('r')})
        out = self.rt.run_lab(other['id'], f.FakeModel('no_action', {'reason': 'Independent task'}), self.res)
        self.assertEqual(out['proposal']['status'], 'applied')

    def test_rejected_review_cannot_borrow_another_turns_interpretation(self):
        self.score()
        self.assertEqual(self.meta_interpret('independent-before-review')['proposal']['status'], 'applied')
        def rejected(response, data):
            self.interpret(response, data)
            updates = json.loads(response['scientific_memory_updates'])
            updates[0]['source_evidence_ids'] = ['source-not-delivered']
            response['scientific_memory_updates'] = json.dumps(updates)
        out = self.rt.run_lab(self.review()['id'],
            f.FakeModel('no_action', {'reason': 'Attempt unsupported update'}, mutate=rejected), self.res)
        self.assertEqual(out['proposal']['status'], 'blocked_action')
        self.assertEqual(self.rt.batch_interpretations.rows()[0]['status'], 'awaiting_interpretation')
        fixed = self.finish_review()
        self.assertEqual(fixed['proposal']['status'], 'applied')
        barrier = self.rt.batch_interpretations.rows()[0]
        self.assertTrue(all(self.rt.scientific_memory.get(r['id'], r['revision'])['source_effect_id'] == fixed['effect']['id']
                            for r in barrier['decision_refs']))

    def test_additional_independent_interpretation_does_not_withdraw_pinned_review(self):
        self.score(); self.finish_review()
        before = self.rt.batch_interpretations.rows()[0]['decision_refs']
        self.assertEqual(self.meta_interpret('independent-after-review')['proposal']['status'], 'applied')
        work = next(w for w in self.c.ready('r') if w['kind'] == 'lab')
        context = self.rt.batch_interpretations.context(work)
        self.assertEqual(context['decision_refs'], before)
        self.rt.batch_interpretations.admit_execution('candidate_test')

    def test_changed_source_blocks_execution_after_review_was_completed(self):
        result = self.score(); self.finish_review()
        self.rt.scientific_memory.mark_source_changed('result_' + result['effect']['id'],
                                                      reason='Instrument correction requires review')
        count = len(self.store.list('r', 'effect'))
        with self.assertRaisesRegex(ValueError, 'current source-bound interpretation'):
            self.rt.execute_adapter('candidate_test', {'candidates': ['next']}, trigger_id='source-changed')
        self.assertEqual(len(self.store.list('r', 'effect')), count)

    def test_withdrawn_decision_revision_blocks_execution_after_completed_review(self):
        self.score(); self.finish_review()
        ref = self.rt.batch_interpretations.rows()[0]['decision_refs'][0]
        work = next(w for w in self.c.ready('r') if w['kind'] == 'lab')
        def withdraw(response, data):
            record = next(r for r in data['task']['scientific_memory']['objects'] if r['id'] == ref['id'])
            response['scientific_memory_updates'] = json.dumps([{
                'kind': 'decision', 'id': ref['id'], 'expected_revision': ref['revision'],
                'question_ids': record['question_ids'], 'content': {'decision': 'Withdraw this interpretation', 'scope': {}},
                'source_evidence_ids': record['required_evidence_ids'], 'object_refs': record['object_refs']}])
        out = self.rt.run_lab(work['id'], f.FakeModel('no_action', {'reason': 'Reconsider evidence'}, mutate=withdraw), self.res)
        self.assertEqual(out['proposal']['status'], 'applied')
        with self.assertRaisesRegex(ValueError, 'current source-bound interpretation'):
            self.rt.batch_interpretations.admit_execution('candidate_test')

    def test_source_change_after_prepare_invalidates_actual_decision_before_model(self):
        result = self.score(); self.finish_review()
        work = next(w for w in self.c.ready('r') if w['kind'] == 'lab')
        prepared = self.rt.prepare_meta(work['id'], self.res)
        self.rt.scientific_memory.mark_source_changed('result_' + result['effect']['id'], reason='Corrected measurement')
        model = f.FakeModel('no_action', {'reason': 'Must not consume a stale interpretation'})
        out = self.rt.run_lab(work['id'], model, self.res)
        self.assertEqual(model.inputs, [])
        self.assertEqual(self.store.get('r', 'effect', prepared['id'])['status'], 'failed_definite')
        self.assertIn('stale_decision', out)
        self.assertIsNone(out['proposal'])

    def test_other_agent_instance_is_not_blocked_by_current_batch(self):
        self.score()
        other = self.c.enqueue('r', 'lab', 'other-agent', 'other-agent',
            inputs={'agent_instance_id': 'independent-agent', 'evidence_ids': []})
        self.assertIn(other['id'], {w['id'] for w in self.c.ready('r')})
        out = self.rt.run_lab(other['id'], f.FakeModel('no_action', {'reason': 'Independent owner'}), self.res)
        self.assertEqual(out['proposal']['status'], 'applied')

    def test_two_completed_tools_recovered_out_of_order_do_not_deadlock_reviews(self):
        # The first execution succeeded before a crash interrupted publication.
        # A second direct tool completes before recovery publishes the first.
        with patch.object(self.rt, 'publish_adapter_effect', side_effect=RuntimeError('publication interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'publication interrupted'):
                self.rt.execute_adapter('candidate_test', {'candidates': ['first']}, trigger_id='first')
        first = self.store.list('r', 'effect', kind='candidate_test')[0]
        self.rt.execute_adapter('candidate_test', {'candidates': ['second']}, trigger_id='second')
        self.rt.publish_adapter_effect(first)
        barriers = self.rt.batch_interpretations.rows()
        self.assertEqual(len(barriers), 2)
        older_review = self.store.get('r', 'work', min(barriers, key=lambda r: r['created_seq'])['review_work_id'])
        self.assertIn(older_review['id'], {w['id'] for w in self.c.ready('r')})
        self.assertEqual(self.finish_review()['proposal']['status'], 'applied')
        ordinary = next(w for w in self.store.list('r', 'work', kind='lab', status='ready')
                        if not w['inputs'].get('batch_interpretation_id'))
        self.assertNotIn(ordinary['id'], {w['id'] for w in self.c.ready('r')})
        with self.assertRaisesRegex(ValueError, 'Lab awaits'):
            self.rt.batch_interpretations.context(ordinary)
        self.assertEqual(self.finish_review()['proposal']['status'], 'applied')
        self.assertTrue(all(r['status'] == 'interpreted' for r in self.rt.batch_interpretations.rows()))
        self.assertEqual(len([w for w in self.c.ready('r') if w['kind'] == 'lab']), 1)

    def test_real_executor_accepts_frozen_interpretation_schema_before_offline_transport(self):
        # A direct fixture tool leaves the first Lab session pristine, so the
        # real persistent executor can verify its first native-request context.
        self.rt.execute_adapter('candidate_test', {'candidates': ['fixture']}, trigger_id='native-envelope')
        executor = CodexExecutor(self.store.root / 'calls')
        fake = f.FakeModel('no_action', {'reason': 'Interpret actual fixture receipt'}, mutate=self.interpret)
        def checked_executor(effect, packet):
            self.assertTrue(effect['metadata']['interpretation_only'])
            wrong = {**effect, 'metadata': {**effect['metadata'], 'interpretation_only': False}}
            with self.assertRaisesRegex(ValueError, 'Unsupported exact model request envelope'):
                executor(wrong, packet)
            # Stop exactly where a provider would be invoked. Nothing invokes
            # a real model, subprocess or scorer; the durable fixture receipt
            # then exercises the same router and memory-release path.
            with patch('rsi_lab.roles.transport.invoke', side_effect=RuntimeError('offline transport reached')) as invoke:
                with self.assertRaisesRegex(RuntimeError, 'offline transport reached'):
                    executor(effect, packet)
                invoke.assert_called_once()
                self.assertEqual(invoke.call_args.args[1], json.loads(packet)['response_schema'])
            return fake(effect, packet)
        out = self.rt.run_lab(self.review()['id'], checked_executor, self.res)
        self.assertEqual(out['proposal']['status'], 'applied')
        self.assertEqual(self.rt.batch_interpretations.rows()[0]['status'], 'interpreted')

    def test_prepared_tool_resume_rechecks_interpretation_without_executing(self):
        result = self.score(); self.finish_review()
        work = next(w for w in self.c.ready('r') if w['kind'] == 'lab')
        proposed = self.rt.run_lab(work['id'], f.FakeModel('request_diagnostic', {
            'capability_id': 'candidate_test', 'capability_version': 'v1',
            'inputs_json': json.dumps({'candidates': ['next']})}), self.res)
        self.assertEqual(proposed['proposal']['status'], 'applied')
        diagnostic = next(w for w in self.store.list('r', 'work', kind='diagnostic', status='ready'))
        with patch.object(self.rt.controller, 'dispatch_effect', side_effect=RuntimeError('crash before tool dispatch')):
            with self.assertRaisesRegex(RuntimeError, 'crash before tool dispatch'):
                self.rt.execute_adapter('candidate_test', {'candidates': ['next']},
                                        trigger_id=diagnostic['id'], work_id=diagnostic['id'])
        prepared = self.store.get('r', 'work', diagnostic['id'])['effect_id']
        self.assertEqual(self.store.get('r', 'effect', prepared)['status'], 'prepared')
        self.rt.scientific_memory.mark_source_changed('result_' + result['effect']['id'], reason='Corrected prior measurement')
        dispatch, executed = self.rt.controller.dispatch_effect, []
        def track(run_id, effect_id, executor, **kwargs):
            def record_execution(effect, packet):
                executed.append(effect['id'])
                return executor(effect, packet)
            return dispatch(run_id, effect_id, record_execution, **kwargs)
        with patch.object(self.rt.controller, 'dispatch_effect', side_effect=track):
            out = self.rt.execute_adapter('candidate_test', {'candidates': ['next']},
                                          trigger_id=diagnostic['id'], work_id=diagnostic['id'])
        self.assertEqual(executed, [])
        self.assertEqual(out['effect']['status'], 'failed_definite')
        self.assertFalse(any(out['effect']['held_reservations'].values()))
        self.assertTrue(all(v == 0 for v in out['effect']['actual_costs'].values()))
        self.assertIsNone(out['result'])

    def test_crash_after_release_replays_without_second_wakeup_or_review(self):
        self.score(); work = self.review()
        mutate = self.rt._mutate
        def interrupted(key, event, apply, payload):
            if event == 'meta_proposal_applied':
                raise RuntimeError('crash after release')
            return mutate(key, event, apply, payload)
        with patch.object(self.rt, '_mutate', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'crash after release'):
                self.rt.run_lab(work['id'], f.FakeModel('no_action', {'reason': 'Review'}, mutate=self.interpret), self.res)
        self.assertEqual(self.rt.batch_interpretations.rows()[0]['status'], 'interpreted')
        effect_count = len(self.store.list('r', 'effect'))
        self.assertEqual(self.rt.recover()['errors'], [])
        self.assertEqual(self.rt.recover()['errors'], [])
        self.assertEqual(len(self.store.list('r', 'effect')), effect_count)
        self.assertEqual(len([w for w in self.c.ready('r') if w['kind'] == 'lab']), 1)

    def test_late_publication_between_admission_and_dispatch_blocks_next_execution(self):
        # A succeeded, but its result publication was interrupted. B then ran
        # and was interpreted, permitting a real next decision for C. Recovery
        # of A arrives precisely after C's out-of-transaction admission checks.
        with patch.object(self.rt, 'publish_adapter_effect', side_effect=RuntimeError('lost publication A')):
            with self.assertRaisesRegex(RuntimeError, 'lost publication A'):
                self.rt.execute_adapter('candidate_test', {'candidates': ['A']}, trigger_id='A')
        old_effect = self.store.list('r', 'effect', kind='candidate_test')[0]
        self.rt.execute_adapter('candidate_test', {'candidates': ['B']}, trigger_id='B')
        self.assertEqual(self.finish_review()['proposal']['status'], 'applied')
        work = next(w for w in self.c.ready('r') if w['kind'] == 'lab')
        next_decision = self.rt.run_lab(work['id'], f.FakeModel('request_diagnostic', {
            'capability_id': 'candidate_test', 'capability_version': 'v1',
            'inputs_json': json.dumps({'candidates': ['C']})}), self.res)
        self.assertEqual(next_decision['proposal']['status'], 'applied')
        diagnostic = next(w for w in self.store.list('r', 'work', kind='diagnostic', status='ready'))
        dispatch = self.rt.controller.dispatch_effect
        executed, inserted_barriers = [], []
        def interleave(run_id, effect_id, executor, **kwargs):
            self.assertIsNotNone(kwargs.get('pre_dispatch_check'))
            self.rt.publish_adapter_effect(old_effect)
            inserted_barriers.extend(r['id'] for r in self.rt.batch_interpretations.rows()
                                     if r['effect_id'] == old_effect['id'])
            def record_execution(effect, packet):
                executed.append(effect['id'])
                return executor(effect, packet)
            return dispatch(run_id, effect_id, record_execution, **kwargs)
        with patch.object(self.rt.controller, 'dispatch_effect', side_effect=interleave):
            out = self.rt.execute_adapter('candidate_test', {'candidates': ['C']},
                trigger_id=diagnostic['id'], work_id=diagnostic['id'])
        self.assertEqual(len(inserted_barriers), 1)
        self.assertEqual(executed, [])
        self.assertEqual(out['effect']['status'], 'failed_definite')
        self.assertFalse(any(out['effect']['held_reservations'].values()))
        self.assertTrue(all(value == 0 for value in out['effect']['actual_costs'].values()))
        self.assertIsNone(out['result'])
        self.assertFalse(self.store.list('r', 'registration', kind='adapter_publication', effect_id=out['effect']['id']))
        pending = self.rt.batch_interpretations.rows()
        self.assertEqual(sum(r['status'] == 'awaiting_interpretation' for r in pending), 1)
        self.assertEqual(self.review()['inputs']['batch_interpretation_id'], inserted_barriers[0])


if __name__ == '__main__':
    unittest.main()
