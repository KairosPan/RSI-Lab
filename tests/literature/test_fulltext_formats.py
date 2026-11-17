"""Format selection chooses provider candidates without introducing model URLs."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from rsi_literature.adapters import _acquire, _download, configuration, make_adapters
from rsi_literature.documents import DocumentStore


XML = b'<article><body><sec><title>Results</title><p>Measured 12.5 units.</p></sec></body></article>'
PDF = b'%PDF-1.4\nregistered format fixture'


class FulltextFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DocumentStore(self.root / 'documents')
        self.config = configuration(self.store.root)
        self.pdf = {'format': 'pdf', 'url': 'https://example.org/paper.pdf', 'media_type': 'application/pdf'}
        self.xml = {'format': 'xml', 'url': 'https://example.org/article.xml', 'media_type': 'application/xml'}
        self.html = {'format': 'html', 'url': 'https://example.org/article', 'media_type': 'text/html'}
        self.client = Mock()

    def acquire(self, candidates, preferred=None, raw=XML, media_type='application/xml'):
        self.client.fulltext_candidates.return_value = {'status': 'ok', 'data': {'paper': {'title': 'Fixture'}, 'candidates': candidates},
            'provenance': {'attempt_count': 1, 'requests': []}, 'errors': []}
        inputs = {'paper_ref': 'PMCID:PMC1'}
        if preferred is not None:
            inputs['preferred_format'] = preferred
        with patch('rsi_literature.adapters._download', return_value=(raw, media_type, 1, None)) as download:
            with patch.dict(sys.modules, {'pypdf': None}):
                result = _acquire(inputs, self.config, self.store, self.client)
        return result, download

    def test_default_remains_xml_first_and_explicit_pdf_selects_pdf(self):
        result, download = self.acquire([self.pdf, self.html, self.xml])
        self.assertEqual(download.call_args.args[0], self.xml)
        self.assertEqual(result['status'], 'ok')
        result, download = self.acquire([self.xml, self.pdf], 'pdf', PDF, 'application/pdf; charset=binary')
        self.assertEqual(download.call_args.args[0], self.pdf)
        self.assertEqual(result['provenance']['attempt_count'], 2)
        manifest = self.store.manifest(result['data']['document_id'])
        self.assertEqual(manifest['media_type'], 'application/pdf; charset=binary')
        self.assertEqual(manifest['source_metadata']['source_url'], self.pdf['url'])
        self.assertEqual(manifest['reason']['code'], 'dependency_missing')

    def test_xml_preference_accepts_jats_and_tei_candidates(self):
        for family, media_type in [('jats', 'application/jats+xml'), ('tei', 'application/tei+xml')]:
            candidate = {**self.xml, 'format': family}
            result, download = self.acquire([self.pdf, candidate], 'xml', XML, media_type)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(download.call_args.args[0], candidate)
            self.assertEqual(result['data']['source_metadata']['acquired_media_type'], media_type)

    def test_no_matching_candidate_is_explicit_and_never_falls_back(self):
        result, download = self.acquire([self.xml, self.html], 'pdf')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['errors'][0]['code'], 'fulltext_format_unavailable')
        self.assertEqual(result['data']['candidates'], [self.xml, self.html])
        self.assertEqual(result['provenance']['attempt_count'], 1)
        download.assert_not_called()
        self.assertEqual(list(self.store.root.glob('doc_*')), [])

    def test_pdf_magic_and_response_type_both_must_match(self):
        for raw, media_type in [(b'<html>challenge</html>', 'text/html'),
                                (b'<html>challenge</html>', 'application/pdf'),
                                (PDF, 'text/html'), (PDF, 'application/octet-stream')]:
            with self.subTest(media_type=media_type, raw=raw[:5]):
                result, download = self.acquire([self.pdf, self.xml], 'pdf', raw, media_type)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['errors'][0]['code'], 'fulltext_format_mismatch')
                self.assertEqual(download.call_count, 1)
                self.assertEqual(result['provenance']['attempt_count'], 2)
                self.assertNotIn('document_id', result['data'])
        self.assertEqual(list(self.store.root.glob('doc_*')), [])

    def test_xml_preference_rejects_html_media_type(self):
        result, _ = self.acquire([self.xml], 'xml', b'<html>challenge</html>', 'text/html')
        self.assertEqual(result['errors'][0]['code'], 'fulltext_format_mismatch')
        self.assertNotIn('document_id', result['data'])

    def test_missing_response_mime_is_not_inferred_from_the_candidate(self):
        with patch('rsi_literature.adapters._public_url'), patch('rsi_literature.adapters.remote.http_request',
                return_value={'status': 200, 'headers': {}, 'body': PDF}):
            _, media_type, attempts, error = _download(self.pdf, self.config)
        self.assertEqual(media_type, 'application/octet-stream')
        self.assertEqual(attempts, 1)
        self.assertIsNone(error)

    def test_invalid_format_or_local_format_dispatches_nothing(self):
        local = self.root / 'local.txt'
        local.write_text('Local full text')
        config = {'local_sources': {'local-paper': str(local)}}
        adapter = next(a for a in make_adapters(self.store.root, config) if a.adapter_id == 'literature.acquire_fulltext')
        for inputs in ({'paper_ref': 'PMCID:PMC1', 'preferred_format': 'html'},
                       {'paper_ref': 'PMCID:PMC1', 'preferred_format': {'url': 'https://example.org'}},
                       {'source_id': 'local-paper', 'preferred_format': 'pdf'},
                       {'paper_ref': 'PMCID:PMC1', 'preferred_format': 'pdf', 'url': 'https://example.org'}):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                adapter.prepare(inputs)
        with self.assertRaises(ValueError):
            _acquire({'paper_ref': 'PMCID:PMC1', 'preferred_format': 'html'}, self.config, self.store, self.client)
        self.client.fulltext_candidates.assert_not_called()

    def test_local_registration_retains_only_host_source_identity_fields(self):
        local = self.root / 'local.txt'
        local.write_text('Local full text')
        source = {'path': str(local), 'content_scope': 'full_text', 'source_url': 'https://repository.example.org/paper.pdf',
            'paper_ref': 'DOI:10.1000/example', 'license': 'CC-BY-4.0', 'unrelated_host_field': 'not source evidence'}
        config = configuration(self.store.root, {'local_sources': {'official-copy': source}})
        result = _acquire({'source_id': 'official-copy'}, config, self.store, self.client)
        metadata = result['data']['source_metadata']
        self.assertEqual(metadata, {'source_id': 'official-copy', 'provider': 'local', 'content_scope': 'full_text',
            'source_url': source['source_url'], 'paper_ref': source['paper_ref'], 'license': source['license']})
        self.client.fulltext_candidates.assert_not_called()


if __name__ == '__main__':
    unittest.main()
