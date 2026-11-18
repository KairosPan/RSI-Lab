"""Team run preparation, options and admission at the host, entirely offline; no run is started."""
import argparse
import contextlib
import copy
import io
import json
import sys
import unittest
from unittest.mock import patch

from experiments.pablo import run as host
from experiments.pablo.tests import test_host_preflight as fixtures
from pablo_task import adapter, harness, methods, team
from pablo_task.research import research_policy
from rsi_lab import native
from rsi_lab.store import Store, digest


SCHEDULE = ('The PABLO Lab Harness is restored: obey constraints.adapter_context["pablo.evaluate"].harness '
            'for the currently due local/challenge role, eligible observed anchor, plateau regime and batch allowance. '
            'Local refines an observed high-scoring series; challenge tests a competing structural series. '
            'After score stagnation the frozen host policy increases challenge frequency. ')


class HostTeamPreflightTests(unittest.TestCase):
    setUp = fixtures.HostPreflightTests.setUp
    open_run = fixtures.HostPreflightTests.open_run
    prepared_child = fixtures.HostPreflightTests.prepared_child
    assert_no_execution = fixtures.HostPreflightTests.assert_no_execution

    def native_file(self, block, name='native-capabilities.json'):
        path = self.base / name
        path.write_text(json.dumps(block))
        return path

    def team_store(self, overrides=None):
        return self.open_run({'research_architecture': team.ARCHITECTURE,
            'pablo_harness': copy.deepcopy(harness.LED_DEFAULT_CONFIG),
            'batch_interpretation_capabilities': [adapter.ADAPTER_ID],
            'native_capabilities': team.native_block({'lab': {'subagents': True}}, protected=[]),
            'research_policy': research_policy(team=True), 'effort': 'high', 'max_open_reviews': 0,
            **(overrides or {})})

    def opened(self, args):
        store = Store(args.root)
        self.addCleanup(store.close)
        return store, store.run('child')['config']

    def test_team_preparation_freezes_architecture_harness_block_limits_and_not_blind(self):
        readable = self.base / 'readable'
        readable.mkdir()
        given = {'lab': {'subagents': True, 'web_search': True, 'terminal': {'readable_roots': [str(readable)]}}}
        args = self.prepared_child(team=True, native_capabilities=self.native_file(given),
                                   challenge_backstop_batches=4, rebuild_history_bytes=None, effort='high')
        store, config = self.opened(args)
        self.assertEqual(json.loads((args.root / 'run-config.json').read_text()), config)
        self.assertEqual(config['research_architecture'], team.ARCHITECTURE)
        self.assertEqual(config['pablo_harness'], harness.LED_DEFAULT_CONFIG)
        self.assertEqual(config['batch_interpretation_capabilities'], [adapter.ADAPTER_ID])
        self.assertEqual(config['native_capabilities'], team.native_block(given, protected=[]))
        self.assertEqual(config['native_capabilities']['lab']['subagents']['roles'], team.ROLES)
        self.assertEqual(config['max_effective_context_bytes'], 4194304)
        self.assertEqual(config['prospective_rebuild_history_bytes'], 4194304)
        self.assertEqual(config['pablo_not_blind'], team.NOT_BLIND)
        self.assertEqual(config['research_policy'], research_policy(team=True))
        goal = config['goal']
        self.assertIn(' ' + team.GOAL + ' For every scoring batch first execute pablo.select_candidates', goal)
        self.assertIn('literature_evidence_ids, decision. Implemented methods', goal)
        self.assertNotIn('The PABLO Lab Harness is restored', goal)
        self.assertNotIn('increases challenge frequency', goal)
        self.assertIn('Do not search for or infer hidden benchmark identity.', goal)
        self.assertEqual((args.root / 'task-contract.txt').read_text(), goal + '\n')
        plan = json.loads((args.root / 'task-plan.json').read_text())
        manifest = json.loads((args.root / 'manifest.json').read_text())
        lineage = json.loads((args.root / 'lineage.json').read_text())
        self.assertEqual(plan['protocol']['parameters']['pablo_harness'], config['pablo_harness'])
        self.assertEqual(plan['protocol']['parameters']['research_architecture'], team.ARCHITECTURE)
        self.assertEqual(manifest['research_architecture'], team.ARCHITECTURE)
        self.assertEqual(lineage['research_architecture'], team.ARCHITECTURE)
        path = 'experiments/pablo/pablo_task/team.py'
        self.assertEqual(manifest['source_hashes'][path], digest((host.REPO / path).read_bytes()))
        preflight = json.loads((args.root / 'preflight.json').read_text())
        self.assertEqual({r['request']['work_kind'] for r in preflight['requests']}, {'lab', 'meta_discovery'})
        self.assertIn(methods.ADAPTER_ID, preflight['expectations']['capabilities'])
        runtime = host.make_runtime(store, 'child')
        self.assertIs(runtime.adapters.by_id[methods.ADAPTER_ID].admit_request, methods.admit_request)
        lab = next(w for w in runtime.controller.ready('child') if w['kind'] == 'lab')
        _, _, task, _ = runtime._context(lab, read_only=True)
        self.assertEqual(task['constraints']['native_capabilities'], native.capabilities(config)['lab'])
        self.assertEqual(host.legacy_host.snapshot(runtime, 'prepared')['not_blind'], team.NOT_BLIND)
        self.assertEqual(json.loads((args.root / 'progress.json').read_text())['not_blind'], team.NOT_BLIND)
        self.assert_no_execution(store, 'child')

    def test_team_preparation_takes_the_backstop_and_rebuild_size_and_says_when_it_is_blind(self):
        given = {'lab': {'subagents': {'max_concurrent': 2}}}
        args = self.prepared_child(team=True, native_capabilities=self.native_file(given),
                                   challenge_backstop_batches=None, rebuild_history_bytes=262144)
        store, config = self.opened(args)
        self.assertEqual(config['pablo_harness'], {**harness.LED_DEFAULT_CONFIG, 'challenge_backstop_batches': None})
        self.assertEqual(config['prospective_rebuild_history_bytes'], 262144)
        self.assertEqual(config['native_capabilities']['lab']['subagents']['max_concurrent'], 2)
        self.assertIn('pablo_not_blind', config)
        self.assertIsNone(config['pablo_not_blind'])
        runtime = host.make_runtime(store, 'child')
        report = host.legacy_host.snapshot(runtime, 'prepared')
        self.assertIn('not_blind', report)
        self.assertIsNone(report['not_blind'])
        self.assert_no_execution(store, 'child')

    def test_a_readable_root_that_could_hand_over_the_objective_is_refused_before_anything_is_written(self):
        for folder in ('source', 'parent', 'runs'):
            (self.base / folder).mkdir()
        roots = {'checkout': (host.REPO / 'experiments', 'lies inside'),
                 'frozen source copy': (self.base / 'source', 'is the protected folder'),
                 'parent run root': (self.base / 'parent', 'is the protected folder'),
                 'folder of the new run root': (self.base / 'runs', 'contains the protected folder')}
        for name, (root, reason) in roots.items():
            with self.subTest(root=name):
                given = {'lab': {'subagents': True, 'terminal': {'readable_roots': [str(root)]}}}
                args = argparse.Namespace(root=self.base / 'runs' / 'child', run_id='child',
                    parent_root=self.base / 'parent', parent_run_id='parent', parent_source_root=self.base / 'source',
                    new_evaluations=2, max_model_calls=24, literature_max_calls=3, integration_smoke=False,
                    team=True, native_capabilities=self.native_file(given))
                with patch.object(host, 'load_parent') as load:
                    with self.assertRaisesRegex(ValueError, 'readable_roots: .*' + reason):
                        host.prepare(args)
                load.assert_not_called()
                self.assertFalse((self.base / 'runs' / 'child').exists())

    @staticmethod
    def team_parent(parent):
        """The parent was itself a team run, with web search on and its own limits."""
        block = team.native_block({'lab': {'subagents': True, 'web_search': True}}, protected=[])
        parent['config'].update(research_architecture=team.ARCHITECTURE, native_capabilities=block,
            pablo_harness=copy.deepcopy(harness.LED_DEFAULT_CONFIG), pablo_not_blind=team.NOT_BLIND,
            max_effective_context_bytes=4194304, prospective_rebuild_history_bytes=1048576,
            research_policy=research_policy(team=True))

    def test_a_plain_preparation_from_a_team_parent_is_refused_before_anything_is_written(self):
        with self.assertRaisesRegex(ValueError, 'The parent is a team run; continue it with --team'):
            self.prepared_child(parent_transform=self.team_parent)
        self.assertFalse((self.base / 'child').exists())

    def test_a_team_preparation_from_a_team_parent_takes_its_block_from_the_file_given_now(self):
        given = {'lab': {'subagents': {'max_concurrent': 2}}}
        args = self.prepared_child(parent_transform=self.team_parent, team=True,
                                   native_capabilities=self.native_file(given))
        store, config = self.opened(args)
        self.assertEqual(config['research_architecture'], team.ARCHITECTURE)
        self.assertEqual(config['pablo_harness'], harness.LED_DEFAULT_CONFIG)
        self.assertEqual(config['native_capabilities'], team.native_block(given, protected=[]))
        self.assertIsNone(config['pablo_not_blind'])
        self.assertEqual(config['prospective_rebuild_history_bytes'], 4194304)
        self.assertEqual(config['max_effective_context_bytes'], 4194304)
        self.assertEqual(config['research_policy'], research_policy(team=True))
        self.assertEqual(config['goal'].count(team.GOAL), 1)
        self.assertIs(host.make_runtime(store, 'child').adapters.by_id[methods.ADAPTER_ID].admit_request,
                      methods.admit_request)
        self.assert_no_execution(store, 'child')

    def test_a_preparation_without_team_is_unchanged(self):
        args = self.prepared_child(effort='high')
        store, config = self.opened(args)
        self.assertEqual(config['research_architecture'], host.ARCHITECTURE)
        self.assertEqual(config['pablo_harness'], harness.DEFAULT_CONFIG)
        self.assertEqual(config['research_policy'], research_policy())
        self.assertNotIn('native_capabilities', config)
        self.assertNotIn('pablo_not_blind', config)
        self.assertNotIn('prospective_rebuild_history_bytes', config)
        # The parent's own limit is kept, as before.
        self.assertEqual(config['max_effective_context_bytes'], 2097152)
        self.assertIn(' ' + SCHEDULE + 'For every scoring batch first execute pablo.select_candidates', config['goal'])
        self.assertIn('literature_evidence_ids. Implemented methods', config['goal'])
        self.assertNotIn(team.GOAL, config['goal'])
        runtime = host.make_runtime(store, 'child')
        self.assertIsNone(runtime.adapters.by_id[methods.ADAPTER_ID].admit_request)
        self.assertNotIn('native_capabilities', next(
            runtime._context(w, read_only=True)[2]['constraints']
            for w in runtime.controller.ready('child') if w['kind'] == 'lab'))
        self.assertNotIn('not_blind', host.legacy_host.snapshot(runtime, 'prepared'))
        self.assert_no_execution(store, 'child')

    def main(self, *options, resume=False):
        argv = ['run.py', '--root', str(self.base / 'new'), '--run-id', 'new', '--prepare-only']
        argv += ['--resume'] if resume else ['--parent-root', str(self.base / 'parent')]
        with patch.object(sys, 'argv', argv + list(options)), patch.object(host, 'prepare') as prepare, \
                patch.object(host, 'drive', return_value=0) as drive, contextlib.redirect_stderr(io.StringIO()):
            try:
                code = host.main()
            except SystemExit as exc:
                code = exc.code
        return code, prepare, drive

    def test_each_option_misuse_is_a_parser_error(self):
        path = str(self.native_file({'lab': {'subagents': True}}))
        team_run = ('--team', '--native-capabilities', path)
        misuses = [('--team',), ('--native-capabilities', path), ('--challenge-backstop-batches', '4'),
                   ('--challenge-backstop-batches', 'off'), ('--rebuild-history-bytes', '262144'),
                   team_run + ('--challenge-backstop-batches', '0'), team_run + ('--challenge-backstop-batches', '101'),
                   team_run + ('--challenge-backstop-batches', 'never'),
                   team_run + ('--rebuild-history-bytes', '262143')]
        for options in misuses:
            with self.subTest(options=options):
                code, prepare, drive = self.main(*options)
                self.assertEqual(code, 2)
                prepare.assert_not_called()
                drive.assert_not_called()
        for options in (('--team',), ('--native-capabilities', path), ('--challenge-backstop-batches', 'off'),
                        ('--rebuild-history-bytes', '4194304'), team_run):
            with self.subTest(resume=options):
                code, prepare, drive = self.main(*options, resume=True)
                self.assertEqual(code, 2)
                prepare.assert_not_called()
                drive.assert_not_called()

    def test_team_options_reach_preparation(self):
        path = str(self.native_file({'lab': {'subagents': True}}))
        for options, backstop, rebuild in ((('--challenge-backstop-batches', 'off'), None, None),
                                           ((), 4, None),
                                           (('--challenge-backstop-batches', '7', '--rebuild-history-bytes',
                                             '262144'), 7, 262144)):
            with self.subTest(options=options):
                code, prepare, _ = self.main('--team', '--native-capabilities', path, *options)
                self.assertEqual(code, 0)
                [(args,), _] = prepare.call_args
                self.assertTrue(args.team)
                self.assertEqual(str(args.native_capabilities), path)
                self.assertEqual(args.challenge_backstop_batches, backstop)
                self.assertEqual(args.rebuild_history_bytes, rebuild)
        code, prepare, _ = self.main()
        self.assertEqual(code, 0)
        [(args,), _] = prepare.call_args
        self.assertFalse(args.team)
        self.assertEqual((args.native_capabilities, args.challenge_backstop_batches, args.rebuild_history_bytes),
                         (None, None, None))

    def test_make_runtime_refuses_an_incomplete_team_config(self):
        runtime = host.make_runtime(self.team_store(), 'run')
        self.assertIs(runtime.adapters.by_id[methods.ADAPTER_ID].admit_request, methods.admit_request)
        only_local = {'lab': {'subagents': {'max_concurrent': 3, 'roles': {'local': team.ROLES['local']}}}}
        for changes in ({'pablo_harness': copy.deepcopy(harness.DEFAULT_CONFIG)}, {'pablo_harness': None},
                        {'batch_interpretation_capabilities': []}, {'batch_interpretation_capabilities': None},
                        {'native_capabilities': {'lab': {'subagents': True}}}, {'native_capabilities': only_local},
                        {'native_capabilities': {}}):
            with self.subTest(changes=changes):
                store = self.team_store(changes)
                with self.assertRaisesRegex(ValueError, 'team run requires'):
                    host.make_runtime(store, 'run')
                self.assert_no_execution(store)

    def session_with_rollout(self, store, runtime, role):
        """A session that names a native rollout, as one whose turn was committed does."""
        rollout = self.base / ('rollout-' + str(self.serial) + '-' + role + '.jsonl')
        rollout.write_text('{}\n')
        binding = {'model': runtime.config['model'], 'effort': runtime.config['effort'],
                   'transport': 'codex_exec_text_v1'}
        session = runtime.agents.ensure_session(role, runtime_binding=binding)
        manifest = {'native_session_ref': str(rollout), 'native_prefix': {'prefix_sha256': '0' * 64}}
        store.mutate('run', 'fixture-rollout:' + role, 'fixture_rollout',
                     lambda tx: tx.update('agent_session', session['id'],
                                          {'provider_thread_id': 'thread-' + role, 'context_manifest': manifest}),
                     {'session_id': session['id']})

    @staticmethod
    def reader(seen):
        def read(path, **options):
            seen.append(options.get('native', 'native= was not passed'))
            raise RuntimeError('rollout reader reached')
        return read

    def test_rebuild_reads_a_rollout_with_its_sessions_role_settings(self):
        for overrides, role, expected in (({}, 'lab', 'lab'), ({}, 'meta', None), (None, 'lab', None)):
            with self.subTest(team=overrides is not None, role=role):
                limit = {'prospective_rebuild_history_bytes': 1}
                store = self.team_store(limit) if overrides is not None else self.open_run(limit)
                runtime = host.make_runtime(store, 'run')
                self.session_with_rollout(store, runtime, role)
                seen = []
                with patch.object(host.legacy_host, 'read_native_snapshot', side_effect=self.reader(seen)):
                    with self.assertRaisesRegex(RuntimeError, 'rollout reader reached'):
                        host.legacy_host.rebuild_at_boundary(runtime)
                self.assertEqual(seen, [runtime.native['lab'] if expected else None])
                self.assert_no_execution(store)

    def test_startup_preflight_reads_a_rollout_with_the_lab_settings(self):
        for store in (self.team_store(), self.open_run()):
            with self.subTest(team='native_capabilities' in store.run('run')['config']):
                runtime = host.make_runtime(store, 'run')
                runtime.controller.add_fact('run', 'input', 'raw_fact', {'observation': 1}, {'source': 'fixture'},
                                            'input')
                runtime.enqueue_lab('start', ['input'])
                self.session_with_rollout(store, runtime, 'lab')
                seen = []
                with patch('rsi_lab.session_context.read_native_snapshot', side_effect=self.reader(seen)):
                    with self.assertRaisesRegex(RuntimeError, 'rollout reader reached'):
                        host.startup_preflight(runtime)
                team_run = 'native_capabilities' in runtime.config
                self.assertEqual(seen, [native.capabilities(runtime.config)['lab'] if team_run else None])
                self.assert_no_execution(store)


if __name__ == '__main__':
    unittest.main()
