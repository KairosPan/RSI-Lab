"""Observable native-session context, with explicit limits on CLI attestation.

The Codex CLI persists rollout history, not a signed copy of every provider
request.  This module verifies the *owned* rollout's exact durable prefix and
bounds all observable history plus the new request.  It never turns an old ACK,
a reasoning summary, or a rollout file into proof of current model delivery.
Unsupported compaction or a changed prefix requires explicit reconstruction.

No model calls or ledger writes occur here.  Provider framing, hidden reasoning,
and automatic internal requests need a separately registered runtime adapter;
``provider_context_verified`` is deliberately False for the CLI rollout reader.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

from .store import digest, encode

CONTRACT = 'codex-observable-rollout-v1'
THREAD_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z')
COMPACTION_KINDS = {'compacted', 'compaction', 'context_compaction', 'compaction_trigger',
                    'context_compacted', 'thread_rolled_back', 'thread_rollback'}
# The first implementation supports the text-only no-native-tools transport.
RESPONSE_KINDS = {'message', 'reasoning'}
OUTER_KINDS = {'session_meta', 'turn_context', 'response_item', 'event_msg'}


class ContextUnavailable(ValueError):
    """The native prefix cannot safely be resumed under the declared contract."""

    def __init__(self, reason, detail):
        self.reason = reason
        super().__init__(f'{reason}: {detail}')


def _json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate JSON key')
            obj[key] = value
        return obj
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))


def _identity(thread_id):
    if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
        raise ValueError('A safe exact provider thread identity is required')


def snapshot_from_bytes(raw, *, expected_thread_id, expected_cwd, expected_prefix=None):
    """Parse a complete local rollout, preserving every byte in its identity.

    ``expected_prefix`` may be a prior snapshot.  A resume is accepted only if
    its previous bytes still form exactly the same prefix.  The caller should
    take this snapshot while holding the session lease and after the native
    writer has quiesced; a truncated final line is not silently ignored.
    """
    _identity(expected_thread_id)
    if not isinstance(raw, bytes) or not raw or not raw.endswith(b'\n'):
        raise ContextUnavailable('stream_incomplete', 'rollout has no complete final record')
    if expected_prefix is not None:
        count = expected_prefix.get('prefix_bytes')
        previous = expected_prefix.get('prefix_sha256')
        if type(count) is not int or count < 1 or len(raw) < count or digest(raw[:count]) != previous:
            raise ContextUnavailable('session_prefix_mismatch', 'native history changed or shrank')
        if expected_prefix.get('provider_thread_id') != expected_thread_id:
            raise ContextUnavailable('session_owner_mismatch', 'prefix belongs to another thread')
    records, messages, contexts, metadata, unsupported = [], [], [], [], []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise ContextUnavailable('stream_incomplete', f'empty rollout record {number}')
        try:
            record = _json(line)
        except (ValueError, UnicodeError) as exc:
            raise ContextUnavailable('stream_incomplete', f'invalid rollout record {number}') from exc
        if not isinstance(record, dict) or not isinstance(record.get('payload'), dict):
            raise ContextUnavailable('context_unverifiable', f'invalid rollout envelope {number}')
        kind, payload = record.get('type'), record['payload']
        records.append(record)
        if kind in COMPACTION_KINDS or payload.get('type') in COMPACTION_KINDS:
            unsupported.append({'record': number, 'kind': kind, 'payload_kind': payload.get('type')})
        elif kind not in OUTER_KINDS:
            unsupported.append({'record': number, 'kind': kind})
        elif kind == 'session_meta':
            metadata.append(payload)
        elif kind == 'turn_context':
            contexts.append(payload)
        elif kind == 'response_item':
            item_kind = payload.get('type')
            if item_kind not in RESPONSE_KINDS:
                unsupported.append({'record': number, 'kind': item_kind})
            elif item_kind == 'message':
                content = payload.get('content')
                if (payload.get('role') not in {'system', 'developer', 'user', 'assistant'}
                        or not isinstance(content, list)
                        or any(not isinstance(item, dict) or item.get('type') not in {'input_text', 'output_text'}
                               or not isinstance(item.get('text'), str) for item in content)):
                    unsupported.append({'record': number, 'kind': 'non_text_message'})
                else:
                    messages.append({'record': number, 'role': payload['role'],
                        'content': '\n'.join(item['text'] for item in content), 'item_hash': digest(payload)})
    if len(metadata) != 1 or metadata[0].get('id') != expected_thread_id:
        raise ContextUnavailable('session_owner_mismatch', 'rollout session_meta does not match thread')
    wanted_cwd = str(Path(expected_cwd).resolve())
    if (not isinstance(metadata[0].get('cwd'), str) or not metadata[0]['cwd'].strip()
            or str(Path(metadata[0]['cwd']).resolve()) != wanted_cwd):
        raise ContextUnavailable('session_owner_mismatch', 'rollout cwd differs from owned session directory')
    if unsupported:
        raise ContextUnavailable('context_unverifiable', 'unsupported compaction/tool/rollout event: ' +
                                 json.dumps(unsupported, separators=(',', ':')))
    # Count the complete raw snapshot, not only user/assistant text. This is a
    # conservative application-byte accounting that includes context metadata,
    # instructions, schemas, reasoning payloads and audit-only events too.
    return {'contract': CONTRACT, 'provider_thread_id': expected_thread_id, 'cwd': wanted_cwd,
            'prefix_bytes': len(raw), 'prefix_sha256': digest(raw), 'record_count': len(records),
            'messages': messages, 'messages_sha256': digest(messages),
            'context_records_sha256': digest(contexts), 'context_record_count': len(contexts),
            'observable_history_bytes': len(raw), 'provider_context_verified': False,
            'read_authority': 'none; current-request delivery must be certified separately',
            'compaction_supported': False,
            'verified_parent_prefix': ({'prefix_bytes': expected_prefix['prefix_bytes'],
                                        'prefix_sha256': expected_prefix['prefix_sha256']}
                                       if expected_prefix is not None else None)}


def read_native_snapshot(path, *, expected_thread_id, expected_cwd, expected_prefix=None):
    """Read only the exact owned rollout; reject symlinks and unstable reads."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ContextUnavailable('context_unverifiable', 'native rollout is missing or a symlink')
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ContextUnavailable('session_prefix_mismatch', 'native rollout changed during snapshot')
    result = snapshot_from_bytes(raw, expected_thread_id=expected_thread_id, expected_cwd=expected_cwd,
                                 expected_prefix=expected_prefix)
    return {**result, 'native_session_path': str(path.resolve())}


