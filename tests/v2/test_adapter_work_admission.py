"""Domain admission must be atomic with queue creation and replay safely."""
import json
from dataclasses import replace
import unittest
from unittest.mock import patch

from rsi_lab.runtime import Runtime
from tests.v2 import test_improvements as fixtures


def reject_work(store, run_id, inputs):
    raise ValueError('already_scheduled: queued-work')


def public_context(store, run_id):
    return {'queued_work_ids': ['queued-work'], 'complete': True}


def oversized_context(store, run_id):
    return {'unbounded': 'x' * 65536}


class AdapterWorkAdmissionTests(unittest.TestCase):
    setUp = fixtures.ImprovementsTests.setUp

    def test_admission_rejection_creates_neither_dependency_nor_work(self):
        adapter = replace(self.rt.adapters.by_id['candidate_test'], admit_work=reject_work)
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        work = self.rt.enqueue_lab('start', ['original'])
        model = fixtures.FakeModel('request_diagnostic', {'capability_id': 'candidate_test',
            'capability_version': 'v1', 'inputs_json': json.dumps({'candidates': ['new']})})
        out = self.rt.run_meta(work['id'], model, self.res)
        self.assertEqual(out['proposal']['status'], 'blocked_action')
        self.assertIn('already_scheduled', out['proposal']['error'])
        self.assertEqual(self.store.list('r', 'dependency', producer='candidate_test'), [])
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])
        self.store.verify_history('r')

    def test_trusted_context_reaches_both_roles_without_creating_state(self):
        adapter = replace(self.rt.adapters.by_id['candidate_test'], context=public_context)
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        works = [self.rt.enqueue_lab('lab', ['original']),
                 self.store.get('r', 'work', self.rt.observe('meta', 'input_changed', 0, ['original'])['work_id'])]
        before = self.store.verify_history('r')
        for work in works:
            agent, mode, task, options = self.rt._context(work, read_only=True)
            self.assertEqual(task['constraints']['adapter_context']['candidate_test'], public_context(None, None))
            self.assertEqual(agent.prepare(mode, task, **options)['status'], 'ready')
        self.assertEqual(before, self.store.verify_history('r'))

    def test_context_size_is_bounded_before_model_dispatch(self):
        adapter = replace(self.rt.adapters.by_id['candidate_test'], context=oversized_context)
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        work = self.rt.enqueue_lab('lab', ['original'])
        before = self.store.verify_history('r')
        with self.assertRaisesRegex(ValueError, '64 KiB'):
            self.rt.request_report(work['id'])
        self.assertEqual(before, self.store.verify_history('r'))
        self.assertEqual(self.store.list('r', 'effect'), [])

    def test_domain_hooks_are_in_the_frozen_adapter_identity(self):
        adapter = self.rt.adapters.by_id['candidate_test']
        plain = adapter.descriptor()
        changed = replace(adapter, admit_work=reject_work, context=public_context).descriptor()
        self.assertNotEqual(plain, changed)
        self.assertIn('admit_work', changed['callbacks'])
        self.assertIn('context', changed['callbacks'])

    def test_legacy_direct_outbox_resumes_and_completed_retry_is_read_only(self):
        inputs = {'candidates': ['new']}
        old = self.c.enqueue('r', 'diagnostic', 'adapter:candidate_test:legacy',
            'adapter-work:candidate_test:legacy', inputs={'adapter_id': 'candidate_test', 'input': inputs})
        # A pre-upgrade direct call already committed this historical payload;
        # the new enqueue shape must never rewrite its idempotency key.
        result = self.rt.execute_adapter('candidate_test', inputs, trigger_id='legacy')
        self.assertEqual(result['effect']['work_id'], old['id'])
        self.assertEqual(result['effect']['status'], 'succeeded')
        self.assertEqual(self.store.get('r', 'work', old['id'])['inputs'], old['inputs'])
        before = self.store.verify_history('r')
        with patch.object(self.rt.controller, 'prepare_effect',
                          side_effect=AssertionError('A completed outbox must not prepare another effect')):
            replay = self.rt.execute_adapter('candidate_test', inputs, trigger_id='legacy')
        self.assertEqual(replay, result)
        self.assertEqual(self.store.verify_history('r'), before)
        self.assertEqual(len(self.store.list('r', 'effect')), 1)
        self.assertEqual(self.store.get('r', 'budget', 'tool_calls')['spent'], 1)
        with self.assertRaisesRegex(ValueError, 'differs from its admitted Work'):
            self.rt.execute_adapter('candidate_test', {'candidates': ['changed']}, trigger_id='legacy')
        self.assertEqual(self.store.verify_history('r'), before)
