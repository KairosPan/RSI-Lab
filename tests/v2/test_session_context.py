"""Synthetic native rollout fixtures only; no Codex or model process is run."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab.packets import make_evidence, page_evidence
from rsi_lab.session_context import (ContextUnavailable, build_effective_context,
    find_owned_rollout, read_native_snapshot, retained_source_candidates,
    snapshot_from_bytes, validate_rollout_extension)
from rsi_lab.store import digest


THREAD = '11111111-1111-1111-1111-111111111111'
OTHER = '22222222-2222-2222-2222-222222222222'


def frame(kind, payload):
    return (json.dumps({'timestamp': '2000-01-01T00:00:00Z', 'type': kind, 'payload': payload},
                       ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def fixture(cwd, *, thread=THREAD, prompt='First research question'):
    return (frame('session_meta', {'id': thread, 'cwd': str(cwd), 'base_instructions': {'text': 'Stay within task.'}})
            + frame('turn_context', {'model': 'test-model', 'cwd': str(cwd), 'turn_id': 'turn-1'})
            + frame('response_item', {'type': 'message', 'role': 'user',
                                     'content': [{'type': 'input_text', 'text': prompt}]})
            + frame('response_item', {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'Visible summary'}],
                                     'encrypted_content': 'opaque-native-content'})
            + frame('response_item', {'type': 'message', 'role': 'assistant',
                                     'content': [{'type': 'output_text', 'text': '{"next_step":"measure"}'}]})
            + frame('event_msg', {'type': 'task_complete', 'turn_id': 'turn-1'}))


def usage_metadata(thread=THREAD):
    counters = {'input_tokens': 100, 'cached_input_tokens': 20, 'cache_write_input_tokens': 0,
                'output_tokens': 8, 'reasoning_output_tokens': 3, 'total_tokens': 108}
    return {'thread_id': thread, 'session_id': thread, 'turn_id': 'turn-1',
            'root_turn_id': 'turn-1', 'response_id': 'response-1',
            'usage': counters, 'turn_token_usage': dict(counters), 'thread_token_usage': dict(counters)}


class NativeSnapshotTests(unittest.TestCase):
    def test_native_world_state_and_usage_are_fully_counted_audit_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            original = fixture(root)
            world = {'full': True, 'state': {'model': 'test-model',
                     'host_skills': {'body': 'Synthetic instructions remain counted, never source authority.'}}}
            raw = original + frame('world_state', world) + frame('token_usage_record', usage_metadata())
            before = snapshot_from_bytes(original, expected_thread_id=THREAD, expected_cwd=root)
            after = snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=root, expected_prefix=before)
            self.assertEqual(after['observable_history_bytes'], len(raw))
            self.assertEqual(after['prefix_sha256'], digest(raw))
            self.assertEqual(after['messages'], before['messages'])
            self.assertEqual(after['runtime_metadata_count'], 2)
            self.assertNotEqual(after['runtime_metadata_sha256'], before['runtime_metadata_sha256'])
            self.assertFalse(after['provider_context_verified'])
            self.assertIn('none', after['read_authority'])
            self.assertTrue(validate_rollout_extension(before, after)['same_owner'])

    def test_malformed_native_metadata_and_foreign_usage_owner_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            tails = [frame('world_state', {'full': 'true', 'state': {}}),
                     frame('world_state', {'full': True, 'state': []}),
                     frame('world_state', {'full': True, 'state': {}, 'type': 'function_call'}),
                     frame('token_usage_record', {**usage_metadata(), 'usage': {'input_tokens': -1, 'output_tokens': 1}}),
                     frame('token_usage_record', {**usage_metadata(), 'usage': {'input_tokens': True, 'output_tokens': 1}})]
            for tail in tails:
                with self.subTest(tail=tail), self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable'):
                    snapshot_from_bytes(fixture(root) + tail, expected_thread_id=THREAD, expected_cwd=root)
            for field in ('thread_id', 'session_id'):
                with self.subTest(field=field), self.assertRaisesRegex(ContextUnavailable, 'session_owner_mismatch'):
                    snapshot_from_bytes(fixture(root) + frame('token_usage_record',
                        {**usage_metadata(), field: OTHER}), expected_thread_id=THREAD, expected_cwd=root)

    def test_metadata_does_not_allow_native_tools_compaction_or_prefix_rewrite(self):
        with tempfile.TemporaryDirectory() as root:
            raw = fixture(root) + frame('world_state', {'full': False, 'state': {'model': 'test-model'}})
            before = snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=root)
            for tail in (frame('response_item', {'type': 'function_call', 'name': 'shell'}),
                         frame('compacted', {'message': 'Only a summary remains'}),
                         frame('world_state', {'type': 'compaction', 'full': False, 'state': {}})):
                with self.subTest(tail=tail), self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable'):
                    snapshot_from_bytes(raw + tail, expected_thread_id=THREAD, expected_cwd=root)
            with self.assertRaisesRegex(ContextUnavailable, 'session_prefix_mismatch'):
                snapshot_from_bytes(raw.replace(b'"full":false', b'"full":true'),
                    expected_thread_id=THREAD, expected_cwd=root, expected_prefix=before)

    def test_exact_owned_prefix_without_claiming_provider_visibility(self):
        with tempfile.TemporaryDirectory() as root:
            raw = fixture(root)
            value = snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=root)
            self.assertEqual(value['prefix_sha256'], digest(raw))
            self.assertEqual(value['prefix_bytes'], len(raw))
            self.assertEqual(len(value['messages']), 2)
            self.assertFalse(value['provider_context_verified'])
            self.assertFalse(value['compaction_supported'])
            self.assertIn('none', value['read_authority'])

    def test_owner_cwd_and_thread_are_independent_identity_checks(self):
        with tempfile.TemporaryDirectory() as root:
            raw = fixture(root)
            with self.assertRaisesRegex(ContextUnavailable, 'session_owner_mismatch'):
                snapshot_from_bytes(raw, expected_thread_id=OTHER, expected_cwd=root)
            with self.assertRaisesRegex(ContextUnavailable, 'session_owner_mismatch'):
                snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=Path(root)/'other')

    def test_native_extension_verifies_old_bytes_not_just_length(self):
        with tempfile.TemporaryDirectory() as root:
            raw = fixture(root)
            before = snapshot_from_bytes(raw, expected_thread_id=THREAD, expected_cwd=root)
            appended = raw + frame('response_item', {'type': 'message', 'role': 'user',
                                   'content': [{'type': 'input_text', 'text': 'Continue without repeating the experiment'}]})
            after = snapshot_from_bytes(appended, expected_thread_id=THREAD, expected_cwd=root, expected_prefix=before)
            self.assertTrue(validate_rollout_extension(before, after)['same_owner'])
            changed = appended.replace(b'First research question', b'Other research question')
            with self.assertRaisesRegex(ContextUnavailable, 'session_prefix_mismatch'):
                snapshot_from_bytes(changed, expected_thread_id=THREAD, expected_cwd=root, expected_prefix=before)
            with self.assertRaisesRegex(ContextUnavailable, 'session_prefix_mismatch'):
                snapshot_from_bytes(b''.join(raw.splitlines(keepends=True)[:-1]), expected_thread_id=THREAD, expected_cwd=root, expected_prefix=before)

    def test_truncated_or_invalid_native_tail_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            for bad in (fixture(root)[:-1], fixture(root)+b'{bad json}\n', fixture(root)+b'\n'):
                with self.assertRaisesRegex(ContextUnavailable, 'stream_incomplete'):
                    snapshot_from_bytes(bad, expected_thread_id=THREAD, expected_cwd=root)
            duplicate = fixture(root)+b'{"type":"event_msg","type":"response_item","payload":{}}\n'
            with self.assertRaisesRegex(ContextUnavailable, 'stream_incomplete'):
                snapshot_from_bytes(duplicate, expected_thread_id=THREAD, expected_cwd=root)

    def test_compaction_unknown_items_and_native_tools_require_reconstruction(self):
        with tempfile.TemporaryDirectory() as root:
            tails = [frame('compacted', {'message': 'Only a summary remains'}),
                     frame('response_item', {'type': 'compaction', 'encrypted_content': 'opaque'}),
                     frame('response_item', {'type': 'function_call', 'name': 'shell', 'arguments': '{}'}),
                     frame('future_runtime_record', {}),
                     frame('response_item', {'type': 'message', 'role': 'user',
                           'content': [{'type': 'input_image', 'image_url': 'data:...'}]})]
            for tail in tails:
                with self.assertRaisesRegex(ContextUnavailable, 'context_unverifiable'):
                    snapshot_from_bytes(fixture(root)+tail, expected_thread_id=THREAD, expected_cwd=root)

    def test_exact_filename_lookup_never_parses_unrelated_private_threads(self):
        with tempfile.TemporaryDirectory() as root:
            home=Path(root)/'codex'; cwd=Path(root)/'owned'; cwd.mkdir()
            directory=home/'sessions'/'2000'/'01'/'01'; directory.mkdir(parents=True)
            own=directory/f'rollout-2000-01-01-{THREAD}.jsonl'
            own.write_bytes(fixture(cwd))
            unrelated=directory/f'rollout-2000-01-01-{OTHER}.jsonl'
            unrelated.write_bytes(b'This is deliberately not parseable and must never be opened')
            self.assertEqual(find_owned_rollout(home, THREAD, cwd), own.resolve())
            self.assertEqual(read_native_snapshot(own, expected_thread_id=THREAD, expected_cwd=cwd)['native_session_path'], str(own.resolve()))
            other=(home/'archived_sessions'); other.mkdir()
            (other/own.name).write_bytes(own.read_bytes())
            with self.assertRaisesRegex(ContextUnavailable, 'found 2'):
                find_owned_rollout(home, THREAD, cwd)

    def test_symlinked_rollout_is_not_a_valid_ownership_proof(self):
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'original'; source.write_bytes(fixture(root))
            linked=Path(root)/'linked'; linked.symlink_to(source)
            with self.assertRaisesRegex(ContextUnavailable, 'symlink'):
                read_native_snapshot(linked, expected_thread_id=THREAD, expected_cwd=root)


class EffectiveContextTests(unittest.TestCase):
    def arguments(self):
        return {'session_id':'lab-session', 'owner':{'run_id':'r', 'branch_id':'main', 'role':'lab', 'agent_instance_id':'lab'},
                'max_input_bytes':100000, 'max_input_tokens':100000, 'output_reserve_tokens':64}

    def test_history_and_output_reserve_both_count_toward_admission(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot=snapshot_from_bytes(fixture(root), expected_thread_id=THREAD, expected_cwd=root)
            packet={'prompt':'Next step', 'response_schema':{'type':'object'}}
            first=build_effective_context(packet, **self.arguments())
            continued=build_effective_context(packet, snapshot=snapshot, **self.arguments())
            self.assertEqual(continued['manifest']['input_bytes']-first['manifest']['input_bytes'], len(fixture(root)))
            self.assertEqual(continued['manifest']['total_token_bound']-continued['manifest']['observable_input_token_bound'],64)
            byte_blocked=build_effective_context(packet,snapshot=snapshot,
                **{**self.arguments(),'max_input_bytes':continued['manifest']['input_bytes']-1})
            token_blocked=build_effective_context(packet,snapshot=snapshot,
                **{**self.arguments(),'max_input_tokens':continued['manifest']['total_token_bound']-1})
            self.assertEqual(byte_blocked['status'],'blocked_capacity')
            self.assertEqual(token_blocked['status'],'blocked_capacity')
            self.assertFalse(continued['manifest']['provider_context_verified'])
            self.assertFalse(continued['manifest']['retained_read_authority'])

    def test_exact_wire_bytes_and_local_revision_bind_manifest_identity(self):
        wire=b'{ "prompt": "hi", "response_schema": {} }'
        a=build_effective_context(wire,working_memory_revision=1,**self.arguments())
        b=build_effective_context(wire,working_memory_revision=2,**self.arguments())
        self.assertEqual(a['manifest']['submitted_input_hash'],digest(wire))
        self.assertNotEqual(a['effective_context_hash'],b['effective_context_hash'])
        c=build_effective_context({'prompt':'hi','response_schema':{}},working_memory_revision=1,**self.arguments())
        self.assertNotEqual(a['manifest']['submitted_input_hash'],c['manifest']['submitted_input_hash'])

    def test_registered_tokenizer_and_runtime_allowance_are_visible(self):
        result=build_effective_context({'prompt':'你好'},token_counter=lambda _:17,
                tokenizer_id='fixture-tokenizer-v1',runtime_overhead_tokens=10,runtime_overhead_bytes=40,
                **self.arguments())
        m=result['manifest']
        self.assertEqual(m['observable_input_token_bound'],27)
        self.assertEqual(m['tokenizer'],'fixture-tokenizer-v1')
        self.assertEqual(m['runtime_overhead_bytes'],40)
        with self.assertRaisesRegex(ValueError,'registered tokenizer'):
            build_effective_context({}, token_counter=lambda _:1, **self.arguments())

    def test_summary_and_old_ack_never_grant_current_read_authority(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot=snapshot_from_bytes(fixture(root),expected_thread_id=THREAD,expected_cwd=root)
            raw=page_evidence(make_evidence('raw','measured result',kind='raw_fact',source='fixture'))
            summary=page_evidence(make_evidence('summary','interpretation',kind='summary',source='model',summary_of=['raw']))
            rows=retained_source_candidates(snapshot,[{**raw,'status':'acked'},summary],
                    authorized_source_ids=['raw','summary'],current_versions={'raw':1,'summary':1})
            self.assertEqual(len(rows),1)
            self.assertFalse(rows[0]['current_read_authority'])
            self.assertEqual(rows[0]['presentation'],'retained_unverified')
            with self.assertRaisesRegex(ContextUnavailable,'retained_source_stale'):
                retained_source_candidates(snapshot,[raw],authorized_source_ids=['raw'],current_versions={'raw':2})
            with self.assertRaisesRegex(ContextUnavailable,'handoff_scope_denied'):
                retained_source_candidates(snapshot,[raw],authorized_source_ids=[],current_versions={'raw':1})
            tampered=copy.deepcopy(raw);tampered['content']='invented'
            with self.assertRaisesRegex(ContextUnavailable,'source_integrity_error'):
                retained_source_candidates(snapshot,[tampered],authorized_source_ids=['raw'],current_versions={'raw':1})


if __name__=='__main__':
    unittest.main()
