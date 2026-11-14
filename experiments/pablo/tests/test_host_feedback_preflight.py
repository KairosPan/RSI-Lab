"""Host checks the actual research schema, dependency binding and native capacity."""
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from experiments.pablo import run as host
from experiments.pablo.tests import test_host_preflight as fixtures
from rsi_lab import roles
from rsi_lab.store import Store, digest


class HostFeedbackPreflightTests(unittest.TestCase):
    setUp = fixtures.HostPreflightTests.setUp
    open_run = fixtures.HostPreflightTests.open_run
    prepared_child = fixtures.HostPreflightTests.prepared_child
    assert_no_execution = fixtures.HostPreflightTests.assert_no_execution

    def ready_runtime(self, **config):
        store = self.open_run(config)
        runtime = host.make_runtime(store, 'run')
        runtime.controller.add_fact('run', 'input', 'raw_fact', {'value': 1}, {'source': 'fixture'}, 'input')
        runtime.observe('inspect', 'input_changed', 0, ['input'])
        runtime.enqueue_lab('start', ['input'])
        return store, runtime

    def test_all_feedback_hooks_and_exact_only_manifests_are_mandatory(self):
        for adapter_id in host.EXPECT['capabilities']:
            for hook in ('recommendation_normalize', 'recommendation_completion', 'decision_dependencies', 'progress'):
                with self.subTest(adapter=adapter_id, hook=hook):
                    store = self.open_run()
                    supplied = [replace(a, **{hook: None}) if a.adapter_id == adapter_id else a
                                for a in host.registered_adapters()]
                    with patch.object(host, 'registered_adapters', return_value=supplied):
                        with self.assertRaisesRegex(ValueError, hook + ': ' + adapter_id):
                            host.make_runtime(store, 'run')
                    self.assert_no_execution(store)
        store = self.open_run()
        supplied = host.registered_adapters()
        first = supplied[0]
        supplied[0] = replace(first, manifest={**first.manifest,
            'recommendation_contract_kinds': ['exact_inputs', 'constraints']})
        with patch.object(host, 'registered_adapters', return_value=supplied):
            with self.assertRaisesRegex(ValueError, 'complete exact_inputs'):
                host.make_runtime(store, 'run')

    def test_actual_packets_bind_dependencies_contract_schema_and_fresh_context(self):
        store, runtime = self.ready_runtime()
        before = store.verify_history('run')
        report = host.startup_preflight(runtime)
        self.assertEqual(store.verify_history('run'), before)
        self.assertEqual(len(report['feedback_adapters']), 3)
        by_role = {row['request']['mode']: row for row in report['requests']}
        self.assertEqual(set(by_role), {'lab', 'discovery'})
        for role, row in by_role.items():
            feedback = row['feedback_contract']
            self.assertTrue(feedback['request_bytes_verified'])
            self.assertGreater(feedback['dependency_selectors'], 0)
            self.assertEqual(feedback['persistent_context_status'], 'ready')
            self.assertEqual(feedback['native_history_bytes'], 0)
            self.assertGreater(feedback['observable_input_bytes'], 0)
            self.assertEqual(feedback['recommendation_available'], role == 'discovery')
            self.assertEqual(feedback['execution_contract_required'], role == 'discovery')
        self.assertEqual(store.list('run', 'agent_session'), [])
        self.assert_no_execution(store)

    def test_exhausted_advice_and_final_review_correctly_omit_creation_schema(self):
        store, runtime = self.ready_runtime(max_recommendations=0)
        report = host.startup_preflight(runtime)
        for row in report['requests']:
            self.assertFalse(row['feedback_contract']['recommendation_available'])
            self.assertFalse(row['feedback_contract']['execution_contract_required'])
        runtime.controller.enqueue('run', 'lab', 'final', 'final', inputs={'final_review_only': True})
        report = host.startup_preflight(runtime)
        final = next(row for row in report['requests'] if row['request']['final_review_only'])
        self.assertEqual(final['request']['actions'], ['no_action'])
        self.assertFalse(final['feedback_contract']['recommendation_available'])
        self.assert_no_execution(store)

    def test_missing_machine_contract_schema_fails_before_execution(self):
        store, runtime = self.ready_runtime()
        payload = copy.deepcopy(roles.PAYLOADS['recommend_change'])
        payload['properties'].pop('execution_contract_json')
        payload['required'].remove('execution_contract_json')
        with patch.dict(roles.PAYLOADS, {'recommend_change': payload}):
            with self.assertRaisesRegex(ValueError, 'required execution contract'):
                host.startup_preflight(runtime)
        self.assert_no_execution(store)

    def test_valid_but_incomplete_dependency_manifest_is_not_accepted(self):
        store, runtime = self.ready_runtime()
        original = runtime._context
        def missing_selector(*args, **kwargs):
            agent, mode, task, options = original(*args, **kwargs)
            manifest = task['decision_dependencies']
            manifest['dependencies'] = manifest['dependencies'][1:]
            manifest['token'] = digest({k: v for k, v in manifest.items() if k != 'token'})
            return agent, mode, task, options
        with patch.object(runtime, '_context', side_effect=missing_selector):
            with self.assertRaisesRegex(ValueError, 'dependency binding'):
                host.startup_preflight(runtime)
        self.assert_no_execution(store)

    def test_context_capacity_is_checked_without_starting_a_native_turn(self):
        store, runtime = self.ready_runtime(max_effective_context_bytes=100)
        before = store.verify_history('run')
        with self.assertRaisesRegex(ValueError, 'persistent context preflight failed'):
            host.startup_preflight(runtime)
        self.assertEqual(store.verify_history('run'), before)
        self.assert_no_execution(store)

    def test_fresh_113_segment_freezes_the_scoring_cap_and_uses_high(self):
        args = self.prepared_child(new_evaluations=113, effort='high', max_input_bytes=524288)
        store = Store(args.root)
        self.addCleanup(store.close)
        config = store.run('child')['config']
        plan = json.loads((args.root / 'task-plan.json').read_text())
        lineage = json.loads((args.root / 'lineage.json').read_text())
        self.assertEqual(config['pablo_max_new_evaluations'], 113)
        self.assertEqual(plan['budget']['new_oracle_evaluations'], 113)
        self.assertEqual(len(plan['items']), 113)
        self.assertEqual(lineage['segment_new_evaluations'], 113)
        self.assertEqual(config['effort'], 'high')
        self.assertEqual({s['role'] for s in store.list('child', 'agent_session')}, {'lab', 'meta'})
        for session in store.list('child', 'agent_session'):
            self.assertEqual(session['committed_turn'], 0)
            self.assertIsNone(session['provider_thread_id'])
        self.assert_no_execution(store, 'child')
