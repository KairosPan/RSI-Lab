"""What one Codex call added to its rollout, and the tokens it is charged.

Codex writes one ``token_usage_record`` per provider request to the thread's
rollout. On a resumed thread the event stream's ``turn.completed`` usage is the
thread's running total, so it counts every earlier turn again; the rows a call
appended after its prefix are what that call spent. A sub-agent writes its rows
to a rollout of its own, found by its exact thread id as an owned rollout is
(no scanning); its rows name the parent thread as ``session_id`` and the
parent's turn as ``root_turn_id``.

The same bytes say what the call did: its tool calls, its compactions, the last
account snapshot and the permission profile of its last turn context.
``settle_call`` turns all this into what the receipt of a call that has ended
says, for the executor and for recovery alike; a turn cut off before its
terminal event is never charged. Nothing here runs Codex or writes anything.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import native as native_tools, transport
from .session_context import THREAD_ID
from .store import digest

COUNTERS = ('input_tokens', 'output_tokens', 'cached_input_tokens')
# The record field that holds each tool call's input text.
TOOL_CALLS = {'custom_tool_call': 'input', 'function_call': 'arguments'}
MAX_TOOL_INPUTS = 32
EXCERPT_CHARS = 200


class LedgerUnavailable(ValueError):
    """A rollout cannot say what a call spent."""


def _identity(thread_id, what):
    if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
        raise LedgerUnavailable(f'{what} is not a safe thread identity')


def _read(path):
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise LedgerUnavailable(f'{path} is missing or a symlink')
        return path.read_bytes()
    except OSError as exc:
        raise LedgerUnavailable(f'{path} cannot be read') from exc


def _records(raw, start=0):
    """The ``(type, payload)`` of every record in ``raw[start:]``, split on ``b'\\n'`` only. Each must be complete
    and a JSON object with an object payload, and ``start`` must fall on a record boundary."""
    if start > len(raw):
        raise LedgerUnavailable('the rollout is shorter than its prefix')
    if start and raw[start - 1:start] != b'\n':
        raise LedgerUnavailable('the prefix does not end on a record boundary')
    tail = raw[start:]
    if tail and not tail.endswith(b'\n'):
        raise LedgerUnavailable('the rollout has no complete final record')
    records = []
    for number, line in enumerate(tail.split(b'\n')[:-1], 1):
        try:
            record = json.loads(line.decode('utf-8'))
        except ValueError as exc:                       # UnicodeDecodeError is a ValueError too
            raise LedgerUnavailable(f'line {number} from byte {start} is not JSON') from exc
        if not isinstance(record, dict) or not isinstance(record.get('payload'), dict):
            raise LedgerUnavailable(f'line {number} from byte {start} is not a record with a payload')
        records.append((record.get('type'), record['payload']))
    return records


def _counters(usage):
    """A usage row's per-request counters; ``cached_input_tokens`` is 0 when absent."""
    counters = ({name: usage.get(name, 0 if name == 'cached_input_tokens' else None) for name in COUNTERS}
                if isinstance(usage, dict) else None)
    if counters is None or any(type(value) is not int or value < 0 for value in counters.values()):
        raise LedgerUnavailable('a usage row has invalid counters')
    return counters


def _tool_input(payload):
    text = payload.get(TOOL_CALLS[payload['type']])
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, sort_keys=True)
    return {'name': payload.get('name'), 'sha256': digest(text.encode('utf-8', 'surrogatepass')),
            'excerpt': text[:EXCERPT_CHARS]}


def _account(limits):
    primary = limits.get('primary') if isinstance(limits.get('primary'), dict) else {}
    credits = limits.get('credits') if isinstance(limits.get('credits'), dict) else {}
    return {'plan_type': limits.get('plan_type'), 'used_percent': primary.get('used_percent'),
            'window_minutes': primary.get('window_minutes'), 'resets_at': primary.get('resets_at'),
            'credits_balance': credits.get('balance'), 'has_credits': credits.get('has_credits'),
            'unlimited': credits.get('unlimited')}


