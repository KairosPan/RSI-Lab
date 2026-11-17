"""Adversarial control-plane contracts; all effects are deterministic fixtures."""
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab.controller import Controller
from rsi_lab.store import Store


class ControlAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.control = Controller(self.store)
        self.control.initialize('audit', budgets={
            'model_calls': {'limit': 8, 'hard': True, 'settlement_reserve': 1},
            'tool_calls': {'limit': 8, 'hard': True},
            'tokens': {'limit': 1000, 'hard': False},
        })

    def issue(self, name='i'):
        self.control.add_fact('audit', 'f_' + name, 'raw_fact', {'condition': 'unknown'},
                              {'source': 'fixture'}, 'fact_' + name)
        return self.control.open_issue('audit', name, {'component': name}, 'Missing condition',
            {'target': name}, ['f_' + name], ['Condition may exist elsewhere'],
            ['Applicable validated condition is found'], 'issue_' + name)

    def effect(self, name='e', reservations=None, issue_id=None):
        work = self.control.enqueue('audit', 'diagnostic', name, 'work_' + name, issue_id=issue_id)
        return self.control.prepare_effect('audit', work['id'], 'tool', b'{"input":"fixture"}',
            reservations or {'tool_calls': 1}, 'prepare_' + name)

    def test_known_cost_hold_is_released_on_final_receipt_without_repeating_cost(self):
        effect = self.effect(reservations={'tool_calls': 1, 'tokens': 100})
        self.control.dispatch_effect('audit', effect['id'], lambda e, p: {
            'status': 'unknown', 'actual_costs': {'tokens': 10}})
        settled = self.control.reconcile('audit', effect['id'], {
            'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
            'reconciliation': {'effect_id': effect['id'], 'provenance': 'provider receipt'},
        }, 'final_receipt')
        self.assertEqual(settled['actual_costs'], {'tokens': 10, 'tool_calls': 1})
        self.assertFalse(settled['accounting_unknown'])
        self.assertEqual(settled['held_reservations'], {'tokens': 0, 'tool_calls': 0})
        self.assertEqual(self.store.get('audit', 'budget', 'tokens')['reserved'], 0)
        self.store.verify_history('audit')

    def test_recovery_after_lost_receipt_preserves_unknown_attempt_and_reservations(self):
        effect = self.effect()
        self.store.mutate('audit', 'fixture_dispatch', 'effect_dispatching',
            lambda tx: tx.update('effect', effect['id'], {'status': 'dispatching'}))
        self.control.recover('audit')
        events = self.store.verify_history('audit')['events']
        self.control.recover('audit')
        self.assertEqual(self.store.verify_history('audit')['events'], events)
        current = self.store.get('audit', 'effect', effect['id'])
        self.assertEqual(current['status'], 'unknown')
        self.assertTrue(current['accounting_unknown'])
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['reserved'], 1)
        called = []
        with self.assertRaises(ValueError):
            self.control.dispatch_effect('audit', effect['id'], lambda e, p: called.append(e))
        self.assertEqual(called, [])

    def test_recovery_rejects_cross_attempt_receipt_envelope(self):
        effect = self.effect()
        self.store.mutate('audit', 'fixture_dispatch', 'effect_dispatching',
            lambda tx: tx.update('effect', effect['id'], {'status': 'dispatching'}))
        receipt = self.store.put_blob({'status': 'succeeded', 'actual_costs': {'tool_calls': 1}})
        path = Path(self.tmp.name) / ('receipt_' + effect['id'] + '.json')
        path.write_text(json.dumps({'effect_id': 'different_effect',
            'packet_hash': effect['packet_hash'], 'receipt_ref': receipt}))
        with self.assertRaisesRegex(ValueError, 'different attempt'):
            self.control.recover('audit')
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['spent'], 0)
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['reserved'], 1)

    def test_duplicate_settlement_receipt_never_double_counts(self):
        effect = self.effect()
        settled = self.control.dispatch_effect('audit', effect['id'], lambda e, p: {
            'status': 'succeeded', 'actual_costs': {'tool_calls': 1}})
        duplicate = self.control.settle_effect('audit', effect['id'], settled['receipt_ref'],
                                              'settle:' + effect['id'])
        self.assertEqual(duplicate, settled)
        with self.assertRaises(ValueError):
            self.control.settle_effect('audit', effect['id'], settled['receipt_ref'], 'new_key')
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['spent'], 1)

    def test_unknown_cost_cannot_be_laundered_by_changed_consumption(self):
        effect = self.effect(reservations={'tool_calls': 1, 'tokens': 100})
        self.control.dispatch_effect('audit', effect['id'], lambda e, p: {
            'status': 'unknown', 'actual_costs': {'tool_calls': 1}})
        with self.assertRaisesRegex(ValueError, 'contradicts'):
            self.control.reconcile('audit', effect['id'], {
                'status': 'failed_definite', 'actual_costs': {'tool_calls': 0, 'tokens': 0},
                'reconciliation': {'effect_id': effect['id'], 'provenance': 'inconsistent receipt'},
            }, 'contradictory_receipt')
        self.assertEqual(self.store.get('audit', 'effect', effect['id'])['status'], 'unknown')
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['spent'], 1)
        self.assertEqual(self.store.get('audit', 'budget', 'tokens')['reserved'], 100)

    def test_work_identity_rejects_changed_input_under_another_operation_key(self):
        first = self.control.enqueue('audit', 'diagnostic', 'same_event', 'enqueue_a',
                                     inputs={'protocol': 'v1'})
        second = self.control.enqueue('audit', 'diagnostic', 'same_event', 'enqueue_b',
                                      inputs={'protocol': 'v1'})
        self.assertEqual(first['id'], second['id'])
        with self.assertRaises(ValueError):
            self.control.enqueue('audit', 'diagnostic', 'same_event', 'enqueue_c',
                                 inputs={'protocol': 'v2'})

    def test_transaction_result_mutation_cannot_corrupt_archived_projection_snapshot(self):
        def callback(tx):
            inserted = tx.insert('fact', 'mutable', {'payload': {'values': [1]}})
            inserted['payload']['values'].append(2)
            inserted['changed_only_in_result'] = True
            return inserted
        result = self.store.mutate('audit', 'mutable_result', 'fixture', callback)
        self.assertEqual(result['payload']['values'], [1, 2])
        self.assertEqual(self.store.get('audit', 'fact', 'mutable')['payload']['values'], [1])
        self.store.verify_history('audit')

    def test_unrelated_dependency_change_does_not_wake_wait_or_advance_issue(self):
        issue = self.issue()
        self.control.dependency('audit', 'needed', 'producer', {'status': 'pending'}, 'needed')
        self.control.dependency('audit', 'other', 'elsewhere', {'status': 'pending'}, 'other')
        self.control.park('audit', issue['id'], 'needed', 'producer', 1,
                          {'kind': 'version_changed'}, 100, 'replan', 'park')
        parked = self.store.get('audit', 'issue', issue['id'])
        self.control.dependency('audit', 'other', 'elsewhere', {'status': 'ready'}, 'other_changed')
        self.assertEqual(self.control.tick('audit', now=10), [])
        self.assertEqual(self.store.get('audit', 'issue', issue['id']), parked)
        self.assertEqual(self.control.ready('audit'), [])

    def test_noop_dependency_delivery_does_not_authorize_another_wait_cycle(self):
        self.issue()
        self.control.dependency('audit', 'needed', 'producer', {'status': 'pending'}, 'dep')
        self.control.park('audit', 'i', 'needed', 'producer', 1,
                          {'kind': 'version_changed'}, 10, 'replan', 'park')
        self.control.tick('audit', now=11)
        repeated = self.control.dependency('audit', 'needed', 'producer',
                                           {'status': 'pending'}, 'duplicate_delivery')
        self.assertEqual(repeated['revision'], 1)
        with self.assertRaisesRegex(ValueError, 'new wait cycle'):
            self.control.park('audit', 'i', 'needed', 'producer', 1,
                              {'kind': 'version_changed'}, 1000, 'replan', 'renew')

    def test_status_wait_cannot_park_on_an_already_satisfied_dependency(self):
        self.issue()
        self.control.dependency('audit', 'needed', 'producer', {'status': 'ready'}, 'dep')
        with self.assertRaises(ValueError):
            self.control.park('audit', 'i', 'needed', 'producer', 1,
                              {'kind': 'status', 'status': 'ready'}, 100, 'replan', 'park')

    def test_entity_kind_and_document_kind_filter_do_not_collide(self):
        first = self.control.enqueue('audit', 'diagnostic', 'one', 'one')
        self.control.enqueue('audit', 'meta_discovery', 'two', 'two')
        self.assertEqual(self.store.list('audit', 'work', kind='diagnostic'), [first])
        result = self.store.mutate('audit', 'query', 'fixture',
            lambda tx: tx.find('work', kind='diagnostic'))
        self.assertEqual(result, [first])

    def test_request_preserves_exact_source_version_and_structured_query(self):
        self.issue()
        spec = {'source_id': 'approved_source', 'source_version': 3, 'object_ids': ['target'],
                'fields': ['condition'], 'query': 'declared condition', 'max_pages': 1, 'max_bytes': 1000}
        request = self.control.request('audit', 'i', 'req', spec['query'], spec['source_id'],
            {'objects': spec['object_ids']}, 'request', source_version=3, query_spec=spec)
        self.assertEqual(request['query_spec'], spec)
        self.assertEqual(request['source_version'], 3)
        with self.assertRaisesRegex(ValueError, 'obsolete'):
            self.control.deliver('audit', 'req', 'old', 'old result', 'source', 'old', source_version=1)
        delivery = self.control.deliver('audit', 'req', 'correct', 'result', 'source', 'correct', source_version=3)
        self.assertEqual(delivery['source_version'], 3)

    def test_accounting_retry_is_idempotent_and_keeps_original_execution_result(self):
        effect = self.effect(reservations={'tool_calls': 1, 'tokens': 100})
        first = self.control.dispatch_effect('audit', effect['id'], lambda e, p: {
            'status': 'succeeded', 'actual_costs': {'tool_calls': 1}, 'response': {'value': 'actual result'}})
        self.assertTrue(first['accounting_unknown'])
        self.assertEqual(self.store.get('audit', 'dependency', 'external_capacity')['value']['available'], 1)
        receipt = {'status': 'succeeded', 'actual_costs': {'tokens': 20},
            'reconciliation': {'effect_id': effect['id'], 'provenance': 'complete accounting'}}
        final = self.control.reconcile('audit', effect['id'], receipt, 'accounting')
        again = self.control.reconcile('audit', effect['id'], receipt, 'accounting')
        self.assertEqual(final, again)
        self.assertEqual(final['result_ref'], first['result_ref'])
        self.assertNotEqual(final['receipt_ref'], first['receipt_ref'])
        self.assertEqual(self.store.get('audit', 'work', effect['work_id'])['outcome_ref'], first['result_ref'])
        self.assertEqual(self.store.get('audit', 'budget', 'tokens')['spent'], 20)
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['spent'], 1)

    def test_issue_cannot_close_with_unknown_independent_producer_effect(self):
        self.issue()
        work = self.control.enqueue('audit', 'evidence_retrieve', 'retrieve', 'retrieve',
                                    inputs={'issue_id': 'i', 'request_id': 'request'})
        effect = self.control.prepare_effect('audit', work['id'], 'tool', b'{}',
                                             {'tool_calls': 1}, 'producer')
        self.control.dispatch_effect('audit', effect['id'], lambda e, p: {'status': 'unknown', 'actual_costs': {}})
        with self.assertRaisesRegex(ValueError, 'obligations'):
            self.control.close_issue('audit', 'i', 'closed_unresolved', 'stop', 'close')

    def journal_receipt(self, effect, **changes):
        return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
            'response': {'value': 'original'}, 'sent_packet_sha256': effect['packet_hash'],
            'sent_prompt_sha256': 'fixture-prompt-hash', 'provider_thread_id': 'fixture-thread',
            'recovered_from_journal': True, 'terminal_received': True, 'protocol_valid': True,
            'native_writer_quiesced': False, 'recovery_requires_fencing': True,
            'reconciliation': {'effect_id': effect['id'], 'provenance': 'verified local journal'},
            **changes}

    def test_journal_terminal_can_gain_writer_exit_without_rebilling(self):
        effect = self.effect()
        initial = self.journal_receipt(effect)
        first = self.control.dispatch_effect('audit', effect['id'], lambda *_: initial)
        self.assertFalse(first['accounting_unknown'])
        final_receipt = {**initial, 'native_writer_quiesced': True, 'recovery_requires_fencing': False}
        final = self.control.reconcile('audit', effect['id'], final_receipt, 'writer_exit')
        self.assertNotEqual(first['result_ref'], final['result_ref'])
        self.assertEqual(json.loads(self.store.read_blob(final['result_ref'])), final_receipt)
        self.assertEqual(self.store.get('audit', 'work', effect['work_id'])['outcome_ref'], final['result_ref'])
        self.assertEqual(self.control.reconcile('audit', effect['id'], final_receipt, 'writer_exit'), final)
        with self.assertRaisesRegex(ValueError, 'already settled'):
            self.control.reconcile('audit', effect['id'], final_receipt, 'duplicate_exit')
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['spent'], 1)
        self.store.verify_history('audit')

    def test_journal_exit_cannot_change_response_or_input_identity(self):
        for index, replacement in enumerate(({'response': {'value': 'replaced'}},
                {'sent_packet_sha256': 'other-packet'}, {'sent_prompt_sha256': 'other-prompt'},
                {'provider_thread_id': 'other-thread'}, {'status': 'failed_definite'})):
            with self.subTest(replacement=replacement):
                effect = self.effect(name='journal_conflict_' + str(index))
                initial = self.journal_receipt(effect)
                first = self.control.dispatch_effect('audit', effect['id'], lambda *_: initial)
                final_receipt = {**initial, 'native_writer_quiesced': True,
                    'recovery_requires_fencing': False, **replacement}
                with self.assertRaisesRegex(ValueError, 'contradicts'):
                    self.control.reconcile('audit', effect['id'], final_receipt, 'conflict_' + str(index))
                current = self.store.get('audit', 'effect', effect['id'])
                self.assertEqual(current['result_ref'], first['result_ref'])
                self.assertEqual(current['receipt_ref'], first['receipt_ref'])
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['spent'], 5)

    def test_journal_exit_cannot_upgrade_rejected_protocol(self):
        effect = self.effect()
        initial = self.journal_receipt(effect, protocol_valid=False)
        first = self.control.dispatch_effect('audit', effect['id'], lambda *_: initial)
        final_receipt = {**initial, 'native_writer_quiesced': True,
            'recovery_requires_fencing': False, 'protocol_valid': True}
        with self.assertRaisesRegex(ValueError, 'rejected protocol'):
            self.control.reconcile('audit', effect['id'], final_receipt, 'changed_protocol')
        final_receipt['protocol_valid'] = False
        final = self.control.reconcile('audit', effect['id'], final_receipt, 'confirmed_exit')
        self.assertNotEqual(first['result_ref'], final['result_ref'])
        self.assertFalse(json.loads(self.store.read_blob(final['result_ref']))['protocol_valid'])
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['spent'], 1)

    def test_closed_issue_cancels_independent_ready_producer(self):
        self.issue()
        work = self.control.enqueue('audit', 'evidence_retrieve', 'retrieve', 'retrieve',
                                    inputs={'issue_id': 'i'})
        self.control.close_issue('audit', 'i', 'closed_unresolved', 'stop', 'close')
        self.assertEqual(self.store.get('audit', 'work', work['id'])['status'], 'cancelled')

    def test_stale_prepared_effect_does_not_dispatch_and_can_be_cancelled_exactly_once(self):
        self.issue()
        effect = self.effect(issue_id='i', reservations={'tool_calls': 1, 'tokens': 100})
        self.store.mutate('audit', 'new_schedule', 'fixture_capacity_change',
            lambda tx: tx.update('issue', 'i', {'scheduling_revision': 1}))
        invoked = []
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.control.dispatch_effect('audit', effect['id'], lambda e, p: invoked.append(e))
        self.assertEqual(invoked, [])
        self.assertEqual(self.store.get('audit', 'effect', effect['id'])['status'], 'prepared')
        cancelled = self.control.cancel_prepared('audit', effect['id'], 'input became stale', 'cancel')
        self.assertEqual(cancelled['actual_costs'], {'tool_calls': 0, 'tokens': 0})
        self.assertTrue(cancelled['never_dispatched'])
        self.assertFalse(cancelled['accounting_unknown'])
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['reserved'], 0)
        self.assertEqual(self.store.get('audit', 'budget', 'tokens')['reserved'], 0)
        self.assertEqual(self.store.get('audit', 'work', effect['work_id'])['status'], 'cancelled')
        self.assertEqual(self.control.cancel_prepared('audit', effect['id'], 'input became stale', 'cancel'), cancelled)
        self.store.verify_history('audit')

    def test_unknown_effect_cannot_claim_never_dispatched_to_release_resources(self):
        effect = self.effect()
        self.control.dispatch_effect('audit', effect['id'], lambda e, p: {'status': 'unknown', 'actual_costs': {}})
        with self.assertRaisesRegex(ValueError, 'undispatched'):
            self.control.cancel_prepared('audit', effect['id'], 'try to clear hold', 'cancel_unknown')
        self.assertEqual(self.store.get('audit', 'budget', 'tool_calls')['reserved'], 1)

    def test_new_request_identity_or_wording_cannot_renew_the_same_investigation(self):
        self.issue()
        scope = {'object_ids': ['c1', 'c2'], 'fields': ['structure', 'condition'],
                 'max_pages': 3, 'max_bytes': 4000}
        first = self.control.request('audit', 'i', 'first', 'Retrieve inputs', 'source', scope,
                                     'first', source_version=2)
        self.assertEqual(first['issue_evidence_revision'], 1)
        for index, replacement in enumerate((scope, {**scope, 'object_ids': ['c2', 'c1']},
                {**scope, 'fields': ['condition', 'structure', 'condition']})):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'Duplicate request scope'):
                self.control.request('audit', 'i', 'again' + str(index), 'Please retry that request', 'source',
                                     replacement, 'again' + str(index), source_version=2)
        with self.assertRaisesRegex(ValueError, 'Duplicate request scope'):
            self.control.request('audit', 'i', 'superseding', 'Rephrased investigation', 'source', scope,
                                  'superseding', source_version=2, supersedes='first')
        self.assertEqual(self.store.get('audit', 'request', 'first')['status'], 'created')
        self.assertEqual(self.store.list('audit', 'request'), [first])

    def test_real_scope_source_or_evidence_change_permits_a_new_request(self):
        self.issue()
        scope = {'object_ids': ['c1', 'c2'], 'fields': ['structure'], 'max_pages': 3, 'max_bytes': 4000}
        self.control.request('audit', 'i', 'first', 'Retrieve inputs', 'source', scope, 'first', source_version=2)
        smaller = self.control.request('audit', 'i', 'smaller', 'Retrieve inputs', 'source',
            {**scope, 'max_pages': 1}, 'smaller', source_version=2, supersedes='first')
        self.assertEqual(smaller['issue_evidence_revision'], 1)
        newer_source = self.control.request('audit', 'i', 'newer_source', 'Retrieve inputs', 'source', scope,
                                            'newer_source', source_version=3)
        self.assertEqual(newer_source['source_version'], 3)
        self.control.add_fact('audit', 'new_evidence', 'raw_fact', {'input_revision': 2},
                              {'source': 'fixture'}, 'new_evidence')
        self.control.open_issue('audit', 'i', {'component': 'i'}, 'Missing condition', {'target': 'i'},
            ['f_i', 'new_evidence'], ['Condition may exist elsewhere'],
            ['Applicable validated condition is found'], 'new_evidence_issue')
        newer_evidence = self.control.request('audit', 'i', 'newer_evidence', 'Retrieve inputs', 'source', scope,
                                              'newer_evidence', source_version=2)
        self.assertEqual(newer_evidence['issue_evidence_revision'], 2)


if __name__ == '__main__':
    unittest.main()
