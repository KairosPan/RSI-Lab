"""What one Codex call added to its rollout, and the tokens it is charged.

Codex writes one ``token_usage_record`` per provider request to the thread's
rollout. On a resumed thread the event stream's ``turn.completed`` usage is the
thread's running total, so it counts every earlier turn again; the rows a call
appended after its prefix are what that call spent. A sub-agent writes its rows
to a rollout of its own, found by its exact thread id as an owned rollout is
(no scanning); its rows name the parent thread as ``session_id`` and the
parent's turn as ``root_turn_id``.

The same bytes say what the call did: its tool calls, its tool server calls,
the images it viewed, its compactions, the last account snapshot, the
permission profile of its last turn context and the skills lists Codex wrote.
A sub-agent's own rollout says what it was and did: its role, whether it
started with the parent's history, the model and effort of its turn, its web
searches, tool server calls, viewed images and skills lists. ``settle_call``
turns all this into what the receipt of a call that has ended says, for the
executor and for recovery alike, and checks that Codex showed exactly the
declared skills; a turn cut off before its terminal event is never charged.
Nothing here runs Codex or writes anything.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.parse

from . import native as native_tools, transport
from .session_context import THREAD_ID, TOOL_OUTPUT_KINDS, VIEWED_IMAGE_URL
from .store import digest, encode

COUNTERS = ('input_tokens', 'output_tokens', 'cached_input_tokens')
# The record field that holds each tool call's input text.
TOOL_CALLS = {'custom_tool_call': 'input', 'function_call': 'arguments'}
MAX_TOOL_INPUTS = 32
MAX_SEARCHES = 20
MAX_TOOL_SERVER_CALLS = 32
MAX_IMAGE_PATHS = 32
EXCERPT_CHARS = 200
# What a receipt records of tool server calls and of viewed images, each only for a role with that tool.
TOOL_SERVER_RECORDS = ('tool_server_calls', 'tool_server_calls_omitted')
IMAGE_RECORDS = ('images_viewed',)
# Every viewed image's path, which read_turn and read_child also return for the check of the folders those images
# lie in. No receipt records it.
IMAGE_PATHS = 'image_view_paths'
IMAGES_OUTSIDE = 'Codex showed images outside the folders this role may read: {outside} of {viewed}'
SUB_AGENTS_UNCHECKED = ("Codex's sub-agents could not be checked for images outside the folders this role may read: "
                        '{unchecked} of {named}')
# Codex's skills list of a thread: an input_text part of a developer message, in any position, whose text begins with
# SKILLS_LIST_START and ends with SKILLS_LIST_END. read_turn returns the texts of those the call added
# (SKILLS_LISTS[0]) and of those before it (SKILLS_LISTS[1]); read_child those of the sub-agent's whole rollout
# (SKILLS_LISTS[0]). No receipt records them.
SKILLS_LIST_START, SKILLS_LIST_END = '<skills_instructions>', '</skills_instructions>'
SKILLS_LISTS = ('skills_lists', 'earlier_skills_lists')
SKILL_ROOT = re.compile(r'- `(r\d+)` = `(.+)`')
# The description is greedy, so the last "(file: …)" of a line is the skill's file. A name holds no white space.
SKILL_LINE = re.compile(r'- ([^:\s]+): (.*) \(file: (r\d+)/(.+)\)')
SKILLS_SECTIONS = {'### Skill roots': 'roots', '### Available skills': 'skills'}
SKILLS_TO_NONE = 'Codex showed a skills list to a role without skills: '
SKILLS_NOT_DECLARED = 'Codex showed skills other than the declared ones: '


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


def _loose_records(raw):
    """The ``(type, payload)`` of every line of ``raw`` that is a JSON object with an object payload; any other line
    is passed over. For bytes a call did not add, where what cannot be read only means a list not found."""
    records = []
    for line in raw.split(b'\n'):
        try:
            record = json.loads(line.decode('utf-8'))
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get('payload'), dict):
            records.append((record.get('type'), record['payload']))
    return records


def _skills_lists(records):
    """The texts of the skills lists among ``records``: every ``input_text`` part, in any position, of a
    ``response_item`` ``message`` whose ``role`` is ``developer``, when its text begins with ``SKILLS_LIST_START``.
    Each such part is a list on its own, its text alone. A sub-agent started with a declared role gets the role's
    instructions as the first part of that message and the list as the second; the lead and other sub-agents get the
    list first, then other parts."""
    texts = []
    for kind, payload in records:
        content = payload.get('content')
        if (kind != 'response_item' or payload.get('type') != 'message' or payload.get('role') != 'developer'
                or not isinstance(content, list)):
            continue
        texts += [entry['text'] for entry in content
                  if isinstance(entry, dict) and entry.get('type') == 'input_text'
                  and isinstance(entry.get('text'), str) and entry['text'].startswith(SKILLS_LIST_START)]
    return texts


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


def _text(value):
    return value if isinstance(value, str) else None


def _search_query(item):
    """The query of a web search item, in either shape Codex writes, else ``None``: an ``Extension`` item of kind
    ``web.search``, or a ``WebSearch`` item, whose ``query`` is text."""
    if not isinstance(item, dict) or not isinstance(item.get('query'), str):
        return None
    searched = item.get('type') == 'WebSearch' or (item.get('type') == 'Extension' and item.get('kind') == 'web.search')
    return item['query'] if searched else None


def _duration(value):
    """The seconds of an ``McpToolCall`` item's ``duration``, ``{'secs': int, 'nanos': int}``; else ``None``."""
    secs, nanos = (value.get('secs'), value.get('nanos')) if isinstance(value, dict) else (None, None)
    if type(secs) is not int or type(nanos) is not int or secs < 0 or not 0 <= nanos < 10 ** 9:
        return None
    return secs + nanos / 1e9


