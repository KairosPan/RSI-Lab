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
# context limit and rebuilds an idle session at the same size, before its next request could be refused.
MAX_EFFECTIVE_CONTEXT_BYTES = 4194304
REBUILD_HISTORY_BYTES = 4194304
MIN_REBUILD_HISTORY_BYTES = 262144
# What the host sets in a team run's config; a plain run prepared from a team parent drops them.
RUN_SETTINGS = ('native_capabilities', 'pablo_not_blind', 'max_effective_context_bytes',
                'prospective_rebuild_history_bytes')
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
    'When constraints.adapter_context["pablo.evaluate"].harness.next_plan.required_role is challenge, the Lab '
    'must dispatch Challenge and run its proposal as a challenge batch. '
    'Start Challenge with fork_turns none and pass it the eligible anchors, candidate constraints and observed '
    'results it needs.')
NOT_BLIND = ('Web search is on for the Lab: the hidden objective is protected by instruction only, '
             'and this run is not a blind run.')
# Bounds of the search for the scorer's package under a readable root.
MAX_DEPTH = 6
MAX_FOLDERS = 20000
PACKAGE = 'guacamol'


def native_block(block, *, protected):
    """The run config's native_capabilities block made from the operator's file.

    Refuses a file in which ``lab.subagents`` is off or missing, one whose
    ``lab.subagents.roles`` names ``local`` or ``challenge`` (the host declares
    those with its own text), anything the kernel would refuse, and any role's
    readable root for which ``exposes_objective`` gives a reason. The file's
    other settings and roles are kept; the given block is not changed.
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
    if set(own) & set(ROLES):
        raise ValueError('The host declares the local and challenge roles itself; '
                         'remove them from lab.subagents.roles')
    result = copy.deepcopy(block)
    # The default is written out so that the frozen run does not follow a later change of the kernel's default.
    current = {'max_concurrent': native.DEFAULT_MAX_CONCURRENT} if subagents is True else copy.deepcopy(subagents)
    result['lab']['subagents'] = {**current, 'roles': {**copy.deepcopy(own), **copy.deepcopy(ROLES)}}
    settings = native.capabilities({'native_capabilities': result})
    for role, entry in settings.items():
        for root in (entry['terminal'] or {}).get('readable_roots', ()):
            reason = exposes_objective(root, protected)
            if reason is not None:
                raise ValueError('native_capabilities.' + role + '.terminal.readable_roots: ' + reason)
    return result


def exposes_objective(root, protected):
    """A sentence naming why a terminal readable root could hand over the hidden objective, or None.

    ``protected`` are the repository checkout, the frozen source copy and each
    parent run root. A root is refused when it equals, contains or lies inside
    one of them (by resolved path), or when a folder named guacamol holding
    ``__init__.py`` is found under it. The search does not follow symlinks,
    goes at most ``MAX_DEPTH`` levels below the root and visits at most
    ``MAX_FOLDERS`` folders; a root beyond either bound is refused as too large
    to check, and one that cannot be read is refused as well.
    """
    root = Path(root).resolve()
    for place in protected:
        place = Path(place).resolve()
        if _within(root, place):
            return ('The readable root ' + str(root) + (' is ' if _same(root, place) else ' lies inside ')
                    + 'the protected folder ' + str(place) + '.')
        if _within(place, root):
            return 'The readable root ' + str(root) + ' contains the protected folder ' + str(place) + '.'
    pending, visited = [(root, 0)], 0
    while pending:
        folder, depth = pending.pop()
        visited += 1
        if visited > MAX_FOLDERS:
            return ('The readable root ' + str(root) + ' is too large to check: it holds more than '
                    + str(MAX_FOLDERS) + ' folders.')
        try:
            with os.scandir(folder) as found:
                entries = list(found)
        except OSError as exc:
            return 'The readable root ' + str(root) + ' could not be checked: ' + str(exc)
        if folder.name == PACKAGE and any(entry.name == '__init__.py' for entry in entries):
            return 'The readable root ' + str(root) + ' holds a ' + PACKAGE + ' package at ' + str(folder) + '.'
        below = [Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)]
        if below and depth == MAX_DEPTH:
            return ('The readable root ' + str(root) + ' is too large to check: it has folders more than '
                    + str(MAX_DEPTH) + ' levels deep.')
        pending.extend((path, depth + 1) for path in below)
    return None


def not_blind(block):
    """The plain statement a run with web search on for the Lab carries, or None."""
    lab = block.get('lab') if type(block) is dict else None
    return NOT_BLIND if type(lab) is dict and lab.get('web_search') is True else None


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
    return {'research_architecture': ARCHITECTURE, 'pablo_harness': policy, 'native_capabilities': block,
            'max_effective_context_bytes': MAX_EFFECTIVE_CONTEXT_BYTES,
            'prospective_rebuild_history_bytes': rebuild_history_bytes, 'pablo_not_blind': not_blind(block)}


def lab_roles(config):
    """The names of the sub-agent roles a run config declares for the Lab."""
    return set((native.capabilities(config)['lab']['subagents'] or {}).get('roles', {}))


def session_native(by_role, role):
    """A session's native settings as the kernel sends them with that role's calls: None when all are off.

    ``by_role`` is ``Runtime.native``; ``role`` is the session's role.
    """
    settings = native.validate(by_role[role])
    return None if settings == native.OFF else settings


def _same(path, folder):
    return path == folder or (path.exists() and folder.exists() and os.path.samefile(path, folder))


def _within(path, folder):
    """Whether ``path`` is ``folder`` or lies inside it, by resolved path or by file identity."""
    if path.is_relative_to(folder):
        return True
    return folder.exists() and any(place.exists() and os.path.samefile(place, folder)
                                   for place in (path, *path.parents))
