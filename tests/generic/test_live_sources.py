"""Real local deliveries and durable fault recovery; no model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_domains.docking.live_sources import deliver_pending, freeze_sources, source_contracts
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store


class LiveSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'original.json'
        self.source.write_text('{"observed": 17}\n')
        self.store = Store(self.root / 'ledger'); self.addCleanup(self.store.close)
        self.sources = freeze_sources(self.store, {'original': self.source})
        self.controller = Controller(self.store)
        self.controller.initialize('arbitrary-run', budgets={
            'tool_calls': {'limit': 20, 'hard': True},
            'cpu_seconds': {'limit': 20, 'hard': False},
            'wall_seconds': {'limit': 20, 'hard': False}},
            config={'authorized_sources': ['original'], 'authorized_source_contracts': source_contracts(self.sources)})
        self.runtime = Runtime(self.store, 'arbitrary-run')
        self.controller.add_fact('arbitrary-run', 'fact', 'raw_fact', {'input': 'given'}, {'source': 'fixture'}, 'fact')
        self.controller.open_issue('arbitrary-run', 'issue', ['missing-original'], 'Need original', 'fixture',
            ['fact'], ['It exists'], ['Read it'], 'issue')

    def request(self, **changes):
        spec = dict(source_id='original', source_version=1, object_ids=['original.json'],
            fields=['original_text', 'provenance'], query='Read the exact frozen original', max_pages=1, max_bytes=20000)
        spec.update(changes)
        return self.controller.request('arbitrary-run', 'issue', 'request', spec['query'], spec['source_id'],
            {key: spec[key] for key in ('object_ids', 'fields', 'max_pages', 'max_bytes')}, 'request',
            source_version=spec['source_version'], query_spec=spec)

    def test_frozen_original_delivered_and_charged_exactly_once(self):
        self.request()
        self.source.write_text('{"observed": "later file"}')
        outcome = deliver_pending(self.runtime, self.sources)[0]
        self.assertEqual(outcome['status'], 'delivered')
        self.assertEqual(outcome['content']['original_text'], '{"observed": 17}\n')
        self.assertFalse(outcome['query_answer_verified'])
        self.assertTrue(outcome['request_scope_verified'])
        self.assertEqual(deliver_pending(self.runtime, self.sources), [])
        self.assertEqual(self.store.get('arbitrary-run', 'budget', 'tool_calls')['spent'], 1)
        self.assertEqual(len(self.store.list('arbitrary-run', 'effect')), 1)
        self.store.verify_history('arbitrary-run')

    def test_unavailable_requests_are_not_substituted_or_charged_again(self):
        self.request(object_ids=['foreign.sdf'])
        first = deliver_pending(self.runtime, self.sources)[0]
        second = deliver_pending(self.runtime, self.sources)[0]
        self.assertEqual(first, second)
        self.assertEqual(first['reason_code'], 'requested_objects_unavailable')
        self.assertFalse(first['delivered'])
        self.assertEqual(self.store.get('arbitrary-run', 'request', 'request')['status'], 'created')
        self.assertEqual(self.store.list('arbitrary-run', 'delivery'), [])
        self.assertEqual(self.store.get('arbitrary-run', 'budget', 'tool_calls')['spent'], 1)

    def test_byte_capacity_failure_keeps_original_pending(self):
        self.request(max_bytes=10)
        result = deliver_pending(self.runtime, self.sources)[0]
        self.assertEqual(result['reason_code'], 'source_exceeds_request_capacity')
        self.assertGreater(result['required_bytes'], 10)
        self.assertEqual(self.store.list('arbitrary-run', 'delivery'), [])

    def test_wrong_hash_in_query_cannot_be_fulfilled_by_same_named_source(self):
        self.request(query='Retrieve original at sha256 ' + 'a' * 64)
        result = deliver_pending(self.runtime, self.sources)[0]
        self.assertEqual(result['reason_code'], 'requested_artifact_hash_unavailable')
        self.assertEqual(self.store.list('arbitrary-run', 'delivery'), [])

    def test_unsupported_semantic_fields_require_real_producer(self):
        self.request(fields=['scientific_validity'])
        result = deliver_pending(self.runtime, self.sources)[0]
        self.assertEqual(result['reason_code'], 'requested_fields_unsupported')
        self.assertFalse(result['query_answer_verified'])

    def test_exact_source_revision_required(self):
        self.request(source_version=2)
        result = deliver_pending(self.runtime, self.sources)[0]
        self.assertEqual(result['reason_code'], 'source_version_unavailable')

    def test_delivery_recovery_uses_saved_receipt_without_second_lookup(self):
        self.request()
        with patch.object(self.runtime, 'ingest_delivery', side_effect=KeyboardInterrupt('after saved receipt')):
            with self.assertRaises(KeyboardInterrupt):
                deliver_pending(self.runtime, self.sources)
        self.assertEqual(len(self.store.list('arbitrary-run', 'effect')), 1)
        resumed = Runtime(self.store, 'arbitrary-run')
        with patch('rsi_domains.docking.live_sources._lookup', side_effect=AssertionError('Must not execute twice')):
            result = deliver_pending(resumed, self.sources)[0]
        self.assertEqual(result['status'], 'delivered')
        self.assertEqual(self.store.get('arbitrary-run', 'budget', 'tool_calls')['spent'], 1)

    def test_unknown_lookup_is_not_resent(self):
        self.request()
        original = self.controller.dispatch_effect
        def interrupted(run, effect_id, executor):
            return original(run, effect_id, lambda effect, raw: {
                'status': 'unknown', 'actual_costs': {}, 'reason': 'fixture lost receipt'})
        with patch.object(self.runtime.controller, 'dispatch_effect', side_effect=interrupted):
            result = deliver_pending(self.runtime, self.sources)[0]
        self.assertEqual(result['status'], 'blocked_external')
        with patch.object(self.runtime.controller, 'dispatch_effect', side_effect=AssertionError('Must not resend')):
            self.assertEqual(deliver_pending(self.runtime, self.sources)[0]['status'], 'blocked_external')
        budget = self.store.get('arbitrary-run', 'budget', 'tool_calls')
        self.assertEqual(budget['spent'], 0)
        self.assertEqual(budget['reserved'], 1)

    def test_metadata_mismatch_cannot_masquerade_as_original(self):
        self.request()
        changed = [{**self.sources[0], 'sha256': '0' * 64}]
        result = deliver_pending(self.runtime, changed)[0]
        self.assertEqual(result['reason_code'], 'source_archive_unavailable')
        self.assertEqual(self.store.list('arbitrary-run', 'delivery'), [])

    def test_freeze_rejects_nontext_and_duplicate_contracts(self):
        binary = self.root / 'binary'; binary.write_bytes(b'\xff\xfe')
        with self.assertRaises(UnicodeDecodeError):
            freeze_sources(self.store, {'binary': binary})
        with self.assertRaises(ValueError):
            source_contracts(self.sources + self.sources)


if __name__ == '__main__':
    unittest.main()
