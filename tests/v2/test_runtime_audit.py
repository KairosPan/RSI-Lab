"""Runtime fault-injection audits; model responses are fixtures, not live calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docking_rsi.controller import Controller, identity
from docking_rsi.packets import digest as packet_digest
from docking_rsi.runtime import Runtime
from docking_rsi.store import Store


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'ledger')
        self.addCleanup(self.store.close)
        self.control = Controller(self.store)
        self.control.initialize('audit', budgets={
            'model_calls': {'limit': 30, 'hard': True},
            'tool_calls': {'limit': 30, 'hard': True},
            'tokens': {'limit': 100000, 'hard': False},
            'cpu_seconds': {'limit': 1000, 'hard': False},
            'wall_seconds': {'limit': 1000, 'hard': False},
        }, config={'goal': 'Screen the supplied compounds with docking',
                   'authorized_sources': ['prepared_inputs'], 'max_open_reviews': 2})
        self.runtime = Runtime(self.store, 'audit')
        self.control.add_fact('audit', 'receipt', 'raw_fact', {'preparation': 'hydrogens_added'},
                              {'source': 'fixture_input'}, 'input')
        self.control.open_issue('audit', 'i', 'condition', 'Condition is unverified', 'input set',
            ['receipt'], ['Condition may be documented elsewhere'], ['Applicable record found'], 'issue')

    def command(self, action, payload, *, mode='disposition', issue_id='i'):
        return {'command': {'mode': mode, 'action': action, 'issue_id': issue_id,
            'expected_versions': self.runtime.versions(issue_id), 'evidence_ids': ['receipt'], 'payload': payload}}

    def emit(self, response, work_id=None):
        if work_id is None:
            work_id = next(w['id'] for w in self.control.ready('audit')
                           if w['kind'] == 'meta_disposition' and w['issue_id'] == 'i')
        effect = self.runtime.prepare_meta(work_id, {'model_calls': 1, 'tokens': 100, 'wall_seconds': 1})
        def executor(current, packet):
            envelope = json.loads(packet)
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1, 'tokens': 10, 'wall_seconds': 0.01},
                'response': response, 'sent_packet_sha256': packet_digest(packet),
                'sent_prompt_sha256': packet_digest(envelope['prompt'])}
        return self.control.dispatch_effect('audit', effect['id'], executor)

    def query(self):
        return {'source_id': 'prepared_inputs', 'source_version': 1, 'object_ids': ['input_set'],
                'fields': ['preparation'], 'query': 'Retrieve the preparation receipt',
                'max_pages': 2, 'max_bytes': 10000}

    def direct_request(self):
        query = self.query()
        self.control.request('audit', 'i', 'req', query['query'], query['source_id'],
            {'object_ids': query['object_ids']}, 'request', query_spec=query)
        self.control.dependency('audit', 'req', 'prepared_inputs', {'status': 'pending'}, 'dep')
        self.control.park('audit', 'i', 'req', 'prepared_inputs', 1,
                          {'kind': 'version_changed'}, 9999999999, 'replan', 'park')

    def test_malformed_command_is_persistently_rejected_without_aborting_recovery(self):
        effect = self.emit({'command': []})
        result = self.runtime.apply_meta(effect['id'])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.store.list('audit', 'request'), [])
        self.runtime.recover()

    def test_unknown_narrowing_request_is_rejected_before_any_route_action(self):
        response = self.command('narrow_request', {'request_id': 'missing', 'request_version': 1,
                                                  'replacement': self.query()})
        effect = self.emit(response)
        result = self.runtime.apply_meta(effect['id'])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.store.list('audit', 'request'), [])

    def test_unstarted_admitted_proposal_is_rechecked_after_new_evidence(self):
        effect = self.emit(self.command('request_evidence', self.query()))
        with patch.object(self.runtime, '_route', side_effect=RuntimeError('crash before first route action')):
            with self.assertRaises(RuntimeError):
                self.runtime.apply_meta(effect['id'])
        self.control.add_fact('audit', 'new', 'raw_fact', {'different_inputs': True},
                              {'source': 'changed_input_fixture'}, 'new')
        self.control.open_issue('audit', 'i', 'condition', 'Condition is unverified', 'input set',
            ['receipt', 'new'], ['A new condition applies'], ['New preparation verified'], 'new_issue_evidence')
        self.runtime.recover()
        proposal = self.store.get('audit', 'proposal', identity('proposal', effect['id']))
        self.assertIn(proposal['status'], {'rejected', 'superseded', 'blocked', 'blocked_stale'})
        self.assertEqual(self.store.list('audit', 'request'), [])

    def test_delivery_committed_before_attachment_is_completed_on_recovery(self):
        self.direct_request()
        original = self.runtime._mutate
        def interruption(key, event, fn, payload):
            if event == 'delivered_evidence_attached':
                raise RuntimeError('crash after durable delivery')
            return original(key, event, fn, payload)
        with patch.object(self.runtime, '_mutate', side_effect=interruption):
            with self.assertRaises(RuntimeError):
                self.runtime.ingest_delivery('req', {'receipt': 'provided'}, {'source': 'prepared_inputs'})
        self.assertEqual(len(self.store.list('audit', 'delivery')), 1)
        self.runtime.recover()
        did = identity('delivery', 'req', 0, 1)
        self.assertIn(did, self.store.get('audit', 'issue', 'i')['evidence_ids'])
        self.assertEqual(self.store.get('audit', 'dependency', 'req')['value']['status'], 'delivered')
        self.assertTrue(any(w['kind'] == 'meta_disposition' for w in self.control.ready('audit')))

    def test_source_provenance_cannot_overwrite_controller_delivery_binding(self):
        self.direct_request()
        delivery = self.runtime.ingest_delivery('req', {'receipt': 'provided'},
            {'source': 'prepared_inputs', 'delivery_id': 'unrelated', 'source_version': 999})
        fact = self.store.get('audit', 'fact', delivery['id'])
        self.assertEqual(fact['provenance']['delivery_id'], delivery['id'])
        self.assertEqual(fact['provenance']['source_version'], 1)

    def test_default_open_review_receives_existing_raw_facts_without_user_diagnosis(self):
        review = self.runtime.observe('batch5', 'batch_boundary', 5)
        effect = self.runtime.prepare_meta(review['work_id'], {'model_calls': 1, 'tokens': 100, 'wall_seconds': 1})
        packet = json.loads(self.store.read_blob(effect['metadata']['plan_ref']))
        ids = {page['evidence_id'] for page in packet['delivery_pages']}
        self.assertIn('receipt', ids)

    def test_recorded_fact_content_and_origin_cannot_be_mutated_in_place(self):
        original = self.store.get('audit', 'fact', 'receipt')
        for index, changes in enumerate(({'content_ref': self.store.put_blob({'replacement': True})},
                                         {'provenance': {'source': 'different'}},
                                         {'kind': 'validation'})):
            with self.assertRaisesRegex(ValueError, 'immutable'):
                self.store.mutate('audit', 'change_fact_' + str(index), 'fixture',
                                  lambda tx, changes=changes: tx.update('fact', 'receipt', changes))
        self.assertEqual(self.store.get('audit', 'fact', 'receipt'), original)
        self.store.verify_history('audit')

    def test_unauthorized_source_is_rejected_and_creates_no_request(self):
        query = {**self.query(), 'source_id': 'not_authorized'}
        effect = self.emit(self.command('request_evidence', query))
        result = self.runtime.apply_meta(effect['id'])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.store.list('audit', 'request'), [])

    def test_replayed_successful_route_does_not_duplicate_question_or_wait(self):
        effect = self.emit(self.command('ask_user', {'question': 'Which experimental condition applies?',
            'options': ['provide conditions', 'exploratory scope'], 'reason_user_needed': 'Experimental condition is user held'}))
        first = self.runtime.apply_meta(effect['id'])
        again = self.runtime.apply_meta(effect['id'])
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.list('audit', 'question')), 1)
        self.assertEqual(len(self.store.list('audit', 'wait')), 1)
        self.store.verify_history('audit')

    def test_prepared_meta_can_resume_using_its_frozen_packet_without_new_reservation(self):
        work = next(w for w in self.control.ready('audit') if w['kind'] == 'meta_disposition')
        response = self.command('ask_user', {'question': 'Which experimental condition applies?',
            'options': ['provide conditions', 'exploratory scope'], 'reason_user_needed': 'Experimental condition is user held'})
        reservations = {'model_calls': 1, 'tokens': 100, 'wall_seconds': 1}
        effect = self.runtime.prepare_meta(work['id'], reservations)
        frozen = self.store.read_blob(effect['packet_ref'])
        resumed = Runtime(self.store, 'audit')
        calls = []
        def executor(current, packet):
            calls.append(current['id'])
            self.assertEqual(packet, frozen)
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1, 'tokens': 10, 'wall_seconds': 0.01},
                'response': response, 'sent_packet_sha256': packet_digest(packet),
                'sent_prompt_sha256': packet_digest(json.loads(packet)['prompt'])}
        result = resumed.run_meta(work['id'], executor, reservations)
        self.assertEqual(result['effect']['id'], effect['id'])
        self.assertEqual(result['proposal']['status'], 'applied')
        self.assertEqual(calls, [effect['id']])
        self.assertEqual(len(self.store.list('audit', 'effect')), 1)
        self.assertEqual(self.store.get('audit', 'budget', 'model_calls')['spent'], 1)

    def test_capability_publication_recovers_without_reexecuting_the_accepted_adapter(self):
        from docking_rsi.demo import fixture_delivery
        spec, adapter, manifest, artifact = fixture_delivery(Path(self.tmp.name) / 'adapter')
        effect = self.emit(self.command('request_capability', {
            'capability_id': spec['capability_id'], 'purpose': spec['purpose'],
            'input_contract': 'parent metadata identifiers', 'output_contract': 'identity consistency receipt',
            'applicability': 'synthetic metadata fixture only',
            'acceptance_checks': ['normal input', 'mismatched parent input', 'identity and resource receipts'],
            'affects_scientific_results': False}))
        self.runtime.apply_meta(effect['id'])
        work = next(w for w in self.control.ready('audit') if w['kind'] == 'capability_build')
        with patch.object(self.runtime, '_publish_capability', side_effect=RuntimeError('crash after acceptance')):
            with self.assertRaises(RuntimeError):
                self.runtime.accept_capability(work['id'], spec, adapter, manifest, artifact)
        self.assertEqual(len(self.store.list('audit', 'capability', status='verified')), 1)
        with patch('docking_rsi.runtime.execute_acceptance', side_effect=AssertionError('must not execute again')):
            self.runtime.recover()
        intent = self.store.get('audit', 'registration', work['inputs']['build_intent_id'])
        self.assertEqual(intent['status'], 'verified')
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['spent'], 2)
        self.assertTrue(any(w['kind'] == 'meta_disposition' for w in self.control.ready('audit')))

    def test_summary_delivery_retains_original_identity_and_does_not_break_next_packet(self):
        self.direct_request()
        delivery = self.runtime.ingest_delivery('req', 'A summary of the existing input receipt',
            {'source': 'prepared_inputs', 'summary_of': ['receipt']}, kind='summary')
        work = next(w for w in self.control.ready('audit') if w['kind'] == 'meta_disposition')
        effect = self.runtime.prepare_meta(work['id'], {'model_calls': 1, 'tokens': 100, 'wall_seconds': 1})
        packet = json.loads(self.store.read_blob(effect['metadata']['plan_ref']))
        pages = [p for p in packet['delivery_pages'] if p['evidence_id'] == delivery['id']]
        self.assertTrue(pages)
        self.assertEqual(pages[0]['source_kind'], 'summary')
        self.assertEqual(pages[0]['summary_of'], ['receipt'])


if __name__ == '__main__':
    unittest.main()
