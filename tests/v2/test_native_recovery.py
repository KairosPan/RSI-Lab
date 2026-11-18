"""Recovery of a call that ran with native capabilities, and the refusal to isolate one.

The call folders are built as tests/v2/test_call_recovery.py and tests/v2/test_interrupted_model_quarantine.py build
theirs. The journal holds the stream ``NATIVE_STREAM`` of tests/v2/test_native_transport.py; the session resumes a
thread whose rollout is the one of the first test of tests/v2/test_codex_ledger.py, this call's records built from
tests/v2/native_fixtures.py. The last tests run one call through the real Controller, executor and transport against
a local process standing in for Codex, then recover it from what it left. No Codex, model, network request or
credential is used.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from rsi_lab import native, roles, transport
from rsi_lab.call_recovery import RecoveryBlocked, recover_codex_receipt, recover_interrupted_codex_receipt
from rsi_lab.interruption import ATTESTATION_SCHEMA, certify_interruption
from rsi_lab.journal import JournalWriter
from rsi_lab.store import digest, encode
from tests.v2.liveness_support import open_run
from tests.v2.native_fixtures import PROFILE, SEARCH, TERMINAL, TERMINAL_AND_SEARCH, tool_call, turn_context, usage_row
from tests.v2.test_native_executor import RESERVATION, RESPONSE, config_form
from tests.v2.test_native_transport import NATIVE_ACTIVITY, NATIVE_STREAM, THREAD
from tests.v2.test_session_context import fixture

CALL, SESSION = 'effect_test', 'session_test'
EXEC = 'text(await tools.exec_command({cmd: "cat in.txt"}))'
TOOL_FREE_STREAM = [*NATIVE_STREAM[:2], NATIVE_STREAM[-2], NATIVE_STREAM[-1]]
FAILED_STREAM = [*NATIVE_STREAM[:2], {'type': 'turn.failed', 'error': {'message': 'Synthetic provider failure'}}]
NATIVE_KEYS = {'native_capabilities', 'native_activity', 'child_threads'}


def rows(turn):
    """A call's two provider requests in ``turn``, 60000/100 and 42016/5055 tokens: 107171 in all."""
    return usage_row(THREAD, turn, turn, 60000, 100) + usage_row(THREAD, turn, turn, 42016, 5055)


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.calls_root = self.root / 'calls'
        self.folder = self.calls_root / CALL
        self.workspace = self.root / 'agents' / SESSION / 'workspace'
        self.workspace.mkdir(parents=True)
        self.home = self.root / 'native-home'
        self.rollout = self.home / 'sessions' / ('rollout-synthetic-' + THREAD + '.jsonl')
        self.rollout.parent.mkdir(parents=True)
        self.request = {'schema_version': 'rsi_model_request_v2', 'mode': 'lab', 'model': 'synthetic-model',
                        'effort': 'low', 'prompt': native.PREAMBLE + 'Synthetic recovery input',
                        'response_schema': {'type': 'object'}, 'input_view_mode': 'json'}
        self.packet = encode(self.request)
        # The call resumes its session's thread, whose committed prefix ends with the turn before it.
        self.prefix = fixture(self.workspace, thread=THREAD) + usage_row(THREAD, 't1', 't1', 54394, 5524)
        self.session = {'id': SESSION, 'agent_instance_id': 'lab', 'provider_thread_id': THREAD,
                        'context_manifest': {'native_prefix': {'prefix_bytes': len(self.prefix),
                                                               'prefix_sha256': digest(self.prefix),
                                                               'provider_thread_id': THREAD}}}
        pause = self.root / 'pause.json'
        pause.write_bytes(encode({'schema_version': 'user_requested_experiment_pause_v1', 'run_id': 'r',
                                  'status': 'paused', 'pending_model_effect_id': CALL, 'host_pid': 991001,
                                  'host_exited': True, 'owned_model_processes_exited': True}))
        self.attestation = {'schema_version': ATTESTATION_SCHEMA, 'effect_id': CALL,
                            'packet_sha256': digest(self.packet),
                            'reason': 'Operator observed all owned processes stopped', 'pause_path': str(pause),
                            'pause_sha256': digest(pause.read_bytes()), 'checked_at_unix': time.time(),
                            'checked_pids': [991001, 991002], 'native_writer_quiesced': True,
                            'all_owned_writers_checked': True}
        launch = patch('subprocess.Popen', side_effect=AssertionError('Recovery must not launch Codex'))
        launch.start(); self.addCleanup(launch.stop)
        self.write_call(TERMINAL_AND_SEARCH)

    def effect(self, *, settings):
        """The call's model Effect, whose metadata freezes ``settings``; an Effect names none when they are ``None``."""
        metadata = {'agent_session': self.session, 'agent_session_id': SESSION, 'agent_turn_id': 'turn_test',
                    'session_fence': 1, 'effective_context_hash': 'context_hash'}
        if settings is not None:
            metadata['native_capabilities'] = settings
        return {'id': CALL, 'run_id': 'r', 'kind': 'lab', 'status': 'unknown', 'packet_sha256': digest(self.packet),
                'reservations': dict(RESERVATION), 'metadata': metadata}

    def this_turn(self, *, profile=True):
        """What the call adds to the rollout: its turn context, under the confined permission profile unless
        ``profile`` is false, one terminal command and its two provider requests."""
        confined = PROFILE(self.root, self.home, self.workspace) if profile else None
        return turn_context('t2', confined, cwd=self.workspace) + tool_call('c1', EXEC) + rows('t2')

    def write_call(self, settings, *, stream=NATIVE_STREAM, turn=None, saved=True, descendants_stopped=True,
                   interrupted=False):
        """Leave the call's folder and rollout as ``transport.invoke`` leaves them after Codex ran under ``settings``
        and wrote ``stream``. The rollout is the committed prefix, then ``turn`` (default ``this_turn()``).

        ``saved`` false leaves no transport result, as when the host died before writing it. ``interrupted`` leaves
        the call stopped once its turn started: the journal ends with the KeyboardInterrupt marker and no terminal.
        """
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.folder.mkdir(parents=True)
        binding = {'session_id': SESSION, 'workspace': str(self.workspace), 'provider_thread_id': THREAD,
                   'codex_home': str(self.home)}
        invocation = {'backend': 'codex', 'model': self.request['model'], 'effort': self.request['effort'],
                      'command': ['codex', 'exec', '--json'], 'cwd': str(self.workspace), 'session': binding,
                      'dispatched': False}
        if native.any_on(settings):
            invocation['native'] = settings
        (self.folder / 'prompt.txt').write_text(self.request['prompt'])
        (self.folder / 'schema.json').write_bytes(encode(self.request['response_schema']))
        (self.folder / 'invocation.json').write_bytes(encode({**invocation, 'dispatched': True}))
        start = {'run_id': 'r', 'effect_id': CALL, 'agent_id': 'lab', 'session_id': SESSION, 'turn_id': 'turn_test',
                 'fence': 1, 'invocation': invocation, 'prompt_sha256': digest(self.request['prompt'].encode())}
        failed = any(event['type'] == 'turn.failed' for event in stream)
        with JournalWriter(self.folder / 'journal.jsonl', start) as journal:
            for event in stream[:2] if interrupted else stream:
                frame = encode(event) + b'\n'
                journal.append('stdout', frame)
                journal.append('stdout', frame, kind='event_frame')
            if interrupted:
                journal.append('control', b'KeyboardInterrupt: ', kind='stream_incomplete')
            else:
                journal.append('control', encode({'returncode': 1 if failed else 0,
                    'execution_status': 'failed_definite' if failed else 'succeeded',
                    'parse_error': None, 'error': None}), kind='attempt_finished')
            snapshot = journal.snapshot()
        self.rollout.write_bytes(self.prefix + (self.this_turn() if turn is None else turn))
        if saved and not interrupted:
            answers = [event['item']['text'] for event in stream
                       if event['type'] == 'item.completed' and event['item']['type'] == 'agent_message']
            (self.folder / 'transport_result.json').write_bytes(encode({
                'provider_thread_id': THREAD, 'session_id': SESSION, 'terminal_received': True, 'journal': snapshot,
                'usage': next((event['usage'] for event in stream if 'usage' in event), None),
                'usage_complete': not failed, 'stream_complete': True,
                'execution_status': 'failed_definite' if failed else 'succeeded', 'returncode': 1 if failed else 0,
                'response': None if failed else json.loads(answers[-1]), 'wall_seconds': 1.25,
                'descendants_stopped': descendants_stopped, 'native_session_sha256': digest(self.rollout.read_bytes()),
                **({'native': settings} if native.any_on(settings) else {})}))

    def rewrite_invocation(self, **changes):
        """Rewrite the call's ``invocation.json`` with ``changes``; a value of ``None`` removes its key."""
        path = self.folder / 'invocation.json'
        invocation = {**json.loads(path.read_bytes()), **changes}
        path.write_bytes(encode({key: value for key, value in invocation.items() if value is not None}))

    def recover(self, settings):
        return recover_codex_receipt(self.effect(settings=settings), self.packet, self.calls_root)

    def test_a_native_call_recovers_with_its_tool_items_and_ledger_cost(self):
        receipt = recover_codex_receipt(self.effect(settings=TERMINAL_AND_SEARCH), self.packet, self.calls_root)
        self.assertEqual(receipt['status'], 'succeeded'); self.assertEqual(receipt['actual_costs']['tokens'], 107171)
        self.assertEqual(receipt['response'], {'ok': True})
        self.assertTrue(receipt['protocol_valid']); self.assertIsNone(receipt['error'])
        self.assertEqual((receipt['token_source'], receipt['token_reason']), ('ledger', None))
        self.assertEqual(receipt['ledger'], {'requests': 2, 'input_tokens': 60000 + 42016, 'output_tokens': 100 + 5055,
                                             'cached_input_tokens': 0})
        self.assertEqual(receipt['stream_usage'], {'input_tokens': 20, 'output_tokens': 5})
        self.assertTrue(receipt['usage_complete'])
        self.assertEqual(receipt['native_capabilities'], TERMINAL_AND_SEARCH)
        activity = receipt['native_activity']
        self.assertEqual({name: activity[name] for name in NATIVE_ACTIVITY}, NATIVE_ACTIVITY)
        self.assertEqual((activity['tool_calls'], activity['provider_requests'], activity['compactions']), (1, 2, 0))
        self.assertEqual(activity['tool_inputs'][0]['excerpt'], EXEC)
        self.assertEqual((receipt['child_threads'], receipt['account_snapshot']), ([], None))
        self.assertNotIn('tool_calls', receipt['actual_costs'])
        # The rollout, tool records and all, was read with the call's settings.
        self.assertIsNone(receipt['context_manifest']['native_session_error'])
        self.assertEqual(receipt['context_manifest']['native_prefix']['prefix_sha256'],
                         digest(self.rollout.read_bytes()))
        self.assertTrue(receipt['native_writer_quiesced']); self.assertFalse(receipt['recovery_requires_fencing'])

    def test_changed_settings_block_recovery(self):
        self.rewrite_invocation(native=None)
        with self.assertRaisesRegex(RecoveryBlocked, 'recovery_binding_mismatch'):
            recover_codex_receipt(self.effect(settings=TERMINAL_AND_SEARCH), self.packet, self.calls_root)
        self.write_call(TERMINAL_AND_SEARCH)
        self.rewrite_invocation(native=SEARCH)
        with self.assertRaisesRegex(RecoveryBlocked, 'recovery_binding_mismatch'):
            self.recover(TERMINAL_AND_SEARCH)
        # The journal and invocation.json agree, but the Effect froze other settings.
        for ran, frozen in ((TERMINAL_AND_SEARCH, TERMINAL), (TERMINAL_AND_SEARCH, None),
                            (None, TERMINAL_AND_SEARCH), (None, native.OFF)):
            with self.subTest(ran=ran, frozen=frozen):
                self.write_call(ran, stream=NATIVE_STREAM if ran else TOOL_FREE_STREAM,
                                turn=self.this_turn() if ran else rows('t2'))
                with self.assertRaisesRegex(RecoveryBlocked, 'recovery_binding_mismatch'):
                    self.recover(frozen)

    def test_a_native_call_cannot_be_isolated(self):
        self.write_call(TERMINAL, interrupted=True)
        with self.assertRaisesRegex(RecoveryBlocked, 'native capabilities') as refused:
            recover_interrupted_codex_receipt(self.effect(settings=TERMINAL), self.packet, self.calls_root,
                                              self.attestation)
        self.assertEqual(refused.exception.reason, 'interruption_not_certified')

    def test_isolation_refuses_native_settings_before_any_other_check(self):
        # Only the Effect is given: any other check would fail on the missing artifacts and attestation.
        for settings in (SEARCH, TERMINAL, {'web_search': 'yes'}):
            with self.subTest(settings=settings), self.assertRaisesRegex(RecoveryBlocked,
                    'interruption_not_certified: A call with native capabilities cannot be certified free of tool '
                    'activity'):
                certify_interruption(self.effect(settings=settings), *[None] * 7)
        # With everything off the same Effect passes the Effect checks and stops at the missing attestation.
        with self.assertRaisesRegex(RecoveryBlocked, 'explicit closed operator attestation'):
            certify_interruption(self.effect(settings=None), *[None] * 7)

    def test_everything_off_recovers_as_today_with_its_cost_from_the_ledger(self):
        self.write_call(None, stream=TOOL_FREE_STREAM, turn=rows('t2'), descendants_stopped=False)
        self.assertNotIn('native', json.loads((self.folder / 'invocation.json').read_bytes()))
        receipt = self.recover(None)
        self.assertEqual((receipt['status'], receipt['response'], receipt['error']), ('succeeded', {'ok': True}, None))
        self.assertEqual(receipt['actual_costs'], {'model_calls': 1, 'tokens': 107171, 'wall_seconds': 1.25})
        self.assertEqual(receipt['token_source'], 'ledger')
        self.assertTrue(receipt['protocol_valid']); self.assertTrue(receipt['usage_complete'])
        self.assertFalse(NATIVE_KEYS & set(receipt))
        # The turn was read, as on every call, and Codex recorded no account snapshot in it.
        self.assertIsNone(receipt['account_snapshot'])
        # Everything off quiesces as today: what ran under Codex is not asked about.
        self.assertTrue(receipt['native_writer_quiesced']); self.assertFalse(receipt['recovery_requires_fencing'])

    def test_a_terminal_call_whose_profile_was_not_in_force_is_recovered_invalid(self):
        self.write_call(TERMINAL_AND_SEARCH, turn=self.this_turn(profile=False))
        receipt = self.recover(TERMINAL_AND_SEARCH)
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], 'Permission profile was not in force')
        self.assertEqual(receipt['actual_costs']['tokens'], 107171)

    def test_a_native_writer_is_quiet_only_when_what_ran_under_it_was_stopped(self):
        for options in ({'descendants_stopped': False}, {'saved': False}):
            with self.subTest(**options):
                self.write_call(TERMINAL_AND_SEARCH, **options)
                receipt = self.recover(TERMINAL_AND_SEARCH)
                self.assertEqual(receipt['status'], 'succeeded')
                self.assertFalse(receipt['native_writer_quiesced'])
                self.assertTrue(receipt['recovery_requires_fencing'])

    def test_a_cleanly_failed_native_turn_without_a_message_keeps_the_protocol(self):
        self.write_call(TERMINAL_AND_SEARCH, stream=FAILED_STREAM)
        receipt = self.recover(TERMINAL_AND_SEARCH)
        self.assertEqual(receipt['status'], 'failed_definite')
        self.assertTrue(receipt['protocol_valid'], receipt['error'])
        self.assertIn('Expected a nonempty final assistant message', receipt['error'])
        self.assertIsNone(receipt['response'])

    def test_a_resumed_call_without_usage_rows_is_not_charged_the_stream(self):
        self.write_call(TERMINAL_AND_SEARCH,
                        turn=turn_context('t2', PROFILE(self.root, self.home, self.workspace), cwd=self.workspace))
        receipt = self.recover(TERMINAL_AND_SEARCH)
        self.assertNotIn('tokens', receipt['actual_costs'])
        self.assertEqual(receipt['token_source'], 'unknown'); self.assertTrue(receipt['token_reason'])
        self.assertFalse(receipt['usage_complete'])
        self.assertEqual(receipt['stream_usage'], {'input_tokens': 20, 'output_tokens': 5})


