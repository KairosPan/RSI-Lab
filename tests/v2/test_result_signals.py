"""Semantic content deduplication without model or external execution."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from rsi_lab.controller import Controller
from rsi_lab.result_signals import ResultSignals
from rsi_lab.store import Store, digest


class ResultSignalTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'run')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={}, config={})
        runtime = SimpleNamespace(store=self.store, run_id='r',
            _mutate=lambda key, event, fn, payload: self.store.mutate('r', key, event, fn, payload))
        self.signals = ResultSignals(runtime)
        self.adapter = SimpleNamespace(adapter_id='probe', version='1', progress=lambda e, p: p['data'])

    def publish(self, eid, *, query='q', state='s', result='r', scores=0, info=1):
        description = {'query': query, 'state': state, 'result': result,
                       'new_scores': scores, 'information_units': info}
        fact = {'id': 'fact-' + eid, 'kind': 'raw_fact',
                'content': {'effect_id': eid, 'answer': description}, 'provenance': {'effect_id': eid}}
        self.controller.add_fact('r', fact['id'], fact['kind'], fact['content'], fact['provenance'], 'fact-' + eid)
        effect = {'id': eid, 'status': 'succeeded', 'result_ref': digest(fact['content'])}
        publication = {'facts': [fact], 'conclusions': [], 'data': description}
        return self.signals.record(effect, publication, self.adapter), effect, publication

    def test_receipt_identity_changes_do_not_create_new_information(self):
        first, _, _ = self.publish('one')
        second, effect, publication = self.publish('two')
        self.assertTrue(first['is_new_result'])
        self.assertFalse(second['is_new_result'])
        self.assertEqual(second['reason_code'], 'repeated_result')
        self.assertEqual(second['canonical_evidence_ids'], ['fact-one'])
        self.assertEqual(second['source_evidence_ids'], ['fact-two'])
        self.assertEqual(second['canonical_effect_id'], 'one')
        self.assertEqual(second['batch_index'], 1)
        before = self.store.verify_history('r')
        self.assertEqual(self.signals.record(effect, publication, self.adapter), second)
        self.assertEqual(self.store.verify_history('r'), before)

    def test_zero_new_scores_is_not_progress_even_when_receipt_is_unique(self):
        record, _, _ = self.publish('duplicate-score', scores=0, info=0)
        self.assertEqual(record['outcome'], 'no_result')
        self.assertEqual(record['reason_code'], 'no_semantic_progress')
        self.assertEqual(record['batch_index'], 0)
        novel, _, _ = self.publish('novel-score', result={'candidate': 'new', 'score': 0.4}, scores=1, info=0)
        self.assertEqual(novel['new_scores'], 1)
        self.assertTrue(novel['is_new_result'])

    def test_changed_state_or_answer_is_distinct_and_scoped_to_adapter(self):
        first, _, _ = self.publish('one')
        changed, _, _ = self.publish('two', state='changed')
        self.assertNotEqual(first['semantic_key'], changed['semantic_key'])
        self.assertTrue(changed['is_new_result'])
        self.adapter.adapter_id = 'other-probe'
        other, _, _ = self.publish('three')
        self.assertTrue(other['is_new_result'])

    def test_unsettled_or_malformed_progress_cannot_be_claimed(self):
        row, effect, publication = self.publish('one')
        with self.assertRaises(ValueError):
            self.signals.record({**effect, 'status': 'unknown'}, publication, self.adapter)
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bad = {**publication, 'data': {**publication['data'], 'new_scores': value}}
                self.signals.record({**effect, 'id': 'bad'}, bad, self.adapter)


if __name__ == '__main__':
    unittest.main()
