"""Host admission and frozen-source checks; no model, network, or oracle calls."""
import argparse
import contextlib
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.pablo import run as host
from pablo_task import oracle
from pablo_task.research import registered_adapters, research_policy
from rsi_lab.controller import Controller
from rsi_lab.store import Store, digest


BUDGETS = {name: {'limit': limit, 'hard': name.endswith('_calls')}
           for name, limit in {'model_calls': 30, 'tool_calls': 40, 'tokens': 1000000,
                               'wall_seconds': 10000, 'cpu_seconds': 10000}.items()}


class HostPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='pablo-host-preflight-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.serial = 0
        # These synthetic strings never reach the real scoring implementation.
        self.canonical = patch.object(oracle, 'canonicalize', side_effect=lambda value: value)
        self.fingerprint = patch.object(oracle, 'fingerprint', return_value='fixture-fingerprint')
        self.scorer = patch.object(oracle, 'score', side_effect=AssertionError('No scoring in host tests'))
        for patched in (self.canonical, self.fingerprint, self.scorer):
            patched.start()
            self.addCleanup(patched.stop)

    def open_run(self, overrides=None, *, root=None, run_id='run'):
        self.serial += 1
        root = root or self.base / ('run-' + str(self.serial))
        store = Store(root)
        self.addCleanup(store.close)
        initial_ref = store.put_blob({'candidates': ['CCO'], 'scores': [0.1]})
        config = {'execution_mode': 'test', 'task_id': run_id, 'goal': 'Check host integration.',
                  'model': 'gpt-6.1-sol', 'effort': 'xhigh', 'max_input_bytes': 262144,
                  'max_effective_context_bytes': 2097152,
                  'model_reservation': {'model_calls': 1, 'tokens': 45000, 'wall_seconds': 600},
                  'pablo_run_root': str(root), 'pablo_initial_ref': initial_ref,
                  'pablo_oracle_fingerprint': 'fixture-fingerprint', 'pablo_max_new_evaluations': 2,
                  'research_features': dict(host.FEATURES), 'research_policy': research_policy(),
                  'orchestration': {'no_result_steps': 4, 'max_changes': 10},
                  'scientific_memory_context_bytes': 48000,
                  'literature_run_root': str(root), 'literature_max_calls': 3,
                  'model_timeout_seconds': 30}
        config.update(copy.deepcopy(overrides or {}))
        Controller(store).initialize(run_id, budgets=copy.deepcopy(BUDGETS), config=config)
        return store

    def assert_no_execution(self, store, run_id='run'):
        self.assertEqual(store.list(run_id, 'effect'), [])
        self.assertEqual(store.list(run_id, 'agent_turn'), [])

    def test_every_required_config_switch_is_mandatory(self):
        cases = [({'research_features': {k: v for k, v in host.FEATURES.items() if k != name}}, name)
                 for name in ('scientific_memory', 'meta_lab')]
        cases += [({'orchestration': None}, 'orchestration'), ({'research_policy': None}, 'research_policy')]
        for config, message in cases:
            with self.subTest(config=config):
                store = self.open_run(config)
                with self.assertRaisesRegex(ValueError, message):
                    host.make_runtime(store, 'run')
                self.assert_no_execution(store)

    def test_every_adapter_and_the_identity_provider_are_mandatory(self):
        for omitted in host.EXPECT['capabilities']:
            with self.subTest(omitted=omitted):
                store = self.open_run()
                supplied = [a for a in registered_adapters() if a.adapter_id != omitted]
                with patch.object(host, 'registered_adapters', return_value=supplied):
                    with self.assertRaisesRegex(ValueError, omitted):
                        host.make_runtime(store, 'run')
                self.assert_no_execution(store)
        store = self.open_run()
        with patch.object(host, 'project_publication', None):
            with self.assertRaisesRegex(ValueError, 'identity_provider'):
                host.make_runtime(store, 'run')
        self.assert_no_execution(store)

    def test_both_domain_measurement_callbacks_are_mandatory(self):
        for omitted in host.EXPECT['measure']:
            with self.subTest(omitted=omitted):
                store = self.open_run()
                supplied = [replace(a, measure=None) if a.adapter_id == omitted else a
                            for a in registered_adapters()]
                with patch.object(host, 'registered_adapters', return_value=supplied):
                    with self.assertRaisesRegex(ValueError, 'measure.*' + omitted):
                        host.make_runtime(store, 'run')
                self.assert_no_execution(store)

    def test_literature_execution_needs_its_flag_root_and_bounded_budget(self):
        cases = [{'research_features': {'scientific_memory': True, 'meta_lab': True}},
                 {'literature_run_root': '/wrong/run/root'}, {'literature_max_calls': 0},
                 {'literature_max_calls': 101}]
        for config in cases:
            with self.subTest(config=config):
                store = self.open_run(config)
                with self.assertRaisesRegex(ValueError, 'Literature execution configuration'):
                    host.make_runtime(store, 'run')
                self.assert_no_execution(store)

    def test_fresh_and_reopened_requests_carry_all_research_features_without_writes(self):
        store = self.open_run()
        runtime = host.make_runtime(store, 'run')
        runtime.controller.add_fact('run', 'input', 'raw_fact', {'observation': 1},
                                    {'source': 'fixture'}, 'input')
        meta = runtime.observe('start', 'input_changed', 0, ['input'])
        lab = runtime.enqueue_lab('start', ['input'])
        before = store.verify_history('run')
        first = host.startup_preflight(runtime)
        self.assertEqual(store.verify_history('run'), before)
        self.assertEqual(store.list('run', 'agent_session'), [])
        self.assert_no_execution(store)
        self.assertEqual({r['request']['work_id'] for r in first['requests']}, {meta['work_id'], lab['id']})
        for report in first['requests']:
            request = report['request']
            self.assertEqual(request['status'], 'ready')
            self.assertEqual(set(request['capabilities']), set(host.EXPECT['capabilities']))
            self.assertTrue(all(request['present'].values()), request)
            self.assertIn('scientific_memory_updates', request['extra_fields'])
            self.assertTrue(request['packet_sha256'])
        root = store.root
        store.close()
        reopened = Store(root)
        self.addCleanup(reopened.close)
        second = host.startup_preflight(host.make_runtime(reopened, 'run'))
        self.assertEqual(first['requests'], second['requests'])
        self.assertEqual(first['features'], second['features'])
        self.assertEqual(reopened.verify_history('run'), before)

    def test_request_preflight_rejects_a_tool_hidden_by_lab_settings(self):
        store = self.open_run()
        runtime = host.make_runtime(store, 'run')
        work = runtime.enqueue_lab('start')
        change = {'kind': 'orchestration_change', 'status': 'kept',
                  'settings': {'guidance': None, 'step_digest': 0, 'recent_results': 0,
                               'disabled_tools': ['literature.search']}}
        store.mutate('run', 'settings', 'fixture_settings',
                     lambda tx: tx.insert('registration', 'settings', change), change)
        # Run-level capability registration still passes. The actual Lab packet must fail.
        runtime.preflight(host.EXPECT)
        with self.assertRaisesRegex(ValueError, 'disabled_tools') as caught:
            host.startup_preflight(runtime)
        self.assertIn(work['id'], str(caught.exception))
        self.assert_no_execution(store)

    def test_final_review_omits_tools_but_retains_research_requirements(self):
        store = self.open_run()
        runtime = host.make_runtime(store, 'run')
        runtime.controller.enqueue('run', 'lab', 'final', 'final', inputs={'final_review_only': True})
        result = host.startup_preflight(runtime)
        request = result['requests'][0]['request']
        self.assertTrue(request['final_review_only'])
        self.assertEqual(request['capabilities'], [])
        self.assertIn('scientific_memory_updates', request['extra_fields'])
        self.assertIn('recommendation_responses', request['extra_fields'])
        self.assertTrue(result['features']['identity_provider'])
        self.assert_no_execution(store)

    def prepared_child(self, **overrides):
        """Mock only archived export transport; preparation and ledger import are real."""
        parent_store = self.open_run({'research_features': {}}, root=self.base / 'parent', run_id='parent')
        config = parent_store.run('parent')['config']
        parent = {'root': str(parent_store.root), 'run_id': 'parent', 'config': config,
                  'config_hash': digest(config), 'history': parent_store.verify_history('parent'),
                  'budgets': parent_store.list('parent', 'budget'), 'manifest': {},
                  'manifest_sha256': digest({}), 'result_sha256': digest({'status': 'completed'}),
                  'progress': {'candidates': ['CCO'], 'scores': [0.1],
                               'new_evaluations': 0, 'initial_count': 1, 'trajectory_sha256': digest([])},
                  'evidence_objects': {}, 'scoring_sources': [], 'rejection_dispositions': [],
                  'memories': {role: {'memory': None,
                                     'owner': {'id': role + '-owner', 'memory_head_ref': None, 'memory_revision': 0},
                                     'session': {'id': role + '-session'}, 'checkpoint': {'id': role + '-checkpoint'}}
                               for role in ('lab', 'meta')}}
        parent_store.close()
        parent_transform = overrides.pop('parent_transform', None)
        if parent_transform is not None:
            parent_transform(parent)
        args = argparse.Namespace(root=self.base / 'child', run_id='child', parent_root=self.base / 'parent',
                                  parent_run_id='parent', parent_source_root=self.base / 'source',
                                  new_evaluations=2, max_model_calls=24, literature_max_calls=3,
                                  integration_smoke=False)
        vars(args).update(overrides)
        with patch.object(host, 'load_parent', return_value=parent), contextlib.redirect_stdout(io.StringIO()):
            host.prepare(args)
        return args

    def test_effort_and_capacity_override_create_fresh_config_without_changing_parent(self):
        args = self.prepared_child(effort='high', max_input_bytes=524288)
        store = Store(args.root)
        self.addCleanup(store.close)
        runtime = host.make_runtime(store, 'child')
        self.assertEqual(runtime.config['effort'], 'high')
        self.assertEqual(runtime.config['max_input_bytes'], 524288)
        parent = host.continuation.ReadOnlySnapshot(args.parent_root)
        try:
            self.assertEqual(parent.run('parent')['config']['effort'], 'xhigh')
        finally:
            parent.close()
        for session in store.list('child', 'agent_session'):
            self.assertIn('high', json.dumps(session))
        self.assert_no_execution(store, 'child')

    def test_zero_recommendation_limits_are_preserved_in_frozen_host_config(self):
        args = self.prepared_child(max_recommendations=0, max_pending_recommendations=0)
        store = Store(args.root)
        self.addCleanup(store.close)
        config = store.run('child')['config']
        self.assertEqual(config['max_recommendations'], 0)
        self.assertEqual(config['max_pending_recommendations'], 0)
        reports = json.loads((args.root / 'preflight.json').read_text())['requests']
        for report in reports:
            self.assertNotIn('recommend_change', report['request']['actions'])
        self.assert_no_execution(store, 'child')

    def test_prepare_persists_checked_lab_and_meta_requests_and_resume_checks_them_again(self):
        args = self.prepared_child()
        initial = json.loads((args.root / 'preflight.json').read_text())
        self.assertEqual({r['request']['work_kind'] for r in initial['requests']}, {'lab', 'meta_discovery'})
        with patch.object(host, 'startup_preflight', wraps=host.startup_preflight) as checked:
            with patch.object(host, 'CodexExecutor', side_effect=RuntimeError('stop-after-preflight')) as executor:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(host.drive(args), 2)
        checked.assert_called_once()
        executor.assert_called_once()
        resumed = json.loads((args.root / 'preflight.json').read_text())
        self.assertEqual(initial['requests'], resumed['requests'])
        attention = json.loads((args.root / 'attention.json').read_text())
        self.assertIn('stop-after-preflight', attention['reason'])
        store = Store(args.root)
        self.addCleanup(store.close)
        self.assert_no_execution(store, 'child')

    def test_resume_fails_before_executor_when_a_request_loses_a_required_tool(self):
        args = self.prepared_child()
        store = Store(args.root)
        change = {'kind': 'orchestration_change', 'status': 'kept',
                  'settings': {'guidance': None, 'step_digest': 0, 'recent_results': 0,
                               'disabled_tools': ['literature.search']}}
        store.mutate('child', 'disable-tool', 'fixture_settings',
                     lambda tx: tx.insert('registration', 'disable-tool', change), change)
        store.close()
        with patch.object(host, 'CodexExecutor') as executor, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(host.drive(args), 2)
        executor.assert_not_called()
        self.assertIn('disabled_tools', json.loads((args.root / 'attention.json').read_text())['reason'])

    def test_resume_fails_before_recovery_or_executor_on_frozen_source_mismatch(self):
        args = self.prepared_child()
        path = args.root / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['source_hashes']['experiments/pablo/run.py'] = '0' * 64
        path.write_text(json.dumps(manifest))
        with patch.object(host.Runtime, 'recover') as recovery, patch.object(host, 'CodexExecutor') as executor:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(host.drive(args), 2)
        recovery.assert_not_called()
        executor.assert_not_called()
        self.assertIn('Source differs from prepared research run',
                      json.loads((args.root / 'attention.json').read_text())['reason'])

    def test_resume_configuration_preflight_failure_releases_its_lock_and_reports_attention(self):
        args = self.prepared_child()
        with patch.object(host, 'project_publication', None), patch.object(host, 'CodexExecutor') as executor:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(host.drive(args), 2)
        executor.assert_not_called()
        self.assertIn('identity_provider', json.loads((args.root / 'attention.json').read_text())['reason'])
        with (args.root / 'host.lock').open('a+') as lock:
            host.fcntl.flock(lock.fileno(), host.fcntl.LOCK_EX | host.fcntl.LOCK_NB)

    def test_source_manifest_covers_controller_core_and_domain_but_not_tests(self):
        manifest = host.source_manifest()
        for path in ('experiments/pablo/run.py', 'experiments/pablo/host_support.py',
                     'experiments/pablo/continuation.py', 'experiments/pablo/pablo_task/oracle.py',
                     'experiments/pablo/pablo_task/research.py', 'rsi_lab/preflight.py'):
            self.assertEqual(manifest[path], digest((host.REPO / path).read_bytes()))
        self.assertFalse(any('tests' in Path(path).parts for path in manifest))

    def test_parent_archive_mismatch_and_unfrozen_export_helper_never_launch_export(self):
        parent, source = self.base / 'historical', self.base / 'archive'
        parent.mkdir()
        source.mkdir()
        (source / 'module.py').write_text('original = True\n')
        for hashes, message in [({'module.py': '0' * 64}, 'differs from frozen parent'),
                                ({}, 'export helper is not in its frozen source manifest')]:
            with self.subTest(hashes=hashes):
                (parent / 'manifest.json').write_text(json.dumps({'source_hashes': hashes}))
                with patch.object(host.subprocess, 'run') as process:
                    with self.assertRaisesRegex(ValueError, message):
                        host.load_parent(parent, 'parent', source)
                process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
