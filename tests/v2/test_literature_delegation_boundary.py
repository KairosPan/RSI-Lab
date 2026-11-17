"""Research briefs and child-owned tool choice through real offline Effects.

The scripted responses test routing and authority, not scientific judgment or
model autonomy quality. No provider, model, or molecular evaluator is called.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab import literature_context
from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.literature_context import LITERATURE_DECISION_POLICY_VERSION, LITERATURE_DELEGATION_VERSION
from rsi_lab.roles import validate_command
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from rsi_literature import make_adapters
from rsi_literature.adapters import publication


EXECUTED = []
OPERATIONS = ('search_papers', 'read_document', 'extract_evidence', 'run_review')
BRIEF = {'question': 'Which control does the supplied experiment report?',
    'intended_use': 'Specify a bounded follow-up experiment',
    'scope': 'The host-registered synthetic article',
    'deliverables': ['The original control passage and coverage limits'],
    'max_model_calls': 8, 'max_tool_calls': 8}


def prepare(inputs):
    return dict(inputs)


def reservations(packet):
    return {'tool_calls': 1}


def execute(effect, packet):
    operation = effect['metadata']['adapter_id']
    EXECUTED.append({'effect_id': effect['id'], 'operation': operation, 'inputs': json.loads(packet)})
    data = {'operation': operation, 'fixture': 'Offline synthetic adapter result'}
    if operation == 'literature.read_document':
        text = 'This synthetic experiment reports a matched control.'
        data = {'passages': [{'document_id': 'fixture-paper', 'document_version': digest(b'fixture-document-v1'),
            'source_sha256': digest(b'synthetic-article-original'), 'locator': 'paragraph:1', 'text': text,
            'kind': 'paragraph', 'char_range': [0, len(text)], 'byte_range': [0, len(text.encode())]}]}
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
            'result': {'status': 'ok', 'data': data, 'provenance': {'fixture': True, 'attempt_count': 0},
                       'coverage': {'synthetic_fixture': True}, 'continuation': None, 'errors': []}}


def adapter(operation, *, requires_acceptance=False, version='1.0.0'):
    cid = operation if '.' in operation else 'literature.' + operation
    return Adapter(cid, version, 'fixture_' + cid.replace('.', '_'),
        {'scope': 'Offline synthetic ' + cid,
         'literature_decision_policy': LITERATURE_DECISION_POLICY_VERSION,
         'input_schema': {'type': 'object'}, 'fixture': True},
        prepare, reservations, execute, publication, requires_acceptance=requires_acceptance)


class LiteratureDelegationBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={'model_calls': {'limit': 64, 'hard': True},
            'tool_calls': {'limit': 64, 'hard': True}}, config={'execution_mode': 'replay',
                'goal': 'Resolve the supplied scientific question', 'max_input_bytes': 500000})
        self.adapters = [adapter(op) for op in OPERATIONS] + [adapter('numeric.fixture'),
            adapter('literature.unaccepted', requires_acceptance=True)]
        self.runtime = Runtime(self.store, 'r', adapters=self.adapters)
        self.sequence = 0
        EXECUTED.clear()

    def load(self, ref):
        return json.loads(self.store.read_blob(ref))

    def response(self, work, action, payload, evidence_ids=()):
        mode = 'literature' if work['kind'] == 'literature' else 'lab'
        command = {'mode': mode, 'action': action, 'issue_id': None,
            'expected_versions': self.runtime.versions(None), 'evidence_ids': list(evidence_ids),
            'payload': copy.deepcopy(payload)}
        memory = {}
        if mode == 'literature':
            task = self.store.get('r', 'registration', work['inputs']['literature_task_id'])
            command.update(task_id=task['id'], task_revision=task['task_revision'])
        else:
            memory['rationale_summaries'] = [{'kind': 'literature_decision',
                'decision': 'delegate' if action == 'request_literature' else 'not_needed',
                'reason': 'Offline protocol fixture; this is not a model judgment evaluation.',
                'evidence_ids': list(evidence_ids), 'related_task_ids': []}]
        return {'command': command, 'working_memory_delta': json.dumps(memory)}

    def executor(self, response):
        def call(effect, packet):
            envelope = json.loads(packet)
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1},
                'response': response, 'sent_packet_sha256': digest(packet),
                'sent_prompt_sha256': digest(envelope['prompt'].encode())}
        return call

    def emit(self, work, action, payload, evidence_ids=()):
        return self.runtime.run_meta(work['id'], self.executor(self.response(work, action, payload, evidence_ids)),
                                     {'model_calls': 1})

    def new_lab(self):
        self.sequence += 1
        return self.runtime.enqueue_lab('brief-' + str(self.sequence))

    def ready_child(self, task_id, kind='literature'):
        return next(work for work in self.controller.ready('r') if work['kind'] == kind
                    and work['inputs'].get('literature_task_id') == task_id)

    def delegate(self):
        outcome = self.emit(self.new_lab(), 'request_literature', BRIEF)
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        task = self.store.list('r', 'registration', kind='literature_task',
                               parent_effect_id=outcome['effect']['id'])[0]
        return task, outcome

    def plan(self, work):
        agent, mode, task, options = self.runtime._context(work)
        return agent.prepare(mode, task, **options)

    def validate(self, response, packet):
        return validate_command(response, packet, current_versions=self.runtime.versions(None),
                                capabilities=self.runtime.capability_catalog())

    def test_research_brief_hides_lab_tool_details_and_allows_two_distinct_child_routes(self):
        lab = self.new_lab()
        lab_plan = self.plan(lab)
        context = lab_plan['task']['literature_context']
        self.assertEqual(context['delegation_version'], LITERATURE_DELEGATION_VERSION)
        self.assertTrue(context['available'])
        self.assertEqual(context['tool_selection'], 'literature')
        self.assertEqual(context['available_tool_count'], len(OPERATIONS))
        self.assertEqual(len(context['tool_catalog_sha256']), 64)
        self.assertNotIn('available_capability_ids', context)
        self.assertTrue(all(not cid.startswith('literature.') for cid in lab_plan['task']['capability_catalog']))
        self.assertIn('numeric.fixture', lab_plan['task']['capability_catalog'])
        variants = lab_plan['envelope']['response_schema']['properties']['command']['anyOf']
        delegation = next(row for row in variants if row['properties']['action']['enum'] == ['request_literature'])
        self.assertEqual(set(delegation['properties']['payload']['properties']), set(BRIEF))
        forbidden = self.emit(lab, 'request_diagnostic', {
            'capability_id': 'literature.read_document', 'capability_version': '1.0.0', 'inputs_json': '{}'})
        self.assertEqual(forbidden['proposal']['status'], 'rejected', forbidden['proposal'])
        self.assertFalse(any(work['kind'] == 'diagnostic' for work in self.store.list('r', 'work')))
        old_brief = self.response(lab, 'request_literature',
                                 {**BRIEF, 'capability_ids': ['literature.read_document']})
        with self.assertRaises(ValueError):
            self.validate(old_brief, lab_plan)
        paths = [('search_papers', 'read_document'), ('run_review', 'extract_evidence', 'read_document')]
        observed_paths, tasks = [], []
        for path in paths:
            task, delegated = self.delegate()
            tasks.append(task)
            self.assertEqual(set(task['capability_versions']), {'literature.' + name for name in OPERATIONS})
            child_plan = self.plan(self.ready_child(task['id']))
            self.assertEqual(set(child_plan['task']['capability_catalog']), set(task['capability_versions']))
            start = len(EXECUTED)
            for operation in path:
                child = self.ready_child(task['id'])
                requested = self.emit(child, 'request_diagnostic', {'capability_id': 'literature.' + operation,
                    'capability_version': '1.0.0', 'inputs_json': json.dumps({'document_id': 'fixture-paper'})})
                self.assertEqual(requested['proposal']['status'], 'applied', requested['proposal'])
                work = self.ready_child(task['id'], 'diagnostic')
                result = self.runtime.execute_adapter('literature.' + operation, {'document_id': 'fixture-paper'},
                                                      trigger_id=work['id'], work_id=work['id'])
                self.assertEqual(result['effect']['metadata']['literature_task_id'], task['id'])
                self.assertEqual(result['effect']['actual_costs'], {'tool_calls': 1})
                self.assertEqual(result['effect']['status'], 'succeeded')
            observed_paths.append([row['operation'] for row in EXECUTED[start:]])
            child = self.ready_child(task['id'])
            originals = [fid for fid in child['inputs']['required_evidence_ids']
                if self.store.get('r', 'fact', fid)['provenance'].get('delivery_kind') == 'source_excerpt']
            self.assertEqual(len(originals), 1)
            result = {'answer': 'The synthetic source reports a matched control.', 'coverage_status': 'partial',
                'stop_reason': 'The requested source passage is available', 'limitations': ['Synthetic fixture'],
                'claims': [{'statement': 'The synthetic source reports a matched control.',
                            'evidence_ids': originals, 'limitations': ['As stated in the fixture']}]}
            completed = self.emit(child, 'complete_research', result, originals)
            self.assertEqual(completed['proposal']['status'], 'applied', completed['proposal'])
            finished = self.store.get('r', 'registration', task['id'])
            self.assertEqual(finished['status'], 'completed')
            parent = self.store.get('r', 'work', finished['last_result_work_id'])
            self.assertTrue(set(originals) <= set(parent['inputs']['required_evidence_ids']))
            accepted = self.emit(parent, 'no_action', {'reason': 'The source-backed fixture result was received'})
            self.assertEqual(accepted['proposal']['status'], 'applied')
        self.assertEqual(observed_paths, [['literature.' + name for name in path] for path in paths])
        self.assertEqual(tasks[0]['question'], tasks[1]['question'])
        self.assertEqual(self.store.get('r', 'budget', 'tool_calls')['spent'], sum(map(len, paths)))
        self.store.verify_history('r')

    def test_host_enablement_and_acceptance_define_the_child_menu(self):
        registered = make_adapters(self.root / 'documents', {'enabled_providers': ['europe_pmc']})
        self.runtime = Runtime(self.store, 'r', adapters=registered + [adapter('numeric.fixture'),
            adapter('literature.unaccepted', requires_acceptance=True)])
        self.store.mutate('r', 'verified-without-loaded-adapter', 'fixture_capability_registered',
            lambda tx: tx.insert('capability', 'unloaded-fixture', {'status': 'verified',
                'capability_id': 'literature.not_loaded', 'capability_version': '1.0.0', 'artifact_ref': 'f' * 64}))
        self.assertIn('literature.not_loaded', self.runtime.capability_catalog())
        task, _ = self.delegate()
        child = self.ready_child(task['id'])
        packet = self.plan(child)
        self.assertEqual(set(packet['task']['capability_catalog']), {row.adapter_id for row in registered})
        self.assertNotIn('literature.run_review', task['capability_versions'])
        self.assertNotIn('literature.extract_evidence', task['capability_versions'])
        for cid in ('numeric.fixture', 'literature.unaccepted', 'literature.not_loaded', 'literature.run_review', 'literature.unknown'):
            with self.subTest(capability=cid), self.assertRaises(ValueError):
                self.validate(self.response(child, 'request_diagnostic', {'capability_id': cid,
                    'capability_version': '1.0.0', 'inputs_json': '{}'}), packet)
        search = packet['task']['capability_catalog']['literature.search_papers']['descriptor']['manifest']
        self.assertEqual(search['input_schema']['properties']['provider']['enum'], ['europe_pmc'])
        search_adapter = next(row for row in registered if row.adapter_id == 'literature.search_papers')
        with self.assertRaises(ValueError):
            search_adapter.prepare({'provider': 'semantic_scholar', 'query': BRIEF['question']})
        self.assertEqual(EXECUTED, [])

    def test_newly_loaded_tool_does_not_expand_an_existing_child_task(self):
        task, _ = self.delegate()
        extra = adapter('find_passages')
        self.runtime = Runtime(self.store, 'r', adapters=self.adapters + [extra])
        self.assertIn(extra.adapter_id, self.runtime.capability_catalog())
        child = self.ready_child(task['id'])
        packet = self.plan(child)
        self.assertEqual(set(packet['task']['capability_catalog']), set(task['capability_versions']))
        self.assertNotIn(extra.adapter_id, packet['task']['capability_catalog'])
        with self.assertRaises(ValueError):
            self.validate(self.response(child, 'request_diagnostic', {'capability_id': extra.adapter_id,
                'capability_version': extra.version, 'inputs_json': '{}'}), packet)

    def test_catalog_change_after_lab_admission_rejects_stale_delegation(self):
        work = self.new_lab()
        prepared = self.runtime.prepare_meta(work['id'], {'model_calls': 1})
        response = self.response(work, 'request_literature', BRIEF)
        effect = self.controller.dispatch_effect('r', prepared['id'], self.executor(response))
        self.runtime = Runtime(self.store, 'r', adapters=self.adapters + [adapter('find_passages')])
        outcome = self.runtime.apply_meta(effect['id'])
        self.assertEqual(outcome['status'], 'rejected', outcome)
        self.assertEqual(self.store.list('r', 'registration', kind='literature_task'), [])
        self.assertEqual(EXECUTED, [])
        before = self.store.verify_history('r')
        self.runtime.apply_meta(effect['id'])
        self.assertEqual(self.store.verify_history('r'), before)

    def test_legacy_packet_keeps_explicit_subset_and_replays_without_expansion(self):
        original_context = literature_context.build_literature_context
        def legacy_context(store, run_id, session, capabilities):
            result = original_context(store, run_id, session, capabilities)
            for key in ('delegation_version', 'available', 'tool_selection', 'available_tool_count', 'tool_catalog_sha256'):
                result.pop(key, None)
            result['available_capability_ids'] = sorted(cid for cid in capabilities if cid.startswith('literature.'))
            return result
        work = self.new_lab()
        with patch('rsi_lab.literature_context.build_literature_context', side_effect=legacy_context):
            prepared = self.runtime.prepare_meta(work['id'], {'model_calls': 1})
        plan = self.load(prepared['metadata']['plan_ref'])
        self.assertNotIn('delegation_version', plan['task']['literature_context'])
        brief = {**BRIEF, 'capability_ids': ['literature.read_document']}
        response = self.response(work, 'request_literature', brief)
        effect = self.controller.dispatch_effect('r', prepared['id'], self.executor(response))
        self.runtime = Runtime(self.store, 'r', adapters=self.adapters + [adapter('find_passages')])
        outcome = self.runtime.apply_meta(effect['id'])
        self.assertEqual(outcome['status'], 'applied', outcome)
        task = self.store.list('r', 'registration', kind='literature_task')[0]
        self.assertEqual(task['capability_versions'], {'literature.read_document': '1.0.0'})
        child = self.ready_child(task['id'])
        self.assertEqual(set(self.plan(child)['task']['capability_catalog']), {'literature.read_document'})
        before = self.store.verify_history('r')
        self.runtime.apply_meta(effect['id'])
        self.assertEqual(self.store.verify_history('r'), before)
        self.assertEqual(len(self.store.list('r', 'registration', kind='literature_task')), 1)
        self.assertEqual(EXECUTED, [])


if __name__ == '__main__':
    unittest.main()
