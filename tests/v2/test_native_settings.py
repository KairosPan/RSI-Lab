"""The native_capabilities block of a run config: read, normalised and checked before any call."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from rsi_lab import native
from rsi_lab.__main__ import main
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from tests.v2.liveness_support import BUDGETS
from rsi_lab.store import digest
from tests.v2.native_fixtures import (CHECK_PY, CHEM, PAPERS, PLAIN, PLOT_CHECK, R, ROLES, SEARCH, SKILL_MD, TERMINAL,
                                      URL_SERVER_ONLY, VIEW_IMAGE, WITH_ROLES, WITH_SKILLS, WITH_TOOL_SERVERS, here,
                                      skill_folder)

INVALID = {'lab': {'shell': True}}
EXAMPLE = Path(__file__).resolve().parents[2] / 'examples' / 'native-capabilities-run.json'
# The run config of tests/v2/liveness_support.open_run, which also starts a Runtime; these tests start their own.
BASE = {'execution_mode': 'replay', 'goal': 'Native settings fixture', 'authorized_sources': ['src'],
        'open_review_interval_batches': 1, 'max_open_reviews': 4, 'wait_timeout_seconds': 600}


class NativeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='rsi-native-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run_root, self.codex_home, self.data = self.root / 'run', self.root / 'codex', self.root / 'data'
        for folder in (self.run_root, self.codex_home, self.data):
            folder.mkdir()
        self.missing = self.root / 'missing'
        self.a_file = self.root / 'file.txt'
        self.a_file.write_text('not a directory')

    def initialise(self, run_id, block):
        """A ledger holding run ``run_id`` whose config carries ``block`` as its native_capabilities."""
        store = Store(self.root / 'ledger')
        self.addCleanup(store.close)
        Controller(store).initialize(run_id, max_external=1, budgets=BUDGETS,
                                     config={**BASE, 'native_capabilities': block})
        return store

    def test_absent_block_and_absent_role_are_off(self):
        self.assertEqual(native.capabilities({}), {role: native.OFF for role in native.ROLES})
        got = native.capabilities({'native_capabilities': {'lab': {'web_search': True}}})
        self.assertEqual(got['lab'], {'web_search': True, 'terminal': None, 'subagents': None})
        self.assertEqual(got['meta'], native.OFF)
        self.assertFalse(native.any_on(None)); self.assertFalse(native.any_on(native.OFF))

    def test_true_and_object_forms_normalise(self):
        got = native.capabilities({'native_capabilities': {'lab': {
            'terminal': True, 'subagents': {'max_concurrent': 8}}}})['lab']
        self.assertEqual(got['terminal'], {'readable_roots': []})
        self.assertEqual(got['subagents'], {'max_concurrent': 8})
        self.assertEqual(native.capabilities({'native_capabilities': {'meta': {'subagents': True}}})['meta']['subagents'],
                         {'max_concurrent': 3})

    def test_empty_objects_take_the_defaults_and_false_is_off(self):
        got = native.capabilities({'native_capabilities': {'lab': {'terminal': {}, 'subagents': {}},
            'meta': {'web_search': False, 'terminal': False, 'subagents': False}}})
        self.assertEqual(got['lab'], {'web_search': False, 'terminal': {'readable_roots': []},
                                      'subagents': {'max_concurrent': 3}})
        self.assertEqual(got['meta'], native.OFF)
        self.assertTrue(native.any_on(got['lab']))
        for settings in ({**native.OFF, 'web_search': True}, {**native.OFF, 'terminal': {'readable_roots': []}},
                         {**native.OFF, 'subagents': {'max_concurrent': 1}}):
            with self.subTest(settings=settings):
                self.assertTrue(native.any_on(settings))

    def test_each_role_gets_its_own_object(self):
        got = native.capabilities({})
        got['lab']['web_search'] = True
        self.assertEqual(native.OFF, {'web_search': False, 'terminal': None, 'subagents': None})
        self.assertEqual(got['meta'], native.OFF)
        self.assertIsNot(got['meta'], got['literature'])

    def readable_roots(self, *roots):
        """The Lab's readable roots as capabilities stores them."""
        block = {'lab': {'terminal': {'readable_roots': [str(root) for root in roots]}}}
        return native.capabilities({'native_capabilities': block})['lab']['terminal']['readable_roots']

    def check_lab_roots(self, *roots):
        """check_roots on settings whose Lab terminal may read ``roots``, everything else off."""
        by_role = {**{role: native.OFF for role in native.ROLES},
                   'lab': {'web_search': False, 'terminal': {'readable_roots': [str(r) for r in roots]}, 'subagents': None}}
        native.check_roots(by_role, run_root=self.run_root, codex_home=self.codex_home)

    def test_readable_roots_are_stored_resolved(self):
        link = self.root / 'link-to-data'
        link.symlink_to(self.data, target_is_directory=True)
        for root in (link, os.path.join(self.data, '..', 'data')):
            with self.subTest(root=root):
                self.assertEqual(self.readable_roots(root), [str(self.data.resolve())])

    def test_a_readable_root_listed_twice_is_kept_once_in_first_place(self):
        other = self.root / 'other'
        other.mkdir()
        link = self.root / 'link-to-data'
        link.symlink_to(self.data, target_is_directory=True)
        data, other = str(self.data.resolve()), str(other.resolve())
        self.assertEqual(self.readable_roots(self.data, other, self.data), [data, other])
        self.assertEqual(self.readable_roots(other, link, self.data), [other, data])

    def test_a_symlink_into_the_run_root_is_refused(self):
        (self.run_root / 'objects').mkdir()
        link = self.root / 'into-run'
        link.symlink_to(self.run_root / 'objects', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '^native_capabilities.* the run root'):
            self.check_lab_roots(link)

    def test_a_differently_cased_codex_home_is_refused_on_a_case_insensitive_volume(self):
        upper = self.codex_home.with_name(self.codex_home.name.upper())
        if not upper.exists():
            self.skipTest('the temporary directory is on a case-sensitive volume')
        (self.codex_home / 'sessions').mkdir()
        for root in (upper, upper / 'sessions'):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, '^native_capabilities.* the Codex home'):
                self.check_lab_roots(root)
        self.check_lab_roots(self.root)          # Containing the Codex home stays allowed.

    def test_a_readable_root_may_not_be_a_shared_temp_folder(self):
        shared = self.root / 'shared-tmp'
        (shared / 'data').mkdir(parents=True)
        link = self.root / 'link-to-shared-tmp'
        link.symlink_to(shared, target_is_directory=True)
        with mock.patch.object(native, 'SHARED_TEMP_FOLDERS', (str(self.missing), str(shared))):
            for root in (shared, link):
                with self.subTest(root=root), self.assertRaisesRegex(
                        ValueError, '^native_capabilities.*shared temporary folder.*' + str(root)):
                    self.check_lab_roots(root)
            self.check_lab_roots(shared / 'data')     # Inside it stays allowed: the more specific entry wins.
        if os.path.isdir('/tmp'):                     # On macOS a configured /tmp is stored as /private/tmp.
            for root in ('/tmp', str(Path('/tmp').resolve())):
                with self.subTest(root=root), self.assertRaisesRegex(ValueError, '^native_capabilities'):
                    self.check_lab_roots(root)

    def test_invalid_blocks_are_refused(self):
        for block in ([], {'planner': {}}, {'lab': []}, {'lab': {'shell': True}}, {'lab': {'web_search': 1}},
                      {'lab': {'terminal': {'network': True}}}, {'lab': {'terminal': {'readable_roots': ['relative']}}},
                      {'lab': {'terminal': {'readable_roots': [str(self.missing)]}}},
                      {'lab': {'terminal': {'readable_roots': [str(self.a_file)]}}},
                      {'lab': {'subagents': {'max_concurrent': 0}}}, {'lab': {'subagents': {'max_concurrent': 9}}},
                      {'lab': {'subagents': {'max_concurrent': True}}}):
            with self.subTest(block=block), self.assertRaisesRegex(ValueError, '^native_capabilities'):
                native.capabilities({'native_capabilities': block})

    def test_other_invalid_values_are_refused(self):
        for block in (None, {'lab': None}, {'lab': {'terminal': None}}, {'lab': {'terminal': 'on'}},
                      {'lab': {'terminal': {'readable_roots': str(self.data)}}},
                      {'lab': {'terminal': {'readable_roots': [7]}}}, {'lab': {'subagents': 3}},
                      {'lab': {'subagents': {'max_concurrent': 2.0}}}, {'lab': {'subagents': {'depth': 1}}}):
            with self.subTest(block=block), self.assertRaisesRegex(ValueError, '^native_capabilities'):
                native.capabilities({'native_capabilities': block})

    def test_a_readable_root_may_not_expose_the_run_or_the_codex_home(self):
        for root in (self.run_root, self.run_root / 'objects', self.codex_home, self.codex_home / 'sessions'):
            by_role = {**{r: native.OFF for r in native.ROLES},
                       'lab': {'web_search': False, 'terminal': {'readable_roots': [str(root)]}, 'subagents': None}}
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, '^native_capabilities'):
                native.check_roots(by_role, run_root=self.run_root, codex_home=self.codex_home)
        # A root that merely contains the run root is allowed: the deny entry of Task 2 covers it.
        native.check_roots({**{r: native.OFF for r in native.ROLES}, 'lab': {'web_search': False,
            'terminal': {'readable_roots': [str(self.run_root.parent)]}, 'subagents': None}},
            run_root=self.run_root, codex_home=self.codex_home)

    def test_validate_checks_the_normal_form_without_the_filesystem(self):
        self.assertEqual(native.validate(None), native.OFF)
        self.assertIsNot(native.validate(None), native.OFF)
        settings = {'web_search': True, 'terminal': {'readable_roots': ['/no/such/dir']}, 'subagents': {'max_concurrent': 8}}
        self.assertEqual(native.validate(settings), settings)
        for bad in ([], {}, {'web_search': True}, {**native.OFF, 'extra': 1}, {**native.OFF, 'web_search': 1},
                    {**native.OFF, 'terminal': True}, {**native.OFF, 'terminal': {}},
                    {**native.OFF, 'terminal': {'readable_roots': ['relative']}},
                    {**native.OFF, 'terminal': {'readable_roots': []}, 'extra': 1},
                    {**native.OFF, 'subagents': True}, {**native.OFF, 'subagents': {}},
                    {**native.OFF, 'subagents': {'max_concurrent': 9}},
                    {**native.OFF, 'subagents': {'max_concurrent': True}}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, '^native_capabilities'):
                native.validate(bad)

    def test_runtime_refuses_an_invalid_block_at_start(self):
        store = self.initialise('r', INVALID)
        with self.assertRaisesRegex(ValueError, '^native_capabilities'):
            Runtime(store, 'r')

    def test_runtime_holds_the_settings_of_each_role(self):
        store = self.initialise('r', {'meta': {'web_search': True}})
        self.assertEqual(Runtime(store, 'r').native, {**{role: native.OFF for role in native.ROLES},
                                                      'meta': {**native.OFF, 'web_search': True}})

    def test_runtime_refuses_a_readable_root_inside_its_run(self):
        store = self.initialise('r', {'lab': {'terminal': {'readable_roots': [str(self.root / 'ledger')]}}})
        with self.assertRaisesRegex(ValueError, r'^native_capabilities\.lab\.terminal\.readable_roots .* the run root'):
            Runtime(store, 'r')

    def test_init_refuses_an_invalid_block_before_creating_the_run(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps({'budgets': BUDGETS, 'native_capabilities': INVALID}))
        ledger = os.path.join(self.root, 'ledger')
        with self.assertRaisesRegex(ValueError, '^native_capabilities'):
            main(['--root', ledger, '--run', 'r', 'init', '--config', str(path)])
        store = Store(ledger)
        self.addCleanup(store.close)
        with self.assertRaises(KeyError):
            store.run('r')

    def test_init_refuses_a_readable_root_in_its_run_or_the_codex_home_before_creating_the_run(self):
        (self.codex_home / 'sessions').mkdir()
        ledger_a, ledger_b = self.root / 'ledger-a', self.root / 'ledger-b'
        for ledger, root, place in ((ledger_a, ledger_a, 'the run root'),
                                    (ledger_b, self.codex_home / 'sessions', 'the Codex home')):
            path = self.root / (ledger.name + '.json')
            path.write_text(json.dumps({'budgets': BUDGETS,
                                        'native_capabilities': {'lab': {'terminal': {'readable_roots': [str(root)]}}}}))
            with self.subTest(place=place), mock.patch.dict(os.environ, {'CODEX_HOME': str(self.codex_home)}):
                with self.assertRaisesRegex(ValueError, '^native_capabilities.* ' + place):
                    main(['--root', str(ledger), '--run', 'r', 'init', '--config', str(path)])
                store = Store(ledger)
                self.addCleanup(store.close)
                with self.assertRaises(KeyError):
                    store.run('r')

    def test_the_example_config_is_valid(self):
        config = json.loads(EXAMPLE.read_text())
        by_role = native.capabilities(config)
        self.assertTrue(native.any_on(by_role['lab'])); self.assertTrue(native.any_on(by_role['meta']))
        for role in ('lab', 'meta'):
            self.assertEqual(by_role[role], {'web_search': True, 'terminal': {'readable_roots': []},
                                             'subagents': {'max_concurrent': 3}}, role)
        self.assertEqual(by_role['literature'], {'web_search': True, 'terminal': None, 'subagents': None})
        self.assertEqual(config['model_timeout_seconds'], 1800)
        self.assertEqual(config['max_effective_context_bytes'], 4194304)
        self.assertEqual(config['model'], 'YOUR_MODEL_ID')
        # Budgets a native run does not exhaust in a couple of tool-using turns; a call may run its whole timeout.
        self.assertEqual(config['budgets']['tokens']['limit'], 20000000)
        self.assertEqual(config['budgets']['wall_seconds']['limit'], 36000)
        self.assertEqual(config['model_reservation'], {'model_calls': 1, 'tokens': 200000,
                                                       'wall_seconds': config['model_timeout_seconds']})

    def test_init_accepts_the_example_and_features_reports_its_settings(self):
        ledger = str(self.root / 'ledger')
        with contextlib.redirect_stdout(io.StringIO()):
            main(['--root', ledger, '--run', 'r', 'init', '--config', str(EXAMPLE)])
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            main(['--root', ledger, '--run', 'r', 'features'])
        self.assertEqual(json.loads(printed.getvalue())['native_capabilities'],
                         native.capabilities(json.loads(EXAMPLE.read_text())))


