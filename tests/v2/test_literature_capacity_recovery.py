"""Capacity recovery uses existing Work and never retries unknown execution."""
import json
import unittest
from unittest.mock import patch

from rsi_lab.roles import CodexExecutor
from rsi_lab.runtime import Runtime
from tests.v2 import test_literature_runtime as literature_fixture
from tests.v2 import test_literature_evidence_lifecycle as lifecycle_fixture
from tests.v2 import test_persistent_runtime as native_fixture


class LiteratureCapacityRecoveryTests(unittest.TestCase):
    def setup_fixture(self):
        fixture = literature_fixture.LiteratureRuntimeTests(
            'test_full_loop_uses_child_session_and_delivers_originals_to_same_lab')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def legacy_capacity_block(self, fixture, work):
        # Reproduce the durable boundary written by the preceding implementation.
        # This is a host-state fixture, not a fabricated model/tool receipt.
        return fixture.store.mutate('r', 'legacy-capacity-block:' + work['id'], 'packet_unrepresentable',
            lambda tx: tx.update('work', work['id'], {'status': 'blocked', 'outcome_ref': tx.blob({
                'status': 'packet_unrepresentable', 'max_input_bytes': 131072,
                'reason': 'Legacy source envelope exceeded its configured bound'})}),
            payload={'work_id': work['id'], 'fixture': 'legacy pre-dispatch capacity boundary'})

    def budgets(self, store, run='r'):
        return {row['id']: (row['spent'], row['reserved']) for row in store.list(run, 'budget')}

    def test_repeated_recover_reopens_same_work_once_without_calls_or_charges(self):
        fixture = self.setup_fixture()
        fixture.delegate()
        fixture.request_tool()
        fixture.execute_tool()
        work = fixture.ready('literature')
        blocked = self.legacy_capacity_block(fixture, work)
        task = fixture.task()
        before = self.budgets(fixture.store)
        effect_count = len(fixture.store.list('r', 'effect'))
        with patch.object(fixture.runtime.controller, 'dispatch_effect', side_effect=AssertionError('Recovery must not dispatch')):
            fixture.runtime.recover()
            fixture.runtime.recover()
        reopened = fixture.store.get('r', 'work', work['id'])
        self.assertEqual(reopened['status'], 'ready')
        self.assertEqual(reopened['inputs'], blocked['inputs'])
        self.assertEqual(reopened['version'], blocked['version'] + 1)
        self.assertEqual(fixture.task(), task)
        self.assertEqual(self.budgets(fixture.store), before)
        self.assertEqual(len(fixture.store.list('r', 'effect')), effect_count)
        self.assertEqual([row['id'] for row in fixture.controller.ready('r') if row['kind'] == 'literature'], [work['id']])
        self.assertEqual(len(fixture.store.list('r', 'registration', kind='literature_capacity_reopen')), 1)
        ids = reopened['inputs']['required_evidence_ids']
        done = fixture.emit(reopened, fixture.command(reopened, 'complete_research', fixture.result(ids), ids))
        self.assertEqual(done['proposal']['status'], 'applied', done)
        self.assertEqual(len(literature_fixture.CALLS), 1)
        fixture.store.verify_history('r')

    def test_related_unknown_tool_cannot_be_reopened_or_retried_as_capacity(self):
        fixture = self.setup_fixture()
        fixture.delegate()
        fixture.request_tool()
        dispatch = fixture.runtime.controller.dispatch_effect
        def lost(run_id, effect_id, executor, **kwargs):
            return dispatch(run_id, effect_id,
                            lambda effect, raw: {'status': 'unknown', 'actual_costs': {}}, **kwargs)
        with patch.object(fixture.runtime.controller, 'dispatch_effect', side_effect=lost):
            result = fixture.execute_tool()
        effect = fixture.store.get('r', 'effect', result['effect']['id'])
        self.assertEqual(effect['status'], 'unknown')
        task = fixture.task()
        work = fixture.controller.enqueue('r', 'literature', 'legacy-block', 'legacy-block', inputs={
            'literature_task_id': task['id'], 'task_revision': task['task_revision'],
            'agent_instance_id': task['agent_instance_id'], 'branch_id': task['branch_id'],
            'evidence_ids': [], 'required_evidence_ids': []})
        blocked = self.legacy_capacity_block(fixture, work)
        budgets = self.budgets(fixture.store)
        count = len(fixture.store.list('r', 'effect'))
        with patch.object(fixture.runtime.controller, 'dispatch_effect', side_effect=AssertionError('No retry')):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'reconciled before capacity'):
                    fixture.runtime.recover_literature_work(work['id'])
        self.assertEqual(fixture.store.get('r', 'work', work['id']), blocked)
        self.assertEqual(fixture.store.get('r', 'effect', effect['id']), effect)
        self.assertEqual(self.budgets(fixture.store), budgets)
        self.assertEqual(len(fixture.store.list('r', 'effect')), count)
        self.assertFalse(fixture.store.list('r', 'registration', kind='literature_capacity_reopen'))
        fixture.store.verify_history('r')

    def test_single_unrepresentable_original_returns_once_and_survives_recovery(self):
        fixture = lifecycle_fixture.LiteratureEvidenceLifecycleTests(
            'test_single_unrepresentable_original_returns_unresolved_to_parent_without_dispatch')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        doc = fixture.register_paper('recovery-large-source', ['Original paragraph ' + 'x' * 60000])
        fixture.delegate()
        fixture.request_read(doc, max_bytes=65536)
        large = next(fid for fid in fixture.source_ids(doc)
                     if len(fixture.load(fixture.store.get('lifecycle', 'fact', fid)['content_ref'])['text']) > 50000)
        requested = fixture.emit(fixture.ready('literature'), 'read_cached_evidence',
                                 fixture.cache_payload([large], limit=1))
        self.assertEqual(requested['proposal']['status'], 'applied', requested)
        work = fixture.ready('literature')
        budgets = self.budgets(fixture.store, 'lifecycle')
        effects = len(fixture.store.list('lifecycle', 'effect'))
        result = fixture.runtime.prepare_meta(work['id'], {'model_calls': 1})
        self.assertEqual(result['status'], 'cancelled')
        parent = fixture.ready('lab')
        with patch.object(fixture.runtime.controller, 'dispatch_effect', side_effect=AssertionError('No capacity retry')):
            fixture.runtime.recover()
            fixture.runtime.recover()
        self.assertEqual(fixture.ready('lab'), parent)
        self.assertEqual(len(fixture.store.list('lifecycle', 'effect')), effects)
        self.assertEqual(self.budgets(fixture.store, 'lifecycle'), budgets)
        self.assertIn(large, fixture.task()['evidence_ids'])
        self.assertEqual(len(fixture.task()['result_ids']), 1)
        summary = fixture.load(fixture.store.get('lifecycle', 'fact', fixture.task()['result_ids'][0])['content_ref'])
        self.assertEqual(summary['coverage_status'], 'unresolved')
        self.assertEqual(summary['claims'], [])
        self.assertEqual(len(fixture.store.list('lifecycle', 'registration', kind='literature_capacity_stop')), 1)
        fixture.store.verify_history('lifecycle')

    def test_native_history_capacity_rebuild_preserves_owner_memory_and_cached_sources(self):
        # The local subprocess emits the native Codex protocol fixture. Runtime,
        # executor, journal, rollout checks and accounting all execute normally;
        # no external model or provider is called.
        native = native_fixture.PersistentRuntimeTests('test_driver_executes_ready_lab_work')
        native.setUp()
        self.addCleanup(native.doCleanups)
        store, controller = native.store, native.c
        controller.initialize('capacity', budgets={name: {'limit': limit, 'hard': name.endswith('calls')}
            for name, limit in {'model_calls': 20, 'tool_calls': 20, 'tokens': 100000, 'wall_seconds': 1000}.items()},
            config={'execution_mode': 'live', 'model': 'synthetic-model', 'effort': 'medium',
                'goal': 'Inspect a synthetic control', 'max_input_bytes': 65536,
                'max_effective_context_bytes': 65536})
        runtime = Runtime(store, 'capacity', adapters=[literature_fixture.adapter()])
        executor = CodexExecutor(store.root / 'calls')

        def ready(kind):
            return next(row for row in controller.ready('capacity') if row['kind'] == kind)

        def task():
            return store.list('capacity', 'registration', kind='literature_task')[0]

        def emit(work, action, payload, refs=(), delta=None):
            mode = 'literature' if work['kind'] == 'literature' else 'lab'
            command = {'mode': mode, 'action': action, 'issue_id': None,
                'expected_versions': runtime.versions(None), 'evidence_ids': list(refs), 'payload': payload}
            if mode == 'literature':
                command.update(task_id=task()['id'], task_revision=task()['task_revision'])
            else:
                delta = {'rationale_summaries': [{'kind': 'literature_decision', 'decision': 'delegate',
                    'reason': 'Synthetic capacity lifecycle fixture', 'evidence_ids': [], 'related_task_ids': []}]}
            native.outputs.append({'output': json.dumps({'command': command,
                'working_memory_delta': json.dumps(delta or {})})})
            result = runtime.run_meta(work['id'], executor, native.reservations)
            self.assertEqual(result['proposal']['status'], 'applied', result)
            return result

        emit(runtime.enqueue_lab('capacity-parent'), 'request_literature', {
            'question': 'Which control is reported?', 'intended_use': 'Bounded source inspection',
            'scope': 'One synthetic paper', 'deliverables': ['An original passage'],
            'max_model_calls': 8, 'max_tool_calls': 6})
        emit(ready('literature'), 'request_diagnostic', {'capability_id': 'literature.read_document',
            'capability_version': '1.0.0', 'inputs_json': '{"document_id":"paper"}'},
            delta={'next_step': 'Preserve the control passage across capacity reconstruction'})
        tool = ready('diagnostic')
        runtime.execute_adapter('literature.read_document', {'document_id': 'paper'}, trigger_id=tool['id'], work_id=tool['id'])
        source_ids = ready('literature')['inputs']['required_evidence_ids']
        old = store.list('capacity', 'agent_session', role='literature')[0]
        tools_before = self.budgets(store, 'capacity')['tool_calls']
        for _ in range(5):
            emit(ready('literature'), 'read_cached_evidence', {'evidence_ids': source_ids,
                 'document_ids': [], 'query': '', 'limit': 1})
            sessions = store.list('capacity', 'agent_session', role='literature')
            if len(sessions) > 1:
                break
        else:
            self.fail('The bounded native history did not trigger reconstruction')
        current = next(row for row in sessions if row['status'] == 'idle')
        self.assertEqual(current['owner_id'], old['owner_id'])
        self.assertEqual(current['supersedes_session'], old['id'])
        self.assertNotEqual(current['provider_thread_id'], old['provider_thread_id'])
        self.assertEqual(runtime.agents.working_memory(current['id'])['content']['next_step'],
                         'Preserve the control passage across capacity reconstruction')
        self.assertEqual(task()['evidence_ids'], source_ids)
        self.assertEqual(self.budgets(store, 'capacity')['tool_calls'], tools_before)
        emit(ready('literature'), 'complete_research', {'answer': 'The original reports a matched control.',
            'coverage_status': 'partial', 'stop_reason': 'Requested source checked',
            'limitations': ['Only this synthetic source was read'],
            'claims': [{'statement': 'The control is matched.', 'evidence_ids': source_ids,
                        'limitations': ['As reported']}]}, source_ids)
        calls, budgets = len(native.calls), self.budgets(store, 'capacity')
        runtime.recover()
        runtime.recover()
        self.assertEqual(len(native.calls), calls)
        self.assertEqual(self.budgets(store, 'capacity'), budgets)
        self.assertEqual(budgets['model_calls'], (calls, 0))
        self.assertEqual(len([row for row in controller.ready('capacity') if row['kind'] == 'lab']), 1)
        self.assertEqual(task()['status'], 'completed')
        store.verify_history('capacity')


if __name__ == '__main__':
    unittest.main()
