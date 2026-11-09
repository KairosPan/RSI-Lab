"""Audited, text-only Codex transport. Call admission belongs to the controller.

This module makes no calls on import. Every invocation retains raw evidence,
including failed attempts, and never automatically retries or switches models.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
import uuid

from .journal import JournalWriter, read_journal, raw_stream
import base64

DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use",
    "multi_agent", "multi_agent_v2", "image_generation", "skill_search", "hooks",
    "view_image", "goals", "sleep_tool",
)
ARTIFACTS = frozenset({"invocation.json", "prompt.txt", "schema.json", "events.jsonl",
                       "stderr.log", "response.json", "raw_response.txt", "usage.json", "transport_result.json", "journal.jsonl"})
PREAMBLE = (
    "Return only one JSON object matching the supplied response schema. Use only the supplied "
    "task and evidence. Do not use tools, read files, browse, delegate, or inspect your environment.\n\n"
)


def _write_json(path, value):
    path = Path(path)
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    # Atomic metadata publication; event data uses JournalWriter append+fsync.
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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


def audited_events(folder):
    """Parse the durable unique provider frames, retaining all raw duplicates.

    Older one-shot archives without a journal remain readable. A torn/conflicting
    journal cannot certify a complete invocation.
    """
    folder = Path(folder)
    path = folder / 'journal.jsonl'
    if not path.exists():
        return _events((folder / 'events.jsonl').read_text())
    capture = read_journal(path)
    frames = [base64.b64decode(row['raw_base64']).decode('utf-8', errors='replace')
              for row in capture['records']
              if row['stream'] == 'stdout' and row['kind'] in {'event_frame', 'invalid_event_frame'}
              and row.get('duplicate_of') is None]
    raw, usage, complete, errors = _events(''.join(frames))
    if capture['gaps']:
        errors.append('Execution journal has unresolved gaps or conflicting provider events')
        complete = False
    return raw, usage, complete, errors


def _session_options(session):
    if session is None:
        return None
    if not isinstance(session, dict):
        raise ValueError('Session binding must be an object')
    session_id = session.get('session_id')
    if (not isinstance(session_id, str) or not session_id
            or Path(session_id).name != session_id or session_id in ('.', '..')):
        raise ValueError('Unsafe session identity')
    workspace = Path(session['workspace'])
    if workspace.is_symlink():
        raise ValueError('Session workspace cannot be a symlink')
    workspace.mkdir(parents=True, exist_ok=True)
    native = session.get('provider_thread_id')
    if native is not None:
        try:
            if str(uuid.UUID(native)) != native:
                raise ValueError('Noncanonical thread UUID')
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError('Resume requires an explicit canonical provider thread UUID') from exc
    # The host may provide an isolated, already-authenticated home. Never copy
    # credentials to a run directory, and never use --last to select a thread.
    codex_home = Path(session.get('codex_home') or os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
    return {'session_id': session_id, 'workspace': str(workspace.resolve()),
            'provider_thread_id': native, 'codex_home': str(codex_home),
            'explicit_codex_home': session.get('codex_home') is not None}


def _command(folder, model, effort, cwd, session):
    command = ['codex', '--no-daemon', '--ask-for-approval', 'never', 'exec',
               '--sandbox', 'read-only', '-C', cwd]
    if session and session['provider_thread_id']:
        command.append('resume')
    command.extend(['--ignore-user-config', '--model', model,
                    '--skip-git-repo-check', '--json', '--output-schema', str(folder / 'schema.json'),
                    '-c', f'model_reasoning_effort={json.dumps(effort)}',
                    '-c', 'web_search="disabled"',
                    '-c', 'suppress_unstable_features_warning=true'])
    if session is None:
        command.append('--ephemeral')
    for feature in DISABLED_FEATURES:
        command.extend(['--disable', feature])
    command.extend(['--enable', 'skip_host_skill_discovery'])
    if session and session['provider_thread_id']:
        command.append(session['provider_thread_id'])
    command.append('-')
    return command


def _stream_process(process, prompt, journal, folder, timeout):
    """Drain both pipes while sending input; no turn-sized communicate buffer.

    Each received chunk is durable before parsing complete event frames. This
    also preserves non-UTF8 data and unfinished frames if the writer is killed.
    """
    selector = selectors.DefaultSelector()
    pending = memoryview(prompt.encode('utf-8'))
    buffers = {'stdout': b'', 'stderr': b''}
    captures = {name: (folder / ('events.jsonl' if name == 'stdout' else 'stderr.log')).open('xb', buffering=0)
                for name in buffers}
    deadline = time.monotonic() + timeout
    timed_out, drain_deadline = False, None
    try:
        for name in ('stdout', 'stderr'):
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        while selector.get_map():
            now = time.monotonic()
            if not timed_out and now >= deadline:
                timed_out = True
                _terminate(process)
                drain_deadline = now + 5
                journal.append('control', b'Invocation deadline elapsed', kind='stream_incomplete')
            if timed_out and now >= drain_deadline:
                raise TimeoutError('Unable to drain killed invocation pipes')
            for key, _ in selector.select(min(.05, max(0, (drain_deadline if timed_out else deadline) - now))):
                if key.data == 'stdin':
                    try:
                        count = os.write(key.fd, pending[:65536]) if pending else 0
                        pending = pending[count:]
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                try:
                    raw = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not raw:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                name = key.data
                journal.append(name, raw, kind='stream_chunk')
                capture_tail = memoryview(raw)
                while capture_tail:
                    written = captures[name].write(capture_tail)
                    if not written:
                        raise OSError('Capture write did not advance')
                    capture_tail = capture_tail[written:]
                os.fsync(captures[name].fileno())
                if name == 'stdout':
                    buffers[name] += raw
                    while b'\n' in buffers[name]:
                        frame, buffers[name] = buffers[name].split(b'\n', 1)
                        frame += b'\n'
                        kind, provider_id = 'event_frame', None
                        try:
                            event = _json(frame.decode('utf-8'))
                            if isinstance(event, dict):
                                candidate = event.get('event_id')
                                provider_id = candidate if isinstance(candidate, str) and candidate else None
                            else:
                                kind = 'invalid_event_frame'
                        except (UnicodeError, ValueError, TypeError):
                            kind = 'invalid_event_frame'
                        journal.append(name, frame, kind=kind, provider_event_id=provider_id)
        if buffers['stdout']:
            journal.append('stdout', buffers['stdout'], kind='stream_incomplete')
        return process.wait(timeout=5), timed_out, bool(buffers['stdout'])
    finally:
        selector.close()
        for stream in captures.values():
            stream.close()


def invoke(prompt: str, schema: dict, folder: Path, model: str, effort: str, timeout=300,
           *, session=None, journal_metadata=None) -> dict:
    """Run one admitted text-only request and durably capture it as it happens.

    A session binding opts into persistent create/resume. Calls without a binding
    remain explicitly ephemeral for compatibility with one-shot validators.
    Execution completion, parsing, and application validation are separate.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Prompt must be nonempty text')
    if not isinstance(schema, dict):
        raise ValueError('Schema must be a JSON object')
    json.dumps(schema, allow_nan=False)
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}', model):
        raise ValueError('Invalid model identifier')
    if effort not in {'low', 'medium', 'high', 'xhigh', 'max'}:
        raise ValueError('Unsupported reasoning effort')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Timeout must be positive and finite')
    binding = _session_options(session)
    folder = Path(folder)
    if folder.is_symlink():
        raise ValueError('Call folder cannot be a symlink')
    folder.mkdir(parents=True, exist_ok=True)
    collisions = ARTIFACTS.intersection(path.name for path in folder.iterdir())
    if collisions:
        raise FileExistsError(f'Refusing to overwrite transport artifacts: {sorted(collisions)}')
    folder = folder.resolve()
    with (folder / 'invocation.json').open('x', encoding='utf-8') as stream:
        json.dump({'backend': 'codex', 'model': model, 'effort': effort}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    sent_prompt = PREAMBLE + prompt
    with (folder / 'prompt.txt').open('xb') as stream:
        stream.write(sent_prompt.encode()); stream.flush(); os.fsync(stream.fileno())
    _write_json(folder / 'schema.json', schema)
    started = time.monotonic()
    response = usage = returncode = None
    usage_complete = terminal_received = protocol_valid = False
    error = parse_error = None
    raw = ''
    execution_status = 'failed_definite'
    interrupted = process = journal = None
    thread_id = binding['provider_thread_id'] if binding else None
    temporary = None
    stream_complete = False
    journal_snapshot = None
    invocation = {'backend': 'codex', 'model': model, 'effort': effort,
                  'timeout_seconds': timeout, 'started_at_unix': time.time(), 'dispatched': False,
                  'session': binding}
    try:
        if binding:
            cwd = binding['workspace']
        else:
            temporary = tempfile.TemporaryDirectory(prefix='rsi-codex-')
            cwd = temporary.name
        command = _command(folder, model, effort, cwd, binding)
        invocation.update(command=command, cwd=cwd)
        _write_json(folder / 'invocation.json', invocation)
        journal = JournalWriter(folder / 'journal.jsonl', {
            **(journal_metadata or {}), 'invocation': invocation, 'prompt_sha256': hashlib.sha256(sent_prompt.encode()).hexdigest()})
        environment = None
        if binding and binding['explicit_codex_home']:
            environment = {**os.environ, 'CODEX_HOME': binding['codex_home']}
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, env=environment,
                                   start_new_session=True)
        execution_status = 'unknown'
        invocation['dispatched'] = True
        _write_json(folder / 'invocation.json', invocation)
        returncode, timed_out, partial_tail = _stream_process(process, sent_prompt, journal, folder, timeout)
        captured = read_journal(folder / 'journal.jsonl')
        stdout = raw_stream(captured, 'stdout').decode('utf-8', errors='replace')
        raw, usage, usage_complete, errors = audited_events(folder)
        events = []
        unique_frames = [base64.b64decode(row['raw_base64']).decode('utf-8', errors='replace')
                         for row in captured['records'] if row['stream'] == 'stdout'
                         and row['kind'] == 'event_frame' and row.get('duplicate_of') is None]
        for line in unique_frames:
            try:
                event = _json(line)
                if isinstance(event, dict):
                    events.append(event)
            except (ValueError, TypeError):
                pass
        starts = [e.get('thread_id') for e in events if e.get('type') == 'thread.started']
        terminal = [e.get('type') for e in events if e.get('type') in {'turn.completed', 'turn.failed'}]
        terminal_received = len(terminal) == 1
        stream_complete = not timed_out and not partial_tail and not captured['gaps']
        if binding:
            if len(starts) != 1 or not isinstance(starts[0], str):
                errors.append('Persistent invocation requires exactly one native thread identity')
            else:
                try:
                    if str(uuid.UUID(starts[0])) != starts[0]:
                        raise ValueError('Noncanonical thread UUID')
                    if binding['provider_thread_id'] and starts[0] != binding['provider_thread_id']:
                        errors.append('Provider resumed a different thread')
                    else:
                        thread_id = starts[0]
                except ValueError:
                    errors.append('Provider thread identity is not a UUID')
        if stream_complete and terminal_received:
            if terminal[0] == 'turn.completed' and returncode == 0:
                execution_status = 'succeeded'
            elif terminal[0] == 'turn.failed':
                execution_status = 'failed_definite'
        if returncode != 0:
            errors.append(f'Codex exited with status {returncode}')
        if timed_out:
            errors.append(f'Codex timed out after {timeout} seconds; remote completion and usage may be unknown')
        if partial_tail:
            errors.append('Codex event stream ended with an incomplete frame')
        if captured['gaps']:
            errors.append('Execution journal has unresolved gaps or conflicting provider events')
        if execution_status != 'succeeded':
            usage_complete = False
        # A certified turn.failed is a known execution failure, not a malformed
        # protocol. Keep its error text while permitting the controller to close
        # the native turn without inventing output or a working-memory update.
        expected_failure = stream_complete and terminal == ['turn.failed']
        benign_failure_errors = (
            'Expected exactly one completed turn, observed 0',
            'Expected exactly one nonempty final assistant message',
        )
        contract_errors = [message for message in errors if not (expected_failure and (
            message in benign_failure_errors or message.startswith('Codex error event: ')
            or message.startswith('Codex exited with status ')))]
        protocol_valid = not contract_errors
        if errors:
            error = '; '.join(errors)
        elif raw:
            try:
                response = _json(raw)
                if not isinstance(response, dict):
                    raise ValueError('Structured response must be a JSON object')
            except (ValueError, TypeError) as exc:
                parse_error = f'{type(exc).__name__}: {exc}'
                response = None
        journal.append('control', json.dumps({'returncode': returncode,
                       'execution_status': execution_status, 'parse_error': parse_error,
                       'error': error}).encode(), kind='attempt_finished')
    except BaseException as exc:
        if process is not None and process.poll() is None:
            _terminate(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        returncode = process.returncode if process is not None else None
        error = f'{type(exc).__name__}: {exc}'
        response = None
        usage_complete = protocol_valid = False
        execution_status = 'unknown' if invocation['dispatched'] else 'failed_definite'
        if journal is not None and not journal.failed:
            try:
                journal.append('control', error.encode(), kind='stream_incomplete')
            except OSError:
                pass
        if not isinstance(exc, Exception):
            interrupted = exc
    finally:
        if journal is not None:
            journal_snapshot = journal.snapshot()
            journal.close()
        if process is not None:
            for name in ('stdin', 'stdout', 'stderr'):
                pipe = getattr(process, name, None)
                if pipe is not None and not pipe.closed:
                    pipe.close()
        if temporary is not None:
            temporary.cleanup()
        # A killed writer can leave a useful journal prefix even if receipt
        # publication itself fails; recovery verifies that prefix separately.
        for name in ('events.jsonl', 'stderr.log'):
            path = folder / name
            if not path.exists():
                path.touch(exist_ok=False)
        native_session_ref = native_session_sha256 = native_session_error = None
        if binding and thread_id:
            try:
                from .session_context import find_owned_rollout
                native_path = find_owned_rollout(binding['codex_home'], thread_id, binding['workspace'])
                native_session_ref = str(native_path)
                native_session_sha256 = hashlib.sha256(native_path.read_bytes()).hexdigest()
            except (OSError, ValueError) as exc:
                native_session_error = f'{type(exc).__name__}: {exc}'
        result = {'response': response, 'usage': usage, 'usage_complete': usage_complete,
                  'error': error, 'protocol_valid': protocol_valid, 'parse_error': parse_error, 'execution_status': execution_status,
                  'terminal_received': terminal_received, 'stream_complete': stream_complete,
                  'wall_seconds': time.monotonic() - started, 'returncode': returncode,
                  'backend': 'codex', 'provider_thread_id': thread_id,
                  'session_id': binding['session_id'] if binding else None,
                  'codex_home': binding['codex_home'] if binding else None,
                  'workspace': binding['workspace'] if binding else None,
                  'journal': journal_snapshot, 'dispatched': invocation['dispatched'],
                  'native_session_ref': native_session_ref, 'native_session_sha256': native_session_sha256,
                  'native_session_error': native_session_error}
        invocation.update(finished_at_unix=time.time(), returncode=returncode)
        _write_json(folder / 'invocation.json', invocation)
        with (folder / 'raw_response.txt').open('xb') as stream:
            stream.write(raw.encode()); stream.flush(); os.fsync(stream.fileno())
        _write_json(folder / 'response.json', response)
        _write_json(folder / 'usage.json', {'usage': usage, 'usage_complete': usage_complete})
        _write_json(folder / 'transport_result.json', result)
    if interrupted is not None:
        raise interrupted
    return result
