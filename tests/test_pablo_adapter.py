"""Real local scorer checks and synthetic ledger/crash boundaries; no model calls."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode
from rsi_domains.pablo import adapter, oracle


_HAS_PABLO_DEPENDENCIES = all(
    importlib.util.find_spec(name) is not None for name in ('guacamol', 'rdkit', 'numpy', 'scipy'))
_PABLO_DEPENDENCY_REASON = 'PABLO adapter tests require the optional guacamol, rdkit, numpy, and scipy packages'


@unittest.skipUnless(_HAS_PABLO_DEPENDENCIES, _PABLO_DEPENDENCY_REASON)
class PabloAdapterTests(unittest.TestCase):
    def setUp(self):
        # Lookup tests reuse this fixture without inheriting the test class.
        if not _HAS_PABLO_DEPENDENCIES:
            self.skipTest(_PABLO_DEPENDENCY_REASON)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / 'run'
        self.store = Store(self.root)
        self.run_id = 'test'
        initial_ref = self.store.put_blob({'candidates': ['CCO'], 'scores': [0.1]})
        self.controller = Controller(self.store)
        self.controller.initialize(self.run_id, max_external=1,
            budgets={name: {'limit': limit, 'hard': name in {'model_calls', 'tool_calls'}}
                     for name, limit in {'model_calls': 20, 'tool_calls': 100, 'tokens': 100000,
                                         'wall_seconds': 10000, 'cpu_seconds': 10000}.items()},
            config={'execution_mode': 'test', 'pablo_run_root': str(self.root),
                    'pablo_initial_ref': initial_ref, 'pablo_max_new_evaluations': 2,
                    'max_open_reviews': 0})
        self.runtime = Runtime(self.store, self.run_id, adapters=[adapter.make_adapter()])
        self.counter = 0

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def inputs(self, candidates):
        return {'run_root': str(self.root), 'run_id': self.run_id, 'candidates': candidates}

    def diagnostic(self, candidates, mode='lab'):
        self.counter += 1
        serial = str(self.counter)
        inputs = self.inputs(candidates)
        payload = {'capability_id': adapter.ADAPTER_ID, 'capability_version': adapter.VERSION,
                   'inputs_json': json.dumps(inputs)}
        command = {'mode': mode, 'action': 'request_diagnostic', 'payload': payload, 'issue_id': None}
        origin_work = self.controller.enqueue(self.run_id, 'lab' if mode == 'lab' else 'meta_discovery',
                                              'origin:' + serial, 'origin-work:' + serial)
        zero = {'model_calls': 0, 'tool_calls': 0, 'tokens': 0, 'wall_seconds': 0, 'cpu_seconds': 0}
        origin = self.controller.prepare_effect(self.run_id, origin_work['id'],
            'lab' if mode == 'lab' else 'meta', {'fixture': serial}, zero, 'origin-effect:' + serial,
            metadata={'mode': mode})
        origin = self.controller.dispatch_effect(self.run_id, origin['id'],
            lambda effect, raw: {'status': 'succeeded', 'actual_costs': zero, 'response': {'command': command}})
        pid = 'proposal:' + serial
        self.store.mutate(self.run_id, 'proposal:' + serial, 'fixture_proposal',
            lambda tx: tx.insert('proposal', pid, {'effect_id': origin['id'], 'issue_id': None,
                                                  'status': 'applied', 'command': command}))
        work = self.controller.enqueue(self.run_id, 'diagnostic', 'diagnostic:' + serial, 'diagnostic:' + serial,
            inputs={**payload, 'lab_continuation': True, 'source_proposal_id': pid})
        return inputs, work

    def evaluate(self, candidates, mode='lab'):
        inputs, work = self.diagnostic(candidates, mode)
        return self.runtime.execute_adapter(adapter.ADAPTER_ID, inputs,
                                            trigger_id=work['id'], work_id=work['id'])

    def test_initial_same_batch_dedup_and_budget_clipping(self):
        packet = adapter.prepare(self.inputs(['OCC', 'C', 'C', 'CC', 'CCC', 'not a molecule']))
        self.assertEqual([row['status'] for row in packet['filters']],
                         ['duplicate', 'accepted', 'duplicate', 'accepted', 'budget_clipped', 'invalid'])
        with patch.object(oracle, 'score', side_effect=[0.2, 0.3]) as scorer:
            result = self.evaluate(['OCC', 'C', 'C', 'CC', 'CCC', 'not a molecule'])
        self.assertEqual(scorer.call_count, 2)
        self.assertEqual(result['effect']['status'], 'succeeded')
        self.assertEqual([r['slot_id'] for r in result['result']['evaluations']],
                         ['evaluation-001', 'evaluation-002'])
        progress = adapter.read_progress(self.store, self.run_id)
        self.assertEqual(progress['new_evaluations'], 2)
        self.assertEqual(progress['remaining_evaluations'], 0)
        self.assertEqual(progress['candidates'], ['CCO', 'C', 'CC'])
        self.assertEqual(adapter.prepare(self.inputs(['CCC']))['accepted'], [])

    def test_pending_packets_reserve_identity_and_budget(self):
        inputs, work = self.diagnostic(['C'])
        packet = adapter.prepare(inputs)
        self.controller.prepare_effect(self.run_id, work['id'], adapter.EFFECT_KIND, packet,
            adapter.reservations(packet), 'pending', metadata={'adapter_id': adapter.ADAPTER_ID,
                                                            'adapter_version': adapter.VERSION})
        next_packet = adapter.prepare(self.inputs(['C', 'CC', 'CCC']))
        self.assertEqual([row['status'] for row in next_packet['filters']],
                         ['duplicate', 'accepted', 'budget_clipped'])
        self.assertEqual(next_packet['held_evaluations'], 1)

    def test_original_target_recomputation(self):
        fixtures = [('CCC(C)(C)Br', 2.9931273969774224e-05),
                    ('CCCN(CCc1cccc(-c2ccccc2)c1)C(=O)C1OC(C(=O)O)=CC(N)C1NC(C)=O', 0.4503301042537604)]
        for molecule, expected in fixtures:
            self.assertLessEqual(abs(oracle.score(oracle.canonicalize(molecule)) - expected), 1e-7)

    def test_recover_publication_and_repeat_dispatch_never_rescore(self):
        inputs, work = self.diagnostic(['C'])
        with patch.object(oracle, 'score', return_value=0.2) as scorer:
            first = self.runtime.execute_adapter(adapter.ADAPTER_ID, inputs, trigger_id=work['id'], work_id=work['id'])
            with self.assertRaisesRegex(ValueError, 'Idempotency key reused'):
                self.runtime.execute_adapter(adapter.ADAPTER_ID, inputs, trigger_id=work['id'], work_id=work['id'])
            recovered = self.runtime.recover_adapters()
        self.assertEqual(scorer.call_count, 1)
        self.assertEqual(first['effect']['status'], 'succeeded')
        self.assertEqual(recovered[0]['status'], 'published')
        self.assertEqual(adapter.read_progress(self.store, self.run_id)['new_evaluations'], 1)

    def test_inner_receipt_recovers_after_outer_dispatch_interruption(self):
        inputs, work = self.diagnostic(['C'])
        real_execute = adapter._execute
        def interrupted(current, raw):
            real_execute(current, raw)
            raise RuntimeError('simulated loss before outer receipt')
        with patch.object(oracle, 'score', return_value=0.2) as scorer:
            with patch.object(adapter, '_execute', side_effect=interrupted):
                result = self.runtime.execute_adapter(adapter.ADAPTER_ID, inputs,
                                                      trigger_id=work['id'], work_id=work['id'])
            effect = result['effect']
            self.assertEqual(effect['status'], 'unknown')
            receipt = json.loads((self.root / 'pablo_evaluations' / effect['id'] / 'receipt.json').read_text())
            self.assertEqual(receipt['result']['packet_sha256'], effect['packet_hash'])
            journal = self.root / 'pablo_evaluations' / effect['id'] / 'evaluations.jsonl'
            self.assertEqual(receipt['execution']['journal_sha256'], digest(journal.read_bytes()))
            receipt['reconciliation'] = {'effect_id': effect['id'], 'provenance': 'verified local terminal journal'}
            self.controller.reconcile(self.run_id, effect['id'], receipt, 'recover-local-terminal')
            self.runtime.recover_adapters()
        self.assertEqual(scorer.call_count, 1)
        self.assertEqual(adapter.read_progress(self.store, self.run_id)['new_evaluations'], 1)

    def test_successive_batches_preserve_slot_and_canonical_history(self):
        with patch.object(oracle, 'score', side_effect=[0.2, 0.3]) as scorer:
            first = self.evaluate(['C'])
            second = self.evaluate(['C', 'CC'])
        self.assertEqual(scorer.call_count, 2)
        self.assertEqual(first['result']['evaluations'][0]['slot_id'], 'evaluation-001')
        self.assertEqual(second['result']['evaluations'][0]['slot_id'], 'evaluation-002')
        self.assertEqual(second['result']['filter_counts']['duplicate'], 1)
        self.assertEqual(adapter.read_progress(self.store, self.run_id)['remaining_evaluations'], 0)

    def test_meta_cannot_score_even_with_forged_lab_continuation(self):
        with patch.object(oracle, 'score') as scorer:
            result = self.evaluate(['C'], mode='discovery')
        scorer.assert_not_called()
        self.assertEqual(result['effect']['status'], 'failed_definite')
        self.assertEqual(adapter.read_progress(self.store, self.run_id)['new_evaluations'], 0)

    def test_copied_ledger_root_is_rejected(self):
        duplicate = Path(self.temporary.name).resolve() / 'copy'
        self.store.db.execute('PRAGMA wal_checkpoint(FULL)')
        shutil.copytree(self.root, duplicate)
        with self.assertRaisesRegex(ValueError, 'frozen host binding'):
            adapter.prepare({**self.inputs(['C']), 'run_root': str(duplicate)})
        self.assertFalse((duplicate / 'pablo_evaluations').exists())

    def test_wrong_effect_binding_is_rejected_before_scoring(self):
        packet = adapter.prepare(self.inputs(['C']))
        with patch.object(oracle, 'score') as scorer:
            receipt = adapter.execute({'id': 'invented', 'status': 'dispatching', 'run_id': self.run_id,
                                       'kind': adapter.EFFECT_KIND}, encode(packet))
        scorer.assert_not_called()
        self.assertEqual(receipt['status'], 'failed_definite')

    def test_partial_journal_is_unknown_and_never_replayed(self):
        inputs, work = self.diagnostic(['C', 'CC'])
        with patch.object(oracle, 'score', side_effect=[0.2, RuntimeError('interrupted')]) as scorer:
            result = self.runtime.execute_adapter(adapter.ADAPTER_ID, inputs, trigger_id=work['id'], work_id=work['id'])
        self.assertEqual(result['effect']['status'], 'unknown')
        self.assertEqual(scorer.call_count, 2)
        journal = self.root / 'pablo_evaluations' / result['effect']['id'] / 'evaluations.jsonl'
        self.assertEqual(len(journal.read_text().splitlines()), 1)
        with patch.object(oracle, 'score') as scorer:
            self.controller.recover(self.run_id)
        scorer.assert_not_called()
        pending = adapter.prepare(self.inputs(['C', 'CC', 'CCC']))
        self.assertEqual(pending['accepted'], [])
        self.assertEqual(pending['held_evaluations'], 2)

    def test_success_with_missing_cost_blocks_progress_and_scoring(self):
        inputs, work = self.diagnostic(['C'])
        packet = adapter.prepare(inputs)
        effect = self.controller.prepare_effect(self.run_id, work['id'], adapter.EFFECT_KIND, packet,
            adapter.reservations(packet), 'missing-cost', metadata={'adapter_id': adapter.ADAPTER_ID,
                                                                 'adapter_version': adapter.VERSION})
        def incomplete_cost(current, raw):
            receipt = adapter.execute(current, raw)
            receipt['actual_costs'].pop('cpu_seconds')
            return receipt
        with patch.object(oracle, 'score', return_value=0.2):
            effect = self.controller.dispatch_effect(self.run_id, effect['id'], incomplete_cost)
        self.assertEqual(effect['status'], 'succeeded')
        self.assertTrue(effect['accounting_unknown'])
        with self.assertRaisesRegex(ValueError, 'unresolved accounting'):
            adapter.read_progress(self.store, self.run_id)
        with self.assertRaisesRegex(ValueError, 'unresolved accounting'):
            adapter.prepare(self.inputs(['CC']))


if __name__ == '__main__':
    unittest.main()
