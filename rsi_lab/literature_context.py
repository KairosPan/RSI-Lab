"""Owner-scoped scheduling context for Lab's literature decisions.

Task/result pointers describe orchestration state. They do not deliver paper
contents or grant source-citation authority. No benchmark labels belong here.
"""
from __future__ import annotations

from .store import digest

MAX_TASKS = 16
LITERATURE_DECISION_POLICY_VERSION = 'lab_literature_decision_v1'
LITERATURE_DELEGATION_VERSION = 'literature_research_brief_v2'


def literature_tool_catalog(capabilities):
    """Loaded, admitted tools available for a controller-owned permission snapshot.

    An accepted artifact without an executable adapter descriptor is not a tool.
    Preserve complete rows so the digest binds versions, providers and contracts.
    """
    return {cid: row for cid, row in sorted(capabilities.items())
            if cid.startswith('literature.') and row.get('status') == 'accepted'
            and isinstance(row.get('descriptor'), dict) and row['descriptor']}


def build_literature_context(store, run_id, session, capabilities):
    catalog = literature_tool_catalog(capabilities)
    # Sharing a capability namespace does not opt a legacy adapter into a new
    # Lab response contract. Only an explicitly registered policy version does.
    if not any(row['descriptor'].get('manifest', {}).get(
            'literature_decision_policy') == LITERATURE_DECISION_POLICY_VERSION
            for row in catalog.values()):
        return None
    owned = [task for task in store.list(run_id, 'registration', kind='literature_task')
             if task.get('parent_owner_id') == session['owner_id']
             and task.get('branch_id') == session['branch_id']]
    owned.sort(key=lambda task: (task['status'] != 'active', -task['updated_seq']))
    effects = store.list(run_id, 'effect')
    tasks = []
    for task in owned[:MAX_TASKS]:
        related = [effect for effect in effects
                   if effect['metadata'].get('literature_task_id') == task['id']]
        remaining = {resource: max(0, task['max_' + resource] - sum(
            effect.get('actual_costs', {}).get(resource, 0)
            + effect.get('held_reservations', {}).get(resource, 0) for effect in related))
            for resource in ('model_calls', 'tool_calls')}
        tasks.append({'task_id': task['id'], 'status': task['status'],
            'question': task['question'][:1200], 'scope': task['scope'][:1600],
            'intended_use': task['intended_use'][:600],
            'description_truncated': any(len(task[key]) > limit for key, limit in
                (('question', 1200), ('scope', 1600), ('intended_use', 600))),
            'result_evidence_ids': task.get('result_ids', []), 'remaining': remaining})
    allowance = {row['id']: max(0, row['limit'] - row['spent'] - row['reserved'])
                 for row in store.list(run_id, 'budget')}
    return {'version': LITERATURE_DECISION_POLICY_VERSION,
        'delegation_version': LITERATURE_DELEGATION_VERSION,
        'available': bool(catalog), 'tool_selection': 'literature',
        'available_tool_count': len(catalog), 'tool_catalog_sha256': digest(catalog),
        'owned_tasks': tasks,
        'owned_task_count': len(owned), 'omitted_task_count': max(0, len(owned) - MAX_TASKS),
        'run_allowance': allowance, 'allowance_timing': 'before_this_model_turn',
        'new_task_model_call_ceiling': max(0, allowance.get('model_calls', 0) - 2),
        'allowance_explanation': 'Current Lab turn and a later parent review each need one model call; '
            'all model/tool effects still undergo actual global and child-budget admission.',
        'authority': 'Scheduling context only. Result IDs and task descriptions are pointers, '
            'not original-source delivery. Check current delivered evidence for scientific claims.'}
