"""Code-measured Lab progress and Meta-proposed Lab settings under code settlement.

A Lab step is one Lab model call (a ``kind='lab'`` Effect). Its outcome is
derived from the ledger only: whether the tool it requested published content
no earlier fact carried. A run of steps without a new result, or a Lab chain
that stopped abnormally, becomes a controller fact that wakes Meta. Meta may
then propose one bounded change to four Lab settings; code, never a model,
keeps or reverts it. Nothing here calls a model or executes a tool.
"""
from __future__ import annotations

import json
import time

from .controller import identity
from .store import digest, encode

UNSETTLED = {'prepared', 'dispatching', 'unknown'}
STOPPED_PROPOSALS = {'rejected', 'blocked_stale', 'blocked_action'}
# A Lab chain that ends on one of these has stopped abnormally.
STOP_CODES = {'proposal_rejected', 'proposal_blocked_stale', 'proposal_blocked_action', 'tool_failed'}
EXCERPT_CHARS = 160
REASON_CHARS = 240
# Records kept in one signal or settlement fact; the fact also counts all steps.
RECORD_LIMIT = 16


def orchestration_config(config):
    """The run's validated ``orchestration`` block, or None when the run does not declare one."""
    if 'orchestration' not in config:
        return None
    value = config['orchestration']
    if not isinstance(value, dict) or set(value) != {'no_result_steps', 'max_changes'}:
        raise ValueError('orchestration must contain exactly no_result_steps and max_changes')
    steps, changes = value['no_result_steps'], value['max_changes']
    if type(steps) is not int or not 2 <= steps <= 64:
        raise ValueError('orchestration.no_result_steps must be an integer from 2 to 64')
    if type(changes) is not int or not 0 <= changes <= 20:
        raise ValueError('orchestration.max_changes must be an integer from 0 to 20')
    return {'no_result_steps': steps, 'max_changes': changes}


def _clip(value, limit):
    return value if len(value) <= limit else value[:limit - 3] + '...'


class Ledger:
    """Uniform reads over the store or an open transaction."""

    def __init__(self, store, run_id, tx=None):
        self.store, self.run_id, self.tx = store, run_id, tx

    def find(self, entity_kind, **filters):
        if self.tx:
            return self.tx.find(entity_kind, **filters)
        return self.store.list(self.run_id, entity_kind, **filters)

    def one(self, entity_kind, entity_id):
        rows = self.find(entity_kind, id=entity_id)
        return rows[0] if rows else None

    def read(self, ref):
        return json.loads(self.store.read_blob(ref))


