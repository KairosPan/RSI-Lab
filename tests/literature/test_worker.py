import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv

from rsi_literature.documents import DocumentStore
from rsi_literature.worker import MAX_STREAM_BYTES, run_sdk_worker


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DocumentStore(self.root / "store")
        self.document_id = self.store.ingest(b'Registered source text.', 'text/plain',
            {'paper_id': 'paper-1', 'content_scope': 'abstract'})['document_id']
        self.settings = {"model": "host-model", "embedding": "host-embedding"}
        self.inputs = {"document_ids": [self.document_id], "query": "paper"}

    def run_worker(self, settings=None, inputs=None, effect="effect-1"):
        return run_sdk_worker("paperqa", self.settings if settings is None else settings,
                              self.inputs if inputs is None else inputs, self.store, effect)

    def executable(self, source):
        # A host-selected executable fixture, never a model-supplied command.
        path = self.root / "python-fixture"
        path.write_text("#!" + sys.executable + "\n" + source)
        path.chmod(0o700)
        return str(path)

    def test_real_worker_imports_checkout_from_job_directory_and_reports_missing_configuration(self):
        result = self.run_worker(settings={})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["errors"][0]["code"], "sdk_unavailable")
        self.assertEqual(result["provenance"]["attempt_count"], 1)
        self.assertFalse(result.get("execution_unknown", False))
        self.assertFalse(result["provenance"]["os_sandbox"])

    def test_host_virtual_environment_is_preserved_by_worker_python(self):
        environment = self.root / "host-venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        (self.root / "paperqa.py").write_text('''import sys
class Settings:
    def __init__(self, **kwargs): pass
class Docs:
    async def aadd(self, path, **kwargs): pass
    async def aquery(self, query, **kwargs): return {"python_prefix": sys.prefix}
''')
        settings = {**self.settings, "python": str(environment / "bin/python"), "env_names": ["PYTHONPATH"]}
        with patch.dict(os.environ, {"PYTHONPATH": str(self.root)}):
            result = self.run_worker(settings)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(Path(result["data"]["raw_result"]["python_prefix"]).resolve(), environment.resolve())

    def test_sdk_prints_are_isolated_and_only_explicit_environment_is_inherited(self):
        (self.root / "paperqa.py").write_text('''import os
print("SDK import log")
os.write(1, b"C extension stdout log\\n")
class Settings:
    def __init__(self, **kwargs): pass
class Docs:
    async def aadd(self, path, **kwargs): pass
    async def aquery(self, query, **kwargs):
        print("SDK execution log")
        return {"visible": os.environ.get("RSI_VISIBLE_TEST"), "hidden": os.environ.get("RSI_HIDDEN_TEST"),
                "query": query}
''')
        settings = {**self.settings, "env_names": ["PYTHONPATH", "RSI_VISIBLE_TEST"]}
        with patch.dict(os.environ, {"PYTHONPATH": str(self.root), "RSI_VISIBLE_TEST": "explicit-value",
                                     "RSI_HIDDEN_TEST": "secret-that-must-not-leak"}):
            result = self.run_worker(settings)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["raw_result"], {"visible": "explicit-value", "hidden": None, "query": "paper"})
        job = self.store.root / "jobs/effect-1/sdk-worker"
        self.assertIn("SDK execution log", (job / "stderr.log").read_text())
        self.assertIn("C extension stdout log", (job / "stderr.log").read_text())
        self.assertNotIn("SDK import log", (job / "stdout.log").read_text())
        self.assertNotIn("explicit-value", (job / "request.json").read_text())
        self.assertNotIn("secret-that-must-not-leak", (job / "request.json").read_text())

    def test_timeout_and_failure_after_spawn_mark_execution_unknown(self):
        python = self.executable("import time; time.sleep(30)\n")
        result = self.run_worker({"python": python, "timeout": 1})
        self.assertEqual(result["errors"][0]["code"], "worker_timeout")
        self.assertTrue(result["execution_unknown"])
        python = self.executable("raise SystemExit(4)\n")
        result = self.run_worker({"python": python}, effect="effect-2")
        self.assertEqual(result["errors"][0]["code"], "worker_exit")
        self.assertTrue(result["execution_unknown"])

    def test_stdout_and_stderr_are_independently_bounded_on_disk(self):
        for fd in (1, 2):
            with self.subTest(fd=fd):
                python = self.executable(f"import os; os.write({fd}, b'x' * ({MAX_STREAM_BYTES} + 65536))\n")
                result = self.run_worker({"python": python}, effect=f"effect-{fd}")
                self.assertEqual(result["errors"][0]["code"], "worker_output_limit")
                self.assertTrue(result["execution_unknown"])
                job = self.store.root / f"jobs/effect-{fd}/sdk-worker"
                for name in ("stdout.log", "stderr.log"):
                    self.assertLessEqual((job / name).stat().st_size, MAX_STREAM_BYTES)

    def test_unbound_or_invalid_child_result_cannot_publish(self):
        snippets = [
            'print("log before JSON"); print("{}")',
            'print("{\\"status\\":\\"ok\\",\\"data\\":{}}")',
            '''import json, sys
