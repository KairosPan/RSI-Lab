"""Audited, text-only Codex transport. Call admission belongs to the controller.

This module makes no calls on import. Every invocation retains raw evidence,
including failed attempts, and never automatically retries or switches models.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use",
    "multi_agent", "multi_agent_v2", "image_generation", "skill_search", "hooks",
    "view_image", "goals", "sleep_tool",
)
ARTIFACTS = frozenset({"invocation.json", "prompt.txt", "schema.json", "events.jsonl",
                       "stderr.log", "response.json", "raw_response.txt", "usage.json", "transport_result.json"})
PREAMBLE = (
    "Return only one JSON object matching the supplied response schema. Use only the supplied "
    "task and evidence. Do not use tools, read files, browse, delegate, or inspect your environment.\n\n"
)


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(text):
    return json.loads(text, object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid JSON number: {value}")))


def _events(stdout):
    """Audit the whole event stream, preserving unknown usage instead of zeroes."""
    outputs, errors, usages = [], [], []
    completions = 0
    failed = False
    supported = {"thread.started", "turn.started", "turn.completed", "turn.failed", "error",
                 "item.started", "item.updated", "item.completed"}
    for number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = _json(line)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
        except (ValueError, TypeError) as exc:
            errors.append(f"Invalid event at line {number}: {exc}")
            continue
        kind = event.get("type")
        if kind not in supported:
            errors.append(f"Unsupported event type: {kind!r}")
        if kind == "turn.completed":
            completions += 1
        if kind in {"turn.failed", "error"}:
            failed = True
            errors.append(f"Codex error event: {json.dumps(event, ensure_ascii=False)}")
        if "usage" in event:
            usage = event["usage"]
            if not isinstance(usage, dict):
                errors.append("Malformed usage record")
            else:
                valid = {}
                for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_tokens"):
                    if key in usage:
                        value = usage[key]
                        if type(value) is not int or value < 0:
                            errors.append(f"Invalid usage counter: {key}")
                        else:
                            valid[key] = value
                usages.append((kind, valid))
        if isinstance(kind, str) and kind.startswith("item."):
            item = event.get("item")
            if not isinstance(item, dict):
                errors.append("Malformed item event")
                continue
            item_kind = item.get("type")
            if item_kind not in {"agent_message", "reasoning"}:
                errors.append(f"Forbidden tool or unsupported item: {item_kind!r}")
            if kind == "item.completed" and item_kind == "agent_message":
                if not isinstance(item.get("text"), str):
                    errors.append("Assistant output must be text")
                else:
                    outputs.append(item["text"])
    if completions != 1:
        errors.append(f"Expected exactly one completed turn, observed {completions}")
    if len(outputs) != 1 or not outputs[0].strip():
        errors.append("Expected exactly one nonempty final assistant message")
    # Codex normally reports usage once at turn.completed. Repeated usage frames
    # are retained as ambiguous rather than silently double-counted or discarded.
    usage = usages[0][1] if len(usages) == 1 else None
    complete = (not failed and completions == 1 and len(usages) == 1
                and usages[0][0] == "turn.completed"
                and all(key in usage for key in ("input_tokens", "output_tokens")))
    if len(usages) > 1:
        errors.append("Multiple usage records; totals are ambiguous")
    return "\n".join(outputs), usage, complete, errors


def _terminate(process):
    """Kill the invocation's process group, including any lingering child."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def invoke(prompt: str, schema: dict, folder: Path, model: str, effort: str, timeout=300) -> dict:
    """Invoke one Codex attempt and persist success/failure without overwriting.

    The caller must reserve its model-call budget first and validate application
    semantics after this returns. JSON Schema is supplied to the CLI; this
    transport additionally validates strict JSON syntax and audits all events.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt must be nonempty text")
    if not isinstance(schema, dict):
        raise ValueError("Schema must be a JSON object")
    json.dumps(schema, allow_nan=False)
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", model):
        raise ValueError("Invalid model identifier")
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported reasoning effort")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be positive and finite")
    folder = Path(folder)
    if folder.is_symlink():
        raise ValueError("Call folder cannot be a symlink")
    folder.mkdir(parents=True, exist_ok=True)
    collisions = ARTIFACTS.intersection(path.name for path in folder.iterdir())
    if collisions:
        raise FileExistsError(f"Refusing to overwrite transport artifacts: {sorted(collisions)}")
    folder = folder.resolve()
    # Exclusive creation also prevents a concurrent transport from sharing a call.
    with (folder / "invocation.json").open("x", encoding="utf-8") as stream:
        json.dump({"backend": "codex", "model": model, "effort": effort, "timeout": timeout}, stream)
    sent_prompt = PREAMBLE + prompt
    (folder / "prompt.txt").write_text(sent_prompt, encoding="utf-8")
    _write_json(folder / "schema.json", schema)
    started = time.monotonic()
    stdout = stderr = raw = ""
    response = usage = returncode = None
    usage_complete = False
    error = None
    interrupted = None
    process = None
    invocation = {"backend": "codex", "model": model, "effort": effort,
                  "timeout_seconds": timeout, "started_at_unix": time.time(), "dispatched": False}
    try:
        with tempfile.TemporaryDirectory(prefix="rsi-codex-") as cwd:
            command = ["codex", "--no-daemon", "--ask-for-approval", "never", "exec",
                       "--ignore-user-config", "--ephemeral", "--model", model,
                       "--sandbox", "read-only", "--skip-git-repo-check", "--json", "-C", cwd,
                       "--output-schema", str(folder / "schema.json"),
                       "-c", f"model_reasoning_effort={json.dumps(effort)}",
                       "-c", 'web_search="disabled"',
                       "-c", "suppress_unstable_features_warning=true"]
            for feature in DISABLED_FEATURES:
                command.extend(["--disable", feature])
            command.extend(["--enable", "skip_host_skill_discovery", "-"])
            invocation.update(command=command, cwd=cwd)
            _write_json(folder / "invocation.json", invocation)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                       cwd=cwd, start_new_session=True)
            invocation["dispatched"] = True
            _write_json(folder / "invocation.json", invocation)
            try:
                stdout, stderr = process.communicate(sent_prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate(process)
                stdout, stderr = process.communicate()
                error = f"Codex timed out after {timeout} seconds; remote completion and usage may be unknown"
            returncode = process.returncode
            raw, usage, usage_complete, errors = _events(stdout)
            if returncode != 0:
                errors.append(f"Codex exited with status {returncode}")
            if error is not None or returncode != 0:
                usage_complete = False
            if errors:
                error = "; ".join(([error] if error else []) + errors)
            if error is None:
                response = _json(raw)
                if not isinstance(response, dict):
                    raise ValueError("Structured response must be a JSON object")
    except BaseException as exc:
        if process is not None and process.poll() is None:
            _terminate(process)
            stdout, stderr = process.communicate()
            returncode = process.returncode
            raw, usage, _, _ = _events(stdout)
            usage_complete = False
        error = f"{type(exc).__name__}: {exc}"
        response = None
        if not isinstance(exc, Exception):
            interrupted = exc
    finally:
        if error:
            response = None
        result = {"response": response, "usage": usage, "usage_complete": usage_complete,
                  "error": error, "wall_seconds": time.monotonic() - started,
                  "returncode": returncode, "backend": "codex"}
        invocation.update(finished_at_unix=time.time(), returncode=returncode)
        _write_json(folder / "invocation.json", invocation)
        (folder / "events.jsonl").write_text(stdout, encoding="utf-8")
        (folder / "stderr.log").write_text(stderr, encoding="utf-8")
        (folder / "raw_response.txt").write_text(raw, encoding="utf-8")
        _write_json(folder / "response.json", response)
        _write_json(folder / "usage.json", {"usage": usage, "usage_complete": usage_complete})
        _write_json(folder / "transport_result.json", result)
    if interrupted is not None:
        raise interrupted
    return result
