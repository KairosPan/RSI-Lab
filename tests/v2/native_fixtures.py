"""Normal-form native settings and synthetic rollout records shared by the native-capability tests.

The rollout builders return complete records shaped as the live probe of
2026-11-17 saw them, to follow the base rollout ``fixture`` of
tests/v2/test_session_context.py, whose ``frame`` they use and re-export.
Those of tool server calls, viewed images and skills lists follow the record
shapes Codex 0.170.0 wrote in the spikes of the round-two design. No Codex or
model process is run.
"""
import base64
import hashlib
import sys

from tests.v2.test_session_context import THREAD, frame

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

# Tool servers in normal form: one started locally, one reached by URL. Every path, URL and variable name is invented;
# the command need not exist for validate, and here() swaps in this interpreter for what goes through capabilities.
CHEM = {'description': 'Fingerprints and similarity for SMILES.', 'command': '/opt/tools/bin/python',
        'args': ['-m', 'chem_server'], 'env': {'CHEM_MODE': 'fast'}, 'env_from': ['CHEM_API_KEY'],
        'tool_timeout_seconds': 300}
PAPERS = {'description': 'Full text of open-access papers.', 'url': 'https://papers.example.org/mcp',
          'headers': {'X-Client': 'rsi-lab'}, 'token_from': 'PAPERS_TOKEN'}
TOOL_SERVERS = {'chem': CHEM, 'papers': PAPERS}
WITH_TOOL_SERVERS = {**SEARCH, 'tool_servers': TOOL_SERVERS}
LOCAL_SERVER_ONLY = {'web_search': False, 'terminal': None, 'subagents': None, 'tool_servers': {'chem': CHEM}}
URL_SERVER_ONLY = {'web_search': False, 'terminal': None, 'subagents': None, 'tool_servers': {'papers': PAPERS}}
VIEW_IMAGE = {**TERMINAL, 'view_image': True}

# A tool server call as the event stream of the calling thread shows it. A refused call has a null result, an error
# object and the status failed.
MCP_ITEM = {'type': 'mcp_tool_call', 'server': 'chem', 'tool': 'lookup', 'arguments': {'query': 'CCO'},
            'result': {'content': [{'type': 'text', 'text': 'similarity 0.42'}], 'structured_content': None},
            'error': None, 'status': 'completed'}
# The same call's result as the rollout's McpToolCall item holds it.
MCP_RESULT = {'content': [{'type': 'text', 'text': 'similarity 0.42'}], 'isError': False}
# A viewed image as the image tool returns it: a PNG of 68 bytes as a base64 data URL.
PNG = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + bytes(60)).decode()

# A skills folder of one skill, plot-check, and the frozen form it gives. The root in WITH_SKILLS is invented and need
# not exist for validate; skill_folder() writes the same skill into a temporary folder for what reads a folder.
SKILL_MD = (b'---\nname: plot-check\ndescription: Checks a plot before it is reported.\n---\n'
            b'Open the plot and check its axes and units.\n')
CHECK_PY = b'print("axes checked")\n'
PLOT_CHECK = {'description': 'Checks a plot before it is reported.',
              'files': {'SKILL.md': hashlib.sha256(SKILL_MD).hexdigest(),
                        'scripts/check.py': hashlib.sha256(CHECK_PY).hexdigest()}}
WITH_SKILLS = {**TERMINAL, 'skills': {'root': '/data/research-skills', 'frozen': {'plot-check': PLOT_CHECK}}}


def here(server):
    """A local tool server whose command is this interpreter, an executable file ``capabilities`` accepts."""
    return {**server, 'command': sys.executable}


def skill_folder(root):
    """``root`` made a skills folder holding plot-check: ``SKILL.md`` and ``scripts/check.py``. Returns ``root``."""
    (root / 'plot-check' / 'scripts').mkdir(parents=True)
    (root / 'plot-check' / 'SKILL.md').write_bytes(SKILL_MD)
    (root / 'plot-check' / 'scripts' / 'check.py').write_bytes(CHECK_PY)
    return root


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


def _item_completed(item, turn, thread):
    """An ``item_completed`` event in ``thread``'s rollout under its own ``turn``, as Codex 0.170.0 writes one."""
    return frame('event_msg', {'type': 'item_completed', 'thread_id': thread, 'turn_id': turn, 'item': item})


def mcp_call(call_id, server, tool, arguments, *, turn, status='completed', result=MCP_RESULT, duration=(0, 439666),
             thread=THREAD):
    """A tool server call's ``item_completed`` event in the calling thread's rollout: an ``McpToolCall`` item whose
    ``id`` is ``call_id``, the call id the server receives, with ``duration`` given as ``(secs, nanos)``. The script
    that made the call is a separate ``tool_call``, such as ``text(await tools.mcp__chem__lookup({query:"CCO"}));``."""
    secs, nanos = duration
    return _item_completed({'type': 'McpToolCall', 'id': call_id, 'server': server, 'tool': tool,
                            'arguments': arguments, 'readOnlyHint': True, 'status': status, 'result': result,
                            'duration': {'secs': secs, 'nanos': nanos}}, turn, thread)


def image_view(call_id, path, *, turn, thread=THREAD):
    """A viewed image's ``item_completed`` event in the viewing thread's rollout: an ``ImageView`` item whose
    ``id`` is ``call_id`` and whose ``path`` is ``file://`` followed by the absolute ``path``."""
    return _item_completed({'type': 'ImageView', 'id': call_id, 'path': 'file://' + path}, turn, thread)


def viewed_image(call_id, path, data_url, *, turn, thread=THREAD):
    """An image viewed through the ``exec`` tool: the ``custom_tool_call`` of a script that views ``path``, its
    ``image_view`` event, then the ``custom_tool_call_output``: the script's text and the image as ``data_url``."""
    script = 'const r = await tools.view_image({path:"' + path + '"}); image(r.image_url);'
    return (frame('response_item', {'type': 'custom_tool_call', 'status': 'completed', 'call_id': call_id,
                                    'name': 'exec', 'input': script})
            + image_view(call_id, path, turn=turn, thread=thread)
            + frame('response_item', {'type': 'custom_tool_call_output', 'call_id': call_id, 'output': [
                {'type': 'input_text', 'text': 'Script completed\n'},
                {'type': 'input_image', 'image_url': data_url, 'detail': 'original'}]}))


def skills_list_text(roots, *skills):
    """The text of the skills list Codex writes for a thread, in the record shape of the round-two design: a line of
    prose under ``## Skills``, the ``roots`` as ``r0``, ``r1``, … in order, then one line per skill, each a ``(name,
    description, file)`` tuple whose ``file`` is under a root label, such as ``r0/plot-check/SKILL.md``."""
    return '\n'.join(['<skills_instructions>', '## Skills',
                      'A skill is a set of local instructions in a SKILL.md file; open the file before using it.',
                      '### Skill roots', *('- `r%d` = `%s`' % (number, root) for number, root in enumerate(roots)),
                      '### Available skills', *('- %s: %s (file: %s)' % skill for skill in skills),
                      '</skills_instructions>'])


def skills_list(roots, *skills):
    """The rollout record of a skills list (``skills_list_text``): a ``developer`` message holding it."""
    return frame('response_item', {'type': 'message', 'role': 'developer', 'content': [
        {'type': 'input_text', 'text': skills_list_text(roots, *skills)}]})


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
