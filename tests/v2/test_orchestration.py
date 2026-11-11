"""Meta-driven Lab orchestration: no-result signal, Lab settings and code settlement.

Every role response is scripted; nothing here calls a model or the network.
"""
import json
import os
import shutil
import tempfile
import unittest

from rsi_lab.adapters import Adapter
from rsi_lab.orchestration import orchestration_config
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from tests.v2.liveness_support import (BUDGETS, EXECUTIONS, ISSUE, RES, Scripted, by_seq, diag, none, one_fact, one_tool_call,
                                       open_run, strict_prepare, validating_adapter)

N = 2
ORCH = {'orchestration': {'no_result_steps': N, 'max_changes': 2}}


def fail_execution(effect, packet_bytes):
    return {'status': 'failed_definite', 'actual_costs': {'tool_calls': 1}, 'error': 'probe exploded'}


def failing_adapter():
    return Adapter(adapter_id='probe_fail', version='1', effect_kind='probe_fail_tool', manifest={'scope': 'probe'},
                   prepare=strict_prepare, reservations=one_tool_call, execute=fail_execution, publication=one_fact)


def second_execution(effect, packet_bytes):
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1}, 'result': json.loads(packet_bytes)}


def second_adapter():
    return Adapter(adapter_id='probe2', version='1', effect_kind='probe2_tool', manifest={'scope': 'probe'},
                   prepare=strict_prepare, reservations=one_tool_call, execute=second_execution, publication=one_fact)


def in_order(*answers):
    """Answer successive calls of one mode in order, repeating the last answer."""
    calls = []
    def answer(data):
        calls.append(data)
        return answers[min(len(calls), len(answers)) - 1]
    return answer


def record(n):
    return 'request_diagnostic', diag({'record_id': n})


def outcomes(rt):
    return [(s['outcome'], s['reason_code']) for s in rt.orchestration.steps()]


