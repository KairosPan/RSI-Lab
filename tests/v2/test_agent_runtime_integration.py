"""Controller integration with synthetic decisions and a real local fixture tool."""
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from rsi_lab import input_view
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from tests.generic.test_adapter_boundary import fixture_adapter, EXECUTIONS


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)); self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', max_external=2,
            budgets={k: {'limit': v, 'hard': k.endswith('calls')} for k, v in
                     {'model_calls': 20, 'tool_calls': 10, 'tokens': 10000, 'wall_seconds': 100}.items()},
            config={'execution_mode': 'replay', 'open_review_interval_batches': 1})
        self.runtime = Runtime(self.store, 'r', adapters=[fixture_adapter()])
        self.reservations = {'model_calls': 1, 'tokens': 100, 'wall_seconds': 1}
        self.seen = []
        EXECUTIONS.clear()

    def executor(self, effect, raw):
        envelope = json.loads(raw)
        data = input_view.decode(envelope['prompt'].split('INPUT_DATA_JSON:\n', 1)[1], envelope['input_view_mode'])
        self.seen.append(data)
        role = data['task']['agent_state']['role']
        action, payload = 'no_action', {'reason': 'Synthetic fixture is complete'}
        if role == 'lab' and not data['delivered_pages']:
            action, payload = 'request_diagnostic', {'capability_id': 'numeric.fixture',
                'capability_version': '1', 'inputs_json': '{"value":7}'}
        return {'status': 'succeeded', 'terminal_received': True, 'protocol_valid': True, 'synthetic': True,
            'actual_costs': {'model_calls': 0, 'tokens': 0, 'wall_seconds': 0},
            'sent_packet_sha256': digest(raw), 'sent_prompt_sha256': digest(envelope['prompt'].encode()),
            'response': {'command': {'mode': envelope['mode'], 'action': action,
                'issue_id': data['task']['issue_id'], 'expected_versions': data['task']['expected_versions'],
                'evidence_ids': [p['evidence_id'] for p in data['delivered_pages']], 'payload': payload},
                'working_memory_delta': json.dumps({'plan_items': [role + ' private plan'], 'next_step': 'Review registered result'})}}

    def test_lab_tool_result_lab_continuation_and_meta_use_separate_memory(self):
        self.runtime.enqueue_lab('start')
        result = self.runtime.run_until_wait(self.executor, self.reservations, max_steps=6)
        self.assertEqual(result['status'], 'quiescent')
        sessions = {s['role']: s for s in self.store.list('r', 'agent_session')}
        self.assertEqual(sessions['lab']['committed_turn'], 2)
        self.assertEqual(sessions['meta']['committed_turn'], 1)
        self.assertEqual(len(EXECUTIONS), 1)
        self.assertEqual(len(self.store.list('r', 'fact')), 1)
        self.assertEqual(self.runtime.agents.working_memory(sessions['lab']['id'])['content']['plan_items'], ['lab private plan'])
        self.assertEqual(self.runtime.agents.working_memory(sessions['meta']['id'])['content']['plan_items'], ['meta private plan'])
        second_lab = [d for d in self.seen if d['mode'] == 'lab'][1]
        self.assertEqual(second_lab['task']['agent_state']['working_memory']['plan_items'], ['lab private plan'])
        self.runtime.recover(); self.runtime.recover()
        self.assertEqual(len(EXECUTIONS), 1)
        self.store.verify_history('r')

    def test_unknown_lab_does_not_block_independent_meta_with_free_capacity(self):
        work = self.runtime.enqueue_lab('unknown')
        def unknown(effect, raw):
            return {'status': 'unknown', 'actual_costs': {'model_calls': 1}}
        self.runtime.run_lab(work['id'], unknown, self.reservations)
        self.runtime.enqueue_lab('same-owner-waits')
        self.controller.enqueue('r', 'meta_discovery', 'independent', 'independent')
        self.runtime.run_until_wait(self.executor, self.reservations, max_steps=1)
        self.assertEqual([d['mode'] for d in self.seen], ['discovery'])
        lab = self.store.list('r', 'agent_session', role='lab')[0]
        self.assertEqual(lab['status'], 'recovery_required')
        with self.assertRaises(ValueError):
            self.runtime.agents.rebuild_session(lab['id'], 'cannot evade unknown', 'bad-rebuild')

    def test_handoff_reaches_explicit_target_instance_and_does_not_publish_fact(self):
        self.runtime.enqueue_lab('start')
        self.runtime.run_until_wait(self.executor, self.reservations, max_steps=1)
        lab = self.store.list('r', 'agent_session', role='lab')[0]
        custom = self.runtime.agents.ensure_session('meta', 'reviewer', runtime_binding=lab['runtime_binding'])
        handoff = self.runtime.agents.propose_handoff(lab['id'], custom['id'], ['plan_items'], 'Review plan', 'handoff')
        accepted = self.runtime.agents.accept_handoff(handoff['id'], custom['id'], 'accept')
        self.runtime.run_meta(accepted['target_work_id'], self.executor, self.reservations)
        received = self.seen[-1]['task']['agent_state']
        self.assertEqual(received['agent_instance_id'], 'reviewer')
        self.assertEqual(received['handoff']['content']['plan_items'], ['lab private plan'])
        self.assertEqual(self.store.list('r', 'fact'), [])
        with self.assertRaises(ValueError):
            self.runtime.agents.read_working_memory(lab['id'], actor_session_id=custom['id'])

    def test_finalizing_cancels_prepared_turn_and_seals_without_model_call(self):
        work = self.runtime.enqueue_lab('prepared')
        effect = self.runtime.prepare_meta(work['id'], self.reservations)
        result = self.runtime.finalize('operator stop')
        self.assertEqual(self.store.get('r', 'effect', effect['id'])['status'], 'failed_definite')
        self.assertEqual(self.store.get('r', 'agent_session', effect['metadata']['agent_session_id'])['status'], 'idle')
        self.assertEqual(len(result['checkpoints']), 1)
        new = self.runtime.enqueue_lab('queued-after-stop')
        with self.assertRaisesRegex(ValueError, 'Finalizing'):
            self.runtime.prepare_meta(new['id'], self.reservations)
        self.assertEqual(self.runtime.run_until_wait(self.executor, self.reservations)['status'], 'finalizing')
        self.assertEqual(self.seen, [])

    def test_adapter_cannot_impersonate_lab_model_recovery(self):
        with self.assertRaises(ValueError):
            replace(fixture_adapter(), effect_kind='lab')


if __name__ == '__main__':
    unittest.main()