def find_owned_rollout(codex_home, provider_thread_id, expected_cwd):
    """Locate by exact filename suffix before reading any session contents.

    No history scan is performed. Other user's threads are never opened. Both
    common active and archived folders are considered; ambiguity is an error.
    """
    _identity(provider_thread_id)
    home = Path(codex_home).resolve()
    matches = []
    for folder in ('sessions', 'archived_sessions'):
        root = home / folder
        if not root.exists():
            continue
        for candidate in root.rglob(f'rollout-*-{provider_thread_id}.jsonl'):
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(root.resolve()):
                raise ContextUnavailable('session_owner_mismatch', 'native rollout leaves session root')
            matches.append(candidate)
    if len(matches) != 1:
        raise ContextUnavailable('context_unverifiable', f'expected one owned rollout, found {len(matches)}')
    read_native_snapshot(matches[0], expected_thread_id=provider_thread_id, expected_cwd=expected_cwd)
    return matches[0]


def validate_rollout_extension(before, after):
    """Check identities after read_native_snapshot(... expected_prefix=before)."""
    for key in ('contract', 'provider_thread_id', 'cwd'):
        if before.get(key) != after.get(key):
            raise ContextUnavailable('session_owner_mismatch', f'changed {key}')
    if after.get('prefix_bytes', -1) < before.get('prefix_bytes', 0):
        raise ContextUnavailable('session_prefix_mismatch', 'native history shrank')
    if after.get('verified_parent_prefix') != {'prefix_bytes': before['prefix_bytes'],
                                                'prefix_sha256': before['prefix_sha256']}:
        raise ContextUnavailable('session_prefix_mismatch', 'extension was not checked against the prior bytes')
    return {'same_owner': True, 'before_hash': before['prefix_sha256'],
            'after_hash': after['prefix_sha256']}


