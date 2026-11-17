import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab import roles
from rsi_lab.packets import PacketBuilder, digest, encoded, make_evidence


VERSIONS = {'issue_version': 1, 'evidence_revision': 3, 'scheduling_revision': 5}


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'raw': make_evidence('raw', 'A normalization receipt has no rule-selection evidence.',
                                            kind='raw_fact', source='worker receipt')}
        self.agent = roles.MetaAgent(PacketBuilder(self.catalog))
        self.task = {'issue_id': 'i1', 'expected_versions': VERSIONS, 'goal': 'Validate supplied records'}

    def packet(self, mode='disposition', **kwargs):
        return self.agent.prepare(mode, self.task, evidence_ids=['raw'], required_evidence_ids=['raw'], **kwargs)

    def command(self, action, payload, mode='disposition', **changes):
        return {'command': {'mode': mode, 'action': action, 'issue_id': 'i1',
                            'expected_versions': copy.deepcopy(VERSIONS), 'evidence_ids': ['raw'], 'payload': payload, **changes}}

    def validate(self, response, **kwargs):
        return roles.validate_command(response, self.packet(response['command']['mode']), current_versions=VERSIONS, **kwargs)

    def test_one_agent_switches_two_closed_modes(self):
        issue = {'title': 'Missing normalization evidence', 'problem_kind': 'missing_information', 'scope': 'current records',
                 'hypothesis': 'Normalization was not run', 'competing_explanations': ['It ran but receipt is missing'],
                 'discriminating_check': 'Retrieve the normalization receipt', 'falsifying_observation': 'A valid receipt with applied rules'}
        command = self.validate(self.command('propose_issue', issue, mode='discovery'))
        self.assertEqual(command['action'], 'propose_issue')
        self.assertEqual(self.packet('discovery')['envelope']['model'], self.packet()['envelope']['model'])
        with self.assertRaises(ValueError):
            self.validate(self.command('propose_issue', issue, mode='disposition'))
        with self.assertRaises(ValueError):
            self.validate(self.command('activate_policy', {'version': 'new'}))

    def test_unseen_citations_and_stale_scheduling_versions_rejected(self):
        response = self.command('ask_user', {'question': 'Which scope?', 'options': ['explore', 'reproduce'], 'reason_user_needed': 'Only user knows goal'})
        for changes in ({'evidence_ids': ['unseen']}, {'expected_versions': {**VERSIONS, 'scheduling_revision': 4}}, {'issue_id': 'another'}):
            altered = copy.deepcopy(response); altered['command'].update(changes)
            with self.assertRaises(ValueError):
                self.validate(altered)

    def test_missing_capability_proposal_does_not_register_capability(self):
        response = self.command('request_capability', {'capability_id': 'normalize_records', 'purpose': 'Normalize input records',
            'input_contract': 'structured input records', 'output_contract': 'normalized records and provenance',
            'applicability': 'supported structured records', 'acceptance_checks': ['normal and failed inputs', 'identity and resource receipts'],
            'affects_scientific_results': True})
        result = self.validate(response)
        self.assertEqual(result['action'], 'request_capability')
        with self.assertRaises(ValueError):
            self.validate(self.command('request_diagnostic', {'capability_id': 'normalize_records', 'capability_version': 'v1', 'inputs_json': '{}'}), capabilities={})

    def test_diagnostic_requires_registered_version_and_parameter_object(self):
        caps = {'inspect': {'status': 'accepted', 'version': 'v2'}}
        response = self.command('request_diagnostic', {'capability_id': 'inspect', 'capability_version': 'v2', 'inputs_json': '{"record":"r1"}'})
        self.validate(response, capabilities=caps)
        for field, value in [('capability_version', 'v1'), ('inputs_json', '[]'), ('inputs_json', 'not JSON')]:
            bad = copy.deepcopy(response); bad['command']['payload'][field] = value
            with self.assertRaises(ValueError):
                self.validate(bad, capabilities=caps)

    def test_narrow_request_cannot_expand_source_scope_or_budget(self):
        old = {'source_id': 'authorized', 'source_version': 2, 'object_ids': ['c1', 'c2'], 'fields': ['structure', 'procedure'],
               'query': 'Retrieve evidence', 'max_pages': 3, 'max_bytes': 4000}
        request = {'id': 'r1', 'version': 3, 'query_spec': old}
        narrowed = {**old, 'object_ids': ['c1'], 'fields': ['structure'], 'max_pages': 1, 'max_bytes': 2000}
        response = self.command('narrow_request', {'request_id': 'r1', 'request_version': 3, 'replacement': narrowed})
        self.validate(response, authorized_sources=['authorized'], request=request)
        for key, value in [('object_ids', ['c3']), ('fields', ['private']), ('max_pages', 4), ('max_bytes', 5000), ('source_version', 3)]:
            bad = copy.deepcopy(response); bad['command']['payload']['replacement'][key] = value
            with self.assertRaises(ValueError):
                self.validate(bad, authorized_sources=['authorized'], request=request)

    def test_narrowing_requires_a_strict_scope_or_resource_reduction(self):
        old = {'source_id': 'authorized', 'source_version': 2, 'object_ids': ['c1', 'c2'],
               'fields': ['structure', 'procedure'], 'query': 'Retrieve evidence', 'max_pages': 3, 'max_bytes': 4000}
        request = {'id': 'r1', 'version': 3, 'query_spec': old}
        for changes in ({}, {'query': 'Please retrieve evidence'}, {'object_ids': ['c2', 'c1']},
                        {'fields': ['procedure', 'structure']}, {'object_ids': ['c1', 'c2', 'c1']}):
            response = self.command('narrow_request', {'request_id': 'r1', 'request_version': 3,
                                                       'replacement': {**old, **changes}})
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'strict reduction'):
                self.validate(response, authorized_sources=['authorized'], request=request)
        for changes in ({'object_ids': ['c1']}, {'fields': ['structure']}, {'max_pages': 2}, {'max_bytes': 3999}):
            response = self.command('narrow_request', {'request_id': 'r1', 'request_version': 3,
                                                       'replacement': {**old, **changes}})
            with self.subTest(changes=changes):
                self.assertEqual(self.validate(response, authorized_sources=['authorized'], request=request)['action'], 'narrow_request')

    def test_park_has_specific_dependency_producer_deadline_and_expiry(self):
        payload = {'dependency_kind': 'question', 'dependency_id': 'q1', 'producer_id': 'user', 'observed_version': 1,
                   'wake_condition': 'bound answer delivered', 'deadline_at': '2026-11-06T10:00:00+00:00', 'expiry_action': 'escalate'}
        self.validate(self.command('park', payload))
        for change in ({'deadline_at': '2026-11-06'}, {'producer_id': ''}, {'dependency_id': ''}):
            with self.assertRaises(ValueError):
                self.validate(self.command('park', {**payload, **change}))

    def test_raw_first_review_then_bound_challenge(self):
        initial = self.packet('discovery')
        response = self.command('no_action', {'reason': 'No additional issue from these facts'}, mode='discovery')
        receipt = roles.initial_review_receipt(initial, response, effect_id='actual-effect', response_ref='immutable-response')
        challenged = self.packet('discovery', stage='challenge', prior_review=receipt, owner_interpretation={'claim': 'No gap remains'})
        self.assertIn('owner_interpretation', challenged['envelope']['prompt'])
        self.assertNotIn('owner_interpretation', initial['envelope']['prompt'])
        with self.assertRaises(ValueError):
            self.packet('discovery', owner_interpretation={'claim': 'seeded explanation'})
        with self.assertRaises(ValueError):
            self.packet('discovery', stage='challenge', prior_review={**receipt, 'response_sha256': 'forged'}, owner_interpretation={})
        with self.assertRaises(ValueError):
            roles.initial_review_receipt(challenged, response, effect_id='e2', response_ref='r2')

    def test_raw_first_review_rejects_hypothesis_or_task_interpretation(self):
        evidence = make_evidence('h', 'Owner already thinks this is safe', kind='hypothesis', source='owner response')
        agent = roles.MetaAgent(PacketBuilder({'h': evidence}))
        with self.assertRaises(ValueError):
            agent.prepare('discovery', self.task, evidence_ids=['h'])
        with self.assertRaises(ValueError):
            self.agent.prepare('discovery', {**self.task, 'owner_explanation': 'Accept it'}, evidence_ids=['raw'])

    def test_discovery_is_dual_entry_and_open_review_budget_is_separate(self):
        args = dict(event_id='batch5', trigger_kind='batch_boundary', batch_index=5, last_open_review_batch=0,
                    open_reviews_used=0, max_open_reviews=1, interval_batches=5)
        self.assertTrue(roles.discovery_trigger(**args)['admitted'])
        self.assertFalse(roles.discovery_trigger(**{**args, 'open_reviews_used': 1})['admitted'])
        anomaly = roles.discovery_trigger(**{**args, 'trigger_kind': 'tool_failed', 'open_reviews_used': 1}, anomaly_evidence_ids=['raw'])
        self.assertTrue(anomaly['admitted'])
        self.assertEqual(anomaly['open_review_charge'], 0)
        # Both calls are proposals, not quota mutations; root persists their trigger IDs.
        self.assertEqual(roles.discovery_trigger(**args), roles.discovery_trigger(**args))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.agent = roles.MetaAgent(PacketBuilder({}))
        self.task = {'issue_id': None, 'expected_versions': {'issue_version': 0, 'evidence_revision': 0, 'scheduling_revision': 0}, 'goal': 'Review'}
        self.packet = self.agent.prepare('discovery', self.task)
        self.effect = {'id': 'effect1', 'status': 'dispatching', 'packet_hash': self.packet['packet_sha256']}
        self.response = {'command': {'mode': 'discovery', 'action': 'no_action', 'issue_id': None,
            'expected_versions': self.task['expected_versions'], 'evidence_ids': [], 'payload': {'reason': 'No issue'}}}

    def invoke_fixture(self, prompt, schema, folder, model, effort, timeout):
        folder.mkdir(parents=True)
        (folder/'prompt.txt').write_text(roles.transport.PREAMBLE + prompt)
        usage = {'input_tokens': 20, 'output_tokens': 5}
        result = {'response': self.response, 'usage': usage, 'usage_complete': True, 'error': None, 'wall_seconds': .1, 'returncode': 0}
        (folder/'transport_result.json').write_bytes(encoded(result))
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(self.response)}},
                  {'type': 'turn.completed', 'usage': usage}]
        (folder/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
        return result

    def test_reserved_effect_and_exact_envelope_are_required_before_transport(self):
        executor = roles.CodexExecutor(Path(self.tmp.name))
        with patch.object(roles.transport, 'invoke') as invoke:
            for bad in ({**self.effect, 'status': 'prepared'}, {**self.effect, 'packet_hash': 'wrong'}):
                with self.assertRaises(ValueError):
                    executor(bad, self.packet['envelope_bytes'])
            invoke.assert_not_called()

    def test_actual_prompt_matches_planned_envelope_and_costs_reconcile(self):
        with patch.object(roles.transport, 'invoke', side_effect=self.invoke_fixture) as invoke:
            result = roles.CodexExecutor(self.tmp.name)(self.effect, self.packet['envelope_bytes'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['actual_costs'], {'model_calls': 1, 'tokens': 25, 'wall_seconds': .1})
        self.assertEqual(result['sent_packet_sha256'], self.packet['packet_sha256'])
        invoke.assert_called_once()

    def test_unknown_usage_does_not_become_zero_or_retry(self):
        def unknown(*args, **kwargs):
            result = self.invoke_fixture(*args, **kwargs)
            result.update(usage=None, usage_complete=False, error='Unreconciled transport')
            (args[2]/'transport_result.json').write_bytes(encoded(result))
            return result
        with patch.object(roles.transport, 'invoke', side_effect=unknown) as invoke:
            result = roles.CodexExecutor(self.tmp.name)(self.effect, self.packet['envelope_bytes'])
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('tokens', result['actual_costs'])
        self.assertFalse(result['unknown_usage_is_zero'])
        invoke.assert_called_once()


if __name__ == '__main__':
    unittest.main()
