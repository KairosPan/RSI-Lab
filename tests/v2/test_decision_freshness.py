"""Real packet/driver transactions reject stale issue-less research decisions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller, identity
from rsi_lab.decision_dependencies import StaleDecision
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from rsi_lab.roles import validate_command
from tests.v2.test_improvements import FakeModel, prepare, reservations, execute, publication


class DecisionFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'run'
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.c = Controller(self.store)
        self.c.initialize('r', budgets={name: {'limit': 100, 'hard': name.endswith('calls')}
            for name in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds')},
            config={'execution_mode': 'replay', 'max_input_bytes': 500000})
        self.adapter = Adapter('candidate_test', 'v1', 'candidate_test', {'scope': 'fixture'},
                               prepare, reservations, execute, publication)
        self.rt = Runtime(self.store, 'r', adapters=[self.adapter])
        self.res = {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1}
        self.add_fact('original')

    def add_fact(self, fid, question='q', controller=None):
        return (controller or self.c).add_fact('r', fid, 'raw_fact', {'observation': fid},
            {'source': 'fixture', 'question_id': question}, 'fact:' + fid)

    def work(self, trigger='start'):
        return self.c.enqueue('r', 'lab', trigger, trigger, inputs={
            'evidence_ids': ['original'], 'required_evidence_ids': ['original'],
            'decision_dependency_selectors': [{'kind': 'fact',
                'where': {'provenance.question_id': 'q'}, 'fields': ['kind', 'content_ref']}]})

    def model(self, **kwargs):
        return FakeModel('request_diagnostic', {'capability_id': 'candidate_test',
            'capability_version': 'v1', 'inputs_json': '{"candidates":["new"]}'}, **kwargs)

    def test_context_to_prepare_race_reserves_nothing(self):
        work = self.work()
        original = self.rt.controller.prepare_effect
        def race(*args, **kwargs):
            self.add_fact('new-before-prepare')
            return original(*args, **kwargs)
        with patch.object(self.rt.controller, 'prepare_effect', side_effect=race):
            with self.assertRaises(StaleDecision):
                self.rt.prepare_meta(work['id'], self.res)
        self.assertEqual(self.store.list('r', 'effect'), [])
        self.assertTrue(all(row['reserved'] == 0 for row in self.store.list('r', 'budget')))

    def test_prepared_stale_call_is_cancelled_and_rebuilt_without_dispatch(self):
        work = self.work()
        effect = self.rt.prepare_meta(work['id'], self.res)
        self.add_fact('new-before-dispatch')
        def forbidden(*args):
            raise AssertionError('A stale packet must never be sent')
        result = self.rt.run_meta(work['id'], forbidden, self.res)
        self.assertIn('stale_decision', result)
        self.assertEqual(self.store.get('r', 'effect', effect['id'])['status'], 'failed_definite')
        self.assertTrue(self.store.get('r', 'effect', effect['id'])['never_dispatched'])
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 0)
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['reserved'], 0)
        self.assertEqual(result['work']['inputs']['stale_decision_of'], effect['id'])
        self.assertEqual(result['work']['status'], 'ready')
        fresh = self.rt.run_meta(result['work']['id'], self.model(), self.res)
        self.assertEqual(fresh['proposal']['status'], 'applied')

    def test_response_from_old_research_state_cannot_admit_an_outbox(self):
        work = self.work()
        result = self.rt.run_meta(work['id'], self.model(mutate=lambda response, data:
            self.add_fact('new-during-model')), self.res)
        self.assertEqual(result['proposal']['status'], 'blocked_stale')
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)
        self.assertEqual(self.store.list('r', 'registration', kind='decision_commit'), [])

    def test_other_connection_racing_first_route_write_is_checked_inside_transaction(self):
        work = self.work()
        other = Store(self.root)
        self.addCleanup(other.close)
        original, inserted = self.store.mutate, []
        def race(run_id, key, event, fn, *args, **kwargs):
            if event == 'diagnostic_work_admitted' and not inserted:
                inserted.append(True)
                self.add_fact('concurrent-before-route', controller=Controller(other))
            return original(run_id, key, event, fn, *args, **kwargs)
        with patch.object(self.store, 'mutate', side_effect=race):
            result = self.rt.run_meta(work['id'], self.model(), self.res)
        self.assertEqual(result['proposal']['status'], 'blocked_stale')
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])
        self.assertEqual(self.store.list('r', 'dependency', producer='candidate_test'), [])
        self.assertEqual(self.store.list('r', 'registration', kind='decision_commit'), [])

    def test_unrelated_question_and_own_bookkeeping_do_not_invalidate_decision(self):
        work = self.work()
        self.rt.prepare_meta(work['id'], self.res)
        self.add_fact('unrelated', question='different-question')
        result = self.rt.run_meta(work['id'], self.model(), self.res)
        self.assertEqual(result['proposal']['status'], 'applied')
        self.assertEqual(len(self.store.list('r', 'registration', kind='decision_commit')), 1)

    def test_committed_intent_survives_own_writes_and_later_research_changes(self):
        work = self.work()
        original = self.rt._route_command
        def crash(proposal):
            original(proposal)
            raise KeyboardInterrupt('after first durable action')
        with patch.object(self.rt, '_route_command', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.rt.run_meta(work['id'], self.model(), self.res)
        proposal = self.store.list('r', 'proposal')[0]
        self.assertEqual(proposal['status'], 'admitted')
        self.assertEqual(len(self.store.list('r', 'registration', kind='decision_commit')), 1)
        changed = json.loads(json.dumps(proposal))
        changed['command']['action'] = 'no_action'
        changed['command']['payload'] = {'reason': 'Reinterpret the already accepted tool action'}
        before_reinterpretation = self.store.verify_history('r')
        with self.assertRaisesRegex(ValueError, 'Committed decision identity differs'):
            self.rt._route(changed)
        self.assertEqual(self.store.verify_history('r'), before_reinterpretation)
        self.add_fact('after-accepted-intent')
        result = self.rt.apply_meta(proposal['effect_id'])
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(len(self.store.list('r', 'work', kind='diagnostic')), 1)
        before = self.store.verify_history('r')
        self.assertEqual(self.rt.apply_meta(proposal['effect_id']), result)
        self.assertEqual(self.store.verify_history('r'), before)

    def legacy_response(self, *, commit_route=False):
        """Append a genuine old-format artifact and old-host transitions.

        No existing Effect, packet, receipt, event or projection is rewritten.
        Only the historical dispatch lacks the check introduced by this change.
        """
        work = self.work('legacy')
        agent, mode, task, options = self.rt._context(work)
        task.pop('decision_dependencies')
        planned = agent.prepare(mode, task, **options)
        packet = agent.for_dispatch(planned, mode, task, **options)
        plan_ref = self.store.put_blob({key: value for key, value in packet.items() if key != 'envelope_bytes'})
        effect = self.c.prepare_effect('r', work['id'], 'lab', packet['envelope_bytes'], self.res,
            'legacy-prepare', metadata={'mode': mode, 'plan_ref': plan_ref, 'issue_id': None})
        with patch.object(self.c, '_validate_decision', return_value=None):
            effect = self.c.dispatch_effect('r', effect['id'], self.model())
        receipt = json.loads(self.store.read_blob(effect['result_ref']))
        self.assertNotIn('decision_dependencies', effect['metadata'])
        self.assertNotIn('decision_dependencies', json.loads(self.store.read_blob(plan_ref))['task'])
        self.assertEqual(digest(packet['envelope_bytes']), effect['packet_ref'])
        if commit_route:
            command = validate_command(receipt['response'], packet, current_versions=self.rt.versions(None),
                authorized_sources=[], capabilities=self.rt.capability_catalog())
            pid = identity('proposal', effect['id'])
            proposal = self.store.mutate('r', 'legacy-admit', 'meta_proposal_admitted', lambda tx:
                tx.insert('proposal', pid, {'effect_id': effect['id'], 'issue_id': None, 'status': 'admitted',
                    'command': command, 'error': None, 'delivery_ref': tx.blob(packet['delivery_pages']),
                    'result_ref': None, 'initial_review_ref': None}))
            # The old host committed its diagnostic route before crashing, but
            # had no dependency manifest or decision seal yet.
            self.rt._route_command(proposal)
        self.store.verify_history('r')
        return effect

    def test_legacy_response_without_manifest_cannot_authorize_a_new_action(self):
        effect = self.legacy_response()
        original_effect = self.store.get('r', 'effect', effect['id'])
        original_packet = self.store.read_blob(effect['packet_ref'])
        result = self.rt.apply_meta(effect['id'])
        self.assertEqual(result['status'], 'blocked_stale')
        self.assertIn('legacy_unversioned', result['error'])
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [])
        self.assertEqual(self.store.list('r', 'registration', kind='decision_commit'), [])
        self.assertEqual(self.store.get('r', 'effect', effect['id']), original_effect)
        self.assertEqual(self.store.read_blob(effect['packet_ref']), original_packet)
        self.store.verify_history('r')

    def test_legacy_committed_route_recovers_without_retroactive_versioning_or_second_work(self):
        effect = self.legacy_response(commit_route=True)
        before_events = [tuple(row) for row in self.store.db.execute(
            'SELECT * FROM events WHERE run_id=? ORDER BY seq', ('r',))]
        old_work = self.store.list('r', 'work', kind='diagnostic')[0]
        original_effect = self.store.get('r', 'effect', effect['id'])
        self.add_fact('after-legacy-commit')
        result = self.rt.apply_meta(effect['id'])
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [old_work])
        seal = self.store.list('r', 'registration', kind='decision_commit')[0]
        self.assertEqual(seal['status'], 'legacy_committed_intent')
        self.assertIsNone(seal['dependency_token'])
        self.assertEqual(seal['decision_freshness'], 'legacy_unversioned')
        self.assertEqual(self.store.get('r', 'effect', effect['id']), original_effect)
        after_events = [tuple(row) for row in self.store.db.execute(
            'SELECT * FROM events WHERE run_id=? ORDER BY seq', ('r',))]
        self.assertEqual(before_events, after_events[:len(before_events)])
        before = self.store.verify_history('r')
        self.assertEqual(self.rt.apply_meta(effect['id']), result)
        self.assertEqual(self.store.verify_history('r'), before)

    def scientific_runtime(self):
        store = Store(self.root / 'scientific')
        self.addCleanup(store.close)
        controller = Controller(store)
        controller.initialize('science', budgets={name: {'limit': 100, 'hard': name.endswith('calls')}
            for name in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds')},
            config={'execution_mode': 'replay', 'max_input_bytes': 500000,
                    'research_features': {'scientific_memory': True}, 'goal': 'Interpret a fixed observation'})
        runtime = Runtime(store, 'science', adapters=[self.adapter])
        controller.add_fact('science', 'raw', 'raw_fact', {'score': 0.1},
            {'source': 'fixed test observation'}, 'raw')
        runtime.scientific_memory.observe_fact('raw')
        return runtime

    @staticmethod
    def scientific_update(runtime, oid='claim', revision=0, refs=None):
        return {'kind': 'claim', 'id': oid, 'expected_revision': revision,
            'question_ids': [runtime.scientific_memory.ensure_question()['id']],
            'content': {'statement': 'The observation was bounded', 'scope': {},
                        'judgment': 'tentative', 'outcome': 'negative'},
            'source_evidence_ids': ['raw'], 'object_refs': refs or []}

    def science_turn(self, runtime, trigger, updates, during=None):
        work = runtime.enqueue_lab(trigger, ['raw'])
        def mutate(response, data):
            response['scientific_memory_updates'] = json.dumps(updates)
            if during:
                during(response, data)
        return runtime.run_lab(work['id'], FakeModel('no_action', {'reason': 'Record a bounded interpretation'},
                               mutate=mutate), self.res)

    def test_source_correction_during_model_blocks_old_scientific_updates(self):
        runtime = self.scientific_runtime()
        memory = runtime.scientific_memory
        initial = self.science_turn(runtime, 'create', [self.scientific_update(runtime)])
        self.assertEqual(initial['proposal']['status'], 'applied')
        original_claim = memory.get('claim')
        original_fact = runtime.store.get('science', 'fact', 'raw')
        def correct(response, data):
            self.assertIn('claim', {row['id'] for row in data['task']['scientific_memory']['objects']})
            runtime.controller.add_fact('science', 'corrected', 'raw_fact', {'score': 0.2},
                {'source': 'corrected instrument reading'}, 'correction')
            memory.mark_source_changed('raw', replacement_id='corrected', reason='Instrument correction')
        result = self.science_turn(runtime, 'stale-source', [self.scientific_update(runtime, revision=1)], correct)
        self.assertEqual(result['proposal']['status'], 'blocked_stale')
        self.assertEqual(memory.get('claim'), original_claim)
        self.assertEqual(runtime.store.get('science', 'fact', 'raw'), original_fact)
        self.assertFalse(runtime.store.list('science', 'registration', kind='scientific_memory_update_result',
                                           effect_id=result['effect']['id']))
        self.assertFalse(runtime.store.list('science', 'registration', kind='decision_commit',
                                           effect_id=result['effect']['id']))

    def test_own_scientific_revision_and_reference_commit_does_not_false_stale_on_recovery(self):
        runtime = self.scientific_runtime()
        memory = runtime.scientific_memory
        first = self.science_turn(runtime, 'first', [self.scientific_update(runtime)])
        self.assertEqual(first['proposal']['status'], 'applied')
        updates = [self.scientific_update(runtime, revision=1), self.scientific_update(runtime, 'related',
            refs=[{'id': 'claim', 'revision': 1, 'relation': 'derived_from'}])]
        original = memory.apply_updates
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt('after own scientific revisions committed')
        with patch.object(memory, 'apply_updates', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.science_turn(runtime, 'revise', updates)
        proposal = next(row for row in runtime.store.list('science', 'proposal') if row['status'] == 'admitted')
        self.assertEqual(memory.get('claim')['revision'], 2)
        self.assertEqual(memory.get('related')['object_refs'][0]['revision'], 1)
        recovery = runtime.apply_meta(proposal['effect_id'])
        self.assertEqual(recovery['status'], 'applied')
        self.assertEqual(len(memory.read_history('claim')), 2)
        self.assertEqual(len(memory.read_history('related')), 1)
        before = runtime.store.verify_history('science')
        self.assertEqual(runtime.apply_meta(proposal['effect_id']), recovery)
        self.assertEqual(runtime.store.verify_history('science'), before)

    def test_referenced_scientific_head_advanced_during_model_blocks_old_reference_update(self):
        runtime = self.scientific_runtime()
        memory = runtime.scientific_memory
        self.science_turn(runtime, 'create', [self.scientific_update(runtime)])
        # A different, already committed decision is awaiting only its durable
        # memory append. Its recovery can legitimately advance a selected head
        # while a later model invocation is in flight.
        with patch.object(memory, 'apply_updates', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.science_turn(runtime, 'delayed-revision', [self.scientific_update(runtime, revision=1)])
        pending = next(row for row in runtime.store.list('science', 'proposal') if row['status'] == 'admitted')
        def advance(response, data):
            selected = next(row for row in data['task']['scientific_memory']['objects'] if row['id'] == 'claim')
            self.assertEqual(selected['revision'], 1)
            self.assertEqual(runtime.apply_meta(pending['effect_id'])['status'], 'applied')
            self.assertEqual(memory.get('claim')['revision'], 2)
        update = self.scientific_update(runtime, 'stale-related',
            refs=[{'id': 'claim', 'revision': 1, 'relation': 'derived_from'}])
        result = self.science_turn(runtime, 'reference-reader', [update], advance)
        self.assertEqual(result['proposal']['status'], 'blocked_stale')
        self.assertEqual(memory.get('claim')['revision'], 2)
        with self.assertRaises(KeyError):
            memory.get('stale-related')
        self.assertFalse(runtime.store.list('science', 'registration', kind='scientific_memory_update_result',
                                           effect_id=result['effect']['id']))
        runtime.store.verify_history('science')

    def test_issueful_sealed_outbox_resumes_after_new_issue_evidence_without_reinterpretation(self):
        self.c.open_issue('r', 'issue', 'gap', 'An observation needs testing', 'fixed task', ['original'],
                          ['An alternative explanation'], ['A contrary measurement'], 'issue')
        work = next(row for row in self.c.ready('r') if row['kind'] == 'meta_disposition')
        # The first atomic route created the tool outbox and its decision seal;
        # the later wait transition has not happened when this crash occurs.
        with patch.object(self.rt, '_park', side_effect=KeyboardInterrupt('before park')):
            with self.assertRaises(KeyboardInterrupt):
                self.rt.run_meta(work['id'], self.model(), self.res)
        proposal = self.store.list('r', 'proposal')[0]
        self.assertEqual(proposal['status'], 'admitted')
        self.assertEqual(len(self.store.list('r', 'registration', kind='decision_commit')), 1)
        original_tool = self.store.list('r', 'work', kind='diagnostic')[0]
        self.assertEqual(self.store.list('r', 'wait'), [])
        self.add_fact('new-issue-evidence')
        changed_issue = self.c.open_issue('r', 'issue', 'gap', 'An observation needs testing', 'fixed task',
            ['original', 'new-issue-evidence'], ['An alternative explanation'], ['A contrary measurement'],
            'issue-new-evidence')
        self.assertEqual(changed_issue['evidence_revision'], 2)
        before_events = [tuple(row) for row in self.store.db.execute(
            'SELECT * FROM events WHERE run_id=? ORDER BY seq', ('r',))]
        result = self.rt.apply_meta(proposal['effect_id'])
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(self.store.list('r', 'work', kind='diagnostic'), [original_tool])
        current_issue = self.store.get('r', 'issue', 'issue')
        self.assertEqual(current_issue['evidence_revision'], 2)
        self.assertEqual(current_issue['evidence_ids'], changed_issue['evidence_ids'])
        self.assertEqual(len(self.store.list('r', 'wait')), 1)
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)
        after_events = [tuple(row) for row in self.store.db.execute(
            'SELECT * FROM events WHERE run_id=? ORDER BY seq', ('r',))]
        self.assertEqual(before_events, after_events[:len(before_events)])
        before = self.store.verify_history('r')
        self.assertEqual(self.rt.apply_meta(proposal['effect_id']), result)
        self.assertEqual(self.store.verify_history('r'), before)
