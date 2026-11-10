"""Metered engineering proposal fixtures; production uses the real transport."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.producers.engineering import build
from rsi_lab import transport
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store

SCHEMA = {'type': 'object', 'properties': {'source_code': {'type': 'string'}, 'limitations': {'type': 'string'}},
          'required': ['source_code', 'limitations'], 'additionalProperties': False}


class LiveEngineeringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'ledger'); self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={'model_calls': {'limit': 4, 'hard': True},
            'tokens': {'limit': 300000, 'hard': False}, 'wall_seconds': {'limit': 2000, 'hard': False}},
            config={'model': 'fixture-model'})
        self.controller.add_fact('r', 'evidence', 'raw_fact', {'observed': True}, {'kind': 'fixture'}, 'evidence')
        self.controller.open_issue('r', 'issue', ['capability-gap'], 'Capability missing', 'fixture',
            ['evidence'], ['May already exist'], ['Inspect executable delivery'], 'issue')
        self.store.mutate('r', 'intent', 'engineering_requested', lambda tx: tx.insert('registration', 'intent', {
            'kind': 'capability_build_request', 'issue_id': 'issue', 'status': 'requested',
            'proposal': {'capability_id': 'fixture.capability', 'purpose': 'Fixture execution'}}), payload={})
        self.work = self.controller.enqueue('r', 'capability_build', 'intent', 'build-work',
            inputs={'build_intent_id': 'intent', 'issue_id': 'issue'})
        self.runtime = Runtime(self.store, 'r')
        self.calls = []

    def invoke(self, prompt, schema, folder, model, effort, timeout):
        self.calls.append(prompt)
        folder.mkdir(parents=True)
        # If accidentally executed, this proposed source would write a marker.
        response = {'source_code': f'open({str(self.root / "forbidden")!r}, "w").write("executed")',
                    'limitations': 'Synthetic transport fixture only'}
        usage = {'input_tokens': 100, 'output_tokens': 20, 'cached_input_tokens': 80, 'reasoning_tokens': 10}
        events = [{'type': 'thread.started'}, {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(response)}},
            {'type': 'turn.completed', 'usage': usage}]
        result = {'response': response, 'usage': usage, 'usage_complete': True, 'error': None,
                  'wall_seconds': 2.5, 'returncode': 0, 'backend': 'fixture'}
        (folder / 'prompt.txt').write_text(transport.PREAMBLE + prompt)
        (folder / 'schema.json').write_text(json.dumps(schema))
        (folder / 'events.jsonl').write_text('\n'.join(json.dumps(row) for row in events))
        (folder / 'transport_result.json').write_text(json.dumps(result))
        (folder / 'response.json').write_text(json.dumps(response))
        return result

    def test_engineering_is_metered_archived_and_never_executed(self):
        with patch.object(transport, 'invoke', side_effect=self.invoke):
            result = build(self.runtime, self.work['id'], 'Implement requested fixture capability', SCHEMA)
            repeated = build(self.runtime, self.work['id'], 'Implement requested fixture capability', SCHEMA)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['response'], repeated['response'])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.root / 'forbidden').exists())
        self.assertEqual(self.store.get('r', 'work', self.work['id'])['status'], 'ready')
        self.assertEqual(self.store.list('r', 'capability'), [])
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['spent'], 120)
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)
        for ref in result['receipt']['transport_artifacts'].values():
            self.assertTrue(self.store.read_blob(ref))
        self.store.verify_history('r')

    def test_unknown_attempt_keeps_reservations_and_is_never_resent(self):
        def unknown(*args, **kwargs):
            result = self.invoke(*args, **kwargs)
            result.update(error='Unknown remote completion', usage=None, usage_complete=False, returncode=1)
            return result
        with patch.object(transport, 'invoke', side_effect=unknown):
            result = build(self.runtime, self.work['id'], 'Implement requested fixture capability', SCHEMA)
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['response'])
        with patch.object(transport, 'invoke', side_effect=AssertionError('No resend')):
            repeated = build(self.runtime, self.work['id'], 'Implement requested fixture capability', SCHEMA)
            self.assertEqual(repeated['status'], 'unknown')
            with self.assertRaisesRegex(ValueError, 'requires reconciliation'):
                build(self.runtime, self.work['id'], 'A revised prompt is not a recovery', SCHEMA)
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['reserved'], 60000)

    def test_schema_violation_consumes_real_call_but_returns_no_source(self):
        schema = {'type': 'object', 'properties': {'different': {'type': 'string'}},
                  'required': ['different'], 'additionalProperties': False}
        with patch.object(transport, 'invoke', side_effect=self.invoke):
            result = build(self.runtime, self.work['id'], 'Implement fixture', schema)
        self.assertEqual(result['status'], 'failed_definite')
        self.assertIsNone(result['response'])
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 1)
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['reserved'], 0)

    def test_unadmitted_work_and_unvalidated_schema_fail_before_calls(self):
        unrelated = self.controller.enqueue('r', 'lab', 'input', 'lab-work')
        with patch.object(transport, 'invoke', side_effect=AssertionError('No model call')):
            with self.assertRaisesRegex(ValueError, 'admitted capability'):
                build(self.runtime, unrelated['id'], 'Implement fixture', SCHEMA)
            with self.assertRaisesRegex(ValueError, 'unvalidated keyword'):
                build(self.runtime, self.work['id'], 'Implement fixture', {**SCHEMA, 'unsupported': True})
        self.assertEqual(self.store.list('r', 'effect'), [])

    def test_explicit_revision_after_known_result_has_separate_cost(self):
        with patch.object(transport, 'invoke', side_effect=self.invoke):
            first = build(self.runtime, self.work['id'], 'Implement fixture revision 1', SCHEMA)
            second = build(self.runtime, self.work['id'], 'Implement fixture revision 2 with review comments', SCHEMA)
        self.assertNotEqual(first['effect']['id'], second['effect']['id'])
        self.assertEqual(self.store.get('r', 'budget', 'model_calls')['spent'], 2)
        self.assertEqual(self.store.get('r', 'budget', 'tokens')['spent'], 240)


if __name__ == '__main__':
    unittest.main()
