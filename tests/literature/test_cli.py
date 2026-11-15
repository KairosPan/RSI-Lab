"""CLI registration and local literature reading, with networking forbidden."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.__main__ import main
from rsi_lab.store import Store
from rsi_literature.adapters import OPERATIONS
from rsi_literature.catalog import PROVIDERS, DEFAULT_ENABLED_PROVIDERS


class LiteratureCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rsi-literature-cli-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = self.root / 'ledger'
        for target in ('socket.create_connection', 'socket.socket.connect',
                       'rsi_literature.remote.http_request', 'rsi_literature.providers.http_transport'):
            guard = patch(target, side_effect=AssertionError('CLI configuration/local reading must not use the network'))
            guard.start()
            self.addCleanup(guard.stop)

    def json_file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return str(path)

    def cli(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['--root', str(self.ledger), '--run', 'cli-test', *arguments])
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_tool_catalog_works_before_initialization_without_registering_or_dispatching(self):
        with patch('rsi_lab.__main__.Controller.initialize', side_effect=AssertionError('Catalog cannot initialize a run')):
            result = self.cli('literature-tools')
        self.assertTrue(result['configuration_check_only'])
        self.assertEqual(result['registered_capabilities'], [])
        self.assertEqual({row['provider'] for row in result['providers']}, set(PROVIDERS))
        self.assertTrue(all(row['live_verified'] is False for row in result['providers']))
        store = Store(self.ledger)
        try:
            with self.assertRaises(KeyError):
                store.run('cli-test')
        finally:
            store.close()

    def test_flags_register_only_callable_operations_and_combining_them_does_not_duplicate(self):
        config = self.json_file('literature.json', {'default_provider': 'pubmed'})
        expected = {'literature.' + name for name in OPERATIONS if name not in {'run_review', 'extract_evidence'}}
        self.assertEqual(len(expected), 11)
        for flags in (['--literature-tools'], ['--literature-config', config],
                      ['--literature-tools', '--literature-config', config]):
            with self.subTest(flags=flags):
                result = self.cli(*flags, 'literature-tools')
                self.assertEqual(set(result['registered_capabilities']), expected)
                self.assertEqual(len(result['registered_capabilities']), 11)

    def test_shipped_profiles_keep_credential_backends_closed_and_only_expose_callable_tools(self):
        examples = Path(__file__).resolve().parents[2] / 'examples'
        for filename, extra in [('literature-tools.json', set()),
                                ('literature-tools-optional.json', set())]:
            result = self.cli('--literature-config', str(examples / filename), 'literature-tools')
            enabled = {row['provider'] for row in result['providers'] if row['enabled']}
            self.assertEqual(enabled, set(DEFAULT_ENABLED_PROVIDERS) | extra)
            self.assertNotIn('literature.run_review', result['registered_capabilities'])
            self.assertNotIn('literature.extract_evidence', result['registered_capabilities'])
            self.assertTrue({'scite', 'elicit', 'edison', 'paperqa', 'starling', 'langextract',
                             'semantic_scholar', 'unpaywall', 'grobid'}.isdisjoint(enabled))
        expanded = self.json_file('explicit-paperqa.json', {
            'enabled_providers': list(DEFAULT_ENABLED_PROVIDERS) + ['paperqa']})
        result = self.cli('--literature-config', expanded, 'literature-tools')
        self.assertIn('literature.run_review', result['registered_capabilities'])

    def test_init_and_adapter_run_acquire_then_read_registered_local_source(self):
        text = 'The assay used a matched control.\n\nThe outcome was a small response.'
        source = self.root / 'paper.txt'
        source.write_text(text, encoding='utf-8')
        literature = self.json_file('literature.json', {
            'local_sources': {'paper': {'path': str(source), 'content_scope': 'excerpt'}},
            'connectors': {'edison': {'credential_env': 'RSI_CLI_TEST_EDISON_KEY'}},
        })
        run_config = self.json_file('run.json', {'goal': 'Read the registered assay description',
            'budgets': {'tool_calls': {'limit': 10, 'hard': True},
                        'model_calls': {'limit': 0, 'hard': True}}})
        flags = ['--literature-config', literature]
        secret = 'must-never-enter-cli-descriptor-or-receipt'
        with patch.dict(os.environ, {'RSI_CLI_TEST_EDISON_KEY': secret}):
            self.cli(*flags, 'init', '--config', run_config)
            acquired = self.cli(*flags, 'adapter-run', '--adapter', 'literature.acquire_fulltext',
                '--inputs', self.json_file('acquire.json', {'source_id': 'paper'}), '--event', 'acquire-local')
            self.assertEqual(acquired['effect']['status'], 'succeeded')
            self.assertEqual(acquired['result']['status'], 'ok')
            self.assertNotIn(text, json.dumps(acquired), 'Acquiring bytes does not deliver original prose')
            document_id = acquired['result']['data']['document_id']
            read = self.cli(*flags, 'adapter-run', '--adapter', 'literature.read_document',
                '--inputs', self.json_file('read.json', {'document_id': document_id, 'max_bytes': 1024}),
                '--event', 'read-local')
        self.assertEqual(read['effect']['status'], 'succeeded')
        self.assertEqual(read['result']['status'], 'ok')
        self.assertEqual(read['result']['data']['text'], text)
        self.assertNotIn(secret, json.dumps(read))
        store = Store(self.ledger)
        try:
            self.assertEqual(len(store.list('cli-test', 'effect')), 2)
            self.assertEqual(store.get('cli-test', 'budget', 'model_calls')['spent'], 0)
            self.assertEqual(store.get('cli-test', 'budget', 'tool_calls')['spent'], 2)
            passages = [row for row in store.list('cli-test', 'fact')
                        if row['provenance'].get('delivery_kind') == 'source_excerpt']
            self.assertTrue(passages)
            self.assertTrue(all(row['provenance']['document_id'] == document_id for row in passages))
            # Descriptor defaults contain host configuration and environment
            # variable names, never values read from the process environment.
            for blob in store.blobs.iterdir():
                self.assertNotIn(secret.encode(), blob.read_bytes())
            store.verify_history('cli-test')
        finally:
            store.close()

    def test_reading_run_profile_has_separate_text_history_and_image_budgets(self):
        examples = Path(__file__).resolve().parents[2] / 'examples'
        self.cli('--literature-config', str(examples / 'literature-tools.json'),
                 'init', '--config', str(examples / 'literature-reading-run.json'))
        store = Store(self.ledger)
        try:
            config = store.run('cli-test')['config']
            self.assertEqual(config['max_input_bytes'], 131072)
            self.assertEqual(config['max_effective_context_bytes'], 524288)
            self.assertEqual(config['max_image_bytes'], 4194304)
            self.assertEqual(config['max_effective_image_bytes'], 16777216)
            self.assertEqual(store.list('cli-test', 'effect'), [])
            self.assertEqual(store.get('cli-test', 'budget', 'model_calls')['spent'], 0)
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
