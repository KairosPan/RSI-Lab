"""Deadline behavior without launching a scientific process."""
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_vina_screen as screen


class ScreenDeadlineTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'data').mkdir()
        (root / 'frozen').mkdir()
        protocol = {'seeds': [17, 43], 'worker_timeout_seconds': 300,
                    'versions': {}, 'input_sha256': {}}
        screen.dump(root / 'protocol.json', protocol)
        screen.dump(root / 'data/candidates.json', {'candidates': [{'candidate_id': 'A', 'smiles': 'C'}]})
        screen.dump(root / 'plan.json', {
            'driver_sha256': screen.sha(screen.__file__),
            'protocol_sha256': screen.sha(root / 'protocol.json'),
            'candidate_count': 1, 'seeds': [17, 43], 'max_external_processes': 2,
            'max_seed_jobs': 2, 'compute_deadline_utc': '1970-01-01T00:16:41+00:00'})
        adapter = SimpleNamespace(
            contract=SimpleNamespace(validate_vina_protocol=lambda *a, **k: None),
            check_box=lambda *a: None, input_hashes=lambda *a: {},
            versions=lambda: {}, source_hashes=lambda: {}, ID=re.compile(r'[A-Z]+'))
        worker = SimpleNamespace(engine_receipt=lambda *a: {})
        return {'run_vina_batch': adapter, 'vina_worker': worker}

    def test_expired_absolute_deadline_never_starts_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            modules = self.fixture(root)
            with patch.dict('sys.modules', modules), \
                    patch.object(screen.time, 'time', return_value=1002), \
                    patch.object(screen.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ValueError, 'deadline already expired'):
                    screen.run(root, root / 'vina')
            spawn.assert_not_called()
            self.assertFalse((root / 'jobs').exists())

    def test_existing_job_refuses_implicit_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            modules = self.fixture(root)
            existing = root / 'jobs/A_s17'
            existing.mkdir(parents=True)
            with patch.dict('sys.modules', modules), \
                    patch.object(screen.time, 'time', return_value=1000), \
                    patch.object(screen.subprocess, 'Popen') as spawn:
                with self.assertRaisesRegex(ValueError, 'no implicit resume or retry'):
                    screen.run(root, root / 'vina')
            spawn.assert_not_called()
            self.assertTrue(existing.is_dir())

    def test_launch_failure_is_accounted_and_remaining_seed_not_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            modules = self.fixture(root)
            with patch.dict('sys.modules', modules), \
                    patch.object(screen.time, 'time', return_value=1000), \
                    patch.object(screen.subprocess, 'Popen', side_effect=OSError('cannot launch')) as spawn:
                with self.assertRaisesRegex(RuntimeError, 'cannot launch'):
                    screen.run(root, root / 'vina')
            self.assertEqual(spawn.call_count, 1)
            report = json.loads((root / 'execution_summary.json').read_text())
            self.assertEqual(report['status'], 'interrupted')
            self.assertEqual(report['settled_seed_jobs'], 1)
            self.assertEqual(report['unrun_seed_jobs'], [{'candidate_id': 'A', 'seed': 43}])
            result = json.loads((root / 'jobs/A_s17/result.json').read_text())
            self.assertEqual(result['status'], 'launch_failed')
            self.assertIsNone(result['scores'])

    def test_global_deadline_kills_each_own_group_and_retains_missing_cost(self):
        class Process:
            def __init__(self, pid):
                self.pid, self.returncode = pid, None
            def poll(self):
                return self.returncode
            def wait(self):
                self.returncode = -9
                return self.returncode

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            modules = self.fixture(root)
            clock = iter(i / 20 for i in range(1000))
            with patch.dict('sys.modules', modules), \
                    patch.object(screen.time, 'time', return_value=1000), \
                    patch.object(screen.time, 'monotonic', side_effect=lambda: next(clock)), \
                    patch.object(screen.time, 'sleep'), \
                    patch.object(screen.subprocess, 'Popen', side_effect=[Process(12345), Process(12346)]) as spawn, \
                    patch.object(screen.os, 'killpg') as kill:
                report = screen.run(root, root / 'vina')
            self.assertEqual(spawn.call_count, 2)
            self.assertTrue(all(call.kwargs['start_new_session'] for call in spawn.call_args_list))
            self.assertEqual({call.args[0] for call in kill.call_args_list}, {12345, 12346})
            self.assertEqual(report['status'], 'deadline_reached')
            self.assertEqual(report['settled_seed_jobs'], 2)
            self.assertEqual(report['unrun_seed_jobs'], [])
            self.assertFalse(report['cpu_accounting_complete'])
            for path in (root / 'jobs').glob('*/result.json'):
                result = json.loads(path.read_text())
                self.assertEqual(result['status'], 'global_deadline_timeout')
                self.assertIsNone(result['scores'])
                self.assertIsNone(result['cpu_seconds'])


if __name__ == '__main__':
    unittest.main()
