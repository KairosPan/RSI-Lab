"""Normal-form native settings and synthetic rollout records shared by the native-capability tests.

The rollout builders return complete records shaped as the live probe of
2026-11-17 saw them, to follow the base rollout ``fixture`` of
tests/v2/test_session_context.py, whose ``frame`` they use and re-export.
No Codex or model process is run.
"""
from tests.v2.test_session_context import frame

SEARCH = {'web_search': True, 'terminal': None, 'subagents': None}
TERMINAL = {'web_search': False, 'terminal': {'readable_roots': []}, 'subagents': None}
SUBAGENTS = {'web_search': False, 'terminal': None, 'subagents': {'max_concurrent': 3}}
TERMINAL_AND_SEARCH = {'web_search': True, 'terminal': {'readable_roots': []}, 'subagents': None}
TERMINAL_AND_SUBAGENTS = {'web_search': False, 'terminal': {'readable_roots': []}, 'subagents': {'max_concurrent': 3}}

# Named sub-agent roles in normal form, in name order. The challenge role's text carries what TOML and the
# command line must pass through unchanged: quotes, a backslash, '=', '#' and a line break.
R = {'description': 'Advances the leading series.', 'instructions': 'Refine.'}
ROLES = {'challenge': {'description': 'Argues against the "leading" series.',
                       'instructions': 'Line one.\nSay "no" to C:\\x = y # z.'},
         'local': R}
PLAIN = {'web_search': False, 'terminal': None, 'subagents': {'max_concurrent': 3}}
WITH_ROLES = {'web_search': False, 'terminal': None, 'subagents': {'max_concurrent': 3, 'roles': ROLES}}


def PROFILE(run_root, codex_home, workspace):
    """The ``permission_profile`` a terminal turn's ``turn_context`` carries: the run root and the Codex home
    denied, the workspace writable, the network closed."""
    def entry(path, access):
        return {'path': {'type': 'path', 'path': str(path)}, 'access': access}
    return {'type': 'managed', 'network': 'restricted',
            'file_system': {'type': 'restricted', 'entries': [entry(run_root, 'deny'), entry(codex_home, 'deny'),
                                                              entry(workspace, 'write')]}}


def usage_row(thread, turn, root, inp, out, session=None):
    """One provider request's ``token_usage_record``. In a child's rows ``session`` is the parent thread and
    ``root`` the parent's turn; the request, turn and thread counters hold the same numbers."""
    counters = {'input_tokens': inp, 'cached_input_tokens': 0, 'cache_write_input_tokens': 0,
                'output_tokens': out, 'reasoning_output_tokens': 0, 'total_tokens': inp + out}
    return frame('token_usage_record', {
        'thread_id': thread, 'session_id': thread if session is None else session, 'turn_id': turn,
        'root_turn_id': root, 'response_id': 'response-%s-%d-%d' % (turn, inp, out),
        'usage': counters, 'turn_token_usage': dict(counters), 'thread_token_usage': dict(counters)})


def tool_call(call_id, script):
    """A ``custom_tool_call`` of the ``exec`` tool running ``script``, then its output, a list of text items."""
    return (frame('response_item', {'type': 'custom_tool_call', 'status': 'completed', 'call_id': call_id,
                                    'name': 'exec', 'input': script})
            + frame('response_item', {'type': 'custom_tool_call_output', 'call_id': call_id,
                                      'output': [{'type': 'input_text', 'text': 'done'}]}))


def collab(call_id, name, output):
    """A collaboration ``function_call`` named ``name`` (``spawn_agent``, ``wait_agent``, ``interrupt_agent``),
    then its ``function_call_output``, whose ``output`` is a string."""
    return (frame('response_item', {'type': 'function_call', 'namespace': 'collaboration', 'name': name,
                                    'arguments': '{}', 'call_id': call_id})
            + frame('response_item', {'type': 'function_call_output', 'call_id': call_id, 'output': output}))


def sub_agent_started(child, path, *, model=None, effort=None):
    """The ``item_completed`` event a spawn writes to the parent's rollout, naming the model and effort used."""
    return sub_agent_activity(child, path, 'started', model=model, effort=effort)


def sub_agent_activity(child, path, kind, *, model=None, effort=None):
    """An ``item_completed`` event of a sub-agent's activity in the parent's rollout; the probe saw the kinds
    ``started``, ``interacted``, ``interrupted`` and ``completed``. ``model`` and ``reasoning_effort`` are
    ``null`` unless given, as the probe saw them on every kind but ``started``."""
    return frame('event_msg', {'type': 'item_completed', 'item': {
        'type': 'SubAgentActivity', 'kind': kind, 'agent_thread_id': child, 'agent_path': path,
        'model': model, 'reasoning_effort': effort}})


