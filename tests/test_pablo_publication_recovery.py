"""Scientific publication recovery with a synthetic oracle and the real ledger.

These tests need only the standard library. Molecular canonicalization and
scores are fixtures; cancellation, adapter dispatch, memory and recovery are not.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_domains.pablo import adapter, oracle
from rsi_domains.pablo.research import project_publication
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from tests import test_pablo_adapter as fixtures


class PabloPublicationRecoveryTests(unittest.TestCase):
    inputs = fixtures.PabloAdapterTests.inputs
    diagnostic = fixtures.PabloAdapterTests.diagnostic
    evaluate = fixtures.PabloAdapterTests.evaluate

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        for mock in (patch.object(oracle, 'canonicalize', side_effect=lambda value: value),
                     patch.object(oracle, 'fingerprint', return_value='synthetic-fingerprint')):
            mock.start()
            self.addCleanup(mock.stop)
        self.run_id = 'publication-recovery'
        initial_ref = self.store.put_blob({'candidates': ['CCO'], 'scores': [0.1]})
        self.controller = Controller(self.store)
        self.controller.initialize(self.run_id, max_external=1,
            budgets={name: {'limit': limit, 'hard': name.endswith('_calls')} for name, limit in
                     {'model_calls': 20, 'tool_calls': 20, 'tokens': 100000,
                      'wall_seconds': 10000, 'cpu_seconds': 10000}.items()},
            config={'execution_mode': 'test', 'pablo_run_root': str(self.root),
                    'pablo_initial_ref': initial_ref, 'pablo_max_new_evaluations': 2,
                    'research_features': {'scientific_memory': True}, 'max_open_reviews': 0})
        self.runtime = Runtime(self.store, self.run_id, adapters=[adapter.make_adapter()],
                               scientific_identity_provider=project_publication)
        self.counter = 0

    def prepare_score(self, candidates, key):
        inputs, work = self.diagnostic(candidates)
        packet = adapter.prepare(inputs)
        return self.controller.prepare_effect(self.run_id, work['id'], adapter.EFFECT_KIND,
            packet, adapter.reservations(packet), key,
            metadata={'adapter_id': adapter.ADAPTER_ID, 'adapter_version': adapter.VERSION})

    def test_cancelled_prepared_score_does_not_block_memory_or_lab_continuation(self):
        prepared = self.prepare_score(['C'], 'prepared-score')
        cancelled = self.controller.cancel_prepared(self.run_id, prepared['id'],
                                                   'Replan before scoring', 'cancel-score')
        self.assertEqual(cancelled['status'], 'failed_definite')
        self.assertTrue(json.loads(self.store.read_blob(cancelled['result_ref']))['never_dispatched'])
        with patch.object(oracle, 'score', return_value=.2) as score:
            completed = self.evaluate(['CC'])
            recovered = self.runtime.recover_adapters()
            self.runtime.recover_adapters()
        self.assertEqual(score.call_count, 1)
        self.assertEqual(completed['effect']['status'], 'succeeded')
        self.assertEqual(recovered[0]['status'], 'published')
        self.assertEqual(adapter.read_progress(self.store, self.run_id)['new_evaluations'], 1)
        memory = self.runtime.scientific_memory
        experiences = memory.read_history(kind='method_experience')
        self.assertEqual(len(experiences), 1)
        self.assertEqual(experiences[0]['content']['best_before'], .1)
        self.assertEqual(experiences[0]['content']['best_after'], .2)
        identities = memory.lookup_exact(['CC'], {'evaluator_fingerprint': None,
            'identity_rule': 'canonical_isomeric_SMILES', 'score_direction': 'higher'})
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]['freshness'], 'current')
        continuations = [work for work in self.store.list(self.run_id, 'work', kind='lab')
                         if work['inputs'].get('source_effect_id') == completed['effect']['id']]
        self.assertEqual(len(continuations), 1)
        self.assertEqual(self.store.get(self.run_id, 'budget', 'tool_calls')['spent'], 1)
        self.store.verify_history(self.run_id)

    def test_other_attempts_cannot_borrow_a_cancelled_effect_receipt(self):
        prepared = self.prepare_score(['C'], 'first-score')
        first = self.controller.cancel_prepared(self.run_id, prepared['id'], 'First replan', 'cancel-first')
        prepared = self.prepare_score(['CC'], 'second-score')
        second = self.controller.cancel_prepared(self.run_id, prepared['id'], 'Second replan', 'cancel-second')
        self.store.mutate(self.run_id, 'wrong-cancellation-receipt', 'fixture',
            lambda tx: tx.update('effect', first['id'], {'result_ref': second['result_ref']}))
        with patch.object(oracle, 'score', return_value=.2):
            with self.assertRaisesRegex(ValueError, 'unsettled predecessor'):
                self.evaluate(['CCC'])
        self.assertEqual(self.runtime.scientific_memory.read_history(kind='method_experience'), [])

    def test_dispatched_failure_is_not_reclassified_as_a_prepared_cancellation(self):
        # A forged Lab continuation is an attempted and definitely failed
        # adapter invocation, not a controller cancellation before dispatch.
        with patch.object(oracle, 'score', return_value=.2) as score:
            failed = self.evaluate(['C'], mode='discovery')
            self.assertEqual(failed['effect']['status'], 'failed_definite')
            score.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'unsettled predecessor'):
                self.evaluate(['CC'])
        self.assertEqual(self.runtime.scientific_memory.read_history(kind='method_experience'), [])


if __name__ == '__main__':
    unittest.main()
