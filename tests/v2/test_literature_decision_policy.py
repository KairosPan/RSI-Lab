"""Decision-record contract tests; these do not simulate autonomous judgment."""
import copy
import json
import unittest
from unittest.mock import patch

from rsi_lab.literature_context import LITERATURE_DELEGATION_VERSION, literature_tool_catalog
from rsi_lab.packets import PacketBuilder, encoded, make_evidence
from rsi_lab.roles import CodexExecutor, MetaAgent, literature_decision_record, response_schema, validate_command
from rsi_lab.store import digest


VERSIONS = {'issue_version': 0, 'evidence_revision': 0, 'scheduling_revision': 0}
CAPABILITIES = {'literature.read_document': {'status': 'accepted', 'version': '1.0.0'}}
LOADED_CAPABILITIES = {'literature.read_document': {**CAPABILITIES['literature.read_document'],
    'descriptor': {'manifest': {'scope': 'literature', 'enabled_providers': ['enabled-provider']}}},
    'local.calculate': {'status': 'accepted', 'version': '1.0.0'}}


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

    def brief_task(self):
        task = copy.deepcopy(self.task)
        context = task['literature_context']
        context.pop('available_capability_ids')
        context.update(delegation_version=LITERATURE_DELEGATION_VERSION,
                       available=True, tool_selection='literature', available_tool_count=1,
                       tool_catalog_sha256=digest(literature_tool_catalog(LOADED_CAPABILITIES)))
        return task

    def brief_packet(self, task=None):
        return self.agent.prepare('lab', task or self.brief_task(),
                                  evidence_ids=['source'], required_evidence_ids=['source'])

    def brief_response(self):
        response = self.response('request_literature', 'delegate')
        response['command']['payload'].pop('capability_ids')
        return response

    def validate_brief(self, response, packet=None, capabilities=None):
        return validate_command(response, packet or self.brief_packet(), current_versions=VERSIONS,
                                capabilities=LOADED_CAPABILITIES if capabilities is None else capabilities)

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

    def test_brief_schema_is_versioned_without_mutating_legacy_contract(self):
        def literature_payload(schema):
            return next(item['properties']['payload'] for item in schema['properties']['command']['anyOf']
                        if item['properties']['action']['enum'] == ['request_literature'])
        legacy = response_schema('lab')
        modern = response_schema('lab', literature_delegation_version=LITERATURE_DELEGATION_VERSION)
        self.assertIn('capability_ids', literature_payload(legacy)['required'])
        self.assertEqual(set(literature_payload(modern)['properties']), {
            'question', 'intended_use', 'scope', 'deliverables', 'max_model_calls', 'max_tool_calls'})
        self.assertEqual(response_schema('lab'), legacy)
        self.assertEqual(self.validate_brief(self.brief_response())['action'], 'request_literature')
        with self.assertRaises(ValueError):
            self.validate_brief(self.response('request_literature', 'delegate'))
        with self.assertRaises(ValueError):
            self.validate(self.brief_response())
        self.assertEqual(self.validate(self.response('request_literature', 'delegate'))['action'],
                         'request_literature')

    def test_brief_requires_available_exact_catalog_and_deliverables(self):
        response = self.brief_response()
        for change in [{'available': False}, {'available': 1}, {'tool_selection': 'lab'},
                       {'available_tool_count': 2}, {'tool_catalog_sha256': 'not-the-catalog'}]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'exact available'):
                task = self.brief_task()
                task['literature_context'].update(change)
                self.validate_brief(response, self.brief_packet(task))
        for changed in [{}, CAPABILITIES, {**LOADED_CAPABILITIES,
                'literature.read_document': {**LOADED_CAPABILITIES['literature.read_document'],
                                            'version': '2.0.0'}}]:
            with self.subTest(capabilities=changed), self.assertRaisesRegex(ValueError, 'exact available'):
                self.validate_brief(response, capabilities=changed)
        response['command']['payload']['deliverables'] = []
        with self.assertRaisesRegex(ValueError, 'deliverables'):
            self.validate_brief(response)

    def test_new_lab_cannot_bypass_child_but_local_diagnostics_and_legacy_remain_valid(self):
        response = self.response(decision='local_work')
        response['command'].update(action='request_diagnostic', payload={
            'capability_id': 'literature.read_document', 'capability_version': '1.0.0',
            'inputs_json': '{}'})
        with self.assertRaisesRegex(ValueError, 'Lab must delegate'):
            self.validate_brief(response)
        self.assertEqual(self.validate(response)['action'], 'request_diagnostic')
        response['command']['payload']['capability_id'] = 'local.calculate'
        self.assertEqual(self.validate_brief(response)['action'], 'request_diagnostic')

    def test_unknown_delegation_versions_fail_closed_at_schema_and_packet_boundary(self):
        with self.assertRaisesRegex(ValueError, 'delegation version'):
            response_schema('lab', literature_delegation_version='future')
        task = self.brief_task()
        task['literature_context']['delegation_version'] = 'future'
        with self.assertRaisesRegex(ValueError, 'delegation version'):
            self.brief_packet(task)
        packet = self.brief_packet()
        packet['task']['literature_context']['delegation_version'] = 'future'
        with self.assertRaisesRegex(ValueError, 'delegation version'):
            self.validate_brief(self.brief_response(), packet)

    def test_prompts_assign_tool_planning_to_literature_and_research_brief_to_lab(self):
        instruction = self.agent.arguments('lab', self.brief_task())['instruction']
        self.assertIn('Do not choose tool IDs, providers or an operation sequence', instruction)
        self.assertIn('Preserve explicit user-supplied sources and constraints', instruction)
        self.assertNotIn('Choose only the registered capabilities', instruction)
        child_instruction = self.agent.arguments('literature', {})['instruction']
        self.assertIn('You choose which search, reading, extraction and review operations', child_instruction)
        self.assertIn('the specific providers from the admitted task catalog', child_instruction)

    def test_executor_binds_schema_and_actual_task_to_admitted_delegation_metadata(self):
        for modern in (False, True):
            task = self.brief_task() if modern else copy.deepcopy(self.task)
            task.pop('agent_state')
            packet = self.agent.prepare('lab', task)
            packet_bytes = encoded(packet['envelope'])
            metadata = ({'literature_delegation_version': LITERATURE_DELEGATION_VERSION,
                         'literature_tool_catalog_sha256': task['literature_context']['tool_catalog_sha256']}
                        if modern else {})
            effect = {'id': 'contract-test', 'status': 'dispatching',
                      'packet_sha256': digest(packet_bytes), 'metadata': metadata}
            with self.subTest(modern=modern), patch('rsi_lab.roles.transport.invoke',
                    side_effect=RuntimeError('transport boundary reached')) as invoke:
                with self.assertRaisesRegex(RuntimeError, 'transport boundary reached'):
                    CodexExecutor('/tmp/unused-literature-contract-test')(effect, packet_bytes)
                self.assertEqual(invoke.call_count, 1)
                for alteration in ({'literature_tool_catalog_sha256': 'changed'},
                                   {'literature_delegation_version': None if modern else LITERATURE_DELEGATION_VERSION},
                                   {'literature_delegation_version': 'future'}):
                    changed = {**effect, 'metadata': {**metadata, **alteration}}
                    with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                        CodexExecutor('/tmp/unused-literature-contract-test')(changed, packet_bytes)
                self.assertEqual(invoke.call_count, 1)


if __name__ == '__main__':
    unittest.main()
