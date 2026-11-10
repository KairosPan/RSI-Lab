"""The cooperative driver keeps going past rejected proposals, tool inputs and prepared Effects."""
import json
import unittest

from rsi_lab.runtime import Runtime
from tests.v2.liveness_support import (EXECUTIONS, ISSUE, RES, BUDGETS, Scripted, broken_publication_adapter, by_seq,
                                       diag, dying_adapter, lenient_adapter, none, open_run, validating_adapter)


def trial_payload(plan_ref):
    return {'trial_kind': 'tool_capability', 'baseline_version': 'v1', 'candidate_version': 'v2',
            'hypothesis': 'v2 is better', 'plan_ref': plan_ref}


def in_order(*answers):
    """Answer successive calls of one mode in order, repeating the last answer."""
    calls = []
    def answer(data):
        calls.append(data)
        return answers[min(len(calls), len(answers)) - 1]
    return answer


def discover_issue(rt, controller):
    controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
    return rt.observe('e1', 'batch_boundary', 1, ['f1'])


class RoutingTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def assert_trial_blocked(self, plan_ref_of):
        store, controller, rt = open_run(self)
        discover_issue(rt, controller)
        plan_ref = plan_ref_of(store)
        executor = Scripted({'discovery': lambda d: ('propose_issue', ISSUE),
                             'disposition': lambda d: ('propose_trial', trial_payload(plan_ref))})
        rt.run_until_wait(executor, RES, max_steps=5)
        again = rt.run_until_wait(executor, RES, max_steps=5)
        rt.recover()
        trial = [p for p in store.list('r', 'proposal') if (p['command'] or {}).get('action') == 'propose_trial']
        self.assertEqual([p['status'] for p in trial], ['blocked_action', 'blocked_action'])
        self.assertEqual(trial[0]['error'], 'Trial plan_ref does not name a stored trial specification')
        self.assertEqual(again['steps'], [])
        store.verify_history('r')

    def test_unknown_plan_ref_blocks_the_proposal(self):
        self.assert_trial_blocked(lambda store: 'ab' * 32)

    def test_plan_ref_naming_a_non_object_blob_blocks_the_proposal(self):
        self.assert_trial_blocked(lambda store: store.put_blob(['not', 'a', 'spec']))

    def test_recover_reports_a_failing_item_and_continues(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter(), broken_publication_adapter()])
        good = rt.execute_adapter('probe', {'record_id': 'a'}, trigger_id='t1')['effect']
        with self.assertRaises(TypeError):
            rt.execute_adapter('probe_bad', {'record_id': 'b'}, trigger_id='t2')
        result = rt.recover()
        self.assertEqual([(e['stage'], e['error_type']) for e in result['errors']], [('adapter', 'TypeError')])
        self.assertIn({'effect_id': good['id'], 'status': 'published'}, result['adapter_results'])
        out = rt.run_until_wait(Scripted({'discovery': none}), RES)
        self.assertEqual(out['recovery_errors'], result['errors'])
        self.assertEqual(out['status'], 'blocked_adapter')

    def test_failing_delivery_recovery_is_reported_as_blocked_recovery(self):
        store, controller, rt = open_run(self)
        discover_issue(rt, controller)
        request = {'source_id': 'src', 'source_version': 1, 'object_ids': ['o1'], 'fields': ['f'], 'query': 'q',
                   'max_pages': 1, 'max_bytes': 4096}
        executor = Scripted({'discovery': lambda d: ('propose_issue', ISSUE),
                             'disposition': lambda d: ('request_evidence', request)})
        rt.run_meta(store.list('r', 'work', kind='meta_discovery')[0]['id'], executor, RES)
        rt.run_meta(store.list('r', 'work', kind='meta_disposition')[0]['id'], executor, RES)
        request_id = store.list('r', 'request')[0]['id']
        rt.ingest_delivery(request_id, {'value': 1}, {'source': 'src', 'source_version': 1})
        def boom(*args, **kwargs):
            raise RuntimeError('boom')
        rt.ingest_delivery = boom
        out = rt.run_until_wait(Scripted({'disposition': lambda d: ('close_issue', {'resolution': 'resolved', 'reason': 'delivered'})}), RES)
        self.assertEqual([(e['stage'], e['error_type']) for e in out['recovery_errors']], [('delivery', 'RuntimeError')])
        self.assertEqual(out['status'], 'blocked_recovery')
        self.assertEqual(store.list('r', 'issue')[0]['status'], 'resolved')


class ToolRequestTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def test_rejected_inputs_are_refused_at_admission_and_lab_gets_one_repair(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong_key': 1}))})
        out = rt.run_until_wait(executor, RES, max_steps=6)
        proposals = by_seq(store.list('r', 'proposal'))
        self.assertEqual([p['status'] for p in proposals], ['rejected', 'rejected'])
        self.assertEqual(proposals[0]['error'], 'Tool inputs rejected by probe: Probe adapter requires exactly record_id')
        self.assertEqual(store.list('r', 'work', kind='diagnostic'), [])
        labs = by_seq(store.list('r', 'work', kind='lab'))
        self.assertEqual(len(labs), 2)
        self.assertEqual(labs[1]['trigger'], 'repair:' + proposals[0]['id'])
        self.assertEqual(labs[1]['inputs']['routing_error'], proposals[0]['error'])
        self.assertEqual(labs[1]['inputs']['repair_of'], proposals[0]['id'])
        self.assertEqual(executor.calls, ['lab', 'lab'])
        self.assertEqual(executor.seen[1]['task']['constraints']['previous_routing_error'], proposals[0]['error'])
        self.assertEqual(out['skipped'], [])
        store.verify_history('r')

    def test_repaired_request_executes(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': in_order(('request_diagnostic', diag({'wrong_key': 1})),
                                             ('request_diagnostic', diag({'record_id': 'a'})),
                                             none(None)),
                             'discovery': none})
        rt.run_until_wait(executor, RES, max_steps=10)
        self.assertEqual([e['status'] for e in store.list('r', 'effect', kind='probe_tool')], ['succeeded'])
        self.assertEqual(executor.calls.count('lab'), 3)
        store.verify_history('r')

    def test_execution_time_rejection_fails_the_work_and_wakes_its_issue(self):
        store, controller, rt_a = open_run(self, adapters=[lenient_adapter()])
        discover_issue(rt_a, controller)
        executor_a = Scripted({'discovery': lambda d: ('propose_issue', ISSUE),
                               'disposition': lambda d: ('request_diagnostic', diag({'wrong_key': 1}))})
        rt_a.run_meta(store.list('r', 'work', kind='meta_discovery')[0]['id'], executor_a, RES)
        admitted = rt_a.run_meta(store.list('r', 'work', kind='meta_disposition')[0]['id'], executor_a, RES)
        self.assertEqual(admitted['proposal']['status'], 'applied')
        rt_b = Runtime(store, 'r', adapters=[validating_adapter()])
        out = rt_b.run_until_wait(Scripted({'disposition': lambda d: ('close_issue', {'resolution': 'closed_unresolved', 'reason': 'tool input rejected'})}), RES, max_steps=6)
        work = store.list('r', 'work', kind='diagnostic')[0]
        self.assertEqual(work['status'], 'failed')
        self.assertEqual(json.loads(store.read_blob(work['outcome_ref'])),
                         {'reason': 'Probe adapter requires exactly record_id', 'stage': 'adapter_prepare'})
        self.assertEqual(store.get('r', 'dependency', work['inputs']['dependency_id'])['value']['status'], 'failed')
        self.assertEqual(store.list('r', 'wait')[0]['status'], 'fired')
        self.assertEqual(store.list('r', 'issue')[0]['status'], 'closed_unresolved')
        self.assertEqual(store.list('r', 'effect', kind='probe_tool'), [])
        self.assertEqual(out['skipped'], [])
        store.verify_history('r')

    def test_execution_time_rejection_of_a_lab_request_returns_to_the_lab(self):
        store, controller, rt_a = open_run(self, adapters=[lenient_adapter()])
        lab = rt_a.enqueue_lab('lab-1')
        rt_a.run_meta(lab['id'], Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong_key': 1}))}), RES)
        rt_b = Runtime(store, 'r', adapters=[validating_adapter()])
        executor_b = Scripted({'lab': none})
        rt_b.run_until_wait(executor_b, RES, max_steps=6)
        diagnostic = store.list('r', 'work', kind='diagnostic')[0]
        labs = by_seq(store.list('r', 'work', kind='lab'))
        self.assertEqual(labs[1]['trigger'], 'tool-rejected:' + diagnostic['id'])
        self.assertEqual(labs[1]['inputs']['routing_error'], 'Probe adapter requires exactly record_id')
        self.assertEqual(labs[1]['inputs']['repair_of'], diagnostic['id'])
        self.assertEqual(executor_b.seen[0]['task']['constraints']['previous_routing_error'], 'Probe adapter requires exactly record_id')
        store.verify_history('r')

    def test_one_unaffordable_work_does_not_block_other_work(self):
        budgets = {**BUDGETS, 'tool_calls': {**BUDGETS['tool_calls'], 'limit': 0}}
        store, controller, rt = open_run(self, budgets=budgets, adapters=[validating_adapter()])
        executor = Scripted({'lab': in_order(('request_diagnostic', diag({'record_id': 'a'})), none(None))})
        rt.enqueue_lab('lab-1')
        first = rt.run_until_wait(executor, RES, max_steps=6)
        diagnostic = store.list('r', 'work', kind='diagnostic')[0]
        self.assertEqual(first['skipped'], [{'work_id': diagnostic['id'], 'reason': 'Budget admission denied: tool_calls'}])
        self.assertEqual(first['status'], 'blocked_admission')
        second_lab = rt.enqueue_lab('lab-2')
        second = rt.run_until_wait(executor, RES, max_steps=6)
        self.assertEqual(store.get('r', 'work', second_lab['id'])['status'], 'succeeded')
        self.assertEqual(second['status'], 'blocked_admission')
        self.assertEqual(store.get('r', 'work', diagnostic['id'])['status'], 'ready')
        store.verify_history('r')


class PreparedEffectTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def test_prepared_tool_effect_is_resumed_after_restart(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': in_order(('request_diagnostic', diag({'record_id': 'a'})), none(None)),
                             'discovery': none})
        original = rt.controller.dispatch_effect
        def dies_before_tool_dispatch(run_id, effect_id, fn):
            if store.get(run_id, 'effect', effect_id)['kind'] == 'probe_tool':
                raise KeyboardInterrupt('simulated process death after prepare, before dispatch')
            return original(run_id, effect_id, fn)
        rt.controller.dispatch_effect = dies_before_tool_dispatch
        with self.assertRaises(KeyboardInterrupt):
            rt.run_until_wait(executor, RES, max_steps=6)
        self.assertEqual(store.list('r', 'effect', kind='probe_tool')[0]['status'], 'prepared')
        restarted = Runtime(store, 'r', adapters=[validating_adapter()])
        out = restarted.run_until_wait(executor, RES, max_steps=6)
        effect = store.list('r', 'effect', kind='probe_tool')[0]
        self.assertEqual(effect['status'], 'succeeded')
        self.assertEqual(len(EXECUTIONS), 1)
        self.assertEqual(restarted.pending_adapter_publications(), [])
        self.assertEqual(executor.calls.count('lab'), 2)            # the continuation ran
        self.assertEqual(out['skipped'], [])
        store.verify_history('r')

    def test_prepared_tool_effect_is_cancelled_when_its_capability_is_no_longer_admitted(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': in_order(('request_diagnostic', diag({'record_id': 'a'})), none(None)),
                             'discovery': none})
        original = rt.controller.dispatch_effect
        def dies_before_tool_dispatch(run_id, effect_id, fn):
            if store.get(run_id, 'effect', effect_id)['kind'] == 'probe_tool':
                raise KeyboardInterrupt('simulated process death after prepare, before dispatch')
            return original(run_id, effect_id, fn)
        rt.controller.dispatch_effect = dies_before_tool_dispatch
        with self.assertRaises(KeyboardInterrupt):
            rt.run_until_wait(executor, RES, max_steps=6)
        self.assertEqual(store.list('r', 'effect', kind='probe_tool')[0]['status'], 'prepared')
        store.mutate('r', 'other-version', 'capability_recorded', lambda tx: tx.insert('capability', 'probe:2',
            {'status': 'verified', 'capability_id': 'probe', 'capability_version': '2', 'artifact_ref': 'x'}), payload={})
        restarted = Runtime(store, 'r', adapters=[validating_adapter()])
        out = restarted.run_until_wait(executor, RES, max_steps=6)
        effect = store.list('r', 'effect', kind='probe_tool')[0]
        self.assertEqual([c['effect_id'] for c in out['cancelled_prepared']], [effect['id']])
        self.assertIn('accepted artifact', out['cancelled_prepared'][0]['reason'])
        self.assertEqual((effect['status'], effect['never_dispatched']), ('failed_definite', True))
        self.assertEqual(EXECUTIONS, [])
        store.verify_history('r')

    def test_effect_that_already_left_prepared_is_never_resumed(self):
        store, controller, rt = open_run(self, adapters=[dying_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: ('request_diagnostic', diag({'record_id': 'a'}, 'probe_dying'))})
        with self.assertRaises(KeyboardInterrupt):
            rt.run_until_wait(executor, RES, max_steps=6)
        restarted = Runtime(store, 'r', adapters=[dying_adapter()])
        for _ in range(2):
            self.assertEqual(restarted.run_until_wait(executor, RES)['status'], 'blocked_external')
        self.assertEqual(len(EXECUTIONS), 1)
        self.assertEqual(store.list('r', 'effect', kind='probe_dying_tool')[0]['status'], 'unknown')
        store.verify_history('r')

    def test_stale_prepared_agent_effect_is_cancelled_and_reported(self):
        store, controller, rt = open_run(self)
        discover_issue(rt, controller)
        rt.run_meta(store.list('r', 'work', kind='meta_discovery')[0]['id'],
                    Scripted({'discovery': lambda d: ('propose_issue', ISSUE)}), RES)
        work = [w for w in store.list('r', 'work', kind='meta_disposition') if w['status'] == 'ready'][0]
        effect = rt.prepare_meta(work['id'], RES)
        self.assertEqual(effect['status'], 'prepared')
        controller.add_fact('r', 'f2', 'raw_fact', {'y': 2}, {'source': 'fixture'}, 'k2')
        controller.open_issue('r', 'unused', [ISSUE['problem_kind'], ISSUE['scope'], ISSUE['title']], ISSUE['hypothesis'],
                              ISSUE['scope'], ['f2'], ISSUE['competing_explanations'], ['check', 'falsifier'], 'more-evidence')
        out = Runtime(store, 'r').run_until_wait(Scripted({'disposition': lambda d: ('close_issue', {'resolution': 'closed_unresolved', 'reason': 'n/a'})}), RES, max_steps=6)
        self.assertEqual([c['effect_id'] for c in out['cancelled_prepared']], [effect['id']])
        self.assertIn('stale', out['cancelled_prepared'][0]['reason'])
        cancelled = store.get('r', 'effect', effect['id'])
        self.assertEqual((cancelled['status'], cancelled['never_dispatched']), ('failed_definite', True))
        self.assertTrue(all(b['reserved'] == 0 for b in store.list('r', 'budget')))
        self.assertEqual(store.list('r', 'issue')[0]['status'], 'closed_unresolved')
        self.assertEqual(store.list('r', 'agent_session', role='meta')[0]['status'], 'idle')
        store.verify_history('r')

    def test_prepared_agent_effect_is_resumed_with_its_recorded_reservation(self):
        store, controller, rt = open_run(self)
        work = rt.enqueue_lab('lab-1')
        effect = rt.prepare_meta(work['id'], RES)
        self.assertEqual(effect['status'], 'prepared')
        executor = Scripted({'lab': none})
        out = Runtime(store, 'r').run_until_wait(executor, {'model_calls': 0, 'tokens': 0, 'wall_seconds': 2})
        self.assertEqual(out['cancelled_prepared'], [])
        self.assertEqual(store.get('r', 'effect', effect['id'])['status'], 'succeeded')
        self.assertEqual(store.get('r', 'work', work['id'])['status'], 'succeeded')
        self.assertEqual(executor.calls, ['lab'])
        store.verify_history('r')

    def test_prepared_effect_the_driver_cannot_execute_is_reported(self):
        store, controller, rt = open_run(self)
        work = controller.enqueue('r', 'diagnostic', 'host-tool', 'w1')
        effect = controller.prepare_effect('r', work['id'], 'host_tool', {'x': 1}, {'tool_calls': 1}, 'p1')
        lab = rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none})
        out = rt.run_until_wait(executor, RES)
        self.assertEqual(out['status'], 'blocked_prepared')
        self.assertEqual([(p['effect_id'], p['kind']) for p in out['prepared_blocking']], [(effect['id'], 'host_tool')])
        self.assertEqual(executor.calls, [])
        self.assertEqual(store.get('r', 'work', lab['id'])['status'], 'ready')
        self.assertEqual(store.get('r', 'effect', effect['id'])['status'], 'prepared')
        store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
