import json
import unittest
from unittest.mock import patch

from tests import test_pablo_adapter as fixtures
from rsi_domains.pablo import lookup, oracle, adapter


class PabloLookupTests(unittest.TestCase):
    setUp = fixtures.PabloAdapterTests.setUp
    tearDown = fixtures.PabloAdapterTests.tearDown
    inputs = fixtures.PabloAdapterTests.inputs
    diagnostic = fixtures.PabloAdapterTests.diagnostic
    evaluate = fixtures.PabloAdapterTests.evaluate
    def test_lookup_full_history_canonical_aliases_and_repeat(self):
        with patch.object(oracle, 'score', return_value=0.25):
            self.evaluate(['CCC'])
        with patch.object(oracle, 'score', side_effect=AssertionError('Lookup must not score')):
            packet = lookup.prepare(self.inputs(['OCC', 'CCC', 'C', 'C', 'bad']))
        self.assertEqual([r['status'] for r in packet['rows']],
                         ['already_evaluated', 'already_evaluated', 'not_seen_at_snapshot', 'duplicate_in_query', 'invalid'])
        self.assertEqual(packet['rows'][0]['score'], 0.1)
        self.assertEqual(packet['rows'][1]['score'], 0.25)
        self.assertEqual(packet['rows'][1]['origin'], 'current_segment')
        self.assertEqual(adapter.read_progress(self.store, self.run_id)['new_evaluations'], 1)

    def test_lookup_respects_pending_and_no_fake_effect(self):
        inputs, work = self.diagnostic(['C'])
        packet = adapter.prepare(inputs)
        self.controller.prepare_effect(self.run_id, work['id'], adapter.EFFECT_KIND, packet,
            adapter.reservations(packet), 'pending-lookup', metadata={'adapter_id': adapter.ADAPTER_ID})
        answer = lookup.prepare(self.inputs(['C']))
        self.assertEqual(answer['rows'][0]['status'], 'pending_or_unknown')
        receipt = lookup.execute({'id': 'fake'}, json.dumps(answer).encode())
        self.assertEqual(receipt['status'], 'failed_definite')

    def test_registered_lookup_publishes_and_recovers_without_score(self):
        from rsi_lab.runtime import Runtime
        runtime = Runtime(self.store, self.run_id, adapters=[lookup.make_adapter()])
        inputs = self.inputs(['OCC', 'C'])
        with patch.object(oracle, 'score', side_effect=AssertionError('Lookup must not score')):
            result = runtime.execute_adapter(lookup.ADAPTER_ID, inputs, trigger_id='lookup')
            runtime.recover_adapters()
        self.assertEqual(result['effect']['status'], 'succeeded')
        self.assertEqual(result['result']['rows'][0]['status'], 'already_evaluated')
        self.assertEqual(len(self.store.list(self.run_id, 'fact')), 1)