def read_turn(path, *, thread_id, prefix_bytes):
    """What the call that began at byte ``prefix_bytes`` of the rollout at ``path`` added to it.

    ``rows`` are the call's usage rows, each one provider request, and ``requests`` and the counters add them up;
    ``turn_ids`` are their distinct turns. ``children`` are the distinct sub-agents named by the turn's
    ``SubAgentActivity`` items of any kind, as recorded: their ids are checked only when ``read_child`` looks for
    them, which counts a child's rows only under the turns in ``turn_ids``.
    ``tool_calls`` counts ``custom_tool_call`` and ``function_call`` records; ``tool_inputs`` hashes the input of
    the first ``MAX_TOOL_INPUTS`` and keeps its first ``EXCERPT_CHARS`` characters. ``account`` is the last
    ``rate_limits`` snapshot and ``profile`` the ``permission_profile`` of the last ``turn_context``, each
    ``None`` when absent.

    ``LedgerUnavailable`` is raised when the bytes after the prefix are not whole JSON records starting on a
    record boundary, or a usage row belongs to another thread or session, names no turn or has invalid counters.
    """
    _identity(thread_id, 'the session thread')
    if type(prefix_bytes) is not int or prefix_bytes < 0:
        raise LedgerUnavailable('the prefix must be a byte count')
    rows, children, tool_inputs = [], [], []
    tool_calls = compactions = 0
    account = profile = None
    for kind, payload in _records(_read(path), prefix_bytes):
        if kind == 'token_usage_record':
            if payload.get('thread_id') != thread_id or payload.get('session_id') != thread_id:
                raise LedgerUnavailable('a usage row belongs to another thread')
            turn, root = payload.get('turn_id'), payload.get('root_turn_id')
            if not (isinstance(turn, str) and turn and isinstance(root, str) and root):
                raise LedgerUnavailable('a usage row names no turn')
            rows.append({'turn_id': turn, 'root_turn_id': root, **_counters(payload.get('usage'))})
        elif kind == 'compacted':
            compactions += 1
        elif kind == 'turn_context':
            value = payload.get('permission_profile')
            profile = value if isinstance(value, dict) else None
        elif kind == 'response_item' and payload.get('type') in TOOL_CALLS:
            tool_calls += 1
            if len(tool_inputs) < MAX_TOOL_INPUTS:
                tool_inputs.append(_tool_input(payload))
        elif kind == 'event_msg' and payload.get('type') == 'token_count':
            if isinstance(payload.get('rate_limits'), dict):
                account = _account(payload['rate_limits'])
        elif kind == 'event_msg' and payload.get('type') == 'item_completed':
            item = payload.get('item')
            # Any kind: a sub-agent started in an earlier turn may be addressed again in this one.
            if isinstance(item, dict) and item.get('type') == 'SubAgentActivity':
                child = item.get('agent_thread_id')
                if all(known['thread_id'] != child for known in children):
                    children.append({'thread_id': child, 'agent_path': item.get('agent_path')})
    return {'rows': rows, 'turn_ids': list(dict.fromkeys(row['turn_id'] for row in rows)), 'requests': len(rows),
            **{name: sum(row[name] for row in rows) for name in COUNTERS}, 'children': children,
            'tool_calls': tool_calls, 'tool_inputs': tool_inputs, 'tool_inputs_omitted': tool_calls - len(tool_inputs),
            'compactions': compactions, 'account': account, 'profile': profile}


