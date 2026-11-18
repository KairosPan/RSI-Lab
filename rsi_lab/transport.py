"""Audited Codex transport with bounded evidence images.

Call admission belongs to the controller. Image inputs do not enable tools.

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
from .images import (MAX_IMAGES, attachment_manifest, decoded_image_bytes, image_manifest_sha256,
                     validate_image_attachment)
from . import native as native_tools
import base64

DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use",
    "multi_agent", "multi_agent_v2", "image_generation", "skill_search", "hooks",
    "view_image", "goals", "sleep_tool",
)
ARTIFACTS = frozenset({"invocation.json", "prompt.txt", "schema.json", "events.jsonl",
                       "stderr.log", "response.json", "raw_response.txt", "usage.json", "transport_result.json", "journal.jsonl",
                       "images", "images.json"})
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


def _events(stdout, allowed_items=frozenset()):
    """Audit the whole event stream, preserving unknown usage instead of zeroes.

    ``allowed_items`` are the item types a call's native settings allow beside
    ``agent_message`` and ``reasoning`` (``native.stream_items``). Empty, the
    turn must hold exactly one assistant message. Otherwise earlier messages
    are progress notes and the last completed one is the output.

    Lines end at ``\\n`` only, as the journal frames them: Codex leaves U+2028,
    U+2029 and U+0085 unescaped inside strings. A trailing ``\\r`` is JSON
    whitespace.
    """
    outputs, errors, usages = [], [], []
    completions = 0
    failed = False
    supported = {"thread.started", "turn.started", "turn.completed", "turn.failed", "error",
                 "item.started", "item.updated", "item.completed"}
    allowed = {"agent_message", "reasoning"} | allowed_items
    for number, line in enumerate(stdout.split("\n"), 1):
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
            if item_kind not in allowed:
                errors.append(f"Forbidden tool or unsupported item: {item_kind!r}")
            if kind == "item.completed" and item_kind == "agent_message":
                if not isinstance(item.get("text"), str):
                    errors.append("Assistant output must be text")
                else:
                    outputs.append(item["text"])
    if completions != 1:
        errors.append(f"Expected exactly one completed turn, observed {completions}")
    if not allowed_items:
        if len(outputs) != 1 or not outputs[0].strip():
            errors.append("Expected exactly one nonempty final assistant message")
        raw = "\n".join(outputs)
    else:
        if not outputs or not outputs[-1].strip():
            errors.append("Expected a nonempty final assistant message")
        raw = outputs[-1] if outputs else ""
    # Codex normally reports usage once at turn.completed. Repeated usage frames
    # are retained as ambiguous rather than silently double-counted or discarded.
    usage = usages[0][1] if len(usages) == 1 else None
    complete = (not failed and completions == 1 and len(usages) == 1
                and usages[0][0] == "turn.completed"
                and all(key in usage for key in ("input_tokens", "output_tokens")))
    if len(usages) > 1:
        errors.append("Multiple usage records; totals are ambiguous")
    return raw, usage, complete, errors


# The Popen of import time: tests stand a local process in for Codex by
# replacing ``subprocess.Popen``, and the process table must still be read.
_POPEN = subprocess.Popen


def _process_table():
    """Every process as ``{pid: (ppid, start)}``, from ``ps -axo pid=,ppid=,lstart=``.

    ``start`` is the raw ``lstart`` text, decoded losslessly: with the pid it
    names one process, as a pid can be reused once its process is gone.
    Raises ``OSError`` or ``ValueError`` when the table cannot be read.
    """
    with _POPEN(['ps', '-axo', 'pid=,ppid=,lstart='], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL) as ps:
        try:
            listing = ps.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            ps.kill()
            raise OSError('ps did not list the processes within 5 seconds') from None
    if ps.returncode != 0:
        raise OSError(f'ps exited with status {ps.returncode}')
    table = {}
    for line in listing.decode('utf-8', 'surrogateescape').splitlines():
        pid, parent, start = line.split(None, 2)
        table[int(pid)] = (int(parent), start.strip())
    return table


# Seconds between samples of the process table while a call with the terminal on runs.
TRACK_SECONDS = 1.0


def _track(tracked, table, root=None):
    """Add to ``tracked`` (``{pid: start}``) every process ``table`` shows below ``root`` or below a tracked process
    that is still the same one, with the same start time: a pid reused by a later process is no root."""
    children = {}
    for pid, (parent, start) in table.items():
        children.setdefault(parent, []).append(pid)
    pending = [pid for pid, start in tracked.items() if pid in table and table[pid][1] == start]
    if root is not None:
        pending.append(root)
    while pending:
        for child in children.pop(pending.pop(), ()):
            tracked[child] = table[child][1]
            pending.append(child)


def _gone(pid):
    """Whether no process has ``pid``: only a signal that finds none says so."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:          # A process this one may not signal still runs.
        pass
    return False


