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
from tests.v2.liveness_support import (BUDGETS, EXECUTIONS, RES, Scripted, by_seq, diag, none, one_fact, one_tool_call,
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


if __name__ == '__main__':
    unittest.main()