def lab(subagents):
    """The Lab's settings from a run config whose Lab block sets only ``subagents``."""
    return native.capabilities({'native_capabilities': {'lab': {'subagents': subagents}}})['lab']


class RoleSettingsTests(unittest.TestCase):
    """Named sub-agent roles: their normal form, their limits, the role file, the command and the prompt."""

    def test_roles_reach_the_normal_form_in_name_order(self):
        got = lab({'roles': {'local': R, 'challenge': ROLES['challenge']}})
        self.assertEqual(got['subagents'], {'max_concurrent': 3, 'roles': ROLES})
        self.assertEqual(list(got['subagents']['roles']), ['challenge', 'local'])

    def test_no_roles_leaves_the_settings_as_they_are_today(self):          # Review Focus 1
        for given in (True, {'max_concurrent': 3}, {'roles': {}}):
            self.assertEqual(lab(given)['subagents'], {'max_concurrent': 3})
        old = {'web_search': False, 'terminal': None, 'subagents': {'max_concurrent': 2}}
        self.assertEqual(native.validate(old), old)

    def test_declared_roles_are_read_from_normal_form_settings(self):
        self.assertEqual(native.declared_roles(WITH_ROLES), ROLES)
        for settings in (PLAIN, native.OFF, {**native.OFF, 'web_search': True}):
            with self.subTest(settings=settings):
                self.assertEqual(native.declared_roles(settings), {})

    def test_validate_keeps_roles_in_name_order(self):
        given = {**PLAIN, 'subagents': {'max_concurrent': 3, 'roles': {'local': R, 'challenge': ROLES['challenge']}}}
        self.assertEqual(native.validate(given), WITH_ROLES)
        self.assertEqual(list(native.validate(given)['subagents']['roles']), ['challenge', 'local'])
        self.assertEqual(native.validate({**PLAIN, 'subagents': {'max_concurrent': 3, 'roles': {}}}), PLAIN)
        with self.assertRaisesRegex(ValueError, r'^native_capabilities\.subagents\.roles'):
            native.validate({**PLAIN, 'subagents': {'max_concurrent': 3, 'roles': {'default': R}}})

    def test_roles_at_the_limits_are_accepted(self):
        widest = {'description': 'd' * 300, 'instructions': 'i' * 8000}
        roles = {'a' * 32: widest, 'b': R, 'c_-9': R, **{'r' + str(i): R for i in range(5)}}
        self.assertEqual(len(roles), native.MAX_ROLES)
        self.assertEqual(lab({'roles': roles})['subagents']['roles'], dict(sorted(roles.items())))

    def test_bad_roles_are_refused(self):
        bad = [{'Local': R}, {'default': R}, {'explorer': R}, {'worker': R}, {'a' * 33: R}, {'1x': R}, {'x.y': R},
               {'x': {**R, 'description': ''}}, {'x': {**R, 'description': 'a\nb'}},
               {'x': {**R, 'description': 'd' * 301}}, {'x': {**R, 'instructions': ' \n'}},
               {'x': {**R, 'instructions': 'i' * 8001}}, {'x': {'description': 'd'}}, {'x': {**R, 'model': 'm'}},
               {'r' + str(i): R for i in range(9)}, ['local'], {'x': 'text'},
               {'x': {**R, 'description': 'a\rb'}}, {'x': {**R, 'description': ' '}}, {'x\n': R},
               {'x': {**R, 'instructions': 7}},
               {'max_depth': R}, {'default_subagent_model': R}, {'enabled': R}, {'interrupt_message': R}]
        for roles in bad:
            with self.subTest(roles=roles), self.assertRaisesRegex(
                    ValueError, r'^native_capabilities\.lab\.subagents\.roles'):
                lab({'roles': roles})

    def test_role_file_survives_toml(self):                                  # Review Focus 2
        import tomllib
        for name, role in ROLES.items():
            text = native.role_file_text(name, role)
            self.assertEqual(tomllib.loads(text),
                             {'name': name, 'description': role['description'],
                              'developer_instructions': role['instructions']})
            self.assertEqual(len(text.splitlines()), 3)
            self.assertTrue(text.endswith('\n'))

    def test_command_names_each_role_after_the_agent_limits(self):
        config = native.command_parts(WITH_ROLES, workspace='/w', run_root='/r', codex_home='/h', model='m',
                                      effort='low', roles_dir='/r/agents/s1/roles')['config']
        self.assertEqual(config, [
            'agents.max_depth=1', 'agents.max_concurrent_threads_per_session=3',
            'agents.default_subagent_model="m"', 'agents.default_subagent_reasoning_effort="low"',
            'agents.challenge.description="Argues against the \\"leading\\" series."',
            'agents.challenge.config_file="/r/agents/s1/roles/challenge.toml"',
            'agents.local.description="Advances the leading series."',
            'agents.local.config_file="/r/agents/s1/roles/local.toml"'])

    def test_command_without_roles_is_unchanged(self):
        self.assertEqual(native.command_parts(PLAIN, workspace='/w', run_root='/r', codex_home='/h',
                                              model='m', effort='low')['config'],
                         ['agents.max_depth=1', 'agents.max_concurrent_threads_per_session=3',
                          'agents.default_subagent_model="m"', 'agents.default_subagent_reasoning_effort="low"'])

    def test_roles_need_a_roles_folder(self):
        with self.assertRaisesRegex(ValueError, 'roles_dir'):
            native.command_parts(WITH_ROLES, workspace='/w', run_root='/r', codex_home='/h', model='m', effort='low')

    def test_prompt_paragraph_names_the_roles(self):
        self.assertIn('Sub-agent roles declared for this session, started with agent_type: '
                      'challenge (Argues against the "leading" series.); local (Advances the leading series.). '
                      'Start any other helper with agent_type default. ', native.instruction(WITH_ROLES))
        self.assertNotIn('Sub-agent roles declared', native.instruction(PLAIN))
        # With the terminal on too, the roles sentence stays the paragraph's last.
        text = native.instruction({**WITH_ROLES, 'terminal': {'readable_roots': []}})
        self.assertIn('The terminal has no network access', text)
        self.assertTrue(text.endswith('Start any other helper with agent_type default. '))

    def test_receipt_form_names_roles_by_digest_and_leaves_plain_settings_alone(self):
        self.assertEqual(native.receipt_form(WITH_ROLES)['subagents']['roles']['local'],
                         {'description': 'Advances the leading series.',
                          'instructions_sha256': hashlib.sha256(b'Refine.').hexdigest()})
        self.assertEqual(native.receipt_form(PLAIN), PLAIN)
        self.assertEqual(WITH_ROLES['subagents']['roles'], ROLES)   # The settings given are not changed.


