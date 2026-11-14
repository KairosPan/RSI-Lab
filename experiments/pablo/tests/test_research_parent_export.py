"""Closed parent export/transfer; only synthetic model and scorer fixtures execute."""
import fcntl
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import continuation
from pablo_task import adapter, oracle
from pablo_task.research import project_publication, registered_adapters
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location('export_model_fixture', REPO / 'tests/v2/test_improvements.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
FakeModel = _fixture.FakeModel


class ResearchParentExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'parent'
        store = Store(self.root)
        initial = store.put_blob({'candidates': ['CCO'], 'scores': [0.1]})
        controller = Controller(store)
        controller.initialize('parent', max_external=1,
            budgets={key: {'limit': 10000, 'hard': key.endswith('calls')}
                     for key in ('model_calls', 'tool_calls', 'tokens', 'wall_seconds', 'cpu_seconds')},
            config={'execution_mode': 'replay', 'model': 'gpt-6.1-sol', 'effort': 'high',
                    'research_features': {'scientific_memory': True}, 'max_input_bytes': 500000,
                    'goal': 'Keep observations and role-owned memory', 'pablo_run_root': str(self.root),
                    'pablo_initial_ref': initial, 'pablo_max_new_evaluations': 2,
                    'pablo_oracle_fingerprint': oracle.fingerprint()})
        runtime = Runtime(store, 'parent', adapters=registered_adapters(),
                          scientific_identity_provider=project_publication)
        controller.add_fact('parent', 'raw', 'raw_fact', {'score': 0.1}, {'source': 'fixed fixture'}, 'raw')
        runtime.scientific_memory.observe_fact('raw')
        score_inputs = {'run_root': str(self.root), 'run_id': 'parent', 'candidates': ['CCC']}
        lab = runtime.enqueue_lab('request-score', ['raw'])
        routed = runtime.run_lab(lab['id'], FakeModel('request_diagnostic', {
            'capability_id': adapter.ADAPTER_ID, 'capability_version': adapter.VERSION,
            'inputs_json': json.dumps(score_inputs)}), {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1})
        self.assertEqual(routed['proposal']['status'], 'applied')
        score_work = store.list('parent', 'work', kind='diagnostic')[0]
        with patch.object(oracle, 'score', return_value=0.25) as scorer:
            scored = runtime.execute_adapter(adapter.ADAPTER_ID, score_inputs,
                trigger_id=score_work['id'], work_id=score_work['id'])
        self.assertEqual(scorer.call_count, 1, store.read_blob(scored['effect']['result_ref']).decode())
        for role in ('lab', 'meta'):
            ready = [row for row in controller.ready('parent') if row['kind'] == role]
            work = ready[0] if ready else controller.enqueue('parent', 'lab' if role == 'lab' else 'meta_discovery',
                role, 'work-' + role, inputs={'evidence_ids': ['raw']})
            def private(response, data, role=role):
                response['working_memory_delta'] = json.dumps({'next_step': role + '-private-next-step'})
            result = runtime.run_meta(work['id'], FakeModel('no_action', {'reason': 'Store own private memory'},
                mutate=private), {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1})
            self.assertEqual(result['proposal']['status'], 'applied', result['proposal'])
        # A paused parent can retain an unexecuted intent; it is archived only.
        controller.enqueue('parent', 'diagnostic', 'pending', 'pending',
            inputs={'capability_id': adapter.ADAPTER_ID, 'capability_version': adapter.VERSION,
                    'inputs_json': json.dumps({'run_root': str(self.root), 'run_id': 'parent', 'candidates': ['CCCC']})})
        progress = adapter.read_progress(store, 'parent')
        self.history = store.verify_history('parent')
        self.costs = {row['id']: row['spent'] for row in store.list('parent', 'budget')}
        self.bundle = runtime.scientific_memory.export_bundle()
        store.close()
        (self.root / 'host.lock').touch()
        (self.root / 'progress.json').write_text(json.dumps({'run_id': 'parent', 'status': 'paused',
            'target_new_evaluations': 2, **{key: progress[key] for key in
                                          ('initial_count', 'new_evaluations', 'remaining_evaluations')}}))
        paths = {path for folder in ('rsi_lab', 'rsi_domains', 'experiments/pablo')
                 for path in (REPO / folder).rglob('*.py') if 'tests' not in path.relative_to(REPO).parts}
        self.manifest = {'research_architecture': 'pablo_research_fixture', 'target_new_evaluations': 2,
            'source_hashes': {str(path.relative_to(REPO)): digest(path.read_bytes()) for path in sorted(paths)}}
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def export(self):
        return continuation.load_research_parent(self.root, 'parent', REPO)

    def mutate_parent_fixture(self, fn):
        store = Store(self.root)
        try:
            store.mutate('parent', 'fixture-change', 'fixture', fn)
        finally:
            store.close()

    def test_paused_export_keeps_observations_costs_role_memory_and_graph_without_parent_writes(self):
        # A zero-byte leftover WAL is not evidence of outstanding committed data.
        (self.root / 'ledger.sqlite3-wal').touch()
        before = {str(path.relative_to(self.root)): digest(path.read_bytes())
                  for path in self.root.rglob('*') if path.is_file()}
        parent = self.export()
        self.assertEqual(parent['boundary']['status'], 'paused')
        self.assertEqual(parent['boundary']['source_kind'], 'paused_research_progress')
        self.assertEqual(parent['progress']['candidates'], ['CCO', 'CCC'])
        self.assertEqual(parent['progress']['new_evaluations'], 1)
        self.assertEqual(parent['history'], self.history)
        self.assertEqual(parent['scientific_memory_bundle'], self.bundle)
        self.assertEqual({row['id']: row['spent'] for row in parent['budgets']}, self.costs)
        self.assertEqual(len(parent['pending_parent_state']['work']), 1)
        self.assertEqual(len(parent['scoring_sources']), 1)
        self.assertTrue(all(digest(raw) == key for key, raw in parent['evidence_objects'].items()))
        after = {str(path.relative_to(self.root)): digest(path.read_bytes())
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        target = Store(self.base / 'target')
        self.addCleanup(target.close)
        Controller(target).initialize('target', budgets={}, config={'model': 'gpt-6.1-sol', 'effort': 'high',
            'research_features': {'scientific_memory': True}})
        runtime = Runtime(target, 'target')
        for role in ('lab', 'meta'):
            self.assertEqual(parent['memories'][role]['memory']['content']['next_step'], role + '-private-next-step')
            continuation.import_memory(runtime, role, parent['memories'][role], parent)
        runtime.scientific_memory.import_bundle(parent['scientific_memory_bundle'])
        owners = {row['scope']['role']: row for row in target.list('target', 'agent_owner')}
        for role, owner in owners.items():
            memory = target.get('target', 'working_memory', owner['memory_head_ref'])
            self.assertEqual(memory['content']['next_step'], role + '-private-next-step')
            self.assertEqual(memory['import_provenance']['parent_memory_head_proof']['history'], self.history)
            session = target.get('target', 'agent_session', owner['session_id'])
            self.assertIsNone(session['provider_thread_id'])
        self.assertEqual(target.list('target', 'effect'), [])
        self.assertEqual(target.list('target', 'work'), [])
        lineage = continuation.make_lineage(parent, 1)
        self.assertEqual(lineage['parent_boundary']['status'], 'paused')
        self.assertEqual(lineage['inherited_segment_costs'], self.costs)
        self.assertEqual(lineage['inherited_observations'], 2)

    def test_export_rejects_active_host_and_nonempty_wal(self):
        with (self.root / 'host.lock').open('rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'host is still running'):
                self.export()
        (self.root / 'ledger.sqlite3-wal').write_bytes(b'not-safe-to-ignore')
        with self.assertRaisesRegex(ValueError, 'nonempty WAL'):
            self.export()

    def test_export_rejects_unknown_effect_before_transfer(self):
        self.mutate_parent_fixture(lambda tx: tx.insert('effect', 'unknown',
            {'kind': 'tool', 'status': 'unknown', 'accounting_unknown': False, 'held_reservations': {},
             'actual_costs': {}, 'metadata': {}}))
        with self.assertRaisesRegex(ValueError, 'active/unknown execution'):
            self.export()

    def test_export_rejects_source_manifest_changes_and_false_boundary_counts(self):
        changed = json.loads(json.dumps(self.manifest))
        changed['source_hashes']['rsi_lab/store.py'] = '0' * 64
        (self.root / 'manifest.json').write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'Historical source differs'):
            self.export()
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))
        path = self.root / 'progress.json'
        data = json.loads(path.read_text())
        data['new_evaluations'] = 2
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'differs from authenticated observations'):
            self.export()

    def test_private_memory_owner_mismatch_is_not_reclassified_as_a_checkpoint(self):
        def mismatch(tx):
            owner = next(row for row in tx.find('agent_owner') if row['scope']['role'] == 'meta')
            other = next(row for row in tx.find('agent_owner') if row['scope']['role'] == 'lab')
            tx.update('agent_owner', owner['id'], {'memory_head_ref': other['memory_head_ref']})
        self.mutate_parent_fixture(mismatch)
        with self.assertRaisesRegex(ValueError, 'private memory head differs'):
            self.export()


if __name__ == '__main__':
    unittest.main()
