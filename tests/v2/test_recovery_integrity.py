"""Completed-effect recovery still authenticates the complete ledger boundary.

All calls use temporary ledgers, a synthetic model and the numeric fixture
adapter. Corruption deliberately leaves the event head and completion records
unchanged, so a completion marker cannot masquerade as history verification.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from rsi_lab.controller import Controller, identity
from rsi_lab.decision_dependencies import StaleDecision
from rsi_lab.runtime import COMPLETION_KIND, Runtime
from rsi_lab.store import Store, encode
from tests.generic import test_adapter_boundary as fixture
from tests.v2.test_improvements import FakeModel


class RecoveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'run'
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.control = Controller(self.store)
        self.control.initialize('r', budgets={name: {'limit': 100, 'hard': name.endswith('calls')}
            for name in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds')},
            config={'execution_mode': 'replay', 'max_input_bytes': 500000,
                    'research_features': {'scientific_memory': True, 'meta_lab': True}})
        self.control.add_fact('r', 'original', 'raw_fact', {'observation': 'original'},
            {'source': 'synthetic fixture', 'question_id': 'q'}, 'fixture:source')
        self.runtime = Runtime(self.store, 'r', adapters=[fixture.fixture_adapter()])
        self.runtime.scientific_memory.observe_fact('original')
        self.reservations = {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1}
        self.model = FakeModel('no_action', {'reason': 'Synthetic observation recorded'})
        work = self.runtime.enqueue_lab('initial', ['original'])
        model_result = self.runtime.run_lab(work['id'], self.model, self.reservations)
        self.model_effect = model_result['effect']
        self.assertEqual(model_result['proposal']['status'], 'applied')
        fixture.EXECUTIONS.clear()
        tool_result = self.runtime.execute_adapter('numeric.fixture', {'value': 7}, trigger_id='initial-tool')
        self.tool_effect = tool_result['effect']
        self.assertEqual(tool_result['result'], {'value': 7})
        self.assertEqual(self.runtime.recover()['errors'], [])
        self.markers = self.store.list('r', 'registration', kind=COMPLETION_KIND)
        self.assertEqual({(row['phase'], row['effect_id']) for row in self.markers},
                         {('model', self.model_effect['id']), ('adapter', self.tool_effect['id'])})
        before = self.event_head()
        self.assertEqual(self.runtime.recover()['errors'], [])
        self.assertEqual(self.event_head(), before)
        self.assertEqual(len(fixture.EXECUTIONS), 1)
        self.assertEqual(len(self.model.inputs), 1)

    def event_head(self):
        row = self.store.db.execute(
            'SELECT seq,event_hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1', ('r',)).fetchone()
        return tuple(row)

    def corrupt(self, kind):
        """Return an exact restoration callback; no ledger mutation is used."""
        if kind in {'same_connection', 'other_connection'}:
            raw = self.store.db.execute(
                'SELECT document FROM v2_fact WHERE run_id=? AND id=?', ('r', 'original')).fetchone()[0]
            altered = json.loads(raw)
            altered['provenance']['source'] = 'unrecorded projection edit'
            if kind == 'same_connection':
                self.store.db.execute('UPDATE v2_fact SET document=? WHERE run_id=? AND id=?',
                                      (encode(altered).decode(), 'r', 'original'))
            else:
                with closing(sqlite3.connect(self.root / 'ledger.sqlite3', isolation_level=None)) as other:
                    other.execute('UPDATE v2_fact SET document=? WHERE run_id=? AND id=?',
                                  (encode(altered).decode(), 'r', 'original'))
            return lambda: self.store.db.execute(
                'UPDATE v2_fact SET document=? WHERE run_id=? AND id=?', (raw, 'r', 'original'))
        if kind == 'historical_blob' or kind.startswith('nested_'):
            # An old payload that is unrelated to either Effect's completion
            # basis must still be checked on the next public recovery call.
            if kind == 'historical_blob':
                ref = self.store.db.execute('SELECT payload_ref FROM events WHERE run_id=? AND operation_key=?',
                                            ('r', 'fixture:source')).fetchone()[0]
            else:
                publication = self.store.get('r', 'registration', identity('publication', self.tool_effect['id']))
                refs = {'nested_tool_packet': self.tool_effect['packet_ref'],
                        'nested_tool_result': self.tool_effect['result_ref'],
                        'nested_tool_publication': publication['publication_ref'],
                        'nested_model_receipt': self.model_effect['result_ref']}
                ref = refs[kind]
                direct_refs = {value for row in self.store.db.execute(
                    'SELECT payload_ref,result_ref,changes_ref FROM events WHERE run_id=?', ('r',)) for value in row}
                self.assertNotIn(ref, direct_refs, 'Fixture must exercise a blob outside direct event references')
            path = self.store.blobs / ref
            original = path.read_bytes()
            path.chmod(0o644)
            path.write_bytes(b'{"corrupted_historical_payload":true}')
            def restore():
                path.write_bytes(original)
                path.chmod(0o444)
            return restore
        raise AssertionError('Unsupported corruption fixture')

    def assert_corruption_rejected(self, action, kind, *, adapter_report=False):
        before = self.event_head()
        restore = self.corrupt(kind)
        try:
            self.assertEqual(self.event_head(), before)
            self.assertEqual(self.store.list('r', 'registration', kind=COMPLETION_KIND), self.markers)
            if adapter_report:
                # recover_adapters historically reports per-Effect validation
                # errors; a verification failure at entry may instead raise.
                try:
                    result = action()
                except ValueError as exc:
                    self.assertRegex(str(exc), 'projections differ|Blob content changed')
                else:
                    self.assertEqual(len(result), 1, result)
                    self.assertEqual(result[0]['effect_id'], self.tool_effect['id'])
                    self.assertEqual(result[0]['status'], 'blocked_adapter')
                    self.assertRegex(result[0]['reason'], 'projections differ|Blob content changed')
            else:
                with self.assertRaisesRegex(ValueError, 'projections differ|Blob content changed'):
                    action()
            self.assertEqual(self.event_head(), before)
            self.assertEqual(len(fixture.EXECUTIONS), 1)
            self.assertEqual(len(self.model.inputs), 1)
        finally:
            restore()
        self.store.verify_history('r')

    def test_completed_recovery_rejects_same_connection_projection_edit(self):
        self.assert_corruption_rejected(self.runtime.recover, 'same_connection')

    def test_completed_recovery_rejects_other_connection_projection_edit(self):
        self.assert_corruption_rejected(self.runtime.recover, 'other_connection')

    def test_completed_recovery_rejects_old_blob_corruption_without_head_change(self):
        self.assert_corruption_rejected(self.runtime.recover, 'historical_blob')

    def assert_nested_blob_rejected(self, action, kind, *, adapter_report=False):
        before = self.event_head()
        restore = self.corrupt(kind)
        try:
            # Whole-history verification hashes direct event blobs, not every
            # content-addressed reference recursively contained in those blobs.
            self.store.verify_history('r')
            self.assertEqual(self.event_head(), before)
            self.assertEqual(self.store.list('r', 'registration', kind=COMPLETION_KIND), self.markers)
            effect_id = self.model_effect['id'] if kind == 'nested_model_receipt' else self.tool_effect['id']
            try:
                result = action()
            except ValueError as exc:
                self.assertIn('Blob content changed', str(exc))
            else:
                failures = result if adapter_report else (
                    result['errors'] + result['agent_recovery'] + result['adapter_results'])
                self.assertTrue(any(row.get('id', row.get('effect_id')) == effect_id
                    and 'Blob content changed' in row.get('reason', '')
                    and row.get('status') != 'published' for row in failures), result)
            self.assertEqual(self.event_head(), before)
            self.assertEqual(len(fixture.EXECUTIONS), 1)
            self.assertEqual(len(self.model.inputs), 1)
        finally:
            restore()
        self.store.verify_history('r')

    def test_completed_recovery_rechecks_tool_packet_result_and_publication_blobs(self):
        for kind in ('nested_tool_packet', 'nested_tool_result', 'nested_tool_publication'):
            with self.subTest(corruption=kind):
                self.assert_nested_blob_rejected(self.runtime.recover, kind)

    def test_completed_recovery_rechecks_model_receipt_blob(self):
        self.assert_nested_blob_rejected(self.runtime.recover, 'nested_model_receipt')

    def test_independent_adapter_recovery_rechecks_nested_blobs_before_marker_skip(self):
        for kind in ('nested_tool_packet', 'nested_tool_result', 'nested_tool_publication'):
            with self.subTest(corruption=kind):
                self.assert_nested_blob_rejected(self.runtime.recover_adapters, kind, adapter_report=True)

    def test_independent_apply_meta_authenticates_history_before_terminal_proposal_return(self):
        for kind in ('same_connection', 'other_connection', 'historical_blob'):
            with self.subTest(corruption=kind):
                self.assert_corruption_rejected(lambda: self.runtime.apply_meta(self.model_effect['id']), kind)

    def test_independent_publication_authenticates_history_before_completed_result_return(self):
        for kind in ('same_connection', 'other_connection', 'historical_blob'):
            with self.subTest(corruption=kind):
                self.assert_corruption_rejected(lambda: self.runtime.publish_adapter_effect(self.tool_effect), kind)

    def test_independent_adapter_recovery_authenticates_history_before_completed_marker_skip(self):
        for kind in ('same_connection', 'other_connection', 'historical_blob'):
            with self.subTest(corruption=kind):
                self.assert_corruption_rejected(self.runtime.recover_adapters, kind, adapter_report=True)

    def test_independent_controller_recovery_authenticates_history_with_no_unsettled_effects(self):
        for kind in ('same_connection', 'other_connection', 'historical_blob'):
            with self.subTest(corruption=kind):
                self.assert_corruption_rejected(lambda: self.control.recover('r'), kind)

    def test_completed_publication_still_rejects_a_forged_effect(self):
        forged = {**self.tool_effect, 'packet_hash': '0' * 64}
        before = self.event_head()
        with self.assertRaisesRegex(ValueError, 'actual succeeded, reconciled persisted Effect'):
            self.runtime.publish_adapter_effect(forged)
        self.assertEqual(self.event_head(), before)

    def new_lab_work(self, trigger):
        return self.control.enqueue('r', 'lab', trigger, 'work:' + trigger, inputs={
            'evidence_ids': ['original'], 'required_evidence_ids': ['original'],
            'decision_dependency_selectors': [{'kind': 'fact',
                'where': {'provenance.question_id': 'q'}, 'fields': ['kind', 'content_ref']}]})

    def test_completed_recovery_does_not_weaken_prepare_transaction_freshness(self):
        work = self.new_lab_work('prepare-race')
        effects_before = self.store.list('r', 'effect')
        original = self.runtime.controller.prepare_effect
        def race(*args, **kwargs):
            self.control.add_fact('r', 'racing-fact', 'raw_fact', {'observation': 'new'},
                {'source': 'synthetic fixture', 'question_id': 'q'}, 'fixture:prepare-race')
            return original(*args, **kwargs)
        with patch.object(self.runtime.controller, 'prepare_effect', side_effect=race):
            with self.assertRaises(StaleDecision):
                self.runtime.prepare_meta(work['id'], self.reservations)
        self.assertEqual(self.store.list('r', 'effect'), effects_before)
        self.assertTrue(all(row['reserved'] == 0 for row in self.store.list('r', 'budget')))
        self.assertEqual(self.store.get('r', 'work', work['id'])['status'], 'ready')
        self.store.verify_history('r')

    def test_completed_recovery_does_not_weaken_dispatch_history_verification(self):
        work = self.new_lab_work('dispatch-corruption')
        effect = self.runtime.prepare_meta(work['id'], self.reservations)
        executor = Mock(side_effect=AssertionError('Corrupt history must never reach an executor'))
        for kind in ('same_connection', 'other_connection', 'historical_blob'):
            with self.subTest(corruption=kind):
                self.assert_corruption_rejected(
                    lambda: self.control.dispatch_effect('r', effect['id'], executor), kind)
                executor.assert_not_called()
                self.assertEqual(self.store.get('r', 'effect', effect['id'])['status'], 'prepared')

    def test_completed_recovery_does_not_weaken_dispatch_dependency_validation(self):
        work = self.new_lab_work('dispatch-race')
        effect = self.runtime.prepare_meta(work['id'], self.reservations)
        self.control.add_fact('r', 'dispatch-fact', 'raw_fact', {'observation': 'new'},
            {'source': 'synthetic fixture', 'question_id': 'q'}, 'fixture:dispatch-race')
        executor = Mock(side_effect=AssertionError('A stale packet must never reach an executor'))
        before = self.event_head()
        with self.assertRaises(StaleDecision):
            self.control.dispatch_effect('r', effect['id'], executor)
        executor.assert_not_called()
        self.assertEqual(self.event_head(), before)
        self.assertEqual(self.store.get('r', 'effect', effect['id'])['status'], 'prepared')
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)
        self.store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
