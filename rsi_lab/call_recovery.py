"""Recover a local Codex attempt without repeating a model request.

Only the exact effect directory, its acknowledged journal prefix and the owned
native thread are inspected. A terminal model event is distinct from proof that
the old CLI writer exited; callers must fence an unconfirmed writer before
committing/resuming the session. Missing cost counters are never filled with 0.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import uuid

from .store import digest
from .journal import read_journal
from .transport import _json, audited_events
from .session_context import find_owned_rollout, read_native_snapshot


class RecoveryBlocked(ValueError):
    def __init__(self, reason, detail):
        self.reason = reason
        super().__init__(reason + ': ' + detail)


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RecoveryBlocked('recovery_artifact_missing', path.name + ' is missing or a symlink')
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise RecoveryBlocked('recovery_artifact_changed', path.name + ' changed during recovery')
    return raw


def _object(path):
    try:
        value = _json(_read(path).decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, RecoveryBlocked):
            raise
        raise RecoveryBlocked('recovery_artifact_invalid', Path(path).name) from exc
    if not isinstance(value, dict):
        raise RecoveryBlocked('recovery_artifact_invalid', Path(path).name + ' is not an object')
    return value


def _decoded(record):
    return _json(base64.b64decode(record['raw_base64']).decode('utf-8'))


def _same(value, expected, message):
    if value != expected:
        raise RecoveryBlocked('recovery_binding_mismatch', message)


def recover_codex_receipt(effect, packet_bytes, calls_root):
    """Return an authenticated receipt, None if unfinished, or raise on conflict.

    The result carries ``recovery_requires_fencing`` when no durable CLI exit is
    available, even if the provider already emitted a turn terminal. The caller
    may settle known costs/execution while retaining the session recovery duty.
    This function performs no writes, subprocess launches or provider requests.
    """
    effect_id = effect.get('id')
    if (not isinstance(effect_id, str) or not effect_id or Path(effect_id).name != effect_id
            or effect_id in {'.', '..'}):
        raise RecoveryBlocked('recovery_binding_mismatch', 'unsafe effect identity')
    expected_hash = effect.get('packet_sha256', effect.get('packet_hash'))
    if not isinstance(packet_bytes, bytes) or digest(packet_bytes) != expected_hash:
        raise RecoveryBlocked('recovery_binding_mismatch', 'immutable packet hash differs')
    root = Path(calls_root).resolve()
    folder = root / effect_id
    if not folder.exists():
        return None
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
        raise RecoveryBlocked('recovery_scope_mismatch', 'call directory leaves its configured root')
    journal_path = folder / 'journal.jsonl'
    if not journal_path.exists():
        return None
    try:
        request = _json(packet_bytes.decode('utf-8'))
        prompt = request['prompt'].encode('utf-8')
        schema = request['response_schema']
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise RecoveryBlocked('recovery_binding_mismatch', 'invalid immutable model request') from exc
    _same(_read(folder / 'prompt.txt'), prompt, 'saved prompt differs from dispatched input')
    _same(_object(folder / 'schema.json'), schema, 'saved schema differs from dispatched request')
    invocation = _object(folder / 'invocation.json')
    _same(invocation.get('model'), request['model'], 'invocation model differs')
    _same(invocation.get('effort'), request['effort'], 'invocation effort differs')
    # Read all evidence before interpreting it. The frontier excludes an
    # unconfirmed/torn suffix even when its bytes happen to look like a terminal.
    _read(journal_path)
    _read(journal_path.with_name(journal_path.name + '.frontier.json'))
    try:
        capture = read_journal(journal_path)
    except (ValueError, OSError) as exc:
        raise RecoveryBlocked('journal_integrity_error', str(exc)) from exc
    if capture['gaps']:
        raise RecoveryBlocked('journal_gap', 'journal has an unconfirmed, conflicting or incomplete tail')
    records = capture['records']
    if not records or records[0]['stream'] != 'control' or records[0]['kind'] != 'attempt_started':
        raise RecoveryBlocked('recovery_binding_mismatch', 'journal lacks its attempt identity')
    try:
        start = _decoded(records[0])
    except (ValueError, UnicodeError, KeyError, TypeError) as exc:
        raise RecoveryBlocked('recovery_binding_mismatch', 'invalid attempt identity') from exc
    metadata = effect.get('metadata', {})
    session = metadata.get('agent_session')
    if not session:
        raise RecoveryBlocked('recovery_binding_mismatch', 'recovery requires an explicitly owned agent session')
    expected = {'run_id': effect['run_id'], 'effect_id': effect_id,
                'agent_id': session['agent_instance_id'], 'session_id': metadata['agent_session_id'],
                'turn_id': metadata['agent_turn_id'], 'fence': metadata['session_fence']}
    for key, value in expected.items():
        _same(start.get(key), value, 'journal ' + key + ' differs')
    _same(start.get('prompt_sha256'), digest(prompt), 'journal prompt hash differs')
    recorded_invocation = start.get('invocation', {})
    for key in ('model', 'effort', 'command', 'cwd', 'session'):
        _same(recorded_invocation.get(key), invocation.get(key), 'invocation ' + key + ' changed')
    binding = recorded_invocation.get('session')
    if not isinstance(binding, dict):
        raise RecoveryBlocked('recovery_binding_mismatch', 'journal has no native session binding')
    sid = metadata['agent_session_id']
    _same(session['id'], sid, 'session metadata identity differs')
    _same(binding.get('session_id'), sid, 'native session identity differs')
    workspace = root.parent / 'agents' / sid / 'workspace'
    _same(str(Path(binding['workspace']).resolve()), str(workspace.resolve()), 'native workspace differs')
    _same(str(Path(invocation['cwd']).resolve()), str(workspace.resolve()), 'invocation workspace differs')
    _same(binding.get('provider_thread_id'), session.get('provider_thread_id'), 'resume thread differs')
    frames = []
    for row in records:
        if row['stream'] == 'stdout' and row['kind'] == 'event_frame' and row.get('duplicate_of') is None:
            try:
                frame = _decoded(row)
            except (ValueError, UnicodeError, KeyError, TypeError) as exc:
                raise RecoveryBlocked('journal_integrity_error', 'invalid provider frame') from exc
            if isinstance(frame, dict):
                frames.append(frame)
    terminals = [row for row in frames if row.get('type') in {'turn.completed', 'turn.failed'}]
    if not terminals:
        return None
    if len(terminals) != 1:
        raise RecoveryBlocked('recovery_terminal_conflict', 'expected one native turn terminal')
    starts = [row.get('thread_id') for row in frames if row.get('type') == 'thread.started']
    if len(starts) != 1:
        raise RecoveryBlocked('recovery_binding_mismatch', 'native thread identity is missing or ambiguous')
    try:
        thread_id = str(uuid.UUID(starts[0]))
        _same(thread_id, starts[0], 'native thread UUID is not canonical')
    except (ValueError, TypeError, AttributeError) as exc:
        raise RecoveryBlocked('recovery_binding_mismatch', 'invalid native thread identity') from exc
    if session.get('provider_thread_id'):
        _same(thread_id, session['provider_thread_id'], 'provider continued another native thread')
    raw, usage, usage_complete, errors = audited_events(folder)
    finished_rows = [row for row in records if row['stream'] == 'control' and row['kind'] == 'attempt_finished']
    if len(finished_rows) > 1:
        raise RecoveryBlocked('recovery_terminal_conflict', 'multiple process exit records')
    finished = _decoded(finished_rows[0]) if finished_rows else None
    result_path = folder / 'transport_result.json'
    saved = _object(result_path) if result_path.exists() else None
    if saved is not None:
        _same(saved.get('provider_thread_id'), thread_id, 'saved result thread differs')
        _same(saved.get('session_id'), sid, 'saved result session differs')
        _same(saved.get('terminal_received'), True, 'saved result has no terminal')
        saved_journal = saved.get('journal') or {}
        _same(saved_journal.get('durable_seq'), capture['durable_seq'], 'saved journal frontier differs')
        _same(saved_journal.get('head_hash'), capture['head_hash'], 'saved journal hash differs')
        _same(saved.get('usage'), usage, 'saved usage differs from durable events')
        if saved.get('stream_complete') is not True:
            raise RecoveryBlocked('journal_gap', 'saved result declares incomplete capture')
    status = 'succeeded' if terminals[0]['type'] == 'turn.completed' else 'failed_definite'
    if finished is not None:
        _same(finished.get('execution_status'), status, 'process outcome contradicts native terminal')
        if status == 'succeeded' and finished.get('returncode') != 0:
            raise RecoveryBlocked('recovery_terminal_conflict', 'completed turn has a nonzero process exit')
    if saved is not None:
        _same(saved.get('execution_status'), status, 'saved execution outcome differs')
        if finished is not None:
            _same(saved.get('returncode'), finished.get('returncode'), 'saved process exit differs')
    benign_failure_errors = {
        'Expected exactly one completed turn, observed 0',
        'Expected exactly one nonempty final assistant message',
    }
    contract_errors = [message for message in errors if not (status == 'failed_definite' and (
        message in benign_failure_errors or message.startswith('Codex error event: ')
        or message.startswith('Codex exited with status ')))]
    protocol_valid = not contract_errors and (saved is None or saved.get('protocol_valid') is not False)
    response, parse_error = None, None
    if status == 'succeeded' and not errors:
        try:
            response = _json(raw)
            if not isinstance(response, dict):
                raise ValueError('Structured response must be a JSON object')
        except (ValueError, TypeError) as exc:
            response, parse_error = None, f'{type(exc).__name__}: {exc}'
    if saved is not None:
        _same(saved.get('response'), response, 'saved response differs from durable output')
    costs = {'model_calls': 1}
    if usage is not None and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens', 'output_tokens')):
        costs['tokens'] = usage['input_tokens'] + usage['output_tokens']
    if saved is not None and type(saved.get('wall_seconds')) in {int, float}:
        wall = saved['wall_seconds']
        if math.isfinite(wall) and wall >= 0:
            costs['wall_seconds'] = wall
    context = {'effective_context_hash': metadata.get('effective_context_hash'),
               'native_session_ref': None, 'native_session_sha256': None, 'native_session_error': None,
               'provider_context_verified': False, 'retained_read_authority': False}
    prior = (session.get('context_manifest') or {}).get('native_prefix')
    try:
        if session.get('provider_thread_id') and not prior:
            raise ValueError('Resumed native session has no certified prior prefix')
        native_path = find_owned_rollout(binding['codex_home'], thread_id, workspace)
        snapshot = read_native_snapshot(native_path, expected_thread_id=thread_id,
                                        expected_cwd=workspace, expected_prefix=prior)
        if saved is not None and saved.get('native_session_sha256'):
            _same(snapshot['prefix_sha256'], saved['native_session_sha256'], 'native terminal snapshot changed')
        context.update(native_session_ref=str(native_path), native_session_sha256=snapshot['prefix_sha256'],
            native_prefix={key: snapshot[key] for key in ('prefix_bytes', 'prefix_sha256', 'provider_thread_id')})
    except (ValueError, OSError, KeyError) as exc:
        context['native_session_error'] = str(exc)
        if (session.get('provider_thread_id') or prior is not None
                or finished is not None or saved is not None):
            protocol_valid = False
    latest = read_journal(journal_path)
    if (latest['durable_seq'] != capture['durable_seq'] or latest['head_hash'] != capture['head_hash']
            or latest['gaps']):
        raise RecoveryBlocked('journal_changed', 'journal advanced while recovery was reading it')
    if saved is not None:
        _same(_object(result_path), saved, 'saved result changed during recovery')
    quiesced = finished is not None
    evidence_path = result_path if saved is not None else journal_path
    return {'status': status, 'actual_costs': costs, 'response': response,
            'usage': usage, 'usage_complete': usage_complete,
            'terminal_received': True, 'protocol_valid': protocol_valid,
            'provider_thread_id': thread_id, 'context_manifest': context,
            'journal': {'path': str(journal_path), 'durable_seq': capture['durable_seq'],
                        'head_hash': capture['head_hash']},
            'parse_error': parse_error, 'error': '; '.join(errors) if errors else None,
            'receipt_ref': str(evidence_path), 'receipt_sha256': digest(_read(evidence_path)),
            'sent_packet_sha256': expected_hash, 'sent_prompt_sha256': digest(prompt),
            'unknown_usage_is_zero': False, 'recovered_from_journal': True,
            'native_writer_quiesced': quiesced, 'recovery_requires_fencing': not quiesced,
            'reconciliation': {'effect_id': effect_id, 'provenance': 'verified local durable execution journal'}}
