"""Host crash-boundary checks with synthetic callbacks; no models or oracle calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode
from scripts import run_pablo_rsi as host


CALLS = []


def fixture_prepare(inputs):
    return inputs


def fixture_reservations(packet):
    return {'tool_calls': 1}


def fixture_execute(effect, raw):
    CALLS.append((effect['id'], raw))
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
            'result': {'value': json.loads(raw)['value']}}


def fixture_publication(effect, receipt):
    return {'facts': [], 'conclusions': [], 'data': receipt['result']}


def fixture_adapter():
    return Adapter(adapter_id='host.fixture', version='1', effect_kind='host_fixture',
                   manifest={'scope': 'Synthetic recovery check only'},
                   prepare=fixture_prepare, reservations=fixture_reservations,
                   execute=fixture_execute, publication=fixture_publication)


class PabloHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', max_external=1,
            budgets={name: {'limit': limit, 'hard': name.endswith('_calls')}
                     for name, limit in {'model_calls': 10, 'tool_calls': 10,
                                         'tokens': 10000, 'wall_seconds': 100}.items()},
            config={'execution_mode': 'replay', 'task_id': 'fixture',
                    'pablo_max_new_evaluations': 200,
                    'prospective_rebuild_history_bytes': 1,
                    'model_reservation': {'model_calls': 1, 'tokens': 100, 'wall_seconds': 10}})
        self.runtime = Runtime(self.store, 'r', adapters=[fixture_adapter()])
        CALLS.clear()

    def prepared(self):
        adapter = self.runtime.adapters.get('host.fixture')
        inputs = {'value': 'the original prepared packet'}
        work = self.controller.enqueue('r', 'diagnostic', 'fixture', 'fixture-work',
            inputs={'capability_id': adapter.adapter_id, 'capability_version': adapter.version,
                    'inputs_json': json.dumps(inputs)})
        descriptor = adapter.descriptor()
        return self.controller.prepare_effect('r', work['id'], adapter.effect_kind,
            encode(inputs), {'tool_calls': 1}, 'fixture-effect',
            metadata={'adapter_id': adapter.adapter_id, 'adapter_version': adapter.version,
                      'adapter_descriptor_sha256': digest(descriptor),
                      'adapter_descriptor_ref': self.store.put_blob(descriptor)})

    def progress(self):
        return {'new_evaluations': 0, 'remaining_evaluations': 200,
                'initial_count': 100, 'top20': []}

    def test_prepared_diagnostic_resumes_exact_packet_once_without_repreparing(self):
        effect = self.prepared()
        original = self.store.read_blob(effect['packet_ref'])
        # A restart reconstructs Runtime; no scientific callback has executed yet.
        runtime = Runtime(self.store, 'r', adapters=[fixture_adapter()])
        runtime.recover()
        result = host.resume_prepared_diagnostic(runtime)
        self.assertEqual(result['effect_status'], 'succeeded')
        self.assertEqual(CALLS, [(effect['id'], original)])
        self.assertIsNone(host.resume_prepared_diagnostic(runtime))
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(self.store.get('r', 'budget', 'tool_calls')['spent'], 1)
        self.store.verify_history('r')

    def test_unknown_effect_blocks_dispatch_and_is_visible_after_a_single_step(self):
        effect = self.prepared()
        self.controller.dispatch_effect('r', effect['id'], lambda *_: {
            'status': 'unknown', 'actual_costs': {}, 'reason': 'Synthetic interrupted callback'})
        with patch.object(host, 'read_progress', return_value=self.progress()):
            state = host.snapshot(self.runtime, 'running')
        self.assertIn('requires reconciliation', host.attention_reason(state))
        with self.assertRaisesRegex(RuntimeError, 'requires reconciliation'):
            host.resume_prepared_diagnostic(self.runtime)
        self.assertEqual(CALLS, [])

    def test_blocked_work_is_not_mistaken_for_successful_step_limit(self):
        work = self.runtime.enqueue_lab('synthetic-capacity-failure')
        self.store.mutate('r', 'fixture-block', 'fixture_admission_rejected',
            lambda tx: tx.update('work', work['id'], {'status': 'blocked',
                'outcome_ref': tx.blob({'reason': 'Synthetic capacity rejection'})}))
        with patch.object(host, 'read_progress', return_value=self.progress()):
            state = host.snapshot(self.runtime, 'running')
        self.assertIn(work['id'], host.attention_reason(state))

    def native_session(self):
        session = self.runtime.agents.ensure_session('lab')
        workspace = self.root / 'agents' / session['id'] / 'workspace'
        workspace.mkdir(parents=True)
        path = self.root / 'synthetic-rollout.jsonl'
        thread = '00000000-0000-4000-8000-000000000000'
        raw = encode({'type': 'session_meta', 'payload': {'id': thread, 'cwd': str(workspace)}}) + b'\n'
        path.write_bytes(raw)
        self.store.mutate('r', 'fixture-native', 'fixture_native_snapshot',
            lambda tx: tx.update('agent_session', session['id'], {
                'provider_thread_id': thread, 'committed_turn': 1,
                'context_manifest': {'native_session_ref': str(path), 'native_prefix': {
                    'provider_thread_id': thread, 'prefix_bytes': len(raw), 'prefix_sha256': digest(raw)}}}))
        return session, path

    def test_capacity_checkpoint_rebuilds_owned_idle_session_and_preserves_owner(self):
        session, _ = self.native_session()
        with patch.object(host, 'log'):
            host.rebuild_at_boundary(self.runtime)
        old = self.store.get('r', 'agent_session', session['id'])
        owner = self.store.get('r', 'agent_owner', session['owner_id'])
        new = self.store.get('r', 'agent_session', owner['session_id'])
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(new['owner_id'], session['owner_id'])
        self.assertEqual(new['supersedes_session'], session['id'])
        self.assertEqual(len(self.store.list('r', 'agent_checkpoint')), 1)
        self.store.verify_history('r')

    def test_capacity_rebuild_rejects_uncommitted_native_history_extension(self):
        session, path = self.native_session()
        with path.open('ab') as stream:
            stream.write(encode({'type': 'event_msg', 'payload': {'type': 'synthetic_external_update'}}) + b'\n')
        with self.assertRaisesRegex(RuntimeError, 'advanced outside its committed turn'):
            host.rebuild_at_boundary(self.runtime)
        self.assertEqual(self.store.get('r', 'agent_session', session['id'])['status'], 'idle')
        self.assertEqual(self.store.list('r', 'agent_checkpoint'), [])

    def test_attention_file_survives_unverifiable_scientific_progress(self):
        with patch.object(host, 'read_progress', side_effect=ValueError('Unknown accounting')):
            report = host.record_attention(self.runtime, 'Cannot certify score count')
        saved = json.loads((self.root / 'attention.json').read_text())
        self.assertEqual(saved, report)
        self.assertFalse(saved['progress_verified'])
        self.assertIsNone(saved['new_evaluations'])
        self.assertIn('Unknown accounting', saved['progress_error'])

    def test_final_batch_review_does_not_run_unrelated_or_lab_work(self):
        self.runtime.enqueue_lab('remaining-lab')
        self.runtime.observe('unrelated', 'input_changed', 0, [self.fact()])
        self.assertIsNone(host.review_final_batch(self.runtime,
            lambda *_: self.fail('No scoring batch exists, so no model callback is allowed')))

    def fact(self):
        self.controller.add_fact('r', 'synthetic', 'raw_fact', {'fixture': True},
                                 {'source': 'unit test'}, 'fixture-fact')
        return 'synthetic'


if __name__ == '__main__':
    unittest.main()
