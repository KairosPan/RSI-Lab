"""Literature scheduling context exposes real allowances and only the owning Lab."""
from pathlib import Path
import tempfile
import unittest

from rsi_lab.agent_state import AgentState
from rsi_lab.controller import Controller
from rsi_lab.literature_context import build_literature_context
from rsi_lab.store import Store


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
        self.capabilities = {'literature.search_papers': {'status': 'accepted'},
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

    def test_large_task_description_is_marked_as_incomplete(self):
        self.register('long', self.lab, question='x' * 1201)
        task = self.context()['owned_tasks'][0]
        self.assertEqual(len(task['question']), 1200)
        self.assertTrue(task['description_truncated'])


if __name__ == '__main__':
    unittest.main()
