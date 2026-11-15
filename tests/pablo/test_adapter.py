"""Offline port checks using only molecules from the original initial CSV.

Run with the PABLO scientific environment; no model or network calls occur.
"""
import csv
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from rsi_domains.pablo import adapter, lookup, oracle


REPO = Path(__file__).resolve().parents[2]
INITIAL_CSV = Path(os.environ.get('PABLO_INITIAL_CSV', str(
    REPO.parent / 'pablo-meta-experiment/upstream/data/guacamol_init_data.csv')))
INITIAL_SHA256 = 'ef8b0dd3cda1c3b3f520e21a311d94f0ef324e9ab1bc3ab2385e3b4ee4e5a6c1'
READY = INITIAL_CSV.is_file() and all(importlib.util.find_spec(name) is not None
    for name in ('rdkit', 'guacamol', 'numpy', 'scipy'))


@unittest.skipUnless(READY, 'Original initial CSV and optional PABLO scientific dependencies required')
class PabloAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if digest(INITIAL_CSV.read_bytes()) != INITIAL_SHA256:
            raise AssertionError('Original initialization source changed')
        with INITIAL_CSV.open() as stream:
            rows = list(csv.DictReader(stream))[:100]
        cls.candidates = [oracle.canonicalize(row['smile']) for row in rows]
        cls.scores = [float(row['adip']) for row in rows]
        if len(set(cls.candidates)) != 100 or None in cls.candidates:
            raise AssertionError('Expected 100 unique canonical original molecules')

    def runtime(self, *, holdout=False, maximum=200):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / 'run')
        self.addCleanup(store.close)
        start = 3 if holdout else 0
        initial = {'candidates': self.candidates[start:], 'scores': self.scores[start:]}
        config = {'goal': 'Offline scorer integration fixture, not a research run',
            'pablo_run_root': str(store.root), 'pablo_initial_ref': store.put_blob(initial),
            'pablo_max_new_evaluations': maximum, 'pablo_oracle_fingerprint': oracle.fingerprint()}
        budgets = {key: {'limit': value, 'hard': key.endswith('_calls')} for key, value in {
            'model_calls': 10, 'tool_calls': 10, 'tokens': 1000000,
            'wall_seconds': 1000, 'cpu_seconds': 1000}.items()}
        Controller(store).initialize('r', budgets=budgets, max_external=1, config=config)
        return Runtime(store, 'r', adapters=[adapter.make_adapter(), lookup.make_adapter()])

    def inputs(self, runtime, candidates):
        return {'run_root': str(runtime.store.root), 'run_id': 'r', 'candidates': candidates}

    def lab_request(self, runtime, capability, candidates):
        inputs = self.inputs(runtime, candidates)
        work = runtime.enqueue_lab('fixture-' + capability)
        response = {'command': {'mode': 'lab', 'action': 'request_diagnostic',
            'issue_id': None, 'expected_versions': runtime.versions(None), 'evidence_ids': [],
            'payload': {'capability_id': capability, 'capability_version': '1.0',
                'inputs_json': json.dumps(inputs)}}, 'working_memory_delta': json.dumps({
                    'rationale_summaries': [{'kind': 'literature_decision', 'decision': 'local_work',
                        'reason': 'Offline fixture verifies a registered local scoring operation.',
                        'evidence_ids': [], 'related_task_ids': []}]})}

        def fixture_model(effect, raw):
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1}, 'response': response,
                'sent_packet_sha256': digest(raw),
                'sent_prompt_sha256': digest(json.loads(raw)['prompt'].encode())}

        emitted = runtime.run_meta(work['id'], fixture_model, {'model_calls': 1})
        self.assertEqual(emitted['proposal']['status'], 'applied', emitted)
        diagnostic = next(work for work in runtime.controller.ready('r')
                          if work['kind'] == 'diagnostic')
        return runtime.execute_adapter(capability, inputs,
            trigger_id=diagnostic['id'], work_id=diagnostic['id'])

    def test_original_three_scores_through_current_runtime_and_replay(self):
        # This isolated fixture holds out THREE EXISTING initial molecules. It
        # does not generate candidates or inherit an older research trajectory.
        runtime = self.runtime(holdout=True)
        with patch.object(oracle, 'score', wraps=oracle.score) as scoring:
            result = self.lab_request(runtime, adapter.ADAPTER_ID, self.candidates[:3])
            self.assertEqual(result['effect']['status'], 'succeeded', result)
            self.assertEqual(scoring.call_count, 3)
            for actual, expected in zip(result['result']['evaluations'], self.scores[:3]):
                self.assertAlmostEqual(actual['score'], expected, delta=1e-12)
            replay = Runtime(runtime.store, 'r', adapters=[adapter.make_adapter(), lookup.make_adapter()])
            replay.recover_adapters()
            self.assertEqual(scoring.call_count, 3, 'Receipt publication must not re-score')
        progress = adapter.read_progress(runtime.store, 'r')
        self.assertEqual((progress['initial_count'], progress['new_evaluations']), (97, 3))
        self.assertEqual(progress['remaining_evaluations'], 197)
        facts = runtime.store.list('r', 'fact')
        self.assertEqual(len([fact for fact in facts if fact['id'].startswith('pablo_result_')]), 1)
        self.assertTrue(any(work['kind'] == 'lab' and work['inputs'].get('source_effect_id') ==
                            result['effect']['id'] for work in runtime.controller.ready('r')))
        runtime.store.verify_history('r')

    def test_host_cannot_bypass_applied_lab_scoring_request(self):
        runtime = self.runtime(holdout=True)
        with patch.object(oracle, 'score', side_effect=AssertionError('Must not score')) as scoring:
            result = runtime.execute_adapter(adapter.ADAPTER_ID,
                self.inputs(runtime, self.candidates[:1]), trigger_id='unattributed-host-call')
        self.assertEqual(result['effect']['status'], 'failed_definite')
        self.assertEqual(scoring.call_count, 0)
        self.assertEqual(adapter.read_progress(runtime.store, 'r')['new_evaluations'], 0)

    def test_initial_duplicates_and_invalid_strings_do_not_consume_slots(self):
        runtime = self.runtime()
        with patch.object(oracle, 'score', side_effect=AssertionError('Must not score')) as scoring:
            result = self.lab_request(runtime, adapter.ADAPTER_ID, [self.candidates[0], ''])
        self.assertEqual(result['effect']['status'], 'succeeded', result)
        self.assertEqual(scoring.call_count, 0)
        self.assertEqual(result['result']['filter_counts'],
            {'accepted': 0, 'invalid': 1, 'duplicate': 1, 'budget_clipped': 0})
        self.assertEqual(result['result']['remaining_evaluations'], 200)

    def test_lookup_returns_exact_snapshot_without_oracle_calls(self):
        runtime = self.runtime(holdout=True)
        with patch.object(oracle, 'score', side_effect=AssertionError('Lookup must not score')):
            result = self.lab_request(runtime, lookup.ADAPTER_ID,
                [self.candidates[3], self.candidates[0], self.candidates[0], ''])
        self.assertEqual(result['effect']['status'], 'succeeded', result)
        rows = result['result']['rows']
        self.assertEqual([row['status'] for row in rows],
            ['already_evaluated', 'not_seen_at_snapshot', 'duplicate_in_query', 'invalid'])
        self.assertEqual(rows[0]['score'], self.scores[3])
        self.assertEqual(result['result']['as_of_observations'], 97)

    def test_interrupted_score_preserves_unknown_slot_and_does_not_replay(self):
        runtime = self.runtime(holdout=True)
        with patch.object(oracle, 'score', side_effect=RuntimeError('Offline interruption fixture')) as scoring:
            result = self.lab_request(runtime, adapter.ADAPTER_ID, self.candidates[:1])
            self.assertEqual(result['effect']['status'], 'unknown', result)
            runtime.recover_adapters()
            self.assertEqual(scoring.call_count, 1)
        packet = adapter.prepare(self.inputs(runtime, self.candidates[:2]))
        self.assertEqual(packet['held_evaluations'], 1)
        self.assertEqual(packet['pending_effect_ids'], [result['effect']['id']])
        self.assertEqual([row['status'] for row in packet['filters']], ['duplicate', 'accepted'])

    def test_input_contracts_and_private_objective_boundary(self):
        runtime = self.runtime()
        for registered, maximum in ((adapter.make_adapter(), 20), (lookup.make_adapter(), 100)):
            schema = registered.descriptor()['manifest']['input_schema']
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(schema['properties']['candidates']['maxItems'], maximum)
            with self.assertRaises(ValueError):
                registered.prepare({**self.inputs(runtime, self.candidates[:1]), 'objective': 'override'})
            with self.assertRaises((ValueError, KeyError)):
                registered.prepare({**self.inputs(runtime, self.candidates[:1]), 'run_id': 'another-run'})
            self.assertNotIn(oracle._objective.__name__, json.dumps(registered.manifest))
        self.assertEqual(runtime.store.list('r', 'effect'), [])


if __name__ == '__main__':
    unittest.main()
