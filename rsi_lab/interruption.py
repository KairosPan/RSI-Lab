"""Explicit isolation of a stopped, tool-disabled local Codex invocation.

Isolation releases a physical execution lease, never an unknown financial
obligation or source authority. It is available only to the trusted operator.
"""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import time

from .store import digest
from . import native as native_tools

QUARANTINE_KIND = 'interrupted_codex_model_v1'
ATTESTATION_SCHEMA = 'codex_interruption_operator_attestation_v1'


def effect_is_quarantined(effect):
    ref = effect.get('execution_quarantine_ref')
    return (effect.get('execution_quarantine_kind') == QUARANTINE_KIND
        and effect.get('kind') in {'lab', 'meta', 'literature'}
        and isinstance(ref, str) and len(ref) == 64 and all(c in '0123456789abcdef' for c in ref))


def effect_holds_execution(effect):
    return effect.get('status') in {'prepared', 'dispatching', 'unknown'} and not effect_is_quarantined(effect)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def certify_interruption(effect, packet, folder, request, invocation, capture, frames, attestation):
    """Pure artifact/PID checks; no writes, terminal fabrication or provider calls."""
    from .call_recovery import RecoveryBlocked, _read, _object, _same
    from .session_context import find_owned_rollout, snapshot_from_bytes
    from .transport import _command
    def require(condition, reason):
        if not condition:
            raise RecoveryBlocked('interruption_not_certified', reason)
    # Codex's own tools act without a trace these checks could read; settings that cannot be read count as on.
    try:
        native_on = native_tools.any_on(effect.get('metadata', {}).get('native_capabilities'))
    except ValueError:
        native_on = True
    require(not native_on, 'A call with native capabilities cannot be certified free of tool activity')
    require(set(effect.get('reservations', {})) == {'model_calls', 'tokens', 'wall_seconds'}
            and effect['reservations']['model_calls'] == 1, 'Model invocation lacks the explicit bounded model resource contract')
    require(effect.get('kind') in {'lab', 'meta', 'literature'}
            and effect.get('status') in {'dispatching', 'unknown'}
            and not effect.get('metadata', {}).get('adapter_id')
            and not effect.get('metadata', {}).get('trial_id'), 'Only an unsettled, nonmutating model Effect can be isolated')
    required = {'schema_version', 'effect_id', 'packet_sha256', 'reason', 'pause_path', 'pause_sha256',
        'checked_at_unix', 'checked_pids', 'native_writer_quiesced', 'all_owned_writers_checked'}
    source_fields = {'source_root', 'source_manifest_sha256'}
    require(isinstance(attestation, dict) and required <= set(attestation)
            and set(attestation) <= required | source_fields
            and (set(attestation) & source_fields) in (set(), source_fields),
            'An explicit closed operator attestation is required')
    if source_fields <= set(attestation):
        root = Path(__file__).resolve().parent.parent
        require(isinstance(attestation['source_root'], str)
                and Path(attestation['source_root']).is_absolute()
                and Path(attestation['source_root']).resolve() == root,
                'Operator source root differs from the executing recovery implementation')
        paths = set()
        for directory in ('rsi_lab', 'rsi_domains', 'rsi_literature', 'experiments/pablo'):
            paths.update((root / directory).rglob('*.py'))
        manifest = {str(path.relative_to(root)): digest(_read(path)) for path in sorted(paths)
                    if 'tests' not in path.relative_to(root).parts}
        require(digest(manifest) == attestation['source_manifest_sha256'],
                'Operator source manifest differs from the executing recovery implementation')
    require(attestation['schema_version'] == ATTESTATION_SCHEMA
            and attestation['effect_id'] == effect['id'] and attestation['packet_sha256'] == digest(packet)
            and isinstance(attestation['reason'], str) and bool(attestation['reason'].strip())
            and attestation['native_writer_quiesced'] is True and attestation['all_owned_writers_checked'] is True
            and _finite(attestation['checked_at_unix']) and attestation['checked_at_unix'] <= time.time() + 5,
            'Operator attestation does not bind this interrupted invocation')
    pause_path = Path(attestation['pause_path'])
    require(pause_path.is_absolute(), 'Pause proof must use its explicit absolute path')
    pause_raw = _read(pause_path)
    require(digest(pause_raw) == attestation['pause_sha256'], 'Original pause proof changed')
    pause = json.loads(pause_raw)
    require(pause.get('schema_version') == 'user_requested_experiment_pause_v1'
            and pause.get('status') == 'paused' and pause.get('run_id') == effect['run_id']
            and pause.get('pending_model_effect_id') == effect['id']
            and pause.get('host_exited') is True and pause.get('owned_model_processes_exited') is True,
            'Pause does not certify this owned invocation was stopped')
    pids = attestation['checked_pids']
    require(isinstance(pids, list) and 2 <= len(pids) <= 64 and all(type(pid) is int and pid > 0 for pid in pids)
            and len(set(pids)) == len(pids) and pause.get('host_pid') in pids,
            'Check the stopped host and all its owned native writer PIDs')
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise RecoveryBlocked('interruption_not_certified', 'Cannot prove an attested PID exited') from exc
        raise RecoveryBlocked('interruption_not_certified', 'An attested process is still alive')
    records = capture['records']
    require(capture['gaps'] == [{'kind': 'stream_incomplete', 'seq': capture['durable_seq']}]
            and records[-1]['stream'] == 'control' and records[-1]['kind'] == 'stream_incomplete'
            and base64.b64decode(records[-1]['raw_base64']).decode().startswith('KeyboardInterrupt:'),
            'Only the durable KeyboardInterrupt marker may explain incomplete capture')
    require(not any(row['kind'] == 'attempt_finished' for row in records), 'Finished attempts use normal reconciliation')
    require([(row['stream'], row['kind']) for row in records if row['stream'] == 'control']
            == [('control', 'attempt_started'), ('control', 'stream_incomplete')],
            'Interrupted attempt has unexpected control activity')
    stdout = [row for row in records if row['stream'] == 'stdout']
    require(all(row['kind'] in {'stream_chunk', 'event_frame'} for row in stdout),
            'Interrupted attempt has an invalid or unsupported stdout frame')
    captured = b''.join(base64.b64decode(row['raw_base64']) for row in stdout if row['kind'] == 'stream_chunk')
    framed = b''.join(base64.b64decode(row['raw_base64']) for row in stdout if row['kind'] == 'event_frame')
    require(captured == framed, 'Unparsed captured stdout cannot prove absence of an answer or usage')
    for frame in frames:
        kind = frame.get('type')
        require(kind in {'thread.started', 'turn.started', 'item.started', 'item.updated', 'item.completed'},
                'A terminal, error, answer or unsupported provider event is present')
        if kind.startswith('item.'):
            require(isinstance(frame.get('item'), dict) and frame['item'].get('type') == 'reasoning',
                    'The interrupted request contains an answer or native tool activity')
    result_path = folder / 'transport_result.json'
    saved = _object(result_path)
    require(saved.get('backend') == invocation.get('backend') == 'codex'
            and saved.get('execution_status') == 'unknown' and saved.get('dispatched') is True
            and saved.get('terminal_received') is False and saved.get('stream_complete') is False
            and saved.get('response') is None and saved.get('usage') is None
            and saved.get('usage_complete') is False and saved.get('protocol_valid') is False
            and isinstance(saved.get('error'), str) and saved['error'].startswith('KeyboardInterrupt:'),
            'Transport must durably record a dispatched, interrupted request without answer or usage')
    require(type(saved.get('returncode')) is int and saved['returncode'] != 0
            and saved['returncode'] == invocation.get('returncode')
            and invocation.get('dispatched') is True
            and _finite(invocation.get('started_at_unix')) and _finite(invocation.get('finished_at_unix'))
            and invocation['finished_at_unix'] >= invocation['started_at_unix']
            and attestation['checked_at_unix'] >= invocation['finished_at_unix']
            and _finite(saved.get('wall_seconds')),
            'Invocation completion and transport exit must agree')
    require(abs((invocation['finished_at_unix'] - invocation['started_at_unix']) - saved['wall_seconds']) <= 2,
            'Measured wall duration contradicts invocation start/end')
    _same(saved.get('journal', {}).get('durable_seq'), capture['durable_seq'], 'Interrupted journal frontier differs')
    _same(saved.get('journal', {}).get('head_hash'), capture['head_hash'], 'Interrupted journal hash differs')
    require(saved.get('journal', {}).get('write_failed') is False, 'Journal persistence failed')
    require(_read(folder / 'raw_response.txt') == b'' and _object_null(folder / 'response.json')
            and _object(folder / 'usage.json') == {'usage': None, 'usage_complete': False},
            'Unapplied answer or cost counters exist; use normal recovery')
    _same(pause.get('transport_receipt_sha256'), digest(_read(result_path)), 'Pause names another transport receipt')
    meta, binding = effect['metadata'], invocation['session']
    session = meta['agent_session']
    thread = saved.get('provider_thread_id')
    starts = [row.get('thread_id') for row in frames if row.get('type') == 'thread.started']
    require(starts == [thread] and thread, 'Native thread is missing or ambiguous')
    require(session.get('role') == effect['kind'] and request['mode'] in ({'discovery', 'disposition'}
            if effect['kind'] == 'meta' else {effect['kind']}), 'Native invocation role differs from its model Effect')
    require(saved.get('session_id') == meta['agent_session_id'] and saved.get('workspace') == binding['workspace']
            and saved.get('codex_home') == binding['codex_home'], 'Interrupted transport belongs to another owner')
    if session.get('provider_thread_id'):
        _same(thread, session['provider_thread_id'], 'Interrupted resume continued another thread')
    image_paths = [str(folder / 'images' / ('image-%03d.png' % i))
                   for i in range(len(request.get('image_attachments', [])))]
    _same(invocation['command'], _command(folder, request['model'], request['effort'],
        binding['workspace'], binding, image_paths), 'Only the exact tool-disabled local Codex command can be isolated')
    native_path = find_owned_rollout(binding['codex_home'], thread, binding['workspace'])
    require(str(native_path) == saved.get('native_session_ref') and saved.get('native_session_error') is None,
            'Interrupted native rollout binding is unavailable')
    native_raw = _read(native_path)
    prior = (session.get('context_manifest') or {}).get('native_prefix')
    require(not session.get('provider_thread_id') or bool(prior), 'Resumed invocation lacks its certified original prefix')
    snapshot = snapshot_from_bytes(native_raw, expected_thread_id=thread,
        expected_cwd=binding['workspace'], expected_prefix=prior)
    _same(snapshot['prefix_sha256'], saved.get('native_session_sha256'), 'Interrupted native bytes changed')
    suffix = native_raw[prior['prefix_bytes'] if prior else 0:]
    user_prompts = []
    for line in suffix.splitlines():
        record = json.loads(line)
        kind, payload = record['type'], record['payload']
        if kind == 'response_item':
            require(payload.get('type') == 'reasoning' or
                payload.get('type') == 'message' and payload.get('role') == 'user',
                'Native suffix contains an answer or tool activity')
            if payload.get('type') == 'message':
                user_prompts.append('\n'.join(item.get('text', '') for item in payload['content']
                                               if item.get('type') == 'input_text'))
        elif kind == 'event_msg':
            require(payload.get('type') in {'thread_settings_applied', 'task_started', 'user_message',
                'agent_reasoning', 'item_completed'}, 'Native suffix contains a terminal or unsupported activity')
            if payload.get('type') == 'item_completed':
                require(payload.get('item', {}).get('type') in {'UserMessage', 'Reasoning'},
                        'Native item contains an answer or tool activity')
        else:
            require(kind in {'turn_context', 'session_meta'}, 'Native suffix contains cost/terminal or unsupported records')
    require(user_prompts == [request['prompt']], 'Native suffix must bind exactly this interrupted user request')
    paths = {'prompt': folder / 'prompt.txt', 'schema': folder / 'schema.json',
        'invocation': folder / 'invocation.json', 'transport_result': result_path,
        'journal': folder / 'journal.jsonl', 'journal_frontier': folder / 'journal.jsonl.frontier.json',
        'raw_response': folder / 'raw_response.txt', 'response': folder / 'response.json',
        'usage': folder / 'usage.json', 'native': native_path, 'pause': pause_path}
    artifacts = {key: {'path': str(path), 'sha256': digest(_read(path))} for key, path in paths.items()}
    require(artifacts['native']['sha256'] == saved['native_session_sha256']
            and artifacts['pause']['sha256'] == attestation['pause_sha256'], 'Artifact changed during interruption checks')
    from .journal import read_journal
    latest = read_journal(folder / 'journal.jsonl')
    require(all(latest[key] == capture[key] for key in ('durable_seq', 'head_hash', 'gaps')),
            'Interrupted journal changed during certification')
    _same(_object(result_path), saved, 'Transport result changed during certification')
    _same(_object(folder / 'invocation.json'), invocation, 'Invocation changed during certification')
    _same(_read(folder / 'prompt.txt'), request['prompt'].encode(), 'Prompt changed during certification')
    _same(_object(folder / 'schema.json'), request['response_schema'], 'Schema changed during certification')
    implementation = Path(__file__).resolve().parent
    code = {name: digest((implementation / name).read_bytes()) for name in (
        'interruption.py', 'call_recovery.py', 'controller.py', 'agent_state.py', 'store.py',
        'transport.py', 'journal.py', 'session_context.py', 'images.py')}
    proof = {'schema_version': QUARANTINE_KIND, 'effect_id': effect['id'], 'run_id': effect['run_id'],
        'work_id': effect['work_id'], 'session_id': meta['agent_session_id'], 'turn_id': meta['agent_turn_id'],
        'session_fence': meta['session_fence'], 'packet_sha256': digest(packet),
        'operator_attestation': attestation, 'artifacts': artifacts, 'prior_native_prefix': prior,
        'recovery_implementation': {'source_root': str(implementation.parent), 'files': code},
        'journal': {key: capture[key] for key in ('durable_seq', 'head_hash', 'gaps')},
        'known_actual_costs': {'model_calls': 1, 'wall_seconds': saved['wall_seconds']},
        'terminal_received': False, 'answer_observed': False, 'native_writer_quiesced': True,
        'authority': 'Physical execution isolation only; unknown accounting remains an obligation'}
    return {'status': 'unknown', 'actual_costs': proof['known_actual_costs'], 'response': None,
        'usage': None, 'usage_complete': False, 'terminal_received': False, 'protocol_valid': False,
        'native_writer_quiesced': True, 'recovery_requires_fencing': False, 'unknown_usage_is_zero': False,
        'sent_packet_sha256': digest(packet), 'sent_prompt_sha256': digest(request['prompt'].encode()),
        'interruption_quarantine_proof': proof,
        'reconciliation': {'effect_id': effect['id'], 'provenance': 'Verified durable interruption and explicit operator process fencing'}}


def _object_null(path):
    from .call_recovery import _read
    return json.loads(_read(path)) is None
