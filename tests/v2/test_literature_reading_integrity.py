"""Reading coverage must authenticate the pages actually sent to this child."""
import copy
import json
import unittest
from unittest.mock import patch

from rsi_lab.literature_reading import certified_reads, source_rows
from rsi_lab.store import encode
from tests.v2 import test_literature_runtime as runtime_fixture


class LiteratureReadingIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_fixture.LiteratureRuntimeTests(
            'test_full_loop_uses_child_session_and_delivers_originals_to_same_lab')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        fixture.delegate(max_model_calls=6, max_tool_calls=8)
        fixture.request_tool()
        fixture.execute_tool()
        self.older = fixture.ready('literature')['inputs']['evidence_ids'][0]
        observed = fixture.request_tool()
        self.effect = observed['effect']
        fixture.execute_tool()
        self.latest = fixture.ready('literature')['inputs']['evidence_ids'][0]
        self.store = fixture.store
        self.task = fixture.task()
        self.rows = source_rows(self.store, 'r', self.task)
        self.keys = {row['id']: row['source_key'] for row in self.rows}
        self.plan = json.loads(self.store.read_blob(self.effect['metadata']['plan_ref']))

    def coverage(self, task=None):
        return certified_reads(self.store, 'r', task or self.task, self.rows)

    def blob_view(self, ref, value):
        # Simulate an inconsistent read view without mutating immutable blobs,
        # projections or the audited event history.
        read = self.store.read_blob
        return patch.object(self.store, 'read_blob', side_effect=lambda requested:
                            encode(value) if requested == ref else read(requested))

    def test_only_delivered_original_gets_coverage(self):
        covered, certificates = self.coverage()
        self.assertEqual(covered, {self.keys[self.older]})
        self.assertNotIn(self.keys[self.latest], covered)
        self.assertEqual([row['effect_id'] for row in certificates], [self.effect['id']])
        self.store.verify_history('r')

    def test_plan_cannot_append_a_cached_but_unsent_original(self):
        fixture = self.fixture
        agent, mode, task, options = fixture.runtime._context(fixture.ready('literature'))
        current = agent.prepare(mode, task, **options)
        latest_page = next(page for page in current['delivery_pages'] if page['evidence_id'] == self.latest)
        forged = copy.deepcopy(self.plan)
        forged['delivery_pages'].append(latest_page)
        with self.blob_view(self.effect['metadata']['plan_ref'], forged):
            with self.assertRaisesRegex(ValueError, 'actual dispatched original pages'):
                self.coverage()
        self.assertEqual(self.coverage()[0], {self.keys[self.older]})
        self.store.verify_history('r')

    def test_plan_envelope_must_match_actual_request(self):
        forged = copy.deepcopy(self.plan)
        forged['envelope']['prompt'] += '\nUnsent annotation'
        with self.blob_view(self.effect['metadata']['plan_ref'], forged):
            with self.assertRaisesRegex(ValueError, 'actual dispatched original pages'):
                self.coverage()
        self.store.verify_history('r')

    def test_receipt_must_bind_the_actual_request_and_prompt(self):
        receipt = json.loads(self.store.read_blob(self.effect['result_ref']))
        for field in ('sent_packet_sha256', 'sent_prompt_sha256'):
            with self.subTest(field=field):
                forged = {**receipt, field: '0' * 64}
                with self.blob_view(self.effect['result_ref'], forged):
                    with self.assertRaisesRegex(ValueError, 'authenticate its original request'):
                        self.coverage()
        self.store.verify_history('r')

    def test_source_version_change_is_not_historical_read_authority(self):
        get = self.store.get
        def changed(run_id, kind, entity_id):
            row = get(run_id, kind, entity_id)
            return {**row, 'version': row['version'] + 1} if kind == 'fact' and entity_id == self.older else row
        with patch.object(self.store, 'get', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed since its authenticated delivery'):
                self.coverage()
        self.store.verify_history('r')

    def test_coverage_does_not_cross_tasks_or_owner_branches(self):
        self.assertEqual(self.coverage({**self.task, 'id': 'unrelated-task'}), (set(), []))
        get = self.store.get
        session_id = self.effect['metadata']['agent_session_id']
        def foreign(run_id, kind, entity_id):
            row = get(run_id, kind, entity_id)
            return {**row, 'branch_id': 'foreign'} if kind == 'agent_session' and entity_id == session_id else row
        with patch.object(self.store, 'get', side_effect=foreign):
            with self.assertRaisesRegex(ValueError, 'another owner'):
                self.coverage()
        self.store.verify_history('r')

    def test_unknown_or_unaccounted_effect_is_not_a_read_receipt(self):
        listing = self.store.list
        for changed in ({'status': 'unknown'}, {'accounting_unknown': True}):
            with self.subTest(changed=changed):
                def view(run_id, kind, **filters):
                    values = listing(run_id, kind, **filters)
                    return [{**row, **changed} if kind == 'effect' and row['id'] == self.effect['id'] else row
                            for row in values]
                with patch.object(self.store, 'list', side_effect=view):
                    self.assertEqual(self.coverage(), (set(), []))
        self.store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
