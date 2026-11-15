"""Exact image-source delivery, bounded native history and recovery without models."""
import base64
import copy
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from rsi_lab import images
from rsi_lab.packets import (PacketBuilder, acknowledge_page, make_evidence, present_delivery)
from rsi_lab.session_context import (ContextUnavailable, build_effective_context,
    snapshot_from_bytes, verify_snapshot_images)
from rsi_lab.store import digest, encode
from rsi_lab.store import Store
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from tests.v2 import test_call_recovery


def png(red=255, width=2, height=1):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    row = bytes([0, red, 0, 0]) + bytes([0, 255, 0]) * (width - 1)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(row * height)) + chunk(b'IEND', b''))


def content(width=2, height=1):
    raw = png(width=width, height=height)
    return images.image_content_from_bytes(raw, {'media_type': 'image/png', 'sha256': digest(raw),
        'bytes': len(raw), 'width': width, 'height': height, 'page': 2,
        'source_document_id': 'paper-1', 'source_sha256': 'a' * 64})


def evidence(eid='image-evidence'):
    return make_evidence(eid, 'Rendered original page 2 of document paper-1', kind='source_statement',
                         source='registered PDF renderer', image=images.image_reference(content()))


def packet(**limits):
    return PacketBuilder({'image-evidence': evidence()}, **limits).plan(mode='literature',
        instruction='Inspect the attached page when needed.', schema={'type': 'object'}, task={},
        evidence_ids=['image-evidence'], required_evidence_ids=['image-evidence'])


def native_image(raw=None):
    return {'type': 'input_image', 'image_url': 'data:image/png;base64,' +
            base64.b64encode(png() if raw is None else raw).decode('ascii')}


def frame(kind, payload):
    return encode({'type': kind, 'payload': payload}) + b'\n'


def native(cwd):
    return (frame('session_meta', {'id': test_call_recovery.THREAD, 'cwd': str(cwd)})
        + frame('response_item', {'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Inspect this page'}, native_image()]}))


