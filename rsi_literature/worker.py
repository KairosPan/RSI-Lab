"""Bound optional SDK work in a separate process; this is not an OS sandbox.

The host may select an installed Python interpreter and environment *names*.
Model/API credentials are not inherited unless their names appear in
``settings.env_names``. Environment values must never be put in the packet.
The worker can access host files/network permitted by its normal OS account.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid


PROTOCOL = "literature_sdk_worker_v1"
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_PACKET_BYTES = 2 * 1024 * 1024
_EFFECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_BASE_ENV = frozenset({"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "HF_HOME", "TORCH_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"})


class WorkerError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value
    def invalid_constant(value):
        raise ValueError("Nonfinite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _environment(settings):
    names = settings.get("env_names", [])
    if (not isinstance(names, list) or len(names) > 100
            or any(not isinstance(name, str) or not _ENV.fullmatch(name) for name in names)):
        raise WorkerError("invalid_configuration", "env_names must be a bounded list of environment variable names")
    # Values come directly from the host, never from the request packet.
    return {name: os.environ[name] for name in _BASE_ENV | set(names) if name in os.environ}


def _reject_secret_values(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise WorkerError("invalid_configuration", "Worker settings must have string keys")
            if key.lower() in {"env", "environment", "environ", "api_key", "access_token", "secret", "password", "authorization"}:
                raise WorkerError("invalid_configuration", "Pass credential environment names via env_names, never secret values in worker settings")
            _reject_secret_values(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_values(child)


def _directory(root, effect_id):
    if not isinstance(effect_id, str) or not _EFFECT.fullmatch(effect_id):
        raise WorkerError("invalid_effect_id", "Use a bounded host effect identifier")
    current = root
    for component in ("jobs", effect_id):
        current = current / component
        if current.is_symlink():
            raise WorkerError("invalid_job_directory", "Worker directories cannot be symbolic links")
        current.mkdir(exist_ok=True)
    job = current / "sdk-worker"
    try:
        job.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise WorkerError("effect_already_exists", "SDK worker already exists; reconcile the host effect before retrying") from exc
    return job


def _kill(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _capture(process, job, timeout):
    """Drain both pipes, writing no more than the per-stream disk byte limit."""
    deadline = time.monotonic() + timeout
    streams = {}
    try:
        for name in ("stdout", "stderr"):
            fd = os.open(job / (name + ".log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            streams[name] = os.fdopen(fd, "wb")
        counts = {"stdout": 0, "stderr": 0}
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("worker_timeout", "SDK worker exceeded its time budget; reconcile before retrying")
                for key, _ in selector.select(min(remaining, 0.2)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    allowed = MAX_STREAM_BYTES - counts[name]
                    streams[name].write(data[:allowed])
                    counts[name] += min(len(data), allowed)
                    if len(data) > allowed:
                        raise WorkerError("worker_output_limit", "SDK worker exceeded its output/log byte budget; reconcile before retrying")
            try:
                code = process.wait(timeout=max(deadline - time.monotonic(), 0.001))
            except subprocess.TimeoutExpired as exc:
                raise WorkerError("worker_timeout", "SDK worker exceeded its time budget; reconcile before retrying") from exc
        return code, counts
    except BaseException:
        _kill(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
        for stream in streams.values():
            stream.close()


def _validate(envelope, packet):
    if (not isinstance(envelope, dict) or set(envelope) != {"protocol", "request_id", "provider", "effect_id", "result"}
            or any(envelope.get(key) != packet[key] for key in ("protocol", "request_id", "provider", "effect_id"))):
        raise WorkerError("invalid_worker_response", "SDK worker response does not bind this request")
    result = envelope["result"]
    required = {"status", "data", "provenance", "coverage", "continuation", "errors"}
    if (not isinstance(result, dict) or not required <= set(result) or set(result) - required - {"execution_unknown"}
            or result["status"] not in {"ok", "partial", "unavailable", "failed"}
            or any(not isinstance(result[key], dict) for key in ("data", "provenance", "coverage"))
            or not isinstance(result["errors"], list) or any(not isinstance(error, dict) for error in result["errors"])
            or result["continuation"] is not None and not isinstance(result["continuation"], dict)
            or "execution_unknown" in result and type(result["execution_unknown"]) is not bool
            or result["provenance"].get("provider") != packet["provider"]
            or result["provenance"].get("effect_id") != packet["effect_id"]):
        raise WorkerError("invalid_worker_response", "SDK worker returned an invalid ToolResult contract")
    return result


def run_sdk_worker(provider, settings, inputs, document_store, effect_id):
    """Execute one SDK request under a bounded, host-selected Python process."""
    started = False
    result = {"status": "unavailable", "data": {}, "provenance": {"provider": provider, "effect_id": effect_id,
        "attempt_count": 0, "worker_protocol": PROTOCOL, "os_sandbox": False},
        "coverage": {"original_fulltext_verified": False}, "continuation": None, "errors": []}
    try:
        if not isinstance(provider, str) or provider not in {"langextract", "paperqa", "paperqa2"}:
            raise WorkerError("unsupported_provider", "No SDK worker is registered for this provider")
        if not isinstance(settings, dict) or not isinstance(inputs, dict):
            raise WorkerError("invalid_input", "Worker settings and inputs must be JSON objects")
        _reject_secret_values(settings)
        env = _environment(settings)
        python = settings.get("python", sys.executable)
        if not isinstance(python, str) or not python or "\x00" in python:
            raise WorkerError("invalid_configuration", "python must name one host interpreter, not an argv list")
        executable = shutil.which(python, path=env.get("PATH", os.defpath))
        if not executable:
            raise WorkerError("needs_configuration", "The host Python interpreter is not installed or executable")
        timeout = settings.get("timeout", 120)
        if type(timeout) not in {int, float} or not 1 <= timeout <= 300:
            raise WorkerError("invalid_configuration", "Worker timeout must be between 1 and 300 seconds")
        if set(inputs) & {"python", "command", "env", "env_names", "settings", "document_root", "path"}:
            raise WorkerError("invalid_input", "Models cannot supply worker paths, commands or configuration")
        if "document_ids" in inputs:
            ids = inputs["document_ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 8:
                raise WorkerError("invalid_input", "Use one to eight registered document IDs")
            for document_id in ids:
                document_store.manifest(document_id)
        packet = {"protocol": PROTOCOL, "request_id": uuid.uuid4().hex, "provider": provider, "effect_id": effect_id,
                  "settings": settings, "inputs": inputs, "document_root": str(document_store.root)}
        raw = _json(packet)
        if len(raw) > MAX_PACKET_BYTES:
            raise WorkerError("input_limit", "SDK worker request exceeds its byte budget")
        job = _directory(document_store.root, effect_id)
        fd = os.open(job / "request.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        with (job / "request.json").open("rb") as stdin:
            # Preserve venv interpreter identity: realpath would launch its base
            # Python, discarding the host-selected environment and dependencies.
            process = subprocess.Popen([os.path.abspath(executable), str(Path(__file__).resolve()), "--sdk-worker"],
                stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=job, env=env, start_new_session=True)
        started = True
        result["provenance"]["attempt_count"] = 1
        code, counts = _capture(process, job, timeout)
        if code:
            raise WorkerError("worker_exit", "SDK worker terminated without a certified result; reconcile before retrying")
        envelope = _strict_json((job / "stdout.log").read_bytes())
        certified = _validate(envelope, packet)
        certified["provenance"].update(attempt_count=1, worker_protocol=PROTOCOL, worker_processes=1,
            worker_output_bytes=counts, os_sandbox=False)
        return certified
    except WorkerError as exc:
        result["errors"] = [{"code": exc.code, "detail": str(exc), "retryability": "no" if started else "after_configuration"}]
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError):
        # Child output and OS exceptions may contain secrets. Do not forward them.
        result["errors"] = [{"code": "invalid_worker_response" if started else "worker_configuration_error",
            "detail": "SDK worker could not produce a bound result" if started else "SDK worker input/configuration is invalid",
            "retryability": "no" if started else "after_configuration"}]
    if started:
        result["status"] = "failed"
        result["execution_unknown"] = True
    return result


def _main():
    """No caller-supplied module paths: import this checkout explicitly."""
    output_fd = os.dup(sys.stdout.fileno())
    try:
        sys.stdout.flush()
        # FD redirection also captures C extensions/os.write(1, ...), which
        # contextlib.redirect_stdout alone cannot isolate.
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        raw = sys.stdin.buffer.read(MAX_PACKET_BYTES + 1)
        if len(raw) > MAX_PACKET_BYTES:
            return 2
        packet = _strict_json(raw)
        if not isinstance(packet, dict) or packet.get("protocol") != PROTOCOL:
            return 2
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from rsi_literature.documents import DocumentStore
        from rsi_literature.sdk_tools import run_sdk
        result = run_sdk(packet["provider"], packet["settings"], packet["inputs"],
                         DocumentStore(packet["document_root"]), packet["effect_id"])
        envelope = {key: packet[key] for key in ("protocol", "request_id", "provider", "effect_id")}
        envelope["result"] = result
        encoded = _json(envelope)
        if len(encoded) > MAX_STREAM_BYTES:
            return 3
        with os.fdopen(output_fd, "wb", closefd=False) as output:
            output.write(encoded)
        return 0
    except Exception:
        return 2
    finally:
        os.close(output_fd)


if __name__ == "__main__":
    sys.exit(_main() if sys.argv[1:] == ["--sdk-worker"] else 2)
