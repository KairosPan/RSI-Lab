"""What a cooperative driver call leaves for an operator to attend to.

The report only reads the ledger. It separates a clean finish from a run that
has stopped with an open obligation nobody is scheduled to discharge.
"""
from __future__ import annotations

import json

from .controller import identity

UNSETTLED = {'prepared', 'dispatching', 'unknown'}
PARKED = {'waiting', 'disposition_due', 'disposed'}
STOPPED_PROPOSALS = {'rejected', 'blocked_stale', 'blocked_action'}
CAPACITY_BLOCKS = {'blocked_capacity', 'packet_unrepresentable'}


def build_report(runtime, *, skipped=()):
    """Settlement, Work and attention status of ``runtime``'s run; ``skipped`` comes from the driver."""
    store, run_id = runtime.store, runtime.run_id
    effects = store.list(run_id, 'effect')
    works = store.list(run_id, 'work')
    waits = store.list(run_id, 'wait')
    if any(e['status'] in {'dispatching', 'unknown'} or e.get('accounting_unknown') for e in effects):
        settlement = 'blocked_external'
    elif any(row['reason'].startswith('Budget admission denied') for row in skipped):
        settlement = 'resource_exhausted'
    else:
        settlement = 'settled'
    if any(e['status'] in UNSETTLED for e in effects):
        work_status = 'unresolved_effects'
    elif runtime.controller.ready(run_id):
        work_status = 'pending_queued'
    elif any(w['status'] in PARKED for w in waits):
        work_status = 'parked_dependencies'
    else:
        work_status = 'empty'
    return {'settlement_status': settlement, 'work_status': work_status,
            'attention': {'stalled_issues': _stalled_issues(store, run_id, works, waits, effects),
                          'stopped_lab': _stopped_lab(store, run_id, works),
                          'sessions_requiring_recovery': _sessions_requiring_recovery(store, run_id),
                          'capacity_blocked_work': _capacity_blocked_work(store, works)}}


def _stalled_issues(store, run_id, works, waits, effects):
    stalled = []
    for issue in store.list(run_id, 'issue', status='open'):
        iid = issue['id']
        bound = [w for w in works if w['issue_id'] == iid or w['inputs'].get('issue_id') == iid]
        if any(w['status'] in {'ready', 'leased'} for w in bound):
            continue
        if any(w['issue_id'] == iid and w['status'] in PARKED for w in waits):
            continue
        if any(e['status'] in UNSETTLED and (e['issue_id'] == iid or e.get('metadata', {}).get('issue_id') == iid)
               for e in effects):
            continue
        last = max(bound, key=lambda w: w['created_seq']) if bound else None
        stalled.append({'issue_id': iid, 'last_work_id': last['id'] if last else None,
                        'last_work_status': last['status'] if last else None})
    return stalled


def _stopped_lab(store, run_id, works):
    labs = [w for w in works if w['kind'] == 'lab']
    if not labs or any(w['status'] in {'ready', 'leased'} for w in labs):
        return None
    work = max(labs, key=lambda w: w['created_seq'])
    effect = store.get(run_id, 'effect', work['effect_id']) if work.get('effect_id') else None
    proposal = None
    if effect is not None:
        rows = store.list(run_id, 'proposal', id=identity('proposal', effect['id']))
        proposal = rows[0] if rows else None
    if work['status'] in {'failed', 'blocked'}:
        stopped = True
    elif work['status'] == 'cancelled':
        # A cancelled prepared call ends its chain; a retired notification does not.
        stopped = effect is not None
    elif work['status'] == 'succeeded':
        stopped = proposal is None or proposal['status'] in STOPPED_PROPOSALS
    else:
        stopped = False
    if not stopped:
        return None
    reason = proposal.get('error') if proposal else None
    if reason is None and effect is not None:
        result = json.loads(store.read_blob(effect['result_ref'])) if effect.get('result_ref') else {}
        reason = (result.get('error') if isinstance(result, dict) else None) or effect.get('cancellation_reason')
    return {'work_id': work['id'], 'work_status': work['status'],
            'proposal_status': proposal['status'] if proposal else None, 'reason': reason}


def _sessions_requiring_recovery(store, run_id):
    rows = []
    for owner in store.list(run_id, 'agent_owner'):
        session = store.get(run_id, 'agent_session', owner['session_id'])
        if session['status'] == 'recovery_required':
            rows.append({'session_id': session['id'], 'role': session['role'],
                         'reason': session.get('recovery_reason'), 'active_turn_id': session['active_turn_id']})
    return rows


def _capacity_blocked_work(store, works):
    rows = []
    for work in works:
        if work['status'] != 'blocked' or not work.get('outcome_ref'):
            continue
        outcome = json.loads(store.read_blob(work['outcome_ref']))
        if isinstance(outcome, dict) and outcome.get('status') in CAPACITY_BLOCKS:
            rows.append({'work_id': work['id'], 'kind': work['kind'],
                         'reason': outcome.get('reason') or outcome['status']})
    return rows