def build_effective_context(packet, *, session_id, owner, snapshot=None,
                            working_memory_revision=None, max_input_bytes=131072,
                            max_input_tokens=None, output_reserve_tokens=0,
                            token_counter=None, tokenizer_id=None,
                            runtime_overhead_bytes=0, runtime_overhead_tokens=0):
    """Bound the observable native prefix plus the exact next application input.

    A registered tokenizer callback can count *observable* input. Without one,
    UTF-8 bytes are reported as an explicit conservative text-token estimate;
    this is never labelled a measured provider-token count. Snapshot bytes are
    conservatively reserved, including audit-only rows. Hidden backend context
    remains outside this CLI contract and is reported, not invented.
    """
    if not isinstance(packet, (dict, bytes)) or not isinstance(owner, dict) or not owner:
        raise ValueError('An exact packet and trusted owner are required')
    _identity(session_id)
    for value in (max_input_bytes, output_reserve_tokens, runtime_overhead_bytes, runtime_overhead_tokens):
        if type(value) is not int or value < 0:
            raise ValueError('Context capacities must be nonnegative integers')
    if max_input_bytes < 1 or (max_input_tokens is not None and
                              (type(max_input_tokens) is not int or max_input_tokens < 1)):
        raise ValueError('Context capacity must be positive')
    if token_counter is not None and (not callable(token_counter) or not isinstance(tokenizer_id, str) or not tokenizer_id):
        raise ValueError('A token counter needs its registered tokenizer identity')
    raw = packet if isinstance(packet, bytes) else encode(packet)
    try:
        text = raw.decode('utf-8')
        _json(text)
    except (UnicodeError, ValueError) as exc:
        raise ValueError('Packet must be exact UTF-8 JSON bytes') from exc
    history_bytes = 0
    if snapshot is not None:
        if (snapshot.get('contract') != CONTRACT or snapshot.get('compaction_supported') is not False
                or snapshot.get('provider_context_verified') is not False):
            raise ContextUnavailable('context_unverifiable', 'unsupported snapshot contract')
        history_bytes = snapshot.get('observable_history_bytes')
        if type(history_bytes) is not int or history_bytes < 1:
            raise ContextUnavailable('context_unverifiable', 'invalid observable history byte count')
    current_tokens = token_counter(text) if token_counter is not None else len(raw)
    if type(current_tokens) is not int or current_tokens < 0:
        raise ValueError('Tokenizer returned an invalid count')
    # We retain only a manifest here. The complete snapshot was byte-accounted
    # by its reader, so use that conservative bound instead of pretending the
    # visible message extracts are every native token.
    input_bytes = history_bytes + len(raw) + runtime_overhead_bytes
    input_token_bound = history_bytes + current_tokens + runtime_overhead_tokens
    total_token_bound = input_token_bound + output_reserve_tokens
    fitting = input_bytes <= max_input_bytes and (max_input_tokens is None or total_token_bound <= max_input_tokens)
    manifest = {'contract': CONTRACT, 'session_id': session_id, 'owner': copy.deepcopy(owner),
        'submitted_input_hash': digest(raw), 'working_memory_revision': working_memory_revision,
        'native_prefix_hash': snapshot.get('prefix_sha256') if snapshot else None,
        'native_snapshot_hash': digest(snapshot) if snapshot else None,
        'native_thread_id': snapshot.get('provider_thread_id') if snapshot else None,
        'observable_history_bytes': history_bytes, 'new_input_bytes': len(raw),
        'runtime_overhead_bytes': runtime_overhead_bytes, 'input_bytes': input_bytes,
        'observable_input_token_bound': input_token_bound, 'output_reserve_tokens': output_reserve_tokens,
        'total_token_bound': total_token_bound,
        'tokenizer': tokenizer_id if token_counter is not None else 'utf8-bytes-conservative-estimate-v1',
        'history_token_method': 'complete-rollout-bytes-conservative-estimate-v1',
        'runtime_overhead_tokens': runtime_overhead_tokens, 'provider_context_verified': False,
        'retained_read_authority': False,
        'capacity_scope': 'Observable persisted rollout plus complete application request; hidden provider context is not certified'}
    return {'status': 'ready' if fitting else 'blocked_capacity', 'manifest': manifest,
            'effective_context_hash': digest(manifest), 'max_input_bytes': max_input_bytes,
            'max_input_tokens': max_input_tokens,
            'reason': None if fitting else 'Persistent observable history and new input exceed the registered context budget'}


def retained_source_candidates(snapshot, history_pages, *, authorized_source_ids, current_versions):
    """Identify exact historic pages without granting current-call read rights.

    Callers may choose which sources to re-deliver. Only an adapter with a
    separate real-request input attestation can promote retained candidates to
    current-read authority; this CLI reader intentionally cannot do so.
    """
    if snapshot.get('contract') != CONTRACT:
        raise ContextUnavailable('context_unverifiable', 'unknown native snapshot')
    authorized = set(authorized_source_ids)
    result = []
    for page in history_pages:
        eid = page.get('evidence_id')
        if eid not in authorized:
            raise ContextUnavailable('handoff_scope_denied', 'retained source is no longer authorized')
        if current_versions.get(eid) != page.get('source_version'):
            raise ContextUnavailable('retained_source_stale', 'retained source version changed')
        if page.get('source_kind') == 'summary':
            continue
        if digest(page.get('content', '').encode()) != page.get('content_sha256'):
            raise ContextUnavailable('source_integrity_error', 'historic page content changed')
        result.append({**copy.deepcopy(page), 'presentation': 'retained_unverified',
                       'current_read_authority': False})
    return result
