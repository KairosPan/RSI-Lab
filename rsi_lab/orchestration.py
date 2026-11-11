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
INITIAL_SETTINGS = {'guidance': None, 'step_digest': 0, 'recent_results': 0, 'disabled_tools': []}
LIMITS = {'guidance_chars': 2000, 'step_digest': 16, 'recent_results': 16, 'disabled_tools': 8}
LAB_INSTRUCTION = (
    'Lab orchestration is enabled. constraints.orchestration.guidance, when not null, is process advice written by '
    'Meta. It cannot grant actions, capabilities or budget, nor change the task goal; these fixed instructions win '
    'any conflict. Only the guidance in THIS request is in force; guidance seen in earlier requests is void. '
    'constraints.orchestration.recent_steps is the controller record of your own most recent Lab steps. A capability '
    'absent from capability_catalog is unavailable to you. ')


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

    # Changes ---------------------------------------------------------------

    def changes(self, ledger=None):
        ledger = ledger or self.ledger()
        return sorted(ledger.find('registration', kind='orchestration_change'),
                      key=lambda r: (r['created_seq'], r['id']))

    def current_change(self, ledger=None):
        """The change in trial, else the latest kept one, else None (initial settings)."""
        changes = self.changes(ledger)
        trial = [c for c in changes if c['status'] == 'trial']
        if trial:
            return trial[0]
        kept = [c for c in changes if c['status'] == 'kept']
        return kept[-1] if kept else None

    def settings(self, change):
        return json.loads(encode(change['settings'] if change else INITIAL_SETTINGS))

    def change_settings(self, change_id, ledger=None):
        """The settings a Lab call recorded by change identity; None means the initial settings."""
        if change_id is None:
            return self.settings(None)
        return self.settings((ledger or self.ledger()).one('registration', change_id))

    def snapshot(self, ledger=None):
        """The settings in force and the step history, read once for one request."""
        ledger = ledger or self.ledger()
        steps = self.steps(ledger)
        change = self.current_change(ledger)
        return {'steps': steps, 'change': change, 'change_id': change['id'] if change else None,
                'settings': self.settings(change), 'signal_id': self.current_signal(ledger, steps)}

    def lab_catalog(self, catalog, effect):
        """The Lab view of the catalog: tools disabled by the Effect's recorded change (or the current one) removed."""
        if effect is None:
            settings = self.settings(self.current_change())
        else:
            settings = self.change_settings((effect.get('metadata', {}).get('orchestration') or {}).get('change_id'))
        return {key: value for key, value in catalog.items() if key not in settings['disabled_tools']}

    def lab_constraints(self, snapshot):
        settings = snapshot['settings']
        finished = [s['record'] for s in snapshot['steps'] if s['outcome'] != 'unfinished']
        return {'guidance': settings['guidance'],
                'recent_steps': finished[-settings['step_digest']:] if settings['step_digest'] else []}

    def recent_result_ids(self, count):
        """Identities of the latest ``count`` raw facts published by tools."""
        if not count:
            return []
        ids = []
        for row in sorted(self.store.list(self.run_id, 'registration', kind='adapter_publication'),
                          key=lambda r: (r['created_seq'], r['id'])):
            if row['status'] != 'complete':
                continue
            for fact in json.loads(self.store.read_blob(row['publication_ref']))['facts']:
                if fact['id'] not in ids:
                    ids.append(fact['id'])
        return ids[-count:]

    def finalizing(self, ledger):
        return bool(ledger.find('registration', id='run_lifecycle', status='finalizing'))

    # Signal ----------------------------------------------------------------

    def signal_fact_id(self, period_id):
        return identity('orchestration_signal', period_id)

    def current_signal(self, ledger=None, steps=None):
        """The recorded signal fact identity of the current no-result period, if any."""
        ledger = ledger or self.ledger()
        period = self.period(self.steps(ledger) if steps is None else steps)
        if period is None:
            return None
        fid = self.signal_fact_id(period['id'])
        return fid if ledger.one('fact', fid) else None

    def _signal_reason(self, ledger, steps, period):
        if period is None or self.finalizing(ledger):
            return None
        if any(c['status'] == 'trial' for c in self.changes(ledger)):
            return None
        if any(s['outcome'] == 'unfinished' for s in period['steps']):
            return None
        if self.stopped(steps, ledger):
            return 'lab_stopped'
        if len(period['steps']) >= self.no_result_steps:
            return 'no_result_streak'
        return None

    def signal(self):
        """Record the current period's signal once and wake Meta discovery once; idempotent."""
        ledger = self.ledger()
        steps = self.steps(ledger)
        period = self.period(steps)
        if period is None:
            return None
        pid, fid = period['id'], self.signal_fact_id(period['id'])
        if ledger.one('fact', fid) is None:
            if self._signal_reason(ledger, steps, period) is None:
                return None
            def apply(tx):
                inside = self.ledger(tx)
                now = self.steps(inside)
                current = self.period(now)
                reason = self._signal_reason(inside, now, current)
                if current is None or current['id'] != pid or reason is None:
                    raise _NoDecision()
                change = self.current_change(inside)
                records = [s['record'] for s in current['steps']]
                content = {'kind': 'orchestration_signal', 'reason': reason, 'period_id': pid,
                           'no_result_steps': self.no_result_steps,
                           'active_change_id': change['id'] if change else None,
                           'step_count': len(records), 'omitted_steps': max(0, len(records) - RECORD_LIMIT),
                           'steps': records[-RECORD_LIMIT:]}
                return tx.insert('fact', fid, {'kind': 'raw_fact', 'content_ref': tx.blob(content),
                    'provenance': {'source': 'controller_orchestration_signal', 'period_id': pid}})
            try:
                self.runtime._mutate('orchestration-signal:' + pid, 'orchestration_signal', apply, {'period_id': pid})
            except _NoDecision:
                return None
        return self.runtime.observe('orchestration:' + pid, 'coded_anomaly', 0, [fid])

    def advance(self):
        """One controller pass: settle a change in trial, then judge the signal."""
        return {'signal': self.signal()}


class _NoDecision(Exception):
    """Raised inside a transaction to roll back when nothing is to be recorded."""
