"""Fake SDK contract checks; never import paid providers or perform live calls."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rsi_literature.documents import DocumentStore
from rsi_literature.sdk_tools import run_sdk


@dataclass
class Interval:
    start_pos: int
    end_pos: int


@dataclass
class Extraction:
    extraction_class: str
    extraction_text: str
    attributes: dict
    char_interval: object = None


@dataclass
class Example:
    text: str
    extractions: list


@dataclass
class Annotated:
    text: str
    extractions: list


class SDKTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DocumentStore(Path(self.temp.name) / 'documents')
        self.manifest = self.store.ingest(b'Matched control was used in the assay.', 'text/plain',
            {'paper_id': 'paper-1', 'content_scope': 'abstract'})
        self.did = self.manifest['document_id']
        self.calls = []

    def langextract(self, interval=True, failure=False):
        def extract(**kwargs):
            self.calls.append(kwargs)
            if failure:
                raise TimeoutError('Must not serialize API-key secret')
            extraction = Extraction('control', 'Matched control', {}, Interval(0, 15) if interval else None)
            return Annotated(kwargs['text_or_documents'], [extraction])
        return SimpleNamespace(extract=extract, data=SimpleNamespace(Extraction=Extraction, ExampleData=Example))

    def lx_inputs(self, **extra):
        return {'document_ids': [self.did], 'prompt': 'Extract control descriptions.',
            'examples': [{'text': 'Control A was used.', 'extractions': [
                {'extraction_class': 'control', 'extraction_text': 'Control A', 'attributes': {}}]}], **extra}

    def run_fake(self, provider, module, settings, inputs):
        with patch('rsi_literature.sdk_tools.importlib.import_module', return_value=module):
            return run_sdk(provider, settings, inputs, self.store, 'effect-test')

    def test_langextract_uses_host_model_and_verified_source_coordinates(self):
        out = self.run_fake('langextract', self.langextract(), {'model': 'host-local-model'}, self.lx_inputs())
        self.assertEqual(out['status'], 'ok', out)
        call = self.calls[0]
        self.assertEqual(call['model_id'], 'host-local-model')
        self.assertEqual(call['text_or_documents'], self.store.read(self.did)['text'])
        self.assertFalse(call['fetch_urls'])
        self.assertEqual(call['resolver_params'], {'suppress_parse_errors': False})
        self.assertIsInstance(call['examples'][0], Example)
        extraction = out['data']['grounded_extractions'][0]
        self.assertEqual(extraction['location_status'], 'located')
        self.assertEqual(extraction['source_char_range'], [0, 15])
        self.assertEqual(out['data']['sources'][0]['content_scope'], 'abstract')
        self.assertEqual(out['data']['passages'][0]['source_sha256'], self.manifest['source_sha256'])
        self.assertIsNone(out['provenance']['cost_visibility']['internal_model_calls'])
        self.assertFalse(out['coverage']['original_fulltext_verified'])

    def test_langextract_without_alignment_remains_unlocated(self):
        out = self.run_fake('langextract', self.langextract(interval=False), {'model': 'host-model'}, self.lx_inputs())
        row = out['data']['grounded_extractions'][0]
        self.assertEqual(row['location_status'], 'unlocated')
        self.assertIsNone(row['source_char_range'])

    def test_nonmatching_sdk_span_is_not_repaired_or_claimed_as_grounding(self):
        module = self.langextract()
        module.extract = lambda **kwargs: Annotated(kwargs['text_or_documents'], [
            Extraction('control', 'Invented text', {}, Interval(0, 15))])
        out = self.run_fake('langextract', module, {'model': 'host-model'}, self.lx_inputs())
        self.assertEqual(out['data']['grounded_extractions'][0]['location_status'], 'unlocated')

    def test_input_range_limit_is_partial_and_cursor_remains_bound(self):
        out = self.run_fake('langextract', self.langextract(), {'model': 'host-model', 'max_input_bytes': 16}, self.lx_inputs())
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(len(self.calls[0]['text_or_documents'].encode()), 16)
        cursor = out['continuation']['document_cursors'][self.did]
        next_out = self.run_fake('langextract', self.langextract(interval=False), {'model': 'host-model'},
            self.lx_inputs(cursors={self.did: cursor}))
        self.assertEqual(next_out['data']['sources'][0]['input_char_range'][0], 16)

    def test_missing_module_or_model_is_unavailable_without_invocation(self):
        for provider, settings, inputs in [
                ('langextract', {}, self.lx_inputs()),
                ('paperqa', {'model': 'host-model'}, {'document_ids': [self.did], 'query': 'Which control?'}),
                ('langextract', {'model': 'host-model'}, self.lx_inputs())]:
            with self.subTest(provider=provider, settings=settings):
                with patch('rsi_literature.sdk_tools.importlib.import_module', side_effect=ImportError):
                    out = run_sdk(provider, settings, inputs, self.store, 'test')
                self.assertEqual(out['status'], 'unavailable')
                self.assertEqual(out['provenance']['cost_visibility']['sdk_calls_observed'], 0)

    def test_model_cannot_supply_paths_or_override_host_model(self):
        for extra in ({'path': '/etc/passwd'}, {'model': 'unapproved-model'}, {'document_ids': ['/etc/passwd']}):
            with self.subTest(extra=extra):
                out = self.run_fake('langextract', self.langextract(), {'model': 'host-model'}, self.lx_inputs(**extra))
                self.assertEqual(out['status'], 'failed')
        self.assertEqual(self.calls, [])

    def test_timeout_after_sdk_invocation_is_unknown_and_does_not_leak_error(self):
        out = self.run_fake('langextract', self.langextract(failure=True), {'model': 'host-model'}, self.lx_inputs())
        self.assertEqual(out['status'], 'failed')
        self.assertTrue(out['execution_unknown'])
        self.assertNotIn('API-key secret', json.dumps(out))
        self.assertEqual(out['provenance']['cost_visibility']['sdk_calls_observed'], 1)

    def test_removed_providers_are_rejected_before_importing_or_reading(self):
        for provider in ('tooluniverse', 'llm_nerre', 'llm_organic_synthesis', 'openscholar', 'paperpilot'):
            with self.subTest(provider=provider), \
                    patch('rsi_literature.sdk_tools._sdk', side_effect=AssertionError('Do not import removed SDK')), \
                    patch.object(self.store, 'read', side_effect=AssertionError('Do not read sources')):
                out = run_sdk(provider, {'model': 'host-model', 'embedding': 'host-embedding'},
                    {'document_ids': [self.did], 'query': 'Which control?'}, self.store, 'removed-provider')
                self.assertEqual(out['status'], 'unavailable')
                self.assertEqual(out['provenance']['cost_visibility']['sdk_calls_observed'], 0)
                self.assertEqual(out['data'], {})
                self.assertNotIn('execution_unknown', out)


    def test_paperqa_settings_apply_when_adding_and_querying_explicit_source_files(self):
        calls = self.calls
        class Settings:
            def __init__(self, **kwargs):
                self.values = kwargs
                calls.append(('settings', kwargs))
        class Docs:
            async def aadd(self, path, **kwargs):
                calls.append(('add', Path(path).read_text(), kwargs))
            async def aquery(self, query, **kwargs):
                calls.append(('query', query, kwargs))
                return {'answer': 'A matched control was used.', 'contexts': [{'docname': 'paper-name'}]}
        out = self.run_fake('paperqa', SimpleNamespace(Docs=Docs, Settings=Settings),
            {'model': 'local-answer', 'embedding': 'local-embedding'}, {'document_ids': [self.did], 'query': 'Which control?'})
        self.assertEqual(out['status'], 'ok', out)
        config = calls[0][1]
        self.assertEqual(config['llm'], 'local-answer')
        self.assertEqual(config['summary_llm'], 'local-answer')
        self.assertEqual(config['embedding'], 'local-embedding')
        self.assertEqual(config['parsing'], {'use_doc_details': False, 'multimodal': False})
        index = config['agent']['index']
        self.assertEqual(Path(index['index_directory']).parent, Path(index['paper_directory']))
        self.assertFalse(Path(index['paper_directory']).exists(), 'temporary source/index directory must be cleaned')
        self.assertIs(calls[1][2]['settings'], calls[2][2]['settings'])
        self.assertEqual(calls[1][1], self.store.read(self.did)['text'])
        self.assertEqual(calls[1][2]['docname'], self.did)
        self.assertIn(self.did, out['data']['citation_mapping'])
        self.assertEqual(out['data']['authority'], 'retrieval_augmented_interpretation_not_original_source')
        self.assertEqual(out['provenance']['cost_visibility']['sdk_calls_observed'], 2)
        self.assertEqual(out['provenance']['sdk_package']['name'], 'paper-qa')
        self.assertEqual(os.environ['LITELLM_LOCAL_MODEL_COST_MAP'], 'True')

    def test_installed_package_version_is_recorded(self):
        with patch('rsi_literature.sdk_tools.importlib.metadata.version', return_value='1.7.2'):
            out = self.run_fake('langextract', self.langextract(), {'model': 'host-model'}, self.lx_inputs())
        self.assertEqual(out['provenance']['sdk_package'], {'name': 'langextract', 'version': '1.7.2'})

    def test_oversized_output_is_omitted_with_explicit_partial_coverage(self):
        out = self.run_fake('langextract', self.langextract(), {'model': 'host-model', 'max_output_bytes': 100}, self.lx_inputs())
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(out['data']['omitted'], 'output_capacity')
        self.assertNotIn('passages', out['data'])


if __name__ == '__main__':
    unittest.main()
