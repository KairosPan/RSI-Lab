import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.call_recovery import RecoveryBlocked, recover_codex_receipt
from rsi_lab.journal import JournalWriter
from rsi_lab.store import digest, encode
from rsi_lab.transport import PREAMBLE

THREAD = '018fe27b-a111-7222-8333-123456789abc'


class CallRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = self.root / 'calls'
        self.folder = self.calls / 'effect_test'
        self.folder.mkdir(parents=True)
        self.workspace = self.root / 'agents' / 'session_test' / 'workspace'
        self.workspace.mkdir(parents=True)
        self.home = self.root / 'native-home'
        (self.home / 'sessions').mkdir(parents=True)
        self.request = {'schema_version': 'rsi_model_request_v2', 'mode': 'lab',
            'model': 'synthetic-model', 'effort': 'low', 'prompt': PREAMBLE + 'Synthetic recovery input',
            'response_schema': {'type': 'object'}, 'input_view_mode': 'json'}
        self.packet = encode(self.request)
        self.session = {'id': 'session_test', 'agent_instance_id': 'lab', 'provider_thread_id': None,
                        'context_manifest': None}
        self.effect = {'id': 'effect_test', 'run_id': 'r', 'status': 'unknown',
            'packet_sha256': digest(self.packet), 'metadata': {'agent_session': self.session,
                'agent_session_id': 'session_test', 'agent_turn_id': 'turn_test', 'session_fence': 1,
                'effective_context_hash': 'context_hash'}}

    def fixture(self, *, saved=True, terminal=True, finished=True, raw='{"answer":"synthetic"}',
                usage=True, journal_delta=None, failed=False):
        binding = {'session_id': 'session_test', 'workspace': str(self.workspace),
            'provider_thread_id': self.session['provider_thread_id'], 'codex_home': str(self.home)}
        invocation = {'backend': 'codex', 'model': self.request['model'], 'effort': 'low',
            'command': ['codex', 'exec', '--json'], 'cwd': str(self.workspace), 'session': binding,
            'dispatched': False}
        (self.folder / 'prompt.txt').write_text(self.request['prompt'])
        (self.folder / 'schema.json').write_bytes(encode(self.request['response_schema']))
        (self.folder / 'invocation.json').write_bytes(encode({**invocation, 'dispatched': True}))
        start = {'run_id': 'r', 'effect_id': 'effect_test', 'agent_id': 'lab',
            'session_id': 'session_test', 'turn_id': 'turn_test', 'fence': 1,
            'invocation': invocation, 'prompt_sha256': digest(self.request['prompt'].encode())}
        start.update(journal_delta or {})
        events = [{'type': 'thread.started', 'thread_id': THREAD}, {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': raw}}]
        counts = {'input_tokens': 11, 'output_tokens': 7} if usage else None
        if terminal:
            events.append({'type': 'turn.failed' if failed else 'turn.completed',
                           **({'usage': counts} if counts else {})})
        with JournalWriter(self.folder / 'journal.jsonl', start) as journal:
            for event in events:
                frame = encode(event) + b'\n'
                journal.append('stdout', frame)
                journal.append('stdout', frame, kind='event_frame')
            if finished:
                journal.append('control', encode({'returncode': 1 if failed else 0,
                    'execution_status': 'failed_definite' if failed else 'succeeded',
                    'parse_error': None, 'error': None}), kind='attempt_finished')
            journal_snapshot = journal.snapshot()
        native = self.home / 'sessions' / ('rollout-synthetic-' + THREAD + '.jsonl')
        native.write_bytes(b'\n'.join(encode(row) for row in [
            {'type': 'session_meta', 'payload': {'id': THREAD, 'cwd': str(self.workspace)}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': self.request['prompt']}]}}]) + b'\n')
        if saved:
            try:
                response = json.loads(raw)
            except ValueError:
                response = None
            (self.folder / 'transport_result.json').write_bytes(encode({
                'provider_thread_id': THREAD, 'session_id': 'session_test', 'terminal_received': terminal,
                'journal': journal_snapshot, 'usage': counts, 'usage_complete': bool(counts),
                'stream_complete': True, 'execution_status': 'failed_definite' if failed else 'succeeded',
                'returncode': 1 if failed else 0, 'response': None if failed else response, 'wall_seconds': 1.25}))
        return journal_snapshot

    def recover(self):
        with patch('subprocess.Popen', side_effect=AssertionError('Recovery must not launch Codex')):
            return recover_codex_receipt(self.effect, self.packet, self.calls)

    def test_saved_transport_result_recovers_bound_receipt_and_native_prefix(self):
        self.fixture()
        result = self.recover()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['actual_costs'], {'model_calls': 1, 'tokens': 18, 'wall_seconds': 1.25})
        self.assertTrue(result['native_writer_quiesced'])
        self.assertFalse(result['recovery_requires_fencing'])
        self.assertEqual(result['sent_packet_sha256'], digest(self.packet))
        self.assertEqual(result['provider_thread_id'], THREAD)
        self.assertIn('native_prefix', result['context_manifest'])
        self.assertFalse(result['context_manifest']['retained_read_authority'])

    def test_terminal_journal_without_result_preserves_unknown_wall_cost(self):
        self.fixture(saved=False)
        result = self.recover()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['actual_costs'], {'model_calls': 1, 'tokens': 18})
        self.assertTrue(result['native_writer_quiesced'])
        self.assertFalse((self.folder / 'transport_result.json').exists())

    def test_terminal_without_process_exit_requires_fencing_before_resume(self):
        self.fixture(saved=False, finished=False)
        result = self.recover()
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(result['native_writer_quiesced'])
        self.assertTrue(result['recovery_requires_fencing'])
        self.assertNotIn('wall_seconds', result['actual_costs'])

    def test_known_failed_turn_is_not_a_protocol_violation(self):
        self.fixture(failed=True)
        result = self.recover()
        self.assertEqual(result['status'], 'failed_definite')
        self.assertTrue(result['terminal_received'])
        self.assertTrue(result['protocol_valid'])
        self.assertIsNone(result['response'])
        self.assertTrue(result['error'])

    def test_changed_resume_prefix_keeps_known_cost_but_blocks_memory_commit(self):
        self.session['provider_thread_id'] = THREAD
        self.session['context_manifest'] = {'native_prefix': {
            'prefix_bytes': 5, 'prefix_sha256': '0' * 64, 'provider_thread_id': THREAD}}
        self.fixture()
        result = self.recover()
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(result['protocol_valid'])
        self.assertEqual(result['actual_costs']['tokens'], 18)
        self.assertTrue(result['context_manifest']['native_session_error'])

    def test_saved_terminal_native_hash_cannot_be_replaced(self):
        self.fixture()
        path = self.folder / 'transport_result.json'
        saved = json.loads(path.read_bytes())
        saved['native_session_sha256'] = '0' * 64
        path.write_bytes(encode(saved))
        result = self.recover()
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(result['protocol_valid'])
        self.assertTrue(result['context_manifest']['native_session_error'])

    def test_no_terminal_does_not_invent_completion(self):
        self.fixture(saved=False, terminal=False, finished=False)
        self.assertIsNone(self.recover())

    def test_finished_first_turn_requires_owned_native_snapshot(self):
        self.fixture(saved=False)
        native = self.home / 'sessions' / ('rollout-synthetic-' + THREAD + '.jsonl')
        native.unlink()
        result = self.recover()
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['native_writer_quiesced'])
        self.assertFalse(result['protocol_valid'])
        self.assertTrue(result['context_manifest']['native_session_error'])
        self.assertEqual(result['actual_costs']['tokens'], 18)

    def test_saved_protocol_rejection_is_not_reclassified_as_valid(self):
        self.fixture()
        path = self.folder / 'transport_result.json'
        saved = json.loads(path.read_bytes())
        saved['protocol_valid'] = False
        path.write_bytes(encode(saved))
        result = self.recover()
        self.assertFalse(result['protocol_valid'])
        self.assertEqual(result['status'], 'succeeded')

    def test_missing_attempt_is_not_an_execution_failure(self):
        self.assertIsNone(self.recover())

    def test_foreign_journal_and_changed_prompt_are_rejected(self):
        self.fixture(journal_delta={'run_id': 'other'})
        with self.assertRaises(RecoveryBlocked):
            self.recover()
        (self.folder / 'prompt.txt').write_text('different prompt')
        with self.assertRaises(RecoveryBlocked):
            self.recover()

    def test_saved_response_must_match_immutable_provider_events(self):
        self.fixture()
        p = self.folder / 'transport_result.json'
        result = json.loads(p.read_bytes()); result['response'] = {'altered': True}
        p.write_bytes(encode(result))
        with self.assertRaises(RecoveryBlocked):
            self.recover()

    def test_invalid_json_still_recovers_known_completion(self):
        self.fixture(raw='not JSON')
        result = self.recover()
        self.assertEqual(result['status'], 'succeeded')
        self.assertIsNone(result['response'])
        self.assertTrue(result['parse_error'])
        self.assertEqual(result['actual_costs']['tokens'], 18)

    def test_missing_usage_does_not_become_zero(self):
        self.fixture(usage=False)
        result = self.recover()
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('tokens', result['actual_costs'])
        self.assertFalse(result['usage_complete'])

    def test_journal_gap_and_symlink_cannot_certify_recovery(self):
        self.fixture()
        with (self.folder / 'journal.jsonl').open('ab') as f:
            f.write(b'{"torn":')
        with self.assertRaises(RecoveryBlocked):
            self.recover()

    def test_saved_schema_symlink_is_rejected(self):
        self.fixture()
        schema = self.folder / 'schema.json'
        schema.rename(self.root / 'schema.json')
        schema.symlink_to(self.root / 'schema.json')
        with self.assertRaises(RecoveryBlocked):
            self.recover()

    def test_resume_cannot_change_native_thread_identity(self):
        self.session['provider_thread_id'] = '018fe27b-a111-7222-8333-123456789abd'
        self.fixture()
        with self.assertRaises(RecoveryBlocked):
            self.recover()


if __name__ == '__main__':
    unittest.main()
