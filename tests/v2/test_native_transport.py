"""The Codex command and transport call built from one role's native settings.

The fake provider is a local Python process that writes the Codex event
protocol; no model, network request or credential is used.
"""
import base64
import json
from pathlib import Path
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

    def invoke(self, *, native=None, run_root=None, folder=None):
        wire = ''.join(json.dumps(event) + '\n' for event in frames())
        code = 'import sys; sys.stdin.read(); sys.stdout.write(' + repr(wire) + '); sys.stdout.flush()'

        def fake(command, **kwargs):
            self.commands.append(command)
            return self.real_popen([sys.executable, '-u', '-c', code], **kwargs)
        with patch.object(transport.subprocess, 'Popen', side_effect=fake):
            return transport.invoke('Inspect this task', {'type': 'object'}, folder or self.folder, 'm', 'low',
                                    timeout=10, session=self.session(), journal_metadata={'effect_id': 'effect'},
                                    native=native, run_root=run_root or str(self.root))

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
        command = self.command({'web_search': False, 'terminal': {'readable_roots': ['/data']}, 'subagents': None})
        self.assertNotIn('--sandbox', command)
        self.assertNotIn('shell_tool', command); self.assertNotIn('unified_exec', command)
        self.assertIn('default_permissions="rsi_agent"', command)
        table = tomllib.loads('t = ' + self.config_value(command, 'permissions.rsi_agent.filesystem='))['t']
        self.assertEqual(table, {':minimal': 'read', '/r': 'deny', '/home/.codex': 'deny', '/r/agents/s/workspace': 'write',
                                 '/r/agents/s/workspace/.codex': 'read', '/r/agents/s/workspace/AGENTS.md': 'read',
                                 '/r/agents/s/workspace/AGENTS.override.md': 'read', '/data': 'read'})
        env = tomllib.loads('t = ' + self.config_value(command, 'shell_environment_policy.set='))['t']
        self.assertEqual(env, {'TMPDIR': '/r/agents/s/workspace/.tmp'})
        for feature in ('view_image', 'apps', 'plugins', 'browser_use', 'computer_use', 'multi_agent', 'multi_agent_v2'):
            self.assertEqual(command[command.index(feature) - 1], '--disable')

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


if __name__ == '__main__':
    unittest.main()
