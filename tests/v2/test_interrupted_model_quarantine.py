"""Offline operator recovery contracts; synthetic artifacts, no model calls."""
import copy
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi_lab.agent_state import AgentState
from rsi_lab.call_recovery import RecoveryBlocked, recover_codex_receipt, recover_interrupted_codex_receipt
from rsi_lab.controller import Controller
from rsi_lab.interruption import ATTESTATION_SCHEMA, effect_holds_execution, effect_is_quarantined
from rsi_lab.journal import JournalWriter
from rsi_lab.store import Store, digest, encode
from rsi_lab.transport import PREAMBLE, _command

THREAD = '018fe27b-a111-7222-8333-123456789abc'


class InterruptedModelQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'run')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={
            'model_calls': {'limit': 3, 'hard': True},
            'tokens': {'limit': 300, 'hard': False},
            'wall_seconds': {'limit': 100, 'hard': False}}, max_external=1)
        self.state = AgentState(self.store, 'r')
        self.session = self.state.ensure_session('lab')
        # Trusted fixture memory is distinct from the interrupted request.
        def seed(tx):
            tx.insert('working_memory', 'memory_seed', {'owner_id': self.session['owner_id'],
                'revision': 1, 'content': {'next_step': 'Use committed observations'}})
            tx.update('agent_owner', self.session['owner_id'],
                      {'memory_head_ref': 'memory_seed', 'memory_revision': 1})
        self.store.mutate('r', 'seed-memory', 'fixture_memory', seed)
        self.memory = self.state.working_memory(self.session['id'])
        self.work = self.controller.enqueue('r', 'lab', 'interrupted', 'work')
        self.request = {'schema_version': 'rsi_model_request_v2', 'mode': 'lab',
            'model': 'synthetic-model', 'effort': 'high', 'prompt': PREAMBLE + 'Synthetic recovery input',
            'response_schema': {'type': 'object'}, 'input_view_mode': 'json'}
        self.packet = encode(self.request)
        self.effect = self.controller.prepare_effect('r', self.work['id'], 'lab', self.packet,
            {'model_calls': 1, 'tokens': 100, 'wall_seconds': 10}, 'effect', metadata={
                'agent_session': self.session, 'agent_session_id': self.session['id'],
                'expected_session_head': 0, 'expected_memory_revision': 1})
        with self.assertRaises(KeyboardInterrupt):
            self.controller.dispatch_effect('r', self.effect['id'], self._interrupted_executor)
        self.effect = self.store.get('r', 'effect', self.effect['id'])
        self.baseline = self.store.verify_history('r')

    def _interrupted_executor(self, effect, packet):
        self.effect = effect
        self.write_artifacts()
        raise KeyboardInterrupt()

    def write_artifacts(self, *, command_delta=None, native_extra=None, frame_extra=None,
                        result_delta=None, invocation_delta=None, prefix=None, raw_tail=None):
        self.folder = self.store.root / 'calls' / self.effect['id']
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.folder.mkdir(parents=True)
        self.workspace = self.store.root / 'agents' / self.session['id'] / 'workspace'
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.home = (Path(self.tmp.name) / 'native').resolve()
        (self.home / 'sessions').mkdir(parents=True, exist_ok=True)
        binding = {'session_id': self.session['id'], 'workspace': str(self.workspace),
            'provider_thread_id': self.effect['metadata']['agent_session']['provider_thread_id'],
            'codex_home': str(self.home)}
        invocation = {'backend': 'codex', 'model': self.request['model'], 'effort': self.request['effort'],
            'command': _command(self.folder, self.request['model'], self.request['effort'],
                                str(self.workspace), binding, []),
            'cwd': str(self.workspace), 'session': binding, 'dispatched': False,
            'started_at_unix': time.time() - 6}
        if command_delta:
            command_delta(invocation['command'])
        self.put('prompt.txt', self.request['prompt'].encode())
        self.put('schema.json', self.request['response_schema'])
        start = {'run_id': 'r', 'effect_id': self.effect['id'], 'agent_id': 'lab',
            'session_id': self.session['id'], 'turn_id': self.effect['metadata']['agent_turn_id'],
            'fence': self.effect['metadata']['session_fence'], 'invocation': copy.deepcopy(invocation),
            'prompt_sha256': digest(self.request['prompt'].encode())}
        frames = [{'type': 'thread.started', 'thread_id': THREAD}, {'type': 'turn.started'}]
        if frame_extra:
            frames.append(frame_extra)
        with JournalWriter(self.folder / 'journal.jsonl', start) as journal:
            for event in frames:
                raw = encode(event) + b'\n'
                journal.append('stdout', raw)
                journal.append('stdout', raw, kind='event_frame')
            if raw_tail:
                journal.append('stdout', raw_tail)
            journal.append('control', b'KeyboardInterrupt: ', kind='stream_incomplete')
            capture = journal.snapshot()
        invocation.update(dispatched=True, returncode=-9,
                          finished_at_unix=invocation['started_at_unix'] + 5)
        invocation.update(invocation_delta or {})
        self.put('invocation.json', invocation)
        native_rows = [
            {'type': 'session_meta', 'payload': {'id': THREAD, 'cwd': str(self.workspace)}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': self.request['prompt']}]}}]
        if native_extra:
            native_rows.append(native_extra)
        self.native = self.home / 'sessions' / ('rollout-synthetic-' + THREAD + '.jsonl')
        self.native.write_bytes((prefix or b'') + b'\n'.join(encode(row) for row in native_rows) + b'\n')
        result = {'backend': 'codex', 'execution_status': 'unknown', 'dispatched': True,
            'terminal_received': False, 'stream_complete': False, 'response': None, 'usage': None,
            'usage_complete': False, 'protocol_valid': False, 'error': 'KeyboardInterrupt: ',
            'returncode': -9, 'wall_seconds': 5, 'journal': capture,
            'provider_thread_id': THREAD, 'session_id': self.session['id'],
            'workspace': str(self.workspace), 'codex_home': str(self.home),
            'native_session_ref': str(self.native), 'native_session_error': None,
            'native_session_sha256': digest(self.native.read_bytes())}
        result.update(result_delta or {})
        self.put('transport_result.json', result)
        self.put('raw_response.txt', b'')
        self.put('response.json', None)
        self.put('usage.json', {'usage': None, 'usage_complete': False})
        pause = {'schema_version': 'user_requested_experiment_pause_v1', 'run_id': 'r', 'status': 'paused',
            'pending_model_effect_id': self.effect['id'], 'host_pid': 991001,
            'host_exited': True, 'owned_model_processes_exited': True,
            'transport_receipt_sha256': digest(encode(result))}
        self.pause = self.store.root / 'pause.json'
        self.pause.write_bytes(encode(pause))
        self.attestation = {'schema_version': ATTESTATION_SCHEMA, 'effect_id': self.effect['id'],
            'packet_sha256': digest(self.packet), 'reason': 'Operator observed all owned processes stopped',
            'pause_path': str(self.pause), 'pause_sha256': digest(self.pause.read_bytes()),
            'checked_at_unix': time.time(), 'checked_pids': [991001, 991002],
            'native_writer_quiesced': True, 'all_owned_writers_checked': True}

    def put(self, filename, value):
        (self.folder / filename).write_bytes(value if isinstance(value, bytes) else encode(value))

    def certify(self, attestation=None):
        with patch('rsi_lab.interruption.os.kill', side_effect=ProcessLookupError), \
             patch('subprocess.Popen', side_effect=AssertionError('Must not launch native model')):
            return recover_interrupted_codex_receipt(self.effect, self.packet,
                self.store.root / 'calls', attestation or self.attestation)

    def quarantine(self, attestation=None):
        with patch('rsi_lab.interruption.os.kill', side_effect=ProcessLookupError), \
             patch('subprocess.Popen', side_effect=AssertionError('Must not launch native model')):
            return self.controller.quarantine_interrupted_model('r', self.effect['id'],
                                                               attestation or self.attestation, 'isolate')

    def test_known_costs_and_unknown_holds_survive_new_session_without_new_request(self):
        with self.assertRaisesRegex(RecoveryBlocked, 'journal_gap'):
            recover_codex_receipt(self.effect, self.packet, self.store.root / 'calls')
        record = self.quarantine()
        effect = self.store.get('r', 'effect', self.effect['id'])
        self.assertEqual(effect['status'], 'unknown')
        self.assertTrue(effect['accounting_unknown'])
        self.assertEqual(effect['actual_costs'], {'model_calls': 1, 'wall_seconds': 5})
        self.assertEqual(effect['held_reservations'], {'model_calls': 0, 'tokens': 100, 'wall_seconds': 5})
        self.assertTrue(effect_is_quarantined(effect))
        self.assertFalse(effect_holds_execution(effect))
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['reserved'], 100)
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['spent'], 0)
        self.assertEqual(self.store.get('r', 'dependency', 'external_capacity')['value']['available'], 1)
        self.assertEqual(self.store.get('r', 'work', self.work['id'])['status'], 'cancelled')
        turn = self.store.get('r', 'agent_turn', record['turn_id'])
        self.assertEqual((turn['status'], turn['memory_update_status']), ('rejected', 'discarded'))
        old = self.store.get('r', 'agent_session', self.session['id'])
        self.assertEqual((old['status'], old['fence']), ('superseded', self.effect['metadata']['session_fence'] + 1))
        new = self.state.ensure_session('lab')
        self.assertEqual(new['id'], record['new_session_id'])
        self.assertIsNone(new['provider_thread_id'])
        self.assertEqual(new['committed_turn'], 0)
        self.assertEqual(self.state.working_memory(new['id']), self.memory)
        self.assertEqual(len(self.store.list('r', 'effect')), 1)
        self.assertEqual(len(self.store.list('r', 'work')), 1)
        proof = json.loads(self.store.read_blob(record['proof_ref']))
        self.assertFalse(proof['terminal_received'])
        self.assertFalse(proof['answer_observed'])
        self.assertEqual(proof['packet_sha256'], self.effect['packet_hash'])
        self.assertEqual(json.loads(self.store.read_blob(record['original_effect_ref'])), self.effect)
        self.assertEqual(proof['artifacts']['native']['sha256'], digest(self.native.read_bytes()))
        self.store.verify_history('r')

    def test_replay_is_idempotent_but_changed_attestation_and_late_output_cannot_revive_owner(self):
        record = self.quarantine()
        history = self.store.verify_history('r')
        with patch('rsi_lab.interruption.os.kill', side_effect=AssertionError('Historical replay must not recheck reused PIDs')):
            self.assertEqual(self.controller.quarantine_interrupted_model('r', self.effect['id'],
                            self.attestation, 'another-key'), record)
        self.assertEqual(self.store.verify_history('r'), history)
        with self.assertRaises(ValueError):
            self.quarantine({**self.attestation, 'reason': 'changed'})
        new_session = self.state.ensure_session('lab')
        settled = self.controller.reconcile('r', self.effect['id'], {'status': 'succeeded',
            'actual_costs': {'tokens': 37}, 'response': {'late': 'never apply'},
            'reconciliation': {'effect_id': self.effect['id'], 'provenance': 'Synthetic later provider receipt'}}, 'later-cost')
        self.assertTrue(effect_is_quarantined(settled))
        self.assertFalse(settled['accounting_unknown'])
        self.assertEqual(self.store.get('r', 'work', self.work['id'])['status'], 'cancelled')
        self.assertEqual(self.state.ensure_session('lab'), new_session)
        self.assertEqual(self.state.working_memory(new_session['id']), self.memory)
        self.assertEqual(self.controller.recover('r'), [])
        with self.assertRaises(ValueError):
            self.controller.dispatch_effect('r', self.effect['id'], lambda *_: self.fail('No redispatch'))

    def test_freed_physical_capacity_does_not_remove_financial_admission_holds(self):
        self.quarantine()
        work = self.controller.enqueue('r', 'lab', 'fresh', 'fresh-work')
        with self.assertRaisesRegex(ValueError, 'Budget admission denied: tokens'):
            self.controller.prepare_effect('r', work['id'], 'lab', b'{}', {'tokens': 201}, 'too-expensive')
        fresh = self.controller.prepare_effect('r', work['id'], 'lab', b'{}',
                                               {'model_calls': 1, 'tokens': 100}, 'fresh-effect')
        self.assertEqual(fresh['status'], 'prepared')
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['reserved'], 200)
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)

    def test_invalid_artifacts_refuse_before_settlement(self):
        cases = [
            {'raw_tail': b'{"type":"turn.completed","usage":'},
            {'command_delta': lambda command: command.remove('--ignore-user-config')},
            {'frame_extra': {'type': 'turn.completed', 'usage': {'input_tokens': 3}}},
            {'frame_extra': {'type': 'item.completed', 'item': {'type': 'command_execution'}}},
            {'native_extra': {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': []}}},
            {'native_extra': {'type': 'event_msg', 'payload': {'type': 'token_usage_record', 'input_tokens': 3}}},
            {'invocation_delta': {'returncode': 0}},
            {'result_delta': {'response': {'answer': 'cannot discard'}}},
            {'result_delta': {'wall_seconds': 20}},
            {'result_delta': {'native_session_sha256': '0' * 64}},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.write_artifacts(**case)
                with self.assertRaises(RecoveryBlocked):
                    self.quarantine()
                self.assertEqual(self.store.verify_history('r'), self.baseline)
                self.assertEqual(self.store.get('r', 'effect', self.effect['id'])['status'], 'dispatching')

    def test_attestation_requires_all_bindings_and_quiescent_pids(self):
        for delta in ({'packet_sha256': '0' * 64}, {'pause_sha256': '0' * 64},
                      {'native_writer_quiesced': False}, {'all_owned_writers_checked': False},
                      {'checked_pids': [991002, 991003]}, {'invented': True}):
            with self.subTest(delta=delta), self.assertRaises(RecoveryBlocked):
                self.certify({**self.attestation, **delta})
        with patch('rsi_lab.interruption.os.kill', return_value=None), self.assertRaisesRegex(RecoveryBlocked, 'still alive'):
            recover_interrupted_codex_receipt(self.effect, self.packet, self.store.root / 'calls', self.attestation)
        self.assertEqual(self.store.verify_history('r'), self.baseline)

    def test_source_provenance_pair_must_match_executing_code(self):
        import rsi_lab.interruption as implementation
        root = Path(implementation.__file__).resolve().parent.parent
        paths = set()
        for directory in ('rsi_lab', 'rsi_domains', 'rsi_literature', 'experiments/pablo'):
            paths.update((root / directory).rglob('*.py'))
        manifest = {str(path.relative_to(root)): digest(path.read_bytes()) for path in sorted(paths)
                    if 'tests' not in path.relative_to(root).parts}
        attestation = {**self.attestation, 'source_root': str(root), 'source_manifest_sha256': digest(manifest)}
        proof = self.certify(attestation)['interruption_quarantine_proof']
        self.assertEqual(proof['operator_attestation'], attestation)
        self.assertEqual(proof['recovery_implementation']['source_root'], str(root))
        for delta in ({'source_manifest_sha256': '0' * 64}, {'source_root': str(self.store.root)}):
            with self.subTest(delta=delta), self.assertRaises(RecoveryBlocked):
                self.certify({**attestation, **delta})
        with self.assertRaises(RecoveryBlocked):
            self.certify({**self.attestation, 'source_root': str(root)})

    def test_resume_prefix_and_changed_fence_are_rejected(self):
        self.effect = copy.deepcopy(self.effect)
        session = self.effect['metadata']['agent_session']
        session['provider_thread_id'] = THREAD
        session['context_manifest'] = {'native_prefix': {'prefix_bytes': 5,
            'prefix_sha256': '0' * 64, 'provider_thread_id': THREAD}}
        self.write_artifacts()
        with self.assertRaises(ValueError):
            self.certify()
        self.store.mutate('r', 'fixture-fence-change', 'fixture_changed_fence', lambda tx:
                         tx.update('agent_session', self.session['id'], {'fence': 99}))
        with self.assertRaisesRegex(ValueError, 'still-owned'):
            self.quarantine()
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 0)

    def test_crash_after_known_cost_settlement_replays_without_double_charging(self):
        with patch.object(AgentState, 'quarantine_interrupted_turn_tx', side_effect=RuntimeError('synthetic crash')), \
             self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
            self.quarantine()
        effect = self.store.get('r', 'effect', self.effect['id'])
        self.assertEqual(effect['status'], 'unknown')
        self.assertTrue(effect_holds_execution(effect))
        record = self.quarantine()
        self.assertTrue(effect_is_quarantined(self.store.get('r', 'effect', self.effect['id'])))
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)
        self.assertEqual(self.store.get('r', 'budget', 'wall_seconds')['spent'], 5)
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['reserved'], 100)
        self.assertEqual(self.state.working_memory(record['new_session_id']), self.memory)

    def test_multiple_effect_turn_and_concurrent_work_change_refuse_isolation(self):
        turn_id = self.effect['metadata']['agent_turn_id']
        self.store.mutate('r', 'fixture-other-effect', 'fixture_other_effect', lambda tx:
            tx.update('agent_turn', turn_id, {'effect_ids': [self.effect['id'], 'other_effect']}))
        with self.assertRaisesRegex(ValueError, 'still-owned'):
            self.quarantine()
        self.store.mutate('r', 'fixture-reset-turn', 'fixture_reset_turn', lambda tx:
            tx.update('agent_turn', turn_id, {'effect_ids': [self.effect['id']]}))
        original = self.controller.reconcile
        def change_work(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.mutate('r', 'concurrent-work', 'fixture_concurrent_work', lambda tx:
                tx.update('work', self.work['id'], {'status': 'cancelled'}))
            return result
        with patch.object(self.controller, 'reconcile', side_effect=change_work), \
             self.assertRaisesRegex(ValueError, 'Work or proposal changed'):
            self.quarantine()
        self.assertFalse(effect_is_quarantined(self.store.get('r', 'effect', self.effect['id'])))
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['reserved'], 100)

    def test_compare_and_swap_does_not_hide_concurrent_owner_change(self):
        original = self.controller.reconcile
        def change_owner(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.mutate('r', 'concurrent-owner', 'fixture_concurrent_owner', lambda tx:
                tx.update('agent_owner', self.session['owner_id'], {'session_generation': 99}))
            return result
        with patch.object(self.controller, 'reconcile', side_effect=change_owner), \
             self.assertRaisesRegex(ValueError, 'owner changed'):
            self.quarantine()
        effect = self.store.get('r', 'effect', self.effect['id'])
        self.assertEqual(effect['status'], 'unknown')
        self.assertFalse(effect_is_quarantined(effect))
        self.assertTrue(effect_holds_execution(effect))
        self.assertEqual(effect['held_reservations']['tokens'], 100)
        self.assertEqual(effect['actual_costs']['model_calls'], 1)
        self.assertEqual(self.store.get('r', 'dependency', 'external_capacity')['value']['available'], 0)


if __name__ == '__main__':
    unittest.main()
