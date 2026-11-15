"""Continuation v2 schema-repair regression tests; no model or oracle calls."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from tests import test_pablo_continuation as fixtures
from scripts import continue_pablo_rsi_v2 as continuation
from rsi_lab.controller import Controller, identity
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest


class PabloContinuationV2Tests(fixtures.PabloContinuationTests):
    def setUp(self):
        binding = patch.object(fixtures, 'continuation', continuation)
        binding.start()
        self.addCleanup(binding.stop)
        super().setUp()

    def model_terminal(self, store, work, command, tag):
        """Create a certified synthetic terminal; routing below uses the real core."""
        session = store.list('parent', 'agent_session', role='meta')[0]
        eid, tid = 'synthetic-effect-' + tag, 'synthetic-turn-' + tag
        packet_ref = store.put_blob({'prompt': 'Synthetic test only'})
        costs = {'model_calls': 1, 'tokens': 100}
        receipt = {'status': 'succeeded', 'actual_costs': costs, 'terminal_received': True,
            'protocol_valid': True, 'native_writer_quiesced': True,
            'sent_packet_sha256': packet_ref, 'sent_prompt_sha256': digest(b'Synthetic test only'),
            'response': {'command': command}}
        result_ref = store.put_blob(receipt)
        def terminal(tx):
            tx.update('work', work['id'], {'status': 'succeeded', 'effect_id': eid, 'outcome_ref': result_ref})
            tx.insert('agent_turn', tid, {'status': 'committed', 'work_id': work['id'],
                'session_id': session['id'], 'owner_id': session['owner_id'], 'effect_ids': [eid],
                'terminal_ref': result_ref, 'request_results': {eid: {'status': 'succeeded', 'receipt_hash': result_ref}}})
            for resource, cost in costs.items():
                tx.update('budget', resource, {'spent': tx.get('budget', resource)['spent'] + cost})
            return tx.insert('effect', eid, {'kind': 'meta', 'issue_id': self.iid, 'work_id': work['id'],
                'status': 'succeeded', 'accounting_unknown': False, 'actual_costs': costs,
                'reservations': costs, 'held_reservations': {key: 0 for key in costs},
                'metadata': {'agent_turn_id': tid, 'agent_session_id': session['id']},
                'packet_ref': packet_ref, 'receipt_ref': result_ref, 'result_ref': result_ref})
        return store.mutate('parent', 'terminal:' + tag, 'synthetic_terminal', terminal)

    def admit(self, store, runtime, effect, command, error=None):
        pid = identity('proposal', effect['id'])
        def admit(tx):
            proposal = tx.insert('proposal', pid, {'effect_id': effect['id'], 'issue_id': self.iid,
                'status': 'rejected' if error else 'admitted', 'command': None if error else command,
                'error': error, 'delivery_ref': tx.blob({'fixture': 'delivered packet'}),
                'result_ref': None, 'initial_review_ref': None})
            if error:
                runtime._repair(tx, self.iid, pid, error)
            return proposal
        return store.mutate('parent', 'admit:' + pid, 'meta_proposal_admitted', admit)

    def apply_route(self, store, runtime, proposal):
        result = runtime._route(proposal)
        return store.mutate('parent', 'applied:' + proposal['id'], 'meta_proposal_applied',
            lambda tx: tx.update('proposal', proposal['id'], {'status': 'applied', 'result_ref': tx.blob(result)}))

    def add_rejection_chain(self, *, repair=True, close=True):
        store = Store(self.parent)
        try:
            runtime = Runtime(store, 'parent')
            c = Controller(store)
            self.iid = 'synthetic-issue'
            c.open_issue('parent', self.iid, ['test', 'synthetic'], 'Unproven local ties', 'observed scores',
                         [], ['feature equivalence'], ['observe distinct scores'], 'fixture-issue')
            command = {'action': 'request_evidence', 'issue_id': self.iid, 'mode': 'disposition',
                'evidence_ids': [], 'expected_versions': {'issue_version': 1, 'evidence_revision': 0, 'scheduling_revision': 0},
                'payload': {'query': '', 'source_id': 'initial_observations', 'source_version': 1,
                            'object_ids': ['initial-observations.json'], 'fields': ['original_text'],
                            'max_pages': 1, 'max_bytes': 262144}}
            work = store.list('parent', 'work', issue_id=self.iid, trigger='issue_created')[0]
            source = self.model_terminal(store, work, command, 'rejected')
            rejected = self.admit(store, runtime, source, command, 'Command text cannot be empty')
            self.rejected_id, self.source_effect_id = rejected['id'], source['id']
            if not repair:
                c.close_issue('parent', self.iid, 'closed_unresolved', 'Fixture missing repair', 'fixture-unrepaired-close')
                return
            fixed = copy.deepcopy(command)
            fixed['payload']['query'] = 'Deliver the original frozen table without a summary.'
            repair_work = store.list('parent', 'work', issue_id=self.iid, trigger='repair:' + rejected['id'])[0]
            effect = self.model_terminal(store, repair_work, fixed, 'repair')
            applied = self.apply_route(store, runtime, self.admit(store, runtime, effect, fixed))
            route = json.loads(store.read_blob(applied['result_ref']))
            original = b'{"synthetic": "source observation table"}\n'
            original_ref = store.put_blob(original)
            content = {'original_text': original.decode(), 'original_source_sha256': original_ref}
            provenance = {'source_id': 'initial_observations', 'original_source_sha256': original_ref}
            delivery = runtime.ingest_delivery(route['request_id'], content, provenance)
            self.delivery_id = delivery['id']
            if not close:
                return
            work = store.list('parent', 'work', issue_id=self.iid, status='ready')[0]
            command = {'action': 'close_issue', 'issue_id': self.iid, 'mode': 'disposition',
                'evidence_ids': [delivery['id']], 'expected_versions': runtime.versions(self.iid),
                'payload': {'resolution': 'closed_unresolved', 'reason': 'Raw evidence is inconclusive.'}}
            effect = self.model_terminal(store, work, command, 'close')
            self.apply_route(store, runtime, self.admit(store, runtime, effect, command))
        finally:
            store.close()

    def test_cost_sources_and_colliding_attempt_ids_remain_separate(self):
        continuation.prepare(self.args())
        totals = json.loads((self.child / 'cumulative-costs.json').read_text())
        self.assertEqual(totals['current_segment_costs']['tool_calls'], 0)
        self.assertEqual(totals['prior_segment_costs']['tool_calls'], 10)
        self.assertEqual(totals['prior_attempt_costs']['model_calls'], 2)
        self.assertEqual(totals['cumulative_known_costs']['tokens'], 30)
        self.assertEqual(len(totals['prior_attempts']), 2)
        manifest = json.loads((self.child / 'manifest.json').read_text())
        self.assertIn('scripts/continue_pablo_rsi_v2.py', manifest['source_hashes'])
        self.assertIn('scripts/run_pablo_rsi.py', manifest['source_hashes'])

    def test_completed_schema_repair_is_preserved_without_losing_costs(self):
        self.add_rejection_chain()
        before = {str(p.relative_to(self.parent)): digest(p.read_bytes())
                  for p in self.parent.rglob('*') if p.is_file()}
        continuation.prepare(self.args())
        after = {str(p.relative_to(self.parent)): digest(p.read_bytes())
                 for p in self.parent.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        lineage = json.loads((self.child / 'lineage.json').read_text())
        dispositions = lineage['rejection_dispositions']
        self.assertEqual(len(dispositions), 1)
        self.assertEqual(dispositions[0]['proposal_id'], self.rejected_id)
        self.assertEqual(dispositions[0]['original_status'], 'rejected')
        child = continuation.ReadOnlySnapshot(self.child)
        try:
            chain = json.loads(child.read_blob(dispositions[0]['repair_chain_sha256']))
            self.assertEqual(chain['original_proposal']['status'], 'rejected')
            self.assertIsNone(chain['original_proposal']['command'])
            self.assertIsNone(chain['original_proposal']['result_ref'])
            self.assertEqual(len(chain['effect_result_refs']), 3)
            self.assertEqual(child.list('child', 'proposal'), [])
        finally:
            child.close()
        totals = json.loads((self.child / 'cumulative-costs.json').read_text())
        self.assertEqual(totals['prior_segment_costs']['model_calls'], 3)
        self.assertEqual(totals['prior_segment_costs']['tokens'], 300)
        self.assertEqual(totals['cumulative_known_costs']['model_calls'], 5)
        self.assertEqual(totals['current_segment_costs']['model_calls'], 0)
        parent = continuation.load_parent(self.parent, 'parent')
        parent['manifest']['continuation_lineage'] = {'rejection_dispositions': [dispositions[0]]}
        parent['rejection_dispositions'] = []
        self.assertEqual(continuation.make_lineage(parent, 200)['rejection_dispositions'], dispositions)

    def test_closed_issue_without_applied_core_repair_is_rejected(self):
        self.add_rejection_chain(repair=False)
        with self.assertRaisesRegex(ValueError, 'no applied repair proposal'):
            continuation.prepare(self.args())
        self.assertFalse(self.child.exists())

    def test_repaired_request_with_open_issue_is_rejected(self):
        self.add_rejection_chain(close=False)
        with self.assertRaisesRegex(ValueError, 'open issues'):
            continuation.prepare(self.args())
        self.assertFalse(self.child.exists())

    def test_unknown_source_still_blocks_even_after_repair(self):
        self.add_rejection_chain()
        store = Store(self.parent)
        try:
            store.mutate('parent', 'fixture-unknown-source', 'fixture_unknown', lambda tx:
                tx.update('effect', self.source_effect_id, {'status': 'unknown', 'accounting_unknown': True}))
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, 'unknown execution'):
            continuation.prepare(self.args())

    def test_summary_cannot_substitute_for_original_delivery(self):
        self.add_rejection_chain()
        store = Store(self.parent)
        try:
            store.mutate('parent', 'fixture-summary', 'fixture_changed_delivery', lambda tx:
                tx.update('delivery', self.delivery_id, {'kind': 'summary'}))
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, 'original raw evidence'):
            continuation.prepare(self.args())

    def test_native_writer_without_quiescence_is_rejected(self):
        self.add_rejection_chain()
        store = Store(self.parent)
        try:
            effect = store.get('parent', 'effect', self.source_effect_id)
            receipt = json.loads(store.read_blob(effect['result_ref']))
            receipt['native_writer_quiesced'] = False
            store.mutate('parent', 'fixture-live-writer', 'fixture_changed_receipt', lambda tx:
                tx.update('effect', self.source_effect_id, {'result_ref': tx.blob(receipt)}))
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, 'certified terminal'):
            continuation.prepare(self.args())

    def test_rejection_with_route_side_effects_is_rejected(self):
        self.add_rejection_chain()
        store = Store(self.parent)
        try:
            store.mutate('parent', 'route:' + self.rejected_id + ':unexpected', 'fixture_route', lambda tx: {})
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, 'route side effects'):
            continuation.prepare(self.args())


if __name__ == '__main__':
    unittest.main()