def _rollout_of(codex_home, thread_id):
    """The one rollout of ``thread_id`` (already checked safe), located by its exact file name under the Codex
    home's ``sessions`` or ``archived_sessions``, as an owned rollout is; nothing else is opened.

    ``LedgerUnavailable`` when a match is a symlink, not a file or outside its tree, when the tree cannot be
    listed, and when not exactly one file matches.
    """
    home, matches = Path(codex_home).resolve(), []
    try:
        for folder in ('sessions', 'archived_sessions'):
            root = (home / folder).resolve()
            if not root.is_dir():
                continue
            for candidate in root.rglob(f'rollout-*-{thread_id}.jsonl'):
                if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve().is_relative_to(root):
                    raise LedgerUnavailable('its rollout is a symlink, not a file, or outside the sessions tree')
                matches.append(candidate)
    except OSError as exc:
        raise LedgerUnavailable('the sessions tree cannot be listed') from exc
    if len(matches) != 1:
        raise LedgerUnavailable(f'expected one rollout, found {len(matches)}')
    return matches[0]


def read_child(codex_home, child_thread_id, *, parent_thread_id, root_turn_ids):
    """A sub-agent's usage in the parent's turns ``root_turn_ids``, from its own rollout.

    The rollout is located by its exact file name under ``sessions`` or ``archived_sessions``, as an owned
    rollout is: exactly one non-symlink file must match, and its first ``session_meta`` must name the child and
    this parent. A row counts only when its ``thread_id`` is the child, its ``session_id`` the parent and its
    ``root_turn_id`` one of ``root_turn_ids``; the forked parent history and earlier turns do not.
    """
    _identity(child_thread_id, 'the sub-agent thread')
    _identity(parent_thread_id, 'the parent thread')
    rollout = _rollout_of(codex_home, child_thread_id)
    raw = _read(rollout)
    records = _records(raw)
    meta = next((payload for kind, payload in records if kind == 'session_meta'), None)
    if meta is None or meta.get('id') != child_thread_id or meta.get('parent_thread_id') != parent_thread_id:
        raise LedgerUnavailable('its rollout does not record it as a sub-agent of this thread')
    rows = [_counters(payload.get('usage')) for kind, payload in records
            if kind == 'token_usage_record' and payload.get('thread_id') == child_thread_id
            and payload.get('session_id') == parent_thread_id and payload.get('root_turn_id') in root_turn_ids]
    return {'thread_id': child_thread_id, 'rollout_ref': str(rollout.resolve()), 'rollout_sha256': digest(raw),
            'requests': len(rows), **{name: sum(row[name] for row in rows) for name in COUNTERS}}


def _settled(source, tokens, reason, turn=None, children=()):
    """The settlement: ``tokens`` is what is charged, ``None`` when unknown; ``requests`` and the counters add up
    the usage rows read, the turn's and its readable children's."""
    parts = ([turn] if turn is not None else []) + list(children)
    return {'tokens': tokens, 'source': source, 'reason': reason,
            **{name: sum(part[name] for part in parts) for name in ('requests',) + COUNTERS},
            'children': list(children), 'turn': turn}


def _from_stream(stream_usage, why, turn=None, *, children_possible=False):
    if children_possible:
        return _settled('unknown', None, why + ", and the stream's number leaves out sub-agents", turn)
    counts = ([stream_usage.get('input_tokens'), stream_usage.get('output_tokens')]
              if isinstance(stream_usage, dict) else [None])
    if all(type(count) is int and count >= 0 for count in counts):
        return _settled('stream', sum(counts), why, turn)
    return _settled('unknown', None, why + ', and the stream reported no usage', turn)


