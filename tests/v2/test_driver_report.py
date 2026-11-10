"""The driver report tells a clean finish apart from a run that has silently stopped."""
import unittest

from tests.v2.liveness_support import (BUDGETS, ISSUE, RES, Scripted, diag, none, open_run, validating_adapter)
from tests.v2.test_operator_recovery import LIVE, LIVE_RES, operator_receipt
from tests.v2.test_operator_recovery import unknown as timed_out

EMPTY_ATTENTION = {'stalled_issues': [], 'stopped_lab': None, 'sessions_requiring_recovery': [],
                   'capacity_blocked_work': []}
REQUEST = {'source_id': 'src', 'source_version': 1, 'object_ids': ['o1'], 'fields': ['f'], 'query': 'q',
           'max_pages': 1, 'max_bytes': 4096}


def failed(receipt, index):
    return {**receipt, 'status': 'failed_definite', 'response': None, 'error': 'provider failure'}


def unknown(effect, raw):
    return {'status': 'unknown', 'actual_costs': {'model_calls': 0}}


def discover_issue(rt, controller):
    controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
    return rt.observe('e1', 'batch_boundary', 1, ['f1'])


class DriverReportTests(unittest.TestCase):
    def test_clean_finish_is_quiescent_with_nothing_to_attend(self):
        store, controller, rt = open_run(self)
        rt.enqueue_lab('lab-1')
        out = rt.run_until_wait(Scripted({'lab': none}), RES)
        self.assertEqual(out['status'], 'quiescent')
        self.assertEqual(out['report'], {'settlement_status': 'settled', 'work_status': 'empty',
                                         'attention': EMPTY_ATTENTION})
        store.verify_history('r')

    def test_open_issue_without_work_or_wait_is_stalled(self):
        store, controller, rt = open_run(self)
        discover_issue(rt, controller)
        executor = Scripted({'discovery': lambda d: ('propose_issue', ISSUE), 'disposition': none},
                            receipt=lambda r, i: failed(r, i) if i == 1 else r)
        out = rt.run_until_wait(executor, RES)
        issue = store.list('r', 'issue')[0]
        work = store.list('r', 'work', kind='meta_disposition')[0]
        self.assertEqual((out['status'], out['reason']), ('stalled', 'stalled_issues'))
        self.assertEqual(out['report']['attention']['stalled_issues'],
                         [{'issue_id': issue['id'], 'last_work_id': work['id'], 'last_work_status': 'failed'}])
        store.verify_history('r')

    def test_failed_lab_call_is_reported_as_stopped_lab(self):
        store, controller, rt = open_run(self)
        lab = rt.enqueue_lab('lab-1')
        out = rt.run_until_wait(Scripted({'lab': none}, receipt=failed), RES)
        self.assertEqual((out['status'], out['reason']), ('stalled', 'stopped_lab'))
        self.assertEqual(out['report']['attention']['stopped_lab'],
                         {'work_id': lab['id'], 'work_status': 'failed', 'proposal_status': None,
                          'reason': 'provider failure'})
        store.verify_history('r')

    def test_lab_whose_repair_was_also_rejected_is_reported_as_stopped_lab(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong_key': 1}))})
        stopped = rt.run_until_wait(executor, RES)['report']['attention']['stopped_lab']
        self.assertEqual((stopped['work_status'], stopped['proposal_status']), ('succeeded', 'rejected'))
        self.assertEqual(stopped['reason'], 'Tool inputs rejected by probe: Probe adapter requires exactly record_id')
        store.verify_history('r')

    def test_cancelled_prepared_lab_effect_is_reported_as_stopped_lab(self):
        store, controller, rt = open_run(self)
        work = rt.enqueue_lab('lab-1')
        effect = rt.prepare_meta(work['id'], RES)
        controller.cancel_prepared('r', effect['id'], 'operator stop', 'cancel-1')
        out = rt.run_until_wait(Scripted({'lab': none}), RES)
        self.assertEqual((out['status'], out['reason']), ('stalled', 'stopped_lab'))
        self.assertEqual(out['report']['attention']['stopped_lab'],
                         {'work_id': work['id'], 'work_status': 'cancelled', 'proposal_status': None,
                          'reason': 'operator stop'})
        store.verify_history('r')

    def test_capacity_blocked_work_is_listed(self):
        store, controller, rt = open_run(self, config={'max_effective_context_bytes': 1})
        lab = rt.enqueue_lab('lab-1')
        out = rt.run_until_wait(Scripted({'lab': none}), RES)
        self.assertEqual((out['status'], out['reason']), ('stalled', 'stopped_lab'))
        self.assertEqual([(w['work_id'], w['kind']) for w in out['report']['attention']['capacity_blocked_work']],
                         [(lab['id'], 'lab')])
        self.assertEqual(out['report']['attention']['capacity_blocked_work'][0]['reason'],
                         'Persistent observable history and new input exceed the registered context budget')
        store.verify_history('r')

    def test_session_requiring_recovery_is_listed(self):
        store, controller, rt = open_run(self)
        rt.enqueue_lab('lab-1')
        out = rt.run_until_wait(unknown, RES)
        self.assertEqual(out['status'], 'blocked_external')
        self.assertEqual(out['report']['settlement_status'], 'blocked_external')
        self.assertEqual(out['report']['work_status'], 'unresolved_effects')
        self.assertEqual([s['role'] for s in out['report']['attention']['sessions_requiring_recovery']], ['lab'])
        store.verify_history('r')

    def test_waiting_issue_is_not_stalled(self):
        store, controller, rt = open_run(self)
        discover_issue(rt, controller)
        executor = Scripted({'discovery': lambda d: ('propose_issue', ISSUE),
                             'disposition': lambda d: ('request_evidence', REQUEST)})
        out = rt.run_until_wait(executor, RES)
        self.assertEqual(out['status'], 'awaiting_producer_or_deadline')
        self.assertEqual(out['report']['work_status'], 'pending_queued')     # the evidence_retrieve producer Work is ready
        self.assertEqual(out['report']['attention']['stalled_issues'], [])
        store.verify_history('r')

    def test_ready_work_behind_a_session_requiring_recovery_is_stalled(self):
        store, controller, rt = open_run(self, config=LIVE)
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(timed_out, LIVE_RES)
        effect = store.list('r', 'effect')[0]
        controller.reconcile('r', effect['id'], operator_receipt(effect['id'], actual_costs={'tokens': 1000,
                                                                                            'wall_seconds': 10}), 'k')
        rt.recover()
        lab2 = rt.enqueue_lab('lab-2')
        executor = Scripted({'lab': none})
        out = rt.run_until_wait(executor, LIVE_RES)
        self.assertEqual((out['status'], out['reason']), ('stalled', 'sessions_requiring_recovery'))
        self.assertEqual(executor.calls, [])
        self.assertEqual(store.get('r', 'work', lab2['id'])['status'], 'ready')
        store.verify_history('r')

    def test_budget_denial_is_reported_as_resource_exhausted(self):
        budgets = {**BUDGETS, 'tool_calls': {**BUDGETS['tool_calls'], 'limit': 0}}
        store, controller, rt = open_run(self, budgets=budgets, adapters=[validating_adapter()])
        answers = iter([('request_diagnostic', diag({'record_id': 'a'}))])
        executor = Scripted({'lab': lambda d: next(answers, none(d))})
        rt.enqueue_lab('lab-1')
        out = rt.run_until_wait(executor, RES, max_steps=6)
        self.assertEqual(out['status'], 'blocked_admission')
        self.assertEqual(out['report']['settlement_status'], 'resource_exhausted')
        store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
