"""Literature scheduling context exposes real allowances and only the owning Lab."""
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab import input_view
from rsi_lab.agent_state import AgentState
from rsi_lab.controller import Controller
from rsi_lab.literature_context import LITERATURE_DECISION_POLICY_VERSION, build_literature_context
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from rsi_domains.literature.adapter import make_adapter as make_legacy_adapter
from rsi_literature import make_adapters


class LiteratureContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.addCleanup(self.store.close)
        Controller(self.store).initialize('r', budgets={
            'model_calls': {'limit': 20, 'hard': True},
            'tool_calls': {'limit': 40, 'hard': True}})
        state = AgentState(self.store, 'r')
        self.lab = state.ensure_session('lab', agent_instance_id='primary')
        self.other = state.ensure_session('lab', agent_instance_id='other')
        self.capabilities = {
            'literature.search_papers': {'status': 'accepted', 'descriptor': {'manifest': {
                'literature_decision_policy': LITERATURE_DECISION_POLICY_VERSION}}},
            'literature.disabled': {'status': 'unavailable'},
            'numeric.calculate': {'status': 'accepted'}}

    def register(self, task_id, parent, status='active', question='Which method is supported?'):
        def insert(tx):
            return tx.insert('registration', task_id, {
                'kind': 'literature_task', 'parent_owner_id': parent['owner_id'],
                'branch_id': parent['branch_id'], 'status': status, 'question': question,
                'scope': 'One decision', 'intended_use': 'Select a method',
                'max_model_calls': 4, 'max_tool_calls': 8, 'result_ids': ['result-pointer']})
        self.store.mutate('r', 'add:' + task_id, 'test_task_registered', insert)

    def context(self):
        return build_literature_context(self.store, 'r', self.lab, self.capabilities)

    def test_only_same_owner_tasks_and_accepted_capabilities_are_exposed(self):
        self.register('owned', self.lab)
        self.register('foreign', self.other)
        value = self.context()
        self.assertEqual([task['task_id'] for task in value['owned_tasks']], ['owned'])
        self.assertEqual(value['available_capability_ids'], ['literature.search_papers'])
        self.assertEqual(value['run_allowance'], {'model_calls': 20, 'tool_calls': 40})
        self.assertEqual(value['new_task_model_call_ceiling'], 18)
        self.assertEqual(value['owned_tasks'][0]['remaining'], {'model_calls': 4, 'tool_calls': 8})
        self.assertIn('not original-source delivery', value['authority'])
        self.assertNotIn('answer', value['owned_tasks'][0])

    def test_active_tasks_precede_history_and_omission_is_explicit(self):
        self.register('active', self.lab)
        for index in range(18):
            self.register('done-' + str(index), self.lab, 'completed')
        value = self.context()
        self.assertEqual(value['owned_tasks'][0]['task_id'], 'active')
        self.assertEqual(len(value['owned_tasks']), 16)
        self.assertEqual(value['owned_task_count'], 19)
        self.assertEqual(value['omitted_task_count'], 3)

    def test_no_literature_capabilities_does_not_change_legacy_context_contract(self):
        self.assertIsNone(build_literature_context(self.store, 'r', self.lab, {}))

    def test_namespace_unavailable_or_unknown_policy_does_not_opt_in(self):
        for capability_id, row in [
            ('literature.search', {'status': 'accepted'}),
            ('literature.search_papers', {'status': 'accepted', 'descriptor': {'manifest': {}}}),
            ('literature.search_papers', {'status': 'unavailable', 'descriptor': {'manifest': {
                'literature_decision_policy': LITERATURE_DECISION_POLICY_VERSION}}}),
            ('literature.search_papers', {'status': 'accepted', 'descriptor': {'manifest': {
                'literature_decision_policy': 'lab_literature_decision_v999'}}}),
            ('numeric.calculate', {'status': 'accepted', 'descriptor': {'manifest': {
                'literature_decision_policy': LITERATURE_DECISION_POLICY_VERSION}}}),
        ]:
            with self.subTest(capability_id=capability_id, row=row):
                self.assertIsNone(build_literature_context(
                    self.store, 'r', self.lab, {capability_id: row}))

    def test_registered_legacy_modern_and_mixed_catalog_response_contracts(self):
        legacy = make_legacy_adapter()
        modern = make_adapters(Path(self.temp.name) / 'documents',
                               {'enabled_providers': ['europe_pmc']})
        self.assertNotIn('literature_decision_policy', legacy.descriptor()['manifest'])
        self.assertTrue(modern)
        for adapter in modern:
            self.assertEqual(adapter.descriptor()['manifest']['literature_decision_policy'],
                             LITERATURE_DECISION_POLICY_VERSION)
        for run_id, adapters, requires_record in [
            ('legacy', [legacy], False), ('modern', modern, True),
            ('mixed', [legacy, *modern], True),
        ]:
            with self.subTest(catalog=run_id):
                Controller(self.store).initialize(run_id, budgets={
                    'model_calls': {'limit': 4, 'hard': True},
                    'tool_calls': {'limit': 4, 'hard': True}})
                runtime = Runtime(self.store, run_id, adapters=adapters)
                received = []
                def executor(effect, packet):
                    envelope = json.loads(packet)
                    received.append(input_view.decode(envelope['prompt'].split('INPUT_DATA_JSON:\n', 1)[1],
                                                      envelope['input_view_mode']))
                    memory = {}
                    if len(received) == 2:
                        memory = {'rationale_summaries': [{
                            'kind': 'literature_decision', 'decision': 'not_needed',
                            'reason': 'This fixture completes a local bookkeeping decision.',
                            'evidence_ids': [], 'related_task_ids': []}]}
                    return {'status': 'succeeded', 'actual_costs': {'model_calls': 1},
                        'sent_packet_sha256': digest(packet),
                        'sent_prompt_sha256': digest(envelope['prompt'].encode()),
                        'response': {'command': {
                            'mode': 'lab', 'action': 'no_action', 'issue_id': None,
                            'expected_versions': runtime.versions(None), 'evidence_ids': [],
                            'payload': {'reason': 'No further local work is pending.'}},
                            'working_memory_delta': json.dumps(memory)}}
                first = runtime.enqueue_lab('without-record')
                result = runtime.run_meta(first['id'], executor, {'model_calls': 1})
                context = received[0]['task'].get('literature_context')
                self.assertEqual(context is not None, requires_record)
                self.assertEqual(result['proposal']['status'],
                                 'rejected' if requires_record else 'applied', result)
                if requires_record:
                    self.assertEqual(context['version'], LITERATURE_DECISION_POLICY_VERSION)
                    self.assertEqual(context['available_capability_ids'],
                                     sorted(adapter.adapter_id for adapter in adapters))
                    second = runtime.enqueue_lab('with-record')
                    result = runtime.run_meta(second['id'], executor, {'model_calls': 1})
                    self.assertEqual(result['proposal']['status'], 'applied', result)

    def test_large_task_description_is_marked_as_incomplete(self):
        self.register('long', self.lab, question='x' * 1201)
        task = self.context()['owned_tasks'][0]
        self.assertEqual(len(task['question']), 1200)
        self.assertTrue(task['description_truncated'])


if __name__ == '__main__':
    unittest.main()
