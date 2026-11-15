"""Offline host recovery fixtures; no models, oracle calls, or real-run writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.store import Store, digest
from scripts.recover_campaign_tool_failure import plan_recovery, recover


def prepare(inputs):
    return inputs


def reservations(packet):
    return {'tool_calls': 1}


def execute(effect, raw):
    return {'status': 'failed_definite', 'actual_costs': {'tool_calls': 1},
            'execution': {'reason': 'Deliberate offline failure fixture', 'scored_candidates': 0}}


def publication(effect, receipt):
    raise AssertionError('Recovery must not call an adapter publication or executor')


class CampaignFailureRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'ledger')
        self.addCleanup(self.store.close)
        self.control = Controller(self.store)
        self.control.initialize('r', budgets={'tool_calls': {'limit': 10, 'hard': True}})
        self.control.add_fact('r', 'protocol', 'source_statement', 'Offline fixture protocol',
                             {'source': 'fixture'}, 'protocol')
        self.control.open_issue('r', 'i', 'fixture-gap', 'The tool outcome is unknown', 'Fixture scope',
            ['protocol'], ['The tool may fail'], ['Inspect the true receipt'], 'issue')
        self.adapter = Adapter('fixture.tool', '1', 'fixture_tool', {'scope': 'Offline failure fixture'},
                               prepare, reservations, execute, publication)
        self.control.dependency('r', 'dep', 'fixture.tool', {'status': 'pending'}, 'dep')
        self.work = self.control.enqueue('r', 'diagnostic', 'diagnostic', 'diagnostic',
            inputs={'issue_id': 'i', 'dependency_id': 'dep', 'capability_id': 'fixture.tool',
                    'capability_version': '1', 'inputs_json': '{}'})
        self.control.park('r', 'i', 'dep', 'fixture.tool', 1, {'kind': 'version_changed'},
                          9999999999, 'replan', 'park')

    def failed(self, status='failed_definite'):
        descriptor = self.adapter.descriptor()
        effect = self.control.prepare_effect('r', self.work['id'], 'fixture_tool', {}, {'tool_calls': 1},
            'prepare', metadata={'adapter_id': 'fixture.tool', 'adapter_version': '1',
                'adapter_descriptor_ref': self.store.put_blob(descriptor),
                'adapter_descriptor_sha256': digest(descriptor), 'issue_id': 'i', 'dependency_id': 'dep'})
        return self.control.dispatch_effect('r', effect['id'],
            lambda effect, raw: {**execute(effect, raw), 'status': status})

    def test_read_only_plan_then_exact_publication_and_idempotent_wake(self):
        effect = self.failed()
        before = self.store.verify_history('r')
        budgets = self.store.list('r', 'budget')
        effects = self.store.list('r', 'effect')
        plan = plan_recovery(self.store, 'r', effect['id'], 'i')
        self.assertEqual(self.store.verify_history('r'), before)
        result = recover(self.store, plan)
        self.assertEqual(result['status'], 'complete')
        issue = self.store.get('r', 'issue', 'i')
        self.assertEqual(issue['status'], 'open')
        self.assertIsNone(issue['conclusion_ref'])
        self.assertIn(plan['failure_fact_id'], issue['evidence_ids'])
        fact = self.store.get('r', 'fact', plan['failure_fact_id'])
        content = json.loads(self.store.read_blob(fact['content_ref']))
        self.assertEqual(content['receipt'], json.loads(self.store.read_blob(effect['result_ref'])))
        self.assertEqual(fact['provenance']['authority'], 'execution_receipt_not_scientific_evidence')
        self.assertEqual(self.store.get('r', 'dependency', 'dep')['value']['status'], 'failed')
        self.assertEqual(self.store.get('r', 'work', result['wake_work_id'])['status'], 'ready')
        self.assertEqual(self.store.list('r', 'budget'), budgets)
        self.assertEqual(self.store.list('r', 'effect'), effects)
        completed = self.store.verify_history('r')
        self.assertEqual(recover(self.store, plan_recovery(self.store, 'r', effect['id'], 'i')), result)
        self.assertEqual(self.store.verify_history('r'), completed)

    def test_crash_after_atomic_publication_resumes_without_duplicate_evidence_or_cost(self):
        effect = self.failed()
        plan = plan_recovery(self.store, 'r', effect['id'], 'i')
        with patch.object(Controller, 'tick', side_effect=RuntimeError('Offline crash before wake')):
            with self.assertRaisesRegex(RuntimeError, 'Offline crash'):
                recover(self.store, plan)
        self.assertEqual(self.store.get('r', 'registration', plan['registration_id'])['status'],
                         'published_waiting_for_tick')
        result = recover(self.store, plan_recovery(self.store, 'r', effect['id'], 'i'))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.store.list('r', 'effect')), 1)
        self.assertEqual(len(self.store.list('r', 'fact', id=plan['failure_fact_id'])), 1)
        self.assertEqual(self.store.get('r', 'budget', 'tool_calls')['spent'], 1)

    def test_unknown_execution_is_not_relabelled_or_retried(self):
        effect = self.failed(status='unknown')
        before = self.store.verify_history('r')
        with self.assertRaisesRegex(ValueError, 'definitely failed'):
            plan_recovery(self.store, 'r', effect['id'], 'i')
        self.assertEqual(self.store.verify_history('r'), before)

    def test_wrong_issue_and_non_snapshot_fact_are_rejected_without_writes(self):
        effect = self.failed()
        before = self.store.verify_history('r')
        with self.assertRaisesRegex(ValueError, 'original diagnostic'):
            plan_recovery(self.store, 'r', effect['id'], 'other-issue')
        with self.assertRaisesRegex(ValueError, 'existing original campaign'):
            plan_recovery(self.store, 'r', effect['id'], 'i', 'protocol')
        self.assertEqual(self.store.verify_history('r'), before)


if __name__ == '__main__':
    unittest.main()