def settle(*, codex_home, path, thread_id, prefix_bytes, resumed, stream_usage, children_possible=False,
           finished=True):
    """The tokens one call is charged and where the number comes from.

    When the call added usage rows, ``ledger``: their input plus output tokens and those of every sub-agent it
    started, read through ``read_child``. The stream's input plus output tokens are the turn's own only on a fresh
    thread; on a resumed one they are the thread's running total and are never charged. So without a rollout
    (``path`` is ``None``) or without rows, a fresh thread is charged the stream's number and a resumed one's
    usage is ``unknown``. A rollout or a sub-agent rollout that cannot be read makes the usage ``unknown`` on any
    thread, never zero and never the stream's, with the reason; ``turn`` is then ``None`` when the call's own
    rollout is the one that cannot be read.

    Without a ``path``, the thread's own rollout is first looked for in ``codex_home`` by its exact thread id, as
    ``read_child`` locates a child, when both are given and the id is safe: a rollout the strict context reader
    refused still holds the call's usage rows. Found exactly once, it is the path; otherwise there is none.

    The stream's usage leaves out every sub-agent, so a call that could start any (``children_possible``) is
    never settled from it: where a fresh thread would be, its usage is ``unknown`` instead.

    A turn that did not reach its terminal event (not ``finished``: killed, interrupted or cut off) is never
    charged: a request still in flight when it was stopped, its own or a sub-agent's, wrote no usage row. Its
    usage is ``unknown``, the reason says so first, and the rows read are still added up, as a lower bound.
    """
    spent = _settle(codex_home=codex_home, path=path, thread_id=thread_id, prefix_bytes=prefix_bytes,
                    resumed=resumed, stream_usage=stream_usage, children_possible=children_possible)
    if finished:
        return spent
    reason = 'the turn did not finish, and requests in flight when it was stopped wrote no usage rows'
    return {**spent, 'tokens': None, 'source': 'unknown',
            'reason': reason + ('; ' + spent['reason'] if spent['source'] == 'unknown' else '')}


def _settle(*, codex_home, path, thread_id, prefix_bytes, resumed, stream_usage, children_possible):
    """``settle`` for a turn that reached its terminal event."""
    if path is None and codex_home is not None and isinstance(thread_id, str) and THREAD_ID.fullmatch(thread_id):
        try:
            path = _rollout_of(codex_home, thread_id)
        except LedgerUnavailable:
            pass             # Not found exactly once: the rules run without a rollout.
    if path is None:
        if resumed:
            return _settled('unknown', None, "the resumed thread's rollout could not be read")
        return _from_stream(stream_usage, 'the call has no rollout to read', children_possible=children_possible)
    try:
        turn = read_turn(path, thread_id=thread_id, prefix_bytes=prefix_bytes)
    except LedgerUnavailable as exc:
        return _settled('unknown', None, f'the rollout cannot be read: {exc}')
    if not turn['rows']:
        if resumed:
            return _settled('unknown', None, 'the resumed turn added no usage rows', turn)
        return _from_stream(stream_usage, 'the fresh turn added no usage rows', turn,
                            children_possible=children_possible)
    children, unreadable = [], []
    for child in turn['children']:
        try:
            children.append(read_child(codex_home, child['thread_id'], parent_thread_id=thread_id,
                                       root_turn_ids=turn['turn_ids']))
        except LedgerUnavailable as exc:
            unreadable.append(f'sub-agent {child["thread_id"]!r}: {exc}')
    if unreadable:
        return _settled('unknown', None, '; '.join(unreadable), turn, children)
    spent = _settled('ledger', None, None, turn, children)
    return {**spent, 'tokens': spent['input_tokens'] + spent['output_tokens']}


def _profile_in_force(profile, *, run_root, codex_home):
    """Whether a turn's ``permission_profile`` is the managed, offline one that denies the run root and the Codex home.

    Paths are compared resolved, as the transport resolves the run root and the Codex home it names.
    """
    files = profile.get('file_system') if isinstance(profile, dict) else None
    if (not isinstance(files, dict) or profile.get('type') != 'managed' or profile.get('network') != 'restricted'
            or files.get('type') != 'restricted' or not isinstance(files.get('entries'), list)
            or not isinstance(codex_home, str)):
        return False
    denied = set()
    for entry in files['entries']:
        place = entry.get('path') if isinstance(entry, dict) and entry.get('access') == 'deny' else None
        if (isinstance(place, dict) and place.get('type') == 'path' and isinstance(place.get('path'), str)
                and os.path.isabs(place['path'])):
            try:
                denied.add(Path(place['path']).resolve())
            except (OSError, ValueError, RuntimeError):     # Not a usable path: it denies nothing named here.
                continue
    return {Path(run_root).resolve(), Path(codex_home).resolve()} <= denied


