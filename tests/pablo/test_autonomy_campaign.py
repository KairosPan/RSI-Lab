"""Campaign host contracts only: no model calls or new objective evaluations."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts import run_literature_autonomy_campaign as campaign


INITIAL_CSV = Path(os.environ.get('PABLO_INITIAL_CSV', str(campaign.DEFAULT_INITIAL)))
READY = INITIAL_CSV.is_file() and all(importlib.util.find_spec(name) is not None
    for name in ('rdkit', 'guacamol', 'numpy', 'scipy'))


@unittest.skipUnless(READY, 'Original source and optional scientific environment required')
class CampaignHostTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'fresh'
        self.runtime = campaign.initialize(self.root, INITIAL_CSV,
            campaign.PROJECT / 'examples/literature-tools.json', 1200)
        self.addCleanup(self.runtime.store.close)

    def test_initial_context_is_complete_and_does_not_expose_hidden_objective(self):
        work, prepared = campaign.select_work(self.runtime, None)
        self.assertIsNone(prepared)
        work = campaign.bind_observations(self.runtime, work)
        plan, task = campaign.packet_preflight(self.runtime, work, self.root)
        snapshots = [json.loads(page['content']) for page in plan['delivery_pages']
                     if page['evidence_id'].startswith('campaign_state_')]
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]['initial_count'], 100)
        self.assertEqual(snapshots[0]['new_evaluations'], 0)
        self.assertEqual(snapshots[0]['remaining_evaluations'], 200)
        self.assertEqual(len(snapshots[0]['candidates']), 100)
        self.assertEqual(snapshots[0]['evaluation_receipts'], [])
        self.assertEqual(task['literature_context']['run_allowance']['model_calls'], 200)
        self.assertNotIn('amlodipine', plan['envelope']['prompt'].lower())
        self.assertNotIn('"adip"', plan['envelope']['prompt'])
        self.assertEqual(self.runtime.store.list(campaign.RUN_ID, 'effect'), [])

    def test_resume_keeps_frozen_configuration_and_budget(self):
        before = self.runtime.store.run(campaign.RUN_ID)
        other = campaign.load_runtime(self.root)
        try:
            self.assertEqual(other.store.run(campaign.RUN_ID), before)
            self.assertTrue(all(row['spent'] == row['reserved'] == 0
                                for row in other.store.list(campaign.RUN_ID, 'budget')))
        finally:
            other.store.close()

    def test_modified_source_is_not_accepted_on_resume(self):
        source = self.root / 'inputs/original-source.csv'
        source.write_bytes(source.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'Frozen campaign'):
            campaign.load_runtime(self.root)
        self.assertEqual(self.runtime.store.list(campaign.RUN_ID, 'effect'), [])

    def test_resume_does_not_create_a_missing_ledger(self):
        absent = self.root / 'never-created'
        with self.assertRaisesRegex(ValueError, 'existing initialized'):
            campaign.load_runtime(absent)
        self.assertFalse(absent.exists())

    def test_zero_model_initialization_cannot_pass_campaign_audit(self):
        audit = campaign.audit(self.runtime)
        self.assertFalse(audit['workflow_passed'])
        self.assertTrue(audit['checks']['frozen_inputs_code_and_configuration'])
        self.assertTrue(audit['checks']['initial_100'])
        self.assertFalse(audit['checks']['new_200'])
        self.assertFalse(audit['checks']['all_model_turns_certified'])
        self.assertFalse(audit['checks']['no_pending_controller_work'])
        self.assertEqual(audit['models'], [])

    def test_local_source_uses_registered_fact_identity_and_bounded_fields(self):
        control = self.runtime.controller
        control.open_issue(campaign.RUN_ID, 'fixture_issue', 'fixture_scope', 'Verify contract',
            'Offline source contract test', ['campaign_contract'], ['Record may differ'],
            ['Read the exact contract'], 'fixture-open')
        query = {'source_id': campaign.LOCAL_SOURCE, 'source_version': 1,
            'object_ids': ['campaign_contract'], 'fields': ['content', 'content_ref'],
            'query': 'Read the registered public contract', 'max_pages': 1, 'max_bytes': 10000}
        control.request(campaign.RUN_ID, 'fixture_issue', 'fixture_request', query['query'],
            campaign.LOCAL_SOURCE, {'object_ids': query['object_ids']}, 'fixture-request', query_spec=query)
        work = control.enqueue(campaign.RUN_ID, 'evidence_retrieve', 'fixture-source', 'fixture-work',
            inputs={'issue_id': 'fixture_issue', 'request_id': 'fixture_request', 'query_spec': query})
        delivered = campaign.fulfill_registered_source(self.runtime, work)
        value = json.loads(self.runtime.store.read_blob(delivered['content_ref']))
        self.assertEqual(value['records'][0]['evidence_id'], 'campaign_contract')
        self.assertEqual(value['records'][0]['content']['new_evaluation_limit'], 200)
        self.assertEqual(self.runtime.store.get(campaign.RUN_ID, 'work', work['id'])['status'], 'succeeded')
        # An existing private blob is not a registered fact and is not readable
        # through the public producer even if a requester knows its hash.
        private = self.runtime.store.put_blob({'secret_fixture': 'not a public fact'})
        bad = {**work, 'inputs': {**work['inputs'], 'query_spec': {**query, 'object_ids': [private]}}}
        with self.assertRaises(KeyError):
            campaign.fulfill_registered_source(self.runtime, bad)
        self.assertEqual(self.runtime.store.list(campaign.RUN_ID, 'effect'), [])


if __name__ == '__main__':
    unittest.main()
