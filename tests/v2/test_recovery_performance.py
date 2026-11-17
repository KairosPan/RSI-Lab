"""Incremental recovery preserves crash duties and performs one full audit.

All executions use local fixtures; no provider, scorer or experiment is started.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller, identity
from rsi_lab.runtime import COMPLETION_KIND, Runtime
from rsi_lab.store import Store
from tests.v2.test_direct_adapter_claims import EXECUTIONS, make_adapter
from tests.v2.test_improvements import FakeModel


class RecoveryPerformanceTests(unittest.TestCase):
    def setUp(self):
        EXECUTIONS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'run')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={name: {'limit': 1000, 'hard': name.endswith('calls')}
            for name in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds')},
            config={'execution_mode': 'replay', 'max_open_reviews': 0, 'model_failure_retries': 1,
                    'research_features': {'scientific_memory': True}, 'max_input_bytes': 500000})
        self.rt = Runtime(self.store, 'r', adapters=[make_adapter()])
        self.reservations = {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1}

    def model(self, trigger, *, delta=None):
        work = self.rt.enqueue_lab(trigger)
        def remember(response, data):
            response['working_memory_delta'] = json.dumps(delta or {})
        return self.rt.run_lab(work['id'], FakeModel('no_action', {'reason': 'Fixture inspected'},
                                                     mutate=remember), self.reservations)

    def adapter(self, trigger):
        return self.rt.execute_adapter('toy_claims', {'identity': trigger, 'outcome': 'ok'}, trigger_id=trigger)

    def markers(self):
        return self.store.list('r', 'registration', kind=COMPLETION_KIND)

    def event_count(self):
        return self.store.db.execute('SELECT COUNT(*) FROM events WHERE run_id=?', ('r',)).fetchone()[0]

    def assert_steady_recovery(self, model_count, adapter_count):
        before = (self.event_count(), self.store.list('r', 'effect'), self.store.list('r', 'budget'))
        with patch.object(self.store, 'verify_history', wraps=self.store.verify_history) as verify, \
                patch.object(self.rt, 'apply_meta', side_effect=AssertionError('Completed proposal replayed')), \
                patch.object(self.rt, 'publish_adapter_effect', side_effect=AssertionError('Completed publication replayed')), \
                patch.object(self.rt, '_commit_agent_turn', side_effect=AssertionError('Committed turn replayed')):
            result = self.rt.recover()
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['agent_recovery'], [])
        self.assertEqual(len(result['proposals']), model_count)
        self.assertEqual(len(result['adapter_results']), adapter_count)
        self.assertTrue(all(row['status'] == 'published' for row in result['adapter_results']))
        self.assertEqual(before, (self.event_count(), self.store.list('r', 'effect'), self.store.list('r', 'budget')))
        return result

    def test_many_completed_effects_recover_without_per_effect_full_verification(self):
        for i in range(12):
            self.model('model-' + str(i))
            self.adapter('tool-' + str(i))
        self.assertEqual(len(self.markers()), 24)
        self.assert_steady_recovery(12, 12)
        self.assert_steady_recovery(12, 12)
        self.assertEqual(len(EXECUTIONS), 12)

    def test_public_adapter_recovery_audits_once_and_keeps_published_report(self):
        effect = self.adapter('completed')['effect']
        with patch.object(self.store, 'verify_history', wraps=self.store.verify_history) as verify, \
                patch.object(self.rt, 'publish_adapter_effect', side_effect=AssertionError('Already completed')):
            self.assertEqual(self.rt.recover_adapters(), [{'effect_id': effect['id'], 'status': 'published'}])
        self.assertEqual(verify.call_count, 1)

    def test_completion_records_survive_store_close_and_runtime_restart(self):
        self.model('before-restart', delta={'next_step': 'Keep the persisted observation'})
        self.adapter('tool-before-restart')
        before = (self.event_count(), self.markers(), self.store.list('r', 'effect'),
                  self.store.list('r', 'budget'), self.store.list('r', 'working_memory'))
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.rt = Runtime(self.store, 'r', adapters=[make_adapter()])

        self.assertEqual(len(self.markers()), 2)
        self.assert_steady_recovery(1, 1)
        self.assertEqual(before, (self.event_count(), self.markers(), self.store.list('r', 'effect'),
                                 self.store.list('r', 'budget'), self.store.list('r', 'working_memory')))
        self.assertEqual(len(EXECUTIONS), 1)

    def test_legacy_history_replays_once_and_marks_without_reexecuting(self):
        with patch.object(self.rt, '_record_completion', return_value=None):
            self.model('old-model', delta={'next_step': 'Remember the old observation'})
            self.adapter('old-tool')
        self.assertEqual(self.markers(), [])
        effects, budgets = self.store.list('r', 'effect'), self.store.list('r', 'budget')
        with patch.object(self.rt, 'apply_meta', wraps=self.rt.apply_meta) as model, \
                patch.object(self.rt, 'publish_adapter_effect', wraps=self.rt.publish_adapter_effect) as adapter:
            result = self.rt.recover()
        self.assertEqual((model.call_count, adapter.call_count), (1, 1))
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(self.markers()), 2)
        self.assertEqual(effects, self.store.list('r', 'effect'))
        self.assertEqual(budgets, self.store.list('r', 'budget'))
        self.assertEqual(len(EXECUTIONS), 1)
        self.assert_steady_recovery(1, 1)

    def test_processor_version_change_requires_one_new_postprocessing_pass(self):
        self.model('versioned-model')
        self.adapter('versioned-tool')
        with patch('rsi_lab.runtime.COMPLETION_VERSION', 2):
            with patch.object(self.rt, 'apply_meta', wraps=self.rt.apply_meta) as model, \
                    patch.object(self.rt, 'publish_adapter_effect', wraps=self.rt.publish_adapter_effect) as adapter:
                self.rt.recover()
            self.assertEqual((model.call_count, adapter.call_count), (1, 1))
            self.assertEqual(len(self.markers()), 4)
            self.assert_steady_recovery(1, 1)
        self.assertEqual(len(EXECUTIONS), 1)

    def test_publication_complete_is_not_done_until_claim_cleanup_commits(self):
        mutate = self.rt._mutate
        def crash_before_claims(key, event, fn, payload):
            if key.startswith('claims-publication:'):
                raise KeyboardInterrupt('Crash after publication complete')
            return mutate(key, event, fn, payload)
        with patch.object(self.rt, '_mutate', side_effect=crash_before_claims):
            with self.assertRaises(KeyboardInterrupt):
                self.adapter('claim-gap')
        effect = self.store.list('r', 'effect')[0]
        publication = self.store.get('r', 'registration', identity('publication', effect['id']))
        self.assertEqual(publication['status'], 'complete')
        self.assertEqual(self.store.list('r', 'registration', kind='work_identity_claim')[0]['status'], 'held')
        self.assertEqual(self.markers(), [])
        with patch.object(self.rt, 'publish_adapter_effect', wraps=self.rt.publish_adapter_effect) as publish:
            result = self.rt.recover()
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.store.list('r', 'registration', kind='work_identity_claim')[0]['status'], 'covered')
        claim_seq = self.store.db.execute('SELECT seq FROM events WHERE run_id=? AND operation_key=?',
                                         ('r', 'claims-publication:' + effect['id'])).fetchone()[0]
        self.assertGreater(self.markers()[0]['created_seq'], claim_seq)
        self.assert_steady_recovery(0, 1)
        self.assertEqual(len(EXECUTIONS), 1)

    def test_model_receipt_crash_recovers_memory_and_proposal_before_completion(self):
        with patch.object(self.rt, '_commit_agent_turn', side_effect=KeyboardInterrupt('Before memory commit')):
            with self.assertRaises(KeyboardInterrupt):
                self.model('receipt-gap', delta={'next_step': 'Use the recovered result'})
        effect = self.store.list('r', 'effect')[0]
        turn_id = effect['metadata']['agent_turn_id']
        self.assertNotEqual(self.store.get('r', 'agent_turn', turn_id)['status'], 'committed')
        self.assertEqual(self.markers(), [])
        result = self.rt.recover()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['proposals'][0]['status'], 'applied')
        self.assertEqual(self.store.get('r', 'agent_turn', turn_id)['status'], 'committed')
        memories = self.store.list('r', 'working_memory')
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]['content']['next_step'], 'Use the recovered result')
        self.assertEqual(len(self.markers()), 1)
        self.assert_steady_recovery(1, 0)

    def test_committed_memory_does_not_hide_unapplied_proposal(self):
        with patch.object(self.rt, 'apply_meta', side_effect=KeyboardInterrupt('Before routing')):
            with self.assertRaises(KeyboardInterrupt):
                self.model('route-gap', delta={'next_step': 'Keep committed private memory'})
        self.assertEqual(self.store.list('r', 'agent_turn')[0]['status'], 'committed')
        self.assertEqual(self.store.list('r', 'proposal'), [])
        self.assertEqual(self.markers(), [])
        result = self.rt.recover()
        self.assertEqual(result['proposals'][0]['status'], 'applied')
        self.assertEqual(len(self.store.list('r', 'working_memory')), 1)
        self.assertEqual(len(self.markers()), 1)
        self.assert_steady_recovery(1, 0)

    def test_missing_completion_after_last_commit_is_recoverable(self):
        with patch.object(self.rt, '_record_completion', side_effect=KeyboardInterrupt('Before completion record')):
            with self.assertRaises(KeyboardInterrupt):
                self.model('completion-gap', delta={'next_step': 'Already committed'})
        self.assertEqual(self.store.list('r', 'proposal')[0]['status'], 'applied')
        self.assertEqual(self.store.list('r', 'agent_turn')[0]['status'], 'committed')
        self.assertEqual(self.markers(), [])
        self.rt.recover()
        self.assertEqual(len(self.markers()), 1)
        self.assertEqual(len(self.store.list('r', 'working_memory')), 1)
        self.assert_steady_recovery(1, 0)

    def test_failure_policy_error_cannot_mark_turn_complete_before_retry_is_durable(self):
        work = self.rt.enqueue_lab('failed-provider')
        effect = self.rt.prepare_meta(work['id'], self.reservations)
        model = FakeModel('no_action', {'reason': 'Fixture failure'})
        def definite_failure(effect, packet):
            return {**model(effect, packet), 'status': 'failed_definite', 'error': 'Known local fixture failure'}
        self.controller.dispatch_effect('r', effect['id'], definite_failure)
        with patch.object(self.rt, '_schedule_retry', side_effect=OSError('Retry enqueue interrupted')):
            first = self.rt.recover()
        self.assertEqual(first['errors'][0]['error_type'], 'OSError')
        self.assertEqual(self.store.list('r', 'agent_turn')[0]['status'], 'committed')
        self.assertEqual(self.markers(), [])
        self.assertEqual(len(self.store.list('r', 'work', kind='lab')), 1)
        second = self.rt.recover()
        self.assertEqual(second['errors'], [])
        successors = [w for w in self.store.list('r', 'work') if w['inputs'].get('retry_of') == work['id']]
        self.assertEqual(len(successors), 1)
        self.assertEqual(successors[0]['inputs']['retry_attempt'], 1)
        self.assertEqual(len(self.markers()), 1)
        self.assert_steady_recovery(0, 0)
