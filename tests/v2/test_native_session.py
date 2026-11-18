"""A native session's rollout read under its role's settings.

Synthetic rollouts built from tests/v2/native_fixtures.py; the one transport
test runs a local Python process that writes the Codex event protocol. No
model, network request or credential is used.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab import native, transport
from rsi_lab.session_context import ContextUnavailable, build_effective_context, snapshot_from_bytes
from tests.v2.native_fixtures import (PROFILE, SEARCH, SUBAGENTS, TERMINAL, TERMINAL_AND_SUBAGENTS, account,
    agent_message, collab, compacted, frame, sub_agent_started, tool_call, turn_context, usage_row)
from tests.v2.test_image_evidence import native_image
from tests.v2.test_journal_transport import frames
from tests.v2.test_session_context import THREAD, fixture

# The keys of the snapshot the reader returned before native settings existed.
TODAYS_SNAPSHOT_KEYS = {
    'contract', 'provider_thread_id', 'cwd', 'prefix_bytes', 'prefix_sha256', 'record_count',
    'messages', 'messages_sha256', 'context_records_sha256', 'context_record_count',
    'runtime_metadata_sha256', 'runtime_metadata_count', 'observable_history_bytes', 'provider_context_verified',
    'observable_text_history_bytes', 'observable_image_bytes', 'image_inputs', 'image_inputs_sha256',
    'read_authority', 'compaction_supported', 'verified_parent_prefix'}
EVERYTHING = {'web_search': True, 'terminal': {'readable_roots': []}, 'subagents': {'max_concurrent': 3}}
CHILD = '33333333-3333-3333-3333-333333333333'


def session_meta(cwd):
    """The base fixture's ``session_meta`` record, which still counts after a compaction."""
    return fixture(cwd).splitlines(keepends=True)[0]