def _stop(tracked):
    """Stop every process of ``tracked`` (``{pid: start}``) that is still the same one.

    A pid can be reused once its process is gone, so the table is read just
    before any signal, and only a pid still present with its recorded start
    time is signalled: through its group when it leads one, by pid otherwise,
    and this process's own group never. Returns whether, within 5 seconds, no
    tracked process is left, a pid that is gone or has another start time
    counting as stopped; ``False``, with no signal sent, when that read fails.
    """
    if not tracked:
        return True
    try:
        table = _process_table()
    except (OSError, ValueError):
        return False
    own_group = os.getpgrp()
    for pid, start in tracked.items():
        if pid not in table or table[pid][1] != start:
            continue         # Gone, or the pid now names another process: nothing tracked is left to stop.
        try:
            if os.getpgid(pid) == pid and pid != own_group:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:      # Gone already, or not this process's to signal: the check below decides.
            pass
    remaining = tracked
    deadline = time.monotonic() + 5
    while True:
        remaining = {pid: start for pid, start in remaining.items() if not _gone(pid)}
        if remaining:
            try:
                table = _process_table()
            except (OSError, ValueError):
                table = {}   # Then no pid still answering counts as reused.
            remaining = {pid: start for pid, start in remaining.items() if pid not in table or table[pid][1] == start}
        if not remaining or time.monotonic() >= deadline:
            return not remaining
        time.sleep(.05)


def _terminate(process, tracked=None):
    """Kill the invocation's process group and every process seen under it.

    A command Codex runs can lead its own process group, out of reach of
    Codex's. Before any signal, what now runs below Codex is added to
    ``tracked``, the processes earlier samples saw under the call (none when
    the terminal is off), since a killed parent's children are reparented and
    no longer found through it. Codex's group is then killed, and every
    tracked process by ``_stop``. Codex's own pid cannot have been reused, as
    Codex is not reaped before this. Returns whether every process seen under
    the call, at a sample or at the kill, was stopped; a process that detaches
    and loses its parent between two samples is not seen. ``False`` when a
    read of the process table fails, as nothing can then be confirmed, though
    Codex's group is killed all the same.
    """
    tracked = {} if tracked is None else tracked
    try:
        _track(tracked, _process_table(), process.pid)
        listed = True
    except (OSError, ValueError):
        listed = False
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return _stop(tracked) and listed


def _sweep(tracked):
    """Stop what still runs of ``tracked`` once a call has ended without a kill.

    A last sample follows the tracked processes that are still the same to
    their children. Codex's pid is no root: Codex has been reaped, and its pid
    can be reused. Returns what ``_stop`` returns, ``False`` when the last
    sample cannot be read; ``True`` when nothing was tracked.
    """
    if not tracked:
        return True
    try:
        _track(tracked, _process_table())
        sampled = True
    except (OSError, ValueError):
        sampled = False
    return _stop(tracked) and sampled


