"""A host can check a tool request against the record of the model call that made it.

Hooks are module-level functions because the adapter registry pins their source
and rejects closures. Nothing here calls a model or the network.
"""
import json
from dataclasses import replace
import unittest

from tests.v2.liveness_support import RES, Scripted, by_seq, diag, open_run, validating_adapter

SEEN = []
CHILDREN = [{'thread_id': 'c1', 'role': 'challenge'}]


def record_request(store, run_id, inputs, effect):
    receipt = json.loads(store.read_blob(effect['result_ref']))
    SEEN.append({'run_id': run_id, 'inputs': inputs, 'effect': effect, 'child_threads': receipt.get('child_threads')})


def refuse_work(store, run_id, inputs):
    raise ValueError('no')


def refuse_request(store, run_id, inputs, effect):
    raise ValueError('no')


def with_children(receipt, index):
    return {**receipt, 'child_threads': CHILDREN}


def request(data):
    return 'request_diagnostic', diag({'record_id': 'a'})


class AdmitRequestTests(unittest.TestCase):
    def setUp(self):
        SEEN.clear()

    def lab(self, adapter, *, receipt=None):
        store, controller, rt = open_run(self, adapters=[adapter])
        work = rt.enqueue_lab('lab-1')
        return store, rt.run_lab(work['id'], Scripted({'lab': request}, receipt=receipt), RES)

    def test_hook_receives_the_effect_that_made_the_request(self):
        store, out = self.lab(replace(validating_adapter(), admit_request=record_request))
        self.assertEqual(out['proposal']['status'], 'applied', out['proposal'])
        [seen] = SEEN
        self.assertEqual(seen['effect']['id'], out['proposal']['effect_id'])
        self.assertEqual(seen['effect'], store.get('r', 'effect', out['effect']['id']))
        self.assertEqual(seen['effect']['kind'], 'lab')
        self.assertEqual((seen['run_id'], seen['inputs']), ('r', {'record_id': 'a'}))
        self.assertEqual(len(store.list('r', 'work', kind='diagnostic')), 1)
        store.verify_history('r')

    def test_hook_can_read_the_calls_child_threads(self):
        store, out = self.lab(replace(validating_adapter(), admit_request=record_request), receipt=with_children)
        self.assertEqual(out['proposal']['status'], 'applied', out['proposal'])
        [seen] = SEEN
        self.assertEqual(seen['child_threads'], [{'thread_id': 'c1', 'role': 'challenge'}])

    def test_a_meta_request_is_checked_against_the_meta_call(self):
        store, controller, rt = open_run(self, adapters=[replace(validating_adapter(), admit_request=record_request)])
        review = rt.observe('meta-1', 'batch_boundary', 1)
        out = rt.run_meta(review['work_id'], Scripted({'discovery': request}, receipt=with_children), RES)
        self.assertEqual(out['proposal']['status'], 'applied', out['proposal'])
        [seen] = SEEN
        self.assertEqual(seen['effect']['id'], out['effect']['id'])
        self.assertEqual(seen['effect']['kind'], 'meta')
        self.assertEqual(seen['child_threads'], CHILDREN)

    def test_refusal_is_handled_like_an_admit_work_refusal(self):
        outcomes = []
        for adapter in (replace(validating_adapter(), admit_work=refuse_work),
                        replace(validating_adapter(), admit_request=refuse_request)):
            store, out = self.lab(adapter)
            outcomes.append({'status': out['proposal']['status'], 'error': out['proposal']['error'],
                             'work': [(w['kind'], w['status'], w['inputs']) for w in by_seq(store.list('r', 'work'))],
                             'dependencies': store.list('r', 'dependency', producer='probe')})
            store.verify_history('r')
        by_admit_work, by_admit_request = outcomes
        self.assertEqual(by_admit_request, by_admit_work)
        self.assertEqual(by_admit_request['status'], 'blocked_action')
        self.assertEqual(by_admit_request['error'], 'no')
        self.assertNotIn('diagnostic', [kind for kind, _, _ in by_admit_request['work']])
        self.assertEqual(by_admit_request['dependencies'], [])

    def test_hook_is_not_asked_once_admit_work_has_refused(self):
        store, out = self.lab(replace(validating_adapter(), admit_work=refuse_work, admit_request=record_request))
        self.assertEqual(out['proposal']['status'], 'blocked_action')
        self.assertEqual(SEEN, [])

    def test_the_hook_is_part_of_the_adapter_identity(self):
        plain = validating_adapter()
        hooked = replace(plain, admit_request=record_request)
        self.assertIn('admit_request', hooked.descriptor()['callbacks'])
        self.assertNotEqual(hooked.descriptor(), plain.descriptor())
        with self.assertRaisesRegex(ValueError, 'admit_request'):
            replace(plain, admit_request='not callable')

    def test_adapters_without_the_hook_are_unaffected(self):
        plain = validating_adapter()
        self.assertIsNone(plain.admit_request)
        self.assertNotIn('admit_request', plain.descriptor()['callbacks'])
        store, out = self.lab(plain, receipt=with_children)
        self.assertEqual(out['proposal']['status'], 'applied', out['proposal'])
        [work] = store.list('r', 'work', kind='diagnostic')
        self.assertEqual(work['status'], 'ready')
        self.assertEqual(json.loads(work['inputs']['inputs_json']), {'record_id': 'a'})
        store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