def lab_from(entry):
    """The Lab's settings from a run config whose Lab block is ``entry``."""
    return native.capabilities({'native_capabilities': {'lab': entry}})['lab']


class ToolServerSettingsTests(unittest.TestCase):
    """Tool servers and image viewing: their normal form, their limits, the secret names and the receipt form."""

    def test_servers_reach_the_normal_form_in_name_order(self):
        got = lab_from({'web_search': True, 'tool_servers': {'papers': PAPERS, 'chem': here(CHEM)}})
        self.assertEqual(got, {**SEARCH, 'tool_servers': {'chem': here(CHEM), 'papers': PAPERS}})
        self.assertEqual(list(got['tool_servers']), ['chem', 'papers'])
        self.assertEqual(lab_from({'tool_servers': {'a': {'description': 'd', 'command': sys.executable}}})
                         ['tool_servers']['a'],
                         {'description': 'd', 'command': sys.executable, 'args': [], 'env': {}, 'env_from': []})
        self.assertEqual(lab_from({'tool_servers': {'b': {'description': 'd', 'url': 'http://[::1]:8080/mcp'}}})
                         ['tool_servers']['b'],
                         {'description': 'd', 'url': 'http://[::1]:8080/mcp', 'headers': {}})

    def test_keys_that_are_off_leave_the_settings_as_today(self):                  # Review Focus 3
        for extra in ({}, {'view_image': False}, {'tool_servers': {}}):
            self.assertEqual(lab_from({'terminal': True, **extra}), TERMINAL)
        for settings in (SEARCH, TERMINAL, PLAIN, WITH_ROLES):
            self.assertEqual(native.validate(settings), settings)
        for settings in (SEARCH, TERMINAL, PLAIN):
            self.assertEqual(native.receipt_form(settings), settings)

    def test_image_viewing_needs_the_terminal(self):
        self.assertEqual(lab_from({'terminal': True, 'view_image': True}), VIEW_IMAGE)
        for entry in ({'view_image': True}, {'web_search': True, 'view_image': True},
                      {'terminal': True, 'view_image': 1}):
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, '^native_capabilities.lab.view_image'):
                lab_from(entry)

    def test_bad_servers_are_refused(self):
        ok, url = {'description': 'd', 'command': sys.executable}, {'description': 'd', 'url': PAPERS['url']}
        bad = [{'Chem': ok}, {'codex_x': ok}, {'1x': ok}, {'a' * 33: ok}, {'s%d' % i: ok for i in range(9)}, ['chem'],
               {'x': {**ok, 'url': PAPERS['url']}}, {'x': {'description': 'd'}}, {'x': {**ok, 'command': 'python'}},
               {'x': {**ok, 'command': '/nonexistent/server'}}, {'x': {**ok, 'args': '-m x'}},
               {'x': {**ok, 'env': {'A': 1}}},
               {'x': {**ok, 'env': {'1A': 'v'}}}, {'x': {**ok, 'env_from': ['A', 'A']}},
               {'x': {**ok, 'env': {'A': 'v'}, 'env_from': ['A']}}, {'x': {**ok, 'headers': {}}},
               {'x': {**url, 'env': {}}},
               {'x': {**ok, 'description': ''}}, {'x': {**ok, 'description': 'a\nb'}},
               {'x': {**ok, 'description': 'd' * 301}},
               {'x': {**ok, 'tool_timeout_seconds': 0}}, {'x': {**ok, 'tool_timeout_seconds': 3601}},
               {'x': {**ok, 'tool_timeout_seconds': True}}, {'x': {**url, 'url': 'http://papers.example.org/mcp'}},
               {'x': {**url, 'url': 'ftp://localhost/mcp'}},
               {'x': {**url, 'url': 'https://user:pw@papers.example.org/mcp'}},
               {'x': {**url, 'url': 'https:///mcp'}}, {'x': {**url, 'headers': {'Authorization': 'Bearer t'}}},
               {'x': {**url, 'headers': {'authorization': 't'}}}, {'x': {**url, 'headers': {'X-A': 'a\nb'}}},
               {'x': {**url, 'token_from': 'not a name'}},
               # Spellings an older urlsplit reads as another host than Codex does: refused on every interpreter.
               {'x': {**url, 'url': 'http://evil.example\\[::1]/mcp'}},
               {'x': {**url, 'url': 'https://papers.example.org\\mcp'}},
               {'x': {**url, 'url': 'http://evil.example[::1]/mcp'}}, {'x': {**url, 'url': 'http://[localhost]/mcp'}}]
        for servers in bad:
            with self.subTest(servers=servers), self.assertRaisesRegex(ValueError,
                                                                      '^native_capabilities.lab.tool_servers'):
                lab_from({'tool_servers': servers})

    def test_loopback_http_and_the_limits_are_accepted(self):
        for host in ('localhost', '127.0.0.1', '[::1]'):
            lab_from({'tool_servers': {'x': {'description': 'd', 'url': 'http://' + host + ':8080/mcp'}}})
        for address in ('http://[::1]/mcp', 'http://[::1]', 'https://[2001:db8::1]:8443/mcp'):
            lab_from({'tool_servers': {'x': {'description': 'd', 'url': address}}})
        lab_from({'tool_servers': {'s%d' % i: {'description': 'd' * 300, 'command': sys.executable,
                                               'tool_timeout_seconds': 3600} for i in range(8)}})

    def test_validate_checks_the_shape_only(self):
        self.assertEqual(native.validate(WITH_TOOL_SERVERS), WITH_TOOL_SERVERS)       # the command need not exist
        self.assertEqual(native.validate(VIEW_IMAGE), VIEW_IMAGE)
        self.assertTrue(native.any_on(URL_SERVER_ONLY))
        with self.assertRaises(ValueError):
            native.validate({**SEARCH, 'view_image': True})

    def test_secret_names_and_local_servers(self):
        self.assertEqual((native.tool_servers(SEARCH), native.local_servers(WITH_TOOL_SERVERS)), ({}, ['chem']))
        self.assertEqual(native.secret_variables(WITH_TOOL_SERVERS), ['CHEM_API_KEY', 'PAPERS_TOKEN'])
        self.assertEqual(native.unset_variables(WITH_TOOL_SERVERS, {'CHEM_API_KEY': 'x', 'PAPERS_TOKEN': ''}),
                         ['PAPERS_TOKEN'])

    def test_receipt_form_keeps_env_names_not_values(self):
        got = native.receipt_form(WITH_TOOL_SERVERS)['tool_servers']
        self.assertEqual(got['chem'], {'description': CHEM['description'], 'command': CHEM['command'],
                                       'args': CHEM['args'], 'env_names': ['CHEM_MODE'], 'env_from': ['CHEM_API_KEY'],
                                       'tool_timeout_seconds': 300})
        self.assertEqual(got['papers'], PAPERS)


