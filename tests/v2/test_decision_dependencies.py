"""Scoped research dependencies against a real ledger, without model calls."""
import copy
import tempfile
import unittest
from pathlib import Path

from rsi_lab.controller import Controller
from rsi_lab.decision_dependencies import StaleDecision, capture, validate, validate_binding
from rsi_lab.scientific_memory import ScientificMemory, _key
from rsi_lab.store import Store, digest


class DecisionDependencyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'run')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={'model_calls': {'limit': 10, 'hard': True}},
                                   config={'goal': 'Compare alternatives'})
        self.memory = ScientificMemory(self.store, 'r')
        self.sequence = 0

    def tx(self, fn):
        self.sequence += 1
        return self.store.mutate('r', 'fixture:' + str(self.sequence), 'fixture', fn)

    def fact(self, fid='fact-A'):
        return self.controller.add_fact('r', fid, 'raw_fact', {'score': 0.5},
                                        {'source': 'local fixture'}, 'fact:' + fid)

    def record(self, oid, *, kind='claim', sources=(), refs=(), questions=(), revision=0, content=None):
        row = self.memory._record(kind, oid, content or {'statement': oid},
                                  sources=list(sources), refs=list(refs), question_ids=list(questions))
        return self.tx(lambda tx: self.memory._append(tx, row, revision))

    def capture(self, task=None, ids=(), selectors=()):
        return capture(self.store, 'r', task or {}, ids, selectors)

    def test_empty_context_binds_real_config_and_is_read_only(self):
        before = self.store.verify_history('r')
        result = self.capture()
        self.assertEqual(result['config_hash'], self.store.run('r')['config_hash'])
        self.assertEqual(result['dependencies'], [])
        self.assertEqual(result['token'], self.capture()['token'])
        validate(self.store, 'r', result)
        self.assertEqual(self.store.verify_history('r'), before)
        self.controller.initialize('other', budgets={}, config={'goal': 'Other research'})
        with self.assertRaises(StaleDecision):
            validate(self.store, 'other', result)

    def test_fact_correction_invalidates_without_mutating_fact_version(self):
        self.fact()
        self.fact('unrelated')
        frozen = self.capture(ids=['fact-A'])
        self.memory.mark_source_changed('unrelated', reason='Unrelated source corrected')
        validate(self.store, 'r', frozen)
        self.memory.mark_source_changed('fact-A', reason='Relevant source corrected')
        self.assertEqual(self.store.get('r', 'fact', 'fact-A')['version'], 1)
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)

    def test_query_catches_scoped_phantoms_and_members_leaving_scope(self):
        selector = {'kind': 'work', 'where': {'kind': 'diagnostic', 'inputs.branch_id': 'one',
                    'status': {'$in': ['ready', 'leased']}}, 'fields': ['inputs', 'status']}
        frozen = self.capture(selectors=[selector])
        other = self.controller.enqueue('r', 'diagnostic', 'other', 'enqueue-other',
                                        inputs={'branch_id': 'two'})
        validate(self.store, 'r', frozen)
        own = self.controller.enqueue('r', 'diagnostic', 'own', 'enqueue-own',
                                      inputs={'branch_id': 'one'})
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)
        frozen = self.capture(selectors=[selector])
        self.tx(lambda tx: tx.update('work', other['id'], {'status': 'failed'}))
        validate(self.store, 'r', frozen)
        self.tx(lambda tx: tx.update('work', own['id'], {'status': 'failed'}))
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)

    def test_exact_projection_ignores_ledger_version_and_unselected_fields(self):
        self.tx(lambda tx: tx.insert('registration', 'trajectory',
            {'kind': 'trajectory', 'state': {'count': 3}, 'note': 'old'}))
        selector = {'kind': 'registration', 'id': 'trajectory', 'fields': ['state.count', 'state.missing']}
        frozen = self.capture(selectors=[selector])
        self.tx(lambda tx: tx.update('registration', 'trajectory', {'note': 'new'}))
        validate(self.store, 'r', frozen)
        self.tx(lambda tx: tx.update('registration', 'trajectory', {'state': {'count': 3, 'missing': None}}))
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)
        missing = self.capture(selectors=[{'kind': 'registration', 'id': 'not-yet-created'}])
        self.tx(lambda tx: tx.insert('registration', 'not-yet-created', {'state': 'new'}))
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', missing)

    def test_selected_scientific_object_binds_recursive_dependencies(self):
        self.fact()
        source = self.memory._source('fact-A')
        dependency = self.record('B', sources=[source])
        selected = self.record('A', refs=[{'id': 'B', 'revision': 1, 'relation': 'supports'}])
        task = {'scientific_memory': {'objects': [selected]}}
        frozen = self.capture(task)
        self.record('unrelated-object')
        validate(self.store, 'r', frozen)
        self.record('B', sources=[source], revision=dependency['revision'], content={'statement': 'B revised'})
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)
        frozen = self.capture(task)
        self.memory.mark_source_changed('fact-A', reason='Closure source corrected')
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)

    def test_pending_review_is_a_dependency_even_with_unchanged_head(self):
        selected = self.record('claim-A')
        frozen = self.capture({'scientific_memory': {'objects': [selected]}})
        head = self.memory._head('claim-A')
        self.tx(lambda tx: tx.insert('registration', 'pending',
            {'kind': 'scientific_memory_review_pending', 'object_id': 'claim-A',
             'revision': 1, 'source_refs': [], 'reason': 'New observations'}))
        self.assertEqual(self.memory._head('claim-A'), head)
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)

    def test_new_scientific_objects_are_scoped_to_selected_questions(self):
        selected = self.record('claim-A', questions=['question-A'])
        frozen = self.capture({'scientific_memory': {'objects': [selected]}})
        self.record('other-question-claim', questions=['question-B'])
        validate(self.store, 'r', frozen)
        self.record('new-relevant-claim', questions=['question-A'])
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)

    def test_virtual_root_preview_matches_ensured_rows_but_cannot_authorize_missing_rows(self):
        virtual = self.memory._unregistered_question()
        task = {'scientific_memory': {'objects': [virtual]}}
        before = self.store.verify_history('r')
        preview = self.capture(task)
        self.assertEqual(self.store.verify_history('r'), before)
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', preview)
        malformed = copy.deepcopy(task)
        malformed['scientific_memory']['objects'][0]['content']['question'] = 'Invented task'
        with self.assertRaises(ValueError):
            self.capture(malformed)
        self.memory.ensure_question()
        self.assertEqual(self.capture(task), preview)
        validate(self.store, 'r', preview)
        self.record('new-root-claim', questions=[virtual['id']])
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', preview)

    def test_interpretation_authority_includes_the_decision_head(self):
        investigation = self.record('investigation', kind='investigation')
        decision = self.record('decision', kind='decision',
            refs=[{'id': 'investigation', 'revision': 1, 'relation': 'context'}],
            content={'result_interpretation': 'Observed result was reviewed'})
        self.tx(lambda tx: tx.insert('registration', 'interpretation',
            {'kind': 'scientific_memory_interpretation', 'investigation_id': 'investigation',
             'investigation_revision': 1, 'decision_ref': {'id': 'decision', 'revision': 1},
             'disposition': 'interpreted'}))
        frozen = self.capture({'scientific_memory': {'objects': [investigation]}})
        self.record('decision', kind='decision', revision=decision['revision'],
                    content={'result_interpretation': '', 'result_disposition': 'deferred'})
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)

    def test_only_selected_recommendations_and_active_settings_are_bound(self):
        row = self.tx(lambda tx: tx.insert('registration', 'recommendation',
            {'kind': 'research_recommendation', 'status': 'proposed'}))
        task = {'constraints': {'research_loop': {'recommendations': [row],
                                                'recommendation_admission': {'remaining': 16}}}}
        frozen = self.capture(task)
        self.tx(lambda tx: tx.insert('registration', 'other-recommendation',
            {'kind': 'research_recommendation', 'status': 'reviewed'}))
        validate(self.store, 'r', frozen)
        self.tx(lambda tx: tx.update('registration', 'recommendation', {'status': 'adopted'}))
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)
        task = {'constraints': {'orchestration': {'settings': {'guidance': ''}}}}
        frozen = self.capture(task)
        self.tx(lambda tx: tx.insert('registration', 'rejected-settings',
            {'kind': 'orchestration_change', 'status': 'rejected', 'settings': {'guidance': 'unused'}}))
        validate(self.store, 'r', frozen)
        self.tx(lambda tx: tx.insert('registration', 'active-settings',
            {'kind': 'orchestration_change', 'status': 'trial', 'settings': {'guidance': 'new'}}))
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)
        frozen = self.capture(task)
        self.tx(lambda tx: tx.update('registration', 'active-settings', {'completed_steps': 2}))
        validate(self.store, 'r', frozen)

    def test_bookkeeping_does_not_invalidate_scientific_reads(self):
        self.fact()
        task = {'dependencies': self.store.list('r', 'dependency'),
                'agent_state': {'memory_revision': 1, 'committed_turn': 0}}
        frozen = self.capture(task, ids=['fact-A'])
        self.tx(lambda tx: tx.update('budget', 'model_calls', {'spent': 1}))
        self.tx(lambda tx: tx.update('dependency', 'external_capacity', {'revision': 2, 'value': {'available': 0}}))
        self.tx(lambda tx: tx.insert('agent_turn', 'turn', {'status': 'committed'}))
        self.tx(lambda tx: tx.insert('registration', 'discovery_counters', {'open_reviews_used': 1}))
        validate(self.store, 'r', frozen)

    def test_disposition_lease_bookkeeping_does_not_invalidate_wait_obligation(self):
        wait = self.tx(lambda tx: tx.insert('wait', 'waiting', {
            'issue_id': 'issue', 'dependency_id': 'source', 'producer': 'local',
            'observed_version': 1, 'wake_condition': {'kind': 'version_changed'},
            'deadline_at': 123, 'expiry_action': 'replan', 'wait_epoch': 1,
            'status': 'disposition_due', 'disposition_attempted': False, 'disposition_work_id': 'work'}))
        frozen = self.capture({'constraints': {'waiting': [wait]}})
        self.tx(lambda tx: tx.update('wait', 'waiting', {'status': 'disposed', 'disposition_attempted': True}))
        validate(self.store, 'r', frozen)
        self.tx(lambda tx: tx.update('wait', 'waiting', {'observed_version': 2}))
        with self.assertRaises(StaleDecision):
            validate(self.store, 'r', frozen)

    def test_selector_operators_and_json_type_identity(self):
        self.tx(lambda tx: tx.insert('registration', 'typed',
            {'kind': 'typed', 'value': True, 'questions': ['Q'], 'optional': None}))
        selectors = [
            {'kind': 'registration', 'where': {'value': 1}},
            {'kind': 'registration', 'where': {'value': {'$eq': True}, 'questions': {'$contains': 'Q'},
                                             'optional': {'$exists': True}, 'missing': {'$exists': False}}},
        ]
        frozen = self.capture(selectors=selectors)
        self.assertEqual(sorted(row['count'] for row in frozen['dependencies']), [0, 1])
        validate(self.store, 'r', frozen)

    def test_canonical_bounded_schema_rejects_unknown_or_tampered_fields(self):
        selector = {'kind': 'registration', 'where': {'kind': 'trajectory'}, 'fields': ['status', 'id']}
        self.assertEqual(self.capture(selectors=[selector, selector]), self.capture(selectors=[selector]))
        for bad in [
            {'kind': 'registration', 'where': {}, 'unknown': True},
            {'kind': 'registration', 'where': {}, 'id': 'both'},
            {'kind': 'registration', 'where': {'status': {'$unknown': True}}},
            {'kind': 'registration', 'where': {'status': {'$exists': 1}}},
            {'kind': 'registration', 'id': 'a', 'fields': ['x'] * 65},
            {'kind': 'registration', 'where': {'bad..path': True}},
        ]:
            with self.subTest(selector=bad), self.assertRaises(ValueError):
                self.capture(selectors=[bad])
        frozen = self.capture(selectors=[selector])
        for change in ('unknown', 'count', 'token'):
            malformed = copy.deepcopy(frozen)
            if change == 'unknown':
                malformed['unknown'] = True
            elif change == 'count':
                malformed['dependencies'][0]['count'] = True
                malformed['token'] = digest({k: v for k, v in malformed.items() if k != 'token'})
            else:
                malformed['token'] = '0' * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate(self.store, 'r', malformed)
        with self.assertRaises(ValueError):
            self.capture(selectors=[selector] * 257)

    def test_exact_packet_binding_preserves_legacy_absence_without_fabrication(self):
        self.assertIsNone(validate_binding({'metadata': {}}, {}))
        manifest = self.capture()
        effect = {'run_id': 'r', 'metadata': {'decision_dependencies': manifest}}
        task = {'run_id': 'r', 'decision_dependencies': manifest}
        self.assertEqual(validate_binding(effect, task), manifest)
        for other_effect, other_task in [
            (effect, {}), ({'metadata': {}}, task),
            ({'metadata': {'decision_dependencies': None}}, {'decision_dependencies': None}),
            ({**effect, 'run_id': 'different'}, task),
        ]:
            with self.subTest(effect=other_effect), self.assertRaises(ValueError):
                validate_binding(other_effect, other_task)
        changed = copy.deepcopy(task)
        changed['decision_dependencies']['token'] = '0' * 64
        with self.assertRaises(ValueError):
            validate_binding(effect, changed)


if __name__ == '__main__':
    unittest.main()