def sub_agent_meta(child, parent, *, agent_path='/root/a', role=None, forked=False, cwd='/workspace'):
    """A sub-agent's first ``session_meta``: the child, its parent, its path and nickname, at the top and under
    ``source.subagent.thread_spawn``. ``agent_role`` is there, in both places, only when ``role`` is not ``None``;
    any value is written as given. A sub-agent started with the parent's history (``forked``) also names the
    parent as ``forked_from_id``, with ``subagent_history_start_ordinal``."""
    named = {'agent_path': agent_path, 'agent_nickname': 'Fixture'}
    if role is not None:
        named['agent_role'] = role
    payload = {'id': child, 'parent_thread_id': parent, 'cwd': str(cwd), **named,
               'source': {'subagent': {'thread_spawn': {'parent_thread_id': parent, 'depth': 1, **named}}}}
    if forked:
        payload.update(forked_from_id=parent, subagent_history_start_ordinal=1)
    return frame('session_meta', payload)


def web_search(query, turn, *, shape='Extension'):
    """A web search's ``item_completed`` event in the searching thread's rollout, under its own ``turn``: the
    ``Extension`` item of kind ``web.search`` the probe saw, or the older ``WebSearch`` item."""
    action = {'type': 'search', 'query': query}
    if shape == 'Extension':
        item = {'type': 'Extension', 'kind': 'web.search', 'query': query, 'action': action, 'results': []}
    else:
        item = {'type': 'WebSearch', 'id': 'search-' + turn, 'query': query, 'action': action}
    return frame('event_msg', {'type': 'item_completed', 'turn_id': turn, 'item': item})


def agent_message(author, recipient, text):
    """A message between agents: a ``response_item`` of type ``agent_message`` whose content is, as observed, an
    ``input_text`` item followed by an ``encrypted_content`` item holding an opaque string."""
    return frame('response_item', {'type': 'agent_message', 'author': author, 'recipient': recipient,
                                   'content': [{'type': 'input_text', 'text': text},
                                               {'type': 'encrypted_content', 'encrypted_content': 'opaque-payload'}]})


def compacted(history):
    """A compaction: the outer ``compacted`` record whose ``replacement_history`` is ``history``."""
    return frame('compacted', {'message': '', 'replacement_history': history, 'latest_token_usage_record': None})


def account(balance, used_percent, plan_type='pro'):
    """A ``token_count`` event carrying the account snapshot: plan, window used and credit balance."""
    return frame('event_msg', {'type': 'token_count', 'info': None, 'rate_limits': {
        'plan_type': plan_type,
        'primary': {'used_percent': used_percent, 'window_minutes': 300, 'resets_at': 946702800},
        'credits': {'has_credits': True, 'unlimited': False, 'balance': balance}}})


SANDBOX_POLICY = {'type': 'workspace-write', 'network_access': False, 'exclude_tmpdir_env_var': True,
                  'exclude_slash_tmp': True}


def turn_context(turn, profile=None, *, cwd='/workspace', model='test-model', effort='low'):
    """A ``turn_context`` record of ``turn`` run in ``cwd`` on ``model`` and ``effort`` under the probe's
    ``sandbox_policy``; it carries ``permission_profile`` only when ``profile`` is given."""
    payload = {'turn_id': turn, 'root_turn_id': turn, 'cwd': str(cwd), 'model': model, 'effort': effort,
               'approval_policy': 'never', 'sandbox_policy': dict(SANDBOX_POLICY)}
    if profile is not None:
        payload['permission_profile'] = profile
    return frame('turn_context', payload)


def child_rollout(child, parent, turn_rows, *, agent_path='/root/a', cwd='/workspace', role=None, forked=True):
    """A sub-agent's own rollout: its ``session_meta`` (``sub_agent_meta``), spawned at ``agent_path`` in ``cwd``
    with ``role``, then, when the history is ``forked``, the parent's, then one usage row per ``(turn_id,
    root_turn_id, input_tokens, output_tokens)`` in ``turn_rows``."""
    own = sub_agent_meta(child, parent, agent_path=agent_path, role=role, forked=forked, cwd=cwd)
    history = frame('session_meta', {'id': parent, 'cwd': str(cwd)}) if forked else b''
    return own + history + b''.join(
        usage_row(child, turn, root, inp, out, session=parent) for turn, root, inp, out in turn_rows)
