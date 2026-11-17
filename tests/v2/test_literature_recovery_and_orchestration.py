"""A delegated Literature child under completion records, per-item recovery and orchestration.

These scripted, offline scenarios cover the places where the Literature role
meets machinery that was written for Lab and Meta only. They test routing and
recovery, not model judgment; no provider or model is called.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.literature_context import LITERATURE_DECISION_POLICY_VERSION
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from rsi_literature.adapters import publication
from tests.v2.test_orchestration import install


RES = {'model_calls': 1}
BRIEF = {'question': 'Which control does the supplied experiment report?',
    'intended_use': 'Specify a bounded follow-up experiment',
    'scope': 'The host-registered synthetic article',
    'deliverables': ['The original control passage and coverage limits'],
    'max_model_calls': 8, 'max_tool_calls': 8}
READ = {'capability_id': 'literature.read_document', 'capability_version': '1.0.0',
        'inputs_json': json.dumps({'document_id': 'fixture-paper'})}
UNRESOLVED = {'answer': 'The requested source could not be read.', 'coverage_status': 'unresolved',
              'stop_reason': 'The reading tool was unavailable', 'limitations': ['No original was delivered'],
              'claims': []}
# Host-side conditions that change after a request was admitted.
SWITCH = {'refuse_prepare': False, 'fail_execute': False}


def prepare(inputs):
    if SWITCH['refuse_prepare']:
        raise ValueError('Provider was disabled after this request was admitted')
    return dict(inputs)


def reservations(packet):
    return {'tool_calls': 1}


def execute(effect, packet):
    if SWITCH['fail_execute']:
        return {'status': 'failed_definite', 'actual_costs': {'tool_calls': 1}, 'error': 'Provider unavailable'}
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
            'result': {'status': 'ok', 'data': {'fixture': 'Offline synthetic adapter result'},
                       'provenance': {'fixture': True, 'attempt_count': 0},
                       'coverage': {'synthetic_fixture': True}, 'continuation': None, 'errors': []}}


def tool(operation):
    return Adapter('literature.' + operation, '1.0.0', 'fixture_literature_' + operation,
        {'scope': 'Offline synthetic literature.' + operation,
         'literature_decision_policy': LITERATURE_DECISION_POLICY_VERSION,
         'input_schema': {'type': 'object'}, 'fixture': True},
        prepare, reservations, execute, publication)


def model(response, *, protocol_valid=True, failed=False):
    def call(effect, packet):
        envelope = json.loads(packet)
        receipt = {'status': 'succeeded', 'actual_costs': {'model_calls': 1}, 'response': response,
                   'sent_packet_sha256': digest(packet),
                   'sent_prompt_sha256': digest(envelope['prompt'].encode())}
        if failed:
            # A provider failure with a certified terminal: nothing was answered.
            receipt.update(status='failed_definite', response=None, terminal_received=True,
                           error='Provider refused the request')
        if not protocol_valid:
            # A certified terminal whose native turn nevertheless broke protocol:
            # the operator may discard it, and nothing in it may be applied.
            receipt.update(terminal_received=True, protocol_valid=False)
        return receipt
    return call


class LiteratureRecoveryAndOrchestrationTests(unittest.TestCase):
    def setUp(self):
        SWITCH.update(refuse_prepare=False, fail_execute=False)
        self.addCleanup(SWITCH.update, refuse_prepare=False, fail_execute=False)
        self.sequence = 0

    def open(self, **config):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={'model_calls': {'limit': 64, 'hard': True},
            'tool_calls': {'limit': 64, 'hard': True}, 'tokens': {'limit': 100000, 'hard': False}},
            config={'execution_mode': 'replay',
                'goal': 'Resolve the supplied scientific question', 'max_input_bytes': 500000, **config})
        self.runtime = Runtime(self.store, 'r', adapters=[tool('search_papers'), tool('read_document')])

    def reply(self, work, action, payload):
        mode = 'literature' if work['kind'] == 'literature' else 'lab'
        command = {'mode': mode, 'action': action, 'issue_id': None,
            'expected_versions': self.runtime.versions(None), 'evidence_ids': [],
            'payload': copy.deepcopy(payload)}
        memory = {}
        if mode == 'literature':
            task = self.store.get('r', 'registration', work['inputs']['literature_task_id'])
            command.update(task_id=task['id'], task_revision=task['task_revision'])
        else:
            memory['rationale_summaries'] = [{'kind': 'literature_decision',
                'decision': 'delegate' if action == 'request_literature' else 'not_needed',
                'reason': 'Offline protocol fixture; this is not a model judgment evaluation.',
                'evidence_ids': [], 'related_task_ids': []}]
        return {'command': command, 'working_memory_delta': json.dumps(memory)}

    def task(self):
        return self.store.list('r', 'registration', kind='literature_task')[0]

    def child(self):
        return next(work for work in self.controller.ready('r') if work['kind'] == 'literature')

    def delegate(self):
        self.sequence += 1
        work = self.runtime.enqueue_lab('brief-' + str(self.sequence))
        outcome = self.runtime.run_meta(work['id'], model(self.reply(work, 'request_literature', BRIEF)), RES)
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        return self.task()

    def request_tool(self):
        child = self.child()
        outcome = self.runtime.run_meta(child['id'], model(self.reply(child, 'request_diagnostic', READ)), RES)
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        return next(work for work in self.controller.ready('r') if work['kind'] == 'diagnostic')

    def execute_tool(self, work):
        return self.runtime.execute_adapter('literature.read_document', {'document_id': 'fixture-paper'},
                                            trigger_id=work['id'], work_id=work['id'])

    def results(self):
        return [json.loads(self.store.read_blob(fact['content_ref'])) for fact in self.store.list('r', 'fact')
                if (fact['provenance'] if isinstance(fact['provenance'], dict) else {}).get('producer') == 'literature']

    def test_usage_policy_charges_a_child_call_with_unmeasured_usage(self):
        self.open(unknown_usage_policy='charge_reservation')
        self.delegate()
        child = self.child()
        # The fixture model reports its call but no token usage.
        outcome = self.runtime.run_meta(child['id'], model(self.reply(child, 'complete_research', UNRESOLVED)),
                                        {'model_calls': 1, 'tokens': 10})
        self.assertFalse(outcome['effect'].get('accounting_unknown'))
        self.assertEqual(outcome['effect']['actual_costs']['tokens'], 10)
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        self.assertEqual(self.task()['status'], 'completed')

    def test_a_failed_child_call_is_retried_within_the_allowance(self):
        self.open(model_failure_retries=1)
        self.delegate()
        child = self.child()
        self.runtime.run_meta(child['id'], model(None, failed=True), RES)
        retry = self.child()
        self.assertNotEqual(retry['id'], child['id'])
        self.assertEqual((retry['inputs']['retry_of'], retry['inputs']['retry_attempt']), (child['id'], 1))
        self.assertEqual(retry['inputs']['literature_task_id'], self.task()['id'])
        self.assertEqual(self.task()['status'], 'active')
        self.assertEqual(self.results(), [])

    def returned_to_lab(self):
        return [work for work in self.controller.ready('r')
                if work['kind'] == 'lab' and work['inputs'].get('literature_result_task_id')]

    def test_a_failed_child_call_without_a_retry_returns_the_task_unresolved(self):
        self.open()
        self.delegate()
        self.runtime.run_meta(self.child()['id'], model(None, failed=True), RES)
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual([(row['coverage_status'], row['stop_reason']) for row in self.results()],
                         [('unresolved', 'Provider refused the request')])
        self.assertEqual(len(self.returned_to_lab()), 1)
        settled = self.store.verify_history('r')
        self.runtime.recover()
        self.runtime.recover()
        self.assertEqual(self.store.verify_history('r'), settled)

    def test_a_retry_that_also_fails_ends_the_task_only_then(self):
        self.open(model_failure_retries=1)
        self.delegate()
        self.runtime.run_meta(self.child()['id'], model(None, failed=True), RES)
        self.assertEqual(self.task()['status'], 'active')
        self.runtime.run_meta(self.child()['id'], model(None, failed=True), RES)
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual([row['coverage_status'] for row in self.results()], ['unresolved'])
        self.assertEqual(len(self.returned_to_lab()), 1)

    def test_a_child_action_blocked_while_routing_returns_the_task_unresolved(self):
        self.open()
        self.delegate()
        child = self.child()
        missing = {'evidence_ids': ['never-cached'], 'document_ids': [], 'query': '', 'limit': 1}
        outcome = self.runtime.run_meta(child['id'], model(self.reply(child, 'read_cached_evidence', missing)), RES)
        self.assertEqual(outcome['proposal']['status'], 'blocked_action', outcome['proposal'])
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual([row['coverage_status'] for row in self.results()], ['unresolved'])
        self.assertEqual(len(self.returned_to_lab()), 1)
        self.runtime.apply_meta(outcome['effect']['id'])
        self.runtime.recover()
        self.assertEqual(len(self.results()), 1)
        self.store.verify_history('r')

    def test_an_active_task_nobody_works_on_reads_stalled(self):
        self.open()
        task = self.delegate()
        child = self.child()
        self.store.mutate('r', 'fixture-lost-child-work', 'fixture_lost_child_work',
                          lambda tx: tx.update('work', child['id'], {'status': 'cancelled'}))
        driven = self.runtime.run_until_wait(model(None), RES, max_steps=1)
        self.assertEqual((driven['status'], driven['reason']), ('stalled', 'stalled_literature_tasks'))
        self.assertEqual([row['task_id'] for row in driven['report']['attention']['stalled_literature_tasks']],
                         [task['id']])

    def test_discarded_child_turn_returns_its_task_to_the_lab_in_the_recovery_that_completes_it(self):
        # Recovery records an Effect complete as soon as its proposal is
        # terminal, and never replays a completed Effect. The child's task must
        # therefore go back to its Lab in that same pass, not on a later replay.
        self.open()
        self.delegate()
        child = self.child()
        with self.assertRaisesRegex(ValueError, 'protocol violation'):
            self.runtime.run_meta(child['id'], model(self.reply(child, 'complete_research', UNRESOLVED),
                                                     protocol_valid=False), RES)
        session = next(row for row in self.store.list('r', 'agent_session') if row['role'] == 'literature')
        reason = 'Operator discarded an invalid child turn'
        self.runtime.agents.discard_completed_turn(session['id'], reason,
                                                   'discard-for-rebuild:' + session['active_turn_id'])
        self.runtime.agents.rebuild_session(session['id'], reason, 'rebuild:' + session['id'])
        self.assertEqual(self.task()['status'], 'active')
        self.runtime.recover()
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual([(row['coverage_status'], row['stop_reason']) for row in self.results()],
                         [('unresolved', 'Agent turn explicitly discarded')])
        returned = [work for work in self.controller.ready('r')
                    if work['kind'] == 'lab' and work['inputs'].get('literature_result_task_id')]
        self.assertEqual(len(returned), 1)
        effect = next(row for row in self.store.list('r', 'effect') if row['kind'] == 'literature')
        self.assertEqual([row['effect_id'] for row in self.store.list(
            'r', 'registration', kind='runtime_effect_completion') if row['effect_id'] == effect['id']],
            [effect['id']])
        settled = self.store.verify_history('r')
        self.runtime.recover()
        self.runtime.recover()
        self.assertEqual(self.store.verify_history('r'), settled)
        self.assertEqual(len(self.results()), 1)

    def test_a_literature_tool_disabled_for_the_lab_is_withheld_from_its_child_without_blocking_the_lab(self):
        self.open(orchestration={'no_result_steps': 2, 'max_changes': 2})
        install(self.store, {'disabled_tools': ['literature.search_papers']})
        work = self.runtime.enqueue_lab('brief-1')
        prepared = self.runtime.prepare_meta(work['id'], RES)
        snapshot = json.loads(self.store.read_blob(prepared['metadata']['literature_tool_catalog_ref']))
        self.assertEqual(sorted(snapshot), ['literature.read_document'])
        context = json.loads(self.store.read_blob(prepared['metadata']['plan_ref']))['task']['literature_context']
        self.assertEqual(context['available_tool_count'], 1)
        self.assertEqual(context['tool_catalog_sha256'], digest(snapshot))
        outcome = self.runtime.run_meta(work['id'], model(self.reply(work, 'request_literature', BRIEF)), RES)
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        self.assertEqual(self.task()['capability_versions'], {'literature.read_document': '1.0.0'})
        _, _, task, _ = self.runtime._context(self.child())
        self.assertEqual(sorted(task['capability_catalog']), ['literature.read_document'])
        self.store.verify_history('r')

    def test_a_child_tool_refused_at_execution_returns_to_the_child_as_an_observation(self):
        self.open()
        task = self.delegate()
        tool_work = self.request_tool()
        requested = self.task()
        SWITCH['refuse_prepare'] = True
        result = self.execute_tool(tool_work)
        self.assertEqual(result['rejected'], 'Provider was disabled after this request was admitted')
        self.assertIsNone(result['effect'])
        self.assertEqual(self.store.get('r', 'work', tool_work['id'])['status'], 'failed')
        self.assertFalse([row for row in self.store.list('r', 'effect') if row['metadata'].get('adapter_id')])
        current = self.task()
        self.assertEqual((current['status'], current['task_revision']), ('active', requested['task_revision'] + 1))
        child = self.child()
        self.assertEqual(child['inputs']['literature_task_id'], task['id'])
        fact = self.store.get('r', 'fact', child['inputs']['required_evidence_ids'][0])
        content = json.loads(self.store.read_blob(fact['content_ref']))
        self.assertEqual((content['kind'], content['status'], content['adapter_id']),
                         ('ToolResult', 'rejected_before_execution', 'literature.read_document'))
        self.assertIn('Provider was disabled', content['reason'])
        self.assertEqual(fact['provenance']['authority'], 'execution_receipt_not_paper_evidence')
        self.assertEqual(self.results(), [])
        SWITCH['refuse_prepare'] = False
        done = self.runtime.run_meta(child['id'], model(self.reply(child, 'complete_research', UNRESOLVED)), RES)
        self.assertEqual(done['proposal']['status'], 'applied', done['proposal'])
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual([row['coverage_status'] for row in self.results()], ['unresolved'])
        self.store.verify_history('r')

    def test_recovery_reports_a_child_that_cannot_be_reopened_and_still_drives_other_work(self):
        self.open()
        self.delegate()
        child = self.child()
        self.store.mutate('r', 'fixture-capacity-block', 'fixture_capacity_block', lambda tx: tx.update(
            'work', child['id'], {'status': 'blocked', 'outcome_ref': tx.blob(
                {'status': 'packet_unrepresentable', 'max_input_bytes': 1})}))
        refusal = ValueError('Literature execution must be reconciled before capacity recovery')
        with patch.object(self.runtime, 'recover_literature_work', side_effect=refusal):
            recovered = self.runtime.recover()
            self.assertEqual(recovered['literature_capacity_recovery'], [])
            self.assertEqual([(row['stage'], row['id'], row['reason']) for row in recovered['errors']],
                             [('literature_capacity', child['id'], str(refusal))])
            lab = self.runtime.enqueue_lab('independent-local-work')
            driven = self.runtime.run_until_wait(
                model(self.reply(lab, 'no_action', {'reason': 'Independent local bookkeeping is complete.'})),
                RES, max_steps=1)
        self.assertEqual([row['stage'] for row in driven['recovery_errors']], ['literature_capacity'])
        self.assertEqual([step['proposal_status'] for step in driven['steps']], ['applied'])
        self.store.verify_history('r')

    def test_recovery_isolates_a_failed_child_tool_whose_task_has_moved_on(self):
        self.open()
        self.delegate()
        tool_work = self.request_tool()
        SWITCH['fail_execute'] = True
        failed = self.execute_tool(tool_work)['effect']
        self.assertEqual(failed['status'], 'failed_definite')
        self.assertEqual(self.child()['trigger'], 'tool-failure:' + failed['id'])
        moved = ValueError('Work is outside its active delegated Literature task')
        with patch.object(self.runtime, '_literature_tool_failure', side_effect=moved):
            recovered = self.runtime.recover()
        self.assertIn({'effect_id': failed['id'], 'status': 'blocked_adapter', 'reason': str(moved)},
                      recovered['adapter_results'])
        self.assertEqual(recovered['errors'], [])
        self.store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
