"""The Codex command and transport call built from one role's native settings.

The fake provider is a local Python process that writes the Codex event
protocol; no model, network request or credential is used.
"""
import base64
import hashlib
import itertools
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rsi_lab import native, transport
from rsi_lab.journal import read_journal
from tests.v2.native_fixtures import (CHECK_PY, LOCAL_SERVER_ONLY, MCP_ITEM, PLAIN, PLOT_CHECK, R, ROLES, SEARCH,
                                      SKILL_MD, SUBAGENTS, TERMINAL, TERMINAL_AND_SEARCH, TERMINAL_AND_SUBAGENTS,
                                      TOOL_SERVERS, URL_SERVER_ONLY, VIEW_IMAGE, WITH_ROLES, WITH_SKILLS,
                                      WITH_TOOL_SERVERS)
from tests.v2.test_journal_transport import THREAD, frames

TODAY = ['codex', '--no-daemon', '--ask-for-approval', 'never', 'exec', '--sandbox', 'read-only', '-C', '/w',
         '--ignore-user-config', '--model', 'm', '--skip-git-repo-check', '--json', '--output-schema', '/f/schema.json',
         '-c', 'model_reasoning_effort="low"', '-c', 'web_search="disabled"',
         '-c', 'suppress_unstable_features_warning=true', '-c', 'agents.enabled=false',
         '-c', 'skills.include_instructions=false']
# ... followed by '--disable', X for every X in transport.DISABLED_FEATURES, then
# '--enable', 'skip_host_skill_discovery', the thread id when resuming, and '-'.
# The command values of TOOL_SERVERS, in server name order.
SERVER_VALUES = [
    'mcp_servers.chem.command="/opt/tools/bin/python"', 'mcp_servers.chem.args=["-m","chem_server"]',
    'mcp_servers.chem.env={"CHEM_MODE"="fast"}', 'mcp_servers.chem.env_vars=["CHEM_API_KEY"]',
    'mcp_servers.chem.tool_timeout_sec=300', 'mcp_servers.chem.default_tools_approval_mode="approve"',
    'mcp_servers.papers.url="https://papers.example.org/mcp"', 'mcp_servers.papers.bearer_token_env_var="PAPERS_TOKEN"',
    'mcp_servers.papers.http_headers={"X-Client"="rsi-lab"}', 'mcp_servers.papers.default_tools_approval_mode="approve"']

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


# The environment ps runs in when the kernel reads the process table: its start times then read the same whoever reads
# them, whatever their locale or time zone.
PS_ENVIRONMENT = {'LC_ALL': 'C', 'TZ': 'UTC0', 'PATH': '/usr/bin:/bin'}
# The kernel's reader of the CLI's version, which every test of NativeTransportTests replaces; its own test calls it
# through a replaced transport._POPEN.
READ_CODEX_VERSION = transport.codex_version


def record(marker):
    """Python statements that write ``"<pid> <start>"`` for the process ``p`` to ``marker`` in one step, the start
    time read from the process table right after ``p`` appears, as the kernel reads it: the identity a cleanup
    confirms before a signal."""
    part = str(marker) + '.part'
    return ('s=subprocess.run(["ps","-o","lstart=","-p",str(p.pid)],capture_output=True,text=True,env=%r).stdout.strip();'
            'open(%r,"w").write(str(p.pid)+" "+s);os.replace(%r,%r);' % (PS_ENVIRONMENT, part, part, str(marker)))


def lstart(pid):
    """The start time ``ps`` shows for ``pid``, as ``record`` reads it; ``''`` when no process has ``pid``."""
    return subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True,
                          env=PS_ENVIRONMENT).stdout.strip()


class NativeTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'calls' / 'effect'
        self.workspace = self.root / 'agents' / 's' / 'workspace'
        self.real_popen = subprocess.Popen
        self.commands = []
        # An empty home: no everyday Codex home to deny, whatever this machine's own home holds.
        (self.root / 'home').mkdir()
        home = patch.dict(os.environ, {'HOME': str(self.root / 'home')})
        home.start(); self.addCleanup(home.stop)
        # A Codex CLI of the checked version, whatever this machine has installed: the CLI is never asked.
        version = patch.object(transport, 'codex_version', return_value='0.170.0')
        version.start(); self.addCleanup(version.stop)

    def command(self, settings, thread=None):
        session = {'provider_thread_id': thread, 'codex_home': '/home/.codex'}
        return transport._command(Path('/f'), 'm', 'low', '/r/agents/s/workspace', session,
                                  native=settings, run_root='/r')

    def parts(self, settings, codex_home='/h'):
        return native.command_parts(settings, workspace='/w', run_root='/r', codex_home=codex_home, model='m',
                                    effort='low')

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

    def stop_recorded(self, marker):
        """Kill the process ``marker`` records only while it is still that process, so a failing test leaves nothing
        behind: a pid that now has another start time, or none, may name an unrelated process and is never signalled."""
        if marker.exists():
            pid, start = marker.read_text().split(' ', 1)
            if start and lstart(pid) == start:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def assert_stopped(self, marker):
        """The process ``marker`` records is gone; the marker then goes too, so no cleanup can signal its pid."""
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text().split()[0]), 0)
        marker.unlink()

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

    def test_sub_agents_are_switched_off_by_the_agents_setting_unless_they_are_on(self):
        # Codex 0.170.0 ignores the multi_agent features as an off switch.
        for settings in (None, SEARCH, TERMINAL, TERMINAL_AND_SEARCH):
            command = transport._command(Path('/f'), 'm', 'low', '/w', {'provider_thread_id': None, 'codex_home': '/h'},
                                         native=settings, run_root='/r')
            self.assertEqual(command[command.index('agents.enabled=false') - 1], '-c')
        for settings in (SUBAGENTS, TERMINAL_AND_SUBAGENTS):
            command = transport._command(Path('/f'), 'm', 'low', '/w', {'provider_thread_id': None, 'codex_home': '/h'},
                                         native=settings, run_root='/r')
            self.assertNotIn('agents.enabled=false', command)

    def test_a_skill_may_not_install_the_tool_servers_it_names(self):
        self.assertIn('skill_mcp_dependency_install', transport.DISABLED_FEATURES)

    def test_a_codex_version_that_was_not_checked_is_refused(self):
        transport.check_codex_version(None)
        for version in transport.SUPPORTED_CODEX_VERSIONS:
            transport.check_codex_version(version)
        with patch.dict('os.environ', {}, clear=False):
            os.environ.pop('RSI_LAB_ALLOW_CODEX_VERSION', None)
            with self.assertRaisesRegex(ValueError, 'Codex CLI 0.163.0 is not a version'):
                transport.check_codex_version('0.163.0')
            os.environ['RSI_LAB_ALLOW_CODEX_VERSION'] = '0.163.0'
            transport.check_codex_version('0.163.0')
            with self.assertRaises(ValueError):
                transport.check_codex_version('0.999.0')

    def test_the_codex_version_is_read_from_what_the_cli_prints(self):
        def cli(text, status=0):
            code = 'import sys; sys.stdout.write(' + repr(text) + '); sys.exit(' + str(status) + ')'
            return lambda command, **kwargs: subprocess.Popen([sys.executable, '-c', code], **kwargs)
        for text, status, expected in (('codex-cli 0.170.0\n', 0, '0.170.0'), ('codex-cli 0.171.0-alpha.2\n', 0, '0.171.0-alpha.2'),
                                       ('codex-cli 0.170.0\n', 1, None), ('something else 1.2.3\n', 0, None), ('', 0, None)):
            with self.subTest(text=text, status=status), patch.object(transport, '_POPEN', cli(text, status)):
                self.assertEqual(READ_CODEX_VERSION(), expected)
        with patch.object(transport, '_POPEN', side_effect=FileNotFoundError('codex')):
            self.assertIsNone(READ_CODEX_VERSION())

    def test_a_call_records_the_codex_version_it_ran_on(self):
        wire = ''.join(json.dumps(event) + '\n' for event in frames(thread=THREAD)).encode()
        code = 'import sys; sys.stdin.read(); sys.stdout.buffer.write(' + repr(wire) + '); sys.stdout.buffer.flush()'
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as tmp, patch.object(transport, 'codex_version', return_value='0.170.0'), \
                patch.object(transport.subprocess, 'Popen', side_effect=lambda command, **kwargs:
                             real_popen([sys.executable, '-u', '-c', code], **kwargs)):
            folder = Path(tmp) / 'call'
            result = transport.invoke('Inspect this task', {'type': 'object'}, folder, 'm', 'low', timeout=10)
            self.assertEqual(result['codex_version'], '0.170.0')
            self.assertEqual(json.loads((folder / 'invocation.json').read_text())['codex_version'], '0.170.0')
            self.assertEqual(json.loads((folder / 'transport_result.json').read_text())['codex_version'], '0.170.0')

    def test_invoke_sends_nothing_on_an_unchecked_codex_version(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(transport, 'codex_version', return_value='0.1.0'), \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('Codex must not be started')):
            with self.assertRaisesRegex(ValueError, 'not a version this kernel was checked against'):
                transport.invoke('p', {'type': 'object'}, Path(tmp) / 'call', 'm', 'low')
            self.assertFalse((Path(tmp) / 'call').exists())

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
        items = ['-m', 'a b', 'quote"back\\slash', 'line\nbreak', '数据', '']
        self.assertEqual(tomllib.loads('t = ' + native.toml_array(items))['t'], items)
        self.assertEqual(native.toml_array([]), '[]')

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

    def test_role_files_are_written_fresh(self):                              # Review Focus 2
        folder = self.root / 'agents' / 's1' / 'roles'
        folder.mkdir(parents=True)
        (folder / 'local.toml').write_text('developer_instructions = "stale"\n', encoding='utf-8')
        (folder / 'gone.toml').write_text('x = 1\n', encoding='utf-8')
        transport.write_role_files(folder, ROLES)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['challenge.toml', 'local.toml'])
        self.assertEqual(tomllib.loads((folder / 'challenge.toml').read_text(encoding='utf-8'))
                         ['developer_instructions'], ROLES['challenge']['instructions'])
        # Each file holds exactly the role's name, description and instructions, readable by its owner only.
        for name, role in ROLES.items():
            path = folder / (name + '.toml')
            self.assertEqual(tomllib.loads(path.read_text(encoding='utf-8')),
                             {'name': name, 'description': role['description'],
                              'developer_instructions': role['instructions']})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # Written again: the file of a role no longer declared goes, and so does a stale link (not what it points
        # to); an entry that is no role file, or a folder, stays.
        (folder / 'notes.txt').write_text('Not a role file.\n', encoding='utf-8')
        (folder / 'kept.toml').mkdir()
        outside = self.root / 'outside.toml'
        outside.write_text('kept = true\n', encoding='utf-8')
        (folder / 'old.toml').symlink_to(outside)
        transport.write_role_files(folder, {'local': R})
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['kept.toml', 'local.toml', 'notes.txt'])
        self.assertEqual((folder / 'notes.txt').read_text(encoding='utf-8'), 'Not a role file.\n')
        self.assertEqual(outside.read_text(encoding='utf-8'), 'kept = true\n')
        # A folder that does not exist yet is made, open to its owner only.
        fresh = self.root / 'agents' / 's2' / 'roles'
        transport.write_role_files(fresh, ROLES)
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in fresh.iterdir()), ['challenge.toml', 'local.toml'])

    def test_role_folder_or_file_may_not_be_a_symlink(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        folder = self.root / 'agents' / 's1' / 'roles'
        folder.parent.mkdir(parents=True)
        folder.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            transport.write_role_files(folder, ROLES)
        self.assertEqual(list(elsewhere.iterdir()), [])
        # A real folder whose local.toml is a link: nothing is written, replaced or deleted.
        folder.unlink()
        folder.mkdir()
        outside = self.root / 'outside.toml'
        outside.write_text('kept = true\n', encoding='utf-8')
        (folder / 'local.toml').symlink_to(outside)
        (folder / 'gone.toml').write_text('x = 1\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            transport.write_role_files(folder, ROLES)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ['gone.toml', 'local.toml'])
        self.assertTrue((folder / 'local.toml').is_symlink())
        self.assertEqual(outside.read_text(encoding='utf-8'), 'kept = true\n')

    def test_role_text_or_name_that_cannot_be_written_is_refused_before_anything_is_written(self):
        folder = self.root / 'agents' / 's1' / 'roles'
        folder.mkdir(parents=True)
        (folder / 'gone.toml').write_text('x = 1\n', encoding='utf-8')
        lone_surrogate = {'challenge': ROLES['challenge'], 'local': {**R, 'instructions': 'Refine \ud800.'}}
        for roles, message in ((lone_surrogate, 'UTF-8'), ({'challenge': ROLES['challenge'], '../escape': R}, 'name')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                transport.write_role_files(folder, roles)
            self.assertEqual([path.name for path in folder.iterdir()], ['gone.toml'])
        self.assertEqual([path.name for path in folder.parent.iterdir()], ['roles'])

    def test_command_carries_roles_only_when_declared(self):
        session = {'provider_thread_id': None, 'codex_home': '/home/.codex'}
        limits = ['-c', 'agents.max_depth=1', '-c', 'agents.max_concurrent_threads_per_session=3',
                  '-c', 'agents.default_subagent_model="m"', '-c', 'agents.default_subagent_reasoning_effort="low"']
        tail = [part for f in transport.DISABLED_FEATURES if f != 'multi_agent' for part in ('--disable', f)] + [
            '--enable', 'skip_host_skill_discovery', '-']
        # Sub-agents on: no agents.enabled=false, and the skills switch follows the unstable features warning.
        head = TODAY[:-4] + ['-c', 'skills.include_instructions=false']
        # Without roles a folder for them changes nothing: the command is the one built at the base.
        for roles_dir in (None, '/r/agents/s/roles'):
            self.assertEqual(transport._command(Path('/f'), 'm', 'low', '/w', session, native=PLAIN, run_root='/r',
                                                roles_dir=roles_dir), head + limits + tail)
        command = transport._command(Path('/f'), 'm', 'low', '/w', session, native=WITH_ROLES, run_root='/r',
                                     roles_dir='/r/agents/s/roles')
        roles = ['-c', 'agents.challenge.description="Argues against the \\"leading\\" series."',
                 '-c', 'agents.challenge.config_file="/r/agents/s/roles/challenge.toml"',
                 '-c', 'agents.local.description="Advances the leading series."',
                 '-c', 'agents.local.config_file="/r/agents/s/roles/local.toml"']
        self.assertEqual(command, head + limits + roles + tail)
        self.assertEqual(tomllib.loads('t = ' + self.config_value(command, 'agents.challenge.description='))['t'],
                         ROLES['challenge']['description'])
        with self.assertRaisesRegex(ValueError, 'roles_dir'):
            transport._command(Path('/f'), 'm', 'low', '/w', session, native=WITH_ROLES, run_root='/r')

    def test_invoke_refuses_roles_it_cannot_hand_to_codex_before_codex_starts(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        linked = self.root / 'agents' / 's' / 'linked-roles'
        linked.parent.mkdir(parents=True)
        linked.symlink_to(elsewhere, target_is_directory=True)
        roles_dir = self.root / 'agents' / 's' / 'roles'
        lone_surrogate = {**WITH_ROLES, 'subagents': {'max_concurrent': 3, 'roles': {
            'local': {**R, 'instructions': 'Refine \ud800.'}}}}
        cases = {'no role folder': (WITH_ROLES, None, 'roles_dir'),
                 'a symlinked role folder': (WITH_ROLES, linked, 'symlink'),
                 'role text that cannot be UTF-8': (lone_surrogate, roles_dir, 'UTF-8')}
        for case, (settings, folder, message) in cases.items():
            with self.subTest(case=case), \
                    patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('Codex started')) as popen:
                with self.assertRaisesRegex(ValueError, message):
                    transport.invoke('p', {'type': 'object'}, self.folder, 'm', 'low', session=self.session(),
                                     native=settings, run_root=str(self.root), roles_dir=folder)
                popen.assert_not_called()
                self.assertFalse(self.folder.exists())
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse(roles_dir.exists())

    def test_tool_servers_are_named_in_name_order(self):
        self.assertEqual(self.parts(WITH_TOOL_SERVERS)['config'], SERVER_VALUES)
        self.assertEqual(self.parts(WITH_TOOL_SERVERS)['sandbox'], ['--sandbox', 'read-only'])
        command = self.command(WITH_TOOL_SERVERS)
        for value in SERVER_VALUES + ['skills.include_instructions=false']:
            self.assertEqual(command[command.index(value) - 1], '-c')
        # Read as TOML, as Codex reads each -c value, they give the servers' tables; the timeout is an integer.
        self.assertEqual(tomllib.loads('\n'.join(SERVER_VALUES)), {'mcp_servers': {
            'chem': {'command': '/opt/tools/bin/python', 'args': ['-m', 'chem_server'], 'env': {'CHEM_MODE': 'fast'},
                     'env_vars': ['CHEM_API_KEY'], 'tool_timeout_sec': 300, 'default_tools_approval_mode': 'approve'},
            'papers': {'url': 'https://papers.example.org/mcp', 'bearer_token_env_var': 'PAPERS_TOKEN',
                       'http_headers': {'X-Client': 'rsi-lab'}, 'default_tools_approval_mode': 'approve'}}})

    def test_values_not_given_are_left_out(self):
        bare = {'web_search': False, 'terminal': None, 'subagents': None, 'tool_servers': {
            'a': {'description': 'd', 'command': '/bin/s', 'args': [], 'env': {}, 'env_from': []},
            'b': {'description': 'd', 'url': 'http://localhost:9/mcp', 'headers': {}}}}
        self.assertEqual(self.parts(bare)['config'], [
            'mcp_servers.a.command="/bin/s"', 'mcp_servers.a.default_tools_approval_mode="approve"',
            'mcp_servers.b.url="http://localhost:9/mcp"', 'mcp_servers.b.default_tools_approval_mode="approve"'])

    def test_image_viewing_is_no_longer_disabled(self):
        command = self.command(VIEW_IMAGE)
        self.assertNotIn('view_image', command)
        self.assertEqual(command[command.index('image_generation') - 1], '--disable')
        self.assertEqual(self.parts(VIEW_IMAGE)['enabled'], {'shell_tool', 'unified_exec', 'view_image'})
        without = self.command(TERMINAL)
        self.assertEqual(without[without.index('view_image') - 1], '--disable')

    def test_skills_settings_on_the_command(self):
        self.assertEqual(self.parts(WITH_SKILLS)['config'][-2:], ['skills.bundled.enabled=false', 'project_root_markers=[]'])
        command = self.command(WITH_SKILLS)
        self.assertNotIn('skills.include_instructions=false', command)
        moved = {**WITH_SKILLS, 'skills': {**WITH_SKILLS['skills'], 'root': '/elsewhere'}}
        self.assertEqual(self.command(moved), command)                         # Nothing depends on the owner's folders.
        for settings in (None, SEARCH, TERMINAL, PLAIN, WITH_TOOL_SERVERS):
            self.assertIn('skills.include_instructions=false', self.command(settings))
        # Everything in order: terminal values, sub-agent values, tool server values, skills values.
        everything = {**WITH_SKILLS, 'subagents': {'max_concurrent': 2}, 'tool_servers': TOOL_SERVERS}
        config = self.parts(everything)['config']
        self.assertEqual(config[-12:], SERVER_VALUES + ['skills.bundled.enabled=false', 'project_root_markers=[]'])
        self.assertEqual(config[-16:-12], ['agents.max_depth=1', 'agents.max_concurrent_threads_per_session=2',
                                           'agents.default_subagent_model="m"',
                                           'agents.default_subagent_reasoning_effort="low"'])
        self.assertEqual(config[-17], 'project_doc_max_bytes=0')

    def test_a_separate_home_also_denies_the_everyday_one(self):
        home = self.root / 'user'; (home / '.codex').mkdir(parents=True)
        everyday = str((home / '.codex').resolve())
        with patch.dict(os.environ, {'HOME': str(home)}):
            table = tomllib.loads('t = ' + self.config_value(self.command(TERMINAL), 'permissions.rsi_agent.filesystem='))['t']
            self.assertEqual(list(table)[2:4], ['/home/.codex', everyday]); self.assertEqual(table[everyday], 'deny')
            same = self.parts(TERMINAL, codex_home=everyday)['config']
            self.assertEqual(sum(value.count(json.dumps(everyday)) for value in same), 1)
            # Named through a link, the everyday home is still the call's own (both are compared resolved): it gets
            # no second entry.
            link = self.root / 'home-link'
            link.symlink_to(home / '.codex', target_is_directory=True)
            linked = self.parts(TERMINAL, codex_home=str(link))['config']
            self.assertEqual(sum(value.count(json.dumps(everyday)) for value in linked), 0)
            # Without the terminal there is no profile, and the everyday home is not named.
            self.assertNotIn(everyday, ''.join(self.command(WITH_TOOL_SERVERS)))
        with patch.dict(os.environ, {'HOME': str(self.root / 'nobody')}):
            self.assertNotIn('nobody', ''.join(self.command(TERMINAL)))

    def test_secret_values_reach_no_record_and_no_command(self):                  # Review Focus 4
        secret = 'invented-secret-4f1c'
        with patch.dict(os.environ, {'CHEM_API_KEY': secret, 'PAPERS_TOKEN': secret + '-url'}), \
                patch.object(transport, 'codex_version', return_value='0.170.0'):
            result = self.invoke(native={**LOCAL_SERVER_ONLY, 'tool_servers': TOOL_SERVERS})
        self.assertTrue(result['protocol_valid'], result['error'])
        for path in self.folder.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), path.name)
        self.assertFalse(any(secret in part for part in self.commands[0]))
        self.assertIn('mcp_servers.chem.env_vars=["CHEM_API_KEY"]', self.commands[0])

    def test_an_unset_or_empty_variable_refuses_before_codex_starts(self):        # Review Focus 4
        for token in (None, ''):
            with self.subTest(token=token), patch.dict(os.environ, {'CHEM_API_KEY': 'x'}), \
                    patch.object(transport, 'codex_version', side_effect=AssertionError('asked')):
                os.environ.pop('PAPERS_TOKEN', None)
                if token is not None:
                    os.environ['PAPERS_TOKEN'] = token
                with self.assertRaisesRegex(ValueError, 'not set: PAPERS_TOKEN$'):
                    self.invoke(native=WITH_TOOL_SERVERS)
                self.assertEqual(self.commands, []); self.assertFalse(self.folder.exists())
                self.assertFalse(self.workspace.exists())
        with patch.dict(os.environ, {}), patch.object(transport, 'codex_version', side_effect=AssertionError('asked')):
            for name in ('CHEM_API_KEY', 'PAPERS_TOKEN'):
                os.environ.pop(name, None)
            with self.assertRaisesRegex(ValueError, 'are not set: CHEM_API_KEY, PAPERS_TOKEN$'):
                self.invoke(native=WITH_TOOL_SERVERS)

    def test_a_skills_call_in_a_home_with_skills_is_refused_before_codex_is_asked(self):
        codex_home = self.root / 'codex-home'
        (codex_home / 'skills' / '.system' / 'imagegen').mkdir(parents=True)
        # The session's Codex home is the one looked at, not CODEX_HOME, which is another, empty folder here.
        (self.root / 'other-home').mkdir()
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'other-home')}), \
                patch.object(native, 'MACHINE_SKILLS', self.root / 'machine-skills'), \
                patch.object(transport, 'codex_version', return_value='0.170.0') as version:
            self.invoke(native=WITH_SKILLS, folder=self.root / 'calls' / 'system-only')   # Codex's own .system
            self.assertEqual((len(self.commands), version.call_count), (1, 1))
            (codex_home / 'skills' / 'secret-skill').mkdir()
            with self.assertRaises(ValueError) as raised:
                self.invoke(native=WITH_SKILLS)
            self.assertEqual(str(raised.exception), 'A role with skills needs a separate Codex home with no skills '
                             'of its own: ' + str(codex_home.resolve() / 'skills') + ' holds entries. '
                             + native.SEPARATE_HOME_HELP)
            self.assertEqual((len(self.commands), version.call_count), (1, 1))
            self.assertFalse(self.folder.exists())
            # A call without skills goes ahead in the same home.
            self.invoke(native=TERMINAL)
            self.assertEqual(len(self.commands), 2)

    def stored(self, folder, *raws):
        """``folder``'s ``objects`` made a run store's object folder holding each of ``raws`` under its SHA-256,
        read-only as the store keeps them. Returns the object folder."""
        objects = folder / 'objects'
        objects.mkdir(parents=True)
        for raw in raws:
            path = objects / hashlib.sha256(raw).hexdigest()
            path.write_bytes(raw)
            path.chmod(0o444)
        return objects

    def stale_agents(self, workspace):
        """``workspace`` made with a ``.agents`` left by an earlier call: ``stale.md`` and a skill ``old``."""
        (workspace / '.agents' / 'skills' / 'old').mkdir(parents=True)
        (workspace / '.agents' / 'stale.md').write_text('Stale.\n', encoding='utf-8')
        (workspace / '.agents' / 'skills' / 'old' / 'SKILL.md').write_text('---\nname: old\n---\n', encoding='utf-8')

    def files_under(self, folder):
        """The relative paths of the files under ``folder``, sorted; no folder, no files."""
        return sorted(str(path.relative_to(folder)) for path in folder.rglob('*') if path.is_file())

    def test_skills_are_written_fresh_into_the_workspace(self):
        objects = self.stored(self.root, SKILL_MD, CHECK_PY)
        self.stale_agents(self.workspace)
        transport.write_skills(self.workspace, {'plot-check': PLOT_CHECK}, objects)
        agents = self.workspace / '.agents'
        self.assertEqual(self.files_under(agents), ['skills/plot-check/SKILL.md', 'skills/plot-check/scripts/check.py'])
        self.assertEqual((agents / 'skills/plot-check/SKILL.md').read_bytes(), SKILL_MD)
        self.assertEqual((agents / 'skills/plot-check/scripts/check.py').read_bytes(), CHECK_PY)
        # Open to their owner only: every folder 0o700, every file 0o600, whatever the blob's mode (0o444 here).
        for path in [agents, *agents.rglob('*')]:
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o700 if path.is_dir() else 0o600, path)
        # Nothing else in the workspace was touched, and a workspace not made yet is made.
        self.assertEqual([path.name for path in self.workspace.iterdir()], ['.agents'])
        fresh = self.root / 'agents' / 's2' / 'workspace'
        transport.write_skills(fresh, {'plot-check': PLOT_CHECK}, objects)
        self.assertEqual(self.files_under(fresh), ['.agents/skills/plot-check/SKILL.md',
                                                   '.agents/skills/plot-check/scripts/check.py'])

    def test_a_missing_changed_or_linked_blob_writes_nothing(self):
        check = PLOT_CHECK['files']['scripts/check.py']

        def missing(objects):
            (objects / check).unlink()

        def changed(objects):
            (objects / check).chmod(0o600)
            (objects / check).write_bytes(b'print("other bytes")\n')

        def linked(objects):
            # The link leads to the right bytes; the blob is still not the store's own file.
            (objects / check).unlink()
            (objects.parent / 'elsewhere.py').write_bytes(CHECK_PY)
            (objects / check).symlink_to(objects.parent / 'elsewhere.py')

        def a_folder(objects):
            (objects / check).unlink()
            (objects / check).mkdir()
        cases = {'missing': (missing, 'is missing'), 'changed': (changed, 'holds other bytes than its name says'),
                 'linked': (linked, 'is a symlink'), 'a folder': (a_folder, 'is not a regular file')}
        for case, (alter, message) in cases.items():
            with self.subTest(case=case):
                place = self.root / case
                objects = self.stored(place, SKILL_MD, CHECK_PY)
                alter(objects)
                workspace = place / 'workspace'
                self.stale_agents(workspace)
                with self.assertRaises(ValueError) as raised:
                    transport.write_skills(workspace, {'plot-check': PLOT_CHECK}, objects)
                self.assertEqual(str(raised.exception), 'The run store does not hold skill file '
                                 'plot-check/scripts/check.py: objects/' + check + ' ' + message)
                self.assertEqual(self.files_under(workspace), ['.agents/skills/old/SKILL.md', '.agents/stale.md'])
                self.assertEqual((workspace / '.agents' / 'stale.md').read_text(encoding='utf-8'), 'Stale.\n')

    def test_a_skill_name_path_or_hash_that_leaves_its_folder_writes_nothing(self):
        objects = self.stored(self.root, SKILL_MD, CHECK_PY)
        self.stale_agents(self.workspace)
        before = self.files_under(self.root)
        files = PLOT_CHECK['files']
        cases = {'a name': {'../../plot-check': PLOT_CHECK},
                 'a path': {'plot-check': {**PLOT_CHECK, 'files': {**files, '../../../../escape.py': files['SKILL.md']}}},
                 'a hash': {'plot-check': {**PLOT_CHECK, 'files': {**files, 'scripts/check.py': '../' + 'a' * 61}}}}
        for case, skills in cases.items():
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, 'skill'):
                    transport.write_skills(self.workspace, skills, objects)
                self.assertEqual(self.files_under(self.root), before)

    def test_skill_paths_that_clash_or_cannot_be_printed_write_nothing(self):
        # A hand-made mapping the settings would refuse: on a volume that ignores letter case one file would replace
        # the other, or a file would stand where a folder must.
        objects = self.stored(self.root, SKILL_MD, CHECK_PY)
        self.stale_agents(self.workspace)
        files, blob = PLOT_CHECK['files'], PLOT_CHECK['files']['scripts/check.py']
        cases = {'skill.md': "Skill plot-check may not hold both 'SKILL.md' and 'skill.md': where letter case is "
                             'ignored they are one file, or a file and a folder at one path',
                 'Scripts': "Skill plot-check may not hold both 'Scripts' and 'scripts/check.py'",
                 'scripts/check.py/x': "Skill plot-check may not hold both 'scripts/check.py' and 'scripts/check.py/x'",
                 'a\nb': "Invalid path of skill plot-check: 'a\\nb'",
                 'a\x00b': "Invalid path of skill plot-check: 'a\\x00b'"}
        for path, message in cases.items():
            with self.subTest(path=path):
                skills = {'plot-check': {**PLOT_CHECK, 'files': {**files, path: blob}}}
                with self.assertRaises(ValueError) as raised:
                    transport.write_skills(self.workspace, skills, objects)
                self.assertTrue(str(raised.exception).startswith(message), str(raised.exception))
                self.assertEqual(self.files_under(self.workspace), ['.agents/skills/old/SKILL.md', '.agents/stale.md'])

    def test_a_symlinked_agents_folder_is_replaced_not_followed(self):
        objects = self.stored(self.root, SKILL_MD, CHECK_PY)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('Kept.\n', encoding='utf-8')
        self.workspace.mkdir(parents=True)
        (self.workspace / '.agents').symlink_to(outside, target_is_directory=True)
        transport.write_skills(self.workspace, {'plot-check': PLOT_CHECK}, objects)
        self.assertEqual(self.files_under(outside), ['keep.txt'])
        self.assertEqual((outside / 'keep.txt').read_text(encoding='utf-8'), 'Kept.\n')
        agents = self.workspace / '.agents'
        self.assertFalse(agents.is_symlink())
        self.assertTrue(agents.is_dir())
        self.assertEqual(self.files_under(agents), ['skills/plot-check/SKILL.md', 'skills/plot-check/scripts/check.py'])
        # A file in its place, or a link inside it, goes the same way; what a link inside leads to stays.
        agents_file = self.root / 'file' / 'workspace'
        agents_file.mkdir(parents=True)
        (agents_file / '.agents').write_text('Not a folder.\n', encoding='utf-8')
        transport.write_skills(agents_file, {'plot-check': PLOT_CHECK}, objects)
        self.assertEqual(self.files_under(agents_file / '.agents'), ['skills/plot-check/SKILL.md',
                                                                     'skills/plot-check/scripts/check.py'])
        linked_inside = self.root / 'inner' / 'workspace'
        (linked_inside / '.agents' / 'skills').mkdir(parents=True)
        (linked_inside / '.agents' / 'skills' / 'old').symlink_to(outside, target_is_directory=True)
        transport.write_skills(linked_inside, {'plot-check': PLOT_CHECK}, objects)
        self.assertEqual(self.files_under(outside), ['keep.txt'])
        self.assertEqual(self.files_under(linked_inside / '.agents'), ['skills/plot-check/SKILL.md',
                                                                       'skills/plot-check/scripts/check.py'])

    def test_a_symlinked_workspace_is_refused_and_nothing_is_written_through_it(self):
        objects = self.stored(self.root, SKILL_MD, CHECK_PY)
        outside = self.root / 'outside'
        self.stale_agents(outside)
        self.workspace.parent.mkdir(parents=True)
        self.workspace.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            transport.write_skills(self.workspace, {'plot-check': PLOT_CHECK}, objects)
        self.assertEqual(self.files_under(outside), ['.agents/skills/old/SKILL.md', '.agents/stale.md'])

    def archive(self, events, invocation=None, name='archive'):
        """A call folder written before journals: ``events.jsonl`` and, unless ``None``, ``invocation.json`` text."""
        folder = self.root / name
        folder.mkdir()
        (folder / 'events.jsonl').write_bytes(self.wire(events).encode())
        if invocation is not None:
            (folder / 'invocation.json').write_text(invocation)
        return folder

    def test_call_files_are_read_as_utf8_whatever_the_locale(self):
        # Under a locale whose encoding is not UTF-8, a non-ASCII readable root would read as everything off.
        data = self.root / 'données'
        data.mkdir()
        settings = {**TERMINAL_AND_SEARCH, 'terminal': {'readable_roots': [str(data)]}}
        stream = [*NATIVE_STREAM[:2], {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Début.'}},
                  *NATIVE_STREAM[3:]]
        folder = self.archive(stream)
        (folder / 'invocation.json').write_bytes(json.dumps({'backend': 'codex', 'native': settings},
                                                            ensure_ascii=False).encode())
        read_text = Path.read_text

        def ascii_locale(path, encoding=None, errors=None, newline=None):
            return read_text(path, encoding=encoding or 'ascii', errors=errors, newline=newline)
        with patch.object(Path, 'read_text', ascii_locale):
            self.assertEqual(transport._recorded_native(folder), settings)
            self.assertEqual(transport.audited_events(folder),
                             ('{"ok":true}', {'input_tokens': 20, 'output_tokens': 5}, True, []))
            self.assertEqual(transport.stream_activity(folder)['items']['agent_message'], 2)

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
        # Tool server calls appear only with tool servers declared; image viewing adds no item type.
        self.assertEqual(native.stream_items(WITH_TOOL_SERVERS), always | {'web_search', 'mcp_tool_call'})
        for settings in (LOCAL_SERVER_ONLY, URL_SERVER_ONLY):
            self.assertEqual(native.stream_items(settings), always | {'mcp_tool_call'})
        self.assertEqual(native.stream_items(VIEW_IMAGE), native.stream_items(TERMINAL))

    def test_tool_server_items_only_for_a_declared_server(self):
        stream = [*NATIVE_STREAM[:2], {'type': 'item.completed', 'item': MCP_ITEM}, *NATIVE_STREAM[-2:]]
        allowed = native.stream_items(WITH_TOOL_SERVERS)
        self.assertEqual(transport._events(self.wire(stream), allowed, frozenset({'chem', 'papers'}))[3], [])
        other = [*stream[:2], {'type': 'item.started', 'item': {**MCP_ITEM, 'server': 'other'}}, *stream[2:]]
        self.assertIn("Undeclared tool server: 'other'",
                      transport._events(self.wire(other), allowed, frozenset({'chem'}))[3])
        # Without tool servers the item stays forbidden, with the one error it had before they existed.
        for settings in (SEARCH, None):
            with self.subTest(settings=settings):
                self.assertEqual(transport._events(self.wire(stream), native.stream_items(settings))[3],
                                 ["Forbidden tool or unsupported item: 'mcp_tool_call'"])

    def test_a_tool_server_item_must_name_a_declared_server_exactly(self):
        allowed = native.stream_items(LOCAL_SERVER_ONLY)
        for server, shown in ((None, 'None'), (['chem'], "['chem']"), ('', "''"), ('Chem', "'Chem'")):
            item = {key: value for key, value in MCP_ITEM.items() if key != 'server'}
            if server is not None:
                item['server'] = server
            for kind in ('item.started', 'item.updated', 'item.completed'):
                stream = [*NATIVE_STREAM[:2], {'type': kind, 'item': item}, *NATIVE_STREAM[-2:]]
                with self.subTest(server=server, kind=kind):
                    self.assertEqual(transport._events(self.wire(stream), allowed, frozenset({'chem'}))[3],
                                     ['Undeclared tool server: ' + shown])

    def test_audited_events_reads_the_declared_servers(self):
        stream = [*NATIVE_STREAM[:2], {'type': 'item.completed', 'item': MCP_ITEM}, *NATIVE_STREAM[-2:]]
        recorded = json.dumps({'backend': 'codex', 'native': WITH_TOOL_SERVERS})
        self.assertEqual(transport.audited_events(self.archive(stream, recorded)),
                         ('{"ok":true}', {'input_tokens': 20, 'output_tokens': 5}, True, []))
        other = [*stream[:2], {'type': 'item.completed', 'item': {**MCP_ITEM, 'server': 'other'}}, *stream[3:]]
        self.assertEqual(transport.audited_events(self.archive(other, recorded, name='other'))[3],
                         ["Undeclared tool server: 'other'"])
        # Settings that declare only the URL server do not cover a call to chem.
        papers = json.dumps({'backend': 'codex', 'native': URL_SERVER_ONLY})
        self.assertEqual(transport.audited_events(self.archive(stream, papers, name='papers'))[3],
                         ["Undeclared tool server: 'chem'"])

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
        self.addCleanup(self.stop_recorded, marker)
        code = ('import os,subprocess,sys,time;sys.stdin.read();'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True);'
                + record(marker) + 'time.sleep(60)')
        result = self.invoke(code=code, timeout=2, session=self.session())     # real local process, no model
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertTrue(result['descendants_stopped'])
        self.assert_stopped(marker)

    def test_a_call_that_ends_on_its_own_reports_descendants_stopped(self):
        self.assertTrue(self.invoke(frames(), session=self.session())['descendants_stopped'])

    def orphaning_codex(self, marker, *, ends, events=None, status=0):
        """A "Codex" that starts a launcher, which starts a 60-second grandchild in its own session, records it to
        ``marker`` and exits after about 0.5 seconds, orphaning it; "Codex" then runs about 1.5 seconds more, writes
        ``events`` (default a valid stream) and exits with ``status``, if it ``ends``, or never ends. Neither
        descendant holds Codex's pipes."""
        detached = 'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL'
        launcher = ('import os,subprocess,sys,time;'
                    'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True,'
                    + detached + ');' + record(marker) + 'time.sleep(.5)')
        wire = self.wire(frames() if events is None else events)
        rest = ('time.sleep(1.5);sys.stdout.write(%r);sys.stdout.flush();sys.exit(%d)' % (wire, status) if ends
                else 'time.sleep(60)')
        return ('import subprocess,sys,time;sys.stdin.read();'
                'subprocess.Popen([sys.executable,"-c",%r],%s).wait();' % (launcher, detached) + rest)

    def test_a_terminal_call_that_ends_on_its_own_stops_what_it_orphaned(self):
        marker = self.root / 'grandchild.pid'
        self.addCleanup(self.stop_recorded, marker)
        with patch.object(transport, 'TRACK_SECONDS', .1):
            result = self.invoke(code=self.orphaning_codex(marker, ends=True), native=TERMINAL)
        self.assertEqual(result['execution_status'], 'succeeded', result['error'])
        self.assertTrue(result['descendants_stopped'])
        self.assert_stopped(marker)

    def test_a_terminal_call_that_ends_without_a_terminal_event_stops_what_it_orphaned(self):
        # Codex exits on its own with no turn.completed or turn.failed: a cut stream, or a crash.
        for name, events, status in (('no-turn-completed', frames()[:3], 0), ('nonzero-exit', frames()[:2], 3)):
            with self.subTest(case=name):
                marker = self.root / (name + '.pid')
                self.addCleanup(self.stop_recorded, marker)
                with patch.object(transport, 'TRACK_SECONDS', .1):
                    result = self.invoke(code=self.orphaning_codex(marker, ends=True, events=events, status=status),
                                         native=TERMINAL, folder=self.root / 'calls' / name)
                self.assertEqual((result['returncode'], result['terminal_received']), (status, False))
                self.assertTrue(result['descendants_stopped'])
                self.assert_stopped(marker)

    def test_an_interrupted_sweep_still_writes_every_receipt(self):
        # The sweep alone is interrupted; then the call itself was interrupted first, after Codex had exited.
        for name, call, raised in (('sweep', None, 'sweep'), ('call-then-sweep', KeyboardInterrupt('call'), 'call')):
            with self.subTest(case=name):
                folder = self.root / 'calls' / name
                with patch.object(transport, '_sweep', side_effect=KeyboardInterrupt('sweep')), \
                        patch.object(transport, 'read_journal', wraps=transport.read_journal, side_effect=call), \
                        self.assertRaises(KeyboardInterrupt) as caught:
                    self.invoke(native=TERMINAL, folder=folder)
                self.assertEqual(str(caught.exception), raised)
                for receipt in ('transport_result.json', 'invocation.json', 'raw_response.txt', 'response.json',
                                'usage.json'):
                    self.assertTrue((folder / receipt).is_file(), receipt)
                self.assertIs(json.loads((folder / 'transport_result.json').read_text())['descendants_stopped'], False)
                read_journal(folder / 'journal.jsonl')

    def test_a_second_interruption_during_the_kill_still_records_the_first(self):
        # Ctrl-C while Codex runs, then Ctrl-C again while the kill is under way.
        for name, settings in (('off', None), ('terminal', TERMINAL)):
            with self.subTest(settings=name):
                folder = self.root / 'calls' / ('twice-' + name)

                def interrupted_kill(process, tracked=None, record=None):
                    process.kill(); process.wait()
                    raise KeyboardInterrupt('second')
                with patch.object(transport, '_stream_process', side_effect=KeyboardInterrupt('first')), \
                        patch.object(transport, '_terminate', side_effect=interrupted_kill), \
                        self.assertRaises(KeyboardInterrupt) as caught:
                    self.invoke(code='import sys,time;sys.stdin.read();time.sleep(60)', native=settings, folder=folder)
                self.assertEqual(str(caught.exception), 'second')
                result = json.loads((folder / 'transport_result.json').read_text())
                self.assertEqual(result['error'], 'KeyboardInterrupt: first')
                self.assertEqual((result['execution_status'], result['response'], result['usage_complete'],
                                  result['protocol_valid']), ('unknown', None, False, False))
                self.assertIs(result['descendants_stopped'], False)

    def test_a_terminal_call_that_times_out_stops_what_it_orphaned(self):
        marker = self.root / 'grandchild.pid'
        self.addCleanup(self.stop_recorded, marker)
        with patch.object(transport, 'TRACK_SECONDS', .1):
            result = self.invoke(code=self.orphaning_codex(marker, ends=False), native=TERMINAL, timeout=2.5)
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertTrue(result['descendants_stopped'])
        self.assert_stopped(marker)

    def test_a_call_without_the_terminal_never_reads_the_process_table(self):
        for name, settings in (('off', None), ('search', SEARCH)):
            with self.subTest(settings=name):
                marker = self.root / (name + '.pid')
                self.addCleanup(self.stop_recorded, marker)     # Its grandchild is not tracked, so it outlives the call.
                with patch.object(transport, 'TRACK_SECONDS', .1), \
                        patch.object(transport, '_process_table', wraps=transport._process_table) as table:
                    result = self.invoke(code=self.orphaning_codex(marker, ends=True), native=settings,
                                         folder=self.root / 'calls' / name)
                table.assert_not_called()
                self.assertEqual(result['execution_status'], 'succeeded', result['error'])
                self.assertTrue(result['descendants_stopped'])

    def test_a_local_server_alone_tracks_and_stops_what_the_call_orphaned(self):
        marker = self.root / 'server.pid'; self.addCleanup(self.stop_recorded, marker)
        with patch.object(transport, 'TRACK_SECONDS', .1), patch.object(transport, 'codex_version', return_value='0.170.0'), \
                patch.dict(os.environ, {'CHEM_API_KEY': 'invented'}):
            result = self.invoke(code=self.orphaning_codex(marker, ends=True), native=LOCAL_SERVER_ONLY)
        self.assertEqual(result['execution_status'], 'succeeded', result['error'])
        self.assertTrue(result['descendants_stopped'])
        pid = int(marker.read_text().split()[0])
        self.assertIn(pid, transport.read_tracked(self.folder))
        self.assert_stopped(marker)

    def test_a_url_server_starts_no_tracking(self):
        with patch.object(transport, 'TRACK_SECONDS', .1), patch.object(transport, 'codex_version', return_value='0.170.0'), \
                patch.dict(os.environ, {'PAPERS_TOKEN': 'invented'}), \
                patch.object(transport, '_process_table', wraps=transport._process_table) as table:
            result = self.invoke(native=URL_SERVER_ONLY)
        table.assert_not_called()
        self.assertEqual(result['execution_status'], 'succeeded', result['error'])
        self.assertTrue(result['descendants_stopped'])
        self.assertFalse((self.folder / transport.TRACKED_FILE).exists())

    def test_the_tracked_file_is_written_before_a_kill(self):
        marker = self.root / 'server.pid'; self.addCleanup(self.stop_recorded, marker)
        # What the file names at each signal to a process group, and what each write of it held.
        at_kill, written = [], []
        real_killpg, real_write = os.killpg, transport.write_tracked

        def killpg(group, sig):
            at_kill.append(transport.read_tracked(self.folder))
            real_killpg(group, sig)

        def write(folder, tracked):
            written.append(dict(tracked))
            real_write(folder, tracked)
        with patch.object(transport, 'TRACK_SECONDS', .1), patch.object(transport, 'codex_version', return_value='0.170.0'), \
                patch.dict(os.environ, {'CHEM_API_KEY': 'invented'}), \
                patch.object(transport, 'write_tracked', side_effect=write), \
                patch.object(transport.os, 'killpg', side_effect=killpg):
            result = self.invoke(code=self.orphaning_codex(marker, ends=False), native=LOCAL_SERVER_ONLY, timeout=2.5)
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertTrue(result['descendants_stopped'])
        pid, start = marker.read_text().split(' ', 1)
        # Codex's group is the first signalled; the grandchild, with its start time, was in the file by then.
        self.assertEqual(at_kill[0].get(int(pid)), start)
        self.assertEqual(transport.read_tracked(self.folder)[int(pid)], start)
        # Written whenever a sample added a process, not at every sample.
        self.assertTrue(written)
        self.assertTrue(all(earlier != later for earlier, later in zip(written, written[1:])), written)
        self.assertEqual(transport.read_tracked(self.folder), written[-1])
        self.assert_stopped(marker)

    def test_a_tracked_file_that_cannot_be_written_ends_the_call(self):
        marker = self.root / 'server.pid'; self.addCleanup(self.stop_recorded, marker)
        with patch.object(transport, 'TRACK_SECONDS', .1), patch.object(transport, 'codex_version', return_value='0.170.0'), \
                patch.dict(os.environ, {'CHEM_API_KEY': 'invented'}), \
                patch.object(transport, 'write_tracked', side_effect=OSError('No space left on device')):
            result = self.invoke(code=self.orphaning_codex(marker, ends=False), native=LOCAL_SERVER_ONLY, timeout=30)
        # Like a journal that cannot be written: Codex is killed long before its deadline, with what ran under it.
        self.assertEqual((result['execution_status'], result['error']), ('unknown', 'OSError: No space left on device'))
        self.assertLess(result['wall_seconds'], 20)
        self.assertTrue(result['descendants_stopped'])
        self.assertTrue((self.folder / 'transport_result.json').is_file())

    def test_the_tracked_file_holds_only_pids_and_start_times(self):
        folder = self.root / 'tracked'; folder.mkdir()
        self.assertEqual(transport.read_tracked(folder), {})          # Nothing recorded yet.
        transport.write_tracked(folder, {4242: 'Thu Nov 19 10:00:01 2026', 77: 'Thu Nov 19 10:00:00 2026'})
        path = folder / transport.TRACKED_FILE
        self.assertEqual(path.name, 'tracked_processes.json')
        self.assertIn(path.name, transport.ARTIFACTS)
        self.assertEqual(json.loads(path.read_text()), {'processes': [{'pid': 77, 'start': 'Thu Nov 19 10:00:00 2026'},
                                                                      {'pid': 4242, 'start': 'Thu Nov 19 10:00:01 2026'}]})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(transport.read_tracked(folder), {77: 'Thu Nov 19 10:00:00 2026', 4242: 'Thu Nov 19 10:00:01 2026'})
        # A start time that ps printed in another encoding than UTF-8 is read back as it was decoded.
        transport.write_tracked(folder, {77: 'jeu. 19 d\udce9c. 10:00:00 2026'})
        self.assertEqual(transport.read_tracked(folder), {77: 'jeu. 19 d\udce9c. 10:00:00 2026'})
        self.assertEqual(os.listdir(folder), [transport.TRACKED_FILE])
        entry = '{"pid": 77, "start": "s"}'
        malformed = [b'', b'\xff', b'not json', b'[]', b'{"processes": {}}', b'{"processes": [], "pids": []}',
                     b'{"processes": [], "processes": []}', b'{"processes": [7]}', b'{"processes": [{"pid": 77}]}',
                     b'{"processes": [{"pid": "77", "start": "s"}]}', b'{"processes": [{"pid": true, "start": "s"}]}',
                     b'{"processes": [{"pid": 0, "start": "s"}]}', b'{"processes": [{"pid": -1, "start": "s"}]}',
                     b'{"processes": [{"pid": 2147483648, "start": "s"}]}', b'{"processes": [{"pid": 7.0, "start": "s"}]}',
                     b'{"processes": [{"pid": 77, "start": ""}]}', b'{"processes": [{"pid": 77, "start": 1}]}',
                     b'{"processes": [{"pid": 77, "start": "s", "command": "x"}]}',
                     ('{"processes": [%s, %s]}' % (entry, entry)).encode()]
        for raw in malformed:
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    transport.read_tracked(folder)
                # Nothing it names is signalled, or even looked for.
                with patch.object(transport, '_process_table', side_effect=AssertionError('process table read')):
                    self.assertFalse(transport.stop_recorded(folder))
        # Too large, or nested too deep for the JSON reader, it cannot be read either.
        too_large = '{"processes": [%s]}' % ', '.join('{"pid": %d, "start": "Thu Nov 19 10:00:00 2026"}' % pid
                                                       for pid in range(2, 2000))
        self.assertGreater(len(too_large), 65536)
        for name, raw in (('too large', too_large.encode()), ('too deep', b'[' * 60000)):
            with self.subTest(name):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    transport.read_tracked(folder)
                with patch.object(transport, '_process_table', side_effect=AssertionError('process table read')):
                    self.assertFalse(transport.stop_recorded(folder))
        path.unlink()
        # Not a regular file: a symlink, a directory, or a named pipe, which must not hold recovery waiting for a writer.
        (self.root / 'elsewhere.json').write_text('{"processes": []}')
        for name, make in (('symlink', lambda: path.symlink_to(self.root / 'elsewhere.json')),
                           ('dangling symlink', lambda: path.symlink_to(self.root / 'nowhere.json')),
                           ('directory', path.mkdir), ('named pipe', lambda: os.mkfifo(path))):
            with self.subTest(name):
                make()
                with self.assertRaises(ValueError):
                    transport.read_tracked(folder)
                self.assertFalse(transport.stop_recorded(folder))
                path.rmdir() if name == 'directory' else path.unlink()
        # Missing, or naming none, there is nothing to stop.
        with patch.object(transport, '_process_table', side_effect=AssertionError('process table read')):
            self.assertTrue(transport.stop_recorded(folder))
            transport.write_tracked(folder, {})
            self.assertTrue(transport.stop_recorded(folder))

    def test_tracking_follows_codex_and_every_tracked_process_that_is_still_the_same(self):
        # 200 was seen under Codex and still runs, orphaned; 500 was seen too, but its pid now names a later process.
        tracked = {200: 'T1', 500: 'T5'}
        table = {1: (0, 'boot'), 100: (1, 'T0'), 150: (100, 'T6'), 200: (1, 'T1'), 210: (200, 'T7'),
                 211: (210, 'T8'), 500: (1, 'T9'), 510: (500, 'T9'), 600: (1, 'T2')}
        transport._track(tracked, table, 100)
        self.assertEqual(tracked, {200: 'T1', 500: 'T5', 150: 'T6', 210: 'T7', 211: 'T8'})
        # Without a root (Codex reaped, its pid free for reuse) only what is under a tracked process is followed.
        transport._track(tracked, {**table, 300: (150, 'T4'), 160: (100, 'T3')})
        self.assertEqual(tracked, {200: 'T1', 500: 'T5', 150: 'T6', 210: 'T7', 211: 'T8', 300: 'T4'})

    def test_the_process_table_reads_the_same_whatever_the_caller_s_locale_and_time_zone(self):
        # A start time is written by one process and checked by another, perhaps from a shell with other settings.
        starts = []
        for settings in ({'LC_ALL': 'fr_FR.UTF-8', 'TZ': 'EST5'}, {'LC_ALL': 'zh_CN.UTF-8', 'TZ': 'CST-8'},
                         {'LC_ALL': 'C', 'TZ': 'UTC0', 'LANG': 'de_DE.UTF-8', 'LC_TIME': 'ja_JP.UTF-8'}):
            with self.subTest(**settings), patch.dict(os.environ, settings), \
                    patch.object(transport, '_POPEN', wraps=transport._POPEN) as ps:
                starts.append(transport._process_table()[os.getpid()][1])
                self.assertEqual(ps.call_args.kwargs['env'], PS_ENVIRONMENT)
        self.assertEqual(starts, [lstart(os.getpid())] * 3)

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

    def with_tables(self, run, *reads, alive=()):
        """``run()`` with the process table read as ``reads`` (an exception is raised), the last read repeated;
        signals are recorded, not sent, only the pids in ``alive`` answer one, and the clock moves a second per
        reading without waiting. Returns ``run()``'s result and the signals."""
        signals, reads = [], list(reads)

        def read():
            table = reads.pop(0) if len(reads) > 1 else reads[0]
            if isinstance(table, Exception):
                raise table
            return table

        def kill(pid, sig):
            if sig == 0:
                if pid not in alive:
                    raise ProcessLookupError(pid)
            else:
                signals.append(('kill', pid))
        with patch.object(transport, '_process_table', side_effect=read), \
                patch.object(transport.os, 'kill', side_effect=kill), \
                patch.object(transport.os, 'killpg', side_effect=lambda group, sig: signals.append(('killpg', group))), \
                patch.object(transport.os, 'getpgid', side_effect=lambda pid: 200 if pid == 400 else pid), \
                patch.object(transport.time, 'monotonic', side_effect=itertools.count()), \
                patch.object(transport.time, 'sleep'):
            return run(), signals

    def terminate(self, tracked=None):
        """``_terminate`` on Codex pid 100, given what earlier samples ``tracked``."""
        return lambda: transport._terminate(SimpleNamespace(pid=100), tracked)

    # Codex 100 started 200 and 300, each leading its own group, and 200 started 400 in 200's group.
    LISTED = {1: (0, 'boot'), 100: (1, 'T0'), 200: (100, 'T1'), 300: (100, 'T2'), 400: (200, 'T3')}

    def test_a_reused_pid_is_not_signalled_and_counts_as_stopped(self):
        # By the second read 300 has exited and its pid names a bystander that started later and still runs.
        again = {1: (0, 'boot'), 200: (1, 'T1'), 300: (1, 'T9'), 400: (1, 'T3')}
        stopped, signals = self.with_tables(self.terminate(), self.LISTED, again, {1: (0, 'boot'), 300: (1, 'T9')},
                                            alive={300})
        self.assertEqual(signals, [('killpg', 100), ('killpg', 200), ('kill', 400)])
        self.assertTrue(stopped)

    def test_a_failed_second_read_signals_no_descendant(self):
        stopped, signals = self.with_tables(self.terminate(), self.LISTED, OSError('ps unavailable'))
        self.assertEqual(signals, [('killpg', 100)])
        self.assertFalse(stopped)

    # 500, seen under the call at a sample, was orphaned before the kill and leads its own group.
    ORPHANED = {1: (0, 'boot'), 100: (1, 'T0'), 500: (1, 'T5')}

    def test_a_tracked_process_that_survives_the_kill_is_reported(self):
        stopped, signals = self.with_tables(self.terminate({500: 'T5'}), self.ORPHANED, alive={500})
        self.assertEqual(signals, [('killpg', 100), ('killpg', 500)])
        self.assertFalse(stopped)

    def test_a_tracked_process_missing_from_the_confirming_read_is_not_signalled(self):
        # 500 exits between the kill-time listing and the confirming read.
        stopped, signals = self.with_tables(self.terminate({500: 'T5'}), self.ORPHANED, {1: (0, 'boot')})
        self.assertEqual(signals, [('killpg', 100)])
        self.assertTrue(stopped)

    def test_the_end_of_call_sweep_reports_an_unreadable_table_and_signals_nothing(self):
        stopped, signals = self.with_tables(lambda: transport._sweep({500: 'T5'}), OSError('ps unavailable'),
                                            alive={500})
        self.assertEqual(signals, [])
        self.assertFalse(stopped)

    def test_a_call_that_never_read_the_process_table_confirms_nothing_stopped(self):
        # Codex ends on its own, and every look at the process table fails: nothing seen under it is confirmed.
        with patch.object(transport, 'TRACK_SECONDS', .1), \
                patch.object(transport, '_process_table', side_effect=OSError('ps unavailable')) as table:
            result = self.invoke(native=TERMINAL)
        self.assertEqual(result['execution_status'], 'succeeded', result['error'])
        self.assertIs(result['descendants_stopped'], False)
        self.assertGreaterEqual(table.call_count, 2)            # A sample during the call, and the last look.
        self.assertIs(json.loads((self.folder / 'transport_result.json').read_text())['descendants_stopped'], False)
        # Every sample failed but the last look at the end read the table: that read counts.
        sweeping, real_sweep, real_table = [], transport._sweep, transport._process_table

        def sweep(*arguments):
            sweeping.append(True)
            return real_sweep(*arguments)

        def table_at_the_end():
            if not sweeping:
                raise OSError('ps unavailable')
            return real_table()
        with patch.object(transport, 'TRACK_SECONDS', .1), patch.object(transport, '_sweep', side_effect=sweep), \
                patch.object(transport, '_process_table', side_effect=table_at_the_end):
            result = self.invoke(native=TERMINAL, folder=self.root / 'calls' / 'read-at-the-end')
        self.assertEqual((result['execution_status'], sweeping), ('succeeded', [True]))
        self.assertIs(result['descendants_stopped'], True)

    def test_a_sweep_with_nothing_tracked_needs_one_read_of_the_table(self):
        # Nothing was tracked: a sample read the table, so nothing ran under the call; else the last look must.
        stopped, signals = self.with_tables(lambda: transport._sweep({}, table_read=False), OSError('ps unavailable'))
        self.assertEqual((stopped, signals), (False, []))
        stopped, signals = self.with_tables(lambda: transport._sweep({}, table_read=False), self.ORPHANED)
        self.assertEqual((stopped, signals), (True, []))
        with patch.object(transport, '_process_table', side_effect=AssertionError('process table read')):
            self.assertTrue(transport._sweep({}, table_read=True))

    def test_what_a_kill_or_a_sweep_finds_is_recorded_before_any_signal(self):
        recorded = []

        def record(tracked):
            # What is recorded, and whether a signal had been sent by then (a signal 0 only asks if a pid is in use).
            sent = transport.os.killpg.called or any(call.args[1] for call in transport.os.kill.call_args_list)
            recorded.append((dict(tracked), sent))
        stopped, signals = self.with_tables(lambda: transport._terminate(SimpleNamespace(pid=100), {}, record),
                                            self.LISTED)
        self.assertEqual(recorded, [({200: 'T1', 300: 'T2', 400: 'T3'}, False)])
        self.assertEqual((signals[0], stopped), (('killpg', 100), True))
        # The sweep: 500, tracked at a sample, still runs and has started 510 since.
        recorded.clear()
        stopped, signals = self.with_tables(lambda: transport._sweep({500: 'T5'}, record),
                                            {**self.ORPHANED, 510: (500, 'T6')})
        self.assertEqual(recorded, [({500: 'T5', 510: 'T6'}, False)])
        self.assertEqual((signals, stopped), ([('killpg', 500), ('killpg', 510)], True))
        # A kill or a sweep that finds nothing new records nothing again.
        recorded.clear()
        self.with_tables(lambda: transport._terminate(SimpleNamespace(pid=100), {500: 'T5'}, record), self.ORPHANED)
        self.with_tables(lambda: transport._sweep({500: 'T5'}, record), self.ORPHANED)
        self.assertEqual(recorded, [])

    def test_a_record_that_fails_does_not_hold_back_a_kill_or_a_sweep(self):
        for failure in (OSError('No space left on device'), TypeError('not JSON serializable'), RuntimeError('bug')):
            with self.subTest(failure=failure):
                def record(tracked):
                    raise failure
                stopped, signals = self.with_tables(lambda: transport._terminate(SimpleNamespace(pid=100), {}, record),
                                                    self.LISTED)
                self.assertEqual((signals, stopped),
                                 ([('killpg', 100), ('killpg', 200), ('killpg', 300), ('kill', 400)], True))
                stopped, signals = self.with_tables(lambda: transport._sweep({500: 'T5'}, record),
                                                    {**self.ORPHANED, 510: (500, 'T6')})
                self.assertEqual((signals, stopped), ([('killpg', 500), ('killpg', 510)], True))

    def test_recovery_never_signals_this_process_its_ancestors_its_group_or_its_session(self):
        # This process runs under a parent and a grandparent; 700 is a server the call orphaned. Fake pids are far
        # above any pid this machine gives out. Nothing here sends a real signal.
        me, parent, grandparent = os.getpid(), 2_000_001, 2_000_002
        group, session = os.getpgrp(), os.getsid(0)
        table = {1: (0, 'boot'), grandparent: (1, 'TG'), parent: (grandparent, 'TP'), me: (parent, 'TM'),
                 2_000_500: (1, 'T5'), 2_000_600: (1, 'T6'), 2_000_700: (1, 'T7')}
        folder = self.root / 'recovered'; folder.mkdir()

        def stop(tracked, read=table):
            transport.write_tracked(folder, tracked)

            def run():
                # 2_000_500 is in this process's group, 2_000_600 in its session; every other process leads its own.
                with patch.object(transport.os, 'getpgid', side_effect=lambda pid: group if pid == 2_000_500 else pid), \
                        patch.object(transport.os, 'getsid',
                                     side_effect=lambda pid: session if pid in (0, 2_000_600) else pid):
                    return transport.stop_recorded(folder)
            return self.with_tables(run, read)
        refused = {'this process': {me: 'TM'}, 'pid 1': {1: 'boot'}, 'its parent': {parent: 'TP'},
                   'an ancestor': {grandparent: 'TG'}, 'a member of its group': {2_000_500: 'T5'},
                   'a member of its session': {2_000_600: 'T6'},
                   # Recorded with another start time, the parent's pid is refused all the same.
                   'its parent, reused': {parent: 'T0'}}
        for case, tracked in refused.items():
            with self.subTest(case):
                self.assertEqual(stop(tracked), (False, []))
        # A process table that does not show this process says nothing of its ancestors: nothing is signalled.
        self.assertEqual(stop({2_000_700: 'T7'}, read={1: (0, 'boot'), 2_000_700: (1, 'T7')}), (False, []))
        # Beside such a pid the call's own processes are still stopped, but the call is not confirmed stopped.
        self.assertEqual(stop({me: 'TM', 2_000_700: 'T7'}), (False, [('killpg', 2_000_700)]))
        self.assertEqual(stop({2_000_700: 'T7'}), (True, [('killpg', 2_000_700)]))


if __name__ == '__main__':
    unittest.main()
