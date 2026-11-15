"""Synthetic completed-parent continuation tests; no model or oracle calls."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import continue_pablo_rsi as continuation
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode
from rsi_domains.pablo import adapter, oracle


class PabloContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.parent = self.base / 'parent'
        self.child = self.base / 'child'
        self.canonical = patch.object(oracle, 'canonicalize', side_effect=lambda s: s if s.startswith('mol-') else None)
        self.fingerprint = patch.object(oracle, 'fingerprint', return_value='synthetic-fingerprint')
        self.canonical.start(); self.fingerprint.start()
        self.addCleanup(self.canonical.stop); self.addCleanup(self.fingerprint.stop)
        self.make_parent()

    def make_parent(self):
        store = Store(self.parent)
        controller = Controller(store)
        initial = {'candidates': [f'mol-{i:03d}' for i in range(1, 101)], 'scores': [0.1] * 100}
        initial_ref = store.put_blob(initial)
        controller.initialize('parent', budgets={name: {'limit': limit, 'hard': name.endswith('_calls')}
            for name, limit in {'model_calls': 600, 'tool_calls': 1000, 'tokens': 100000000,
                                'wall_seconds': 432000, 'cpu_seconds': 86400}.items()},
            config={'execution_mode': 'live', 'task_id': 'parent', 'task_plan_id': 'parent',
                    'model': 'gpt-6.1-sol', 'effort': 'medium', 'pablo_run_root': str(self.parent),
                    'pablo_initial_ref': initial_ref, 'pablo_oracle_fingerprint': 'synthetic-fingerprint',
                    'pablo_max_new_evaluations': 200, 'max_working_memory_bytes': 32768,
                    'max_input_bytes': 262144, 'max_effective_context_bytes': 2097152,
                    'prospective_rebuild_history_bytes': 1200000,
                    'model_reservation': {'model_calls': 1, 'tokens': 45000, 'wall_seconds': 600}})
        for offset in range(0, 200, 20):
            accepted = [{'canonical': f'mol-{101 + offset + i:03d}', 'source_index': i} for i in range(20)]
            packet = {'evaluation_start': offset, 'accepted': accepted}
            work = controller.enqueue('parent', 'diagnostic', f'fixture-{offset}', f'work-{offset}')
            effect = controller.prepare_effect('parent', work['id'], adapter.EFFECT_KIND, packet,
                {'tool_calls': 1, 'wall_seconds': 1}, f'effect-{offset}',
                metadata={'adapter_id': adapter.ADAPTER_ID, 'adapter_version': adapter.VERSION})
            def execute(effect, raw):
                value = json.loads(raw)
                rows = [{'slot_id': f'evaluation-{value["evaluation_start"] + i + 1:03d}',
                         'evaluation': value['evaluation_start'] + i + 1,
                         'candidate': row['canonical'], 'source_index': row['source_index'], 'score': 0.2}
                        for i, row in enumerate(value['accepted'])]
                folder = self.parent / 'pablo_evaluations' / effect['id']
                folder.mkdir(parents=True)
                journal = b''.join(encode({'effect_id': effect['id'], 'packet_sha256': digest(raw), **row}) + b'\n' for row in rows)
                (folder / 'evaluations.jsonl').write_bytes(journal)
                return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1, 'wall_seconds': 0.5},
                        'result': {'effect_id': effect['id'], 'packet_sha256': digest(raw),
                                   'evaluation_start': value['evaluation_start'], 'evaluations': rows},
                        'execution': {'journal_sha256': digest(journal)}}
            last = controller.dispatch_effect('parent', effect['id'], execute)
        runtime = Runtime(store, 'parent', adapters=[adapter.make_adapter()])
        binding = {'model': 'gpt-6.1-sol', 'effort': 'medium', 'transport': 'codex_exec_text_v1'}
        lab = runtime.agents.ensure_session('lab', runtime_binding=binding)
        runtime.agents.ensure_session('meta', runtime_binding=binding)
        self.private = {'next_step': 'Historical 190 evaluated, 10 remaining; verify current task before continuing',
                        'plan_items': ['private Lab strategy'], 'evidence_refs': ['parent-only-reference']}
        def memory(tx):
            owner = tx.get('agent_owner', lab['owner_id'])
            tx.insert('working_memory', 'parent-lab-memory', {'owner_id': owner['id'], 'owner': owner['scope'],
                'revision': 20, 'parent_ref': None, 'content': self.private,
                'source_turn_id': None, 'source_call_id': None, 'source_effect_id': last['id'],
                'schema_version': 'agent_working_memory_v1', 'verification': 'unverified_working_state'})
            return tx.update('agent_owner', owner['id'], {'memory_revision': 20, 'memory_head_ref': 'parent-lab-memory'})
        store.mutate('parent', 'fixture-memory', 'fixture_private_memory', memory)
        controller.enqueue('parent', 'lab', 'final-result', 'final-ready', inputs={'source_effect_id': last['id']})
        runtime.finalize('requested_new_evaluations_completed')
        store.close()
        (self.parent / 'host.lock').touch()
        continuation.host.write_json(self.parent / 'result.json', {'status': 'completed', 'run_id': 'parent',
            'new_evaluations': 200, 'initial_count': 100})
        # Identical effect IDs in distinct attempt directories must not be merged.
        attempts = [{'summary_path': str(self.base / name / 'summary.json'), 'effect_ids': ['same-effect'],
                     'actual_costs': {'model_calls': 1, 'tokens': tokens}} for name, tokens in [('attempt-a', 13), ('attempt-b', 17)]]
        continuation.host.write_json(self.parent / 'manifest.json', {
            'source_hashes': {'scripts/run_pablo_rsi.py': digest(Path(continuation.host.__file__).read_bytes())},
            'prior_attempts': attempts, 'algorithm': 'Synthetic fixture, no model or science executed'})

    def args(self):
        return argparse.Namespace(root=self.child, parent_root=self.parent, parent_run_id='parent',
                                  run_id='child', new_evaluations=200)

    def test_inherits_300_without_rescoring_and_prevents_historical_duplicates(self):
        before = {str(p.relative_to(self.parent)): digest(p.read_bytes())
                  for p in self.parent.rglob('*') if p.is_file()}
        with patch.object(oracle, 'score', side_effect=AssertionError('Historical molecules must never be rescored')):
            continuation.prepare(self.args())
        after = {str(p.relative_to(self.parent)): digest(p.read_bytes())
                 for p in self.parent.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        plan = json.loads((self.child / 'task-plan.json').read_text())
        self.assertEqual(plan['protocol']['parameters']['initial_count'], 300)
        self.assertEqual(plan['protocol']['parameters']['original_initial_count'], 100)
        self.assertEqual(plan['protocol']['parameters']['historical_generated_evaluations'], 200)
        self.assertEqual(plan['protocol']['parameters']['global_observation_ordinal_offset'], 300)
        self.assertEqual(plan['protocol']['parameters']['target_total_observations'], 500)
        self.assertIn('500 total observations', plan['protocol']['parameters']['slot_semantics'])
        packet = adapter.prepare({'run_root': str(self.child), 'run_id': 'child',
                                  'candidates': ['mol-001', 'mol-299', 'mol-301']})
        self.assertEqual([r['status'] for r in packet['filters']], ['duplicate', 'duplicate', 'accepted'])
        self.assertEqual(packet['evaluation_start'], 0)

    def test_private_memory_import_is_role_scoped_and_has_no_fictitious_model_turn(self):
        continuation.prepare(self.args())
        store = continuation.ReadOnlySnapshot(self.child)
        self.addCleanup(store.close)
        runtime = Runtime(store, 'child', adapters=[adapter.make_adapter()])
        owners = {o['scope']['role']: o for o in store.list('child', 'agent_owner')}
        lab, meta = (store.get('child', 'agent_session', owners[r]['session_id']) for r in ('lab', 'meta'))
        imported = runtime.agents.working_memory(lab['id'])
        self.assertEqual(imported['content'], self.private)
        self.assertEqual(imported['revision'], 1)
        self.assertEqual(runtime.agents.working_memory(meta['id'])['revision'], 0)
        with self.assertRaises(ValueError):
            runtime.agents.read_working_memory(lab['id'], actor_session_id=meta['id'])
        row = store.get('child', 'working_memory', imported['head_ref'])
        self.assertIsNone(row['source_turn_id']); self.assertIsNone(row['source_effect_id'])
        self.assertEqual(row['import_provenance']['parent_memory_revision'], 20)
        self.assertIsNone(lab['provider_thread_id']); self.assertEqual(lab['committed_turn'], 0)
        self.assertEqual(store.list('child', 'agent_turn'), [])
        self.assertNotIn('private Lab strategy', (self.child / 'task-contract.txt').read_text())

    def test_cost_sources_and_colliding_attempt_ids_remain_separate(self):
        continuation.prepare(self.args())
        totals = json.loads((self.child / 'cumulative-costs.json').read_text())
        self.assertEqual(totals['current_segment_costs']['tool_calls'], 0)
        self.assertEqual(totals['prior_segment_costs']['tool_calls'], 10)
        self.assertEqual(totals['prior_attempt_costs']['model_calls'], 2)
        self.assertEqual(totals['cumulative_known_costs']['tokens'], 30)
        self.assertEqual(len(totals['prior_attempts']), 2)
        manifest = json.loads((self.child / 'manifest.json').read_text())
        self.assertIn('scripts/continue_pablo_rsi.py', manifest['source_hashes'])
        self.assertIn('scripts/run_pablo_rsi.py', manifest['source_hashes'])

    def test_unknown_parent_is_rejected_before_creating_child(self):
        store = Store(self.parent)
        try:
            store.mutate('parent', 'fixture-unknown', 'fixture_unknown_effect', lambda tx:
                tx.insert('effect', 'unknown', {'status': 'unknown', 'accounting_unknown': True,
                                              'held_reservations': {'tool_calls': 1}}))
        finally:
            store.close()
        with self.assertRaisesRegex(ValueError, 'unknown execution'):
            continuation.prepare(self.args())
        self.assertFalse(self.child.exists())

    def test_parent_projection_or_source_hash_tampering_is_rejected(self):
        manifest_path = self.parent / 'manifest.json'
        value = json.loads(manifest_path.read_text())
        value['source_hashes']['scripts/run_pablo_rsi.py'] = '0' * 64
        continuation.host.write_json(manifest_path, value)
        with self.assertRaisesRegex(ValueError, 'frozen source differs'):
            continuation.load_parent(self.parent, 'parent')
        value['source_hashes']['scripts/run_pablo_rsi.py'] = digest(Path(continuation.host.__file__).read_bytes())
        continuation.host.write_json(manifest_path, value)
        db = sqlite3.connect(self.parent / 'ledger.sqlite3')
        old = json.loads(db.execute("SELECT document FROM v2_budget WHERE id='tokens'").fetchone()[0])
        old['spent'] = 777
        db.execute("UPDATE v2_budget SET document=? WHERE id='tokens'", (json.dumps(old),)); db.commit(); db.close()
        with self.assertRaisesRegex(ValueError, 'projections differ'):
            continuation.load_parent(self.parent, 'parent')


if __name__ == '__main__':
    unittest.main()