p=json.load(sys.stdin)
e={k:p[k] for k in ("protocol", "request_id", "provider", "effect_id")}
e["effect_id"]="another-effect";e["result"]={};print(json.dumps(e))''',
            '''import json, sys
p=json.load(sys.stdin)
e={k:p[k] for k in ("protocol", "request_id", "provider", "effect_id")}
e["result"]={"status":"ok","data":{},"provenance":{"provider":"paperqa","effect_id":"wrong"},"coverage":{},"continuation":None,"errors":[]}
print(json.dumps(e))''',
        ]
        for index, snippet in enumerate(snippets):
            with self.subTest(index=index):
                result = self.run_worker({"python": self.executable(snippet)}, effect=f"effect-{index}")
                self.assertEqual(result["errors"][0]["code"], "invalid_worker_response")
                self.assertTrue(result["execution_unknown"])

    def test_config_credentials_argv_and_model_paths_are_rejected_before_spawn(self):
        for settings in ({"env": {"SECRET": "value"}}, {"api_key": "secret"}, {"python": [sys.executable, "-c", "bad"]},
                         {"env_names": ["BAD=VALUE"]}, {"timeout": 301}):
            with self.subTest(settings=settings):
                result = self.run_worker(settings)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["provenance"]["attempt_count"], 0)
                self.assertFalse(result.get("execution_unknown", False))
        result = self.run_worker(inputs={"python": "/tmp/model-executable"})
        self.assertEqual(result["errors"][0]["code"], "invalid_input")
        result = self.run_worker(inputs={"document_ids": ["../../outside"]})
        self.assertEqual(result["provenance"]["attempt_count"], 0)

    def test_effect_and_job_boundaries_prevent_path_access_and_replay(self):
        result = self.run_worker(effect="../escape")
        self.assertEqual(result["errors"][0]["code"], "invalid_effect_id")
        self.run_worker(settings={})
        result = self.run_worker(settings={})
        self.assertEqual(result["errors"][0]["code"], "effect_already_exists")
        self.assertEqual(result["provenance"]["attempt_count"], 0)
        (self.store.root / "jobs/linked").symlink_to(self.root)
        result = self.run_worker(effect="linked")
        self.assertEqual(result["errors"][0]["code"], "invalid_job_directory")

    def test_removed_providers_are_rejected_before_worker_setup_or_spawn(self):
        for provider in ("tooluniverse", "llm_nerre", "llm_organic_synthesis", "openscholar", "paperpilot"):
            with self.subTest(provider=provider), \
                    patch("rsi_literature.worker.subprocess.Popen", side_effect=AssertionError("No removed worker")), \
                    patch.object(self.store, "manifest", side_effect=AssertionError("Do not inspect sources")):
                result = run_sdk_worker(provider, self.settings, self.inputs, self.store, "removed-provider")
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["errors"][0]["code"], "unsupported_provider")
                self.assertEqual(result["provenance"]["attempt_count"], 0)
                self.assertFalse(result.get("execution_unknown", False))
                self.assertFalse((self.store.root / "jobs").exists())


if __name__ == "__main__":
    unittest.main()
