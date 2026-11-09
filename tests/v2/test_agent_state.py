"""Synthetic continuity/fault tests. No native model or scientific service calls."""
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab.agent_state import AgentState
from rsi_lab.controller import Controller
from rsi_lab.store import Store


class AgentStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={'model_calls': {'limit': 30, 'hard': True}},
                                   config={'task_id': 'task'}, max_external=4)
        self.state = AgentState(self.store, 'r')
        self.lab = self.state.ensure_session('lab')
        self.meta = self.state.ensure_session('meta')
        self.sequence = 0

    def tx(self, fn):
        self.sequence += 1
        return self.store.mutate('r', 'test:' + str(self.sequence), 'test_operation', fn)

    def prepared(self, session=None):
        session = session or self.lab
        self.sequence += 1
        suffix = str(self.sequence)
        work = self.controller.enqueue('r', 'lab' if session['role'] == 'lab' else 'meta_discovery',
                                       'test:' + suffix, 'work:' + suffix)
        turn = self.tx(lambda tx: self.state.prepare_turn_tx(tx, session['id'], work['id']))
        effect = self.controller.prepare_effect('r', work['id'], 'model', b'{"input":"synthetic"}',
                                               {'model_calls': 1}, 'effect:' + suffix)
        return turn, effect

    def dispatch(self, turn, effect, status='succeeded'):
        claimed = self.tx(lambda tx: self.state.claim_turn_tx(tx, turn['id'], effect['id']))
        result = self.controller.dispatch_effect('r', effect['id'], lambda *_: {
            'status': status, 'actual_costs': {'model_calls': 1}, 'response': {'synthetic': True}})
        receipt = json.loads(self.store.read_blob(result['result_ref']))
        self.tx(lambda tx: self.state.settle_request_tx(tx, turn['id'], effect['id'], receipt))
        return claimed, result

    def complete(self, delta=None, session=None):
        turn, effect = self.prepared(session)
        claimed, effect = self.dispatch(turn, effect)
        committed = self.tx(lambda tx: self.state.commit_turn_tx(tx, turn['id'],
            terminal_ref=effect['receipt_ref'], working_memory_delta=delta,
            provider_thread_id='native-' + (session or self.lab)['id'],
            context_manifest={'request_id': effect['id'], 'synthetic': True}))
        return committed, effect

    def test_owners_have_independent_sessions_and_memory(self):
        self.assertNotEqual(self.lab['id'], self.meta['id'])
        self.assertEqual(self.lab, self.state.ensure_session('lab'))
        self.complete({'plan_items': ['measure B'], 'next_step': 'verify B',
                       'tentative_hypotheses': ['A may be unsuitable']})
        memory = self.state.read_working_memory(self.lab['id'], actor_session_id=self.lab['id'])
        self.assertEqual(memory['revision'], 1)
        self.assertEqual(memory['content']['next_step'], 'verify B')
        self.assertEqual(self.state.working_memory(self.meta['id'])['revision'], 0)
        with self.assertRaises(ValueError):
            self.state.read_working_memory(self.lab['id'], actor_session_id=self.meta['id'])
        self.assertEqual(self.store.list('r', 'fact'), [])
        self.store.verify_history('r')

    def test_memory_revision_is_immutable_and_empty_delta_is_valid(self):
        self.complete({'rationale_summaries': ['Control comparison required']})
        first = self.state.working_memory(self.lab['id'])
        self.complete({})
        self.assertEqual(self.state.working_memory(self.lab['id']), first)
        self.complete({'next_step': 'read control'})
        latest = self.state.working_memory(self.lab['id'])
        self.assertEqual(latest['revision'], 2)
        self.assertEqual(latest['content']['rationale_summaries'], ['Control comparison required'])
        with self.assertRaises(ValueError):
            self.tx(lambda tx: tx.update('working_memory', first['head_ref'], {'content': {}}))
        self.assertEqual(len(self.store.list('r', 'working_memory')), 2)

    def test_invalid_memory_cannot_erase_known_completion(self):
        self.complete({'next_step': 'existing plan'})
        before = self.state.working_memory(self.lab['id'])
        turn, effect = self.complete({'owner': 'meta', 'next_step': 'steal state'})
        self.assertEqual(turn['status'], 'committed')
        self.assertEqual(turn['memory_update_status'], 'rejected')
        self.assertEqual(effect['status'], 'succeeded')
        self.assertEqual(self.state.working_memory(self.lab['id']), before)
        self.assertEqual(self.store.get('r', 'agent_session', self.lab['id'])['status'], 'idle')

    def test_false_non_objects_are_rejected_as_memory_updates(self):
        for delta in ([], '', 0, False):
            turn, _ = self.complete(delta)
            self.assertEqual(turn['memory_update_status'], 'rejected')
        self.assertEqual(self.state.working_memory(self.lab['id'])['revision'], 0)

    def test_live_turn_requires_native_terminal_evidence(self):
        turn, effect = self.prepared()
        self.dispatch(turn, effect)
        effect = self.store.get('r', 'effect', effect['id'])
        self.state.config = {**self.state.config, 'execution_mode': 'live'}
        with self.assertRaises(ValueError):
            self.tx(lambda tx: self.state.commit_turn_tx(tx, turn['id'], terminal_ref=effect['receipt_ref']))
        self.assertEqual(self.store.get('r', 'agent_session', self.lab['id'])['committed_turn'], 0)

    def test_double_turn_claim_and_changed_memory_snapshot_are_rejected(self):
        turn, effect = self.prepared()
        with self.assertRaises(ValueError):
            self.tx(lambda tx: self.state.prepare_turn_tx(tx, self.lab['id'], effect['work_id']))
        self.dispatch(turn, effect)
        with self.assertRaises(ValueError):
            self.tx(lambda tx: self.state.claim_turn_tx(tx, turn['id'], effect['id']))
        result = self.store.get('r', 'effect', effect['id'])
        with self.assertRaises(ValueError):
            self.tx(lambda tx: self.state.commit_turn_tx(tx, turn['id'], terminal_ref=result['receipt_ref'],
                                                        expected_memory_revision=8))
        self.assertEqual(self.store.get('r', 'agent_session', self.lab['id'])['committed_turn'], 0)

    def test_core_commit_rejects_bad_protocol_or_unconfirmed_native_writer(self):
        for index, unsafe in enumerate(({'protocol_valid': False},
                {'native_writer_quiesced': False}, {'recovery_requires_fencing': True})):
            with self.subTest(unsafe=unsafe):
                session = self.state.ensure_session('lab', agent_instance_id='guard-' + str(index))
                turn, effect = self.prepared(session)
                self.tx(lambda tx: self.state.claim_turn_tx(tx, turn['id'], effect['id']))
                receipt = {'status': 'succeeded', 'actual_costs': {'model_calls': 1},
                    'terminal_received': True, **unsafe}
                effect = self.controller.dispatch_effect('r', effect['id'], lambda *_: receipt)
                self.tx(lambda tx: self.state.settle_request_tx(tx, turn['id'], effect['id'], receipt))
                with self.assertRaises(ValueError):
                    self.tx(lambda tx: self.state.commit_turn_tx(tx, turn['id'],
                        terminal_ref=effect['result_ref'], working_memory_delta={'next_step': 'unsafe'}))
                current = self.store.get('r', 'agent_session', session['id'])
                self.assertEqual(current['committed_turn'], 0)
                self.assertEqual(current['active_turn_id'], turn['id'])
                self.assertEqual(self.state.working_memory(session['id'])['revision'], 0)

    def test_child_settlement_does_not_commit_turn_and_terminal_is_idempotent(self):
        turn, effect = self.prepared()
        self.dispatch(turn, effect)
        session = self.store.get('r', 'agent_session', self.lab['id'])
        self.assertEqual(session['status'], 'in_turn')
        self.assertEqual(session['committed_turn'], 0)
        effect = self.store.get('r', 'effect', effect['id'])
        first = self.tx(lambda tx: self.state.commit_turn_tx(tx, turn['id'], terminal_ref=effect['receipt_ref'],
                                                           working_memory_delta={'next_step': 'B'}))
        second = self.tx(lambda tx: self.state.commit_turn_tx(tx, turn['id'], terminal_ref=effect['receipt_ref'],
                                                            working_memory_delta={'next_step': 'B'}))
        self.assertEqual(first, second)
        self.assertEqual(self.state.working_memory(self.lab['id'])['revision'], 1)
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)

    def test_multiple_child_requests_reuse_fence_and_commit_only_once(self):
        turn, effect = self.prepared()
        first, effect = self.dispatch(turn, effect)
        work = self.controller.enqueue('r', 'diagnostic', 'child2', 'child2')
        second = self.controller.prepare_effect('r', work['id'], 'model', b'{}', {'model_calls': 1},
            'second-effect', metadata={'agent_turn_id': turn['id']})
        settled = self.controller.dispatch_effect('r', second['id'], lambda *_: {
            'status': 'succeeded', 'actual_costs': {'model_calls': 1}})
        self.assertEqual(self.store.get('r', 'agent_turn', turn['id'])['fence'], first['fence'])
        receipt = json.loads(self.store.read_blob(settled['receipt_ref']))
        self.tx(lambda tx: self.state.settle_request_tx(tx, turn['id'], second['id'], receipt))
        self.tx(lambda tx: self.state.commit_turn_tx(tx, turn['id'], terminal_ref=settled['receipt_ref']))
        self.assertEqual(self.store.get('r', 'agent_session', self.lab['id'])['committed_turn'], 1)
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 2)

    def test_unknown_turn_blocks_rebuild_or_reuse_but_not_other_agent(self):
        turn, effect = self.prepared()
        self.dispatch(turn, effect, 'unknown')
        self.assertEqual(self.store.get('r', 'agent_session', self.lab['id'])['status'], 'recovery_required')
        with self.assertRaises(ValueError):
            self.state.rebuild_session(self.lab['id'], 'lost native session', 'rebuild-unknown')
        with self.assertRaises(ValueError):
            self.state.seal_checkpoint(self.lab['id'], 'unknown-checkpoint')
        self.complete({'next_step': 'investigate'}, self.meta)
        settled = self.controller.reconcile('r', effect['id'], {'status': 'succeeded',
            'actual_costs': {'model_calls': 1},
            'reconciliation': {'effect_id': effect['id'], 'provenance': 'synthetic provider receipt'}}, 'reconciled')
        receipt = json.loads(self.store.read_blob(settled['receipt_ref']))
        self.tx(lambda tx: self.state.settle_request_tx(tx, turn['id'], effect['id'], receipt))
        self.tx(lambda tx: self.state.commit_turn_tx(tx, turn['id'], terminal_ref=settled['receipt_ref']))
        self.assertEqual(self.store.get('r', 'agent_session', self.lab['id'])['status'], 'idle')

    def test_cancel_prepared_releases_only_session_not_finished_history(self):
        turn, effect = self.prepared()
        self.controller.cancel_prepared('r', effect['id'], 'input stale', 'cancel-effect')
        self.tx(lambda tx: self.state.cancel_turn_tx(tx, turn['id'], 'input stale'))
        session = self.store.get('r', 'agent_session', self.lab['id'])
        self.assertEqual((session['status'], session['committed_turn']), ('idle', 0))
        self.complete()
        with self.assertRaises(ValueError):
            self.tx(lambda tx: self.state.cancel_turn_tx(tx, turn['id'], 'repeat'))

    def test_checkpoint_and_native_rebuild_preserve_memory_and_costs(self):
        self.complete({'next_step': 'continue from saved plan'})
        before = self.state.working_memory(self.lab['id'])
        checkpoint = self.state.seal_checkpoint(self.lab['id'], 'seal')
        with self.assertRaises(ValueError):
            self.tx(lambda tx: tx.update('agent_checkpoint', checkpoint['id'], {'ledger_seq': 0}))
        new = self.state.rebuild_session(self.lab['id'], 'native files missing', 'rebuild', checkpoint_id=checkpoint['id'])
        self.assertNotEqual(new['id'], self.lab['id'])
        self.assertEqual(new['supersedes_session'], self.lab['id'])
        self.assertEqual(self.state.working_memory(new['id']), before)
        self.assertEqual(self.store.get('r', 'agent_session', self.meta['id'])['status'], 'idle')
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)
        self.assertEqual(self.state.ensure_session('lab')['id'], new['id'])
        self.store.verify_history('r')

    def test_handoff_is_explicit_scoped_and_does_not_publish_scientific_fact(self):
        self.complete({'next_step': 'review discrepancy', 'rationale_summaries': ['not for recipient']})
        handoff = self.state.propose_handoff(self.lab['id'], self.meta['id'], ['next_step'], 'investigate', 'handoff')
        with self.assertRaises(ValueError):
            self.state.handoff_payload(handoff['id'], actor_session_id=self.meta['id'])
        accepted = self.state.accept_handoff(handoff['id'], self.meta['id'], 'accept')
        repeat = self.state.accept_handoff(handoff['id'], self.meta['id'], 'accept-again')
        self.assertEqual(accepted, repeat)
        payload = self.state.handoff_payload(handoff['id'], actor_session_id=self.meta['id'])
        self.assertEqual(payload['content'], {'next_step': 'review discrepancy'})
        self.assertEqual(len(self.store.list('r', 'work', trigger='handoff:' + handoff['id'])), 1)
        self.assertEqual(self.state.working_memory(self.meta['id'])['revision'], 0)
        self.assertEqual(self.store.list('r', 'fact'), [])
        sibling = self.state.ensure_session('meta', branch_id='trial')
        with self.assertRaises(ValueError):
            self.state.propose_handoff(self.lab['id'], sibling['id'], ['next_step'], 'cross branch', 'bad-handoff')
        with self.assertRaises(ValueError):
            self.state.accept_handoff(handoff['id'], self.lab['id'], 'wrong-actor')

    def test_superseded_session_cannot_publish_current_private_memory(self):
        self.complete({'next_step': 'new private plan'})
        self.state.rebuild_session(self.lab['id'], 'replace native session', 'replace-for-handoff')
        with self.assertRaises(ValueError):
            self.state.propose_handoff(self.lab['id'], self.meta['id'], ['next_step'], 'late handoff', 'late-handoff')

    def test_owner_uses_task_run_branch_and_agent_identity(self):
        another = self.state.ensure_session('meta', agent_instance_id='inspector')
        self.assertNotEqual(self.meta['owner_id'], another['owner_id'])
        other_branch = self.state.ensure_session('lab', branch_id='branch2')
        self.assertNotEqual(self.lab['owner_id'], other_branch['owner_id'])
        self.assertEqual(self.lab['owner']['task_id'], 'task')
        with self.assertRaises(KeyError):
            AgentState(self.store, 'unknown')

    def test_discard_known_terminal_preserves_memory_and_allows_explicit_rebuild(self):
        self.complete({'next_step': 'preserved'})
        before = self.state.working_memory(self.lab['id'])
        turn, effect = self.prepared()
        self.tx(lambda tx: self.state.claim_turn_tx(tx, turn['id'], effect['id']))
        final = self.controller.dispatch_effect('r', effect['id'], lambda *_: {
            'status': 'succeeded', 'actual_costs': {'model_calls': 1},
            'terminal_received': True, 'native_writer_quiesced': True,
            'protocol_valid': False, 'response': {'invalid': 'proposal'}})
        receipt = json.loads(self.store.read_blob(final['receipt_ref']))
        self.tx(lambda tx: self.state.settle_request_tx(tx, turn['id'], effect['id'], receipt))
        discarded = self.state.discard_completed_turn(self.lab['id'], 'invalid native prefix', 'discard')
        self.assertIsNone(discarded['active_turn_id'])
        self.assertEqual(discarded['status'], 'recovery_required')
        self.assertEqual(discarded['committed_turn'], 1)
        self.assertEqual(self.store.get('r', 'agent_turn', turn['id'])['status'], 'rejected')
        new = self.state.rebuild_session(self.lab['id'], 'explicit reconstruction', 'rebuild-discarded')
        self.assertEqual(self.state.working_memory(new['id']), before)
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 2)

    def test_discard_rejects_unknown_execution_or_unconfirmed_writer(self):
        turn, effect = self.prepared()
        self.dispatch(turn, effect, 'unknown')
        with self.assertRaises(ValueError):
            self.state.discard_completed_turn(self.lab['id'], 'cannot bypass', 'discard-unknown')
        second, child = self.prepared(self.meta)
        self.tx(lambda tx: self.state.claim_turn_tx(tx, second['id'], child['id']))
        final = self.controller.dispatch_effect('r', child['id'], lambda *_: {
            'status': 'succeeded', 'actual_costs': {'model_calls': 1},
            'terminal_received': True, 'native_writer_quiesced': False,
            'recovery_requires_fencing': True})
        receipt = json.loads(self.store.read_blob(final['receipt_ref']))
        self.tx(lambda tx: self.state.settle_request_tx(tx, second['id'], child['id'], receipt))
        with self.assertRaises(ValueError):
            self.state.discard_completed_turn(self.meta['id'], 'writer may run', 'discard-writer')

    def test_never_dispatched_effect_can_be_discarded_without_native_progress(self):
        turn, effect = self.prepared()
        self.tx(lambda tx: self.state.claim_turn_tx(tx, turn['id'], effect['id']))
        final = self.controller.dispatch_effect('r', effect['id'], lambda *_: {
            'status': 'failed_definite', 'actual_costs': {'model_calls': 0},
            'never_dispatched': True})
        receipt = json.loads(self.store.read_blob(final['receipt_ref']))
        self.tx(lambda tx: self.state.settle_request_tx(tx, turn['id'], effect['id'], receipt))
        discarded = self.state.discard_completed_turn(self.lab['id'], 'local setup failed', 'discard-local')
        self.assertEqual(discarded['committed_turn'], 0)
        self.assertEqual(self.state.working_memory(self.lab['id'])['revision'], 0)
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 0)

    def test_state_survives_database_reopen(self):
        self.complete({'next_step': 'resume after process restart'})
        root = self.store.root
        reopened = Store(root)
        self.addCleanup(reopened.close)
        restored = AgentState(reopened, 'r')
        self.assertEqual(restored.ensure_session('lab')['provider_thread_id'], 'native-' + self.lab['id'])
        self.assertEqual(restored.working_memory(self.lab['id'])['content']['next_step'], 'resume after process restart')
        reopened.verify_history('r')


if __name__ == '__main__':
    unittest.main()