def _provider_stdout(folder):
    """A call's unique provider stdout frames as text, and its journal capture.

    An older one-shot archive without a journal is read from ``events.jsonl``
    and has no capture.
    """
    path = folder / 'journal.jsonl'
    if not path.exists():
        return (folder / 'events.jsonl').read_text(), None
    capture = read_journal(path)
    frames = [base64.b64decode(row['raw_base64']).decode('utf-8', errors='replace')
              for row in capture['records']
              if row['stream'] == 'stdout' and row['kind'] in {'event_frame', 'invalid_event_frame'}
              and row.get('duplicate_of') is None]
    return ''.join(frames), capture


def _recorded_native(folder):
    """The native settings a call's ``invocation.json`` records, in normal form.

    A missing or unreadable file, one without a ``native`` key, and settings
    that are not normal form all read as everything off: today's strict reading.
    """
    try:
        invocation = _json((folder / 'invocation.json').read_text())
        if isinstance(invocation, dict):
            return native_tools.validate(invocation.get('native'))
    except (OSError, ValueError):
        pass
    return native_tools.validate(None)


def audited_events(folder):
    """Parse the durable unique provider frames, retaining all raw duplicates.

    The item types allowed are those of the native settings recorded in
    ``invocation.json``. Older one-shot archives without a journal remain
    readable. A torn/conflicting journal cannot certify a complete invocation.
    """
    folder = Path(folder)
    stdout, capture = _provider_stdout(folder)
    raw, usage, complete, errors = _events(stdout, native_tools.stream_items(_recorded_native(folder)))
    if capture is not None and capture['gaps']:
        errors.append('Execution journal has unresolved gaps or conflicting provider events')
        complete = False
    return raw, usage, complete, errors


ACTIVITY_ENTRIES = 32
ACTIVITY_CHARACTERS = 200


def stream_activity(folder):
    """What a call's event stream shows its tools did, from its completed items.

    Reads the unique frames ``audited_events`` reads, split as ``_events``
    splits them. ``items`` counts the
    completed items of each type; ``commands``, ``searches`` and
    ``files_changed`` keep the first ``ACTIVITY_ENTRIES`` entries in stream
    order, and ``omitted`` counts the entries left out of the three. Every
    string is cut to ``ACTIVITY_CHARACTERS``; a value of the wrong type is
    ``None``. The stream is not a complete record: a command that fails may
    produce no item.
    """
    def text(value):
        return value[:ACTIVITY_CHARACTERS] if isinstance(value, str) else None

    items, commands, searches, files_changed = {}, [], [], []
    for line in _provider_stdout(Path(folder))[0].split('\n'):
        try:
            event = _json(line)
        except (ValueError, TypeError):
            continue
        item = event.get('item') if isinstance(event, dict) and event.get('type') == 'item.completed' else None
        if not isinstance(item, dict) or not isinstance(item.get('type'), str):
            continue
        kind = text(item['type'])
        items[kind] = items.get(kind, 0) + 1
        if kind == 'command_execution':
            exit_code = item.get('exit_code')
            commands.append({'command': text(item.get('command')),
                             'exit_code': exit_code if type(exit_code) is int else None,
                             'status': text(item.get('status'))})
        elif kind == 'web_search' and isinstance(item.get('query'), str):
            searches.append(text(item['query']))
        elif kind == 'file_change' and isinstance(item.get('changes'), list):
            files_changed += [text(change['path']) for change in item['changes']
                              if isinstance(change, dict) and isinstance(change.get('path'), str)]
    lists = {'commands': commands, 'searches': searches, 'files_changed': files_changed}
    omitted = sum(max(0, len(entries) - ACTIVITY_ENTRIES) for entries in lists.values())
    return {'items': items, **{name: entries[:ACTIVITY_ENTRIES] for name, entries in lists.items()},
            'omitted': omitted}


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


