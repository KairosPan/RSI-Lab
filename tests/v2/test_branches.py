"""Actual isolated branch processes using explicit synthetic metric adapters."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docking_rsi.branches import BranchExecutor, branch_reservations, make_branch_packet
from docking_rsi.capabilities import digest
from docking_rsi.controller import Controller
from docking_rsi.store import Store
from docking_rsi.trials import TrialEngine


ADAPTER = '''import hashlib,json,sys
from pathlib import Path
p=json.load(sys.stdin)
prior=Path("branch-marker.txt").exists()
Path("branch-marker.txt").write_text(p["identity"]["arm"])
source=hashlib.sha256(json.dumps(p["input"],sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
r={"schema_version":"docking_trial_output_v2","identity":p["identity"],
   "metrics":{"utility":10 if p["identity"]["arm"]=="baseline" else 12,"state_leaks":int(prior)},
   "execution_status":"complete","input_receipt_sha256":source}
json.dump(r,sys.stdout)
'''


class BranchTests(unittest.TestCase):
    def setup_run(self, source=ADAPTER, wall_seconds=2):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = Store(root / 'ledger'); self.addCleanup(store.close)
        controller = Controller(store)
        controller.initialize('run', budgets={name: {'limit': limit, 'hard': name in ('model_calls', 'tool_calls')}
            for name, limit in {'model_calls': 0, 'tool_calls': 10, 'tokens': 0, 'cpu_seconds': 20, 'wall_seconds': 20}.items()})
        input_value = {'fixture': True, 'input_numbers': [1, 2, 3]}
        artifact = digest(source.encode())
        versions = {'policy': 'policy1', 'capability': 'fixture-adapter1', 'scientific_protocol': 'synthetic1'}
        spec = {'schema_version': 'docking_trial_v2', 'intervention_kind': 'policy',
            'baseline_versions': versions, 'candidate_versions': {**versions, 'policy': 'policy2'},
            'study_id': 'synthetic-study1', 'new_study_id': None,
            'applicability': {'scope': 'synthetic process isolation fixture', 'branch_execution': {
                'profile': 'reviewed_local_python_metrics_v1', 'artifact_sha256': {'baseline': artifact, 'candidate': artifact},
                'limits': {'wall_seconds': wall_seconds, 'cpu_seconds': 1, 'output_bytes': 8192}}},
            'execution_mode': 'isolated_branch', 'pairs': [{'pair_id': 'pair1', 'input_hash': digest(input_value),
                'baseline_run_id': 'baseline-run1', 'candidate_run_id': 'candidate-run1'}], 'minimum_pairs': 1,
            'metrics': [{'name': 'utility', 'direction': 'higher', 'role': 'primary', 'min_gain': 1, 'max_loss': 0, 'per_pair_no_regression': True},
                        {'name': 'state_leaks', 'direction': 'lower', 'role': 'guard', 'min_gain': 0, 'max_loss': 0, 'per_pair_no_regression': True}],
            'release_rule': 'all_primary', 'budget': {'model_calls': 0, 'tool_calls': 3, 'tokens': 0, 'cpu_seconds': 5, 'wall_seconds': 10},
            'settlement_reserve': {'tool_calls': 1, 'wall_seconds': 1}, 'recovery_owner': 'fixture-controller'}
        engine = TrialEngine(store, 'run')
        engine.register('trial1', spec, key='register'); engine.start('trial1', key='start')
        path = root / 'adapter.py'; path.write_text(source)
        return store, controller, engine, spec, input_value, path

    def run_arm(self, setup, arm):
        store, controller, engine, spec, input_value, path = setup
        packet = make_branch_packet(store, 'run', 'trial1', 'pair1', arm, input_value, path,
                                    execution_authorization_sha256=digest(path.read_bytes()))
        work = controller.enqueue('run', 'lab', arm, 'work-' + arm)
        effect = controller.prepare_effect('run', work['id'], 'isolated_trial_branch', packet,
            branch_reservations(store, 'run', 'trial1'), 'reserve-' + arm,
            metadata={key: packet[key] for key in ('trial_id', 'pair_id', 'arm', 'run_id')})
        result = controller.dispatch_effect('run', effect['id'], BranchExecutor(store, 'run'))
        self.assertEqual(result['status'], 'succeeded')
        engine.record_measurement('trial1', result['id'], key='measure-' + arm)
        return json.loads(store.read_blob(result['result_ref']))

    def test_paired_processes_start_with_distinct_clean_workspaces_and_settle(self):
        setup = self.setup_run()
        baseline, candidate = self.run_arm(setup, 'baseline'), self.run_arm(setup, 'candidate')
        self.assertNotEqual(baseline['trial_execution']['workspace_id'], candidate['trial_execution']['workspace_id'])
        for receipt in (baseline, candidate):
            self.assertEqual(receipt['trial_measurement']['metrics']['state_leaks'], 0)
            self.assertTrue(receipt['trial_execution']['workspace_removed'])
            self.assertEqual(receipt['actual_costs']['tool_calls'], 1)
            self.assertGreater(receipt['actual_costs']['wall_seconds'], 0)
        outcome = setup[2].settle('trial1', key='settle')['outcome']
        self.assertEqual((outcome['execution'], outcome['evidence'], outcome['version_action']), ('valid', 'met', 'approve_next'))
        self.assertEqual(outcome['known_costs']['tool_calls'], 2)
        setup[0].verify_history('run')

    def test_input_and_artifact_must_match_pre_result_registration(self):
        store, _, _, _, input_value, path = self.setup_run()
        with patch('docking_rsi.branches.subprocess.Popen') as process:
            with self.assertRaisesRegex(ValueError, 'prospectively pinned'):
                make_branch_packet(store, 'run', 'trial1', 'pair1', 'baseline', input_value, path,
                                   execution_authorization_sha256='0' * 64)
            with self.assertRaisesRegex(ValueError, 'frozen input'):
                make_branch_packet(store, 'run', 'trial1', 'pair1', 'baseline', {'different': True}, path,
                                   execution_authorization_sha256=digest(path.read_bytes()))
            path.write_text(ADAPTER + '\n# post-registration change\n')
            with self.assertRaisesRegex(ValueError, 'prospectively pinned'):
                make_branch_packet(store, 'run', 'trial1', 'pair1', 'baseline', input_value, path,
                                   execution_authorization_sha256=digest(path.read_bytes()))
            process.assert_not_called()

    def test_output_identity_and_versions_cannot_be_relabelled(self):
        source = ADAPTER.replace('json.dump(r,sys.stdout)', 'r["identity"]["versions"]["scientific_protocol"]="wrong"\njson.dump(r,sys.stdout)')
        setup = self.setup_run(source)
        receipt = self.run_arm(setup, 'candidate')
        self.assertEqual(receipt['trial_measurement']['execution_status'], 'invalid')
        self.assertTrue(all(value is None for value in receipt['trial_measurement']['metrics'].values()))
        self.assertIn('identity', receipt['trial_execution']['error'])
        self.assertEqual(setup[2].settle('trial1', key='settle')['outcome']['evidence'], 'insufficient')

    def test_timeout_is_incomplete_and_nonzero_exit_is_invalid(self):
        for source, timeout, expected in [('import time\ntime.sleep(1)\n', .05, 'incomplete'),
                                           ('raise SystemExit(3)\n', 2, 'invalid')]:
            with self.subTest(expected=expected):
                setup = self.setup_run(source, timeout)
                receipt = self.run_arm(setup, 'baseline')
                self.assertEqual(receipt['trial_measurement']['execution_status'], expected)
                self.assertEqual(receipt['actual_costs']['tool_calls'], 1)
                self.assertTrue(all(value is None for value in receipt['trial_measurement']['metrics'].values()))

    def test_missing_or_nonfinite_metrics_remain_invalid(self):
        sources = [ADAPTER.replace('json.dump(r,sys.stdout)', 'r["metrics"].pop("utility")\njson.dump(r,sys.stdout)'),
                   ADAPTER.replace('json.dump(r,sys.stdout)', 'r["metrics"]["utility"]=float("nan")\njson.dump(r,sys.stdout)')]
        for source in sources:
            with self.subTest(source=source[-100:]):
                receipt = self.run_arm(self.setup_run(source), 'baseline')
                self.assertEqual(receipt['trial_measurement']['execution_status'], 'invalid')
                self.assertTrue(all(value is None for value in receipt['trial_measurement']['metrics'].values()))

    def test_unpersisted_effect_or_changed_packet_cannot_launch_a_process(self):
        setup = self.setup_run()
        store, controller, _, _, input_value, path = setup
        packet = make_branch_packet(store, 'run', 'trial1', 'pair1', 'baseline', input_value, path,
                                   execution_authorization_sha256=digest(path.read_bytes()))
        work = controller.enqueue('run', 'lab', 'baseline', 'work')
        effect = controller.prepare_effect('run', work['id'], 'isolated_trial_branch', packet,
            branch_reservations(store, 'run', 'trial1'), 'reserve',
            metadata={key: packet[key] for key in ('trial_id', 'pair_id', 'arm', 'run_id')})
        with patch('docking_rsi.branches.subprocess.Popen') as process:
            with self.assertRaisesRegex(ValueError, 'persisted dispatching'):
                BranchExecutor(store, 'run')(effect, store.read_blob(effect['packet_ref']))
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
