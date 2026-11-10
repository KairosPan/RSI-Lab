"""Opt-in policies for model calls without usage counters and for definite provider failures."""
import json
import unittest

from rsi_lab.runtime import Runtime
from tests.v2.liveness_support import ISSUE, RES, Scripted, by_seq, diag, none, open_run, validating_adapter

RES1 = {'model_calls': 0, 'tokens': 50, 'wall_seconds': 1}
CHARGE = {'unknown_usage_policy': 'charge_reservation'}
REQUEST = {'source_id': 'src', 'source_version': 1, 'object_ids': ['o1'], 'fields': ['f'], 'query': 'q',
           'max_pages': 1, 'max_bytes': 4096}


def failed(receipt, index):
    return {**receipt, 'status': 'failed_definite', 'response': None, 'error': 'provider failure'}


def no_tokens(receipt, index):
    return {**receipt, 'actual_costs': {'model_calls': 0, 'wall_seconds': 0}}


def fails_first(receipt, index):
    return failed(receipt, index) if index == 0 else receipt


def unknown(effect, raw):
    return {'status': 'unknown', 'actual_costs': {'model_calls': 0}}


def discover_issue(rt, controller):
    controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
    return rt.observe('e1', 'batch_boundary', 1, ['f1'])


class UnknownUsagePolicyTests(unittest.TestCase):
    def test_hold_is_the_default(self):
        store, controller, rt = open_run(self)
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none}, receipt=no_tokens)
        rt.run_until_wait(executor, RES1)
        effect = store.list('r', 'effect')[0]
        self.assertTrue(effect['accounting_unknown'])
        self.assertEqual(store.get('r', 'budget', 'tokens')['reserved'], 50)
        self.assertEqual(store.list('r', 'proposal'), [])
        store.verify_history('r')

    def test_charge_reservation_settles_and_lets_the_proposal_apply(self):
        store, controller, rt = open_run(self, config=CHARGE)
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none}, receipt=no_tokens)
        rt.run_until_wait(executor, RES1)
        effect = store.list('r', 'effect')[0]
        self.assertFalse(effect['accounting_unknown'])
        self.assertEqual(effect['actual_costs']['tokens'], 50)
        tokens = store.get('r', 'budget', 'tokens')
        self.assertEqual((tokens['spent'], tokens['reserved']), (50, 0))
        receipt = json.loads(store.read_blob(effect['receipt_ref']))
        self.assertEqual(receipt['estimated_resources'], ['tokens'])
        self.assertEqual(receipt['reconciliation']['provenance'], 'run config unknown_usage_policy=charge_reservation')
        self.assertEqual([p['status'] for p in store.list('r', 'proposal')], ['applied'])
        store.verify_history('r')

    def test_charge_reservation_lets_an_issue_be_closed_after_a_failed_call(self):
        store, controller, rt = open_run(self, config=CHARGE)
        discover_issue(rt, controller)
        executor = Scripted({'discovery': lambda d: ('propose_issue', ISSUE),
                             'disposition': lambda d: ('close_issue', {'resolution': 'closed_unresolved',
                                                                       'reason': 'provider failed'})},
                            receipt=lambda r, i: no_tokens(failed(r, i), i) if i == 1 else r)
        rt.run_until_wait(executor, RES1)
        iid = store.list('r', 'issue')[0]['id']
        controller.enqueue('r', 'meta_disposition', 'manual', 'manual', issue_id=iid)
        rt.run_until_wait(executor, RES1)
        self.assertEqual(executor.calls, ['discovery', 'disposition', 'disposition'])
        self.assertEqual(store.list('r', 'issue')[0]['status'], 'closed_unresolved')
        store.verify_history('r')

    def test_policy_is_applied_by_recover_after_a_restart(self):
        store, controller, rt = open_run(self, config=CHARGE)
        work = rt.enqueue_lab('lab-1')
        effect = rt.prepare_meta(work['id'], RES1)
        rt.controller.dispatch_effect('r', effect['id'], Scripted({'lab': none}, receipt=no_tokens))
        self.assertTrue(store.list('r', 'effect')[0]['accounting_unknown'])
        Runtime(store, 'r').recover()
        self.assertFalse(store.list('r', 'effect')[0]['accounting_unknown'])
        self.assertEqual([p['status'] for p in store.list('r', 'proposal')], ['applied'])
        store.verify_history('r')

    def test_unknown_effects_are_never_charged(self):
        store, controller, rt = open_run(self, config=CHARGE)
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(unknown, RES1)
        rt.recover()
        self.assertEqual(store.list('r', 'effect')[0]['status'], 'unknown')
        self.assertEqual(store.get('r', 'budget', 'tokens')['reserved'], 50)
        store.verify_history('r')

    def test_call_awaiting_writer_exit_proof_is_not_charged(self):
        store, controller, rt = open_run(self, config=CHARGE)
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none}, receipt=lambda r, i: {**no_tokens(r, i), 'recovered_from_journal': True,
                                                                'recovery_requires_fencing': True,
                                                                'native_writer_quiesced': False})
        rt.run_until_wait(executor, RES1)
        rt.recover()
        effect = store.list('r', 'effect')[0]
        self.assertTrue(effect['accounting_unknown'])
        self.assertEqual(store.get('r', 'budget', 'tokens')['reserved'], 50)
        store.verify_history('r')

    def test_invalid_policy_value_is_refused(self):
        with self.assertRaises(ValueError):
            open_run(self, config={'unknown_usage_policy': 'zero'})