def _command(folder, model, effort, cwd, session, image_paths=(), native=None, run_root=None):
    # Everything off is today's command; native settings that are on need the
    # session's Codex home and the run root for the permission profile.
    settings = native_tools.validate(native)
    parts = {'sandbox': ['--sandbox', 'read-only'], 'flags': [], 'config': [], 'enabled': set()}
    if native_tools.any_on(settings):
        parts = native_tools.command_parts(settings, workspace=cwd, run_root=run_root,
                                           codex_home=session['codex_home'], model=model, effort=effort)
    command = ['codex', '--no-daemon', '--ask-for-approval', 'never', 'exec',
               *parts['sandbox'], '-C', cwd]
    if session and session['provider_thread_id']:
        command.append('resume')
    command.extend(['--ignore-user-config', *parts['flags'], '--model', model,
                    '--skip-git-repo-check', '--json', '--output-schema', str(folder / 'schema.json'),
                    '-c', f'model_reasoning_effort={json.dumps(effort)}',
                    '-c', ('web_search="live"' if settings['web_search'] else 'web_search="disabled"'),
                    '-c', 'suppress_unstable_features_warning=true'])
    for value in parts['config']:
        command.extend(['-c', value])
    if session is None:
        command.append('--ephemeral')
    for feature in DISABLED_FEATURES:
        if feature not in parts['enabled']:
            command.extend(['--disable', feature])
    command.extend(['--enable', 'skip_host_skill_discovery'])
    for path in image_paths:
        command.extend(['-i', str(path)])
    if session and session['provider_thread_id']:
        command.append(session['provider_thread_id'])
    command.append('-')
    return command