class ConfigTests(unittest.TestCase):
    def test_valid_config_is_normalized(self):
        self.assertIsNone(orchestration_config({}))
        self.assertEqual(orchestration_config(ORCH), {'no_result_steps': 2, 'max_changes': 2})
        self.assertEqual(orchestration_config({'orchestration': {'no_result_steps': 64, 'max_changes': 0}}),
                         {'no_result_steps': 64, 'max_changes': 0})

    def test_missing_extra_and_out_of_range_values_are_refused(self):
        bad = [None, [], {}, {'no_result_steps': 4}, {'max_changes': 1},
               {'no_result_steps': 4, 'max_changes': 1, 'interval': 3},
               {'no_result_steps': 1, 'max_changes': 1}, {'no_result_steps': 65, 'max_changes': 1},
               {'no_result_steps': 4, 'max_changes': -1}, {'no_result_steps': 4, 'max_changes': 21},
               {'no_result_steps': True, 'max_changes': 1}, {'no_result_steps': 4.0, 'max_changes': 1},
               {'no_result_steps': 4, 'max_changes': '1'}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                orchestration_config({'orchestration': value})

    def test_runtime_refuses_an_invalid_stored_config(self):
        with self.assertRaises(ValueError):
            open_run(self, config={'orchestration': {'no_result_steps': 4}})

    def test_runtime_without_orchestration_has_none(self):
        store, controller, rt = open_run(self)
        self.assertIsNone(rt.orchestration)
        store.verify_history('r')

    def test_init_refuses_an_invalid_orchestration_before_creating_the_run(self):
        from rsi_lab.__main__ import main
        root = tempfile.mkdtemp(prefix='rsi-orch-init-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = os.path.join(root, 'config.json')
        with open(path, 'w') as stream:
            json.dump({'budgets': BUDGETS, 'orchestration': {'no_result_steps': 4, 'max_changes': 99}}, stream)
        with self.assertRaises(ValueError):
            main(['--root', os.path.join(root, 'ledger'), '--run', 'r', 'init', '--config', path])
        store = Store(os.path.join(root, 'ledger'))
        self.addCleanup(store.close)
        with self.assertRaises(KeyError):
            store.run('r')


class StepOutcomeTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def lab(self, rt, executor, steps):
        rt.enqueue_lab('lab-1')
        for _ in range(steps):
            work = next(w for w in rt.controller.ready('r') if w['kind'] in {'lab', 'diagnostic'})
            if work['kind'] == 'lab':
                rt.run_lab(work['id'], executor, RES)
            else:
                rt.execute_adapter(work['inputs']['capability_id'], json.loads(work['inputs']['inputs_json']),
                                   trigger_id=work['id'], work_id=work['id'])

    def test_new_result_then_repeated_content(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        self.lab(rt, Scripted({'lab': lambda d: record('a')}), 4)
        self.assertEqual(outcomes(rt), [('new_result', 'new_result'), ('no_result', 'repeated_result')])
        steps = rt.orchestration.steps()
        self.assertEqual(steps[1]['record']['action'], 'request_diagnostic')
        self.assertEqual(steps[1]['record']['capability_id'], 'probe')
        self.assertEqual(steps[1]['record']['inputs_excerpt'], '{"record_id": "a"}')
        period = rt.orchestration.period(steps)
        self.assertEqual([s['effect_id'] for s in period['steps']], [steps[1]['effect_id']])
        store.verify_history('r')

    def test_a_new_result_resets_the_period(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        self.lab(rt, Scripted({'lab': in_order(record('a'), record('a'), record('b'))}), 6)
        self.assertEqual([o for o, _ in outcomes(rt)], ['new_result', 'no_result', 'new_result'])
        self.assertIsNone(rt.orchestration.period(rt.orchestration.steps()))
        store.verify_history('r')

    def test_rejected_proposal_and_stop(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        self.lab(rt, Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong': 1}))}), 2)
        self.assertEqual(outcomes(rt), [('no_result', 'proposal_rejected')] * 2)
        steps = rt.orchestration.steps()
        self.assertIn('Probe adapter requires exactly record_id', steps[0]['record']['reason'])
        self.assertEqual(len(rt.orchestration.period(steps)['steps']), 2)
        self.assertTrue(rt.orchestration.stopped(steps))
        store.verify_history('r')

    def test_one_rejection_with_a_pending_repair_is_not_a_stop(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        self.lab(rt, Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong': 1}))}), 1)
        self.assertFalse(rt.orchestration.stopped(rt.orchestration.steps()))
        store.verify_history('r')

    def test_tool_rejecting_its_input(self):
        from tests.v2.liveness_support import lenient_adapter
        store, controller, rt = open_run(self, config=ORCH, adapters=[lenient_adapter()])
        rt.enqueue_lab('lab-1')
        work = rt.controller.ready('r')[0]
        rt.run_lab(work['id'], Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong': 1}))}), RES)
        strict = Runtime(store, 'r', adapters=[validating_adapter()])
        diagnostic = store.list('r', 'work', kind='diagnostic')[0]
        strict.execute_adapter('probe', {'wrong': 1}, trigger_id=diagnostic['id'], work_id=diagnostic['id'])
        self.assertEqual(outcomes(strict), [('no_result', 'tool_rejected')])
        self.assertFalse(strict.orchestration.stopped(strict.orchestration.steps()))
        store.verify_history('r')

    def test_tool_failing_definitely_is_a_stop(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[failing_adapter()])
        self.lab(rt, Scripted({'lab': lambda d: ('request_diagnostic', diag({'record_id': 'a'}, 'probe_fail'))}), 2)
        self.assertEqual(outcomes(rt), [('no_result', 'tool_failed')])
        self.assertIn('probe exploded', rt.orchestration.steps()[0]['record']['reason'])
        self.assertTrue(rt.orchestration.stopped(rt.orchestration.steps()))
        store.verify_history('r')

    def test_no_action_is_no_result_but_not_a_stop(self):
        store, controller, rt = open_run(self, config=ORCH)
        self.lab(rt, Scripted({'lab': none}), 1)
        self.assertEqual(outcomes(rt), [('no_result', 'no_action')])
        self.assertFalse(rt.orchestration.stopped(rt.orchestration.steps()))
        store.verify_history('r')

    def test_pending_tool_call_is_unfinished(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        self.lab(rt, Scripted({'lab': lambda d: record('a')}), 1)
        self.assertEqual(outcomes(rt), [('unfinished', None)])
        self.assertFalse(rt.orchestration.stopped(rt.orchestration.steps()))
        store.verify_history('r')

    def test_model_failure_and_final_review_are_not_steps(self):
        def failed(receipt, index):
            return {**receipt, 'status': 'failed_definite', 'response': None, 'error': 'provider down'}
        store, controller, rt = open_run(self, config=ORCH)
        self.lab(rt, Scripted({'lab': none}, receipt=failed), 1)
        self.assertEqual(store.list('r', 'effect', kind='lab')[0]['status'], 'failed_definite')
        self.assertEqual(rt.orchestration.steps(), [])
        controller.enqueue('r', 'lab', 'final', 'final', inputs={'evidence_ids': [], 'final_review_only': True})
        rt.run_lab(rt.controller.ready('r')[0]['id'], Scripted({'lab': none}), RES)
        self.assertEqual(rt.orchestration.steps(), [])
        store.verify_history('r')



def is_signal(data):
    return any('controller_orchestration_signal' in page['source'] for page in data['delivered_pages'])


def discover(data):
    return ('propose_issue', ISSUE) if is_signal(data) else none(data)


CLOSE = ('close_issue', {'resolution': 'closed_unresolved', 'reason': 'not pursued'})


def signals(store):
    return [f for f in store.list('r', 'fact') if f['provenance'].get('source') == 'controller_orchestration_signal']


def signal_discoveries(store):
    return [w for w in store.list('r', 'work', kind='meta_discovery') if w['trigger'].startswith('event:orchestration:')]


class SignalTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def test_n_steps_without_a_new_result_write_one_fact_and_wake_discovery(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: record('a'), 'discovery': none})
        rt.run_until_wait(executor, RES, max_steps=14)
        self.assertEqual([o for o, _ in outcomes(rt)][:3], ['new_result', 'no_result', 'no_result'])
        facts = signals(store)
        self.assertEqual(len(facts), 1)
        content = json.loads(store.read_blob(facts[0]['content_ref']))
        self.assertEqual(content['reason'], 'no_result_streak')
        self.assertEqual(content['no_result_steps'], N)
        self.assertIsNone(content['active_change_id'])
        self.assertEqual([r['outcome'] for r in content['steps']], ['no_result', 'no_result'])
        self.assertEqual(facts[0]['kind'], 'raw_fact')
        work = signal_discoveries(store)
        self.assertEqual(len(work), 1)
        self.assertEqual(work[0]['inputs']['evidence_ids'], [facts[0]['id']])
        seen = [d for d in executor.seen if d['mode'] == 'discovery' and is_signal(d)]
        self.assertEqual(len(seen), 1)
        self.assertIn(facts[0]['id'], seen[0]['required_evidence_ids'])
        store.verify_history('r')

    def test_fewer_than_n_steps_do_not_signal(self):
        store, controller, rt = open_run(self, config={'orchestration': {'no_result_steps': 3, 'max_changes': 1}},
                                         adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(Scripted({'lab': in_order(record('a'), record('a'), record('a'), none(None)),
                                    'discovery': none}), RES, max_steps=20)
        self.assertEqual([o for o, _ in outcomes(rt)], ['new_result', 'no_result', 'no_result', 'no_result'])
        self.assertEqual(len(signals(store)), 1)
        store2, controller2, rt2 = open_run(self, config={'orchestration': {'no_result_steps': 3, 'max_changes': 1}},
                                            adapters=[validating_adapter()], run_id='r')
        rt2.enqueue_lab('lab-1')
        rt2.run_until_wait(Scripted({'lab': in_order(record('a'), record('a'), none(None)),
                                     'discovery': none}), RES, max_steps=20)
        self.assertEqual([o for o, _ in outcomes(rt2)], ['new_result', 'no_result', 'no_result'])
        self.assertEqual(signals(store2), [])
        store.verify_history('r')
        store2.verify_history('r')

    def test_a_new_result_in_between_restarts_the_count(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(Scripted({'lab': in_order(record('a'), record('a'), record('b'), record('b'), none(None)),
                                    'discovery': none}), RES, max_steps=20)
        self.assertEqual([o for o, _ in outcomes(rt)], ['new_result', 'no_result', 'new_result', 'no_result', 'no_result'])
        facts = signals(store)
        self.assertEqual(len(facts), 1)
        self.assertEqual(len(json.loads(store.read_blob(facts[0]['content_ref']))['steps']), 2)
        store.verify_history('r')

    def test_two_rejections_wake_discovery_in_the_same_driver_call(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong': 1})), 'discovery': none})
        rt.run_until_wait(executor, RES, max_steps=10)
        self.assertEqual(executor.calls, ['lab', 'lab', 'discovery'])
        content = json.loads(store.read_blob(signals(store)[0]['content_ref']))
        self.assertEqual(content['reason'], 'lab_stopped')
        self.assertIn('Probe adapter requires exactly record_id', content['steps'][0]['reason'])
        store.verify_history('r')

    def test_a_definite_tool_failure_wakes_discovery(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[failing_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: ('request_diagnostic', diag({'record_id': 'a'}, 'probe_fail')),
                             'discovery': none})
        rt.run_until_wait(executor, RES, max_steps=10)
        self.assertEqual(executor.calls, ['lab', 'discovery'])
        self.assertEqual(json.loads(store.read_blob(signals(store)[0]['content_ref']))['reason'], 'lab_stopped')
        store.verify_history('r')

    def test_no_action_model_failure_and_capacity_block_do_not_signal(self):
        store, controller, rt = open_run(self, config=ORCH)
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(Scripted({'lab': none}), RES, max_steps=5)
        self.assertEqual(signals(store), [])

        def fail_second(receipt, index):
            return receipt if index == 0 else {**receipt, 'status': 'failed_definite', 'response': None}
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong': 1}))},
                                   receipt=fail_second), RES, max_steps=5)
        self.assertEqual([w['status'] for w in by_seq(store.list('r', 'work', kind='lab'))], ['succeeded', 'failed'])
        self.assertEqual(signals(store), [])

        from rsi_lab.packets import PacketBuilder
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        rt.run_meta(rt.controller.ready('r')[0]['id'],
                    Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong': 1}))}), RES)
        rt.packet_builder_factory = lambda catalog, max_input_bytes: PacketBuilder(catalog, max_input_bytes=300)
        out = rt.run_until_wait(Scripted({}), RES, max_steps=5)
        self.assertEqual(out['report']['attention']['capacity_blocked_work'][0]['kind'], 'lab')
        self.assertEqual(signals(store), [])
        store.verify_history('r')

    def test_repeated_advance_writes_nothing_and_a_later_period_signals_again(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        lab = in_order(record('a'), record('a'), record('a'), record('b'), record('b'), record('b'), none(None))
        rt.run_until_wait(Scripted({'lab': lab, 'discovery': none}), RES, max_steps=9)
        self.assertEqual(len(signals(store)), 1)
        events = store.verify_history('r')['events']
        rt.orchestration.advance()
        rt.orchestration.advance()
        self.assertEqual(store.verify_history('r')['events'], events)
        rt.run_until_wait(Scripted({'lab': lab, 'discovery': none}), RES, max_steps=30)
        self.assertEqual(len(signals(store)), 2)
        self.assertEqual(len(signal_discoveries(store)), 2)
        store.verify_history('r')

    def test_an_unknown_lab_call_defers_judgment_until_reconciled(self):
        saved = {}
        def unknown_second(receipt, index):
            if index == 1:
                saved['receipt'] = receipt
                return {'status': 'unknown', 'actual_costs': {}}
            return receipt
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong': 1})), 'discovery': none},
                            receipt=unknown_second)
        rt.run_until_wait(executor, RES, max_steps=5)
        self.assertEqual(outcomes(rt), [('no_result', 'proposal_rejected'), ('unfinished', None)])
        self.assertEqual(signals(store), [])
        effect = by_seq(store.list('r', 'effect', kind='lab'))[1]
        controller.reconcile('r', effect['id'], {**saved['receipt'], 'reconciliation': {
            'effect_id': effect['id'], 'provenance': 'operator receipt'}}, 'operator:' + effect['id'])
        rt.run_until_wait(executor, RES, max_steps=5)
        self.assertEqual(outcomes(rt), [('no_result', 'proposal_rejected')] * 2)
        self.assertEqual(json.loads(store.read_blob(signals(store)[0]['content_ref']))['reason'], 'lab_stopped')
        self.assertEqual(executor.calls[-1], 'discovery')
        store.verify_history('r')

    def test_signal_is_required_evidence_in_discovery_and_disposition(self):
        store, controller, rt = open_run(self, config={**ORCH, 'max_input_bytes': 24000},
                                         adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: ('request_diagnostic', diag({'wrong': 1})), 'discovery': discover,
                             'disposition': lambda d: CLOSE})
        rt.run_until_wait(executor, RES, max_steps=3)
        fid = signals(store)[0]['id']
        issue = store.list('r', 'issue')[0]
        big = []
        for i in range(6):
            controller.add_fact('r', 'big%d' % i, 'raw_fact', {'text': 'x' * 6000}, {'source': 'bulk'}, 'big%d' % i)
            big.append('big%d' % i)
        controller.open_issue('r', 'again', issue['semantic_key'], ISSUE['hypothesis'], ISSUE['scope'], big,
                              ISSUE['competing_explanations'], ['c', 'f'], 'again')
        rt.run_until_wait(executor, RES, max_steps=3)
        disposition = [d for d in executor.seen if d['mode'] == 'disposition'][-1]
        self.assertIn(fid, disposition['required_evidence_ids'])
        pages = {p['evidence_id']: p for p in disposition['delivered_pages']}
        self.assertTrue(pages[fid]['complete_source'])
        self.assertTrue(set(big) - set(pages))
        discovery = [d for d in executor.seen if d['mode'] == 'discovery'][0]
        self.assertIn(fid, discovery['required_evidence_ids'])
        store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