def _tool_server_call(item):
    """What a receipt records of one ``McpToolCall`` item: its argument text (the string itself, else its JSON with
    sorted keys) hashed whole and cut to ``EXCERPT_CHARS``, the size of its result and its duration. ``call_id`` is
    the item's ``id``, the ``_meta.callId`` the server received. A field of the wrong type is ``None``."""
    arguments = item.get('arguments')
    text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    result = item.get('result')
    try:
        size = len(encode(result)) if isinstance(result, dict) else None
    except ValueError:                      # A value JSON cannot carry, such as NaN or a lone surrogate.
        size = None
    return {'server': _text(item.get('server')), 'tool': _text(item.get('tool')), 'status': _text(item.get('status')),
            'call_id': _text(item.get('id')), 'arguments_sha256': digest(text.encode('utf-8', 'surrogatepass')),
            'arguments_excerpt': text[:EXCERPT_CHARS], 'result_bytes': size,
            'duration_seconds': _duration(item.get('duration'))}


def _image_bytes(image):
    """The decoded size of a viewed image's base64 data URL, from its length and padding alone; 0 for any other."""
    url = image.get('image_url')
    if not isinstance(url, str) or VIEWED_IMAGE_URL.match(url) is None:
        return 0
    payload = url.split(',', 1)[1]
    return max(0, 3 * len(payload) // 4 - payload[-2:].count('='))


def _tools_and_images(items, outputs):
    """The tool server calls and viewed images of a thread in one call, from its completed ``items`` and its tool
    ``outputs`` that count.

    ``tool_server_calls`` records the first ``MAX_TOOL_SERVER_CALLS`` ``McpToolCall`` items, the rest counted in
    ``tool_server_calls_omitted``. ``images_viewed`` keeps the ``path`` of the first ``MAX_IMAGE_PATHS``
    ``ImageView`` items (``None`` when not text), counts the rest in ``paths_omitted``, and gives the ``count`` of
    ``input_image`` items in the outputs and their decoded ``bytes``. ``IMAGE_PATHS`` holds every path.
    """
    calls = [item for item in items if item.get('type') == 'McpToolCall']
    paths = [_text(item.get('path')) for item in items if item.get('type') == 'ImageView']
    images = [entry for output in outputs if isinstance(output, list) for entry in output
              if isinstance(entry, dict) and entry.get('type') == 'input_image']
    return {'tool_server_calls': [_tool_server_call(item) for item in calls[:MAX_TOOL_SERVER_CALLS]],
            'tool_server_calls_omitted': max(0, len(calls) - MAX_TOOL_SERVER_CALLS),
            'images_viewed': {'paths': paths[:MAX_IMAGE_PATHS], 'paths_omitted': max(0, len(paths) - MAX_IMAGE_PATHS),
                              'count': len(images), 'bytes': sum(_image_bytes(image) for image in images)},
            IMAGE_PATHS: paths}


def _completed_item(kind, payload):
    """The item of an ``item_completed`` event when it is an object, else ``None``."""
    item = payload.get('item') if kind == 'event_msg' and payload.get('type') == 'item_completed' else None
    return item if isinstance(item, dict) else None


def _in_turns(records, turns):
    """The records that lie after a ``turn_context`` of one of ``turns`` and before the next ``turn_context``: a
    sub-agent's records of the turns counted for a call, found by position for those that carry no turn id."""
    inside, kept = False, []
    for kind, payload in records:
        if kind == 'turn_context':
            turn = payload.get('turn_id')
            inside = isinstance(turn, str) and turn in turns
        elif inside:
            kept.append((kind, payload))
    return kept


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
    them, which counts a child's rows only under the turns in ``turn_ids``. Each holds its ``thread_id``, the
    ``agent_path`` of the first item that names it, and ``started_in_call``: whether an item of kind ``started``
    names it, so that this call started it rather than addressing one started earlier. ``model`` and ``effort``
    are that item's ``model`` and ``reasoning_effort`` when they are text, else ``None``.
    ``tool_calls`` counts ``custom_tool_call`` and ``function_call`` records; ``tool_inputs`` hashes the input of
    the first ``MAX_TOOL_INPUTS`` and keeps its first ``EXCERPT_CHARS`` characters. ``account`` is the last
    ``rate_limits`` snapshot and ``profile`` the ``permission_profile`` of the last ``turn_context``, each
    ``None`` when absent. ``tool_server_calls``, ``tool_server_calls_omitted``, ``images_viewed`` and every viewed
    image's path (``IMAGE_PATHS``) are those of ``_tools_and_images``, from the ``item_completed`` events and the
    tool outputs after the prefix. ``skills_lists`` are the texts of the skills lists Codex wrote after the prefix
    and ``earlier_skills_lists`` those within it, read from every line that is a record (``_loose_records``).

    ``LedgerUnavailable`` is raised when the bytes after the prefix are not whole JSON records starting on a
    record boundary, or a usage row belongs to another thread or session, names no turn or has invalid counters.
    """
    _identity(thread_id, 'the session thread')
    if type(prefix_bytes) is not int or prefix_bytes < 0:
        raise LedgerUnavailable('the prefix must be a byte count')
    rows, children, tool_inputs, items, outputs = [], [], [], [], []
    tool_calls = compactions = 0
    account = profile = None
    raw = _read(path)
    records = _records(raw, prefix_bytes)
    for kind, payload in records:
        item = _completed_item(kind, payload)
        if item is not None:
            items.append(item)
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
        elif kind == 'response_item' and payload.get('type') in TOOL_OUTPUT_KINDS:
            outputs.append(payload.get('output'))
        elif kind == 'event_msg' and payload.get('type') == 'token_count':
            if isinstance(payload.get('rate_limits'), dict):
                account = _account(payload['rate_limits'])
        elif item is not None:
            # Any kind: a sub-agent started in an earlier turn may be addressed again in this one.
            if item.get('type') == 'SubAgentActivity':
                child = item.get('agent_thread_id')
                entry = next((known for known in children if known['thread_id'] == child), None)
                if entry is None:
                    entry = {'thread_id': child, 'agent_path': item.get('agent_path'), 'started_in_call': False,
                             'model': None, 'effort': None}
                    children.append(entry)
                if item.get('kind') == 'started' and not entry['started_in_call']:
                    entry.update(started_in_call=True, model=_text(item.get('model')),
                                 effort=_text(item.get('reasoning_effort')))
    return {'rows': rows, 'turn_ids': list(dict.fromkeys(row['turn_id'] for row in rows)), 'requests': len(rows),
            **{name: sum(row[name] for row in rows) for name in COUNTERS}, 'children': children,
            'tool_calls': tool_calls, 'tool_inputs': tool_inputs, 'tool_inputs_omitted': tool_calls - len(tool_inputs),
            'compactions': compactions, 'account': account, 'profile': profile, **_tools_and_images(items, outputs),
            'skills_lists': _skills_lists(records),
            'earlier_skills_lists': _skills_lists(_loose_records(raw[:prefix_bytes]))}


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
    """A sub-agent's usage, model, effort and searches in the parent's turns ``root_turn_ids``, and its role.

    All are read from the sub-agent's own rollout, located by its exact file name under ``sessions`` or
    ``archived_sessions``, as an owned rollout is: exactly one non-symlink file must match, and its first
    ``session_meta`` must name the child and this parent. A row counts only when its ``thread_id`` is the child,
    its ``session_id`` the parent and its ``root_turn_id`` one of ``root_turn_ids``; the forked parent history
    and earlier turns do not.

    ``role`` is the ``agent_role`` of that first ``session_meta`` when it is non-empty text, else ``None`` (a
    sub-agent started without a role has none); ``forked_history`` is whether that record names, as non-empty
    ``forked_from_id``, the thread whose history the sub-agent started with. ``model`` and ``effort`` are those
    of the child's last ``turn_context`` record whose ``turn_id`` is the child's own turn of a counted row, each
    when it is text, else ``None``. ``searches`` are the queries of the child's web searches in this call:
    ``item_completed`` events whose item is an ``Extension`` of kind ``web.search`` or a ``WebSearch``, with a
    text ``query``, and whose ``turn_id`` is the child's own turn of a counted row. The first ``MAX_SEARCHES``
    are kept, each cut to ``EXCERPT_CHARS`` characters, and the rest are counted in ``searches_omitted``.
    ``tool_server_calls``, ``tool_server_calls_omitted``, ``images_viewed`` and every viewed image's path
    (``IMAGE_PATHS``) are those of ``_tools_and_images``, from the ``item_completed`` events whose ``turn_id`` is
    such a turn and from the tool outputs, which carry no turn id, that lie after a ``turn_context`` of such a turn
    and before the next ``turn_context``. ``skills_lists`` are the texts of the skills lists in its whole rollout:
    the list Codex built when it started, any written since, and, when it started with the parent's history, the
    copies of the parent's. None of these makes the rollout unreadable.
    """
    _identity(child_thread_id, 'the sub-agent thread')
    _identity(parent_thread_id, 'the parent thread')
    rollout = _rollout_of(codex_home, child_thread_id)
    raw = _read(rollout)
    records = _records(raw)
    meta = next((payload for kind, payload in records if kind == 'session_meta'), None)
    if meta is None or meta.get('id') != child_thread_id or meta.get('parent_thread_id') != parent_thread_id:
        raise LedgerUnavailable('its rollout does not record it as a sub-agent of this thread')
    counted = [payload for kind, payload in records
               if kind == 'token_usage_record' and payload.get('thread_id') == child_thread_id
               and payload.get('session_id') == parent_thread_id and payload.get('root_turn_id') in root_turn_ids]
    rows = [_counters(payload.get('usage')) for payload in counted]
    turns = {turn for turn in (payload.get('turn_id') for payload in counted) if isinstance(turn, str) and turn}
    searches, items, context = [], [], {}
    for kind, payload in records:
        turn = payload.get('turn_id')
        item = _completed_item(kind, payload)
        if item is not None and isinstance(turn, str) and turn in turns:
            items.append(item)
            query = _search_query(item)
            if query is not None:
                searches.append(query[:EXCERPT_CHARS])
        elif kind == 'turn_context' and isinstance(turn, str) and turn in turns:
            context = payload
    outputs = [payload.get('output') for kind, payload in _in_turns(records, turns)
               if kind == 'response_item' and payload.get('type') in TOOL_OUTPUT_KINDS]
    role, forked = meta.get('agent_role'), meta.get('forked_from_id')
    return {'thread_id': child_thread_id, 'rollout_ref': str(rollout.resolve()), 'rollout_sha256': digest(raw),
            'requests': len(rows), **{name: sum(row[name] for row in rows) for name in COUNTERS},
            'role': role if isinstance(role, str) and role else None,
            'forked_history': isinstance(forked, str) and forked != '',
            'model': _text(context.get('model')), 'effort': _text(context.get('effort')),
            'searches': searches[:MAX_SEARCHES], 'searches_omitted': max(0, len(searches) - MAX_SEARCHES),
            **_tools_and_images(items, outputs), 'skills_lists': _skills_lists(records)}


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
    started or addressed, read through ``read_child``. Each readable sub-agent is listed in ``children`` as the
    turn's entry for it (``read_turn``) joined with what ``read_child`` read; its ``model`` and ``effort`` are
    ``read_child``'s where they are not ``None``, else the turn entry's. The stream's input plus output tokens
    are the turn's own only on a fresh thread; on a resumed one they are the thread's running total and are
    never charged. So without a rollout (``path`` is ``None``) or without rows, a fresh thread is charged the
    stream's number and a resumed one's usage is ``unknown``. A rollout or a sub-agent rollout that cannot be
    read makes the usage ``unknown`` on any thread, never zero and never the stream's, with the reason; ``turn``
    is then ``None`` when the call's own rollout is the one that cannot be read.

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
            own = read_child(codex_home, child['thread_id'], parent_thread_id=thread_id, root_turn_ids=turn['turn_ids'])
        except LedgerUnavailable as exc:
            unreadable.append(f'sub-agent {child["thread_id"]!r}: {exc}')
            continue
        # The sub-agent's own turn says what it ran on; the started item of this call, if any, is the fallback.
        children.append({**child, **own, **{name: child[name] if own[name] is None else own[name]
                                             for name in ('model', 'effort')}})
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


def _resolved(path):
    """``path`` resolved now, symbolic links followed, else ``None``.

    A part that does not exist is kept as written, so a plot deleted after it was viewed still lies where it was. Any
    other failure to look at a part gives ``None``: a folder that cannot be searched, which would otherwise hide a
    link inside it, a loop of links, a part that is not a folder. Python releases without ``os.path.ALLOW_MISSING``
    resolve strictly, so there a part that does not exist gives ``None`` too.
    """
    try:
        return Path(os.path.realpath(path, strict=getattr(os.path, 'ALLOW_MISSING', True)))
    except (OSError, ValueError, RuntimeError):
        return None


def _image_file(path):
    """The local file an ``ImageView`` item's ``path`` names, resolved; ``None`` when it names none.

    Codex writes a ``file://`` URL with an empty host, whose path is percent-decoded; a plain absolute path is taken
    as it is, never decoded, since ``%`` is a character of a file name. Anything else names no local file: another
    scheme, a host, a query or a fragment, a relative path, a value that is not text.
    """
    if not isinstance(path, str):
        return None
    if path.startswith('file://'):
        rest = path[len('file://'):]
        if not rest.startswith('/') or '?' in rest or '#' in rest:
            return None
        try:
            path = os.fsdecode(urllib.parse.unquote_to_bytes(rest))
        except ValueError:                  # Text that is not UTF-8, such as a lone surrogate.
            return None
    return _resolved(path) if path.startswith('/') else None


def _images_outside(paths, *, workspace, roots):
    """How many of the viewed images at ``paths`` lie outside the ``workspace`` and the readable ``roots``.

    Both sides are compared resolved (``_resolved``) as they are when the call is settled, not when the image was
    viewed, so a symbolic link in the workspace to a file elsewhere lies outside. Paths are compared as spelled after
    resolution: one written in another letter case than the workspace lies outside, even on a filesystem that ignores
    case. Without a ``workspace`` every image lies outside; a path that names no local file (``_image_file``) does too.
    Codex's permission profile is what keeps a role from reading elsewhere; this only reports what it sees afterwards.
    """
    places = [] if workspace is None else [place for place in map(_resolved, (workspace, *roots)) if place is not None]
    files = [_image_file(path) for path in paths]
    return sum(not (file is not None and any(file.is_relative_to(place) for place in places)) for file in files)


def parse_skills_list(text):
    """The roots and skills of a skills list Codex wrote: ``{'roots': {label: path}, 'skills': [{'name',
    'description', 'root', 'file'}]}``, the skills in the order listed.

    The text begins with ``SKILLS_LIST_START`` and ends with ``SKILLS_LIST_END``. Between them, a line
    ``### Skill roots`` or ``### Available skills`` starts that section and any other line beginning with ``#`` ends
    one. In the roots section every line beginning with ``- `` must be ``SKILL_ROOT``, each label once; in the
    skills section every such line must be ``SKILL_LINE`` under a label the roots section gave. Both sections must
    be there and the roots section must not be empty. Every other line, such as prose, is ignored.
    ``ValueError`` otherwise.
    """
    if not isinstance(text, str) or not text.startswith(SKILLS_LIST_START) or not text.endswith(SKILLS_LIST_END):
        raise ValueError('a skills list must begin with <skills_instructions> and end with </skills_instructions>')
    roots, skills, sections, section = {}, [], set(), None
    for line in text[len(SKILLS_LIST_START):-len(SKILLS_LIST_END)].split('\n'):
        if line.startswith('#'):
            section = SKILLS_SECTIONS.get(line.rstrip())
            sections.add(section)
        elif section == 'roots' and line.startswith('- '):
            match = SKILL_ROOT.fullmatch(line)
            if match is None or match[1] in roots:
                raise ValueError('a skill root line cannot be read')
            roots[match[1]] = match[2]
        elif section == 'skills' and line.startswith('- '):
            match = SKILL_LINE.fullmatch(line)
            if match is None or match[3] not in roots:
                raise ValueError('a skill line cannot be read')
            skills.append({'name': match[1], 'description': match[2], 'root': match[3], 'file': match[4]})
    if not {'roots', 'skills'} <= sections or not roots:
        raise ValueError('a skills list must give its skill roots and its available skills')
    return {'roots': roots, 'skills': skills}


def compare_skills(lists, *, declared, workspace):
    """How the skills lists ``lists`` (texts) compare with the ``declared`` skill names.

    A skill is ``shown`` when its root lies at the workspace's ``.agents/skills`` (both absolute and compared
    resolved, ``_resolved``), its file is ``<name>/SKILL.md`` and its name is declared and not yet seen in that
    list. Any other skill is ``unexpected``, and its root path as written goes to ``folders``, each folder once; its
    name goes nowhere. ``shown`` holds the names shown by the last list that can be read; ``missing`` the declared
    names, in the order given, that some readable list does not show; ``unreadable`` counts the lists
    ``parse_skills_list`` refuses. Descriptions are not compared: Codex may collapse their white space.
    """
    declared = list(declared)
    home = None if workspace is None else _resolved(Path(workspace) / '.agents' / 'skills')
    shown, folders, absent = [], [], set()
    unexpected = unreadable = 0
    for text in lists:
        try:
            listed = parse_skills_list(text)
        except ValueError:
            unreadable += 1
            continue
        seen = []
        for skill in listed['skills']:
            root = listed['roots'][skill['root']]
            if (skill['name'] in declared and skill['name'] not in seen and skill['file'] == skill['name'] + '/SKILL.md'
                    and home is not None and os.path.isabs(root) and _resolved(root) == home):
                seen.append(skill['name'])
                continue
            unexpected += 1
            if root not in folders:
                folders.append(root)
        shown = seen
        absent.update(set(declared) - set(seen))
    return {'shown': shown, 'unexpected': unexpected, 'folders': folders,
            'missing': [name for name in declared if name in absent], 'unreadable': unreadable}


def _skills_check(settings, turn, children, *, read, resumed, finished, thread_id, workspace):
    """The skills error of a dispatched call, and what was compared for the threads its receipt names.

    The threads checked are the lead's, when the call has one, with the lists Codex wrote after its prefix
    (``None`` when its rollout was not ``read``), and each sub-agent the turn started, with the lists of its whole
    rollout (``None`` when it is not among the readable ``children``: its rollout cannot be read or was never
    looked for). Without declared skills each list must be absent, a sub-agent's copy of a list in the lead's
    prefix (one forked with the lead's history) excepted; what was not read is not checked. With declared skills
    every list is compared (``compare_skills``), copies included, and an unreadable rollout or, in a ``finished``
    call, a thread it started without a list is an error too. Returns ``(error, lead, started)``: the error or
    ``None``, and the comparisons under declared skills of the lead (``None`` when not read) and of each started
    sub-agent read, by thread id.
    """
    declared = list(native_tools.declared_skills(settings))
    threads = ([(thread_id, turn['skills_lists'] if read else None, not resumed, False)]
               if thread_id is not None else [])
    for child in turn.get('children', ()):
        if child['started_in_call']:
            # Compared, not looked up: the turn records a sub-agent's id as written, which need not be text.
            own = next((own for own in children if own['thread_id'] == child['thread_id']), None)
            threads.append((child['thread_id'], None if own is None else own['skills_lists'], True, True))
    findings, lead, started = [], None, {}
    for thread, lists, fresh, is_child in threads:
        parts = []
        if not declared:
            # A sub-agent forked with the lead's history carries a copy of its lists, perhaps from before roles
            # without skills were shown none; only a list of its own counts.
            lists = [text for text in lists or () if not (is_child and text in turn.get('earlier_skills_lists', ()))]
            if lists:
                got = compare_skills(lists, declared=(), workspace=workspace)
                if len(lists) > got['unreadable']:
                    parts.append(f'{got["unexpected"]} skills'
                                 + (f' from {", ".join(got["folders"])}' if got['folders'] else ''))
                if got['unreadable']:
                    parts.append(f'{got["unreadable"]} lists unreadable')
        elif lists is None:
            parts.append('rollout unreadable')
        else:
            got = compare_skills(lists, declared=declared, workspace=workspace)
            if is_child:
                started[thread] = got
            else:
                lead = got
            if got['unexpected']:
                parts.append(f'{got["unexpected"]} unexpected from {", ".join(got["folders"])}')
            if got['missing']:
                parts.append('missing ' + ', '.join(got['missing']))
            if got['unreadable']:
                parts.append(f'{got["unreadable"]} lists unreadable')
            if fresh and finished and not lists:
                parts.append('no list')
        if parts:
            findings.append(f'{thread}: ' + '; '.join(parts))
    error = ((SKILLS_NOT_DECLARED if declared else SKILLS_TO_NONE) + '; '.join(findings)) if findings else None
    return error, lead, started


def _skills_records(compared, *, started):
    """What a receipt records of one thread's comparison (``None`` when its rollout was not read) under declared
    skills: ``skills_shown`` when the call ``started`` the thread, ``None`` when not read; and ``skills_unexpected``,
    the count and folders of the skills not declared, when there are any."""
    return {**({'skills_shown': None if compared is None else compared['shown']} if started else {}),
            **({'skills_unexpected': {'count': compared['unexpected'], 'folders': compared['folders']}}
               if compared is not None and compared['unexpected'] else {})}


def settle_call(folder, settings, *, session, codex_home, path, thread_id, stream_usage, run_root, finished,
                dispatched=True, workspace=None):
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
    ``images_error`` is ``None`` unless image viewing is on. With it on, every image the call viewed, the turn's and
    each readable sub-agent's (every ``ImageView`` path counted for ``images_viewed``, beyond those listed too), must
    lie in the session's ``workspace`` or one of the terminal's readable roots, compared resolved as they are now
    (``_images_outside``); when any does not, it is ``IMAGES_OUTSIDE`` with how many of how many, never a path. And
    every sub-agent the turn names must have been read: one whose rollout could not be read, or that was never looked
    for because the turn added no usage rows, adds ``SUB_AGENTS_UNCHECKED`` with how many of the named were not read,
    after ``'; '`` when both apply. A turn whose own rollout cannot be read names none; with the terminal on, which
    image viewing needs, its ``profile_error`` already makes the call invalid.
    ``skills_error`` is ``None`` for a call never ``dispatched``; else it is what ``_skills_check`` finds in the skills
    lists of the lead's thread and of each sub-agent the turn started, against the skills declared (none for a role
    without skills, everything off included), each skill not declared by count and folder, never by name:
    ``SKILLS_TO_NONE`` for a role without skills, ``SKILLS_NOT_DECLARED`` for one with.
    ``receipt`` holds ``token_source``, ``token_reason`` (only when unknown), ``stream_usage`` and ``ledger`` for every
    call. ``ledger`` adds up the usage rows read, the turn's and its readable children's (``requests`` and the
    counters, a lower bound when the usage is unknown), and names the turns whose rows those are (``turn_ids``, the
    turn's own; empty when its rollout was not read). ``account_snapshot``, the turn's last account snapshot or
    ``None``, is there for a ``dispatched`` call whose turn was read from the rollout, whatever its settings; and
    when anything is on, ``native_capabilities`` (``native.receipt_form`` of the settings: each declared sub-agent
    role by its description and the SHA-256 of its instructions), ``native_activity``
    (``transport.stream_activity`` of ``folder`` with the turn's tool calls, tool inputs, compactions and
    ``provider_requests``) and ``child_threads``, the readable sub-agents as ``settle`` lists them. With tool
    servers declared, ``native_activity`` and each ``child_threads`` entry also hold ``TOOL_SERVER_RECORDS``, and
    with image viewing on ``IMAGE_RECORDS``; other receipts hold neither, and none holds ``IMAGE_PATHS``. With skills
    declared, ``native_activity`` holds ``skills_shown`` in a call that started the lead's thread and each entry of a
    sub-agent the turn started holds its own, with ``skills_unexpected`` beside them when a list held skills not
    declared (``_skills_records``); no receipt holds ``SKILLS_LISTS``. That activity is recorded only: Codex's own
    tool calls charge no ``tool_calls``.
    """
    resumed = session is not None and bool(session.get('provider_thread_id'))
    prefix = ((session or {}).get('context_manifest') or {}).get('native_prefix') or {}
    spent = settle(codex_home=codex_home, path=path if session is not None else None, thread_id=thread_id,
                   prefix_bytes=prefix.get('prefix_bytes') if resumed else 0, resumed=resumed,
                   stream_usage=stream_usage, children_possible=settings['subagents'] is not None, finished=finished)
    turn = spent['turn'] or {}
    viewing, skilled = settings.get('view_image', False), bool(native_tools.declared_skills(settings))
    recorded = (TOOL_SERVER_RECORDS if native_tools.tool_servers(settings) else ()) + (IMAGE_RECORDS if viewing else ())
    # A call never dispatched showed nothing.
    skills_error, lead_skills, child_skills = (_skills_check(
        settings, turn, spent['children'], read=spent['turn'] is not None, resumed=resumed, finished=finished,
        thread_id=thread_id, workspace=workspace) if dispatched else (None, None, {}))
    receipt = {'token_source': spent['source'],
               'token_reason': spent['reason'] if spent['source'] == 'unknown' else None,
               'stream_usage': stream_usage, 'ledger': {**{name: spent[name] for name in ('requests',) + COUNTERS},
                                                        'turn_ids': list(turn.get('turn_ids', []))}}
    if dispatched and spent['turn'] is not None:
        receipt['account_snapshot'] = turn['account']
    if native_tools.any_on(settings):
        unrecorded = set(TOOL_SERVER_RECORDS + IMAGE_RECORDS + (IMAGE_PATHS,) + SKILLS_LISTS) - set(recorded)
        receipt.update(native_capabilities=native_tools.receipt_form(settings),
                       native_activity={**transport.stream_activity(folder),
                           **{name: turn.get(name) for name in ('tool_calls', 'tool_inputs', 'tool_inputs_omitted',
                                                                'compactions')},
                           'provider_requests': turn.get('requests'), **{name: turn.get(name) for name in recorded},
                           **(_skills_records(lead_skills, started=not resumed) if skilled else {})},
                       child_threads=[{**{key: value for key, value in child.items() if key not in unrecorded},
                                       **(_skills_records(child_skills[child['thread_id']], started=True)
                                          if child['thread_id'] in child_skills else {})}
                                      for child in spent['children']])
    confined = (settings['terminal'] is None or not dispatched
                or _profile_in_force(turn.get('profile'), run_root=run_root, codex_home=codex_home))
    images_error = None
    if viewing:
        paths = [*turn.get(IMAGE_PATHS, ()), *(path for child in spent['children'] for path in child[IMAGE_PATHS])]
        roots = (settings['terminal'] or {}).get('readable_roots', ())       # Image viewing needs the terminal.
        outside = _images_outside(paths, workspace=workspace, roots=roots)
        # A sub-agent the turn names whose rollout was not read, or was never looked for, viewed what nobody checked.
        named, read = turn.get('children', []), [child['thread_id'] for child in spent['children']]
        unchecked = sum(child['thread_id'] not in read for child in named)
        images_error = '; '.join(
            ([IMAGES_OUTSIDE.format(outside=outside, viewed=len(paths))] if outside else [])
            + ([SUB_AGENTS_UNCHECKED.format(unchecked=unchecked, named=len(named))] if unchecked else [])) or None
    return {'tokens': spent['tokens'], 'usage_known': spent['source'] != 'unknown',
            'profile_error': None if confined else 'Permission profile was not in force',
            'images_error': images_error, 'skills_error': skills_error, 'receipt': receipt}
