"""Per-role switches for Codex's own tools: web search, a confined terminal and sub-agents.

A run config may carry ``native_capabilities`` keyed by role. Each role's entry
is read into one settings object in normal form::

    {'web_search': bool,
     'terminal': None | {'readable_roots': [absolute directory, ...]},
     'subagents': None | {'max_concurrent': 1..8}}

A run without the block, and a role absent from it, has everything off.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


ROLES = ('lab', 'meta', 'literature')
PROFILE = 'rsi_agent'
OFF = {'web_search': False, 'terminal': None, 'subagents': None}
DEFAULT_MAX_CONCURRENT = 3
MAX_CONCURRENT = 8
# Shared temporary folders the terminal's commands may neither read nor write;
# each is denied when it exists as a directory on this machine.
SHARED_TEMP_FOLDERS = ('/tmp', '/private/tmp', '/var/tmp', '/private/var/tmp')
PREAMBLE = (
    "Return one JSON object matching the supplied response schema as your final message. "
    "You may use the tools available in this session before answering.\n\n"
)


def capabilities(config):
    """One normal-form settings object per role from the run config.

    ``terminal`` and ``subagents`` may be ``false``, ``true`` or an object; a
    missing key is off. Readable roots must be absolute existing directories
    and are stored resolved, each once. Anything else raises ``ValueError``.
    """
    block = config.get('native_capabilities', {})
    if type(block) is not dict or set(block) - set(ROLES):
        raise ValueError('native_capabilities must be an object keyed by lab, meta or literature')
    return {role: _role(role, block.get(role, {})) for role in ROLES}


def validate(settings):
    """A checked copy of one normal-form settings object; ``None`` is everything off.

    Only the shape is checked: readable roots must be absolute, but the
    filesystem is not consulted.
    """
    return dict(OFF) if settings is None else _shape(settings, 'native_capabilities')


def any_on(settings):
    """Whether a normal-form settings object (or ``None``) switches anything on."""
    return validate(settings) != OFF


def stream_items(settings):
    """The event-stream item types one role's settings allow beside ``agent_message`` and ``reasoning``.

    Empty when everything is off, which keeps today's strict reading of the
    stream. ``mcp_tool_call`` is never allowed.
    """
    settings = validate(settings)
    if settings == OFF:
        return frozenset()
    items = {'todo_list', 'error'}
    if settings['web_search']:
        items.add('web_search')
    if settings['terminal'] is not None:
        items |= {'command_execution', 'file_change'}
    if settings['subagents'] is not None:
        items.add('collab_tool_call')
    return frozenset(items)


def rollout_kinds(settings):
    """The rollout record types one role's settings allow beyond today's reader.

    ``response`` holds ``response_item`` types and ``outer`` record types. Both
    are empty when everything is off, which keeps today's strict reading of the
    rollout. Any setting that is on allows a ``compacted`` record.
    """
    settings = validate(settings)
    response, outer = set(), set()
    if settings != OFF:
        outer.add('compacted')
    if settings['web_search'] or settings['terminal'] is not None:
        response |= {'custom_tool_call', 'custom_tool_call_output'}
    if settings['subagents'] is not None:
        response |= {'function_call', 'function_call_output', 'agent_message'}
        outer.add('inter_agent_communication_metadata')
    return {'response': frozenset(response), 'outer': frozenset(outer)}


def command_parts(settings, *, workspace, run_root, codex_home, model, effort):
    """What one role's settings change in the Codex command.

    ``sandbox`` replaces ``--sandbox read-only``: with the terminal on, the
    permission profile confines commands instead. ``flags`` follow
    ``--ignore-user-config``. ``config`` holds the values each passed after its
    own ``-c``. ``enabled`` names the features that are no longer disabled.
    The web search value is set by the caller.

    With the terminal on, the network is closed, commands get only the core
    environment, the shared temporary folders are denied (temporary files go
    to the workspace's ``.tmp``), and nothing written in the workspace becomes
    instructions, config, skills or rules on a later turn: project docs and
    rules are ignored, and the workspace's ``.codex``, ``.agents`` and
    ``AGENTS`` files stay read-only.
    """
    settings = validate(settings)
    sandbox, flags, config, enabled = ['--sandbox', 'read-only'], [], [], set()
    if settings['terminal'] is not None:
        sandbox, flags = [], ['--ignore-rules']
        temporary = os.path.join(workspace, '.tmp')
        entries = [(':minimal', 'read'), (run_root, 'deny'), (codex_home, 'deny')]
        # A folder already denied as the run root or the Codex home is named once.
        entries += [(folder, 'deny') for folder in SHARED_TEMP_FOLDERS
                    if os.path.isdir(folder) and folder not in (run_root, codex_home)]
        entries += [(workspace, 'write')]
        entries += [(os.path.join(workspace, name), 'read')
                    for name in ('.codex', '.agents', 'AGENTS.md', 'AGENTS.override.md')]
        entries += [(root, 'read') for root in settings['terminal']['readable_roots']]
        # zsh writes here-documents under TMPPREFIX, which defaults to /tmp/zsh.
        environment = [('TMPDIR', temporary), ('TMPPREFIX', os.path.join(temporary, 'zsh'))]
        config += ['default_permissions=' + toml_string(PROFILE),
                   'permissions.' + PROFILE + '.filesystem=' + toml_table(entries),
                   'permissions.' + PROFILE + '.network={enabled=false}',
                   'shell_environment_policy.inherit="core"',
                   'shell_environment_policy.set=' + toml_table(environment),
                   'project_doc_max_bytes=0']
        enabled |= {'shell_tool', 'unified_exec'}
    if settings['subagents'] is not None:
        config += ['agents.max_depth=1',
                   'agents.max_concurrent_threads_per_session=' + str(settings['subagents']['max_concurrent']),
                   'agents.default_subagent_model=' + toml_string(model),
                   'agents.default_subagent_reasoning_effort=' + toml_string(effort)]
        enabled |= {'multi_agent'}
    return {'sandbox': sandbox, 'flags': flags, 'config': config, 'enabled': enabled}


def toml_string(text):
    """A TOML basic string holding ``text``.

    A JSON string is one, except that TOML also requires DEL to be escaped.
    Anything but text is refused: ``None`` would otherwise become the bare key ``null``.
    """
    if not isinstance(text, str):
        raise ValueError('A TOML string must be made from text')
    return json.dumps(text, ensure_ascii=False).replace('\x7f', '\\u007f')


def toml_table(pairs):
    """A TOML inline table of string keys and string values, in the given order."""
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    if repeated:
        raise ValueError('A TOML table cannot hold a repeated key: ' + ', '.join(repeated))
    return '{' + ','.join(toml_string(key) + '=' + toml_string(value) for key, value in pairs) + '}'


def default_codex_home():
    """The Codex home a call uses unless its session names another: ``CODEX_HOME``, else ``~/.codex``."""
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def check_roots(by_role, *, run_root, codex_home):
    """Refuse a readable root equal to or inside the run root or the Codex home.

    There the root's read entry would be more specific than the denial of
    those directories and would override it. A root that merely contains them
    is allowed: the denial covers what lies beneath.

    A root that is itself one of the existing shared temporary folders is
    refused too: it would contradict that folder's denial. A root inside one
    is allowed, as its more specific entry.
    """
    denied = (('the run root', Path(run_root).resolve()), ('the Codex home', Path(codex_home).resolve()))
    shared = [Path(folder) for folder in SHARED_TEMP_FOLDERS if os.path.isdir(folder)]
    for role, settings in by_role.items():
        for root in (settings['terminal'] or {}).get('readable_roots', ()):
            resolved = Path(root).resolve()
            for name, folder in denied:
                if _inside(resolved, folder):
                    raise ValueError('native_capabilities.' + role + '.terminal.readable_roots entries may not be '
                                     'or lie inside ' + name + ': ' + root)
            if any(resolved == folder.resolve() or (resolved.exists() and os.path.samefile(resolved, folder))
                   for folder in shared):
                raise ValueError('native_capabilities.' + role + '.terminal.readable_roots entries may not be '
                                 'a shared temporary folder, which the terminal is denied: ' + root)


def _inside(path, folder):
    """Whether ``path`` is ``folder`` or lies inside it, both resolved.

    Decided by file identity: ``path`` or one of its ancestors is the same file
    as ``folder``. That catches spellings the text of a resolved path does not,
    such as another letter case on a case-insensitive volume. The text
    comparison remains for paths that do not exist.
    """
    if path.is_relative_to(folder):
        return True
    return folder.exists() and any(place.exists() and os.path.samefile(place, folder)
                                   for place in (path, *path.parents))


def _role(role, entry):
    where = 'native_capabilities.' + role
    if type(entry) is not dict or set(entry) - set(OFF):
        raise ValueError(where + ' may only set web_search, terminal and subagents')
    settings = _shape({
        'web_search': entry.get('web_search', False),
        'terminal': _switch(where + '.terminal', entry.get('terminal', False), {'readable_roots': []}),
        'subagents': _switch(where + '.subagents', entry.get('subagents', False),
                             {'max_concurrent': DEFAULT_MAX_CONCURRENT})}, where)
    if settings['terminal'] is not None:
        roots = settings['terminal']['readable_roots']
        for root in roots:
            if not Path(root).is_dir():
                raise ValueError(where + '.terminal.readable_roots entries must be existing directories: ' + root)
        # Resolved spellings of one directory collapse to its first place in the list.
        settings['terminal']['readable_roots'] = list(dict.fromkeys(str(Path(root).resolve()) for root in roots))
    return settings


def _switch(where, value, default):
    """The config form of one capability in normal form: false is off, true is the default."""
    if value is False:
        return None
    if value is True:
        return default
    if type(value) is not dict:
        raise ValueError(where + ' must be true, false or an object')
    return {**default, **value}


def _shape(settings, where):
    if type(settings) is not dict or set(settings) != set(OFF):
        raise ValueError(where + ' must hold exactly web_search, terminal and subagents')
    web_search, terminal, subagents = settings['web_search'], settings['terminal'], settings['subagents']
    if type(web_search) is not bool:
        raise ValueError(where + '.web_search must be true or false')
    if terminal is not None:
        if (type(terminal) is not dict or set(terminal) != {'readable_roots'}
                or type(terminal['readable_roots']) is not list
                or any(type(root) is not str or not os.path.isabs(root) for root in terminal['readable_roots'])):
            raise ValueError(where + '.terminal may only hold readable_roots, a list of absolute paths')
        terminal = {'readable_roots': list(terminal['readable_roots'])}
    if subagents is not None:
        if (type(subagents) is not dict or set(subagents) != {'max_concurrent'}
                or type(subagents['max_concurrent']) is not int
                or not 1 <= subagents['max_concurrent'] <= MAX_CONCURRENT):
            raise ValueError(where + '.subagents may only hold max_concurrent, an integer from 1 to '
                             + str(MAX_CONCURRENT))
        subagents = {'max_concurrent': subagents['max_concurrent']}
    return {'web_search': web_search, 'terminal': terminal, 'subagents': subagents}
