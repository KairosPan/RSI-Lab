"""Offline Meta audit/recommendation boundaries with frozen packet contracts."""
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.literature_context import LITERATURE_DECISION_POLICY_VERSION, LITERATURE_DELEGATION_VERSION
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from tests.v2.test_improvements import FakeModel, prepare, reservations, execute, publication


def audit_adapter(*, modern=False, requires_acceptance=False):
    manifest = {'scope': 'Offline audit fixture'}
    if modern:
        manifest['literature_decision_policy'] = LITERATURE_DECISION_POLICY_VERSION
    name = 'modern' if modern else 'audit'
    return Adapter('literature.' + name, 'v1', 'fixture_' + name, manifest,
                   prepare, reservations, execute, publication, requires_acceptance=requires_acceptance)


class LiteratureFeedbackBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={name: {'limit': 40, 'hard': name.endswith('calls')}
            for name in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds')}, config={'execution_mode': 'replay',
                'research_features': {'meta_lab': True}, 'max_input_bytes': 500000})
        self.numeric = Adapter('candidate_test', 'v1', 'fixture_numeric', {'scope': 'Offline numeric fixture'},
                               prepare, reservations, execute, publication)
        self.legacy = audit_adapter()
        self.modern = audit_adapter(modern=True)
        self.reset_runtime(modern=True)
        self.controller.add_fact('r', 'original', 'raw_fact', {'score': .5, 'candidate': 'old'},
                                 {'source': 'Offline measured fixture'}, 'original')
        self.sequence = 0
        self.reservations = {'model_calls': 1, 'tokens': 1, 'wall_seconds': 1}

    def reset_runtime(self, *, modern):
        self.runtime = Runtime(self.store, 'r', adapters=[self.numeric, self.legacy] +
                               ([self.modern] if modern else []))

    def review_work(self):
        self.sequence += 1
        # Ordinary new evidence is now subject to the open-review allowance; an
        # input change always admits the Meta review these fixtures need.
        event = self.runtime.observe('fixture-review-' + str(self.sequence), 'input_changed', 0, ['original'])
        return self.store.get('r', 'work', event['work_id'])

    def plan(self, work):
        agent, mode, task, options = self.runtime._context(work)
        return agent.prepare(mode, task, **options)

    @staticmethod
    def advice(capability='literature.audit'):
        return {'change_kind': 'bounded_check', 'instruction': 'Perform the proposed bounded check',
                'capability_id': capability, 'capability_version': 'v1',
                # Every recommendation now freezes a machine-checkable execution contract.
                'execution_contract_json': json.dumps({'version': 1, 'kind': 'exact_inputs',
                                                       'inputs': {'candidates': ['new']}}),
                'expected_outcome': 'An original observation', 'measurement': 'Reported result',
                'falsifier': 'No supporting observation'}

    @staticmethod
    def lab_memory(response, data):
        if data['task'].get('literature_context'):
            response['working_memory_delta'] = json.dumps({'rationale_summaries': [{
                'kind': 'literature_decision', 'decision': 'local_work',
                'reason': 'This offline fixture only tests the permitted action boundary.',
                'evidence_ids': response['command']['evidence_ids'], 'related_task_ids': []}]})

    def lab(self, decision):
        work = next(row for row in self.controller.ready('r') if row['kind'] == 'lab')
        model = FakeModel('request_diagnostic' if decision == 'adopt' else 'no_action',
            {'capability_id': 'candidate_test', 'capability_version': 'v1',
             'inputs_json': '{"candidates":["new"]}'} if decision == 'adopt' else
            {'reason': 'The legacy tool recommendation is not an executable Lab action in this contract.'},
            decision=decision, mutate=self.lab_memory)
        return self.runtime.run_lab(work['id'], model, self.reservations)

    def legacy_recommendation_after_upgrade(self):
        self.reset_runtime(modern=False)
        work = self.review_work()
        prepared = self.runtime.prepare_meta(work['id'], self.reservations)
        packet = json.loads(self.store.read_blob(prepared['metadata']['plan_ref']))
        self.assertNotIn('lab_literature_delegation_version', packet['task']['constraints']['research_loop'])
        effect = self.controller.dispatch_effect('r', prepared['id'], FakeModel('recommend_change', self.advice()))
        self.reset_runtime(modern=True)
        outcome = self.runtime.apply_meta(effect['id'])
        self.assertEqual(outcome['status'], 'applied', outcome)
        return effect, self.runtime.improvements.rows()[0]

    def test_marker_requires_an_accepted_loaded_modern_adapter(self):
        self.reset_runtime(modern=False)
        work = self.review_work()
        scope = self.plan(work)['task']['constraints']['research_loop']
        self.assertNotIn('lab_literature_delegation_version', scope)
        self.runtime = Runtime(self.store, 'r', adapters=[self.numeric, self.legacy,
                               audit_adapter(modern=True, requires_acceptance=True)])
        self.assertNotIn('lab_literature_delegation_version',
                         self.plan(work)['task']['constraints']['research_loop'])
        self.store.mutate('r', 'unloaded', 'fixture_unloaded_capability', lambda tx: tx.insert(
            'capability', 'unloaded', {'status': 'verified', 'capability_id': 'literature.unloaded',
                'capability_version': 'v1', 'artifact_ref': 'f' * 64}))
        self.assertNotIn('lab_literature_delegation_version',
                         self.plan(work)['task']['constraints']['research_loop'])
        self.reset_runtime(modern=True)
        self.assertEqual(self.plan(work)['task']['constraints']['research_loop'][
            'lab_literature_delegation_version'], LITERATURE_DELEGATION_VERSION)

    def test_modern_meta_can_audit_literature_but_cannot_recommend_its_tools_to_lab(self):
        work = self.review_work()
        packet = self.plan(work)
        self.assertIn('literature.audit', packet['task']['capability_catalog'])
        self.assertIn('for your independent audit', packet['envelope']['prompt'])
        self.assertIn('not allowed recommend_change targets', packet['envelope']['prompt'])
        model = FakeModel('recommend_change', self.advice())
        rejected = self.runtime.run_meta(work['id'], model, self.reservations)
        self.assertEqual(rejected['proposal']['status'], 'rejected')
        self.assertIn('not Lab recommendation targets', rejected['proposal']['error'])
        self.assertEqual(self.runtime.improvements.rows(), [])
        with self.assertRaisesRegex(ValueError, 'not Lab recommendation targets'):
            self.runtime.improvements.recommend({'id': 'forged-route', 'effect_id': rejected['effect']['id'],
                'command': {'action': 'recommend_change', 'payload': self.advice(), 'evidence_ids': ['original']}})
        audited = self.runtime.run_meta(self.review_work()['id'], FakeModel('request_diagnostic', {
            'capability_id': 'literature.audit', 'capability_version': 'v1',
            'inputs_json': '{"candidates":["audit-fixture"]}'}), self.reservations)
        self.assertEqual(audited['proposal']['status'], 'applied', audited['proposal'])
        diagnostic = next(row for row in self.controller.ready('r') if row['kind'] == 'diagnostic')
        self.assertNotIn('literature_task_id', diagnostic['inputs'])
        result = self.runtime.execute_adapter('literature.audit', {'candidates': ['audit-fixture']},
                                              trigger_id=diagnostic['id'], work_id=diagnostic['id'])
        self.assertEqual(result['effect']['status'], 'succeeded')
        self.assertEqual(self.runtime.improvements.rows(), [])

    def test_numeric_recommendation_adoption_execution_and_review_still_complete(self):
        outcome = self.runtime.run_meta(self.review_work()['id'],
            FakeModel('recommend_change', self.advice('candidate_test')), self.reservations)
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        adopted = self.lab('adopt')
        self.assertEqual(adopted['proposal']['status'], 'applied', adopted['proposal'])
        recommendation = self.runtime.improvements.rows()[0]
        work = self.store.get('r', 'work', recommendation['tool_work_id'])
        self.runtime.execute_adapter('candidate_test', {'candidates': ['new']}, trigger_id=work['id'], work_id=work['id'])
        review = next(row for row in self.controller.ready('r') if row['inputs'].get('improvement_review_ids'))
        checked = self.runtime.run_meta(review['id'], FakeModel('no_action', {'reason': 'Review the actual measurement'},
                                        review=True), self.reservations)
        self.assertEqual(checked['proposal']['status'], 'applied', checked['proposal'])
        self.assertEqual(self.runtime.improvements.rows()[0]['status'], 'reviewed')
        self.store.verify_history('r')

    def test_legacy_packet_replay_keeps_its_contract_and_modern_lab_can_decline(self):
        effect, _ = self.legacy_recommendation_after_upgrade()
        before = self.store.verify_history('r')
        self.assertEqual(self.runtime.apply_meta(effect['id'])['status'], 'applied')
        self.assertEqual(self.store.verify_history('r'), before)
        outcome = self.lab('decline')
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        self.assertEqual(self.runtime.improvements.rows()[0]['status'], 'declined')
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])

    def test_legacy_recommendation_can_be_deferred_without_bypassing_lab_tool_boundary(self):
        self.legacy_recommendation_after_upgrade()
        for expected_status in ('deferred', 'deferred_closed'):
            outcome = self.lab('defer')
            self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
            self.assertEqual(self.runtime.improvements.rows()[0]['status'], expected_status)
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])
        self.assertEqual(self.runtime.pending_improvements(), [])


if __name__ == '__main__':
    unittest.main()