class ImageEvidenceTests(unittest.TestCase):
    def test_cli_attachment_patch_budget_preserves_generic_source_acceptance(self):
        for width, height, accepted in [(1600, 1600, True), (1601, 1601, False),
                                        (1411, 2000, False), (1900, 1000, True)]:
            with self.subTest(width=width, height=height):
                source = content(width, height)
                # Generic PNG/source validation still accepts bounded originals;
                # only CLI presentation imposes the verified no-resize policy.
                self.assertEqual(images.validate_image_content(source), source)
                row = make_evidence('sized-image', 'Registered original page', kind='source_statement',
                                    source='renderer', image=images.image_reference(source))
                attachment = {**{key: source[key] for key in images.IMAGE_FIELDS},
                    'attachment_id': 'attachment-1', 'evidence_id': row['evidence_id'], 'source_version': 1,
                    'blob_ref': source['sha256'], 'data_base64': source['data_base64']}
                builder = PacketBuilder({row['evidence_id']: row})
                arguments = {'mode': 'literature', 'instruction': 'Inspect the page.', 'schema': {}, 'task': {},
                             'evidence_ids': [row['evidence_id']], 'required_evidence_ids': [row['evidence_id']]}
                if accepted:
                    self.assertEqual(images.validate_image_attachment(attachment, with_data=True), attachment)
                    self.assertEqual(builder.plan(**arguments)['status'], 'ready')
                else:
                    with self.assertRaisesRegex(ValueError, '2500 32x32 patches'):
                        images.validate_image_attachment(attachment, with_data=True)
                    with self.assertRaisesRegex(ValueError, 'no-resize'):
                        builder.plan(**arguments)

    def test_source_rejects_tampered_hash_size_dimensions_and_paths(self):
        original = content()
        for changed in ({'sha256': 'b' * 64}, {'bytes': original['bytes'] + 1}, {'width': 2001},
                        {'height': 2}, {'page': 0}, {'artifact_path': '/tmp/forged.png'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                images.validate_image_content({**original, **changed})
        broken = bytearray(png()); broken[-1] ^= 1
        with self.assertRaisesRegex(ValueError, 'checksum'):
            images.png_dimensions(bytes(broken))

    def test_packet_binds_image_bytes_separately_without_putting_pixels_in_prompt(self):
        value = packet()
        self.assertEqual(value['status'], 'ready')
        self.assertEqual(value['input_bytes'], len(value['envelope_bytes']))
        self.assertEqual(value['image_input_bytes'], len(png()))
        self.assertEqual(value['image_count'], 1)
        self.assertNotIn('data_base64', value['envelope']['prompt'])
        self.assertNotIn(content()['data_base64'], value['envelope']['prompt'])
        image = value['envelope']['image_attachments'][0]
        self.assertEqual(image['blob_ref'], digest(png()))
        self.assertEqual(image['page'], 2)
        self.assertEqual(image['evidence_id'], 'image-evidence')

    def test_required_image_cannot_bypass_count_or_byte_budget_by_narrowing(self):
        for limits in ({'max_images': 0}, {'max_image_bytes': len(png()) - 1}):
            with self.subTest(limits=limits):
                value = packet(**limits)
                self.assertEqual(value['status'], 'packet_unrepresentable')
                self.assertTrue(all(attempt['status'] == 'blocked_capacity' for attempt in value['attempts']))

    def test_pointer_or_forged_manifest_cannot_certify_visual_delivery(self):
        value = packet()
        kwargs = {'effect_id': 'effect-image', 'sent_packet_sha256': value['packet_sha256'],
            'sent_prompt_sha256': value['prompt_sha256'], 'receipt_ref': 'bound-receipt'}
        with self.assertRaisesRegex(ValueError, 'verified real image'):
            present_delivery(value, **kwargs)
        stripped = copy.deepcopy(value)
        stripped['envelope'].pop('image_attachments')
        with self.assertRaises(ValueError):
            present_delivery(stripped, **kwargs)
        changed = copy.deepcopy(value)
        changed['delivery_pages'][0]['image']['page'] = 3
        with self.assertRaisesRegex(ValueError, 'pages differ'):
            present_delivery(changed, **kwargs)
        manifest = images.attachment_manifest(value['envelope']['image_attachments'])
        presented = present_delivery(value, **kwargs, sent_image_manifest=manifest,
            sent_image_manifest_sha256=images.image_manifest_sha256(manifest), image_delivery_verified=True)[0]
        self.assertTrue(presented['visual_presented'])
        ack = acknowledge_page(presented, page_id=presented['page_id'], source_version=1,
            content_sha256=presented['content_sha256'], raw_source_id=presented['evidence_id'])
        self.assertEqual(ack['status'], 'acked')
        with self.assertRaisesRegex(ValueError, 'visual presentation'):
            acknowledge_page({**presented, 'visual_presented': False}, page_id=presented['page_id'],
                source_version=1, content_sha256=presented['content_sha256'])
        tampered = copy.deepcopy(presented)
        tampered['image']['page'] = 3
        with self.assertRaisesRegex(ValueError, 'certified source attachment'):
            acknowledge_page(tampered, page_id=presented['page_id'], source_version=1,
                             content_sha256=presented['content_sha256'])

    def test_canonical_plan_storage_does_not_change_wire_identity(self):
        value = packet()
        restored = json.loads(encode({key: item for key, item in value.items() if key != 'envelope_bytes'}))
        restored['envelope_bytes'] = value['envelope_bytes']
        manifest = images.attachment_manifest(value['envelope']['image_attachments'])
        shown = present_delivery(restored, effect_id='effect-image', sent_packet_sha256=value['packet_sha256'],
            sent_prompt_sha256=value['prompt_sha256'], receipt_ref='bound-receipt',
            sent_image_manifest=manifest, sent_image_manifest_sha256=images.image_manifest_sha256(manifest),
            image_delivery_verified=True)
        self.assertTrue(shown[0]['visual_presented'])

    def test_snapshot_retains_png_hash_and_budgets_images_without_current_read_rights(self):
        with tempfile.TemporaryDirectory() as root:
            raw = native(root)
            snapshot = snapshot_from_bytes(raw, expected_thread_id=test_call_recovery.THREAD, expected_cwd=root)
            self.assertEqual(snapshot['prefix_sha256'], digest(raw))
            self.assertEqual(snapshot['observable_history_bytes'], len(raw))
            self.assertEqual(snapshot['observable_image_bytes'], len(png()))
            self.assertEqual(snapshot['observable_text_history_bytes'], len(raw) - len(content()['data_base64']))
            self.assertIn('none', snapshot['read_authority'])
            value = packet()
            result = build_effective_context(value['envelope_bytes'], session_id='owned-session', owner={'role': 'literature'},
                snapshot=snapshot, max_input_bytes=100000, max_image_bytes=2 * len(png()) - 1)
            self.assertEqual(result['status'], 'blocked_capacity')
            self.assertEqual(result['manifest']['total_image_bytes'], 2 * len(png()))
            self.assertIsNone(result['manifest']['image_token_bound'])
            self.assertFalse(result['manifest']['retained_read_authority'])

    def test_native_input_must_match_current_attachment_not_old_history_or_another_png(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = snapshot_from_bytes(native(root), expected_thread_id=test_call_recovery.THREAD, expected_cwd=root)
            expected = packet()['envelope']['image_attachments']
            self.assertTrue(verify_snapshot_images(snapshot, expected)['native_image_inputs_verified'])
            with self.assertRaisesRegex(ContextUnavailable, 'image_delivery_mismatch'):
                verify_snapshot_images(snapshot, expected, after_record=snapshot['record_count'])
            other = native(root).replace(base64.b64encode(png()), base64.b64encode(png(0)))
            changed = snapshot_from_bytes(other, expected_thread_id=test_call_recovery.THREAD, expected_cwd=root)
            with self.assertRaisesRegex(ContextUnavailable, 'image_delivery_mismatch'):
                verify_snapshot_images(changed, expected)
            bad = native(root).replace(b'data:image/png;base64,', b'https://not-an-inline-image/')
            with self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable'):
                snapshot_from_bytes(bad, expected_thread_id=test_call_recovery.THREAD, expected_cwd=root)

    def test_native_read_only_audit_frames_are_bounded_without_becoming_sources(self):
        with tempfile.TemporaryDirectory() as root:
            counters = dict.fromkeys(('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens'), 0)
            usage = {'thread_id': test_call_recovery.THREAD, 'turn_id': 'turn-1', 'session_id': test_call_recovery.THREAD,
                'root_turn_id': 'turn-1', 'response_id': 'response-1', 'usage': counters,
                'turn_token_usage': counters, 'thread_token_usage': counters}
            base = native(root)
            tail = frame('world_state', {'full': True, 'state': {'audit': 'unverified host snapshot'}})
            tail += frame('token_usage_record', usage)
            snapshot = snapshot_from_bytes(base + tail, expected_thread_id=test_call_recovery.THREAD, expected_cwd=root)
            self.assertEqual(snapshot['observable_history_bytes'], len(base + tail))
            self.assertEqual(len(snapshot['messages']), 1)
            self.assertFalse(snapshot['provider_context_verified'])
            for kind, payload in [('world_state', {'full': 'yes', 'state': {}}),
                                  ('token_usage_record', {**usage, 'thread_id': 'another-thread'}),
                                  ('token_usage_record', {**usage, 'usage': {**counters, 'total_tokens': -1}}),
                                  ('response_item', {'type': 'function_call', 'name': 'shell', 'arguments': '{}'}),
                                  ('compacted', {'message': 'summary'})]:
                with self.subTest(kind=kind), self.assertRaises(ContextUnavailable):
                    snapshot_from_bytes(base + frame(kind, payload), expected_thread_id=test_call_recovery.THREAD, expected_cwd=root)

    def test_raw_text_facts_remain_readable_alongside_image_references(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            self.addCleanup(store.close)
            Controller(store).initialize('r', budgets={'model_calls': {'limit': 2, 'hard': True}}, config={})
            def publish(tx):
                tx.insert('fact', 'text-fact', {'kind': 'raw_fact', 'content_ref': tx.blob(b'plain text, not JSON'),
                    'provenance': {'producer': 'fixture'}})
                tx.blob(png())
                tx.insert('fact', 'image-fact', {'kind': 'source_statement',
                    'content_ref': tx.blob(images.image_reference(content())), 'provenance': {'producer': 'fixture'}})
            store.mutate('r', 'fixtures', 'fixture_sources', publish)
            runtime = Runtime(store, 'r')
            work = runtime.enqueue_lab('inspect', ['text-fact', 'image-fact'])
            agent, mode, task, options = runtime._context(work)
            value = agent.prepare(mode, task, **options)
            self.assertEqual(value['status'], 'ready')
            self.assertIn('plain text, not JSON', value['envelope']['prompt'])
            self.assertEqual(value['envelope']['image_attachments'][0]['evidence_id'], 'image-fact')

    def recovery_fixture(self):
        case = test_call_recovery.CallRecoveryTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        manifest = images.attachment_manifest(packet()['envelope']['image_attachments'])
        case.request['image_attachments'] = manifest
        case.packet = encode(case.request)
        case.effect['packet_sha256'] = digest(case.packet)
        image_folder = case.folder / 'images'; image_folder.mkdir()
        path = (image_folder / 'image-000.png').resolve(); path.write_bytes(png())
        command = ['codex', 'exec', '--json', '-i', str(path)]
        binding = {'session_id': 'session_test', 'workspace': str(case.workspace),
            'provider_thread_id': None, 'codex_home': str(case.home)}
        invocation = {'backend': 'codex', 'model': case.request['model'], 'effort': 'low',
            'command': command, 'cwd': str(case.workspace), 'session': binding, 'dispatched': False,
            'requested_image_manifest': manifest,
            'requested_image_manifest_sha256': images.image_manifest_sha256(manifest)}
        case.fixture(saved=False, journal_delta={'invocation': invocation})
        (case.folder / 'invocation.json').write_bytes(encode({**invocation, 'dispatched': True,
            'sent_image_manifest': manifest, 'sent_image_manifest_sha256': images.image_manifest_sha256(manifest)}))
        (case.folder / 'images.json').write_bytes(encode({'manifest': manifest, 'files': [{
            'attachment_id': manifest[0]['attachment_id'], 'path': str(path), 'sha256': digest(png()), 'bytes': len(png())}]}))
        rollout = next((case.home / 'sessions').glob('*.jsonl'))
        with rollout.open('ab') as stream:
            stream.write(frame('response_item', {'type': 'message', 'role': 'user', 'content': [native_image()]}))
        return case, path

    def test_recovery_certifies_existing_image_without_relaunching_and_rejects_tampering(self):
        case, path = self.recovery_fixture()
        receipt = case.recover()
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertTrue(receipt['image_delivery_verified'])
        self.assertTrue(receipt['context_manifest']['native_image_inputs_verified'])
        path.write_bytes(png(0))
        with self.assertRaisesRegex(test_call_recovery.RecoveryBlocked, 'recovery_image_mismatch'):
            case.recover()


if __name__ == '__main__':
    unittest.main()