def _archive_images(folder, images, manifest):
    """Write only validated bytes to fresh host-chosen files before dispatch."""
    image_folder = folder / 'images'
    image_folder.mkdir(mode=0o700)
    files = []
    for index, attachment in enumerate(images):
        path = image_folder / f'image-{index:03d}.png'
        raw = decoded_image_bytes(attachment)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        files.append({'attachment_id': attachment['attachment_id'], 'path': str(path),
                      'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})
    directory = os.open(image_folder, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _write_json(folder / 'images.json', {'manifest': manifest, 'files': files})
    return [record['path'] for record in files]


def _stream_process(process, prompt, journal, folder, timeout, stop, tracked=None):
    """Drain both pipes while sending input; no turn-sized communicate buffer.

    Each received chunk is durable before parsing complete event frames. This
    also preserves non-UTF8 data and unfinished frames if the writer is killed.
    ``stop`` kills the process tree when the deadline elapses; the pipes then
    have 5 seconds to drain. Given ``tracked``, the process table is sampled
    every ``TRACK_SECONDS`` until then, adding to it what runs under the
    process; a sample that cannot be read is skipped.
    """
    selector = selectors.DefaultSelector()
    pending = memoryview(prompt.encode('utf-8'))
    buffers = {'stdout': b'', 'stderr': b''}
    captures = {name: (folder / ('events.jsonl' if name == 'stdout' else 'stderr.log')).open('xb', buffering=0)
                for name in buffers}
    deadline = time.monotonic() + timeout
    next_sample = time.monotonic() if tracked is not None else math.inf
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
            if not timed_out and now >= next_sample:
                next_sample = now + TRACK_SECONDS
                try:
                    _track(tracked, _process_table(), process.pid)
                except (OSError, ValueError):
                    pass     # The next sample tries again.
            if not timed_out and now >= deadline:
                timed_out = True
                stop()
                drain_deadline = time.monotonic() + 5
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
           *, session=None, journal_metadata=None, images=None, native=None, run_root=None) -> dict:
    """Run one admitted request and durably capture text and optional PNG inputs.

    A session binding opts into persistent create/resume. Calls without a binding
    remain explicitly ephemeral for compatibility with one-shot validators.
    Execution completion, parsing, and application validation are separate.
    Native settings that switch anything on need a session and the run root;
    they are recorded as ``native`` in the invocation and the result, which
    carry no such key when everything is off.

    The result's ``descendants_stopped`` says whether every process seen under
    the call was stopped by its end. With the terminal on, the process table
    is sampled every ``TRACK_SECONDS`` while Codex runs, and whatever was seen
    and still runs is stopped however the call ends. A timeout or an
    interruption that kills Codex also stops what runs under it at the kill.
    A process that detaches and loses its parent between two samples is not
    seen. With the terminal off nothing is sampled, and a call Codex ends on
    its own reports ``True``.
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
    if images is None:
        images = []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError('Images must be a bounded list of validated attachments')
    images = [validate_image_attachment(record, with_data=True) for record in images]
    image_manifest = attachment_manifest(images)
    image_digest = image_manifest_sha256(images) if images else None
    settings = native_tools.validate(native)
    native_on = native_tools.any_on(settings)
    if native_on:
        if session is None or run_root is None:
            raise ValueError('Native capabilities require a persistent session and a run root')
        # Denied by its resolved path, as the session's workspace and Codex home are named.
        run_root = str(Path(run_root).resolve())
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
    sent_prompt = (native_tools.PREAMBLE if native_on else PREAMBLE) + prompt
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
    descendants_stopped = True
    tracked = {}                             # Every process seen under the call, as pid: start time.
    terminal_on = settings['terminal'] is not None
    killed = False

    def stop():
        nonlocal descendants_stopped, killed
        killed = True
        descendants_stopped = False          # Stays False if _terminate is interrupted before it confirms.
        descendants_stopped = _terminate(process, tracked)

    invocation = {'backend': 'codex', 'model': model, 'effort': effort,
                  'timeout_seconds': timeout, 'started_at_unix': time.time(), 'dispatched': False,
                  'session': binding}
    if native_on:
        invocation['native'] = settings
    if images:
        invocation.update(requested_image_manifest=image_manifest,
                          requested_image_manifest_sha256=image_digest)
    try:
        if binding:
            cwd = binding['workspace']
        else:
            temporary = tempfile.TemporaryDirectory(prefix='rsi-codex-')
            cwd = temporary.name
        if native_on:
            # TMPDIR of the terminal's commands; the workspace is their only writable place.
            Path(cwd, '.tmp').mkdir(exist_ok=True)
        image_paths = _archive_images(folder, images, image_manifest) if images else []
        command = _command(folder, model, effort, cwd, binding, image_paths, native=settings, run_root=run_root)
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
        if images:
            invocation.update(sent_image_manifest=image_manifest,
                              sent_image_manifest_sha256=image_digest)
        _write_json(folder / 'invocation.json', invocation)
        returncode, timed_out, partial_tail = _stream_process(process, sent_prompt, journal, folder, timeout, stop,
                                                              tracked if terminal_on else None)
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
            'Expected a nonempty final assistant message',
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
            stop()
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
        if terminal_on and process is not None and not killed:
            # Codex ended without a kill, however the call ended: what was seen under it and still runs is stopped.
            descendants_stopped = False      # Stays False if _sweep is interrupted before it confirms.
            descendants_stopped = _sweep(tracked)
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
                  'descendants_stopped': descendants_stopped, 'backend': 'codex', 'provider_thread_id': thread_id,
                  'session_id': binding['session_id'] if binding else None,
                  'codex_home': binding['codex_home'] if binding else None,
                  'workspace': binding['workspace'] if binding else None,
                  'journal': journal_snapshot, 'dispatched': invocation['dispatched'],
                  'native_session_ref': native_session_ref, 'native_session_sha256': native_session_sha256,
                  'native_session_error': native_session_error}
        if images and invocation['dispatched']:
            result.update(sent_image_manifest=image_manifest, sent_image_manifest_sha256=image_digest)
        if native_on:
            result['native'] = settings
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
