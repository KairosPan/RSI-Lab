"""Durability and native-session transport contracts, with local process fixtures.

The fake provider is a Python process that writes the Codex event protocol; no
model, network request, credential, or scientific workload is used.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rsi_lab import transport
from rsi_lab.journal import (JournalIntegrityError, JournalWriter, read_journal,
                             raw_stream, unapplied_records)

THREAD = 'f38a21a2-1f39-43cf-8027-6ae633f1f445'
OTHER_THREAD = 'c8b9dd72-e405-4c83-bafc-e460d890847a'


def frames(output='{"ok":true}', thread=THREAD):
    return [{'type': 'thread.started', 'thread_id': thread},
            {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': output}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 5}}]


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'journal.jsonl'

    def test_exact_raw_bytes_and_distinct_received_events(self):
        with JournalWriter(self.path, {'effect_id': 'e'}) as writer:
            writer.append('stdout', b'\xffpartial', kind='stream_chunk')
            writer.append('stdout', b'{"x":1}\n', kind='event_frame')
            writer.append('stdout', b'{"x":1}\n', kind='event_frame')
            frontier = writer.snapshot()
            # Data is readable before close and before a turn could complete.
            snap = read_journal(self.path)
            self.assertEqual(snap['durable_seq'], frontier['durable_seq'])
            self.assertEqual(snap['head_hash'], frontier['head_hash'])
            self.assertEqual(raw_stream(snap, 'stdout'), b'\xffpartial')
            self.assertEqual(len(unapplied_records(snap, 1)), 3)
            self.assertEqual([r['duplicate_of'] for r in snap['records']], [None] * 4)

    def test_provider_repeated_identity_is_distinct_from_repeated_text(self):
        with JournalWriter(self.path) as writer:
            first = writer.append('stdout', b'first', kind='event_frame', provider_event_id='one')
            duplicate = writer.append('stdout', b'first', kind='event_frame', provider_event_id='one')
            self.assertEqual(duplicate['duplicate_of'], first['seq'])
            conflict = writer.append('stdout', b'different', kind='event_frame', provider_event_id='one')
            self.assertEqual(conflict['kind'], 'provider_event_conflict')
        self.assertEqual(read_journal(self.path)['gaps'][0]['kind'], 'provider_event_conflict')

    def test_torn_tail_is_not_acknowledged_and_corruption_is_not_skipped(self):
        with JournalWriter(self.path) as writer:
            writer.append('stdout', b'ok')
        original = self.path.read_bytes()
        with self.path.open('ab') as stream:
            stream.write(b'{"seq":3')
        snap = read_journal(self.path)
        self.assertEqual(snap['durable_seq'], 2)
        self.assertEqual(snap['gaps'][0]['kind'], 'incomplete_tail')
        self.path.write_bytes(original.replace(b'"stream":"stdout"', b'"stream":"stderr"'))
        with self.assertRaises(JournalIntegrityError):
            read_journal(self.path)

    def test_failed_fsync_does_not_advance_acknowledged_frontier(self):
        with JournalWriter(self.path) as writer:
            before = writer.snapshot()
            with patch('rsi_lab.journal.os.fsync', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    writer.append('stdout', b'event')
            self.assertEqual(writer.durable_seq, before['durable_seq'])
            self.assertTrue(writer.failed)
            recovered = read_journal(self.path)
            self.assertEqual(recovered['durable_seq'], before['durable_seq'])
            self.assertEqual(recovered['gaps'][0]['kind'], 'unconfirmed_tail')
            with self.assertRaises(OSError):
                writer.append('stdout', b'must not continue')

    def test_existing_journal_is_never_overwritten(self):
        with JournalWriter(self.path):
            pass
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            JournalWriter(self.path)
        self.assertEqual(before, self.path.read_bytes())


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'call'
        self.real_popen = subprocess.Popen
        self.commands = []

    def invoke(self, events=None, *, code=None, session=None, timeout=3):
        if code is None:
            wire = ''.join(json.dumps(e) + '\n' for e in (events or frames()))
            code = 'import sys; sys.stdin.read(); sys.stdout.write(' + repr(wire) + '); sys.stdout.flush()'
        def fake(command, **kwargs):
            self.commands.append(command)
            return self.real_popen([sys.executable, '-u', '-c', code], **kwargs)
        with patch.object(transport.subprocess, 'Popen', side_effect=fake):
            return transport.invoke('Inspect this task', {'type': 'object'}, self.folder,
                                    'test-model', 'medium', timeout=timeout, session=session,
                                    journal_metadata={'effect_id': self.folder.name})

    def session(self, native=None):
        return {'session_id': 'owned-session', 'workspace': str(self.root / 'workspace'),
                'provider_thread_id': native, 'codex_home': str(self.root / 'codex-home')}

    def test_create_and_resume_use_owned_identity_without_ephemeral_or_last(self):
        first = self.invoke(session=self.session())
        self.assertEqual(first['execution_status'], 'succeeded')
        self.assertEqual(first['provider_thread_id'], THREAD)
        self.assertNotIn('--ephemeral', self.commands[0])
        self.assertNotIn('resume', self.commands[0])
        self.assertTrue((self.root / 'workspace').is_dir())
        self.folder = self.root / 'second'
        second = self.invoke(session=self.session(THREAD))
        command = self.commands[1]
        self.assertIn('resume', command)
        self.assertEqual(command[-2:], [THREAD, '-'])
        self.assertNotIn('--last', command)
        self.assertNotIn('--ephemeral', command)
        self.assertEqual(second['provider_thread_id'], THREAD)
        self.assertIsNotNone(second['native_session_error'])

    def test_one_shot_compatibility_is_explicitly_ephemeral(self):
        result = self.invoke()
        self.assertEqual(result['response'], {'ok': True})
        self.assertTrue(result['protocol_valid'])
        self.assertIn('--ephemeral', self.commands[0])
        self.assertEqual(json.loads((self.folder / 'transport_result.json').read_text()), result)
        self.assertEqual(transport.audited_events(self.folder)[1], result['usage'])

    def test_known_completion_with_invalid_json_preserves_usage_and_journal(self):
        result = self.invoke(frames('not valid JSON'))
        self.assertEqual(result['execution_status'], 'succeeded')
        self.assertTrue(result['usage_complete'])
        self.assertTrue(result['terminal_received'])
        self.assertTrue(result['protocol_valid'])
        self.assertIsNone(result['error'])
        self.assertIsNone(result['response'])
        self.assertIn('JSONDecodeError', result['parse_error'])
        self.assertEqual((self.folder / 'raw_response.txt').read_text(), 'not valid JSON')

    def test_duplicate_keys_nonfinite_values_and_nonobjects_reject_response(self):
        for index, text in enumerate(('{"ok":true,"ok":false}', '{"value":NaN}', '[]')):
            with self.subTest(text=text):
                self.folder = self.root / f'invalid-response-{index}'
                result = self.invoke(frames(text))
                self.assertEqual(result['execution_status'], 'succeeded')
                self.assertTrue(result['terminal_received'])
                self.assertTrue(result['usage_complete'])
                self.assertIsNone(result['response'])
                self.assertIsNotNone(result['parse_error'])
                self.assertEqual((self.folder / 'raw_response.txt').read_text(), text)

    def test_missing_usage_remains_unknown_after_completed_turn(self):
        events = frames()
        events[-1].pop('usage')
        result = self.invoke(events)
        self.assertTrue(result['terminal_received'])
        self.assertIsNone(result['usage'])
        self.assertFalse(result['usage_complete'])

    def test_nonzero_exit_preserves_observed_usage_without_claiming_completion(self):
        wire = ''.join(json.dumps(event) + '\n' for event in frames())
        code = ('import sys;sys.stdin.read();sys.stdout.write(' + repr(wire) + ');'
                'sys.stdout.flush();sys.stderr.write("transport failed");sys.exit(1)')
        result = self.invoke(code=code)
        self.assertIsNone(result['response'])
        self.assertEqual(result['usage'], {'input_tokens': 20, 'output_tokens': 5})
        self.assertFalse(result['usage_complete'])
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertEqual((self.folder / 'stderr.log').read_text(), 'transport failed')
        self.assertEqual(len(self.commands), 1)

    def test_missing_cli_records_definite_undispatched_failure(self):
        with patch.object(transport.subprocess, 'Popen', side_effect=FileNotFoundError('provider unavailable')) as spawn:
            result = transport.invoke('Inspect this task', {'type': 'object'}, self.folder,
                                      'test-model', 'medium')
        spawn.assert_called_once()
        self.assertEqual(result['execution_status'], 'failed_definite')
        self.assertFalse(result['dispatched'])
        self.assertIsNone(result['returncode'])
        self.assertIsNone(result['usage'])
        self.assertFalse(result['usage_complete'])
        self.assertIn('FileNotFoundError', result['error'])
        self.assertFalse(json.loads((self.folder / 'invocation.json').read_text())['dispatched'])
        self.assertTrue((self.folder / 'events.jsonl').is_file())
        self.assertTrue((self.folder / 'journal.jsonl').is_file())

    def test_invalid_parameters_do_not_spawn_or_create_attempt(self):
        with patch.object(transport.subprocess, 'Popen') as spawn:
            for timeout in (True, 0, float('nan'), float('inf')):
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    transport.invoke('Inspect this task', {}, self.folder,
                                     'test-model', 'medium', timeout=timeout)
            with self.assertRaises(ValueError):
                transport.invoke('Inspect this task', {}, self.folder, 'test-model', 'invalid-effort')
        spawn.assert_not_called()
        self.assertFalse(self.folder.exists())

    def test_certified_failed_turn_is_known_and_protocol_valid(self):
        events = [{'type': 'thread.started', 'thread_id': THREAD},
                  {'type': 'turn.started'},
                  {'type': 'turn.failed', 'error': {'message': 'Synthetic provider failure'}}]
        result = self.invoke(events, session=self.session())
        self.assertEqual(result['execution_status'], 'failed_definite')
        self.assertTrue(result['terminal_received'])
        self.assertTrue(result['protocol_valid'])
        self.assertIsNotNone(result['error'])
        self.assertIsNone(result['response'])
        self.folder = self.root / 'failed-exit'
        wire = ''.join(json.dumps(e) + '\n' for e in events)
        code = 'import sys;sys.stdin.read();sys.stdout.write(' + repr(wire) + ');sys.stdout.flush();sys.exit(1)'
        exited = self.invoke(code=code, session=self.session())
        self.assertEqual(exited['execution_status'], 'failed_definite')
        self.assertTrue(exited['protocol_valid'])
        self.assertEqual(exited['returncode'], 1)
        self.folder = self.root / 'bad-failure'
        bad = [*events[:2], {'type': 'item.completed', 'item': {'type': 'command_execution'}}, events[-1]]
        rejected = self.invoke(bad, session=self.session())
        self.assertEqual(rejected['execution_status'], 'failed_definite')
        self.assertFalse(rejected['protocol_valid'])
        self.assertIn('Forbidden tool', rejected['error'])

    def test_wrong_native_thread_is_a_protocol_failure_not_a_new_binding(self):
        result = self.invoke(frames(thread=OTHER_THREAD), session=self.session(THREAD))
        self.assertIn('different thread', result['error'])
        self.assertFalse(result['protocol_valid'])
        self.assertEqual(result['provider_thread_id'], THREAD)
        self.assertIsNone(result['response'])

    def test_unsafe_resume_identity_is_rejected_before_spawn(self):
        with self.assertRaisesRegex(ValueError, 'UUID'):
            self.invoke(session=self.session('--last'))
        self.assertFalse(self.commands)

    def test_duplicate_provider_frame_is_not_double_counted(self):
        events = frames()
        events[-1]['event_id'] = 'terminal-1'
        events.append(dict(events[-1]))
        result = self.invoke(events)
        self.assertTrue(result['protocol_valid'], result)
        self.assertEqual(result['usage']['output_tokens'], 5)
        snap = read_journal(self.folder / 'journal.jsonl')
        self.assertEqual(sum(r.get('duplicate_of') is not None for r in snap['records']), 1)
        self.assertEqual((self.folder / 'events.jsonl').read_text().count('terminal-1'), 2)

    def test_events_are_durable_before_process_finishes(self):
        first = json.dumps(frames()[0]) + '\n'
        rest = ''.join(json.dumps(e) + '\n' for e in frames()[1:])
        code = ('import sys,time;sys.stdin.read();sys.stdout.write(' + repr(first) + ');sys.stdout.flush();'
                'time.sleep(.7);sys.stdout.write(' + repr(rest) + ');sys.stdout.flush()')
        result = []
        t = threading.Thread(target=lambda: result.append(self.invoke(code=code)))
        t.start()
        deadline = time.monotonic() + 2
        captured = None
        while time.monotonic() < deadline:
            path = self.folder / 'journal.jsonl'
            if path.exists():
                snap = read_journal(path)
                if any(r['kind'] == 'event_frame' for r in snap['records']):
                    captured = snap
                    break
            time.sleep(.01)
        self.assertIsNotNone(captured)
        self.assertTrue(t.is_alive(), 'Turn must still be running after a durable event')
        self.assertIn(b'thread.started', raw_stream(captured, 'stdout'))
        t.join(timeout=4)
        self.assertFalse(t.is_alive())
        self.assertEqual(result[0]['execution_status'], 'succeeded')

    def test_timeout_preserves_stdout_stderr_and_never_retries(self):
        wire = json.dumps(frames()[0]) + '\n'
        code = ('import sys,time;sys.stdin.read();sys.stdout.write(' + repr(wire) + ');sys.stdout.flush();'
                'sys.stderr.write("still running");sys.stderr.flush();time.sleep(10)')
        result = self.invoke(code=code, timeout=.15)
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertIn('timed out', result['error'])
        self.assertFalse(result['stream_complete'])
        self.assertEqual(len(self.commands), 1)
        snap = read_journal(self.folder / 'journal.jsonl')
        self.assertIn(b'thread.started', raw_stream(snap, 'stdout'))
        self.assertEqual(raw_stream(snap, 'stderr'), b'still running')
        self.assertTrue(snap['gaps'])

    def test_unterminated_frame_and_invalid_event_remain_in_journal(self):
        code = 'import sys;sys.stdin.read();sys.stdout.write("{invalid}\\npartial");sys.stdout.flush()'
        result = self.invoke(code=code)
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertFalse(result['stream_complete'])
        snap = read_journal(self.folder / 'journal.jsonl')
        self.assertEqual(raw_stream(snap, 'stdout'), b'{invalid}\npartial')
        self.assertIn('invalid_event_frame', [r['kind'] for r in snap['records']])
        self.assertTrue(snap['gaps'])

    def test_journal_failure_terminates_child_and_keeps_unknown_responsibility(self):
        original = JournalWriter.append
        def fail_stdout(writer, stream, raw, **kwargs):
            if stream == 'stdout':
                writer.failed = True
                raise OSError('journal storage exhausted')
            return original(writer, stream, raw, **kwargs)
        wire = json.dumps(frames()[0]) + '\n'
        code = ('import sys,time;sys.stdin.read();sys.stdout.write(' + repr(wire) + ');sys.stdout.flush();time.sleep(10)')
        with patch.object(JournalWriter, 'append', fail_stdout):
            result = self.invoke(code=code)
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertTrue(result['journal']['write_failed'])
        self.assertIn('journal storage exhausted', result['error'])
        self.assertEqual(len(self.commands), 1)

    def test_artifacts_cannot_be_overwritten(self):
        self.invoke()
        before = (self.folder / 'journal.jsonl').read_bytes()
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.assertEqual(before, (self.folder / 'journal.jsonl').read_bytes())


if __name__ == '__main__':
    unittest.main()