class RecoveryAgreesWithTheExecutorTests(unittest.TestCase):
    """One Lab call runs through the real Controller, executor and transport, a local process standing in for Codex;
    recovered from the folder and rollout it left, it gets the receipt the executor gave it."""

    def run_and_recover(self, settings, stream, turn):
        """Run the first Lab turn of a run whose Lab has ``settings``. Codex is a local process that writes
        ``stream``; before it starts, the thread's rollout is the base fixture and ``turn(run_root, codex_home,
        workspace)``. Returns the executor's receipt and the one recovered from the call's folder."""
        config = {'native_capabilities': {'lab': config_form(settings)}} if settings else {}
        store, controller, runtime = open_run(self, config=config)
        codex_home = Path(tempfile.mkdtemp(prefix='rsi-native-home-'))
        self.addCleanup(shutil.rmtree, codex_home, ignore_errors=True)
        effect = runtime.prepare_meta(runtime.enqueue_lab('lab-1')['id'], RESERVATION)
        self.assertEqual(effect['status'], 'prepared', effect)
        wire = ''.join(json.dumps(event) + '\n' for event in stream).encode()
        real_popen = subprocess.Popen

        def spawn(command, **options):
            workspace = Path(options['cwd'])
            path = codex_home / 'sessions' / 'fixture' / ('rollout-fixture-' + THREAD + '.jsonl')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fixture(workspace, thread=THREAD) + turn(store.root, codex_home, workspace))
            code = 'import sys; sys.stdin.read(); sys.stdout.buffer.write(%r); sys.stdout.buffer.flush()' % wire
            return real_popen([sys.executable, '-u', '-c', code], **options)

        receipts, packets = [], []

        def executor(dispatching, raw):
            packets.append(raw)
            receipts.append(roles.CodexExecutor(store.root / 'calls')(dispatching, raw))
            return receipts[-1]

        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), \
                patch.object(transport.subprocess, 'Popen', side_effect=spawn):
            settled = controller.dispatch_effect('r', effect['id'], executor)
        with patch('subprocess.Popen', side_effect=AssertionError('Recovery must not launch Codex')):
            return receipts[0], recover_codex_receipt(settled, packets[0], store.root / 'calls')

    def test_recovery_settles_a_call_as_the_executor_did(self):
        answer = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(RESPONSE)}}

        def confined(run_root, codex_home, workspace):
            return (turn_context('t1', PROFILE(run_root, codex_home, workspace), cwd=workspace)
                    + tool_call('c1', EXEC) + rows('t1'))

        def unconfined(run_root, codex_home, workspace):
            return turn_context('t1', cwd=workspace) + tool_call('c1', EXEC) + rows('t1')

        cases = {'native': (TERMINAL_AND_SEARCH, [*NATIVE_STREAM[:-2], answer, NATIVE_STREAM[-1]], confined),
                 'profile not in force': (TERMINAL_AND_SEARCH, [*NATIVE_STREAM[:-2], answer, NATIVE_STREAM[-1]],
                                          unconfined),
                 'everything off': (None, [*NATIVE_STREAM[:2], answer, NATIVE_STREAM[-1]],
                                    lambda run_root, codex_home, workspace: rows('t1'))}
        for name, (settings, stream, turn) in cases.items():
            with self.subTest(name):
                executed, recovered = self.run_and_recover(settings, stream, turn)
                self.assertEqual(executed['actual_costs']['tokens'], 107171)
                self.assertEqual(executed['protocol_valid'], name != 'profile not in force')
                # Every key of the executor's receipt but its journal frontier, which recovery records by path.
                shared = set(executed) - {'journal', 'never_dispatched'}
                self.assertEqual({key: recovered.get(key) for key in shared}, {key: executed[key] for key in shared})
                keys = NATIVE_KEYS | {'account_snapshot'}
                self.assertEqual(keys & set(recovered), keys & set(executed))
                self.assertIn('account_snapshot', executed)


if __name__ == '__main__':
    unittest.main()