def world_state(full, number):
    return frame('world_state', {'full': full, 'state': {'number': number}})


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name

    def read(self, raw, settings, **extra):
        return snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=self.cwd, native=settings, **extra)

    def test_tool_records_are_accepted_only_under_matching_settings(self):          # Review Focus 4
        base = fixture(self.cwd)
        cases = [(tool_call('c1', 'text(await tools.exec_command({cmd:"ls"}))'), SEARCH, True),
                 (tool_call('c1', 'x'), None, False), (tool_call('c1', 'x'), native.OFF, False),
                 (collab('c2', 'spawn_agent', '{"task_name":"/root/a"}'), SEARCH, False),
                 (collab('c2', 'spawn_agent', '{"task_name":"/root/a"}'), SUBAGENTS, True),
                 (frame('inter_agent_communication_metadata', {'trigger_turn': False}), SUBAGENTS, True),
                 (frame('inter_agent_communication_metadata', {'trigger_turn': False}), SEARCH, False),
                 (frame('event_msg', {'type': 'thread_rolled_back'}), SUBAGENTS, False),
                 (frame('response_item', {'type': 'local_shell_call'}), TERMINAL, False)]
        for tail, settings, accepted in cases:
            with self.subTest(tail=tail[:60], settings=settings):
                call = lambda: snapshot_from_bytes(base + tail, expected_thread_id=THREAD, expected_cwd=self.cwd,
                                                   native=settings)
                if accepted: call()
                else: self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable', call)

    def test_a_tool_output_that_is_not_text_is_refused(self):
        tail = frame('response_item', {'type': 'custom_tool_call_output', 'call_id': 'c1',
                                       'output': [{'type': 'input_image', 'image_url': 'data:image/png;base64,AAAA'}]})
        with self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable'):
            snapshot_from_bytes(fixture(self.cwd) + tail, expected_thread_id=THREAD, expected_cwd=self.cwd,
                                native=TERMINAL)

    def test_history_is_counted_from_the_last_compaction(self):
        head = fixture(self.cwd) + tool_call('c1', 'x' * 4000)
        tail = compacted([{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'kept'}]}]) \
            + tool_call('c2', 'y')
        plain = snapshot_from_bytes(head, expected_thread_id=THREAD, expected_cwd=self.cwd, native=TERMINAL)
        after = snapshot_from_bytes(head + tail, expected_thread_id=THREAD, expected_cwd=self.cwd,
                                    expected_prefix=plain, native=TERMINAL)
        counted = len(session_meta(self.cwd)) + len(tail)
        self.assertEqual(plain['observable_text_history_bytes'], len(head)); self.assertEqual(plain['compactions'], 0)
        self.assertEqual(after['observable_text_history_bytes'], counted); self.assertEqual(after['compactions'], 1)
        self.assertEqual(after['prefix_bytes'], len(head + tail))
        ready = build_effective_context({'x': 1}, session_id='s', owner={'role': 'lab'}, snapshot=after,
                                        max_input_bytes=counted + 64)
        self.assertEqual(ready['status'], 'ready')

    def test_the_session_meta_and_the_last_full_world_state_before_a_compaction_still_count(self):
        # A compaction replaces the conversation, not the instructions and environment Codex keeps sending.
        head = (fixture(self.cwd) + world_state(True, 1) + tool_call('c1', 'x' * 4000) + world_state(True, 2)
                + world_state(False, 3))
        tail = (compacted([{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'kept'}]}])
                + tool_call('c2', 'y') + world_state(True, 4))
        got = self.read(head + tail, TERMINAL)
        self.assertEqual(got['observable_text_history_bytes'],
                         len(session_meta(self.cwd)) + len(world_state(True, 2)) + len(tail))
        self.assertEqual(got['history_counted_from_record'], len(head.splitlines()) + 1)
        # Before any compaction the whole file counts, as before.
        self.assertEqual(self.read(head, TERMINAL)['observable_text_history_bytes'], len(head))

    def test_compaction_is_refused_when_everything_is_off(self):
        with self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable'):
            snapshot_from_bytes(fixture(self.cwd) + compacted([]), expected_thread_id=THREAD, expected_cwd=self.cwd)

    def test_a_snapshot_without_settings_is_unchanged(self):
        raw = fixture(self.cwd)
        plain = snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=self.cwd)
        self.assertEqual(plain, snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=self.cwd,
                                                    native=native.OFF))
        self.assertEqual(set(plain), TODAYS_SNAPSHOT_KEYS)
        self.assertIs(plain['compaction_supported'], False)

    def test_settings_that_are_on_add_four_keys_and_change_no_other_value(self):
        raw = (fixture(self.cwd) + frame('world_state', {'full': True, 'state': {}})
               + usage_row(THREAD, 't1', 't1', 7, 1))
        plain = self.read(raw, None)
        for settings in (SEARCH, TERMINAL, SUBAGENTS, EVERYTHING):
            with self.subTest(settings=settings):
                self.assertEqual(self.read(raw, settings),
                                 {**plain, 'native': settings, 'compactions': 0, 'compaction_supported': True,
                                  'history_counted_from_record': None})

    def test_rollout_kinds_follow_the_settings(self):
        tools = {'custom_tool_call', 'custom_tool_call_output'}
        collaboration = {'function_call', 'function_call_output', 'agent_message'}
        talk = {'compacted', 'inter_agent_communication_metadata'}
        for settings, response, outer in ((None, set(), set()), (native.OFF, set(), set()),
                                          (SEARCH, tools, {'compacted'}), (TERMINAL, tools, {'compacted'}),
                                          (SUBAGENTS, collaboration, talk),
                                          (TERMINAL_AND_SUBAGENTS, tools | collaboration, talk)):
            with self.subTest(settings=settings):
                kinds = native.rollout_kinds(settings)
                self.assertEqual(kinds, {'response': response, 'outer': outer})
                self.assertIsInstance(kinds['response'], frozenset); self.assertIsInstance(kinds['outer'], frozenset)

    def test_a_session_with_every_record_the_probe_saw_is_read(self):
        kept = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'kept'}]}
        raw = (fixture(self.cwd) + turn_context('t1', PROFILE('/r', '/h', self.cwd))
               + sub_agent_started(CHILD, '/root/a') + collab('c1', 'spawn_agent', '{"agent_id":"' + CHILD + '"}')
               + frame('inter_agent_communication_metadata', {'trigger_turn': False})
               + agent_message('/root/a', '/root', 'Found it.') + collab('c2', 'wait_agent', 'completed')
               + tool_call('c3', 'text(await tools.web_search({query:"q"}))')
               + compacted([kept]) + account('1200.50', 12.5) + usage_row(THREAD, 't1', 't1', 83032, 291))
        got = self.read(raw, EVERYTHING)
        self.assertEqual((got['native'], got['compactions'], got['compaction_supported']), (EVERYTHING, 1, True))
        self.assertEqual(len(got['messages']), 2)
        self.assertEqual(got['observable_history_bytes'], len(raw))

    def test_tool_outputs_are_accepted_only_as_text(self):
        cases = [('function_call_output', {'output': 'spawned'}, True),
                 ('custom_tool_call_output', {'output': [{'type': 'input_text', 'text': 'a'},
                                                         {'type': 'input_text', 'text': ''}]}, True),
                 ('custom_tool_call_output', {'output': []}, True),
                 ('function_call_output', {}, False),
                 ('function_call_output', {'output': None}, False),
                 ('custom_tool_call_output', {'output': {'type': 'input_text', 'text': 'a'}}, False),
                 ('custom_tool_call_output', {'output': ['a']}, False),
                 ('custom_tool_call_output', {'output': [{'type': 'input_text'}]}, False),
                 ('custom_tool_call_output', {'output': [{'type': 'input_text', 'text': 1}]}, False),
                 ('function_call_output', {'output': [{'type': 'input_image', 'text': '',
                                                       'image_url': 'https://example.invalid/a.png'}]}, False)]
        for kind, fields, accepted in cases:
            tail = frame('response_item', {'type': kind, 'call_id': 'c1', **fields})
            with self.subTest(kind=kind, fields=fields):
                call = lambda: self.read(fixture(self.cwd) + tail, EVERYTHING)
                if accepted: call()
                else: self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable', call)

    def test_a_message_between_agents_may_not_carry_an_image(self):
        # Images reach a model only as Controller attachments; a sub-agent's message has text and opaque parts.
        text = {'type': 'input_text', 'text': 'Found it.'}
        opaque = {'type': 'encrypted_content', 'encrypted_content': 'opaque-sub-agent-payload'}
        refused = {'an input_image item': [text, native_image()], 'an image type alone': [{'type': 'input_image'}],
                   'an image_url on a text item': [{**text, 'image_url': 'https://example.invalid/a.png'}],
                   'another image type': [{'type': 'output_image', 'data': 'AAAA'}],
                   'an item that is not an object': [text, 'Found it.'], 'no content': None,
                   'an object as content': text}
        accepted = {'text and an opaque part': [text, opaque], 'an opaque part alone': [opaque],
                    'text alone': [text], 'an item of another kind': [text, {'type': 'summary_text', 'text': 's'}],
                    'a string': 'Found it.'}
        for cases, kept in ((refused, False), (accepted, True)):
            for case, content in cases.items():
                tail = frame('response_item', {'type': 'agent_message', 'author': '/root/a', 'recipient': '/root',
                                               'content': content})
                with self.subTest(case=case):
                    call = lambda: self.read(fixture(self.cwd) + tail, SUBAGENTS)
                    if kept: call()
                    else: self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable', call)
        # The fixture writes the shape the probe saw, which is accepted.
        self.read(fixture(self.cwd) + agent_message('/root/a', '/root', 'Found it.'), SUBAGENTS)
        self.assertEqual(json.loads(agent_message('/root', '/root/a', 'Go.'))['payload']['content'][1]['type'],
                         'encrypted_content')

    def test_rollback_and_other_compaction_records_stay_refused_with_everything_on(self):
        for tail in (frame('event_msg', {'type': 'thread_rolled_back'}), frame('thread_rollback', {}),
                     frame('compaction', {}), frame('context_compacted', {}),
                     frame('response_item', {'type': 'compaction', 'encrypted_content': 'opaque'}),
                     frame('inter_agent_communication_metadata', {'type': 'thread_rolled_back'}),
                     frame('compacted', {'type': 'compaction', 'replacement_history': []}),
                     frame('response_item', {'type': 'local_shell_call'}),
                     frame('response_item', {'type': 'mcp_tool_call'}), frame('future_runtime_record', {})):
            with self.subTest(tail=tail), self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable'):
                self.read(fixture(self.cwd) + tail, EVERYTHING)

    def test_a_compaction_without_its_replacement_history_counts_the_whole_file(self):
        # Counting from a compaction is sound only when the record holds what replaced the history.
        for payload in ({'message': 'Only a summary remains'}, {'message': '', 'replacement_history': None}):
            with self.subTest(payload=payload):
                raw = fixture(self.cwd) + tool_call('c1', 'x') + frame('compacted', payload) + tool_call('c2', 'y')
                got = self.read(raw, TERMINAL)
                self.assertEqual(got['observable_text_history_bytes'], len(raw))
                self.assertEqual(got['compactions'], 1)
                self.assertIsNone(got['history_counted_from_record'])

    def test_history_counts_from_the_last_compaction_that_carries_its_history(self):
        head = fixture(self.cwd) + tool_call('c1', 'x' * 4000)
        tail = (compacted([{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'kept'}]}])
                + tool_call('c2', 'y') + frame('compacted', {'message': 'Only a summary remains'})
                + tool_call('c3', 'z'))
        got = self.read(head + tail, TERMINAL)
        self.assertEqual(got['observable_text_history_bytes'], len(session_meta(self.cwd)) + len(tail))
        self.assertEqual(got['compactions'], 2)
        self.assertEqual(got['history_counted_from_record'], len(head.splitlines()) + 1)

    def test_the_manifest_names_where_history_is_counted_from(self):
        today = 'complete-rollout-bytes-conservative-estimate-v1'
        since = 'rollout-bytes-since-last-compaction-estimate-v1'
        base = fixture(self.cwd) + tool_call('c1', 'x')
        arguments = {'session_id': 's', 'owner': {'role': 'lab'}}
        for snapshot, method in ((None, today), (snapshot_from_bytes(fixture(self.cwd), expected_thread_id=THREAD,
                                                                     expected_cwd=self.cwd), today),
                                 (self.read(base, TERMINAL), today),
                                 (self.read(base + frame('compacted', {'message': 'Only a summary remains'}), TERMINAL),
                                  today),
                                 (self.read(base + compacted([]) + tool_call('c2', 'y'), TERMINAL), since)):
            with self.subTest(snapshot=None if snapshot is None else snapshot.get('history_counted_from_record')):
                manifest = build_effective_context({'x': 1}, snapshot=snapshot, **arguments)['manifest']
                self.assertEqual(manifest['history_token_method'], method)

    def test_images_after_the_last_compaction_stay_outside_its_text_count(self):
        image = native_image()
        encoded = len(image['image_url'].split(',', 1)[1])
        message = frame('response_item', {'type': 'message', 'role': 'user',
                                          'content': [{'type': 'input_text', 'text': 'Inspect this page'}, image]})
        head = fixture(self.cwd) + message
        tail = compacted([]) + message
        got = self.read(head + tail, TERMINAL)
        self.assertEqual(got['observable_text_history_bytes'], len(session_meta(self.cwd)) + len(tail) - encoded)
        self.assertEqual(len(got['image_inputs']), 2)
        self.assertEqual(got['observable_image_bytes'], 2 * got['image_inputs'][0]['bytes'])

    def test_compaction_support_needs_settings_that_are_on(self):
        on = self.read(fixture(self.cwd), TERMINAL)
        arguments = {'session_id': 's', 'owner': {'role': 'lab'}}
        self.assertEqual(build_effective_context({'x': 1}, snapshot=on, **arguments)['status'], 'ready')
        without = {key: value for key, value in on.items() if key != 'native'}
        for snapshot in (without, {**on, 'native': native.OFF}, {**on, 'native': None},
                         {**on, 'native': {**TERMINAL, 'web_search': 'yes'}}, {**on, 'compaction_supported': 1}):
            with self.subTest(snapshot={key: snapshot.get(key) for key in ('native', 'compaction_supported')}), \
                    self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable'):
                build_effective_context({'x': 1}, snapshot=snapshot, **arguments)

    def test_invoke_reads_its_rollout_under_the_call_settings(self):
        root = Path(self.cwd)
        workspace, home = root / 'workspace', root / 'codex-home'
        rollout = home / 'sessions' / '2000' / '01' / '01' / f'rollout-2000-01-01-{THREAD}.jsonl'
        rollout.parent.mkdir(parents=True); workspace.mkdir()
        rollout.write_bytes(fixture(workspace) + tool_call('c1', 'text(await tools.exec_command({cmd:"ls"}))'))
        wire = ''.join(json.dumps(event) + '\n' for event in frames(thread=THREAD)).encode()
        code = 'import sys; sys.stdin.read(); sys.stdout.buffer.write(' + repr(wire) + '); sys.stdout.buffer.flush()'
        real_popen = subprocess.Popen
        session = {'session_id': 's', 'workspace': str(workspace), 'provider_thread_id': None, 'codex_home': str(home)}
        results = {}
        for name, settings in (('on', TERMINAL), ('off', None)):
            # A local process that writes the event stream stands in for Codex.
            with patch.object(transport.subprocess, 'Popen', side_effect=lambda command, **kwargs:
                              real_popen([sys.executable, '-u', '-c', code], **kwargs)):
                results[name] = transport.invoke('Inspect this task', {'type': 'object'}, root / 'calls' / name,
                                                 'm', 'low', timeout=10, session=session, native=settings,
                                                 run_root=str(root))
        self.assertEqual(results['on']['execution_status'], 'succeeded')
        self.assertIsNone(results['on']['native_session_error'])
        self.assertEqual(results['on']['native_session_ref'], str(rollout.resolve()))
        self.assertIsNone(results['off']['native_session_ref'])
        self.assertIn('context_unverifiable', results['off']['native_session_error'])


if __name__ == '__main__':
    unittest.main()
