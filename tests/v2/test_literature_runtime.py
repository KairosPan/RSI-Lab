"""Offline lifecycle tests: delegation, delivered sources, quotas and crash replay."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller, identity
from rsi_lab.runtime import Runtime
from rsi_lab.roles import validate_command
from rsi_lab.store import Store, digest


CALLS = []


def prepare(inputs):
    return dict(inputs)


def reserve(packet):
    return {'tool_calls': 3}


def execute(effect, packet):
    CALLS.append(effect['id'])
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 2},
            'result': {'text': 'The reported assay used a matched control.', 'page': 4}}


def publish(effect, receipt):
    return {'facts': [{'id': identity('passage', effect['id']), 'kind': 'source_statement',
        'content': receipt['result'], 'provenance': {'source': 'fixture-paper', 'page': 4,
            'effect_id': effect['id'], 'delivery_kind': 'original'}}], 'conclusions': [],
        'data': {'not_delivered_as_evidence': 'hidden-data-field'}}


def adapter(adapter_id='literature.read_document'):
    return Adapter(adapter_id=adapter_id, version='1.0.0', effect_kind=adapter_id.replace('.', '_'),
        manifest={'input_schema': {'type': 'object'}, 'output_contract': 'original passage'},
        prepare=prepare, reservations=reserve, execute=execute, publication=publish)


class LiteratureRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={'model_calls': {'limit': 30, 'hard': True},
            'tool_calls': {'limit': 30, 'hard': True}}, config={'goal': 'Investigate the assay'})
        self.adapters = [adapter(), adapter('numeric.fixture')]
        self.runtime = Runtime(self.store, 'r', adapters=self.adapters)
        CALLS.clear()

    def task(self):
        return self.store.list('r', 'registration', kind='literature_task')[0]

    def ready(self, kind):
        return next(w for w in self.controller.ready('r') if w['kind'] == kind)

    def command(self, work, action, payload, evidence=(), **overrides):
        mode = 'literature' if work['kind'] == 'literature' else 'lab'
        value = {'mode': mode, 'action': action, 'issue_id': None,
            'expected_versions': self.runtime.versions(None), 'evidence_ids': list(evidence), 'payload': payload}
        if mode == 'literature':
            value.update(task_id=self.task()['id'], task_revision=self.task()['task_revision'])
        value.update(overrides)
        memory = {}
        if mode == 'lab':
            memory['rationale_summaries'] = [{
                'kind': 'literature_decision',
                'decision': 'delegate' if action == 'request_literature' else 'not_needed',
                'reason': 'Scripted lifecycle fixture; this does not test autonomous judgment.',
                'evidence_ids': list(evidence), 'related_task_ids': []}]
        return {'command': value, 'working_memory_delta': json.dumps(memory)}

    def emit(self, work, command):
        def model(effect, raw):
            envelope = json.loads(raw)
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1}, 'response': command,
                'sent_packet_sha256': digest(raw), 'sent_prompt_sha256': digest(envelope['prompt'].encode())}
        return self.runtime.run_meta(work['id'], model, {'model_calls': 1})

    def delegate(self, max_model_calls=4, max_tool_calls=6):
        lab = self.runtime.enqueue_lab('start')
        result = self.emit(lab, self.command(lab, 'request_literature', {
            'question': 'Which control supports the reported assay?', 'intended_use': 'Plan a replication',
            'scope': 'The supplied paper', 'deliverables': ['Evidence-backed control identification'],
            'capability_ids': ['literature.read_document'], 'max_model_calls': max_model_calls,
            'max_tool_calls': max_tool_calls}))
        self.assertEqual(result['proposal']['status'], 'applied', result)
        return self.ready('literature')

    def request_tool(self, work=None, capability='literature.read_document'):
        work = work or self.ready('literature')
        return self.emit(work, self.command(work, 'request_diagnostic', {'capability_id': capability,
            'capability_version': '1.0.0', 'inputs_json': json.dumps({'document_id': 'paper'})}))

    def execute_tool(self):
        work = self.ready('diagnostic')
        return self.runtime.execute_adapter('literature.read_document', {'document_id': 'paper'},
            trigger_id=work['id'], work_id=work['id'])

    def result(self, ids=()):
        return {'answer': 'The assay used a matched control.', 'coverage_status': 'partial',
            'stop_reason': 'Requested passage checked', 'limitations': ['Other papers not examined'],
            'claims': [{'statement': 'The reported control is matched.', 'evidence_ids': list(ids),
                        'limitations': ['As reported by the authors']}] if ids else []}

    def test_full_loop_uses_child_session_and_delivers_originals_to_same_lab(self):
        self.delegate()
        self.assertEqual(self.request_tool()['proposal']['status'], 'applied')
        self.execute_tool()
        work = self.ready('literature')
        ids = work['inputs']['evidence_ids']
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.runtime._literature_remaining(self.task())['tool_calls'], 4)
        done = self.emit(work, self.command(work, 'complete_research', self.result(ids), ids))
        self.assertEqual(done['proposal']['status'], 'applied', done)
        self.assertEqual(self.task()['status'], 'completed')
        lab = self.ready('lab')
        self.assertEqual(lab['inputs']['agent_instance_id'], 'lab')
        self.assertTrue(set(ids).issubset(lab['inputs']['required_evidence_ids']))
        prepared = self.runtime.prepare_meta(lab['id'], {'model_calls': 1})
        packet = json.loads(self.store.read_blob(prepared['metadata']['plan_ref']))
        self.assertTrue(set(ids).issubset({p['evidence_id'] for p in packet['delivery_pages']}))
        self.assertNotIn('hidden-data-field', json.dumps(packet))
        sessions = self.store.list('r', 'agent_session')
        self.assertEqual({s['role'] for s in sessions}, {'lab', 'literature'})
        self.assertEqual(next(s for s in sessions if s['role'] == 'literature')['committed_turn'], 2)
        self.assertFalse(any(w['kind'] == 'meta_discovery' for w in self.store.list('r', 'work')))

    def test_child_cannot_use_registered_non_literature_capability(self):
        self.delegate()
        rejected = self.request_tool(capability='numeric.fixture')['proposal']
        self.assertEqual(rejected['status'], 'rejected')
        self.assertFalse(any(w['kind'] == 'diagnostic' for w in self.store.list('r', 'work')))
        self.assertEqual(self.task()['status'], 'completed')

    def test_claim_cannot_cite_fact_not_delivered_in_this_turn(self):
        work = self.delegate()
        self.controller.add_fact('r', 'unread', 'source_statement', 'Never delivered', {'source': 'paper'}, 'add')
        rejected = self.emit(work, self.command(work, 'complete_research', self.result(['unread']), ['unread']))
        self.assertEqual(rejected['proposal']['status'], 'rejected')

    def test_two_tool_results_preserve_history_but_only_latest_is_required_and_citable_when_narrowed(self):
        self.delegate(max_model_calls=5, max_tool_calls=8)
        self.assertEqual(self.request_tool()['proposal']['status'], 'applied')
        self.execute_tool()
        older = self.ready('literature')['inputs']['evidence_ids'][0]

        second_request = self.request_tool()
        self.assertEqual(second_request['proposal']['status'], 'applied')
        previous_packet = json.loads(self.store.read_blob(second_request['effect']['metadata']['plan_ref']))
        self.assertIn(older, {page['evidence_id'] for page in previous_packet['delivery_pages']},
                      'The older source really was delivered in the preceding child turn')
        self.execute_tool()
        work = self.ready('literature')
        accumulated = work['inputs']['evidence_ids']
        self.assertEqual(len(CALLS), 2, 'Both tools must execute through the real adapter/ledger lifecycle')
        self.assertEqual(len(accumulated), 2)
        latest = next(fid for fid in accumulated if fid != older)
        self.assertEqual(accumulated, [older, latest])
        self.assertEqual(self.task()['evidence_ids'], accumulated)
        self.assertEqual(work['inputs']['required_evidence_ids'], [latest])
        self.assertEqual({row['id'] for row in self.store.list('r', 'fact')}, set(accumulated))

        agent, mode, task, options = self.runtime._context(work)
        arguments = agent.arguments(mode, task, **options)
        # Derive the limit from the actual role/schema/task envelope, as in
        # PacketBuilder's narrowing tests. Ordinary small source passages are
        # sufficient; no fabricated huge documents or removed facts are needed.
        narrowed = agent.builder.build(**arguments, strategy='narrowed')
        agent.builder.max_input_bytes = narrowed['input_bytes']
        planned = agent.prepare(mode, task, **options)
        self.assertEqual(planned['status'], 'ready')
        self.assertEqual(planned['strategy'], 'narrowed')
        packet = agent.for_dispatch(planned, mode, task, **options)
        self.assertEqual(packet['required_evidence_ids'], [latest])
        self.assertEqual(packet['deferred_evidence_ids'], [older])
        self.assertEqual([page['evidence_id'] for page in packet['delivery_pages']], [latest])
        self.assertLessEqual(packet['input_bytes'], agent.builder.max_input_bytes)
        self.assertEqual(self.task()['evidence_ids'], accumulated, 'Narrowing must not erase research history')

        # Previous delivery/persistent history does not authorize an omitted
        # citation in this request, even though the immutable fact still exists.
        with self.assertRaisesRegex(ValueError, 'not actually present in this packet'):
            validate_command(self.command(work, 'complete_research', self.result([older]), [older]),
                packet, current_versions=self.runtime.versions(None))
        accepted = validate_command(self.command(work, 'complete_research', self.result([latest]), [latest]),
            packet, current_versions=self.runtime.versions(None))
        self.assertEqual(accepted['action'], 'complete_research')

    def test_task_identity_spoof_is_rejected(self):
        work = self.delegate()
        result = self.emit(work, self.command(work, 'complete_research', self.result(), task_id='other-child'))
        self.assertEqual(result['proposal']['status'], 'rejected')

    def test_tool_reservation_includes_nested_requests_and_exhaustion_returns_to_lab(self):
        self.delegate(max_tool_calls=2)
        self.request_tool()
        result = self.execute_tool()
        self.assertEqual(result['work']['status'], 'blocked')
        self.assertEqual(CALLS, [])
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual(self.store.get('r', 'budget', 'tool_calls')['reserved'], 0)
        self.assertEqual(self.ready('lab')['inputs']['literature_result_task_id'], self.task()['id'])

    def test_last_model_turn_cannot_launch_unread_tool_result(self):
        self.delegate(max_model_calls=2)
        self.request_tool()
        self.execute_tool()
        self.request_tool()
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual(len(CALLS), 1)
        self.assertFalse(any(w['kind'] == 'diagnostic' for w in self.controller.ready('r')))

    def test_update_returns_sources_to_lab_and_preserves_child_autonomy(self):
        self.delegate()
        self.request_tool()
        self.execute_tool()
        cited = self.ready('literature')['inputs']['evidence_ids'][0]
        self.request_tool()
        self.execute_tool()
        work = self.ready('literature')
        ids = work['inputs']['evidence_ids']
        self.emit(work, self.command(work, 'publish_research_update', self.result([cited]), [cited]))
        self.assertEqual(self.task()['status'], 'active')
        self.assertEqual({w['kind'] for w in self.controller.ready('r')}, {'lab', 'literature'})
        self.assertEqual(self.ready('literature')['inputs']['agent_instance_id'], work['inputs']['agent_instance_id'])
        self.assertEqual(self.ready('literature')['inputs']['evidence_ids'], ids)
        self.assertEqual(self.ready('literature')['inputs']['required_evidence_ids'], [cited])

    def test_restart_replays_tool_publication_without_another_tool_call(self):
        self.delegate()
        self.request_tool()
        with patch.object(self.runtime, 'publish_adapter_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.execute_tool()
        self.runtime = Runtime(self.store, 'r', adapters=self.adapters)
        self.runtime.recover()
        self.runtime.recover()
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(len([w for w in self.controller.ready('r') if w['kind'] == 'literature']), 1)
        self.assertEqual(len(self.ready('literature')['inputs']['evidence_ids']), 1)
        self.store.verify_history('r')

    def test_unknown_tool_keeps_reservation_and_blocks_child_continuation(self):
        self.delegate()
        self.request_tool()
        original_dispatch = self.runtime.controller.dispatch_effect
        def interrupt_dispatch(run_id, effect_id, executor):
            def interrupted(effect, raw):
                raise KeyboardInterrupt('Process lost after durable dispatch')
            return original_dispatch(run_id, effect_id, interrupted)
        with patch.object(self.runtime.controller, 'dispatch_effect', side_effect=interrupt_dispatch):
            with self.assertRaises(KeyboardInterrupt):
                self.execute_tool()
        pending = self.store.list('r', 'effect', status='dispatching')
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['held_reservations']['tool_calls'], 3)
        self.runtime.recover()
        self.assertEqual(self.store.get('r', 'effect', pending[0]['id'])['status'], 'unknown')
        self.assertEqual(self.runtime._literature_remaining(self.task())['tool_calls'], 3)
        self.assertFalse(any(w['kind'] == 'literature' for w in self.controller.ready('r')))

    def test_prepared_tool_resumes_through_driver_then_returns_to_parent(self):
        self.delegate()
        self.request_tool()
        with patch.object(self.runtime.controller, 'dispatch_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.execute_tool()
        def model(effect, raw):
            work = self.store.get('r', 'work', effect['work_id'])
            ids = work['inputs']['evidence_ids']
            command = (self.command(work, 'complete_research', self.result(ids), ids)
                if work['kind'] == 'literature' else self.command(work, 'no_action', {'reason': 'Review complete'}, ids))
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1}, 'response': command,
                'sent_packet_sha256': digest(raw), 'sent_prompt_sha256': digest(json.loads(raw)['prompt'].encode())}
        resumed = Runtime(self.store, 'r', adapters=self.adapters)
        result = resumed.run_until_wait(model, {'model_calls': 1}, max_steps=4)
        self.assertEqual(result['status'], 'quiescent', result)
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(self.task()['status'], 'completed')

    def test_completion_route_replays_after_commit_without_duplicate_parent_work(self):
        self.delegate()
        self.request_tool()
        self.execute_tool()
        work = self.ready('literature')
        ids = work['inputs']['evidence_ids']
        original = self.runtime._mutate
        def interrupt_applied(key, *args):
            if key.startswith('applied:'):
                raise KeyboardInterrupt('Lost after routing committed')
            return original(key, *args)
        with patch.object(self.runtime, '_mutate', side_effect=interrupt_applied):
            with self.assertRaises(KeyboardInterrupt):
                self.emit(work, self.command(work, 'complete_research', self.result(ids), ids))
        self.runtime.recover()
        self.runtime.recover()
        self.assertEqual(len([w for w in self.controller.ready('r') if w['kind'] == 'lab']), 1)
        self.assertEqual(len(self.task()['result_ids']), 1)

    def test_failed_tool_receipt_returns_to_child_without_becoming_paper_evidence(self):
        self.delegate()
        self.request_tool()
        self.execute_tool()
        earlier = self.ready('literature')['inputs']['evidence_ids'][0]
        self.request_tool()
        original_dispatch = self.runtime.controller.dispatch_effect
        def fail_dispatch(run_id, effect_id, executor):
            def failure(effect, raw):
                return {'status': 'failed_definite', 'actual_costs': {'tool_calls': 1}, 'error': 'Provider unavailable'}
            return original_dispatch(run_id, effect_id, failure)
        with patch.object(self.runtime.controller, 'dispatch_effect', side_effect=fail_dispatch):
            self.execute_tool()
        self.runtime.recover()
        child = self.ready('literature')
        self.assertEqual(len(child['inputs']['evidence_ids']), 2)
        failure = next(fid for fid in child['inputs']['evidence_ids'] if fid != earlier)
        self.assertEqual(child['inputs']['required_evidence_ids'], [failure])
        self.assertEqual(self.task()['evidence_ids'], child['inputs']['evidence_ids'])
        fact = self.store.get('r', 'fact', failure)
        self.assertEqual(fact['provenance']['authority'], 'execution_receipt_not_paper_evidence')


if __name__ == '__main__':
    unittest.main()
