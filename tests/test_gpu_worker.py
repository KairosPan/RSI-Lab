"""Parent process failures cannot be hidden by a worker's earlier result file."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    import run_lab_gpu_batch as worker
finally:
    sys.path.pop(0)


class WorkerTests(unittest.TestCase):
    def test_parent_failure_overrides_an_earlier_completed_result(self):
        for timeout, returncode, expected in [(True, -9, "worker_timeout"), (False, 2, "worker_error"), (False, 0, "completed")]:
            with self.subTest(timeout=timeout, returncode=returncode), tempfile.TemporaryDirectory() as tmp:
                job = Path(tmp) / "job"
                class Process:
                    pid = 123
                    def __init__(self, *args, **kwargs):
                        self.returncode = returncode
                        self.calls = 0
                        worker.pilot.dump(job / "result.json", {"status": "completed", "cpu_seconds": 1.0})
                    def wait(self, timeout=None):
                        self.calls += 1
                        if timeout is not None and timeout_case and self.calls == 1:
                            raise subprocess.TimeoutExpired("fixture", timeout)
                        return self.returncode
                timeout_case = timeout
                with mock.patch.object(worker.subprocess, "Popen", Process), mock.patch.object(worker.os, "killpg"):
                    result = worker.execute_job(job, {"candidate": {"candidate_id": "C00"}, "seed": 17})
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["parent_observed_returncode"], returncode)
                if expected != "completed":
                    self.assertIsNone(result["cpu_seconds"])
                    self.assertEqual(result["worker_reported_status"], "completed")


if __name__ == "__main__":
    unittest.main()
