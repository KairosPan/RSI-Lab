"""A team run: the Lab leads named local and challenge sub-agents and decides each batch's role.

This module prepares the run's settings only. It imports neither the scorer
nor the host, so it can be checked without either. What a sub-agent returns is
a lead, not evidence; only the Lab requests selection and scoring.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

from rsi_lab import native
from . import harness


ARCHITECTURE = 'pablo_research_pr4_team_v1'
# A native turn's history is much larger than a plain one, so a team run raises the per-request
# context limit. The kernel refuses a request whose history plus request exceeds that limit, so an idle
# session is rebuilt once its history reaches the limit less the largest request the host allows (1048576,
# the top of --max-input-bytes), before its next request could be refused for size. This is the default
# and the largest value --rebuild-history-bytes accepts.
MAX_EFFECTIVE_CONTEXT_BYTES = 4194304
REBUILD_HISTORY_BYTES = MAX_EFFECTIVE_CONTEXT_BYTES - 1048576
MIN_REBUILD_HISTORY_BYTES = 262144
_CLOSING = (' You are a sub-agent of the Lab. You cannot score or select molecules. Return one anchor from the '
            'eligible anchors you were given, a pool of candidate SMILES, the hypothesis, and the result that '
            'would falsify it. Do not predict scores or guess the hidden objective.')
ROLES = {
    'challenge': {'description': 'Proposes a batch that argues against the leading structural series.',
                  'instructions': harness.ROLE_INSTRUCTIONS['challenge'] + _CLOSING},
    'local': {'description': 'Proposes a batch that refines the leading structural series.',
              'instructions': harness.ROLE_INSTRUCTIONS['local'] + _CLOSING},
}
GOAL = (
    'In this team run the Lab leads a team and may start sub-agents with agent_type local, challenge or default. '
    'Sub-agents only propose; what a sub-agent returns is a lead, not evidence. '
    'Only the Lab requests pablo.select_candidates and pablo.evaluate. '
    'For every scoring batch the Lab decides whether to ask Challenge and which proposal to run, and states this '
    'in the decision of its pablo.select_candidates request: challenge_dispatched (whether this Lab call started '
    "or addressed the Challenge sub-agent), challenge_adopted (whether the batch runs Challenge's proposal) and "
    'reason (one line of at most 300 characters). '
    'Obey constraints.adapter_context["pablo.evaluate"].harness for the eligible observed anchors of the chosen '
    'role and for the batch allowance. '
    'When constraints.adapter_context["pablo.evaluate"].harness.next_plan.required_role is challenge, the Lab '
    'must dispatch Challenge and run its proposal as a challenge batch. '
    'Start Challenge with fork_turns none and pass it the eligible anchors, candidate constraints and observed '
    'results it needs.')
# How the not-blind statement names each role whose web search is on or that declares a tool server, in the
# kernel's role order.
ROLE_NAMES = {'lab': 'the Lab', 'meta': 'Meta', 'literature': 'Literature'}
NOT_BLIND_TEMPLATE = ('Web search is on for {}: the hidden objective is protected by instruction only, '
                      'and this run is not a blind run.')
NOT_BLIND = NOT_BLIND_TEMPLATE.format(ROLE_NAMES['lab'])
# A tool server can reach anything, whether Codex starts it from a command or reaches it by URL.
TOOL_SERVERS_TEMPLATE = ('Tool servers are on for {}: the hidden objective is protected by instruction only, '
                         'and this run is not a blind run.')
# Bounds of the search under a readable root.
MAX_DEPTH = 6
MAX_FOLDERS = 20000
# Any entry whose name starts with this, compared case-insensitively: the package, its metadata, links, archives.
PACKAGE = 'guacamol'
# The scorer's source as it lies in a checkout or a frozen source copy: pablo_task/oracle.py.
SCORER_FOLDER, SCORER_FILE = 'pablo_task', 'oracle.py'
# The ledger file the kernel's Store (rsi_lab/store.py, Store.__init__) creates in every run root.
RUN_STORE = 'ledger.sqlite3'


def native_block(block, *, protected):
    """The run config's native_capabilities block made from the operator's file.

    Refuses a file in which ``lab.subagents`` is off or missing, one in which
    any role's ``subagents.roles`` names ``local`` or ``challenge`` (the host
    declares those for the Lab with its own text, and the names are reserved
    for every role), anything the kernel would refuse, and any role's readable
    root or skills folder for which ``exposes_objective`` gives a reason. The
    file's other settings and roles are kept; the given block is not changed.

    A role's skills folder is read and frozen as the kernel freezes it
    (``native.freeze_skills``), without storing anything: the run's store does
    not exist yet. The host stores the files when it creates the store, and
    refuses a folder that changed since.
    """
    if type(block) is not dict:
        raise ValueError('The native capabilities file must hold a JSON object')
    lab = block.get('lab')
    subagents = lab.get('subagents', False) if type(lab) is dict else False
    if subagents is False:
        raise ValueError('A team run needs lab.subagents switched on')
    if subagents is not True and type(subagents) is not dict:
        raise ValueError('native_capabilities.lab.subagents must be true, false or an object')
    own = {} if subagents is True else subagents.get('roles', {})
    if type(own) is not dict:
        raise ValueError('native_capabilities.lab.subagents.roles must be an object')
    for role, entry in block.items():
        switch = entry.get('subagents') if type(entry) is dict else None
        declared = switch.get('roles') if type(switch) is dict else None
        if type(declared) is dict and set(declared) & set(ROLES):
            raise ValueError('The host declares the local and challenge roles itself; '
                             'remove them from ' + str(role) + '.subagents.roles')
    result = copy.deepcopy(block)
    # The default is written out so that the frozen run does not follow a later change of the kernel's default.
    current = {'max_concurrent': native.DEFAULT_MAX_CONCURRENT} if subagents is True else copy.deepcopy(subagents)
    result['lab']['subagents'] = {**current, 'roles': {**copy.deepcopy(own), **copy.deepcopy(ROLES)}}
    result = native.freeze_skills(result)
    by_role = native.capabilities({'native_capabilities': result})
    _check_roots(by_role, protected)
    # Every call gets the skills written into its workspace, where the terminal reads them. The folder is checked
    # here alone: the run holds the copy frozen now, which later changes to the folder do not reach.
    for role, entry in by_role.items():
        if 'skills' in entry:
            reason = exposes_objective(entry['skills']['root'], protected)
            if reason is not None:
                raise ValueError('native_capabilities.' + role + '.skills.root: ' + reason)
    return result


def check_readable_roots(config):
    """Check a team run's readable roots again, as the host does at every start.

    What lies under a readable root can change after preparation, so every
    role's roots are checked against the protected paths frozen in the config
    (``pablo_team_protected_roots``). Raises ValueError with the reason.
    """
    protected = config.get('pablo_team_protected_roots')
    if (type(protected) is not list or not protected
            or any(type(place) is not str or not os.path.isabs(place) for place in protected)):
        raise ValueError('A team run needs pablo_team_protected_roots, the absolute paths frozen at preparation')
    _check_roots(native.capabilities(config), protected)


def exposes_objective(root, protected):
    """A sentence naming why a terminal readable root could hand over the hidden objective, or None.

    ``protected`` are the repository checkout, the frozen source copy, the
    parent and prior-attempt run roots and the new run root. A root is refused
    when it equals, contains or lies inside one of them (by resolved path).
    Places not listed are caught by what the walk finds under the root: an
    entry of any kind whose name starts with ``guacamol`` (case-insensitive),
    the scorer's source ``pablo_task/oracle.py`` of a checkout or source copy,
    or a run's store (``RUN_STORE``). A root itself named like guacamol is
    refused too. The walk does not follow symlinks, goes at most ``MAX_DEPTH``
    levels below the root and visits at most ``MAX_FOLDERS`` folders; a root
    beyond either bound is refused as too large to check, and one that cannot
    be read is refused as well.
    """
    root = Path(root).resolve()
    for place in protected:
        place = Path(place).resolve()
        if _within(root, place):
            return ('The readable root ' + str(root) + (' is ' if _same(root, place) else ' lies inside ')
                    + 'the protected folder ' + str(place) + '.')
        if _within(place, root):
            return 'The readable root ' + str(root) + ' contains the protected folder ' + str(place) + '.'
    if root.name.casefold().startswith(PACKAGE):
        return 'The readable root ' + str(root) + ' is named like the ' + PACKAGE + ' package.'
    pending, visited = [(root, 0)], 0
    while pending:
        folder, depth = pending.pop()
        visited += 1
        if visited > MAX_FOLDERS:
            return ('The readable root ' + str(root) + ' is too large to check: it holds more than '
                    + str(MAX_FOLDERS) + ' folders.')
        try:
            with os.scandir(folder) as found:
                entries = sorted(found, key=lambda entry: entry.name)
        except OSError as exc:
            return 'The readable root ' + str(root) + ' could not be checked: ' + str(exc)
        for entry in entries:
            name = entry.name.casefold()
            if name.startswith(PACKAGE):
                return ('The readable root ' + str(root) + ' holds ' + entry.path + ', named like the '
                        + PACKAGE + ' package.')
            if name == SCORER_FILE and folder.name.casefold() == SCORER_FOLDER:
                return 'The readable root ' + str(root) + " holds the scorer's source " + entry.path + '.'
            if name == RUN_STORE:
                return ('The readable root ' + str(root) + " holds a run's store: " + entry.path
                        + ' in the run root ' + str(folder) + '.')
        below = [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]
        if below and depth == MAX_DEPTH:
            return ('The readable root ' + str(root) + ' is too large to check: it has folders more than '
                    + str(MAX_DEPTH) + ' levels deep.')
        pending.extend((path, depth + 1) for path in below)
    return None


def not_blind(block):
    """The plain statement a run with web search on or a tool server declared for any role carries, or None.

    The web-search sentence (``NOT_BLIND_TEMPLATE``) names the roles whose web
    search is on; the tool-server sentence (``TOOL_SERVERS_TEMPLATE``) those
    that declare a tool server, local or by URL. With both, the web-search
    sentence comes first and one space joins them. The block is read as the
    kernel reads it, so the statement cannot disagree with what the kernel
    switches on; a skills folder in it must already be frozen (``native_block``
    returns it so). The roles are named in the order lab, meta, literature; for
    web search on the Lab alone the sentence is ``NOT_BLIND``.
    """
    settings = native.capabilities({'native_capabilities': block})
    searching = [role for role in native.ROLES if settings[role]['web_search']]
    serving = [role for role in native.ROLES if native.tool_servers(settings[role])]
    sentences = [template.format(_role_names(roles))
                 for template, roles in ((NOT_BLIND_TEMPLATE, searching), (TOOL_SERVERS_TEMPLATE, serving)) if roles]
    return ' '.join(sentences) or None


def preparation(block, *, protected, backstop=harness.LED_DEFAULT_CONFIG['challenge_backstop_batches'],
                rebuild_history_bytes=None):
    """Every setting a team run's config gets from the host, except its research policy and goal text.

    ``backstop`` is the challenge backstop, 1 to 100, or None for off.
    ``rebuild_history_bytes`` is at least ``MIN_REBUILD_HISTORY_BYTES``;
    None is ``REBUILD_HISTORY_BYTES``.
    """
    policy = harness.configuration({'pablo_harness': {**harness.LED_DEFAULT_CONFIG,
                                                      'challenge_backstop_batches': backstop}})
    if rebuild_history_bytes is None:
        rebuild_history_bytes = REBUILD_HISTORY_BYTES
    if type(rebuild_history_bytes) is not int or rebuild_history_bytes < MIN_REBUILD_HISTORY_BYTES:
        raise ValueError('The rebuild history size must be an integer of at least '
                         + str(MIN_REBUILD_HISTORY_BYTES) + ' bytes')
    block = native_block(block, protected=protected)
    # Frozen so that every host start can check the readable roots again against the same places.
    roots = list(dict.fromkeys(str(Path(place).resolve()) for place in protected))
    return {'research_architecture': ARCHITECTURE, 'pablo_harness': policy, 'native_capabilities': block,
            'max_effective_context_bytes': MAX_EFFECTIVE_CONTEXT_BYTES,
            'prospective_rebuild_history_bytes': rebuild_history_bytes, 'pablo_not_blind': not_blind(block),
            'pablo_team_protected_roots': roots}


def lab_roles(config):
    """The names of the sub-agent roles a run config declares for the Lab."""
    return set((native.capabilities(config)['lab']['subagents'] or {}).get('roles', {}))


def session_native(by_role, role):
    """A session's native settings as the kernel sends them with that role's calls: None when all are off.

    ``by_role`` is ``Runtime.native``; ``role`` is the session's role.
    """
    settings = native.validate(by_role[role])
    return None if settings == native.OFF else settings


def _role_names(roles):
    """The roles as a not-blind sentence names them, such as ``the Lab``, ``the Lab and Meta`` or
    ``the Lab, Meta and Literature``."""
    names = [ROLE_NAMES[role] for role in roles]
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]


def _check_roots(by_role, protected):
    """Raise ValueError for the first readable root of any role that ``exposes_objective`` refuses."""
    for role, entry in by_role.items():
        for root in (entry['terminal'] or {}).get('readable_roots', ()):
            reason = exposes_objective(root, protected)
            if reason is not None:
                raise ValueError('native_capabilities.' + role + '.terminal.readable_roots: ' + reason)


def _same(path, folder):
    return path == folder or (path.exists() and folder.exists() and os.path.samefile(path, folder))


def _within(path, folder):
    """Whether ``path`` is ``folder`` or lies inside it, by resolved path or by file identity."""
    if path.is_relative_to(folder):
        return True
    return folder.exists() and any(place.exists() and os.path.samefile(place, folder)
                                   for place in (path, *path.parents))
