"""Decision-record contract tests; these do not simulate autonomous judgment."""
import copy
import json
import unittest

from rsi_lab.packets import PacketBuilder, make_evidence
from rsi_lab.roles import MetaAgent, literature_decision_record, validate_command


VERSIONS = {'issue_version': 0, 'evidence_revision': 0, 'scheduling_revision': 0}
CAPABILITIES = {'literature.read_document': {'status': 'accepted', 'version': '1.0.0'}}


class LiteratureDecisionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.agent = MetaAgent(PacketBuilder({
            name: make_evidence(name, text, kind='source_statement', source='test-original')
            for name, text in [('source', 'The supplied protocol defines the decision threshold.'),
                               ('unread', 'This original was never delivered.')]}))
        self.task = {'issue_id': None, 'expected_versions': VERSIONS,
            'goal': 'Choose the next step from the available experimental evidence.',
            'agent_state': {'session_id': 'lab-session'},
            'literature_context': {'version': 'lab_literature_decision_v1',
                'available_capability_ids': list(CAPABILITIES),
                'owned_tasks': [{'task_id': 'owned-task', 'status': 'active',
                    'question': 'Resolve the decision-relevant external assumption.'}],
                'run_allowance': {'model_calls': 10, 'tool_calls': 10},
                'allowance_timing': 'before_this_model_turn'}}

    def packet(self, *, legacy=False):
        task = copy.deepcopy(self.task)
        if legacy:
            task.pop('literature_context')
        return self.agent.prepare('lab', task, evidence_ids=['source'], required_evidence_ids=['source'])

    def response(self, action='no_action', decision='use_delivered_evidence'):
        payload = {'reason': 'The delivered protocol resolves this bounded decision.'}
        if action == 'request_literature':
            payload = {'question': 'Does the unresolved assumption apply under the target conditions?',
                'intended_use': 'Choose the next experiment after resolving this assumption.',
                'scope': 'Only the decision-relevant assumption; finish with source support or an explicit gap.',
                'deliverables': ['Source evidence and applicability limits'],
                'capability_ids': list(CAPABILITIES), 'max_model_calls': 3, 'max_tool_calls': 3}
        record = {'kind': 'literature_decision', 'decision': decision,
            'reason': 'The action follows the currently delivered evidence and the stated scope.',
            'evidence_ids': ['source'], 'related_task_ids': []}
        return {'command': {'mode': 'lab', 'action': action, 'issue_id': None,
            'expected_versions': VERSIONS, 'evidence_ids': ['source'], 'payload': payload},
            'working_memory_delta': json.dumps({'rationale_summaries': [record]})}

    def record(self, response):
        return json.loads(response['working_memory_delta'])['rationale_summaries'][0]

    def replace_record(self, response, record):
        response['working_memory_delta'] = json.dumps({'rationale_summaries': [record]})
        return response

    def validate(self, response, packet=None):
        return validate_command(response, packet or self.packet(), current_versions=VERSIONS,
                                capabilities=CAPABILITIES)

    def test_both_delegation_and_evidence_sufficiency_remain_available(self):
        for action, decision in [('request_literature', 'delegate'),
                                 ('no_action', 'use_delivered_evidence')]:
            with self.subTest(action=action):
                response = self.response(action, decision)
                self.assertEqual(self.validate(response)['action'], action)
                self.assertEqual(literature_decision_record(response, self.packet()), self.record(response))

    def test_versioned_context_requires_exactly_one_auditable_record(self):
        response = self.response()
        record = self.record(response)
        invalid = ['{}', 'not json', '[]', json.dumps({'rationale_summaries': []}),
                   json.dumps({'rationale_summaries': ['A bare undocumented decision.']}),
                   json.dumps({'rationale_summaries': [record, record]}),
                   response['working_memory_delta'].replace('"decision": "use_delivered_evidence"',
                       '"decision": "delegate", "decision": "use_delivered_evidence"')]
        for delta in invalid:
            with self.subTest(delta=delta), self.assertRaisesRegex(ValueError, 'decision record'):
                self.validate({**response, 'working_memory_delta': delta})
        missing = copy.deepcopy(response)
        missing.pop('working_memory_delta')
        with self.assertRaisesRegex(ValueError, 'decision record'):
            self.validate(missing)

    def test_decision_shape_and_action_must_agree(self):
        response = self.response()
        record = self.record(response)
        for changed in [{**record, 'reason': ' '}, {**record, 'decision': 'always_research'},
                        {**record, 'extra_authority': True}, {**record, 'decision': 'delegate'}]:
            with self.subTest(record=changed), self.assertRaises(ValueError):
                self.validate(self.replace_record(copy.deepcopy(response), changed))
        with self.assertRaisesRegex(ValueError, 'agree'):
            self.validate(self.response('request_literature', 'not_needed'))

    def test_catalog_and_other_tasks_do_not_grant_evidence_or_owner_authority(self):
        response = self.response()
        record = self.record(response)
        for changed in [{**record, 'evidence_ids': ['unread']},
                        {**record, 'evidence_ids': ['owned-task']},
                        {**record, 'evidence_ids': ['source', 'source']},
                        {**record, 'related_task_ids': ['another-labs-task']},
                        {**record, 'related_task_ids': ['owned-task', 'owned-task']}]:
            with self.subTest(record=changed), self.assertRaisesRegex(ValueError, 'unavailable'):
                self.validate(self.replace_record(copy.deepcopy(response), changed))
        waiting = self.replace_record(response, {**record, 'decision': 'await_existing',
                                                'related_task_ids': ['owned-task']})
        self.assertEqual(self.validate(waiting)['action'], 'no_action')

    def test_legacy_packets_and_other_roles_keep_their_original_contract(self):
        response = self.response()
        response['working_memory_delta'] = '{}'
        self.assertEqual(self.validate(response, self.packet(legacy=True))['action'], 'no_action')
        self.assertIsNone(literature_decision_record(response, self.packet(legacy=True)))
        packet = copy.deepcopy(self.packet())
        packet['envelope']['mode'] = 'literature'
        self.assertIsNone(literature_decision_record(response, packet))
        discovery_task = {key: value for key, value in self.task.items() if key != 'literature_context'}
        discovery = self.agent.prepare('discovery', discovery_task,
                                       evidence_ids=['source'], required_evidence_ids=['source'])
        self.assertNotIn('lab_literature_decision_v1', discovery['envelope']['prompt'])


if __name__ == '__main__':
    unittest.main()
