"""The Codex command and transport call built from one role's native settings.

The fake provider is a local Python process that writes the Codex event
protocol; no model, network request or credential is used.
"""
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from rsi_lab import native, transport
from rsi_lab.journal import read_journal
from tests.v2.native_fixtures import SEARCH, SUBAGENTS, TERMINAL, TERMINAL_AND_SEARCH, TERMINAL_AND_SUBAGENTS
from tests.v2.test_journal_transport import THREAD, frames

TODAY = ['codex', '--no-daemon', '--ask-for-approval', 'never', 'exec', '--sandbox', 'read-only', '-C', '/w',
         '--ignore-user-config', '--model', 'm', '--skip-git-repo-check', '--json', '--output-schema', '/f/schema.json',
         '-c', 'model_reasoning_effort="low"', '-c', 'web_search="disabled"',
         '-c', 'suppress_unstable_features_warning=true']
# ... followed by '--disable', X for every X in transport.DISABLED_FEATURES, then
# '--enable', 'skip_host_skill_discovery', the thread id when resuming, and '-'.

NATIVE_STREAM = [
    {'type': 'thread.started', 'thread_id': THREAD}, {'type': 'turn.started'},
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Starting.'}},
    {'type': 'item.started', 'item': {'type': 'command_execution', 'command': "/bin/zsh -lc 'cat in.txt'", 'status': 'in_progress'}},
    {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': "/bin/zsh -lc 'cat in.txt'",
                                        'aggregated_output': 'x\n', 'exit_code': 0, 'status': 'completed'}},
    {'type': 'item.completed', 'item': {'type': 'file_change', 'status': 'completed',
                                        'changes': [{'path': '/w/notes.md', 'kind': 'add'}]}},
    {'type': 'item.completed', 'item': {'type': 'web_search', 'query': 'q', 'action': {'type': 'search', 'query': 'q'}}},
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"ok":true}'}},
    {'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 5}}]
NATIVE_ACTIVITY = {
    'items': {'agent_message': 2, 'command_execution': 1, 'file_change': 1, 'web_search': 1},
    'commands': [{'command': "/bin/zsh -lc 'cat in.txt'", 'exit_code': 0, 'status': 'completed'}],
    'searches': ['q'], 'files_changed': ['/w/notes.md'], 'omitted': 0}


class NativeTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'calls' / 'effect'
        self.workspace = self.root / 'agents' / 's' / 'workspace'
        self.real_popen = subprocess.Popen
        self.commands = []

    def command(self, settings, thread=None):
        session = {'provider_thread_id': thread, 'codex_home': '/home/.codex'}
        return transport._command(Path('/f'), 'm', 'low', '/r/agents/s/workspace', session,
                                  native=settings, run_root='/r')

    def config_value(self, command, prefix):
        """The one ``-c`` value that starts with ``prefix``, without the prefix."""
        values = [command[i + 1][len(prefix):] for i, part in enumerate(command[:-1])
                  if part == '-c' and command[i + 1].startswith(prefix)]
        self.assertEqual(len(values), 1, prefix)
        return values[0]

    def session(self):
        return {'session_id': 's', 'workspace': str(self.workspace), 'provider_thread_id': None,
                'codex_home': str(self.root / 'codex-home')}

    def wire(self, events, end='\n'):
        """Event lines as Codex writes them: non-ASCII text is not escaped."""
        return ''.join(json.dumps(event, ensure_ascii=False) + end for event in events)

    def invoke(self, events=None, *, code=None, timeout=10, session=None, native=None, run_root=None, folder=None,
               end='\n'):
        """Run ``transport.invoke`` against a local process that runs ``code``, by default writing ``events``
        (default ``frames()``), with ``session`` (default ``self.session()``)."""
        if code is None:
            wire = self.wire(frames() if events is None else events, end).encode()
            code = 'import sys; sys.stdin.read(); sys.stdout.buffer.write(' + repr(wire) + '); sys.stdout.buffer.flush()'

        def fake(command, **kwargs):
            self.commands.append(command)
            return self.real_popen([sys.executable, '-u', '-c', code], **kwargs)
        with patch.object(transport.subprocess, 'Popen', side_effect=fake):
            return transport.invoke('Inspect this task', {'type': 'object'}, folder or self.folder, 'm', 'low',
                                    timeout=timeout, session=session or self.session(),
                                    journal_metadata={'effect_id': 'effect'},
                                    native=native, run_root=run_root or str(self.root))

    def kill_if_alive(self, marker):
        """Kill the process whose pid ``marker`` holds, if it runs, so a failing test leaves nothing behind."""
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def start_record(self, folder):
        first = read_journal(folder / 'journal.jsonl')['records'][0]
        return json.loads(base64.b64decode(first['raw_base64']))

    def test_fixtures_are_normal_form_and_switch_something_on(self):
        for settings in (SEARCH, TERMINAL, SUBAGENTS, TERMINAL_AND_SEARCH, TERMINAL_AND_SUBAGENTS):
            self.assertEqual(native.validate(settings), settings)
            self.assertTrue(native.any_on(settings))
        self.assertEqual(SUBAGENTS['subagents']['max_concurrent'], 3)

    def test_everything_off_is_todays_command(self):
        session = {'provider_thread_id': None}
        expected = TODAY + [part for f in transport.DISABLED_FEATURES for part in ('--disable', f)] + [
            '--enable', 'skip_host_skill_discovery', '-']
        for settings in (None, native.OFF):
            self.assertEqual(transport._command(Path('/f'), 'm', 'low', '/w', session, native=settings, run_root='/r'), expected)

    def test_search_only_keeps_the_sandbox_and_the_disabled_shell(self):
        command = self.command({'web_search': True, 'terminal': None, 'subagents': None})
        self.assertIn('web_search="live"', command)
        self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
        for feature in transport.DISABLED_FEATURES:
            self.assertEqual(command[command.index(feature) - 1], '--disable')

    def test_terminal_replaces_the_sandbox_with_the_profile(self):
        with patch.object(native, 'SHARED_TEMP_FOLDERS', ()):
            command = self.command({'web_search': False, 'terminal': {'readable_roots': ['/data']}, 'subagents': None})
        self.assertNotIn('--sandbox', command)
        self.assertNotIn('shell_tool', command); self.assertNotIn('unified_exec', command)
        self.assertIn('default_permissions="rsi_agent"', command)
        table = tomllib.loads('t = ' + self.config_value(command, 'permissions.rsi_agent.filesystem='))['t']
        self.assertEqual(table, {':minimal': 'read', '/r': 'deny', '/home/.codex': 'deny', '/r/agents/s/workspace': 'write',
                                 '/r/agents/s/workspace/.codex': 'read', '/r/agents/s/workspace/.agents': 'read',
                                 '/r/agents/s/workspace/AGENTS.md': 'read',
                                 '/r/agents/s/workspace/AGENTS.override.md': 'read', '/data': 'read'})
        self.assertEqual(list(table), [':minimal', '/r', '/home/.codex', '/r/agents/s/workspace',
                                       '/r/agents/s/workspace/.codex', '/r/agents/s/workspace/.agents',
                                       '/r/agents/s/workspace/AGENTS.md', '/r/agents/s/workspace/AGENTS.override.md',
                                       '/data'])
        env = tomllib.loads('t = ' + self.config_value(command, 'shell_environment_policy.set='))['t']
        self.assertEqual(env, {'TMPDIR': '/r/agents/s/workspace/.tmp', 'TMPPREFIX': '/r/agents/s/workspace/.tmp/zsh'})
        for feature in ('view_image', 'apps', 'plugins', 'browser_use', 'computer_use', 'multi_agent', 'multi_agent_v2'):
            self.assertEqual(command[command.index(feature) - 1], '--disable')

    def test_terminal_denies_the_shared_temp_folders_that_exist(self):
        first, second, missing = self.root / 'tmp', self.root / 'private-tmp', self.root / 'no-such-tmp'
        first.mkdir(); second.mkdir()
        with patch.object(native, 'SHARED_TEMP_FOLDERS', (str(first), str(missing), str(second))):
            command = self.command(TERMINAL)
        table = tomllib.loads('t = ' + self.config_value(command, 'permissions.rsi_agent.filesystem='))['t']
        self.assertEqual(list(table)[:6], [':minimal', '/r', '/home/.codex', str(first), str(second),
                                           '/r/agents/s/workspace'])
        self.assertEqual((table[str(first)], table[str(second)]), ('deny', 'deny'))
        self.assertNotIn(str(missing), table)

    def test_a_run_root_or_codex_home_that_is_a_shared_temp_folder_is_denied_once(self):
        shared = self.root / 'shared-tmp'
        shared.mkdir()
        key = native.toml_string(str(shared)) + '='
        for run_root, codex_home in ((str(shared), '/h'), ('/r', str(shared))):
            with self.subTest(run_root=run_root, codex_home=codex_home), \
                    patch.object(native, 'SHARED_TEMP_FOLDERS', (str(shared),)):
                parts = native.command_parts(TERMINAL, workspace=str(shared / 'agents' / 's' / 'workspace'),
                                             run_root=run_root, codex_home=codex_home, model='m', effort='low')
                value = next(v for v in parts['config'] if v.startswith('permissions.rsi_agent.filesystem='))
                table = tomllib.loads('t = ' + value.split('=', 1)[1])['t']
                self.assertEqual(table[str(shared)], 'deny')
                self.assertEqual(value.count(key), 1)

    def test_the_shared_temp_folders_are_the_four_system_ones(self):
        self.assertEqual(native.SHARED_TEMP_FOLDERS, ('/tmp', '/private/tmp', '/var/tmp', '/private/var/tmp'))

    def test_terminal_closes_the_network_the_environment_and_workspace_instructions(self):
        for thread in (None, THREAD):
            command = self.command(TERMINAL, thread)
            network = tomllib.loads('t = ' + self.config_value(command, 'permissions.rsi_agent.network='))['t']
            self.assertEqual(network, {'enabled': False})
            for value in ('shell_environment_policy.inherit="core"', 'project_doc_max_bytes=0'):
                self.assertEqual(command[command.index(value) - 1], '-c')
            self.assertEqual(command[command.index('--ignore-user-config') + 1], '--ignore-rules')
            self.assertEqual(command.count('--ignore-rules'), 1)
            self.assertEqual('resume' in command, thread is not None)

    def test_only_the_terminal_gets_the_confinement_flags(self):
        extras = ('--ignore-rules', 'project_doc_max_bytes=0', 'shell_environment_policy.inherit="core"')
        for settings in (SEARCH, SUBAGENTS, None, native.OFF):
            for thread in (None, THREAD):
                command = self.command(settings, thread)
                for extra in extras:
                    self.assertNotIn(extra, command)
                self.assertFalse(any(part.startswith('permissions.rsi_agent.network') for part in command))
                self.assertFalse(any('TMPPREFIX' in part or 'permissions.rsi_agent.filesystem' in part
                                     or any(folder in part for folder in native.SHARED_TEMP_FOLDERS)
                                     for part in command))
                self.assertEqual(command[command.index('--ignore-user-config') + 1], '--model')
        for settings, flags in ((TERMINAL, ['--ignore-rules']), (SEARCH, []), (SUBAGENTS, []), (native.OFF, [])):
            self.assertEqual(native.command_parts(settings, workspace='/w', run_root='/r', codex_home='/h',
                                                  model='m', effort='low')['flags'], flags)

    def test_subagents_set_depth_concurrency_model_and_effort(self):
        command = self.command({'web_search': False, 'terminal': None, 'subagents': {'max_concurrent': 2}})
        self.assertNotIn('multi_agent', command); self.assertIn('multi_agent_v2', command)
        for value in ('agents.max_depth=1', 'agents.max_concurrent_threads_per_session=2',
                      'agents.default_subagent_model="m"', 'agents.default_subagent_reasoning_effort="low"'):
            self.assertIn(value, command)

    def test_features_outside_the_settings_stay_disabled_in_every_combination(self):
        never = ('apps', 'plugins', 'browser_use', 'computer_use', 'image_generation', 'view_image',
                 'skill_search', 'hooks', 'goals', 'sleep_tool', 'multi_agent_v2')
        for settings in (SEARCH, TERMINAL, SUBAGENTS, TERMINAL_AND_SEARCH, TERMINAL_AND_SUBAGENTS):
            command = self.command(settings)
            disabled = {command[i + 1] for i, part in enumerate(command[:-1]) if part == '--disable'}
            self.assertTrue(set(never) <= disabled, settings)
            self.assertEqual(command[command.index('--enable') + 1:], ['skip_host_skill_discovery', '-'])

    def test_awkward_paths_survive_toml_encoding(self):          # Review Focus 3
        workspace = '/r/agents/s/work space "x" \\ 数据'
        parts = native.command_parts({'web_search': False, 'terminal': {'readable_roots': []}, 'subagents': None},
                                     workspace=workspace, run_root='/r', codex_home='/h', model='m', effort='low')
        value = next(v for v in parts['config'] if v.startswith('permissions.rsi_agent.filesystem='))
        self.assertEqual(tomllib.loads('t = ' + value.split('=', 1)[1])['t'][workspace], 'write')

    def test_toml_strings_round_trip_control_characters(self):
        for text in ('tab\there', 'line\nbreak', 'nul\x00', 'unit\x1f', 'delete\x7f', 'quote"back\\slash', '数据'):
            self.assertEqual(tomllib.loads('t = ' + native.toml_string(text))['t'], text)
        self.assertEqual(tomllib.loads('t = ' + native.toml_table([('a b', '"x"'), ('/c', 'd')]))['t'],
                         {'a b': '"x"', '/c': 'd'})

    def test_a_table_with_a_repeated_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'repeated'):
            native.toml_table([('/r', 'deny'), ('/r', 'read')])

    def test_a_missing_run_root_is_refused_not_written_as_a_bare_key(self):
        with self.assertRaisesRegex(ValueError, 'text'):
            native.command_parts(TERMINAL, workspace='/r/agents/s/workspace', run_root=None, codex_home='/h',
                                 model='m', effort='low')

    def test_invoke_records_settings_and_uses_the_native_preamble(self):
        # Patch transport.subprocess.Popen with the fake process of tests/v2/test_journal_transport.py.
        result = self.invoke(native={'web_search': True, 'terminal': {'readable_roots': []}, 'subagents': None})
        self.assertTrue((self.folder / 'prompt.txt').read_text().startswith(native.PREAMBLE))
        self.assertEqual(json.loads((self.folder / 'invocation.json').read_text())['native']['web_search'], True)
        self.assertTrue((self.workspace / '.tmp').is_dir())
        self.assertEqual(result['native']['web_search'], True)

    def test_the_journal_start_record_carries_the_settings(self):
        self.invoke(native=TERMINAL_AND_SEARCH)
        self.assertEqual(self.start_record(self.folder)['invocation']['native'], TERMINAL_AND_SEARCH)

    def test_invoke_denies_the_resolved_run_root(self):
        link = self.root / 'run-link'
        link.symlink_to(self.root, target_is_directory=True)
        self.invoke(native=TERMINAL, run_root=str(link))
        table = tomllib.loads('t = ' + self.config_value(self.commands[0], 'permissions.rsi_agent.filesystem='))['t']
        self.assertEqual(table[str(self.root.resolve())], 'deny')
        self.assertEqual(table[str(self.workspace.resolve())], 'write')
        self.assertNotIn(str(link), table)

    def test_invoke_with_everything_off_leaves_no_native_trace(self):
        for index, settings in enumerate((None, native.OFF)):
            folder = self.root / 'calls' / ('off-' + str(index))
            result = self.invoke(native=settings, folder=folder)
            self.assertTrue((folder / 'prompt.txt').read_text().startswith(transport.PREAMBLE))
            invocation = json.loads((folder / 'invocation.json').read_text())
            self.assertNotIn('native', invocation)
            self.assertNotIn('native', self.start_record(folder)['invocation'])
            self.assertNotIn('native', result)
            binding = transport._session_options(self.session())
            self.assertEqual(invocation['command'],
                             transport._command(folder.resolve(), 'm', 'low', binding['workspace'], binding))
        self.assertFalse((self.workspace / '.tmp').exists())

    def test_invoke_refuses_native_settings_without_a_session(self):
        with self.assertRaisesRegex(ValueError, 'persistent session'):
            transport.invoke('p', {'type': 'object'}, self.folder, 'm', 'low',
                             native={'web_search': True, 'terminal': None, 'subagents': None}, run_root=str(self.root))
        self.assertFalse(self.folder.exists())

    def test_invoke_refuses_native_settings_without_a_run_root(self):
        with self.assertRaisesRegex(ValueError, 'run root'):
            transport.invoke('p', {'type': 'object'}, self.folder, 'm', 'low', session=self.session(), native=SEARCH)
        self.assertFalse(self.folder.exists())

    def archive(self, events, invocation=None, name='archive'):
        """A call folder written before journals: ``events.jsonl`` and, unless ``None``, ``invocation.json`` text."""
        folder = self.root / name
        folder.mkdir()
        (folder / 'events.jsonl').write_bytes(self.wire(events).encode())
        if invocation is not None:
            (folder / 'invocation.json').write_text(invocation)
        return folder

    def test_stream_items_follow_the_settings(self):
        for settings in (None, native.OFF):
            self.assertEqual(native.stream_items(settings), frozenset())
        always = {'todo_list', 'error'}
        self.assertEqual(native.stream_items(SEARCH), always | {'web_search'})
        self.assertEqual(native.stream_items(TERMINAL), always | {'command_execution', 'file_change'})
        self.assertEqual(native.stream_items(SUBAGENTS), always | {'collab_tool_call'})
        self.assertEqual(native.stream_items(TERMINAL_AND_SUBAGENTS),
                         always | {'command_execution', 'file_change', 'collab_tool_call'})
        self.assertIsInstance(native.stream_items(SEARCH), frozenset)

    def test_the_last_message_is_the_response_when_tools_are_on(self):
        allowed = native.stream_items({'web_search': True, 'terminal': {'readable_roots': []}, 'subagents': None})
        raw, usage, complete, errors = transport._events(self.wire(NATIVE_STREAM), allowed)
        self.assertEqual((raw, errors, complete), ('{"ok":true}', [], True))

    def test_the_same_stream_is_refused_when_everything_is_off(self):
        errors = transport._events(self.wire(NATIVE_STREAM))[3]
        self.assertTrue(any('Forbidden tool' in e for e in errors))
        self.assertIn('Expected exactly one nonempty final assistant message', errors)

    def test_an_item_outside_the_settings_is_still_forbidden(self):
        allowed = native.stream_items({'web_search': True, 'terminal': None, 'subagents': None})
        for kind in ('command_execution', 'collab_tool_call', 'mcp_tool_call'):
            stream = [*NATIVE_STREAM[:2], {'type': 'item.completed', 'item': {'type': kind}}, *NATIVE_STREAM[-2:]]
            with self.subTest(kind=kind):
                self.assertTrue(any('Forbidden tool' in e for e in transport._events(self.wire(stream), allowed)[3]))

    def test_a_tool_turn_needs_a_nonempty_last_message(self):
        allowed = native.stream_items(TERMINAL_AND_SEARCH)
        silent = [e for e in NATIVE_STREAM if e.get('item', {}).get('type') != 'agent_message']
        blank = [*NATIVE_STREAM[:-1], {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': ' \n'}},
                 NATIVE_STREAM[-1]]
        for stream in (silent, blank):
            with self.subTest(messages=len(stream)):
                self.assertIn('Expected a nonempty final assistant message', transport._events(self.wire(stream), allowed)[3])

    def test_a_turn_that_ends_on_a_progress_note_is_a_parse_error_not_a_crash(self):   # Review Focus 1
        stream = [e for e in NATIVE_STREAM if e.get('item', {}).get('text') != '{"ok":true}']
        result = self.invoke(stream, native=TERMINAL_AND_SEARCH)
        self.assertEqual(result['execution_status'], 'succeeded')
        self.assertIsNone(result['response']); self.assertIsNotNone(result['parse_error'])
        self.assertEqual(result['usage'], {'input_tokens': 20, 'output_tokens': 5})

    def test_a_tool_turn_answers_through_invoke(self):
        result = self.invoke(NATIVE_STREAM, native=TERMINAL_AND_SEARCH)
        self.assertEqual((result['response'], result['error'], result['protocol_valid']), ({'ok': True}, None, True))
        self.assertEqual((self.folder / 'raw_response.txt').read_text(), '{"ok":true}')

    def test_a_certified_failed_tool_turn_without_a_message_is_protocol_valid(self):
        stream = [*NATIVE_STREAM[:2], {'type': 'turn.failed', 'error': {'message': 'Synthetic provider failure'}}]
        result = self.invoke(stream, native=SEARCH)
        self.assertEqual(result['execution_status'], 'failed_definite')
        self.assertTrue(result['protocol_valid'], result['error'])

    def test_audited_events_reads_the_recorded_settings(self):
        folder = self.archive(NATIVE_STREAM, json.dumps({'backend': 'codex', 'native': TERMINAL_AND_SEARCH}))
        self.assertEqual(transport.audited_events(folder),
                         ('{"ok":true}', {'input_tokens': 20, 'output_tokens': 5}, True, []))

    def test_audited_events_reads_a_folder_without_settings_as_today(self):
        expected = transport._events(self.wire(NATIVE_STREAM))
        self.assertTrue(any('Forbidden tool' in e for e in expected[3]))
        # Missing, unreadable, not an object, without settings, and with settings that are not normal form.
        for index, invocation in enumerate((None, '{"backend": "codex"', '[]', json.dumps({'backend': 'codex'}),
                                            json.dumps({'native': {'web_search': 'yes'}}))):
            with self.subTest(invocation=invocation):
                folder = self.archive(NATIVE_STREAM, invocation, name='archive-' + str(index))
                self.assertEqual(transport.audited_events(folder), expected)

    def test_stream_activity_summarises_completed_items(self):
        self.invoke(NATIVE_STREAM, native=TERMINAL_AND_SEARCH)
        self.assertEqual(transport.stream_activity(self.folder), {
            'items': {'agent_message': 2, 'command_execution': 1, 'file_change': 1, 'web_search': 1},
            'commands': [{'command': "/bin/zsh -lc 'cat in.txt'", 'exit_code': 0, 'status': 'completed'}],
            'searches': ['q'], 'files_changed': ['/w/notes.md'], 'omitted': 0})

    def test_stream_activity_reads_unique_frames_and_completed_searches_only(self):
        command = {**NATIVE_STREAM[4], 'event_id': 'command-1'}
        started = {'type': 'item.started', 'item': {'type': 'web_search', 'query': '', 'action': {'type': 'other'}}}
        stream = [*NATIVE_STREAM[:4], command, command, started, *NATIVE_STREAM[5:]]
        result = self.invoke(stream, native=TERMINAL_AND_SEARCH)
        self.assertTrue(result['protocol_valid'], result['error'])
        self.assertEqual(transport.stream_activity(self.folder), NATIVE_ACTIVITY)

    def test_stream_activity_falls_back_to_the_events_file(self):
        self.assertEqual(transport.stream_activity(self.archive(NATIVE_STREAM)), NATIVE_ACTIVITY)

    def test_stream_activity_is_bounded(self):
        def completed(item):
            return {'type': 'item.completed', 'item': item}
        stream = [completed({'type': 'command_execution', 'command': 'c' * 300, 'exit_code': 1, 'status': 'failed'})] * 40
        stream += [completed({'type': 'web_search', 'query': 'q' * 250})] * 33
        stream += [completed({'type': 'file_change', 'changes': [{'path': 'p' * 201, 'kind': 'update'}] * 35})]
        stream += [completed({'type': 'command_execution', 'command': ['rm'], 'exit_code': True, 'status': {'s': 1}})]
        activity = transport.stream_activity(self.archive(stream))
        self.assertEqual(activity['items'], {'command_execution': 41, 'web_search': 33, 'file_change': 1})
        self.assertEqual(activity['commands'], [{'command': 'c' * 200, 'exit_code': 1, 'status': 'failed'}] * 32)
        self.assertEqual(activity['searches'], ['q' * 200] * 32)
        self.assertEqual(activity['files_changed'], ['p' * 200] * 32)
        self.assertEqual(activity['omitted'], 9 + 1 + 3)
        malformed = transport.stream_activity(self.archive(stream[-1:], name='malformed'))
        self.assertEqual(malformed['commands'], [{'command': None, 'exit_code': None, 'status': None}])

    def test_line_separators_in_command_output_do_not_split_an_event(self):
        output = 'a b c\x85d\n'
        stream = [*NATIVE_STREAM[:4], {'type': 'item.completed', 'item': {**NATIVE_STREAM[4]['item'], 'aggregated_output': output}},
                  *NATIVE_STREAM[5:]]
        self.assertTrue(all(character in self.wire(stream) for character in '  \x85'))
        raw, usage, complete, errors = transport._events(self.wire(stream), native.stream_items(TERMINAL_AND_SEARCH))
        self.assertEqual((raw, complete, errors), ('{"ok":true}', True, []))
        result = self.invoke(stream, native=TERMINAL_AND_SEARCH)
        self.assertEqual((result['response'], result['error'], result['protocol_valid']), ({'ok': True}, None, True))
        self.assertEqual(transport.stream_activity(self.folder), NATIVE_ACTIVITY)

    def test_a_line_separator_in_the_only_message_is_kept_when_everything_is_off(self):
        text = '{"note": "a b"}'
        self.assertEqual(transport._events(self.wire(frames(text))),
                         (text, {'input_tokens': 20, 'output_tokens': 5}, True, []))
        result = self.invoke(frames(text))
        self.assertEqual((result['response'], result['error']), ({'note': 'a b'}, None))
        self.assertEqual((self.folder / 'raw_response.txt').read_bytes().decode(), text)

    def test_crlf_line_ends_still_parse_in_both_modes(self):
        for name, events, settings in (('off', frames(), None), ('on', NATIVE_STREAM, TERMINAL_AND_SEARCH)):
            with self.subTest(mode=name):
                raw, usage, complete, errors = transport._events(self.wire(events, '\r\n'), native.stream_items(settings))
                self.assertEqual((raw, complete, errors), ('{"ok":true}', True, []))
                folder = self.root / 'calls' / ('crlf-' + name)
                result = self.invoke(events, native=settings, folder=folder, end='\r\n')
                self.assertEqual((result['response'], result['error'], result['protocol_valid']), ({'ok': True}, None, True))
        self.assertEqual(transport.stream_activity(self.root / 'calls' / 'crlf-on'), NATIVE_ACTIVITY)

    def test_a_timeout_stops_a_command_running_in_its_own_session(self):
        marker = self.root / 'grandchild.pid'
        self.addCleanup(self.kill_if_alive, marker)
        code = ('import os,subprocess,sys,time;sys.stdin.read();'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True);'
                'open(%r,"w").write(str(p.pid));time.sleep(60)' % str(marker))
        result = self.invoke(code=code, timeout=2, session=self.session())     # real local process, no model
        pid = int(marker.read_text())
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertTrue(result['descendants_stopped'])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_call_that_ends_on_its_own_reports_descendants_stopped(self):
        self.assertTrue(self.invoke(frames(), session=self.session())['descendants_stopped'])

    def test_an_unreadable_process_table_still_kills_codex_and_reports_false(self):
        real = transport._POPEN
        failures = {'ps cannot start': OSError('ps unavailable'),
                    'ps exits nonzero': lambda command, **kwargs: real([sys.executable, '-c', 'raise SystemExit(3)'],
                                                                       **kwargs)}
        for case, failure in failures.items():
            with self.subTest(case=case):
                codex = self.real_popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
                self.addCleanup(codex.wait)
                self.addCleanup(codex.kill)          # Runs first: no-op once the process has been reaped.
                with patch.object(transport, '_POPEN', side_effect=failure) as ps:
                    self.assertFalse(transport._terminate(codex))
                ps.assert_called_once()
                self.assertEqual(codex.wait(timeout=5), -signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