class RetryPolicyTests(unittest.TestCase):
    def test_no_retry_by_default(self):
        store, controller, rt = open_run(self)
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none}, receipt=failed)
        rt.run_until_wait(executor, RES)
        self.assertEqual([w['status'] for w in store.list('r', 'work', kind='lab')], ['failed'])
        store.verify_history('r')

    def test_failed_lab_call_is_retried_and_then_succeeds(self):
        store, controller, rt = open_run(self, config={'model_failure_retries': 1})
        executor = Scripted({'lab': none}, receipt=fails_first)
        first = rt.enqueue_lab('lab-1')
        rt.run_until_wait(executor, RES)
        works = by_seq(store.list('r', 'work', kind='lab'))
        self.assertEqual([w['status'] for w in works], ['failed', 'succeeded'])
        self.assertEqual(works[1]['trigger'], 'retry:1:' + first['id'])
        self.assertEqual((works[1]['inputs']['retry_of'], works[1]['inputs']['retry_attempt']), (first['id'], 1))
        self.assertEqual(executor.calls, ['lab', 'lab'])
        store.verify_history('r')

    def test_retries_are_bounded(self):
        store, controller, rt = open_run(self, config={'model_failure_retries': 2})
        executor = Scripted({'lab': none}, receipt=failed)
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(executor, RES, max_steps=20)
        self.assertEqual(len(store.list('r', 'work', kind='lab')), 3)
        self.assertEqual(executor.calls, ['lab', 'lab', 'lab'])
        store.verify_history('r')

    def test_retry_is_not_duplicated_by_recover(self):
        store, controller, rt = open_run(self, config={'model_failure_retries': 1})
        work = rt.enqueue_lab('lab-1')
        rt.run_meta(work['id'], Scripted({'lab': none}, receipt=failed), RES)
        rt.recover()
        rt.recover()
        self.assertEqual(len(store.list('r', 'work', kind='lab')), 2)
        store.verify_history('r')

    def test_never_dispatched_failure_is_not_retried(self):
        store, controller, rt = open_run(self, config={'model_failure_retries': 1})
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none}, receipt=lambda r, i: {**failed(r, i), 'never_dispatched': True,
                                                                'terminal_received': False})
        rt.run_until_wait(executor, RES)
        self.assertEqual(len(store.list('r', 'work', kind='lab')), 1)
        store.verify_history('r')

    def test_wait_expiry_disposition_is_never_retried(self):
        store, controller, rt = open_run(self, config={'model_failure_retries': 2, 'wait_timeout_seconds': 0})
        discover_issue(rt, controller)
        executor = Scripted({'discovery': lambda d: ('propose_issue', ISSUE),
                             'disposition': lambda d: ('request_evidence', REQUEST)},
                            receipt=lambda r, i: failed(r, i) if i >= 2 else r)
        rt.run_until_wait(executor, RES, max_steps=20)
        rt.run_until_wait(executor, RES, max_steps=20)
        self.assertEqual(executor.calls, ['discovery', 'disposition', 'disposition'])
        self.assertEqual(len(store.list('r', 'work', kind='disposition')), 1)
        self.assertEqual([w for w in store.list('r', 'work') if w['trigger'].startswith('retry:')], [])
        store.verify_history('r')

    def test_retry_and_repair_together_stay_bounded(self):
        store, controller, rt = open_run(self, config={'model_failure_retries': 1}, adapters=[validating_adapter()])
        executor = Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong_key': 1}))}, receipt=fails_first)
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(executor, RES, max_steps=20)
        self.assertEqual(executor.calls, ['lab', 'lab', 'lab'])      # failed, retry rejected, one repair rejected
        self.assertEqual(rt.run_until_wait(executor, RES, max_steps=20)['steps'], [])
        store.verify_history('r')

    def test_invalid_retry_value_is_refused(self):
        for bad in (-1, 6, 1.5, True, 'two'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                open_run(self, config={'model_failure_retries': bad})


if __name__ == '__main__':
    unittest.main()