class Orchestration:
    def __init__(self, runtime, config):
        self.runtime, self.store, self.run_id = runtime, runtime.store, runtime.run_id
        self.no_result_steps = config['no_result_steps']
        self.max_changes = config['max_changes']
        self.clock = time.time

    def ledger(self, tx=None):
        return Ledger(self.store, self.run_id, tx)

    # Steps -----------------------------------------------------------------

    def steps(self, ledger=None):
        """Every Lab step in creation order with its outcome and one-line record."""
        ledger = ledger or self.ledger()
        first_seen = {}
        for fact in ledger.find('fact'):
            ref = fact['content_ref']
            first_seen[ref] = min(first_seen.get(ref, fact['created_seq']), fact['created_seq'])
        result = []
        for effect in sorted(ledger.find('effect', kind='lab'), key=lambda e: (e['created_seq'], e['id'])):
            metadata = effect.get('metadata', {})
            # Definite model failures and cancelled intents never reached a
            # Lab decision; a final review only interprets delivered results.
            if effect['status'] == 'failed_definite' or metadata.get('final_review_only'):
                continue
            outcome, code, reason = self._outcome(ledger, effect, first_seen)
            orchestration = metadata.get('orchestration') or {}
            result.append({'effect_id': effect['id'], 'work_id': effect['work_id'], 'seq': effect['created_seq'],
                           'change_id': orchestration.get('change_id'), 'outcome': outcome, 'reason_code': code,
                           'record': self._record(ledger, effect, outcome, code, reason)})
        return result

    def _outcome(self, ledger, effect, first_seen):
        if effect['status'] in UNSETTLED or effect.get('accounting_unknown'):
            return 'unfinished', None, None
        pid = identity('proposal', effect['id'])
        proposal = ledger.one('proposal', pid)
        if proposal is None or proposal['status'] == 'admitted':
            return 'unfinished', None, None
        if proposal['status'] in STOPPED_PROPOSALS:
            return 'no_result', 'proposal_' + proposal['status'], proposal.get('error')
        action = (proposal.get('command') or {}).get('action')
        if action != 'request_diagnostic':
            return 'no_result', action or 'no_command', None
        work = ledger.one('work', identity('work', None, 'diagnostic', identity('object', pid)))
        if work is None or work['status'] in {'ready', 'leased', 'blocked'}:
            return 'unfinished', None, None
        if work['status'] == 'cancelled':
            return 'no_result', 'tool_cancelled', None
        if work['status'] == 'failed':
            if not work.get('effect_id'):
                outcome = ledger.read(work['outcome_ref']) if work.get('outcome_ref') else {}
                return 'no_result', 'tool_rejected', outcome.get('reason') if isinstance(outcome, dict) else None
            tool = ledger.one('effect', work['effect_id'])
            receipt = ledger.read(tool['result_ref']) if tool and tool.get('result_ref') else {}
            reason = (receipt.get('error') or receipt.get('reason')) if isinstance(receipt, dict) else None
            return 'no_result', 'tool_failed', reason
        tool = ledger.one('effect', work['effect_id']) if work.get('effect_id') else None
        if tool is None or tool['status'] != 'succeeded' or tool.get('accounting_unknown'):
            return 'unfinished', None, None
        publication = ledger.one('registration', identity('publication', tool['id']))
        if publication is None or publication['status'] != 'complete':
            return 'unfinished', None, None
        content = ledger.read(publication['publication_ref'])
        seq = publication['created_seq']
        for row in content['facts'] + content['conclusions']:
            fact = ledger.one('fact', row['id'])
            # New only when this publication inserted it and no earlier fact
            # had the same content.
            if fact and fact['created_seq'] == seq and first_seen.get(fact['content_ref']) == seq:
                return 'new_result', 'new_result', None
        return 'no_result', 'repeated_result' if content['facts'] + content['conclusions'] else 'no_facts', None

    def _record(self, ledger, effect, outcome, code, reason):
        command = {}
        if effect.get('result_ref'):
            try:
                response = ledger.read(effect['result_ref']).get('response')
                command = response.get('command') if isinstance(response, dict) else {}
            except (ValueError, TypeError, AttributeError):
                command = {}
        command = command if isinstance(command, dict) else {}
        payload = command.get('payload') if isinstance(command.get('payload'), dict) else {}
        def text(value):
            return value if isinstance(value, str) else None
        inputs = text(payload.get('inputs_json'))
        return {'effect_id': effect['id'], 'action': text(command.get('action')),
                'capability_id': text(payload.get('capability_id')),
                'capability_version': text(payload.get('capability_version')),
                'inputs_sha256': digest(inputs.encode()) if inputs is not None else None,
                'inputs_excerpt': _clip(inputs, EXCERPT_CHARS) if inputs is not None else None,
                'outcome': outcome, 'reason_code': code,
                'reason': _clip(reason, REASON_CHARS) if isinstance(reason, str) else None}

    def period(self, steps):
        """The steps after the latest new result, identified by its first step; None when empty."""
        last = max((i for i, s in enumerate(steps) if s['outcome'] == 'new_result'), default=-1)
        rows = steps[last + 1:]
        if not rows:
            return None
        return {'id': identity('orchestration_period', rows[0]['effect_id']), 'steps': rows}

    def stopped(self, steps, ledger=None):
        """The latest Lab Work finished, nothing follows it, and its step ended on a rejection or tool failure."""
        ledger = ledger or self.ledger()
        labs = ledger.find('work', kind='lab')
        if not labs or any(w['status'] in {'ready', 'leased'} for w in labs):
            return False
        last = max(labs, key=lambda w: (w['created_seq'], w['id']))
        if last['status'] != 'succeeded' or not last.get('effect_id'):
            return False
        step = next((s for s in steps if s['effect_id'] == last['effect_id']), None)
        return step is not None and step['outcome'] == 'no_result' and step['reason_code'] in STOP_CODES
