import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from lab_harness.transport import invoke


def events(text='{"action":"stop"}', usage=True, extras=()):
    entries = [{"type": "thread.started", "thread_id": "test"}, {"type": "turn.started"}]
    entries.extend(extras)
    entries.append({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": text}})
    completed = {"type": "turn.completed"}
    if usage:
        completed["usage"] = {"input_tokens": 11, "cached_input_tokens": 2, "output_tokens": 4}
    entries.append(completed)
    return "\n".join(json.dumps(entry) for entry in entries) + "\n"


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "call"
        self.schema = {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"], "additionalProperties": False}

    def invoke_mock(self, stdout, stderr="", returncode=0):
        process = Mock(pid=123456, returncode=returncode)
        process.communicate.return_value = (stdout, stderr)
        process.poll.return_value = returncode
        with patch("lab_harness.transport.subprocess.Popen", return_value=process) as popen:
            result = invoke("Plan from evidence.", self.schema, self.folder, "gpt-6-astra", "medium")
        return result, popen, process

    def test_structured_success_and_audit_artifacts(self):
        self.folder.mkdir()
        (self.folder / "input.json").write_text('{"candidate":"c1"}')
        result, popen, process = self.invoke_mock(events())
        self.assertEqual(result["response"], {"action": "stop"})
        self.assertIsNone(result["error"])
        self.assertTrue(result["usage_complete"])
        self.assertEqual(result["usage"], {"input_tokens": 11, "cached_input_tokens": 2, "output_tokens": 4})
        command = popen.call_args.args[0]
        self.assertIn("--output-schema", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--no-daemon", command)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        cwd = Path(popen.call_args.kwargs["cwd"])
        self.assertFalse(cwd.exists())
        self.assertEqual(json.loads((self.folder / "transport_result.json").read_text()), result)
        self.assertEqual(json.loads((self.folder / "input.json").read_text()), {"candidate": "c1"})
        self.assertIn("Plan from evidence.", process.communicate.call_args.args[0])

    def test_missing_usage_remains_unknown(self):
        result, _, _ = self.invoke_mock(events(usage=False))
        self.assertIsNone(result["error"])
        self.assertIsNone(result["usage"])
        self.assertFalse(result["usage_complete"])

    def test_tool_events_are_rejected_even_with_valid_final_json(self):
        extra = {"type": "item.completed", "item": {"type": "command_execution", "command": "anything"}}
        result, _, _ = self.invoke_mock(events(extras=[extra]))
        self.assertIsNone(result["response"])
        self.assertIn("Forbidden tool", result["error"])
        self.assertIn("command_execution", (self.folder / "events.jsonl").read_text())

    def test_invalid_json_and_duplicate_keys_fail_closed(self):
        for number, text in enumerate(("```json\n{}\n```", '{"action":"a","action":"b"}', '{"value":NaN}', '[]')):
            self.folder = Path(self.tmp.name) / f"invalid_{number}"
            result, _, _ = self.invoke_mock(events(text))
            self.assertIsNone(result["response"])
            self.assertIsNotNone(result["error"])

    def test_incomplete_or_unrecognized_events_fail_closed(self):
        for number, stdout in enumerate((events().replace('"turn.completed"', '"unknown.event"'), "not json\n" + events(), events() + events())):
            self.folder = Path(self.tmp.name) / f"events_{number}"
            result, _, _ = self.invoke_mock(stdout)
            self.assertIsNone(result["response"])
            self.assertIsNotNone(result["error"])

    def test_nonzero_exit_retains_observed_usage_without_claiming_completeness(self):
        result, _, _ = self.invoke_mock(events(), stderr="transport failed", returncode=1)
        self.assertIsNone(result["response"])
        self.assertEqual(result["usage"]["input_tokens"], 11)
        self.assertFalse(result["usage_complete"])
        self.assertEqual((self.folder / "stderr.log").read_text(), "transport failed")

    def test_timeout_kills_process_group_and_does_not_retry(self):
        process = Mock(pid=123456, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 0.1), (events(usage=False), "timeout")]
        process.poll.return_value = -9
        with patch("lab_harness.transport.subprocess.Popen", return_value=process) as popen, patch("lab_harness.transport.os.killpg") as kill:
            result = invoke("Plan", self.schema, self.folder, "gpt-6-astra", "medium", timeout=0.1)
        kill.assert_called_once_with(123456, signal.SIGKILL)
        popen.assert_called_once()
        self.assertIsNone(result["response"])
        self.assertFalse(result["usage_complete"])
        self.assertIn("timed out", result["error"])

    def test_missing_cli_is_logged_as_failed_attempt(self):
        with patch("lab_harness.transport.subprocess.Popen", side_effect=FileNotFoundError("codex unavailable")):
            result = invoke("Plan", self.schema, self.folder, "gpt-6-astra", "medium")
        self.assertIn("FileNotFoundError", result["error"])
        self.assertIsNone(result["returncode"])
        self.assertIsNone(result["usage"])
        self.assertFalse(json.loads((self.folder / "invocation.json").read_text())["dispatched"])
        self.assertTrue((self.folder / "events.jsonl").exists())

    def test_existing_execution_artifacts_are_never_overwritten(self):
        self.folder.mkdir()
        (self.folder / "events.jsonl").write_text("existing evidence")
        with patch("lab_harness.transport.subprocess.Popen") as popen:
            with self.assertRaises(FileExistsError):
                invoke("Plan", self.schema, self.folder, "gpt-6-astra", "medium")
        popen.assert_not_called()
        self.assertEqual((self.folder / "events.jsonl").read_text(), "existing evidence")

    def test_invalid_parameters_do_not_dispatch(self):
        with patch("lab_harness.transport.subprocess.Popen") as popen:
            for timeout in (True, 0, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    invoke("Plan", self.schema, self.folder, "gpt-6-astra", "medium", timeout=timeout)
            with self.assertRaises(ValueError):
                invoke("Plan", self.schema, self.folder, "gpt-6-astra", "ultra")
        popen.assert_not_called()
        self.assertFalse(self.folder.exists())


if __name__ == "__main__":
    unittest.main()
