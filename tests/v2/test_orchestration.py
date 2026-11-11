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



INITIAL = {'guidance': None, 'step_digest': 0, 'recent_results': 0, 'disabled_tools': []}


def install(store, settings, *, status='kept', change_id='change-1'):
    """Record a change directly; the proposal path is exercised by the action tests."""
    document = {'kind': 'orchestration_change', 'status': status, 'settings': {**INITIAL, **settings}}
    return store.mutate('r', 'test-install:' + change_id, 'test_change_installed',
                        lambda tx: tx.insert('registration', change_id, document), document)


def set_status(store, change_id, status):
    return store.mutate('r', 'test-status:' + change_id + ':' + status, 'test_change_status',
                        lambda tx: tx.update('registration', change_id, {'status': status}), {'status': status})


def lab_packets(executor):
    return [d for d in executor.seen if d['mode'] == 'lab']


class UnchangedWithoutOrchestrationTests(unittest.TestCase):
    def test_packets_schema_and_effect_records_match_the_baseline(self):
        from rsi_lab import roles
        store, controller, rt = open_run(self)
        controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
        rt.enqueue_lab('lab-1', ['f1'])
        executor = Scripted({'lab': lambda d: ('propose_issue', ISSUE),
                             'disposition': lambda d: CLOSE})
        rt.run_until_wait(executor, RES, max_steps=12)
        effects = by_seq(store.list('r', 'effect'))
        # Identities computed on the baseline commit before this feature.
        self.assertEqual([e['packet_hash'] for e in effects],
                         ['9a35bfbd5afd8f2726a8e7889e081c4c4bb3e7a28f2419c404c7d9b8fc336f1d',
                          'c2a56d33a9d971086c6194d69e6a91fb2f525c0b3eca6c94463a858b5b17230e'])
        for effect in effects:
            self.assertNotIn('orchestration', effect['metadata'])
        for data in executor.seen:
            self.assertNotIn('orchestration', data['task']['constraints'])
        self.assertNotIn('propose_lab_settings', json.dumps(roles.response_schema('disposition', working_memory=True)))
        store.verify_history('r')


class LabSettingsTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def test_initial_settings_change_nothing_but_the_orchestration_block(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none})
        rt.run_until_wait(executor, RES, max_steps=2)
        data = lab_packets(executor)[0]
        self.assertEqual(data['task']['constraints']['orchestration'], {'guidance': None, 'recent_steps': []})
        self.assertIn('probe', data['task']['capability_catalog'])
        effect = store.list('r', 'effect', kind='lab')[0]
        self.assertIsNone(effect['metadata']['orchestration']['change_id'])
        store.verify_history('r')

    def test_guidance_and_instruction_reach_later_lab_requests(self):
        store, controller, rt = open_run(self, config=ORCH)
        install(store, {'guidance': 'Use record ids from the unit map.'})
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none})
        rt.run_until_wait(executor, RES, max_steps=2)
        self.assertEqual(lab_packets(executor)[0]['task']['constraints']['orchestration']['guidance'],
                         'Use record ids from the unit map.')
        effect = store.list('r', 'effect', kind='lab')[0]
        self.assertEqual(effect['metadata']['orchestration']['change_id'], 'change-1')
        prompt = json.loads(store.read_blob(effect['packet_ref']))['prompt']
        self.assertIn('Only the guidance in THIS request is in force', prompt)
        store.verify_history('r')

    def test_step_digest_gives_that_many_records(self):
        store, controller, rt = open_run(self, config={'orchestration': {'no_result_steps': 8, 'max_changes': 1}},
                                         adapters=[validating_adapter()])
        install(store, {'step_digest': 2})
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': in_order(record('a'), record('b'), record('c'), none(None)), 'discovery': none})
        rt.run_until_wait(executor, RES, max_steps=20)
        packets = lab_packets(executor)
        self.assertEqual([len(p['task']['constraints']['orchestration']['recent_steps']) for p in packets], [0, 1, 2, 2])
        last = packets[-1]['task']['constraints']['orchestration']['recent_steps']
        self.assertEqual([r['inputs_excerpt'] for r in last], ['{"record_id": "b"}', '{"record_id": "c"}'])
        self.assertEqual([r['outcome'] for r in last], ['new_result', 'new_result'])
        store.verify_history('r')

    def test_recent_results_are_optional_evidence(self):
        store, controller, rt = open_run(self, config={'orchestration': {'no_result_steps': 8, 'max_changes': 1}},
                                         adapters=[validating_adapter()])
        install(store, {'recent_results': 2})
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': in_order(record('a'), record('b'), record('c'), none(None)), 'discovery': none})
        rt.run_until_wait(executor, RES, max_steps=20)
        published = [r['id'] for r in by_seq(store.list('r', 'fact'))]
        last = lab_packets(executor)[-1]
        delivered = [p['evidence_id'] for p in last['delivered_pages']]
        self.assertEqual(set(published[-2:]) - set(delivered), set())
        self.assertNotIn(published[0], delivered)
        self.assertTrue(set(published[-2:]) - set(last['required_evidence_ids']))
        store.verify_history('r')

    def test_recent_results_are_paged_or_omitted_under_small_capacity(self):
        store, controller, rt = open_run(self, config={'orchestration': {'no_result_steps': 8, 'max_changes': 1},
                                                       'max_input_bytes': 20000})
        for i in range(6):
            controller.add_fact('r', 'big%d' % i, 'raw_fact', {'text': 'x' * 6000}, {'source': 'bulk'}, 'big%d' % i)
            store.mutate('r', 'pub%d' % i, 'test_publication', lambda tx, i=i: tx.insert(
                'registration', 'pub%d' % i, {'kind': 'adapter_publication', 'status': 'complete',
                                              'publication_ref': tx.blob({'facts': [{'id': 'big%d' % i}],
                                                                          'conclusions': [], 'data': None})}), {})
        install(store, {'recent_results': 6})
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': none})
        out = rt.run_until_wait(executor, RES, max_steps=2)
        self.assertEqual(executor.calls, ['lab'])
        data = lab_packets(executor)[0]
        delivered = {p['evidence_id']: p for p in data['delivered_pages']}
        self.assertTrue(any(not p['complete_source'] for p in delivered.values())
                        or len(delivered) < 6)
        self.assertEqual(out['report']['attention']['capacity_blocked_work'], [])
        store.verify_history('r')

    def test_disabled_tool_leaves_the_lab_catalog_and_its_request_is_refused(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter(), second_adapter()])
        install(store, {'disabled_tools': ['probe']})
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: record('a'), 'discovery': none})
        rt.run_until_wait(executor, RES, max_steps=3)
        data = lab_packets(executor)[0]
        self.assertNotIn('probe', data['task']['capability_catalog'])
        self.assertIn('probe2', data['task']['capability_catalog'])
        proposal = by_seq(store.list('r', 'proposal'))[0]
        self.assertEqual(proposal['status'], 'rejected')
        self.assertEqual(proposal['error'], 'Diagnostic requires an accepted registered capability version')
        self.assertEqual(store.list('r', 'work', kind='diagnostic'), [])
        store.verify_history('r')

    def test_meta_requests_and_queued_calls_are_not_affected(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        rt.run_lab(rt.controller.ready('r')[0]['id'], Scripted({'lab': lambda d: record('a')}), RES)
        install(store, {'disabled_tools': ['probe']})
        controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
        controller.open_issue('r', 'i1', ['anomaly', 's', 't'], 'h', 's', ['f1'], ['alt'], ['c', 'f'], 'i1')
        rt.run_until_wait(Scripted({'disposition': in_order(record('m'), CLOSE), 'lab': none, 'discovery': none}),
                          RES, max_steps=10)
        tools = by_seq(store.list('r', 'effect', kind='probe_tool'))
        self.assertEqual([e['status'] for e in tools], ['succeeded', 'succeeded'])
        disposition = [p for p in store.list('r', 'proposal') if p['command'] and p['command']['mode'] == 'disposition']
        self.assertEqual([p['status'] for p in disposition], ['applied', 'applied'])
        store.verify_history('r')

    def test_a_lab_proposal_is_checked_against_the_settings_its_effect_recorded(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        install(store, {'disabled_tools': ['probe']}, status='trial')
        lab = rt.enqueue_lab('lab-1')
        rt.prepare_meta(lab['id'], RES)
        set_status(store, 'change-1', 'reverted')
        result = rt.run_lab(lab['id'], Scripted({'lab': lambda d: record('a')}), RES)
        self.assertEqual(result['proposal']['status'], 'rejected')
        self.assertEqual(store.list('r', 'effect', kind='lab')[0]['metadata']['orchestration']['change_id'], 'change-1')
        self.assertIn('probe', rt.orchestration.lab_catalog(rt.capability_catalog(), None))
        store.verify_history('r')



def reject(data):
    return 'request_diagnostic', diag({'wrong': 1})


def settings(**values):
    return {**INITIAL, 'rationale': 'Lab keeps sending the wrong input field', **values}


GUIDE = settings(guidance='Send exactly {"record_id": ...} to probe version 1.', step_digest=4)


def run_next(rt, kinds, executor):
    """Run the first ready Work of the given kinds; tools run through their loaded adapter."""
    work = next(w for w in rt.controller.ready('r') if w['kind'] in kinds)
    if work['kind'] == 'diagnostic':
        return rt.execute_adapter(work['inputs']['capability_id'], json.loads(work['inputs']['inputs_json']),
                                  trigger_id=work['id'], work_id=work['id'])
    return rt.run_meta(work['id'], executor, RES)


def stopped_run(test, config=ORCH, adapters=None):
    """A Lab chain stopped by two rejections; its signal opened an issue whose disposition is ready."""
    store, controller, rt = open_run(test, config=config, adapters=adapters or [validating_adapter()])
    rt.enqueue_lab('lab-1')
    rt.run_until_wait(Scripted({'lab': reject, 'discovery': discover}), RES, max_steps=3)
    issue = store.list('r', 'issue')[0]
    work = [w for w in rt.controller.ready('r') if w['kind'] == 'meta_disposition']
    test.assertEqual(len(work), 1)
    return store, controller, rt, issue, work[0]


def propose(rt, work, payload, *, receipt=None):
    return rt.run_meta(work['id'], Scripted({'disposition': lambda d: ('propose_lab_settings', payload)},
                                            receipt=receipt), RES)['proposal']


def changes(store):
    return by_seq(store.list('r', 'registration', kind='orchestration_change'))


class ActionTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def test_action_appears_only_when_changes_are_allowed(self):
        from rsi_lab import roles
        store, controller, rt, issue, work = stopped_run(self, config={'orchestration': {'no_result_steps': 2,
                                                                                          'max_changes': 0}})
        effect = rt.prepare_meta(work['id'], RES)
        envelope = json.loads(store.read_blob(effect['packet_ref']))
        self.assertNotIn('propose_lab_settings', json.dumps(envelope['response_schema']))
        self.assertEqual(effect['metadata']['orchestration'], {'lab_settings_action': False})
        proposal = rt.run_meta(work['id'], Scripted({'disposition': lambda d: ('propose_lab_settings', GUIDE)}),
                               effect['reservations'])['proposal']
        self.assertEqual(proposal['status'], 'rejected')
        self.assertEqual(changes(store), [])
        for mode in ('lab', 'discovery'):
            self.assertEqual(roles.response_schema(mode, working_memory=True, lab_settings=True),
                             roles.response_schema(mode, working_memory=True))
        enabled = roles.response_schema('disposition', working_memory=True, lab_settings=True)
        self.assertIn('propose_lab_settings', json.dumps(enabled))
        self.assertEqual(roles.response_schema('disposition', final_review_only=True, lab_settings=True),
                         roles.response_schema('disposition', final_review_only=True))
        store.verify_history('r')

    def test_meta_sees_settings_limits_and_the_current_signal(self):
        store, controller, rt, issue, work = stopped_run(self)
        executor = Scripted({'disposition': lambda d: CLOSE})
        rt.run_meta(work['id'], executor, RES)
        constraints = executor.seen[0]['task']['constraints']['orchestration']
        self.assertEqual(constraints['settings'], INITIAL)
        self.assertEqual(constraints['signal_fact_id'], signals(store)[0]['id'])
        self.assertEqual(constraints['changes_remaining'], 2)
        self.assertIsNone(constraints['trial_change'])
        self.assertEqual(constraints['limits']['guidance_chars'], 2000)
        prompt = json.loads(store.read_blob(store.list('r', 'effect', kind='meta')[-1]['packet_ref']))['prompt']
        self.assertIn('not a diagnosis', prompt)
        self.assertIn('propose_lab_settings', prompt)
        store.verify_history('r')

    def test_out_of_range_unknown_tool_and_unchanged_settings_are_rejected(self):
        bad = {'guidance too long': settings(guidance='g' * 2001),
               'digest too large': settings(step_digest=17),
               'results too large': settings(recent_results=17),
               'too many tools': settings(disabled_tools=['t%d' % i for i in range(9)]),
               'repeated tool': settings(disabled_tools=['probe', 'probe']),
               'unknown tool': settings(disabled_tools=['nope']),
               'unchanged': settings()}
        for name, payload in bad.items():
            with self.subTest(name):
                store, controller, rt, issue, work = stopped_run(self)
                proposal = propose(rt, work, payload)
                self.assertEqual(proposal['status'], 'rejected')
                self.assertEqual(changes(store), [])
                store.verify_history('r')

    def test_a_command_must_cite_the_current_signal(self):
        store, controller, rt, issue, work = stopped_run(self)
        def uncited(receipt, index):
            receipt['response']['command']['evidence_ids'] = []
            return receipt
        proposal = propose(rt, work, GUIDE, receipt=uncited)
        self.assertEqual(proposal['status'], 'rejected')
        self.assertIn('current orchestration signal', proposal['error'])
        store.verify_history('r')

    def test_a_signal_expires_once_lab_produces_a_new_result(self):
        store, controller, rt, issue, work = stopped_run(self)
        rt.enqueue_lab('lab-2')
        run_next(rt, {'lab'}, Scripted({'lab': lambda d: record('fresh')}))
        run_next(rt, {'diagnostic'}, None)
        self.assertEqual(outcomes(rt)[-1], ('new_result', 'new_result'))
        proposal = propose(rt, work, GUIDE)
        self.assertEqual(proposal['status'], 'rejected')
        self.assertIn('current orchestration signal', proposal['error'])
        store.verify_history('r')

    def test_one_change_at_a_time_and_at_most_max_changes(self):
        store, controller, rt, issue, work = stopped_run(self, config={'orchestration': {'no_result_steps': 2,
                                                                                          'max_changes': 1}})
        self.assertEqual(propose(rt, work, GUIDE)['status'], 'applied')
        controller.add_fact('r', 'f2', 'raw_fact', {'x': 2}, {'source': 'fixture'}, 'f2')
        controller.open_issue('r', 'again', issue['semantic_key'], 'h', 's', ['f2'], ['a'], ['c', 'f'], 'again')
        second = next(w for w in rt.controller.ready('r') if w['kind'] == 'meta_disposition')
        proposal = propose(rt, second, settings(guidance='Another idea.'))
        self.assertEqual(proposal['status'], 'rejected')
        self.assertIn('already in trial', proposal['error'])
        set_status(store, changes(store)[0]['id'], 'reverted')
        controller.add_fact('r', 'f3', 'raw_fact', {'x': 3}, {'source': 'fixture'}, 'f3')
        controller.open_issue('r', 'third', issue['semantic_key'], 'h', 's', ['f3'], ['a'], ['c', 'f'], 'third')
        third = next(w for w in rt.controller.ready('r') if w['kind'] == 'meta_disposition')
        proposal = propose(rt, third, settings(guidance='Another idea.'))
        self.assertEqual(proposal['status'], 'rejected')
        self.assertIn('max_changes', proposal['error'])
        self.assertEqual(len(changes(store)), 1)
        store.verify_history('r')


class RealExecutorTests(unittest.TestCase):
    def invoke_fixture(self, prompt, schema, folder, model, effort, timeout, **session):
        from rsi_lab import roles
        from rsi_lab.packets import encoded
        folder.mkdir(parents=True)
        (folder / 'prompt.txt').write_text(roles.transport.PREAMBLE + prompt)
        response = {'command': {}, 'working_memory_delta': '{}'}
        result = {'response': response, 'usage': {'input_tokens': 1, 'output_tokens': 1}, 'usage_complete': True,
                  'error': None, 'wall_seconds': .01, 'returncode': 0}
        (folder / 'transport_result.json').write_bytes(encoded(result))
        (folder / 'events.jsonl').write_text(json.dumps({'type': 'turn.completed', 'usage': result['usage']}))
        return result

    def test_codex_executor_recomputes_the_schema_from_the_effect_record(self):
        from unittest.mock import patch
        from rsi_lab import roles
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        rt.run_until_wait(Scripted({'lab': reject, 'discovery': none}), RES, max_steps=2)
        rt.orchestration.advance()
        controller.open_issue('r', 'i1', ['anomaly', 's', 't'], 'h', 's', [signals(store)[0]['id']], ['a'],
                              ['c', 'f'], 'i1')
        work = next(w for w in rt.controller.ready('r') if w['kind'] == 'meta_disposition')
        effect = rt.prepare_meta(work['id'], RES)
        packet = store.read_blob(effect['packet_ref'])
        envelope = json.loads(packet)
        self.assertIn('propose_lab_settings', json.dumps(envelope['response_schema']))
        self.assertEqual(effect['metadata']['orchestration'], {'lab_settings_action': True})
        dispatching = {**effect, 'status': 'dispatching', 'metadata': {**effect['metadata'], 'session_fence': 1}}
        root = tempfile.mkdtemp(prefix='rsi-orch-codex-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with patch.object(roles.transport, 'invoke', side_effect=self.invoke_fixture) as invoke:
            roles.CodexExecutor(os.path.join(root, 'calls'))(dispatching, packet)
            invoke.assert_called_once()
            self.assertEqual(invoke.call_args.args[1], envelope['response_schema'])
        with patch.object(roles.transport, 'invoke') as invoke:
            for metadata in ({k: v for k, v in dispatching['metadata'].items() if k != 'orchestration'},
                             {**dispatching['metadata'], 'orchestration': {'lab_settings_action': False}}):
                with self.subTest(metadata=metadata.get('orchestration')), self.assertRaises(ValueError):
                    roles.CodexExecutor(os.path.join(root, 'calls2'))({**dispatching, 'metadata': metadata}, packet)
            invoke.assert_not_called()
        store.verify_history('r')


class PublicationTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def test_publication_records_the_change_parks_the_issue_and_restarts_a_stopped_lab(self):
        store, controller, rt, issue, work = stopped_run(self)
        signal = signals(store)[0]['id']
        proposal = propose(rt, work, GUIDE)
        self.assertEqual(proposal['status'], 'applied')
        result = json.loads(store.read_blob(proposal['result_ref']))
        [change] = changes(store)
        self.assertEqual(change['status'], 'trial')
        self.assertEqual(change['settings'], {k: v for k, v in GUIDE.items() if k != 'rationale'})
        self.assertEqual(change['signal_fact_id'], signal)
        self.assertIsNone(change['previous_change_id'])
        self.assertEqual(change['issue_id'], issue['id'])
        [wait] = store.list('r', 'wait')
        self.assertEqual((wait['status'], wait['expiry_action'], wait['dependency_id']),
                         ('waiting', 'close_unresolved', change['id']))
        self.assertEqual(wait['deadline_at'], change['deadline_at'])
        lab = store.get('r', 'work', result['lab_work_id'])
        self.assertEqual(lab['status'], 'ready')
        self.assertIn(signal, lab['inputs']['required_evidence_ids'])
        self.assertNotIn('repair_of', lab['inputs'])
        executor = Scripted({'lab': none})
        run_next(rt, {'lab'}, executor)
        self.assertEqual(store.list('r', 'effect', id=lab['effect_id'] if lab['effect_id'] else
                                    store.get('r', 'work', lab['id'])['effect_id'])[0]['metadata']['orchestration'],
                         {'lab_settings_action': True, 'change_id': change['id']})
        constraints = executor.seen[0]['task']['constraints']['orchestration']
        self.assertEqual(constraints['guidance'], GUIDE['guidance'])
        self.assertEqual(len(constraints['recent_steps']), 2)
        store.verify_history('r')

    def test_an_unfinished_last_step_gets_no_extra_lab_step(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        lab = Scripted({'lab': lambda d: record('a'), 'discovery': discover})
        for kinds in ({'lab'}, {'diagnostic'}, {'lab'}, {'diagnostic'}, {'lab'}, {'diagnostic'}):
            run_next(rt, kinds, lab)
        rt.orchestration.advance()
        run_next(rt, {'meta_discovery'}, lab)
        while any(w['kind'] == 'meta_discovery' for w in rt.controller.ready('r')):
            run_next(rt, {'meta_discovery'}, lab)
        run_next(rt, {'lab'}, lab)
        self.assertEqual(outcomes(rt)[-1], ('unfinished', None))
        work = next(w for w in rt.controller.ready('r') if w['kind'] == 'meta_disposition')
        labs = len(store.list('r', 'work', kind='lab'))
        proposal = propose(rt, work, GUIDE)
        self.assertEqual(proposal['status'], 'applied')
        self.assertIsNone(json.loads(store.read_blob(proposal['result_ref']))['lab_work_id'])
        self.assertEqual(len(store.list('r', 'work', kind='lab')), labs)
        run_next(rt, {'diagnostic'}, lab)
        self.assertEqual(len(store.list('r', 'work', kind='lab')), labs + 1)
        store.verify_history('r')

    def test_interrupted_publication_resumes_to_the_same_result(self):
        store, controller, rt, issue, work = stopped_run(self)
        park = rt._park
        def die(*args, **kwargs):
            raise KeyboardInterrupt('simulated death before parking')
        rt._park = die
        with self.assertRaises(KeyboardInterrupt):
            propose(rt, work, GUIDE)
        [proposal] = [p for p in store.list('r', 'proposal') if p['status'] == 'admitted']
        self.assertEqual(len(changes(store)), 1)
        self.assertEqual(store.list('r', 'wait'), [])
        rt._park = park
        applied = rt.apply_meta(proposal['effect_id'])
        self.assertEqual(applied['status'], 'applied')
        self.assertEqual(rt.apply_meta(proposal['effect_id']), applied)
        self.assertEqual(len(changes(store)), 1)
        self.assertEqual(len(store.list('r', 'wait')), 1)
        self.assertEqual(len([w for w in store.list('r', 'work', kind='lab') if w['trigger'].startswith('orchestration:')]), 1)
        store.verify_history('r')



def guided(data):
    """Lab that only gets the input right once Meta's guidance is in force."""
    return record('a') if data['task']['constraints']['orchestration']['guidance'] else reject(data)


def settlement(store, change):
    fact = store.get('r', 'fact', change['settlement_fact_id'])
    return fact, json.loads(store.read_blob(fact['content_ref']))


def published_trial(test, config=ORCH, adapters=None, payload=GUIDE):
    store, controller, rt, issue, work = stopped_run(test, config=config, adapters=adapters)
    test.assertEqual(propose(rt, work, payload)['status'], 'applied')
    return store, controller, rt, issue, changes(store)[0]


class SettlementTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()

    def assert_settled(self, store, rt, change_id, status, reason):
        change = store.get('r', 'registration', change_id)
        self.assertEqual((change['status'], change['reason']), (status, reason))
        fact, content = settlement(store, change)
        self.assertEqual(fact['kind'], 'validation')
        self.assertEqual(fact['provenance'], {'source': 'code_settlement', 'change_id': change_id})
        self.assertEqual((content['result'], content['reason'], content['benefit_claim']),
                         (status, reason, 'not_evaluated'))
        self.assertEqual(content['no_result_steps'], rt.orchestration.no_result_steps)
        self.assertEqual(content['step_count'], len(content['steps']))
        self.assertEqual(content['settings'], change['settings'])
        issue = store.get('r', 'issue', change['issue_id'])
        self.assertIn(fact['id'], issue['evidence_ids'])
        self.assertEqual(store.get('r', 'dependency', change_id)['value']['status'], status)
        self.assertEqual(store.list('r', 'wait')[0]['status'], 'fired')
        woken = [w for w in store.list('r', 'work', kind='meta_disposition')
                 if w['trigger'].startswith('dependency:' + change_id)]
        self.assertEqual(len(woken), 1)
        return change, content

    def test_a_new_result_keeps_the_change(self):
        store, controller, rt, issue, change = published_trial(self)
        executor = Scripted({'lab': guided, 'discovery': none, 'disposition': lambda d: CLOSE})
        rt.run_until_wait(executor, RES, max_steps=4)
        change, content = self.assert_settled(store, rt, change['id'], 'kept', 'new_result')
        self.assertEqual(rt.orchestration.current_change()['id'], change['id'])
        rt.run_until_wait(executor, RES, max_steps=10)
        disposition = [d for d in executor.seen if d['mode'] == 'disposition'][-1]
        self.assertIn(change['settlement_fact_id'], [p['evidence_id'] for p in disposition['delivered_pages']])
        self.assertEqual(store.get('r', 'issue', issue['id'])['status'], 'closed_unresolved')
        store.verify_history('r')

    def test_n_steps_without_a_new_result_revert_and_published_facts_remain(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': lambda d: record('a'), 'discovery': discover,
                             'disposition': in_order(('propose_lab_settings', GUIDE), CLOSE)})
        rt.run_until_wait(executor, RES, max_steps=26)
        [change] = changes(store)
        change, content = self.assert_settled(store, rt, change['id'], 'reverted', 'no_result')
        under = [s for s in rt.orchestration.steps() if s['change_id'] == change['id']]
        self.assertEqual([s['outcome'] for s in under][:N], ['no_result'] * N)
        self.assertEqual([r['reason_code'] for r in content['steps']][:N], ['repeated_result'] * N)
        tools = [e for e in store.list('r', 'effect', kind='probe_tool') if e['status'] == 'succeeded']
        for effect in tools:
            self.assertTrue(store.list('r', 'fact', id='probe_' + effect['id'][-16:]))
        guided_flags = [d['task']['constraints']['orchestration']['guidance'] is not None
                        for d in lab_packets(executor)]
        last_guided = max(i for i, flag in enumerate(guided_flags) if flag)
        self.assertEqual(last_guided - guided_flags.index(True) + 1, N)
        self.assertTrue(guided_flags[last_guided + 1:])
        self.assertFalse(any(guided_flags[last_guided + 1:]))
        for data in lab_packets(executor)[last_guided + 1:]:
            self.assertEqual(data['task']['constraints']['orchestration']['recent_steps'], [])
        self.assertEqual(rt.orchestration.current_change(), None)
        self.assertEqual(len(signals(store)), 1)
        store.verify_history('r')

    def test_a_stop_during_the_trial_reverts(self):
        store, controller, rt, issue, change = published_trial(
            self, adapters=[validating_adapter(), failing_adapter()])
        rt.run_until_wait(Scripted({'lab': lambda d: ('request_diagnostic', diag({'record_id': 'a'}, 'probe_fail')),
                                    'discovery': none, 'disposition': lambda d: CLOSE}), RES, max_steps=3)
        self.assert_settled(store, rt, change['id'], 'reverted', 'lab_stopped')
        self.assertEqual(rt.orchestration.current_change(), None)
        store.verify_history('r')

    def test_a_capacity_block_during_the_trial_reverts_at_once(self):
        from rsi_lab.packets import PacketBuilder
        store, controller, rt, issue, change = published_trial(self)
        rt.packet_builder_factory = lambda catalog, max_input_bytes: PacketBuilder(catalog, max_input_bytes=300)
        rt.run_until_wait(Scripted({'disposition': lambda d: CLOSE}), RES, max_steps=2)
        self.assert_settled(store, rt, change['id'], 'reverted', 'lab_stopped')
        store.verify_history('r')

    def test_the_deadline_reverts_without_a_conclusion(self):
        store, controller, rt, issue, change = published_trial(self)
        executor = Scripted({'lab': none, 'disposition': lambda d: CLOSE})
        rt.run_until_wait(executor, RES, max_steps=1)
        self.assertEqual(store.get('r', 'registration', change['id'])['status'], 'trial')
        events = store.verify_history('r')['events']
        rt.orchestration.clock = lambda: change['deadline_at'] - 1
        rt.orchestration.advance()
        self.assertEqual(store.verify_history('r')['events'], events)
        rt.orchestration.clock = lambda: change['deadline_at']
        rt.run_until_wait(executor, RES, max_steps=1)
        self.assert_settled(store, rt, change['id'], 'reverted', 'no_conclusion')
        rt.enqueue_lab('after')
        rt.run_until_wait(executor, RES, max_steps=3)
        self.assertEqual(lab_packets(executor)[-1]['task']['constraints']['orchestration'],
                         {'guidance': None, 'recent_steps': []})
        store.verify_history('r')

    def test_finalizing_reverts_a_change_in_trial(self):
        store, controller, rt, issue, change = published_trial(self)
        rt.finalize('operator stop')
        change = store.get('r', 'registration', change['id'])
        self.assertEqual((change['status'], change['reason']), ('reverted', 'no_conclusion'))
        self.assertIn(change['settlement_fact_id'], store.get('r', 'issue', issue['id'])['evidence_ids'])
        store.verify_history('r')

    def test_settlement_is_idempotent_and_resumes_after_a_crash(self):
        store, controller, rt, issue, change = published_trial(self)
        rt.run_until_wait(Scripted({'lab': none}), RES, max_steps=1)
        dependency = rt.controller.dependency
        def die(run_id, dependency_id, producer, value, key):
            if key.startswith('orchestration-settle-dependency:'):
                raise KeyboardInterrupt('simulated death before notification')
            return dependency(run_id, dependency_id, producer, value, key)
        rt.controller.dependency = die
        rt.orchestration.clock = lambda: change['deadline_at'] + 1
        with self.assertRaises(KeyboardInterrupt):
            rt.orchestration.advance()
        self.assertEqual(store.get('r', 'registration', change['id'])['status'], 'reverted')
        self.assertEqual(store.list('r', 'wait')[0]['status'], 'waiting')
        rt.controller.dependency = dependency
        rt.orchestration.advance()
        self.assertEqual(store.list('r', 'wait')[0]['status'], 'fired')
        events = store.verify_history('r')['events']
        rt.orchestration.advance()
        rt.orchestration.advance()
        self.assertEqual(store.verify_history('r')['events'], events)
        self.assertEqual(len([f for f in store.list('r', 'fact') if f['kind'] == 'validation']), 1)
        store.verify_history('r')


class EndToEndTests(unittest.TestCase):
    """The two paths of the design example, with N = 4."""
    CONFIG = {'orchestration': {'no_result_steps': 4, 'max_changes': 2}}

    def setUp(self):
        EXECUTIONS.clear()

    def test_guidance_fixes_a_rejected_lab_and_is_kept(self):
        store, controller, rt = open_run(self, config=self.CONFIG, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        resolve = ('close_issue', {'resolution': 'resolved', 'reason': 'Lab produces results again'})
        executor = Scripted({'lab': lambda d: guided(d) if not any(
                                 'record_id' in (s['inputs_excerpt'] or '') and s['outcome'] == 'new_result'
                                 for s in d['task']['constraints']['orchestration']['recent_steps']) else none(d),
                             'discovery': discover,
                             'disposition': in_order(('propose_lab_settings', GUIDE), resolve)})
        out = rt.run_until_wait(executor, RES, max_steps=30)
        self.assertEqual(executor.calls[:4], ['lab', 'lab', 'discovery', 'disposition'])
        self.assertEqual(json.loads(store.read_blob(signals(store)[0]['content_ref']))['reason'], 'lab_stopped')
        [change] = changes(store)
        self.assertEqual((change['status'], change['reason']), ('kept', 'new_result'))
        self.assertEqual(store.list('r', 'issue')[0]['status'], 'resolved')
        self.assertEqual(rt.orchestration.current_change()['id'], change['id'])
        self.assertEqual(out['status'], 'quiescent')
        store.verify_history('r')

    def test_a_change_that_does_not_help_is_reverted(self):
        store, controller, rt = open_run(self, config=self.CONFIG, adapters=[validating_adapter()])
        rt.enqueue_lab('lab-1')
        executor = Scripted({'lab': reject, 'discovery': discover,
                             'disposition': in_order(('propose_lab_settings', GUIDE), CLOSE)})
        rt.run_until_wait(executor, RES, max_steps=30)
        [change] = changes(store)
        self.assertEqual((change['status'], change['reason']), ('reverted', 'lab_stopped'))
        self.assertEqual(rt.orchestration.current_change(), None)
        self.assertEqual(len(signals(store)), 1)
        self.assertEqual(store.list('r', 'issue')[0]['status'], 'closed_unresolved')
        packets = lab_packets(executor)
        self.assertEqual([p['task']['constraints']['orchestration']['guidance'] is not None for p in packets],
                         [False, False, True, True])
        store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
