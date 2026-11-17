"""Direct adapter APIs retain atomic claims across outbox/receipt crash points.

The toy executor only records calls in memory; no model, network or oracle runs.
"""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.runtime import ExistingWork, Runtime
from rsi_lab.store import Store


EXECUTIONS = []


def prepare(inputs):
    if set(inputs) != {'identity', 'outcome'}:
        raise ValueError('Exact toy input required')
    return dict(inputs)


def reservations(packet):
    return {'tool_calls': 1}


def reject_prepare(inputs):
    raise ValueError('Inputs no longer executable after admission')


def claim_identities(store, run_id, inputs):
    return [inputs['identity']]


def execute(effect, raw):
    inputs = json.loads(raw)
    EXECUTIONS.append(effect['id'])
    if inputs['outcome'] == 'fail':
        return {'status': 'failed_definite', 'actual_costs': {'tool_calls': 1}, 'error': 'known failure'}
    if inputs['outcome'] == 'unknown':
        return {'status': 'unknown', 'actual_costs': {'tool_calls': 1}, 'error': 'execution uncertain'}
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
            'result': {'identity': inputs['identity']}}


def publication(effect, receipt):
    return {'facts': [{'id': 'result_' + effect['id'], 'kind': 'raw_fact',
                      'content': receipt['result'], 'provenance': {'effect_id': effect['id']}}],
            'conclusions': [], 'data': receipt['result']}


def completed_identities(effect, result):
    return [result['data']['identity']]


def make_adapter():
    return Adapter('toy_claims', 'v1', 'toy_claims', {'scope': 'test identities'},
                   prepare, reservations, execute, publication,
                   claim_identities=claim_identities, completed_identities=completed_identities)


class DirectAdapterClaimTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'run')
        self.addCleanup(self.store.close)
        Controller(self.store).initialize('r', budgets={'tool_calls': {'limit': 20, 'hard': True}},
                                         config={'execution_mode': 'test', 'max_open_reviews': 0})
        self.rt = Runtime(self.store, 'r', adapters=[make_adapter()])

    def run_tool(self, trigger, outcome='ok'):
        return self.rt.execute_adapter('toy_claims', {'identity': 'shared', 'outcome': outcome},
                                       trigger_id=trigger)

    def claims(self):
        return self.store.list('r', 'registration', kind='work_identity_claim')

    def test_direct_success_deduplicates_later_trigger_and_replays_without_execution(self):
        first = self.run_tool('first')
        self.assertEqual(self.claims()[0]['status'], 'covered')
        with self.assertRaises(ExistingWork) as caught:
            self.run_tool('later')
        self.assertEqual(caught.exception.disposition['status'], 'covered')
        self.assertEqual(caught.exception.disposition['owner_work_ids'], [first['effect']['work_id']])
        replay = self.run_tool('first')
        self.assertEqual(replay['effect']['id'], first['effect']['id'])
        self.assertEqual(EXECUTIONS, [first['effect']['id']])
        self.assertEqual(len(self.store.list('r', 'work', kind='diagnostic')), 1)
        self.assertEqual(self.store.get('r', 'budget', 'tool_calls')['spent'], 1)

    def test_direct_outbox_crash_before_prepare_is_drained_by_normal_driver(self):
        with patch.object(self.rt.controller, 'prepare_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_tool('crash-before-prepare')
        self.assertEqual(EXECUTIONS, [])
        self.assertEqual(self.store.list('r', 'effect'), [])
        work = self.store.list('r', 'work', kind='diagnostic')[0]
        self.assertEqual(work['status'], 'ready')
        self.assertEqual(work['inputs']['capability_id'], 'toy_claims')
        self.assertEqual(self.claims()[0]['status'], 'held')
        with self.assertRaises(ExistingWork) as caught:
            self.run_tool('competitor')
        self.assertEqual(caught.exception.disposition['status'], 'waiting_existing')
        recovered = Runtime(self.store, 'r', adapters=[make_adapter()])
        def no_model(*args):
            raise AssertionError('The driver must drain the existing diagnostic before any model')
        report = recovered.run_until_wait(no_model, {'model_calls': 1, 'tokens': 1, 'wall_seconds': 1}, max_steps=1)
        self.assertEqual(report['steps'][0]['work_id'], work['id'])
        self.assertEqual(len(EXECUTIONS), 1)
        self.assertEqual(self.claims()[0]['status'], 'covered')
        caller_replay = self.run_tool('crash-before-prepare')
        self.assertEqual(caller_replay['effect']['id'], report['steps'][0]['effect_id'])
        self.assertEqual(len(EXECUTIONS), 1)

    def test_known_failure_releases_then_recovery_and_new_trigger_retry_are_safe(self):
        first = self.run_tool('failure', 'fail')
        self.assertEqual(first['effect']['status'], 'failed_definite')
        self.assertEqual(self.claims()[0]['status'], 'released')
        recovered = Runtime(self.store, 'r', adapters=[make_adapter()])
        recovered.recover()
        self.assertEqual(len(EXECUTIONS), 1)
        second = recovered.execute_adapter('toy_claims', {'identity': 'shared', 'outcome': 'ok'}, trigger_id='retry')
        self.assertNotEqual(first['effect']['id'], second['effect']['id'])
        self.assertEqual(self.claims()[0]['status'], 'covered')
        self.assertEqual(self.claims()[0]['generation'], 2)
        self.assertEqual(EXECUTIONS, [first['effect']['id'], second['effect']['id']])

    def test_success_before_publication_crash_holds_until_recovery_without_resending(self):
        with patch.object(self.rt, 'publish_adapter_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_tool('publication-crash')
        effect = self.store.list('r', 'effect')[0]
        self.assertEqual(effect['status'], 'succeeded')
        self.assertEqual(self.claims()[0]['status'], 'held')
        with self.assertRaises(ExistingWork) as caught:
            self.run_tool('too-early')
        self.assertEqual(caught.exception.disposition['status'], 'waiting_existing')
        recovered = Runtime(self.store, 'r', adapters=[make_adapter()])
        recovered.recover_adapters()
        recovered.recover_adapters()
        self.assertEqual(self.claims()[0]['status'], 'covered')
        self.assertEqual(EXECUTIONS, [effect['id']])

    def test_unknown_execution_holds_through_recovery_and_rejects_new_owner(self):
        first = self.run_tool('uncertain', 'unknown')
        self.assertEqual(first['effect']['status'], 'unknown')
        self.assertEqual(self.claims()[0]['status'], 'held')
        recovered = Runtime(self.store, 'r', adapters=[make_adapter()])
        recovered.recover()
        with self.assertRaises(ExistingWork) as caught:
            recovered.execute_adapter('toy_claims', {'identity': 'shared', 'outcome': 'ok'}, trigger_id='unsafe-retry')
        self.assertEqual(caught.exception.disposition['status'], 'waiting_existing')
        self.assertEqual(EXECUTIONS, [first['effect']['id']])
        self.assertEqual(self.claims()[0]['status'], 'held')

    def test_prepare_failure_after_outbox_admission_releases_immediately(self):
        with patch.object(self.rt.controller, 'prepare_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_tool('admitted')
        work = self.store.list('r', 'work', kind='diagnostic')[0]
        refused = Runtime(self.store, 'r', adapters=[replace(make_adapter(), prepare=reject_prepare)])
        result = refused.execute_adapter('toy_claims', {'identity': 'shared', 'outcome': 'ok'},
                                          trigger_id=work['id'], work_id=work['id'])
        self.assertIsNone(result['effect'])
        self.assertEqual(result['work']['status'], 'failed')
        self.assertEqual(self.claims()[0]['status'], 'released')
        self.assertEqual(EXECUTIONS, [])
        # A new owner may retry after a known local refusal; no unknown work is hidden.
        retried = self.run_tool('after-refusal')
        self.assertEqual(retried['effect']['status'], 'succeeded')
        self.assertEqual(len(EXECUTIONS), 1)

    def test_recovery_releases_known_cancelled_owner_from_pre_reconciliation_crash(self):
        with patch.object(self.rt.controller, 'prepare_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_tool('cancelled-before-prepare')
        work = self.store.list('r', 'work', kind='diagnostic')[0]
        self.store.mutate('r', 'cancel-with-old-transition', 'fixture_cancel',
            lambda tx: tx.update('work', work['id'], {'status': 'cancelled'}))
        self.assertEqual(self.claims()[0]['status'], 'held')
        recovered = Runtime(self.store, 'r', adapters=[make_adapter()])
        recovered.recover()
        self.assertEqual(self.claims()[0]['status'], 'released')
        self.assertEqual(EXECUTIONS, [])
        retried = recovered.execute_adapter('toy_claims', {'identity': 'shared', 'outcome': 'ok'},
                                             trigger_id='after-recovered-cancel')
        self.assertEqual(retried['effect']['status'], 'succeeded')
        self.assertEqual(len(EXECUTIONS), 1)
