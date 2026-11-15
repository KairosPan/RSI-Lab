import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from rsi_literature.catalog import CORE_PROVIDERS, OPTIONAL_PROVIDERS, DEFAULT_ENABLED_PROVIDERS, PROVIDERS, provider_catalog


class CatalogTests(unittest.TestCase):
    def catalog(self, config=None, env=None):
        # Configuration inspection must not import optional SDKs or make calls.
        with patch('rsi_literature.catalog.importlib.util.find_spec', return_value=None):
            return {row['provider']: row for row in provider_catalog(config, env={} if env is None else env)}

    def test_default_catalog_keeps_nine_core_and_six_optional_with_explicit_defaults_enabled(self):
        rows = self.catalog()
        self.assertEqual(set(rows), set(PROVIDERS))
        self.assertEqual(len(rows), 15)
        self.assertEqual({key for key, row in rows.items() if row['enabled']}, set(DEFAULT_ENABLED_PROVIDERS))
        self.assertEqual(len(DEFAULT_ENABLED_PROVIDERS), 6)
        self.assertEqual(len(CORE_PROVIDERS), 9)
        self.assertEqual(len(OPTIONAL_PROVIDERS), 6)
        self.assertIn('poppler', DEFAULT_ENABLED_PROVIDERS)
        self.assertEqual({key for key, row in rows.items() if row['tier'] == 'optional'}, set(OPTIONAL_PROVIDERS))
        self.assertTrue(all(rows[key]['status'] == 'disabled' for key in OPTIONAL_PROVIDERS))
        self.assertTrue(all(row['live_verified'] is False for row in rows.values()))

    def test_poppler_readiness_uses_host_executables_without_model_or_credentials(self):
        with patch('rsi_literature.catalog.shutil.which', side_effect=lambda name: '/host/bin/' + name):
            row = self.catalog()['poppler']
        self.assertEqual(row['status'], 'configured')
        self.assertEqual(row['missing'], [])
        self.assertFalse(row['live_verified'])
        self.assertEqual(row['operations'], ['build_reading_package', 'render_document_page'])
        self.assertEqual([key for key, spec in PROVIDERS.items() if 'render_document_page' in spec['operations']], ['poppler'])
        with patch('rsi_literature.catalog.shutil.which', return_value=None):
            row = self.catalog()['poppler']
        self.assertEqual(row['status'], 'needs_configuration')
        self.assertEqual(set(row['missing']), {'executable:pdftotext_path', 'executable:pdfinfo_path', 'executable:pdftoppm_path'})

    def test_poppler_explicit_paths_must_be_executable_and_cannot_be_masked_by_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'host-poppler'
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o700)
            settings = {key: str(executable) for key in ('pdftotext_path', 'pdfinfo_path', 'pdftoppm_path')}
            config = {'enabled_providers': ['poppler'], 'connectors': {'poppler': settings}}
            self.assertEqual(self.catalog(config)['poppler']['status'], 'configured')
            settings['pdftoppm_path'] = str(Path(directory) / 'not-installed')
            self.assertEqual(self.catalog(config)['poppler']['missing'], ['executable:pdftoppm_path'])
            settings['pdftoppm_path'] = []
            self.assertEqual(self.catalog(config)['poppler']['missing'], ['executable:pdftoppm_path'])
            settings['pdftoppm_path'] = str(executable)
            executable.chmod(0o600)
            self.assertEqual(len(self.catalog(config)['poppler']['missing']), 3)

    def test_example_configs_enable_poppler_and_leave_optional_or_unconfigured_services_off(self):
        project = Path(__file__).resolve().parents[2]
        for filename in ('literature-tools.json', 'literature-tools-optional.json'):
            with self.subTest(filename=filename):
                config = json.loads((project / 'examples' / filename).read_text())
                self.assertEqual(set(config['enabled_providers']), set(DEFAULT_ENABLED_PROVIDERS))
                self.assertFalse(set(config['enabled_providers']) & set(OPTIONAL_PROVIDERS))
                self.assertNotIn('mineru', config['connectors'])
                self.assertTrue(set(config['connectors']) <= set(PROVIDERS))
                self.assertEqual(config['connectors']['poppler']['pdftoppm_path'], 'pdftoppm')

    def test_absent_optional_credentials_do_not_disable_public_operations(self):
        rows = self.catalog({'enabled_providers': list(PROVIDERS)})
        for provider in ('europe_pmc', 'pubmed', 'openalex', 'semantic_scholar', 'asta'):
            with self.subTest(provider=provider):
                self.assertEqual(rows[provider]['status'], 'configured')
                self.assertEqual(rows[provider]['missing'], [])
        self.assertEqual(rows['unpaywall']['status'], 'needs_configuration')
        self.assertIn('environment:UNPAYWALL_EMAIL', rows['unpaywall']['missing'])
        self.assertIn('environment:SCITE_ACCESS_TOKEN', rows['scite']['missing'])

    def test_environment_allowlist_is_not_a_list_of_required_credentials(self):
        rows = self.catalog({'enabled_providers': ['langextract'], 'connectors': {'langextract': {
            'python': sys.executable, 'model': 'host-model',
            'env_names': ['OPTIONAL_API_KEY', 'UNSET_CACHE_LOCATION']}}})
        self.assertEqual(rows['langextract']['status'], 'configured')
        self.assertEqual(rows['langextract']['missing'], [])

    def test_disabled_providers_remain_visible_without_claiming_callability(self):
        rows = self.catalog({'enabled_providers': ['europe_pmc']})
        self.assertEqual(len(rows), 15)
        self.assertTrue(rows['europe_pmc']['enabled'])
        self.assertEqual(rows['europe_pmc']['status'], 'configured')
        self.assertFalse(rows['pubmed']['enabled'])
        self.assertEqual(rows['pubmed']['status'], 'disabled')
        self.assertEqual(rows['pubmed']['missing'], [])
        self.assertFalse(rows['scite']['enabled'])
        self.assertEqual(rows['scite']['status'], 'disabled')
        self.assertIn('environment:SCITE_ACCESS_TOKEN', rows['scite']['missing'])

    def test_explicit_empty_enabled_list_disables_every_provider(self):
        rows = self.catalog({'enabled_providers': []})
        self.assertTrue(all(not row['enabled'] and row['status'] == 'disabled' for row in rows.values()))

    def test_starling_model_is_required_by_its_execution_contract(self):
        settings = {'command': [sys.executable], 'backend': 'host-corpus'}
        rows = self.catalog({'enabled_providers': ['starling'], 'connectors': {'starling': settings}})
        self.assertEqual(rows['starling']['status'], 'needs_configuration')
        self.assertIn('configuration:model', rows['starling']['missing'])
        rows = self.catalog({'enabled_providers': ['starling'], 'connectors': {'starling': {**settings, 'model': 'host-model'}}})
        self.assertEqual(rows['starling']['status'], 'configured')

    def test_native_credentials_cannot_be_renamed_only_in_the_catalog(self):
        config = {'enabled_providers': ['unpaywall'], 'connectors': {'unpaywall': {'credential_env': 'CUSTOM_UNPAYWALL_EMAIL'}}}
        rows = self.catalog(config, env={'CUSTOM_UNPAYWALL_EMAIL': 'contact@example.org'})
        self.assertEqual(rows['unpaywall']['status'], 'needs_configuration')
        self.assertIn('environment:UNPAYWALL_EMAIL', rows['unpaywall']['missing'])
        rows = self.catalog(config, env={'UNPAYWALL_EMAIL': 'contact@example.org'})
        self.assertEqual(rows['unpaywall']['status'], 'configured')
        # MCP and hosted-service bindings actually support credential_env.
        rows = self.catalog({'enabled_providers': ['scite'], 'connectors': {'scite': {'credential_env': 'CUSTOM_SCITE_TOKEN'}}},
                            env={'CUSTOM_SCITE_TOKEN': 'fixture-token'})
        self.assertEqual(rows['scite']['status'], 'configured')

    def test_host_configuration_rejects_unsupported_native_credential_bindings(self):
        from rsi_literature import make_adapters
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Native.*standard environment names'):
                make_adapters(directory, {'connectors': {'unpaywall': {'credential_env': 'CUSTOM_UNPAYWALL_EMAIL'}}})

    def test_removed_providers_cannot_be_reenabled_or_configured(self):
        from rsi_literature import make_adapters
        removed = ['tooluniverse', 'llm_nerre', 'llm_organic_synthesis', 'openscholar', 'paperpilot', 'mineru']
        with tempfile.TemporaryDirectory() as directory:
            for provider in removed:
                with self.subTest(provider=provider):
                    self.assertNotIn(provider, PROVIDERS)
                    with self.assertRaises(ValueError):
                        make_adapters(directory, {'enabled_providers': ['europe_pmc', provider]})
                    with self.assertRaises(ValueError):
                        make_adapters(directory, {'connectors': {provider: {}}})


if __name__ == '__main__':
    unittest.main()