def skill_text(name, description='Checks a plot before it is reported.'):
    """The SKILL.md of a skill whose front matter gives ``name`` and the line ``description: <description>``."""
    return ('---\nname: ' + name + '\ndescription: ' + description + '\n---\nUse it.\n').encode()


def add_skill(root, name, text=None):
    """A skill folder ``name`` in ``root`` holding only SKILL.md: ``text``, else ``skill_text(name)``."""
    (root / name).mkdir()
    (root / name / 'SKILL.md').write_bytes(skill_text(name) if text is None else text)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class SkillSettingsTests(unittest.TestCase):
    """A declared skills folder: how it is read and frozen into the run, its limits, its normal form and receipt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rsi-skills-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def lab_skills(self, root):
        """The Lab's skills as capabilities reads them once a Lab block with the terminal and the skills folder
        ``root``, given as is, is frozen."""
        block = native.freeze_skills({'lab': {'terminal': True, 'skills': {'root': root}}})
        return native.capabilities({'native_capabilities': block})['lab']['skills']

    def test_freezing_stores_every_file_and_records_its_hash(self):
        root, stored = skill_folder(self.tmp / 'skills'), {}
        def put(raw):
            stored[hashlib.sha256(raw).hexdigest()] = raw
            return hashlib.sha256(raw).hexdigest()
        block = native.freeze_skills({'lab': {'terminal': True, 'skills': {'root': str(root)}}}, put)
        self.assertEqual(block['lab']['skills'], {'root': str(root.resolve()), 'frozen': {'plot-check': PLOT_CHECK}})
        self.assertEqual(sorted(stored.values()), sorted([SKILL_MD, CHECK_PY]))
        self.assertEqual(native.capabilities({'native_capabilities': block})['lab'],
                         {**TERMINAL, 'skills': block['lab']['skills']})
        self.assertEqual(list(block['lab']['skills']['frozen']['plot-check']['files']), ['SKILL.md', 'scripts/check.py'])

    def test_freezing_leaves_everything_else_as_given(self):
        root = str(skill_folder(self.tmp / 'skills'))
        for block in ([], None, 'lab'):
            with self.subTest(block=block), self.assertRaisesRegex(
                    ValueError, '^native_capabilities must be an object keyed by lab, meta or literature$'):
                native.freeze_skills(block)
        # Malformed entries pass through unchanged, for capabilities to refuse.
        others = {'lab': {'terminal': True}, 'meta': {'skills': 'on'}, 'literature': [],
                  'planner': {'skills': {'root': root}}}
        self.assertEqual(native.freeze_skills(others), others)
        for skills in ('on', {}, {'root': root, 'extra': 1}):
            block = native.freeze_skills({'lab': {'terminal': True, 'skills': skills}})
            self.assertEqual(block, {'lab': {'terminal': True, 'skills': skills}})
            with self.subTest(skills=skills), self.assertRaisesRegex(ValueError, '^native_capabilities.lab.skills'):
                native.capabilities({'native_capabilities': block})
        given = {'lab': {'terminal': True, 'skills': {'root': root}}, 'meta': {'web_search': True}}
        frozen = native.freeze_skills(given)
        self.assertEqual(given, {'lab': {'terminal': True, 'skills': {'root': root}}, 'meta': {'web_search': True}})
        self.assertEqual(frozen['meta'], {'web_search': True})

    def test_later_changes_to_the_folder_do_not_reach_the_run(self):
        root, stored = skill_folder(self.tmp / 'skills'), []
        def put(raw):
            stored.append(raw)
            return hashlib.sha256(raw).hexdigest()
        block = native.freeze_skills({'lab': {'terminal': True, 'skills': {'root': str(root)}}})
        settings = native.capabilities({'native_capabilities': block})
        # An unchanged folder freezes to the same entry, and its files are stored again for the new run.
        self.assertEqual(native.freeze_skills(block, put), block)
        self.assertEqual(sorted(stored), sorted([SKILL_MD, CHECK_PY]))
        (root / 'plot-check' / 'scripts' / 'check.py').write_bytes(b'print("changed")\n')
        self.assertEqual(native.capabilities({'native_capabilities': block}), settings)
        with self.assertRaisesRegex(ValueError, '^native_capabilities.lab.skills.*changed since it was frozen'):
            native.freeze_skills(block)
        shutil.rmtree(root)
        self.assertEqual(native.capabilities({'native_capabilities': block}), settings)

    def test_the_kernel_refuses_an_unfrozen_entry_and_skills_without_a_terminal(self):
        root = str(skill_folder(self.tmp / 'skills'))
        with self.assertRaisesRegex(ValueError, '^native_capabilities.lab.skills.*must be frozen with the run'):
            lab_from({'terminal': True, 'skills': {'root': root}})
        with self.assertRaisesRegex(ValueError, '^native_capabilities.skills.*must be frozen with the run'):
            native.validate({**TERMINAL, 'skills': {'root': root}})
        block = native.freeze_skills({'lab': {'web_search': True, 'skills': {'root': root}}})
        with self.assertRaisesRegex(ValueError, '^native_capabilities.lab.skills'):
            native.capabilities({'native_capabilities': block})
        with self.assertRaisesRegex(ValueError, '^native_capabilities.skills'):
            native.validate({**SEARCH, 'skills': WITH_SKILLS['skills']})

    def test_folder_limits_refuse_the_run(self):
        room = native.MAX_SKILL_BYTES - len(SKILL_MD) - len(CHECK_PY)
        elsewhere = self.tmp / 'elsewhere'
        add_skill(self.tmp, 'elsewhere')
        def plot(root, name='SKILL.md'):
            return root / 'plot-check' / name
        def front(old, new):
            return lambda root: plot(root).write_bytes(SKILL_MD.replace(old, new))
        def link_file(root):
            (root / 'plot-check' / 'scripts' / 'check.py').unlink()
            (root / 'plot-check' / 'scripts' / 'check.py').symlink_to(elsewhere / 'SKILL.md')
        def only_hidden(root):
            shutil.rmtree(root / 'plot-check')
            (root / '.DS_Store').write_bytes(b'x')
        def nested_skill(root):
            (root / 'plot-check' / 'examples' / 'x').mkdir(parents=True)
            (root / 'plot-check' / 'examples' / 'x' / 'SKILL.md').write_bytes(skill_text('x'))
        cases = {
            'an empty folder': lambda root: shutil.rmtree(root / 'plot-check'),
            'only hidden entries': only_hidden,
            '17 skills': lambda root: [add_skill(root, 's%02d' % i) for i in range(16)],
            '17 files in one skill': lambda root: [plot(root, 'f%02d' % i).write_bytes(b'x') for i in range(15)],
            '262,145 bytes in one skill': lambda root: plot(root, 'data.bin').write_bytes(b'x' * (room + 1)),
            'a folder named Plot': lambda root: add_skill(root, 'Plot'),
            'a 33-character name': lambda root: add_skill(root, 'a' * 33),
            'another name in the front matter': front(b'name: plot-check', b'name: plot'),
            'no name in the front matter': front(b'name: plot-check\n', b''),
            'a name given twice': front(b'name: plot-check\n', b'name: plot-check\nname: plot-check\n'),
            'no SKILL.md': lambda root: plot(root).unlink(),
            'a SKILL.md below the top of a skill': nested_skill,
            'no front matter': lambda root: plot(root).write_bytes(b'Open the plot.\n'),
            'unclosed front matter': lambda root: plot(root).write_bytes(SKILL_MD.replace(b'\n---\nOpen', b'\nOpen')),
            'SKILL.md not UTF-8': front(b'reported.', b'reported \xff.'),
            'a blank description': front(b'Checks a plot before it is reported.', b'   '),
            'an empty quoted description': front(b'Checks a plot before it is reported.', b'""'),
            'no description': front(b'description: Checks a plot before it is reported.\n', b''),
            'block text': front(b'Checks a plot before it is reported.', b'>\n  Checks a plot.'),
            'literal block text': front(b'Checks a plot before it is reported.', b'|'),
            'a 301-character description': front(b'Checks a plot before it is reported.', b'd' * 301),
            'a symlinked file': link_file,
            'a symlinked skill folder': lambda root: (root / 'elsewhere').symlink_to(elsewhere, target_is_directory=True),
            'a regular file at the top level': lambda root: (root / 'README.md').write_bytes(b'Skills.\n'),
            'a named pipe in a skill': lambda root: os.mkfifo(plot(root, 'pipe')),
        }
        for index, (case, change) in enumerate(cases.items()):
            root = skill_folder(self.tmp / ('case-%02d' % index))
            change(root)
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, '^native_capabilities.lab.skills'):
                self.lab_skills(str(root))
        # A refused folder stores nothing, not even the skills read before the one refused.
        root, stored = skill_folder(self.tmp / 'partly'), []
        add_skill(root, 'a-first')
        front(b'Checks a plot before it is reported.', b'>')(root)
        with self.assertRaisesRegex(ValueError, '^native_capabilities.lab.skills.plot-check'):
            native.freeze_skills({'lab': {'terminal': True, 'skills': {'root': str(root)}}},
                                 lambda raw: stored.append(raw) or sha(raw))
        self.assertEqual(stored, [])
        for root in ('research-skills', str(self.tmp / 'missing'), str(elsewhere / 'SKILL.md'), 7):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, '^native_capabilities.lab.skills.root'):
                self.lab_skills(root)

    def test_the_limits_themselves_are_accepted(self):
        root = skill_folder(self.tmp / 'skills')
        for name in ['s%02d' % i for i in range(12)] + ['a' * 32, 'b_-9']:
            add_skill(root, name)
        add_skill(root, 'quoted', skill_text('quoted', '"Checks: axes."'))
        (root / 'b_-9' / 'SKILL.md').write_bytes(skill_text('b_-9', "'" + 'd' * 300 + "'"))
        plot = root / 'plot-check'
        for i in range(13):
            (plot / ('f%02d' % i)).write_bytes(b'x')
        (plot / 'data.bin').write_bytes(b'x' * (native.MAX_SKILL_BYTES - len(SKILL_MD) - len(CHECK_PY) - 13))
        for hidden in (root / '.DS_Store', plot / '.DS_Store', plot / 'scripts' / '.cache'):
            hidden.write_bytes(b'\0' * native.MAX_SKILL_BYTES)
        (plot / '.git').mkdir()
        (plot / '.git' / 'config').write_bytes(b'[core]\n')
        frozen = self.lab_skills(str(root))['frozen']
        self.assertEqual(len(frozen), native.MAX_SKILLS)
        self.assertEqual(list(frozen), sorted(frozen))
        self.assertEqual(frozen['quoted']['description'], 'Checks: axes.')
        self.assertEqual(frozen['b_-9']['description'], 'd' * 300)
        files = frozen['plot-check']['files']
        self.assertEqual(len(files), native.MAX_SKILL_FILES)
        self.assertEqual(list(files), sorted(files))
        self.assertEqual(files['SKILL.md'], PLOT_CHECK['files']['SKILL.md'])
        self.assertFalse([path for path in files if '.DS_Store' in path or '.git' in path or '.cache' in path])
        self.assertEqual(native.read_skills(str(root))['plot-check']['files']['scripts/check.py'], CHECK_PY)

    def test_front_matter_values_yaml_could_read_otherwise_are_refused(self):
        # Codex reads the front matter as YAML: each of these would give it another name or description, or none.
        descriptions = [
            'Checks: axes.', 'Checks axes # soon', 'Checks axes\t# soon', 'Checks axes:',     # a key, a comment
            *[mark + 'Checks axes' for mark in '[]{}*&!@%`#,'],                              # reserved first characters
            '- Checks axes', '? Checks axes', ': Checks axes', '-',                           # a sequence, a key
            '"Checks axes', "'Checks axes", '"Checks" axes',                                  # an unbalanced quote
            '"Checks \\ axes"', '"Checks \\"x\\" axes"', '"Checks "x" axes"', "'It''s a check'",  # escapes in quotes
            '42', '1.5e3', '0x1F', '.inf', 'true', 'False', 'null', '~',                      # not text
            'Checks a plot\n  before it is reported.',                                        # continued on a line
            'Checks a plot\n\n\tbefore it is reported.']
        for index, description in enumerate(descriptions):
            root = skill_folder(self.tmp / ('case-%02d' % index))
            (root / 'plot-check' / 'SKILL.md').write_bytes(skill_text('plot-check', description))
            with self.subTest(description=description), self.assertRaisesRegex(
                    ValueError, r'^native_capabilities\.lab\.skills\.plot-check\.SKILL\.md front matter'):
                self.lab_skills(str(root))
        root = self.tmp / 'null'
        root.mkdir()
        add_skill(root, 'null')                                                    # name: null is YAML's null
        with self.assertRaisesRegex(ValueError, r'^native_capabilities\.lab\.skills\.null\.SKILL\.md front matter'):
            self.lab_skills(str(root))

    def test_front_matter_values_yaml_reads_as_written_are_accepted(self):
        root = skill_folder(self.tmp / 'skills')
        expected = {'"Checks: axes."': 'Checks: axes.', "'Checks # axes'": 'Checks # axes',
                    "'Checks \\ axes'": 'Checks \\ axes', '"null"': 'null', 'Checks a plot, then its axes.': None,
                    "It's a check of C# code, ratio 1:2, x - y, 100% sure.": None, 'v1.5 of the check': None}
        for index, description in enumerate(expected):
            add_skill(root, 's%02d' % index, skill_text('s%02d' % index, description))
        add_skill(root, 'null', skill_text('"null"'))
        add_skill(root, 'with-metadata', b'---\nname: with-metadata\ndescription: Checks a plot.\nmetadata:\n'
                                         b'  short-description: "Plots: checked"\n\nlicense: MIT\n---\nUse it.\n')
        frozen = self.lab_skills(str(root))['frozen']
        for index, (description, read) in enumerate(expected.items()):
            self.assertEqual(frozen['s%02d' % index]['description'], read or description)
        self.assertEqual(frozen['null']['description'], PLOT_CHECK['description'])
        self.assertEqual(frozen['with-metadata']['description'], 'Checks a plot.')

    def test_the_frozen_form_is_checked_without_the_filesystem(self):
        self.assertEqual(native.validate(WITH_SKILLS), WITH_SKILLS)          # The root need not exist.
        other = {'description': 'Another.', 'files': {'scripts/z.py': sha(b'z'), 'SKILL.md': sha(b'a')}}
        got = native.validate({**TERMINAL, 'skills': {'root': '/data/research-skills',
                                                      'frozen': {'z-skill': other, 'plot-check': PLOT_CHECK}}})
        self.assertEqual(list(got['skills']['frozen']), ['plot-check', 'z-skill'])
        self.assertEqual(list(got['skills']['frozen']['z-skill']['files']), ['SKILL.md', 'scripts/z.py'])
        def with_skill(**skill):
            return {'root': '/data/research-skills', 'frozen': {'plot-check': {**PLOT_CHECK, **skill}}}
        def with_files(**files):
            return with_skill(files={**PLOT_CHECK['files'], **files})
        bad = ['on', {}, {'frozen': WITH_SKILLS['skills']['frozen']}, {**WITH_SKILLS['skills'], 'extra': 1},
               {**WITH_SKILLS['skills'], 'root': 'research-skills'}, {**WITH_SKILLS['skills'], 'root': 7},
               {'root': '/data/research-skills', 'frozen': {}}, {'root': '/data/research-skills', 'frozen': []},
               {'root': '/data/research-skills', 'frozen': {'s%02d' % i: PLOT_CHECK for i in range(17)}},
               {'root': '/data/research-skills', 'frozen': {'Plot': PLOT_CHECK}},
               {'root': '/data/research-skills', 'frozen': {'a' * 33: PLOT_CHECK}},
               {'root': '/data/research-skills', 'frozen': {'-x': PLOT_CHECK}},
               {'root': '/data/research-skills', 'frozen': {'plot-check': 'skill'}},
               {'root': '/data/research-skills', 'frozen': {'plot-check': {'files': PLOT_CHECK['files']}}},
               with_skill(extra=1), with_skill(description=''), with_skill(description='a\nb'),
               with_skill(description='> folded'), with_skill(description='| literal'),
               with_skill(description='d' * 301), with_skill(description=7),
               with_skill(files={}), with_skill(files=[]), with_skill(files={'scripts/check.py': sha(CHECK_PY)}),
               with_files(**{'f%02d' % i: sha(b'x') for i in range(15)}),
               with_files(**{'examples/x/SKILL.md': sha(b'x')}),
               *[with_files(**{path: sha(b'x')}) for path in ('../x', '/abs', 'a//b', './x', '.hidden', 'a/.git/x',
                                                              'a/', 'a/..', '')],
               with_files(**{'x': sha(b'x').upper()}), with_files(**{'x': sha(b'x')[:63]}), with_files(**{'x': 7})]
        for skills in bad:
            with self.subTest(skills=skills), self.assertRaisesRegex(ValueError, '^native_capabilities.skills'):
                native.validate({**TERMINAL, 'skills': skills})

    def test_declared_skills_are_read_from_normal_form_settings(self):
        self.assertEqual(native.declared_skills(WITH_SKILLS), {'plot-check': PLOT_CHECK})
        for settings in (TERMINAL, native.OFF, VIEW_IMAGE, WITH_TOOL_SERVERS):
            with self.subTest(settings=settings):
                self.assertEqual(native.declared_skills(settings), {})

    def test_init_freezes_the_folder_into_the_run(self):
        root = skill_folder(self.tmp / 'skills')
        home, codex_home = self.tmp / 'home', self.tmp / 'codex-home'
        home.mkdir()
        codex_home.mkdir()
        path = self.tmp / 'config.json'
        path.write_text(json.dumps({'budgets': BUDGETS, 'native_capabilities': {
            'lab': {'terminal': True, 'skills': {'root': str(root)}}}}))
        ledger = str(self.tmp / 'ledger')
        printed = io.StringIO()
        with mock.patch.dict(os.environ, {'HOME': str(home), 'CODEX_HOME': str(codex_home)}):
            with contextlib.redirect_stdout(io.StringIO()):
                main(['--root', ledger, '--run', 'r', 'init', '--config', str(path)])
            (root / 'plot-check' / 'scripts' / 'check.py').write_bytes(b'print("changed")\n')
            with contextlib.redirect_stdout(printed):
                main(['--root', ledger, '--run', 'r', 'features'])
        store = Store(ledger)
        self.addCleanup(store.close)
        skills = store.run('r')['config']['native_capabilities']['lab']['skills']
        self.assertEqual(skills, {'root': str(root.resolve()), 'frozen': {'plot-check': PLOT_CHECK}})
        for name, raw in (('SKILL.md', SKILL_MD), ('scripts/check.py', CHECK_PY)):
            self.assertEqual(store.read_blob(PLOT_CHECK['files'][name]), raw)
        self.assertEqual(json.loads(printed.getvalue())['native_capabilities']['lab'], {**TERMINAL, 'skills': skills})

    def test_receipt_form_names_skills_by_hash(self):
        self.assertEqual(native.receipt_form(WITH_SKILLS)['skills'], {'plot-check': digest(PLOT_CHECK['files'])})
        self.assertEqual(native.receipt_form(WITH_SKILLS)['terminal'], TERMINAL['terminal'])
        self.assertEqual(WITH_SKILLS['skills']['frozen'], {'plot-check': PLOT_CHECK})   # The settings given are kept.


if __name__ == '__main__':
    unittest.main()
