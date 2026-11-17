"""Real local subprocess fixtures verify image bytes and CLI routing, without a model."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from rsi_lab import transport
from rsi_lab.images import attachment_manifest, image_manifest_sha256, verify_image_transport
from rsi_lab.journal import read_journal


THREAD = 'f38a21a2-1f39-43cf-8027-6ae633f1f445'


def attachment(index=0, width=1, height=1):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    raw = (b'\x89PNG\r\n\x1a\n'
           + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress((bytes([0]) + bytes([index, 30, 60]) * width) * height))
           + chunk(b'IEND', b''))
    digest = hashlib.sha256(raw).hexdigest()
    return {'attachment_id': f'image-{index}', 'evidence_id': f'fact-{index}',
            'source_version': 1, 'media_type': 'image/png', 'sha256': digest,
            'bytes': len(raw), 'width': width, 'height': height, 'page': index + 1,
            'source_document_id': 'doc-fixture', 'source_sha256': 'a' * 64,
            'blob_ref': digest, 'data_base64': base64.b64encode(raw).decode('ascii')}


class ImageTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.folder = self.root / 'call'
        self.real_popen = subprocess.Popen
        self.commands = []

    def invoke(self, images=None, session=None, incomplete=False):
        # The subprocess reads the exact -i files before returning their hashes.
        # This exercises materialization and streaming capture rather than mocking
        # the transport response or merely checking a command builder.
        code = '''import hashlib,json,pathlib,sys
sys.stdin.read()
hashes = [hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in sys.argv[1:]]
events = [
    {"type":"thread.started","thread_id":"f38a21a2-1f39-43cf-8027-6ae633f1f445"},
    {"type":"turn.started"},
    {"type":"item.completed","item":{"type":"agent_message","text":json.dumps({"hashes":hashes})}},
    {"type":"turn.completed","usage":{"input_tokens":20,"output_tokens":5}}
]
for event in events:
    print(json.dumps(event), flush=True)
'''
        if incomplete:
            code = 'import sys; sys.stdin.read(); sys.stdout.write("partial"); sys.stdout.flush()'

        def fake(command, **kwargs):
            self.commands.append(command)
            image_paths = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == '-i']
            return self.real_popen([sys.executable, '-u', '-c', code, *image_paths], **kwargs)

        with patch.object(transport.subprocess, 'Popen', side_effect=fake):
            return transport.invoke('Inspect the supplied evidence images.', {'type': 'object'},
                                    self.folder, 'test-model', 'medium', timeout=3,
                                    session=session, images=images)

    def test_create_and_resume_send_exact_images_with_durable_manifests(self):
        images = [attachment(1), attachment(2)]
        manifest = attachment_manifest(images)
        digest = image_manifest_sha256(images)
        for mode, native in [('create', None), ('resume', THREAD)]:
            with self.subTest(mode=mode):
                self.folder = self.root / mode
                session = {'session_id': 'owned-session', 'workspace': str(self.root / 'workspace'),
                           'provider_thread_id': native, 'codex_home': str(self.root / 'codex-home')}
                result = self.invoke(images, session)
                self.assertEqual(result['execution_status'], 'succeeded', result)
                self.assertEqual(result['response']['hashes'], [item['sha256'] for item in images])
                command = self.commands[-1]
                self.assertEqual('resume' in command, native is not None)
                self.assertEqual(command.count('-i'), 2)
                self.assertNotIn('--ephemeral', command)
                for feature in ('shell_tool', 'unified_exec', 'view_image', 'apps', 'browser_use'):
                    position = command.index(feature)
                    self.assertEqual(command[position - 1], '--disable')
                invocation = json.loads((self.folder / 'invocation.json').read_text())
                archived = json.loads((self.folder / 'images.json').read_text())
                receipt = json.loads((self.folder / 'transport_result.json').read_text())
                self.assertEqual(receipt, result)
                for value in (invocation, receipt):
                    self.assertEqual(value['sent_image_manifest'], manifest)
                    self.assertEqual(value['sent_image_manifest_sha256'], digest)
                self.assertEqual(archived['manifest'], manifest)
                self.assertNotIn('data_base64', json.dumps(archived))
                sent_prompt = (self.folder / 'prompt.txt').read_text()
                self.assertNotIn('data_base64', sent_prompt)
                for image in images:
                    self.assertNotIn(image['data_base64'], sent_prompt)
                for index, file in enumerate(archived['files']):
                    path = self.folder / 'images' / f'image-{index:03d}.png'
                    self.assertEqual(file['path'], str(path.resolve()))
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.read_bytes(), base64.b64decode(images[index]['data_base64']))
                self.assertEqual(verify_image_transport(self.folder, manifest, result), manifest)
                first = read_journal(self.folder / 'journal.jsonl')['records'][0]
                requested = json.loads(base64.b64decode(first['raw_base64']))['invocation']
                self.assertEqual(requested['requested_image_manifest'], manifest)
                self.assertEqual(requested['requested_image_manifest_sha256'], digest)
                self.assertFalse(requested['dispatched'])
                self.assertNotIn('sent_image_manifest', requested)

    def test_no_images_preserves_text_only_artifacts_and_arguments(self):
        result = self.invoke()
        self.assertEqual(result['response'], {'hashes': []})
        self.assertNotIn('-i', self.commands[0])
        self.assertNotIn('sent_image_manifest', result)
        self.assertFalse((self.folder / 'images.json').exists())
        self.assertFalse((self.folder / 'images').exists())

    def test_malformed_images_are_rejected_before_any_dispatch_or_folder_creation(self):
        good = attachment()
        bad_hash = {**good, 'sha256': 'b' * 64, 'blob_ref': 'b' * 64}
        cases = [good, [bad_hash], [{**good, 'path': '/user/selected.png'}],
                 [{**good, 'width': 2}], [{**good, 'data_base64': 'not-base64'}],
                 [good, copy.deepcopy(good)], [attachment(i) for i in range(5)]]
        with patch.object(transport.subprocess, 'Popen') as popen:
            for images in cases:
                with self.subTest(images_type=type(images).__name__):
                    with self.assertRaises(ValueError):
                        transport.invoke('Inspect', {'type': 'object'}, self.folder,
                                         'test-model', 'medium', images=images)
                    self.assertFalse(self.folder.exists())
            popen.assert_not_called()

    def test_oversized_patch_grid_is_rejected_before_dispatch_and_artifacts(self):
        with patch.object(transport.subprocess, 'Popen') as popen:
            for width, height in ((1601, 1601), (1411, 2000)):
                with self.subTest(width=width, height=height), self.assertRaisesRegex(ValueError, 'no-resize'):
                    transport.invoke('Inspect', {'type': 'object'}, self.folder,
                                     'test-model', 'medium', images=[attachment(width=width, height=height)])
                self.assertFalse(self.folder.exists())
            popen.assert_not_called()

    def test_missing_cli_keeps_requested_images_but_never_certifies_submission(self):
        images = [attachment()]
        with patch.object(transport.subprocess, 'Popen', side_effect=FileNotFoundError('codex unavailable')):
            result = transport.invoke('Inspect', {'type': 'object'}, self.folder,
                                      'test-model', 'medium', images=images)
        invocation = json.loads((self.folder / 'invocation.json').read_text())
        self.assertEqual(result['execution_status'], 'failed_definite')
        self.assertFalse(result['dispatched'])
        self.assertEqual(invocation['requested_image_manifest'], attachment_manifest(images))
        self.assertTrue((self.folder / 'images.json').exists())
        for value in (result, invocation):
            self.assertNotIn('sent_image_manifest', value)
            self.assertNotIn('sent_image_manifest_sha256', value)

    def test_dispatched_incomplete_turn_records_submission_without_success_claim(self):
        result = self.invoke([attachment()], incomplete=True)
        self.assertTrue(result['dispatched'])
        self.assertEqual(result['execution_status'], 'unknown')
        self.assertFalse(result['stream_complete'])
        self.assertIsNone(result['response'])
        self.assertEqual(result['sent_image_manifest'], attachment_manifest([attachment()]))
        self.assertEqual(len(self.commands), 1)

    def test_existing_image_artifacts_and_symlinks_are_never_replaced(self):
        for kind in ('file', 'directory', 'symlink'):
            with self.subTest(kind=kind):
                self.folder = self.root / kind
                self.folder.mkdir()
                destination = self.folder / 'images'
                if kind == 'file':
                    destination.write_bytes(b'existing')
                elif kind == 'directory':
                    destination.mkdir()
                else:
                    destination.symlink_to(self.root)
                with patch.object(transport.subprocess, 'Popen') as popen:
                    with self.assertRaises(FileExistsError):
                        transport.invoke('Inspect', {'type': 'object'}, self.folder,
                                         'test-model', 'medium', images=[attachment()])
                    popen.assert_not_called()
                self.assertFalse((self.folder / 'invocation.json').exists())

    def test_image_tampering_invalidates_later_transport_verification(self):
        images = [attachment()]
        result = self.invoke(images)
        path = self.folder / 'images' / 'image-000.png'
        path.write_bytes(base64.b64decode(attachment(1)['data_base64']))
        with self.assertRaises(ValueError):
            verify_image_transport(self.folder, attachment_manifest(images), result)


if __name__ == '__main__':
    unittest.main()
