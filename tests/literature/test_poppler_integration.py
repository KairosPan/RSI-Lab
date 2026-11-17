"""Real local PDF extraction/rendering through registered effect publication."""
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from rsi_literature import make_adapters


def sample_pdf():
    """Two pages: searchable native text, then a drawing with no text layer."""
    text = b'BT /F1 18 Tf 40 220 Td (Baseline result 8.0; inspect page two visually.) Tj ET'
    drawing = b'0.8 0.1 0.1 rg 40 40 80 160 re f 0.1 0.2 0.9 rg 160 40 80 80 re f'
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 500 280] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 500 280] /Contents 7 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(text)).encode() + b' >>\nstream\n' + text + b'\nendstream',
        b'<< /Length ' + str(len(drawing)).encode() + b' >>\nstream\n' + drawing + b'\nendstream',
    ]
    raw, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(str(i).encode() + b' 0 obj\n' + obj + b'\nendobj\n')
    xref = len(raw)
    raw.extend(b'xref\n0 8\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        raw.extend(f'{offset:010d} 00000 n \n'.encode())
    raw.extend(f'trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(raw)


@unittest.skipUnless(all(shutil.which(command) for command in ('pdfinfo', 'pdftotext', 'pdftoppm')),
                     'Local Poppler executables are required')
class PopplerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'paper.pdf'
        source.write_bytes(sample_pdf())
        self.document_root = self.root / 'documents'
        self.config = {'local_sources': {'paper': {'path': str(source), 'content_scope': 'full_text'}}}
        self.adapters = make_adapters(self.document_root, self.config)
        self.store = Store(self.root / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('pdf', budgets={'tool_calls': {'limit': 30, 'hard': True},
            'model_calls': {'limit': 10, 'hard': True}}, config={'goal': 'Read the paper and inspect its chart'})
        self.runtime = Runtime(self.store, 'pdf', adapters=self.adapters)
        self.sequence = 0

    def call(self, name, inputs):
        self.sequence += 1
        return self.runtime.execute_adapter('literature.' + name, inputs, trigger_id='pdf-' + str(self.sequence))

    def source(self):
        return self.call('acquire_fulltext', {'source_id': 'paper'})['result']['data']['document_id']

    def test_native_text_coordinates_and_graphics_page_remain_distinct(self):
        source_id = self.source()
        parsed = self.call('build_reading_package', {'provider': 'poppler', 'document_id': source_id})
        self.assertEqual(parsed['result']['status'], 'partial', parsed)
        doc_id = parsed['result']['data']['document_id']
        found = self.call('find_passages', {'document_ids': [doc_id], 'query': '8.0'})
        passage = found['result']['data']['passages'][0]
        self.assertEqual(passage['source_locator']['page'], 1)
        self.assertEqual(passage['source_locator']['source_document_id'], source_id)
        self.assertTrue(passage['source_locator']['words'])
        rendered = self.call('render_document_page', {'provider': 'poppler', 'document_id': doc_id, 'page': 2})
        self.assertEqual(rendered['result']['status'], 'ok', rendered)
        self.assertEqual(rendered['effect']['actual_costs']['tool_calls'], 2)
        public_image = rendered['result']['data']['image']
        self.assertEqual(public_image['page'], 2)
        self.assertEqual(public_image['source_document_id'], source_id)
        self.assertNotIn('data_base64', public_image)
        self.assertNotIn('artifact_path', public_image)
        image_facts = [f for f in self.store.list('pdf', 'fact')
                       if f['provenance'].get('delivery_kind') == 'source_image']
        self.assertEqual(len(image_facts), 1)
        self.assertEqual(self.runtime.pending_adapter_publications(), [])
        self.store.verify_history('pdf')

    def test_render_recovery_uses_receipt_bytes_not_mutable_output_path(self):
        source_id = self.source()
        with patch.object(self.runtime, 'publish_adapter_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.call('render_document_page', {'provider': 'poppler', 'document_id': source_id, 'page': 2})
        images = list((self.document_root / 'jobs').glob('*/page.png'))
        self.assertEqual(len(images), 1)
        original_sha = hashlib.sha256(images[0].read_bytes()).hexdigest()
        images[0].write_bytes(b'mutated after immutable receipt')
        resumed = Runtime(self.store, 'pdf', adapters=make_adapters(self.document_root, self.config))
        with patch('rsi_literature.poppler.render_document_page', side_effect=AssertionError('Recovery must not rerender')):
            resumed.recover()
        image_facts = [f for f in self.store.list('pdf', 'fact')
                       if f['provenance'].get('delivery_kind') == 'source_image']
        self.assertEqual(len(image_facts), 1)
        self.assertEqual(image_facts[0]['provenance']['image_sha256'], original_sha)
        self.assertEqual(resumed.pending_adapter_publications(), [])

    def test_page_bounds_and_host_only_paths(self):
        for inputs in ({'page': 0}, {'page': True}, {'page': 1, 'dpi': 999},
                       {'page': 1, 'artifact_path': '/tmp/arbitrary.png'}):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                self.call('render_document_page', {'provider': 'poppler', 'document_id': 'doc_unknown', **inputs})
        self.assertEqual(self.store.list('pdf', 'effect'), [])
        result = self.call('render_document_page', {'provider': 'poppler', 'document_id': self.source(), 'page': 3})
        self.assertIn(result['result']['status'], {'failed', 'unavailable'})
        self.assertFalse(any(f['provenance'].get('delivery_kind') == 'source_image'
                             for f in self.store.list('pdf', 'fact')))


if __name__ == '__main__':
    unittest.main()
