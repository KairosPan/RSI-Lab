"""Preflight: what a run has switched on, checked before the first model call.

Role responses are scripted and adapters are probes; nothing here calls a model or the network.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from rsi_lab import input_view, roles
from rsi_lab.__main__ import main
from rsi_lab.adapters import Adapter
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from tests.v2.liveness_support import (BUDGETS, ISSUE, RES, Scripted, by_seq, none, one_fact, one_tool_call, open_run,
                                       record_execution, strict_prepare, validating_adapter)

FEATURES = ('scientific_memory', 'meta_lab', 'orchestration')
MODES = ('lab', 'discovery', 'disposition')
OFF = {'declared': False, 'service': False}
ON = {'declared': True, 'service': True}
ORCH = {'orchestration': {'no_result_steps': 2, 'max_changes': 2}}
ALL_ON = {'research_features': {'scientific_memory': True, 'meta_lab': True}, **ORCH}
MEMORY = {'research_features': {'scientific_memory': True}}
CLOSE = ('close_issue', {'resolution': 'closed_unresolved', 'reason': 'not pursued'})


def events(store):
    return store.verify_history('r')['events']


def no_identities(store, run_id, effect, publication):
    return {}


def count_records(effect, publication):
    return {'records': len(publication['facts'])}


def sent_task(store, effect):
    """The task inside the request bytes an Effect holds."""
    envelope = json.loads(store.read_blob(effect['packet_ref']))
    return input_view.decode(envelope['prompt'].rsplit('INPUT_DATA_JSON:\n', 1)[1], envelope['input_view_mode'])['task']


def with_feature_fields(receipt, index):
    """Complete a scripted Lab response with the fields the enabled features require."""
    receipt['response'].update(working_memory_delta=json.dumps({'next_step': 'check the unit map'}),
                               scientific_memory_updates='[]', recommendation_responses=[])
    return receipt


def with_memory(receipt, index):
    """Complete a scripted response with a private working-memory update."""
    receipt['response']['working_memory_delta'] = json.dumps({'next_step': 'check the unit map'})
    return receipt


def disable_for_lab(store, tools):
    """Record a kept Lab settings change directly; the proposal path has its own tests."""
    change = {'kind': 'orchestration_change', 'status': 'kept',
              'settings': {'guidance': None, 'step_digest': 0, 'recent_results': 0, 'disabled_tools': tools}}
    return store.mutate('r', 'test-change', 'test_change_installed',
                        lambda tx: tx.insert('registration', 'change-1', change), change)


def probe(adapter_id, **options):
    return Adapter(adapter_id=adapter_id, version='1', effect_kind=adapter_id + '_tool', manifest={'scope': 'probe'},
                   prepare=strict_prepare, reservations=one_tool_call, execute=record_execution,
                   publication=one_fact, **options)


class FeatureReportTests(unittest.TestCase):
    def test_default_run_reports_all_three_features_off(self):
        store, controller, rt = open_run(self)
        report = rt.feature_report()
        for name in FEATURES:
            self.assertEqual(report[name], OFF, name)
        self.assertIs(report['identity_provider'], False)
        self.assertIs(report['research_policy'], False)
        self.assertEqual(report['adapters'], [])
        self.assertEqual(report['capability_catalog'], [])
        for mode in MODES:
            self.assertEqual(report['modes'][mode], {'actions': list(roles.ACTIONS[mode]),
                                                     'extra_fields': ['working_memory_delta']}, mode)

    def test_scientific_memory_alone_adds_its_service_and_response_field(self):
        store, controller, rt = open_run(self, config={'research_features': {'scientific_memory': True}})
        report = rt.feature_report()
        self.assertEqual([report[name] for name in FEATURES], [ON, OFF, OFF])
        for mode in MODES:
            self.assertEqual(report['modes'][mode]['actions'], list(roles.ACTIONS[mode]), mode)
            self.assertEqual(report['modes'][mode]['extra_fields'],
                             ['scientific_memory_updates', 'working_memory_delta'], mode)

    def test_meta_lab_alone_adds_its_service_action_and_response_fields(self):
        store, controller, rt = open_run(self, config={'research_features': {'meta_lab': True}})
        report = rt.feature_report()
        self.assertEqual([report[name] for name in FEATURES], [OFF, ON, OFF])
        modes = report['modes']
        self.assertEqual(modes['lab']['actions'], list(roles.ACTIONS['lab']))
        self.assertEqual(modes['lab']['extra_fields'], ['recommendation_responses', 'working_memory_delta'])
        for mode in ('discovery', 'disposition'):
            self.assertEqual(modes[mode]['actions'], list(roles.ACTIONS[mode]) + ['recommend_change'], mode)
            self.assertEqual(modes[mode]['extra_fields'], ['improvement_reviews', 'working_memory_delta'], mode)

    def test_orchestration_alone_adds_its_service_and_the_settings_action(self):
        store, controller, rt = open_run(self, config=ORCH)
        report = rt.feature_report()
        self.assertEqual([report[name] for name in FEATURES], [OFF, OFF, ON])
        modes = report['modes']
        self.assertEqual(modes['disposition']['actions'], list(roles.ACTIONS['disposition']) + ['propose_lab_settings'])
        for mode in ('lab', 'discovery'):
            self.assertEqual(modes[mode]['actions'], list(roles.ACTIONS[mode]), mode)

    def test_orchestration_that_admits_no_change_offers_no_settings_action(self):
        store, controller, rt = open_run(self, config={'orchestration': {'no_result_steps': 2, 'max_changes': 0}})
        report = rt.feature_report()
        self.assertEqual(report['orchestration'], ON)
        self.assertEqual(report['modes']['disposition']['actions'], list(roles.ACTIONS['disposition']))

    def test_a_value_other_than_true_declares_nothing(self):
        store, controller, rt = open_run(self, config={'research_features': {'scientific_memory': 'true', 'meta_lab': 1}})
        report = rt.feature_report()
        self.assertEqual([report[name] for name in FEATURES], [OFF, OFF, OFF])

    def test_identity_provider_and_research_policy_are_reported(self):
        store, controller, rt = open_run(self, config={'research_policy': {'recommendation_guidance': 'Prefer one bounded trial.'}})
        self.assertIs(rt.feature_report()['research_policy'], True)
        self.assertIs(rt.feature_report()['identity_provider'], False)
        with_provider = Runtime(store, 'r', scientific_identity_provider=no_identities)
        self.assertIs(with_provider.feature_report()['identity_provider'], True)

    def test_only_a_callable_counts_as_an_identity_provider(self):
        for value in ([], {'identities': []}):
            with self.subTest(value=value):
                store, controller, _ = open_run(self, config=MEMORY)
                rt = Runtime(store, 'r', scientific_identity_provider=value)
                self.assertIs(rt.feature_report()['identity_provider'], False)

    def test_adapters_are_listed_with_measure_acceptance_and_catalog_membership(self):
        adapters = [validating_adapter(), probe('probe_measured', measure=count_records),
                    probe('probe_gated', requires_acceptance=True)]
        store, controller, rt = open_run(self, adapters=adapters)
        report = rt.feature_report()
        self.assertEqual(report['adapters'], [
            {'adapter_id': 'probe', 'version': '1', 'measure': False, 'requires_acceptance': False, 'in_catalog': True},
            {'adapter_id': 'probe_gated', 'version': '1', 'measure': False, 'requires_acceptance': True,
             'in_catalog': False},
            {'adapter_id': 'probe_measured', 'version': '1', 'measure': True, 'requires_acceptance': False,
             'in_catalog': True}])
        self.assertEqual(report['capability_catalog'], [
            {'capability_id': 'probe', 'version': '1', 'admission': 'explicit_host_registration',
             'adapter_loaded': True},
            {'capability_id': 'probe_measured', 'version': '1', 'admission': 'explicit_host_registration',
             'adapter_loaded': True}])

    def test_an_accepted_capability_without_its_adapter_is_in_the_catalog_but_not_loaded(self):
        store, controller, rt = open_run(self)
        row = {'capability_id': 'remote_tool', 'capability_version': '2', 'status': 'verified', 'artifact_ref': 'a' * 64}
        store.mutate('r', 'test-capability', 'test_capability_verified',
                     lambda tx: tx.insert('capability', 'cap-1', row), row)
        report = rt.feature_report()
        self.assertEqual(report['adapters'], [])
        self.assertEqual(report['capability_catalog'], [
            {'capability_id': 'remote_tool', 'version': '2', 'admission': None, 'adapter_loaded': False}])

    def test_the_report_is_plain_json_and_writes_no_event(self):
        store, controller, rt = open_run(self, config=ALL_ON, adapters=[validating_adapter()])
        before = events(store)
        report = rt.feature_report()
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)
        self.assertEqual(events(store), before)


EXPECT_ALL = {'scientific_memory': True, 'identity_provider': True, 'meta_lab': True, 'orchestration': True,
              'capabilities': ['probe']}


class PreflightTests(unittest.TestCase):
    def problems(self, rt, expect, **options):
        """The failure text, split into the one line each unmet expectation gets."""
        with self.assertRaises(ValueError) as caught:
            rt.preflight(expect, **options)
        lines = str(caught.exception).splitlines()
        found = {}
        for line in lines[1:]:
            self.assertTrue(line.startswith('- '), lines)
            key = line[2:].split(':', 1)[0]
            found.setdefault(key, []).append(line)
        return found

    def test_met_expectations_return_the_feature_report(self):
        store, controller, _ = open_run(self, config=ALL_ON)
        rt = Runtime(store, 'r', adapters=[validating_adapter()], scientific_identity_provider=no_identities)
        self.assertEqual(rt.preflight(EXPECT_ALL), rt.feature_report())

    def test_a_default_run_fails_each_expectation_and_says_where_to_supply_it(self):
        store, controller, rt = open_run(self)
        found = self.problems(rt, EXPECT_ALL)
        self.assertEqual(sorted(found), ['capabilities', 'identity_provider', 'meta_lab', 'orchestration',
                                         'scientific_memory'])
        self.assertTrue(all(len(lines) == 1 for lines in found.values()), found)
        self.assertIn('run config', found['scientific_memory'][0])
        self.assertIn('research_features.scientific_memory', found['scientific_memory'][0])
        self.assertIn('run config', found['meta_lab'][0])
        self.assertIn('research_features.meta_lab', found['meta_lab'][0])
        self.assertIn('run config', found['orchestration'][0])
        self.assertIn('"orchestration"', found['orchestration'][0])
        self.assertIn('Runtime(', found['identity_provider'][0])
        self.assertIn('scientific_identity_provider', found['identity_provider'][0])
        self.assertIn("'probe'", found['capabilities'][0])
        self.assertIn('adapters=', found['capabilities'][0])

    def test_declared_scientific_memory_without_an_identity_provider_fails(self):
        store, controller, rt = open_run(self, config={'research_features': {'scientific_memory': True}})
        found = self.problems(rt, {'scientific_memory': True, 'identity_provider': True})
        self.assertEqual(list(found), ['identity_provider'])
        self.assertIn('scientific_identity_provider', found['identity_provider'][0])

    def test_an_expected_capability_outside_the_catalog_fails_by_name(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        found = self.problems(rt, {'capabilities': ['probe', 'probe_absent']})
        self.assertEqual(list(found), ['capabilities'])
        self.assertEqual(len(found['capabilities']), 1)
        self.assertIn("'probe_absent'", found['capabilities'][0])
        self.assertNotIn("'probe'", found['capabilities'][0])
        self.assertIn('adapters=', found['capabilities'][0])

    def test_a_loaded_adapter_that_still_needs_acceptance_is_not_an_available_capability(self):
        store, controller, rt = open_run(self, adapters=[probe('probe_gated', requires_acceptance=True)])
        found = self.problems(rt, {'capabilities': ['probe_gated']})
        self.assertIn("'probe_gated'", found['capabilities'][0])
        self.assertIn('requires_acceptance', found['capabilities'][0])

    def test_an_accepted_capability_without_its_adapter_cannot_be_executed_here(self):
        store, controller, rt = open_run(self)
        row = {'capability_id': 'remote_tool', 'capability_version': '2', 'status': 'verified', 'artifact_ref': 'a' * 64}
        store.mutate('r', 'test-capability', 'test_capability_verified',
                     lambda tx: tx.insert('capability', 'cap-1', row), row)
        found = self.problems(rt, {'capabilities': ['remote_tool']})
        self.assertIn("'remote_tool'", found['capabilities'][0])
        self.assertIn('never executed', found['capabilities'][0])
        self.assertIn('adapters=', found['capabilities'][0])

    def test_an_identity_provider_passed_to_a_run_without_scientific_memory_is_unused(self):
        store, controller, _ = open_run(self)
        rt = Runtime(store, 'r', scientific_identity_provider=no_identities)
        found = self.problems(rt, {'identity_provider': True})
        self.assertIn('unused', found['identity_provider'][0])
        self.assertIn('research_features.scientific_memory', found['identity_provider'][0])

    def test_an_identity_provider_that_cannot_be_called_is_neither_on_nor_off(self):
        for value in ([], {'identities': []}):
            with self.subTest(value=value):
                store, controller, _ = open_run(self, config=MEMORY)
                rt = Runtime(store, 'r', scientific_identity_provider=value)
                found = self.problems(rt, {'scientific_memory': True, 'identity_provider': True})
                self.assertEqual(list(found), ['identity_provider'])
                self.assertIn('not callable', found['identity_provider'][0])
                self.assertIn(type(value).__name__, found['identity_provider'][0])
                found = self.problems(rt, {'identity_provider': False})
                self.assertIn('expected off', found['identity_provider'][0])

    def test_orchestration_set_under_research_features_gets_its_own_hint(self):
        for value in (True, {'no_result_steps': 4, 'max_changes': 2}):
            with self.subTest(value=value):
                store, controller, rt = open_run(self, config={'research_features': {'orchestration': value}})
                found = self.problems(rt, {'orchestration': True})
                self.assertNotIn('only the JSON value true', found['orchestration'][0])
                self.assertIn('research_features.orchestration', found['orchestration'][0])
                self.assertIn('top-level', found['orchestration'][0])

    def test_a_feature_expected_off_fails_when_it_is_on(self):
        store, controller, _ = open_run(self, config={**ALL_ON, 'research_policy': {'recommendation_kinds': ['trial']}})
        rt = Runtime(store, 'r', scientific_identity_provider=no_identities)
        found = self.problems(rt, {'scientific_memory': False, 'meta_lab': False, 'orchestration': False,
                                   'identity_provider': False, 'research_policy': False})
        self.assertEqual(sorted(found), ['identity_provider', 'meta_lab', 'orchestration', 'research_policy',
                                         'scientific_memory'])
        self.assertIn('research_features.scientific_memory', found['scientific_memory'][0])
        self.assertIn('research_features.meta_lab', found['meta_lab'][0])
        self.assertIn('"orchestration"', found['orchestration'][0])
        self.assertIn('scientific_identity_provider', found['identity_provider'][0])
        self.assertIn('"research_policy"', found['research_policy'][0])
        for lines in found.values():
            self.assertIn('expected off', lines[0])

    def test_a_declared_feature_whose_service_is_missing_is_not_on(self):
        store, controller, rt = open_run(self, config=ALL_ON)
        rt.scientific_memory = rt.improvements = rt.orchestration = None
        found = self.problems(rt, {'scientific_memory': True, 'meta_lab': True, 'orchestration': True})
        self.assertEqual(sorted(found), ['meta_lab', 'orchestration', 'scientific_memory'])
        for lines in found.values():
            self.assertIn('built no service', lines[0])

    def test_a_service_without_its_declaration_is_not_off(self):
        store, controller, declared = open_run(self, config=ALL_ON)
        store, controller, rt = open_run(self)
        rt.scientific_memory, rt.improvements, rt.orchestration = (
            declared.scientific_memory, declared.improvements, declared.orchestration)
        found = self.problems(rt, {'scientific_memory': False, 'meta_lab': False, 'orchestration': False})
        self.assertEqual(sorted(found), ['meta_lab', 'orchestration', 'scientific_memory'])
        for lines in found.values():
            self.assertIn('holds its service', lines[0])

    def test_a_default_run_meets_every_expectation_of_off(self):
        store, controller, rt = open_run(self)
        expect = {'scientific_memory': False, 'meta_lab': False, 'orchestration': False, 'identity_provider': False,
                  'research_policy': False, 'capabilities': [], 'measure': []}
        self.assertEqual(rt.preflight(expect), rt.feature_report())

    def test_research_policy_is_an_expectation(self):
        store, controller, rt = open_run(self)
        found = self.problems(rt, {'research_policy': True})
        self.assertIn('run config', found['research_policy'][0])
        self.assertIn('"research_policy"', found['research_policy'][0])
        store, controller, rt = open_run(self, config={'research_policy': {'final_review_guidance': 'Report limits.'}})
        self.assertIs(rt.preflight({'research_policy': True})['research_policy'], True)

    def test_measure_names_adapters_that_must_interpret_their_results(self):
        adapters = [validating_adapter(), probe('probe_measured', measure=count_records)]
        store, controller, rt = open_run(self, config={'research_features': {'meta_lab': True}}, adapters=adapters)
        self.assertEqual(rt.preflight({'measure': ['probe_measured']}), rt.feature_report())
        found = self.problems(rt, {'measure': ['probe_measured', 'probe', 'probe_absent']})
        self.assertEqual(len(found['measure']), 2)
        self.assertIn("'probe'", found['measure'][0])
        self.assertIn('measure=', found['measure'][0])
        self.assertIn("'probe_absent'", found['measure'][1])
        self.assertIn('adapters=', found['measure'][1])

    def test_a_measure_callback_that_can_never_run_fails(self):
        adapters = [probe('probe_measured', measure=count_records),
                    probe('probe_gated', measure=count_records, requires_acceptance=True)]
        store, controller, rt = open_run(self, adapters=adapters)
        found = self.problems(rt, {'measure': ['probe_measured']})
        self.assertIn("'probe_measured'", found['measure'][0])
        self.assertIn('never called', found['measure'][0])
        self.assertIn('research_features.meta_lab', found['measure'][0])
        store, controller, rt = open_run(self, config={'research_features': {'meta_lab': True}}, adapters=adapters)
        found = self.problems(rt, {'measure': ['probe_measured', 'probe_gated']})
        self.assertEqual(len(found['measure']), 1)
        self.assertIn("'probe_gated'", found['measure'][0])
        self.assertIn('requires_acceptance', found['measure'][0])

    def test_a_switch_set_to_something_other_than_true_is_quoted(self):
        store, controller, rt = open_run(self, config={'research_features': {'scientific_memory': 'true'}})
        found = self.problems(rt, {'scientific_memory': True})
        self.assertIn('found "true"', found['scientific_memory'][0])

    def test_an_unknown_expectation_is_refused_rather_than_ignored(self):
        store, controller, rt = open_run(self, config=ALL_ON)
        with self.assertRaises(ValueError) as caught:
            rt.preflight({'scientfic_memory': True})
        self.assertIn("'scientfic_memory'", str(caught.exception))
        self.assertIn('scientific_memory', str(caught.exception).replace("'scientfic_memory'", ''))

    def test_malformed_expectations_are_refused(self):
        store, controller, rt = open_run(self, config=ALL_ON, adapters=[validating_adapter()])
        bad = [None, ['scientific_memory'], {'scientific_memory': 'yes'}, {'meta_lab': 1}, {'orchestration': None},
               {'capabilities': 'probe'}, {'capabilities': [1]}, {'capabilities': ['']}, {'measure': {'probe': True}},
               {'capabilities': ['probe', 'probe']}]
        for expect in bad:
            with self.subTest(expect=expect):
                with self.assertRaises(ValueError) as caught:
                    rt.preflight(expect)
                self.assertNotIn('expectation(s) not met', str(caught.exception))

    def test_a_catalog_the_runtime_refuses_fails_preflight_with_its_reason(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        row = {'capability_id': 'probe', 'capability_version': '2', 'status': 'verified', 'artifact_ref': 'a' * 64}
        store.mutate('r', 'test-capability', 'test_capability_verified',
                     lambda tx: tx.insert('capability', 'cap-1', row), row)
        with self.assertRaisesRegex(ValueError, 'Host adapter differs from the accepted artifact'):
            rt.preflight({})

    def test_neither_a_passing_nor_a_failing_preflight_writes_an_event(self):
        store, controller, _ = open_run(self, config=ALL_ON)
        rt = Runtime(store, 'r', adapters=[validating_adapter()], scientific_identity_provider=no_identities)
        before = events(store)
        rt.preflight(EXPECT_ALL)
        with self.assertRaises(ValueError):
            rt.preflight({**EXPECT_ALL, 'capabilities': ['probe_absent'], 'research_policy': True})
        self.assertEqual(events(store), before)
        self.assertEqual(store.list('r', 'agent_session'), [])


ABSENT = {'task.scientific_memory': False, 'task.constraints.research_loop': False,
          'task.constraints.orchestration': False, 'task.constraints.research_policy': False}


class RequestReportTests(unittest.TestCase):
    def test_a_default_lab_request_carries_no_research_feature(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        work = rt.enqueue_lab('lab-1')
        report = rt.request_report(work['id'])
        self.assertEqual((report['work_id'], report['work_kind'], report['mode']), (work['id'], 'lab', 'lab'))
        self.assertEqual((report['status'], report['reason']), ('ready', None))
        self.assertEqual(report['present'], ABSENT)
        self.assertIs(report['final_review_only'], False)
        self.assertEqual(report['capabilities'], ['probe'])
        self.assertEqual(report['actions'], list(roles.ACTIONS['lab']))
        self.assertEqual(report['extra_fields'], ['working_memory_delta'])

    def test_every_enabled_feature_reaches_the_lab_request(self):
        config = {**ALL_ON, 'research_policy': {'recommendation_kinds': ['trial']}}
        store, controller, rt = open_run(self, config=config, adapters=[validating_adapter()])
        work = rt.enqueue_lab('lab-1')
        report = rt.request_report(work['id'])
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['present'], {name: True for name in ABSENT})
        self.assertEqual(report['capabilities'], ['probe'])
        expected = rt.feature_report()['modes']['lab']
        self.assertEqual((report['actions'], report['extra_fields']), (expected['actions'], expected['extra_fields']))
        self.assertEqual(report['extra_fields'],
                         ['recommendation_responses', 'scientific_memory_updates', 'working_memory_delta'])
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)

    def same_packet(self, rt, work_id):
        """The report for a Work, checked against the Effect the real path then prepares for it."""
        report = rt.request_report(work_id)
        self.assertEqual(report['status'], 'ready', report)
        self.assertEqual(rt.prepare_meta(work_id, RES)['packet_hash'], report['packet_sha256'])
        return report

    def test_building_the_report_writes_no_event_session_or_question(self):
        for config in ({}, ALL_ON):
            with self.subTest(config=config):
                store, controller, rt = open_run(self, config=config, adapters=[validating_adapter()])
                work = rt.enqueue_lab('lab-1')
                before = events(store)
                self.assertEqual(rt.request_report(work['id']), rt.request_report(work['id']))
                self.assertEqual(events(store), before)
                self.assertEqual(store.list('r', 'agent_owner') + store.list('r', 'agent_session'), [])
                self.assertEqual(store.list('r', 'registration'), [])
                self.assertEqual(store.get('r', 'work', work['id'])['status'], 'ready')

    def test_the_reported_packet_is_the_one_the_first_call_sends(self):
        for config in ({}, ALL_ON):
            with self.subTest(config=config):
                store, controller, rt = open_run(self, config=config, adapters=[validating_adapter()])
                controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
                work = rt.enqueue_lab('lab-1', ['f1'])
                self.same_packet(rt, work['id'])

    def test_a_discovery_request_is_reported_as_sent(self):
        store, controller, rt = open_run(self, config=ALL_ON, adapters=[validating_adapter()])
        controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
        report = self.same_packet(rt, rt.observe('e1', 'input_changed', 0, ['f1'])['work_id'])
        self.assertEqual((report['work_kind'], report['mode']), ('meta_discovery', 'discovery'))
        self.assertEqual(report['present'], {**ABSENT, 'task.scientific_memory': True,
                                             'task.constraints.research_loop': True,
                                             'task.constraints.orchestration': True})
        expected = rt.feature_report()['modes']['discovery']
        self.assertEqual((report['actions'], report['extra_fields']), (expected['actions'], expected['extra_fields']))
        self.assertIn('recommend_change', report['actions'])

    def test_a_disposition_request_is_reported_as_sent(self):
        store, controller, rt = open_run(self, config=ALL_ON, adapters=[validating_adapter()])
        controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
        controller.open_issue('r', 'i1', ['anomaly', 's', 't'], 'h', 's', ['f1'], ['alt'], ['c', 'f'], 'i1')
        work = next(w for w in controller.ready('r') if w['kind'] == 'meta_disposition')
        report = self.same_packet(rt, work['id'])
        self.assertEqual((report['work_kind'], report['mode']), ('meta_disposition', 'disposition'))
        expected = rt.feature_report()['modes']['disposition']
        self.assertEqual((report['actions'], report['extra_fields']), (expected['actions'], expected['extra_fields']))
        self.assertIn('propose_lab_settings', report['actions'])

    def test_a_later_request_of_an_existing_session_is_reported_as_sent(self):
        store, controller, rt = open_run(self, config=ALL_ON, adapters=[validating_adapter()])
        controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
        first = rt.enqueue_lab('lab-1', ['f1'])
        out = rt.run_lab(first['id'], Scripted({'lab': none}, receipt=with_feature_fields), RES)
        self.assertEqual(out['proposal']['status'], 'applied', out['proposal'])
        second = rt.enqueue_lab('lab-2', ['f1'])
        before = events(store)
        report = rt.request_report(second['id'])
        self.assertEqual(events(store), before)
        effect = rt.prepare_meta(second['id'], RES)
        self.assertEqual(effect['packet_hash'], report['packet_sha256'])
        self.assertEqual(sent_task(store, effect)['agent_state']['working_memory'], {'next_step': 'check the unit map'})

    def test_a_handoff_request_is_reported_as_sent(self):
        store, controller, rt = open_run(self)
        controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
        rt.run_lab(rt.enqueue_lab('lab-1', ['f1'])['id'], Scripted({'lab': none}, receipt=with_memory), RES)
        rt.run_meta(rt.observe('e1', 'input_changed', 0, ['f1'])['work_id'], Scripted({'discovery': none}), RES)
        sessions = {row['role']: row['id'] for row in store.list('r', 'agent_session')}
        handoff = rt.agents.propose_handoff(sessions['lab'], sessions['meta'], ['next_step'], 'Share the plan', 'h1')
        work_id = rt.agents.accept_handoff(handoff['id'], sessions['meta'], 'h1-accept')['target_work_id']
        before = events(store)
        report = rt.request_report(work_id)
        self.assertEqual(events(store), before)
        effect = rt.prepare_meta(work_id, RES)
        self.assertEqual(effect['packet_hash'], report['packet_sha256'])
        self.assertEqual(sent_task(store, effect)['agent_state']['handoff']['content'],
                         {'next_step': 'check the unit map'})

    def test_a_handoff_work_for_an_owner_without_a_session_fails_as_the_real_call_does(self):
        store, controller, rt = open_run(self)
        controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
        rt.run_lab(rt.enqueue_lab('lab-1', ['f1'])['id'], Scripted({'lab': none}), RES)
        rt.run_meta(rt.observe('e1', 'input_changed', 0, ['f1'])['work_id'], Scripted({'discovery': none}), RES)
        sessions = {row['role']: row['id'] for row in store.list('r', 'agent_session')}
        handoff = rt.agents.propose_handoff(sessions['lab'], sessions['meta'], ['next_step'], 'Share the plan', 'h1')
        rt.agents.accept_handoff(handoff['id'], sessions['meta'], 'h1-accept')
        inputs = {'agent_instance_id': 'inspector-2', 'trigger': {}, 'evidence_ids': []}
        stray = controller.enqueue('r', 'meta_discovery', 'h', 'h', inputs={**inputs, 'handoff_id': handoff['id']})
        unknown = controller.enqueue('r', 'meta_discovery', 'u', 'u', inputs={**inputs, 'handoff_id': 'handoff_none'})
        before = events(store)
        with self.assertRaisesRegex(ValueError, 'Only the current recipient can read an accepted handoff'):
            rt.request_report(stray['id'])
        with self.assertRaises(KeyError):
            rt.request_report(unknown['id'])
        self.assertEqual(events(store), before)
        with self.assertRaises(KeyError):
            rt.prepare_meta(unknown['id'], RES)
        with self.assertRaisesRegex(ValueError, 'Only the current recipient can read an accepted handoff'):
            rt.prepare_meta(stray['id'], RES)

    def test_a_resumed_run_with_imported_memory_is_reported_as_sent(self):
        old_store, old_controller, old = open_run(self, config=MEMORY, run_id='old')
        for i in range(3):
            old_controller.add_fact('old', 'f%d' % i, 'raw_fact', {'reading': i, 'note': 'x' * 40},
                                    {'source': 'fixture'}, 'k%d' % i)
            old.scientific_memory.observe_fact('f%d' % i)
        # The context budget fits one object, so the selection order decides the request.
        store, controller, rt = open_run(self, config={**MEMORY, 'scientific_memory_context_bytes': 1400})
        rt.scientific_memory.import_bundle(old.scientific_memory.export_bundle())
        work = rt.enqueue_lab('lab-1')
        before = events(store)
        report = rt.request_report(work['id'])
        self.assertEqual(events(store), before)
        effect = rt.prepare_meta(work['id'], RES)
        self.assertEqual(effect['packet_hash'], report['packet_sha256'])
        memory = sent_task(store, effect)['scientific_memory']
        self.assertEqual([row['id'] for row in memory['objects']], [rt.scientific_memory.ensure_question()['id']])
        self.assertEqual(memory['omission_count'], 4)

    def test_a_request_too_large_to_send_is_reported_not_ready(self):
        store, controller, rt = open_run(self, config={'max_input_bytes': 20000})
        controller.add_fact('r', 'big', 'raw_fact', {'text': 'x' * 30000}, {'source': 'bulk'}, 'big')
        work = rt.enqueue_lab('lab-1', ['big'])
        before = events(store)
        report = rt.request_report(work['id'])
        self.assertEqual(report['status'], 'packet_unrepresentable')
        self.assertIn('does not fit', report['reason'])
        self.assertEqual((report['input_bytes'], report['packet_sha256']), (None, None))
        self.assertEqual((report['actions'], report['extra_fields']), (None, None))
        self.assertEqual(report['present'], ABSENT)
        self.assertEqual(events(store), before)
        self.assertEqual(rt.prepare_meta(work['id'], RES)['status'], 'blocked')

    def test_a_blocked_scientific_memory_context_is_reported_not_ready(self):
        store, controller, rt = open_run(self, config={**MEMORY, 'scientific_memory_context_bytes': 512})
        work = rt.enqueue_lab('lab-1')
        before = events(store)
        report = rt.request_report(work['id'])
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('byte_budget', report['reason'])
        self.assertEqual((report['input_bytes'], report['packet_sha256']), (None, None))
        self.assertEqual(events(store), before)
        self.assertEqual(rt.prepare_meta(work['id'], RES)['status'], 'blocked')

    def test_a_blocked_context_naming_an_object_that_is_not_text_is_reported(self):
        store, controller, rt = open_run(self, config=MEMORY)
        work = controller.enqueue('r', 'lab', 'q1', 'q1', inputs={'scientific_query': {'object_ids': [123]}})
        report = rt.request_report(work['id'])
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('123', report['reason'])
        self.assertEqual(rt.prepare_meta(work['id'], RES)['status'], 'blocked')

    def test_a_blocked_context_is_reported_where_the_real_call_stops(self):
        # The real call blocks this Work before the raw-first discovery rule is ever applied.
        store, controller, rt = open_run(self, config={**MEMORY, 'scientific_memory_context_bytes': 512})
        controller.add_fact('r', 'hyp', 'hypothesis', {'x': 1}, {'source': 'fixture'}, 'k1')
        work = controller.enqueue('r', 'meta_discovery', 'd1', 'd1', inputs={'trigger': {}, 'evidence_ids': ['hyp']})
        report = rt.request_report(work['id'])
        self.assertEqual((report['status'], report['actions'], report['extra_fields']), ('blocked', None, None))
        self.assertEqual(rt.prepare_meta(work['id'], RES)['status'], 'blocked')

    def test_only_a_ready_lab_or_meta_work_has_a_request(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        leased = rt.enqueue_lab('lab-1')
        rt.prepare_meta(leased['id'], RES)
        tool = controller.enqueue('r', 'diagnostic', 'tool-1', 'tool-1', inputs={'capability_id': 'probe'})
        for work, state in ((leased, 'lab Work in status leased'), (tool, 'diagnostic Work in status ready')):
            with self.subTest(kind=work['kind']):
                with self.assertRaises(ValueError) as caught:
                    rt.request_report(work['id'])
                self.assertIn(state, str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'no Work work_missing'):
            rt.request_report('work_missing')

    def test_a_replayed_enqueue_names_a_work_that_is_no_longer_ready(self):
        store, controller, rt = open_run(self)
        first = rt.enqueue_lab('start')
        rt.run_lab(first['id'], Scripted({'lab': none}), RES)
        again = rt.enqueue_lab('start')
        self.assertEqual((again['id'], again['status']), (first['id'], 'ready'))
        with self.assertRaisesRegex(ValueError, 'lab Work in status succeeded'):
            rt.request_report(again['id'])

    def test_a_tool_disabled_for_the_lab_is_absent_from_its_request_only(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter(), probe('probe2')])
        disable_for_lab(store, ['probe'])
        work = rt.enqueue_lab('lab-1')
        self.assertEqual(self.same_packet(rt, work['id'])['capabilities'], ['probe2'])
        self.assertEqual([row['capability_id'] for row in rt.feature_report()['capability_catalog']],
                         ['probe', 'probe2'])

    def test_a_final_review_request_offers_no_capability_and_one_action(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        work = controller.enqueue('r', 'lab', 'final', 'final', inputs={'final_review_only': True})
        report = self.same_packet(rt, work['id'])
        self.assertIs(report['final_review_only'], True)
        self.assertEqual((report['capabilities'], report['actions']), ([], ['no_action']))


class StrippedRequests(Runtime):
    """A kernel fault to detect: the services exist, yet the request leaves their blocks out."""

    def _context(self, work, orchestration=None, *, read_only=False):
        agent, mode, task, options = super()._context(work, orchestration, read_only=read_only)
        task.pop('scientific_memory')
        for name in ('research_loop', 'orchestration', 'research_policy'):
            task['constraints'].pop(name)
        return agent, mode, task, options


class UnexpectedBlocks(Runtime):
    """The opposite fault: a request that carries blocks of features the run does not have."""

    def _context(self, work, orchestration=None, *, read_only=False):
        agent, mode, task, options = super()._context(work, orchestration, read_only=read_only)
        task['scientific_memory'] = {'status': 'ready', 'objects': []}
        task['constraints'].update(research_loop={}, orchestration={'guidance': None, 'recent_steps': []},
                                   research_policy={'recommendation_kinds': ['trial']})
        return agent, mode, task, options


class RequestPreflightTests(unittest.TestCase):
    problems = PreflightTests.problems

    def test_with_a_work_the_request_is_checked_too_and_returned(self):
        store, controller, _ = open_run(self, config=ALL_ON)
        rt = Runtime(store, 'r', adapters=[validating_adapter()], scientific_identity_provider=no_identities)
        work = rt.enqueue_lab('lab-1')
        before = events(store)
        report = rt.preflight(EXPECT_ALL, work_id=work['id'])
        self.assertEqual(report, {**rt.feature_report(), 'request': rt.request_report(work['id'])})
        self.assertEqual(events(store), before)
        self.assertEqual(store.list('r', 'agent_session'), [])

    def test_the_documented_host_pattern_holds_on_a_fresh_and_a_resumed_run(self):
        store, controller, _ = open_run(self, config=ALL_ON)

        def host():
            # As in docs/rsi-lab-core.md: check the run, then every ready Lab request.
            rt = Runtime(store, 'r', adapters=[validating_adapter()], scientific_identity_provider=no_identities)
            rt.preflight(EXPECT_ALL)
            rt.enqueue_lab('start')
            ready = [work for work in rt.controller.ready('r') if work['kind'] == 'lab']
            for work in ready:
                rt.preflight(EXPECT_ALL, work_id=work['id'])
            return rt, ready

        rt, ready = host()
        self.assertEqual(len(ready), 1)
        out = rt.run_lab(ready[0]['id'], Scripted({'lab': none}, receipt=with_feature_fields), RES)
        self.assertEqual(out['proposal']['status'], 'applied', out['proposal'])
        # The replayed enqueue names a finished Work; nothing is ready, and the run check still passes.
        self.assertEqual(host()[1], [])

    def test_a_capability_the_run_has_but_the_request_lacks_fails(self):
        store, controller, rt = open_run(self, config=ORCH, adapters=[validating_adapter(), probe('probe2')])
        disable_for_lab(store, ['probe'])
        work = rt.enqueue_lab('lab-1')
        expect = {'orchestration': True, 'capabilities': ['probe', 'probe2']}
        self.assertEqual(rt.preflight(expect), rt.feature_report())
        found = self.problems(rt, expect, work_id=work['id'])
        self.assertEqual(list(found), ['capabilities'])
        self.assertEqual(len(found['capabilities']), 1)
        self.assertIn("'probe'", found['capabilities'][0])
        self.assertIn(work['id'], found['capabilities'][0])
        self.assertIn('disabled_tools', found['capabilities'][0])

    def test_a_final_review_request_fails_a_capability_expectation_and_says_why(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        work = controller.enqueue('r', 'lab', 'final', 'final', inputs={'final_review_only': True})
        found = self.problems(rt, {'capabilities': ['probe']}, work_id=work['id'])
        self.assertIn('final-review', found['capabilities'][0])

    def test_a_request_that_cannot_be_sent_fails(self):
        store, controller, rt = open_run(self, config={'max_input_bytes': 20000})
        controller.add_fact('r', 'big', 'raw_fact', {'text': 'x' * 30000}, {'source': 'bulk'}, 'big')
        work = rt.enqueue_lab('lab-1', ['big'])
        found = self.problems(rt, {'scientific_memory': False}, work_id=work['id'])
        self.assertEqual(list(found), ['request'])
        self.assertIn(work['id'], found['request'][0])
        self.assertIn('packet_unrepresentable', found['request'][0])

    def test_a_work_without_a_request_fails_as_an_expectation(self):
        store, controller, rt = open_run(self, adapters=[validating_adapter()])
        tool = controller.enqueue('r', 'diagnostic', 'tool-1', 'tool-1', inputs={'capability_id': 'probe'})
        found = self.problems(rt, {}, work_id=tool['id'])
        self.assertEqual(list(found), ['request'])
        self.assertIn('ready Lab or Meta Work', found['request'][0])

    def test_a_request_that_cannot_be_built_is_listed_with_the_kind_of_error(self):
        store, controller, rt = open_run(self)
        work = controller.enqueue('r', 'lab', 'h', 'h', inputs={'handoff_id': 'handoff_none'})
        found = self.problems(rt, {}, work_id=work['id'])
        self.assertEqual(list(found), ['request'])
        self.assertIn('KeyError', found['request'][0])
        self.assertIn('agent_handoff', found['request'][0])

    def test_a_block_missing_from_the_request_fails_although_its_service_is_on(self):
        config = {**ALL_ON, 'research_policy': {'recommendation_kinds': ['trial']}}
        store, controller, _ = open_run(self, config=config)
        rt = StrippedRequests(store, 'r')
        work = rt.enqueue_lab('lab-1')
        expect = {'scientific_memory': True, 'meta_lab': True, 'orchestration': True, 'research_policy': True}
        self.assertEqual(rt.preflight(expect), rt.feature_report())
        found = self.problems(rt, expect, work_id=work['id'])
        self.assertEqual(sorted(found), ['meta_lab', 'orchestration', 'research_policy', 'scientific_memory'])
        self.assertIn('task.scientific_memory', found['scientific_memory'][0])
        self.assertIn('task.constraints.research_loop', found['meta_lab'][0])
        self.assertIn('task.constraints.orchestration', found['orchestration'][0])
        self.assertIn('task.constraints.research_policy', found['research_policy'][0])
        for lines in found.values():
            self.assertIn(work['id'], lines[0])

    def test_a_block_in_the_request_fails_an_expectation_of_off(self):
        store, controller, _ = open_run(self)
        rt = UnexpectedBlocks(store, 'r')
        work = rt.enqueue_lab('lab-1')
        expect = {'scientific_memory': False, 'meta_lab': False, 'orchestration': False, 'research_policy': False}
        self.assertEqual(rt.preflight(expect), rt.feature_report())
        found = self.problems(rt, expect, work_id=work['id'])
        self.assertEqual(sorted(found), ['meta_lab', 'orchestration', 'research_policy', 'scientific_memory'])
        for lines in found.values():
            self.assertIn('expected off', lines[0])
            self.assertIn(work['id'], lines[0])

    def test_an_expectation_the_run_already_fails_is_listed_once(self):
        store, controller, rt = open_run(self)
        work = rt.enqueue_lab('lab-1')
        found = self.problems(rt, EXPECT_ALL, work_id=work['id'])
        self.assertTrue(all(len(lines) == 1 for lines in found.values()), found)
        self.assertEqual(sorted(found), ['capabilities', 'identity_provider', 'meta_lab', 'orchestration',
                                         'scientific_memory'])


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='rsi-preflight-cli-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ledger = os.path.join(self.root, 'ledger')

    def cli(self, *arguments):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(main(['--root', self.ledger, '--run', 'r', *arguments]), 0)
        return json.loads(printed.getvalue())

    def init(self, config):
        path = os.path.join(self.root, 'config.json')
        with open(path, 'w') as stream:
            json.dump({'budgets': BUDGETS, 'execution_mode': 'replay', **config}, stream)
        self.cli('init', '--config', path)

    def history(self):
        store = Store(self.ledger)
        try:
            return store.verify_history('r')
        finally:
            store.close()

    def test_features_prints_the_feature_report_and_writes_nothing(self):
        self.init(ALL_ON)
        before = self.history()
        report = self.cli('--adapter-factory', 'tests.v2.liveness_support:validating_adapter', 'features')
        self.assertEqual(self.history(), before)
        self.assertEqual([report[name] for name in FEATURES], [ON, ON, ON])
        self.assertEqual([row['adapter_id'] for row in report['adapters']], ['probe'])
        self.assertIn('propose_lab_settings', report['modes']['disposition']['actions'])
        store = Store(self.ledger)
        self.addCleanup(store.close)
        self.assertEqual(report, Runtime(store, 'r', adapters=[validating_adapter()]).feature_report())

    def test_the_command_line_cannot_pass_an_identity_provider_and_says_so(self):
        self.init(MEMORY)
        report = self.cli('features')
        self.assertEqual(report['scientific_memory'], ON)
        self.assertIs(report['identity_provider'], False)
        self.assertEqual(report['adapters'], [])


class UnchangedRunTests(unittest.TestCase):
    def test_a_default_run_sends_the_baseline_packets_with_every_check_in_between(self):
        store, controller, rt = open_run(self)
        controller.add_fact('r', 'f1', 'raw_fact', {'x': 1}, {'source': 'fixture'}, 'k1')
        rt.preflight({name: False for name in FEATURES})
        work = rt.enqueue_lab('lab-1', ['f1'])
        rt.request_report(work['id'])
        rt.feature_report()
        executor = Scripted({'lab': lambda d: ('propose_issue', ISSUE), 'disposition': lambda d: CLOSE})
        rt.run_until_wait(executor, RES, max_steps=12)
        # The current no-orchestration baseline includes decision dependencies.
        self.assertEqual([e['packet_hash'] for e in by_seq(store.list('r', 'effect'))],
                         ['6a92b964b7d817973c1e34f98c471e06137358fe34d1562957f184760528527f',
                          '889273ceb15ca270fe5d7f4ad8abc4deeb3e6515d8a1d2be4fc4f32b8634b365'])
        store.verify_history('r')


if __name__ == '__main__':
    unittest.main()