def settle_call(folder, settings, *, session, codex_home, path, thread_id, stream_usage, run_root, finished,
                dispatched=True):
    """What one ended call is charged, and what its receipt says of its tokens, its native tools and its confinement.

    The executor and recovery both settle a call here, so a call gets the same receipt from either. ``folder`` is the
    call's folder, ``settings`` its native settings in normal form and ``session`` its agent session as admitted
    (``None`` for a call without one). The call is settled by ``settle``: ``codex_home``, ``thread_id``,
    ``stream_usage`` and ``finished`` (whether the turn reached its terminal event) are passed on, and ``path`` (the
    call's own rollout as found, else ``None``) when there is a session. On a resumed thread the call's bytes start at
    the session's committed ``native_prefix``; sub-agents switched on mean the stream's number is never charged; a
    turn that did not finish is never charged.

    ``tokens`` is what is charged, ``None`` when unknown, and ``usage_known`` is false then. ``profile_error`` is
    ``'Permission profile was not in force'`` when the terminal is on, the call was ``dispatched`` and its last turn
    did not run under the managed offline profile that denies ``run_root`` and ``codex_home``; else ``None``.
    ``receipt`` holds ``token_source``, ``token_reason`` (only when unknown), ``stream_usage`` and ``ledger`` for every
    call. ``ledger`` adds up the usage rows read, the turn's and its readable children's (``requests`` and the
    counters, a lower bound when the usage is unknown), and names the turns whose rows those are (``turn_ids``, the
    turn's own; empty when its rollout was not read). ``account_snapshot``, the turn's last account snapshot or ``None``, for a ``dispatched`` call whose turn was
    read from the rollout, whatever its settings; and when anything is on, ``native_capabilities``,
    ``native_activity`` (``transport.stream_activity`` of ``folder`` with the turn's tool calls, tool inputs,
    compactions and ``provider_requests``) and ``child_threads``. That activity is recorded only: Codex's own tool
    calls charge no ``tool_calls``.
    """
    resumed = session is not None and bool(session.get('provider_thread_id'))
    prefix = ((session or {}).get('context_manifest') or {}).get('native_prefix') or {}
    spent = settle(codex_home=codex_home, path=path if session is not None else None, thread_id=thread_id,
                   prefix_bytes=prefix.get('prefix_bytes') if resumed else 0, resumed=resumed,
                   stream_usage=stream_usage, children_possible=settings['subagents'] is not None, finished=finished)
    turn = spent['turn'] or {}
    receipt = {'token_source': spent['source'],
               'token_reason': spent['reason'] if spent['source'] == 'unknown' else None,
               'stream_usage': stream_usage, 'ledger': {**{name: spent[name] for name in ('requests',) + COUNTERS},
                                                        'turn_ids': list(turn.get('turn_ids', []))}}
    if dispatched and spent['turn'] is not None:
        receipt['account_snapshot'] = turn['account']
    if native_tools.any_on(settings):
        receipt.update(native_capabilities=settings,
                       native_activity={**transport.stream_activity(folder),
                           **{name: turn.get(name) for name in ('tool_calls', 'tool_inputs', 'tool_inputs_omitted',
                                                                'compactions')},
                           'provider_requests': turn.get('requests')},
                       child_threads=spent['children'])
    confined = (settings['terminal'] is None or not dispatched
                or _profile_in_force(turn.get('profile'), run_root=run_root, codex_home=codex_home))
    return {'tokens': spent['tokens'], 'usage_known': spent['source'] != 'unknown',
            'profile_error': None if confined else 'Permission profile was not in force', 'receipt': receipt}
