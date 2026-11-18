"""The native_capabilities block of a run config: read, normalised and checked before any call."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from rsi_lab import native
from rsi_lab.__main__ import main
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from tests.v2.liveness_support import BUDGETS

INVALID = {'lab': {'shell': True}}
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


if __name__ == '__main__':
    unittest.main()
