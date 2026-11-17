"""V2 code-owned scheduling, bounded investigation and recoverable external effects."""
from __future__ import annotations

import json
import math
import os
import tempfile
import time

from .store import Store, digest, encode

WORK_KINDS = {'lab', 'meta_discovery', 'meta_disposition', 'disposition', 'evidence_retrieve',
              'diagnostic', 'capability_build', 'effect_reconcile', 'trial_prepare', 'trial_settle'}
FACT_KINDS = {'raw_fact', 'source_statement', 'hypothesis', 'validation'}
UNSETTLED = {'prepared', 'dispatching', 'unknown'}
EXPIRY_ACTIONS = {'narrow_request', 'replan', 'ask_user', 'close_unresolved', 'blocked_external'}


def identity(prefix, *parts):
    return prefix + '_' + digest(list(parts))[:24]


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def request_scope_identity(scope):
    """Object/field sets have no semantic order; wording is not request identity."""
    if isinstance(scope, dict):
        scope = {key: (sorted(set(value)) if key in {'object_ids', 'fields'}
                       and isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)
                       else value) for key, value in scope.items()}
    return digest(scope)


def publish_envelope(path, value):
    """Make a receipt envelope visible only once it is complete and durable."""
    fd, temporary = tempfile.mkstemp(prefix='.tmp-' + path.name + '-', dir=path.parent)
    published = False
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encode(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        published = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if not published:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def read_envelope(path):
    """A missing, torn or incomplete envelope proves nothing and counts as absent."""
    try:
        envelope = json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(envelope, dict) or any(not isinstance(envelope.get(field), str)
                                             for field in ('effect_id', 'packet_hash', 'receipt_ref')):
        return None
    return envelope


class Controller:
    schema_version = 'rsi_control_v2'

    def __init__(self, store):
        self.store = store

    def initialize(self, run_id, *, budgets, max_external=1, config=None):
        if type(max_external) is not int or max_external < 1:
            raise ValueError('A positive external dispatch capacity is required')
        for resource, row in budgets.items():
            if not finite(row['limit']) or type(row['hard']) is not bool:
                raise ValueError('Explicit finite budget and hard/soft meaning required')
            if row['hard'] and resource not in {'model_calls', 'tool_calls'}:
                raise ValueError('Only executable call-count restrictions can be hard in v2')
            if not finite(row.get('settlement_reserve', 0)) or row.get('settlement_reserve', 0) > row['limit']:
                raise ValueError('Invalid protected disposition/settlement allowance')
        run_config = {'schema_version': self.schema_version, **(config or {}),
                      'budgets': budgets, 'max_external': max_external}
        def initialize(tx):
            rows = [tx.insert('budget', name, {**row, 'spent': 0, 'reserved': 0,
                'overrun': False}) for name, row in budgets.items()]
            tx.insert('dependency', 'external_capacity', {'producer': 'controller',
                'value': {'status': 'available', 'available': max_external},
                'content_hash': digest({'status': 'available', 'available': max_external}), 'revision': 1})
            return rows
        # The run row and its budgets commit together, so a repeated init either
        # returns the first result or completes a run left without budgets.
        return self.store.mutate(run_id, 'initialize', 'run_initialized', initialize, payload=budgets,
                                 run_config=run_config)

    def _capacity(self, tx):
        maximum = self.store.run(tx.run_id)['config'].get('max_external', 1)
        available = maximum - sum(e['status'] in UNSETTLED for e in tx.find('effect'))
        old = tx.find('dependency', id='external_capacity')
        if old:
            old = old[0]
            value = {'status': 'available' if available > 0 else 'busy', 'available': available}
            if digest(value) != old['content_hash']:
                tx.update('dependency', old['id'], {'value': value, 'content_hash': digest(value), 'revision': old['revision'] + 1})
        from .work_claims import reconcile_all
        reconcile_all(tx)

    def _work(self, tx, issue_id, kind, trigger, *, inputs=None, priority=10):
        if kind not in WORK_KINDS:
            raise ValueError('Unregistered Work kind')
        wid = identity('work', issue_id, kind, trigger)
        existing = tx.find('work', id=wid)
        if existing:
            previous = existing[0]
            if previous['inputs'] != (inputs or {}) or previous['priority'] != priority:
                raise ValueError('Work identity reused with different inputs or priority; use a new trigger')
            return previous
        expected = {}
        if issue_id:
            issue = tx.get('issue', issue_id)
            if issue['status'] != 'open':
                raise ValueError('Closed issue cannot schedule new work')
            expected = {key: issue[key] for key in ('evidence_revision', 'scheduling_revision')}
        return tx.insert('work', wid, {'issue_id': issue_id, 'kind': kind, 'trigger': trigger,
            'expected_versions': expected, 'status': 'ready', 'priority': priority,
            'inputs': inputs or {}, 'attempts': 0, 'effect_id': None, 'outcome_ref': None})

    def enqueue(self, run_id, kind, trigger, key, *, issue_id=None, inputs=None, priority=10):
        payload = {'kind': kind, 'trigger': trigger, 'issue_id': issue_id, 'inputs': inputs, 'priority': priority}
        return self.store.mutate(run_id, key, 'work_enqueued',
            lambda tx: self._work(tx, issue_id, kind, trigger, inputs=inputs, priority=priority), payload=payload)

    def lab_scope(self, run_id, inputs):
        config = self.store.run(run_id)['config']
        return {'branch_id': inputs.get('branch_id', config.get('branch_id', 'main')),
                'agent_instance_id': inputs.get('agent_instance_id') or 'lab'}

    def wake_lab(self, tx, trigger, *, inputs=None, priority=10):
        """Merge only unfrozen, compatible notifications, preserving all evidence."""
        inputs = json.loads(encode(inputs or {}))
        scope = self.lab_scope(tx.run_id, inputs)
        alias = identity('lab_wakeup', scope, trigger)
        request_hash = digest({'inputs': inputs, 'priority': priority})
        prior = tx.find('registration', id=alias)
        if prior:
            if prior[0].get('request_hash') != request_hash:
                raise ValueError('Lab wakeup identity reused with different inputs or priority')
            return tx.get('work', prior[0]['work_id'])
        aggregate = {'evidence_ids', 'required_evidence_ids', 'source_effect_id', 'recommendation_id',
                     'source_effect_ids', 'recommendation_ids', 'wakeups', 'branch_id', 'agent_instance_id'}
        special = {'repair_of', 'retry_of', 'final_review_only', 'allowed_action', 'handoff_id', 'scientific_query',
                   'batch_interpretation_id'}
        target = None
        for work in sorted(tx.find('work', kind='lab', status='ready'), key=lambda w: w['created_seq']):
            old = work['inputs']
            if (work.get('effect_id') or work['issue_id'] is not None or special.intersection(set(old) | set(inputs))
                    or self.lab_scope(tx.run_id, old) != scope or work['priority'] != priority):
                continue
            if {k:v for k,v in old.items() if k not in aggregate} != {k:v for k,v in inputs.items() if k not in aggregate}:
                continue
            merged = dict(old)
            for key in ('evidence_ids', 'required_evidence_ids'):
                merged[key] = list(dict.fromkeys(old.get(key, []) + inputs.get(key, [])))
            for singular, plural in (('source_effect_id', 'source_effect_ids'), ('recommendation_id', 'recommendation_ids')):
                values = old.get(plural, []) + inputs.get(plural, [])
                values += [d[singular] for d in (old, inputs) if d.get(singular)]
                if values:
                    merged[plural] = list(dict.fromkeys(values))
                    merged.setdefault(singular, values[0])
            merged['wakeups'] = old.get('wakeups', [{'trigger': work['trigger'], 'inputs': old}]) + [
                {'trigger': trigger, 'inputs': inputs}]
            target = tx.update('work', work['id'], {'inputs': merged})
            break
        if target is None:
            target = self._work(tx, None, 'lab', identity('lab_notification', scope, trigger),
                                inputs=inputs, priority=priority)
        tx.insert('registration', alias, {'kind': 'lab_wakeup', 'trigger': trigger, 'scope': scope,
                                         'work_id': target['id'], 'request_hash': request_hash})
        return target

    def diagnostic_lab_scope(self, run_id, tool):
        inputs = tool['inputs']
        if inputs.get('lab_scope') is not None:
            return inputs['lab_scope']
        if inputs.get('source_proposal_id'):
            proposal = self.store.get(run_id, 'proposal', inputs['source_proposal_id'])
            effect = self.store.get(run_id, 'effect', proposal['effect_id'])
            source = self.store.get(run_id, 'work', effect['work_id'])
            return self.lab_scope(run_id, source['inputs'])
        return self.lab_scope(run_id, {})

    def lab_blockers(self, run_id, work):
        if work['kind'] != 'lab':
            return []
        scope = self.lab_scope(run_id, work['inputs'])
        blocked = []
        owned_review = any(b['id'] == work['inputs'].get('batch_interpretation_id') and b['scope'] == scope
                          for b in self.store.list(run_id, 'registration', kind='batch_interpretation_barrier'))
        for barrier in self.store.list(run_id, 'registration', kind='batch_interpretation_barrier'):
            if (barrier['scope'] == scope and barrier['status'] != 'interpreted'
                    and not owned_review):
                blocked.append(barrier['id'])
        for tool in self.store.list(run_id, 'work', kind='diagnostic'):
            inputs = tool['inputs']
            if not inputs.get('lab_continuation'):
                continue
            if self.diagnostic_lab_scope(run_id, tool) != scope:
                continue
            effect = self.store.get(run_id, 'effect', tool['effect_id']) if tool.get('effect_id') else None
            pending = tool['status'] in {'ready', 'leased', 'blocked'}
            if effect:
                pending |= effect['status'] in UNSETTLED or bool(effect.get('accounting_unknown'))
                if effect['status'] == 'succeeded':
                    pending |= not bool(self.store.list(run_id, 'registration',
                        id=identity('publication', effect['id']), status='complete'))
            if pending:
                blocked.append(tool['id'])
        return blocked

    def add_fact(self, run_id, fact_id, kind, content, provenance, key):
        if kind not in FACT_KINDS or not provenance:
            raise ValueError('Facts, source statements, hypotheses and validations need explicit provenance')
        payload = {'fact_id': fact_id, 'kind': kind, 'content': content, 'provenance': provenance}
        return self.store.mutate(run_id, key, 'evidence_recorded', lambda tx: tx.insert('fact', fact_id,
            {'kind': kind, 'content_ref': tx.blob(content), 'provenance': provenance}), payload=payload)

    def open_issue(self, run_id, issue_id, semantic_key, hypothesis, scope, evidence_ids,
                   alternatives, falsifiers, key):
        if not semantic_key or not hypothesis or not scope or not alternatives or not falsifiers:
            raise ValueError('Issue needs scope, hypothesis, competing explanations and discriminating results')
        payload = dict(issue_id=issue_id, semantic_key=semantic_key, hypothesis=hypothesis, scope=scope,
                       evidence_ids=evidence_ids, alternatives=alternatives, falsifiers=falsifiers)
        def apply(tx):
            for fid in evidence_ids:
                tx.get('fact', fid)
            matches = tx.find('issue', semantic_hash=digest(semantic_key))
            if matches:
                issue = matches[0]
                new = set(evidence_ids) - set(issue['evidence_ids'])
                patch = {'seen_count': issue['seen_count'] + 1}
                if new:
                    patch.update(evidence_ids=sorted(set(issue['evidence_ids']) | new),
                        evidence_revision=issue['evidence_revision'] + 1, status='open',
                        scheduling_revision=issue['scheduling_revision'] + 1)
                result = tx.update('issue', issue['id'], patch)
                if new:
                    self._work(tx, issue['id'], 'meta_disposition', 'evidence:' + str(result['evidence_revision']))
                return result
            issue = tx.insert('issue', issue_id, {'semantic_key': semantic_key, 'semantic_hash': digest(semantic_key),
                'hypothesis': hypothesis, 'scope': scope, 'evidence_ids': sorted(set(evidence_ids)),
                'alternatives': alternatives, 'falsifiers': falsifiers, 'status': 'open', 'seen_count': 1,
                'evidence_revision': 1 if evidence_ids else 0, 'scheduling_revision': 0, 'conclusion_ref': None})
            self._work(tx, issue_id, 'meta_disposition', 'issue_created')
            return issue
        return self.store.mutate(run_id, key, 'issue_discovered', apply, payload=payload)

    def ready(self, run_id):
        self.store.verify_history(run_id)
        result = []
        for work in self.store.list(run_id, 'work', status='ready'):
            if self.lab_blockers(run_id, work):
                continue
            if work['issue_id']:
                issue = self.store.get(run_id, 'issue', work['issue_id'])
                if issue['status'] != 'open' or any(issue[k] != v for k, v in work['expected_versions'].items()):
                    continue
            result.append(work)
        return sorted(result, key=lambda w: (min(w['priority'], 1) if w['kind'] == 'diagnostic' else w['priority'],
                                              w['created_seq'], w['id']))

    def dependency(self, run_id, dependency_id, producer, value, key):
        """Only relevant value/capacity changes advance the dependency revision."""
        payload = dict(dependency_id=dependency_id, producer=producer, value=value)
        def apply(tx):
            old = tx.find('dependency', id=dependency_id)
            if not old:
                return tx.insert('dependency', dependency_id, {'producer': producer, 'value': value,
                    'content_hash': digest(value), 'revision': 1})
            old = old[0]
            if old['producer'] != producer:
                raise ValueError('Dependency producer changed')
            if old['content_hash'] == digest(value):
                return old
            return tx.update('dependency', dependency_id, {'value': value, 'content_hash': digest(value),
                                                          'revision': old['revision'] + 1})
        return self.store.mutate(run_id, key, 'dependency_updated', apply, payload=payload)

    def park(self, run_id, issue_id, dependency_id, producer, observed_version,
             wake_condition, deadline_at, expiry_action, key):
        if not finite(deadline_at) or expiry_action not in EXPIRY_ACTIONS or not isinstance(wake_condition, dict):
            raise ValueError('Park requires deadline, bounded expiry action and explicit wake condition')
        if set(wake_condition) not in ({'kind'}, {'kind', 'status'}) or wake_condition.get('kind') not in {'version_changed', 'status'}:
            raise ValueError('Unsupported wake condition')
        if wake_condition['kind'] == 'status' and not wake_condition.get('status'):
            raise ValueError('Status wake requires a target status')
        payload = dict(issue_id=issue_id, dependency_id=dependency_id, producer=producer,
            observed_version=observed_version, wake_condition=wake_condition, deadline_at=deadline_at, expiry_action=expiry_action)
        def apply(tx):
            issue, dep = tx.get('issue', issue_id), tx.get('dependency', dependency_id)
            if issue['status'] != 'open' or dep['producer'] != producer or dep['revision'] != observed_version:
                raise ValueError('Park dependency identity/version is stale')
            if wake_condition['kind'] == 'status':
                if not isinstance(dep['value'], dict):
                    raise ValueError('A status wait requires a dependency with explicit status')
                if dep['value'].get('status') == wake_condition['status']:
                    raise ValueError('The requested dependency status is already satisfied; execute eligible work')
            same = tx.find('wait', issue_id=issue_id, dependency_id=dependency_id, observed_version=observed_version)
            if same:
                raise ValueError('A dependency/capacity change is required for a new wait cycle; time cannot renew it')
            if any(e['status'] in UNSETTLED for e in tx.find('effect', issue_id=issue_id)):
                raise ValueError('Unknown execution must be reconciled, not converted into a free wait')
            for work in tx.find('work', issue_id=issue_id, status='ready'):
                tx.update('work', work['id'], {'status': 'cancelled', 'outcome_ref': tx.blob({'reason': 'parked_with_dependency'})})
            self._capacity(tx)
            epoch = 1 + len(tx.find('wait', issue_id=issue_id))
            wid = identity('wait', issue_id, dependency_id, observed_version)
            tx.update('issue', issue_id, {'scheduling_revision': issue['scheduling_revision'] + 1})
            return tx.insert('wait', wid, {**payload, 'wait_epoch': epoch, 'status': 'waiting',
                'disposition_attempted': False, 'disposition_work_id': None})
        return self.store.mutate(run_id, key, 'issue_parked', apply, payload=payload)

    def tick(self, run_id, now=None):
        now = time.time() if now is None else now
        fired = []
        for wait in self.store.list(run_id, 'wait'):
            if wait['status'] not in {'waiting', 'disposition_due', 'disposed'}:
                continue
            dep = self.store.get(run_id, 'dependency', wait['dependency_id'])
            changed = dep['revision'] > wait['observed_version']
            wake = changed and (wait['wake_condition']['kind'] == 'version_changed' or
                               (isinstance(dep['value'], dict) and
                                dep['value'].get('status') == wait['wake_condition'].get('status')))
            due = wait['status'] == 'waiting' and now >= wait['deadline_at']
            if not wake and not due:
                continue
            action = 'wake' if wake else 'expire'
            def apply(tx, wait=wait, action=action, dep=dep):
                current = tx.get('wait', wait['id'])
                issue = tx.get('issue', wait['issue_id'])
                if issue['status'] != 'open':
                    return tx.update('wait', wait['id'], {'status': 'superseded'})
                tx.update('issue', issue['id'], {'scheduling_revision': issue['scheduling_revision'] + 1})
                if action == 'wake':
                    for work in tx.find('work', issue_id=issue['id'], status='ready'):
                        tx.update('work', work['id'], {'status': 'cancelled'})
                    self._capacity(tx)
                    updated = tx.update('wait', wait['id'], {'status': 'fired'})
                    self._work(tx, issue['id'], 'meta_disposition', 'dependency:' + dep['id'] + ':' + str(dep['revision']))
                else:
                    work = self._work(tx, issue['id'], 'disposition', 'wait:' + wait['id'],
                                      inputs={'wait_id': wait['id'], 'allowed_action': wait['expiry_action']}, priority=0)
                    updated = tx.update('wait', wait['id'], {'status': 'disposition_due', 'disposition_work_id': work['id']})
                return updated
            fired.append(self.store.mutate(run_id, action + ':' + wait['id'], 'wait_' + action, apply,
                payload={'wait_id': wait['id'], 'action': action, 'dependency_revision': dep['revision'] if wake else wait['observed_version']}))
        return fired

    def request(self, run_id, issue_id, request_id, query, source, scope, key, *, supersedes=None,
                source_version=1, query_spec=None):
        if not query or not source or not scope:
            raise ValueError('Evidence request needs a bounded query, authorized source and scope')
        if type(source_version) is not int or source_version < 1 or (query_spec is not None and not isinstance(query_spec, dict)):
            raise ValueError('Evidence request needs a positive source version and structured query specification')
        query_spec = ({'query': query, 'source_id': source, 'source_version': source_version, 'scope': scope}
                      if query_spec is None else query_spec)
        for name, expected in {'query': query, 'source_id': source, 'source_version': source_version}.items():
            if name in query_spec and query_spec[name] != expected:
                raise ValueError('Query specification contradicts the requested source/version/query')
        payload = dict(issue_id=issue_id, request_id=request_id, query=query, source=source, scope=scope,
                       supersedes=supersedes, source_version=source_version, query_spec=query_spec)
        def apply(tx):
            issue = tx.get('issue', issue_id)
            if issue['status'] != 'open':
                raise ValueError('Evidence requests must belong to an open issue')
            scope_hash = request_scope_identity(scope)
            for previous in tx.find('request', issue_id=issue_id):
                # A different request/dependency ID or differently worded query
                # must not manufacture another wait epoch after a timeout.
                # Unversioned historical v2 requests conservatively block an
                # identical scope; they do not prove a new evidence boundary.
                revision = previous.get('issue_evidence_revision', issue['evidence_revision'])
                if (previous['source'] == source and previous['source_version'] == source_version
                        and revision == issue['evidence_revision']
                        and request_scope_identity(previous['scope']) == scope_hash):
                    raise ValueError('Duplicate request scope at the same evidence/source version; time or wording cannot renew investigation')
            if supersedes:
                previous = tx.get('request', supersedes)
                if previous['issue_id'] != issue_id or previous['source'] != source:
                    raise ValueError('Replanning cannot silently change issue or authorized source')
                if any(e['status'] in UNSETTLED and e['metadata'].get('request_id') == supersedes for e in tx.find('effect')):
                    raise ValueError('Unreconciled retrieval cannot be superseded')
                tx.update('request', supersedes, {'status': 'superseded'})
            return tx.insert('request', request_id, {'issue_id': issue_id, 'query': query, 'source': source,
                'scope': scope, 'source_version': source_version, 'query_spec': query_spec,
                'scope_hash': scope_hash, 'issue_evidence_revision': issue['evidence_revision'],
                'status': 'created', 'supersedes': supersedes, 'delivery_ids': []})
        return self.store.mutate(run_id, key, 'evidence_requested', apply, payload=payload)

    def deliver(self, run_id, request_id, delivery_id, content, provenance, key, *, kind='raw', page=0, source_version=1):
        if kind not in {'raw', 'summary', 'pointer'} or not provenance or type(page) is not int or page < 0:
            raise ValueError('Delivery needs explicit content kind, provenance and page identity')
        payload = dict(request_id=request_id, delivery_id=delivery_id, content=content, provenance=provenance,
                       kind=kind, page=page, source_version=source_version)
        def apply(tx):
            request = tx.get('request', request_id)
            if request['status'] in {'superseded', 'cancelled'} or request['source_version'] != source_version:
                raise ValueError('Delivery belongs to an obsolete request/source version')
            if any(d['page'] == page for d in tx.find('delivery', request_id=request_id)):
                raise ValueError('Page identity already delivered; use its original receipt')
            result = tx.insert('delivery', delivery_id, {'request_id': request_id, 'content_ref': tx.blob(content),
                'source_version': source_version, 'kind': kind, 'page': page, 'provenance': provenance,
                'status': 'materialized', 'presented_effect_id': None})
            tx.update('request', request_id, {'status': 'delivered', 'delivery_ids': request['delivery_ids'] + [delivery_id]})
            return result
        return self.store.mutate(run_id, key, 'evidence_delivered', apply, payload=payload)

    def ask_user(self, run_id, issue_id, question_id, text, dependency_id, key):
        if not text.strip():
            raise ValueError('Explicit question text required')
        payload = dict(issue_id=issue_id, question_id=question_id, text=text, dependency_id=dependency_id)
        def apply(tx):
            issue = tx.get('issue', issue_id)
            dep = tx.get('dependency', dependency_id)
            if dep['producer'] != 'user':
                raise ValueError('A user question must wait on a user-produced dependency')
            return tx.insert('question', question_id, {'issue_id': issue_id, 'text': text, 'status': 'pending',
                'issue_evidence_revision': issue['evidence_revision'], 'dependency_id': dependency_id,
                'dependency_revision': dep['revision'], 'reply_ref': None})
        return self.store.mutate(run_id, key, 'user_question_saved', apply, payload=payload)

    def reply(self, run_id, question_id, question_version, text, key):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('No reply may be inferred from silence')
        payload = dict(question_id=question_id, question_version=question_version, text=text)
        def apply(tx):
            question = tx.get('question', question_id)
            issue = tx.get('issue', question['issue_id'])
            dep = tx.get('dependency', question['dependency_id'])
            if (question['status'] != 'pending' or question['version'] != question_version
                    or issue['evidence_revision'] != question['issue_evidence_revision']
                    or dep['revision'] != question['dependency_revision']):
                raise ValueError('Reply does not match the original pending question and evidence version')
            reply_ref = tx.blob({'source': 'user_reply', **payload})
            tx.update('question', question_id, {'status': 'answered', 'reply_ref': reply_ref})
            tx.insert('fact', identity('reply', question_id), {'kind': 'source_statement', 'content_ref': reply_ref,
                'provenance': {'question_id': question_id, 'question_version': question_version}})
            tx.update('issue', issue['id'], {'evidence_revision': issue['evidence_revision'] + 1,
                'evidence_ids': issue['evidence_ids'] + [identity('reply', question_id)]})
            return tx.update('dependency', dep['id'], {'revision': dep['revision'] + 1,
                'value': {'status': 'answered', 'reply_ref': reply_ref},
                'content_hash': digest({'status': 'answered', 'reply_ref': reply_ref})})
        return self.store.mutate(run_id, key, 'user_reply_bound', apply, payload=payload)

    def prepare_effect(self, run_id, work_id, kind, packet, reservations, key, metadata=None):
        """Reserve cost and lease in the same commit as the immutable outbox intent."""
        raw = packet if isinstance(packet, bytes) else encode(packet)
        if not raw or not reservations or any(not finite(v) for v in reservations.values()):
            raise ValueError('A bounded nonempty input and explicit resource reservations are required')
        metadata = dict(metadata or {})
        payload = dict(work_id=work_id, kind=kind, packet_hash=digest(raw), reservations=reservations, metadata=metadata)
        def apply(tx):
            work = tx.get('work', work_id)
            if work['status'] != 'ready':
                raise ValueError('Work is not eligible for dispatch')
            if metadata.get('expected_work_version', work['version']) != work['version']:
                raise ValueError('Work changed after packet preparation; rebuild its request')
            if self.lab_blockers(run_id, work):
                raise ValueError('Lab awaits its queued tool execution and publication')
            if 'decision_dependencies' in metadata:
                self._validate_decision(run_id, {'metadata': metadata}, raw)
            if tx.find('registration', id='run_lifecycle', status='finalizing') and work['kind'] not in {'effect_reconcile', 'trial_settle'}:
                raise ValueError('Finalizing run does not admit new external work')
            if work['issue_id']:
                issue = tx.get('issue', work['issue_id'])
                if issue['status'] != 'open' or any(issue[k] != v for k, v in work['expected_versions'].items()):
                    raise ValueError('Dispatch input versions are stale; replan first')
            config = self.store.run(run_id)['config']
            if sum(e['status'] in UNSETTLED for e in tx.find('effect')) >= config.get('max_external', 1):
                raise ValueError('No external execution lease; waiting issues do not count here')
            recovery = work['kind'] in {'disposition', 'effect_reconcile', 'trial_settle'}
            for resource, amount in reservations.items():
                budget = tx.get('budget', resource)
                floor = 0 if recovery else budget.get('settlement_reserve', 0)
                if budget['spent'] + budget['reserved'] + amount > budget['limit'] - floor:
                    raise ValueError('Budget admission denied: ' + resource)
                tx.update('budget', resource, {'reserved': budget['reserved'] + amount})
            if metadata.get('trial_id'):
                trial = tx.get('trial', metadata['trial_id'])
                spec = json.loads(self.store.read_blob(trial['spec_ref']))
                restoration = metadata.get('trial_operation') == 'restore_baseline'
                if restoration:
                    if (work['kind'] != 'trial_settle' or trial['status'] != 'restoring'
                            or metadata.get('baseline_versions') != spec['baseline_versions']):
                        raise ValueError('Restoration requires trial settlement work and exact frozen baseline versions')
                else:
                    if metadata.get('trial_operation') is not None:
                        raise ValueError('Unregistered trial operation')
                    if trial['status'] != 'active':
                        raise ValueError('Trial is not active')
                    arm = metadata.get('arm')
                    pairs = [p for p in spec['pairs'] if p['pair_id'] == metadata.get('pair_id')]
                    if arm not in {'baseline', 'candidate'} or len(pairs) != 1 or pairs[0][arm + '_run_id'] != metadata.get('run_id'):
                        raise ValueError('Effect is not in the prospective trial arm plan')
                effects = [e for e in tx.find('effect') if e['metadata'].get('trial_id') == trial['id']]
                for resource, amount in reservations.items():
                    used = sum(e.get('actual_costs', {}).get(resource, 0) + e.get('held_reservations', {}).get(resource, 0) for e in effects)
                    floor = 0 if restoration else spec['settlement_reserve'].get(resource, 0)
                    if resource not in spec['budget'] or used + amount > spec['budget'][resource] - floor:
                        raise ValueError('Trial budget admission denied')
            eid = identity('effect', run_id, key)
            packet_ref = tx.blob(raw)
            effect_metadata = dict(metadata)
            if metadata.get('agent_session_id'):
                from .agent_state import AgentState
                turn = AgentState(self.store, run_id).prepare_turn_tx(tx,
                    metadata['agent_session_id'], work_id,
                    expected_turn_seq=metadata.get('expected_session_head'),
                    expected_memory_revision=metadata.get('expected_memory_revision'),
                    context_ref=metadata.get('context_ref'))
                effect_metadata['agent_turn_id'] = turn['id']
            effect = tx.insert('effect', eid, {'work_id': work_id, 'issue_id': work['issue_id'], 'kind': kind,
                'status': 'prepared', 'packet_ref': packet_ref, 'packet_hash': packet_ref, 'packet_sha256': packet_ref,
                'idempotency_key': eid, 'metadata': effect_metadata, 'reservations': reservations,
                'held_reservations': reservations, 'actual_costs': {}, 'accounting_unknown': False,
                'receipt_ref': None, 'result_ref': None, 'provider_handle': None})
            tx.update('work', work_id, {'status': 'leased', 'effect_id': eid, 'attempts': work['attempts'] + 1})
            if work['kind'] == 'disposition':
                wait = tx.get('wait', work['inputs']['wait_id'])
                if wait['disposition_attempted']:
                    raise ValueError('Only one disposition attempt per wait cycle')
                tx.update('wait', wait['id'], {'disposition_attempted': True, 'status': 'disposed'})
            self._capacity(tx)
            return effect
        return self.store.mutate(run_id, key, 'effect_prepared', apply, payload=payload)

    def _validate_decision(self, run_id, effect, raw):
        from . import decision_dependencies, input_view
        envelope = json.loads(raw)
        task = input_view.decode(envelope['prompt'].split('INPUT_DATA_JSON:\n', 1)[1],
                                 envelope['input_view_mode'])['task']
        manifest = decision_dependencies.validate_binding(effect, task)
        if manifest is None:
            raise decision_dependencies.StaleDecision('legacy_unversioned: prepare a fresh decision before dispatch')
        decision_dependencies.validate(self.store, run_id, manifest)

    def dispatch_effect(self, run_id, effect_id, executor, *, pre_dispatch_check=None):
        self.store.verify_history(run_id)
        # The dispatch CAS is deliberately not idempotently callable: an already
        # dispatching attempt must reconcile, never invoke its executor twice.
        if self.store.get(run_id, 'effect', effect_id)['status'] != 'prepared':
            raise ValueError('Existing external attempt requires reconciliation; no implicit retry')
        def mark(tx):
            effect = tx.get('effect', effect_id)
            if effect['status'] != 'prepared':
                raise ValueError('Concurrent dispatcher already claimed this effect')
            work = tx.get('work', effect['work_id'])
            if work['status'] != 'leased' or work['effect_id'] != effect_id:
                raise ValueError('Prepared Effect no longer owns its eligible Work lease')
            if pre_dispatch_check is not None:
                # A trusted runtime guard shares the dispatch CAS transaction;
                # no publication can insert a new obligation between its read
                # and the commit. External execution remains outside the lock.
                pre_dispatch_check(effect, work)
            if effect['kind'] in {'lab', 'meta'} and effect['metadata'].get('plan_ref'):
                self._validate_decision(run_id, effect, self.store.read_blob(effect['packet_ref']))
            if work['issue_id']:
                issue = tx.get('issue', work['issue_id'])
                if issue['status'] != 'open' or any(issue[k] != v for k, v in work['expected_versions'].items()):
                    raise ValueError('Prepared input versions are stale; cancel_prepared and replan before dispatch')
            if tx.find('registration', id='run_lifecycle', status='finalizing') and work['kind'] not in {'effect_reconcile', 'trial_settle'}:
                raise ValueError('Finalizing run blocks undispatched external work')
            # Independent producers survive an issue's scheduling changes, but
            # must not run after the issue owning their obligation was closed.
            owners = {effect.get('metadata', {}).get('issue_id'), work.get('inputs', {}).get('issue_id')}
            for issue_id in owners - {None}:
                if tx.get('issue', issue_id)['status'] != 'open':
                    raise ValueError('Producer issue is closed; cancel_prepared before dispatch')
            patch = {'status': 'dispatching'}
            if effect['metadata'].get('agent_turn_id'):
                from .agent_state import AgentState
                turn = AgentState(self.store, run_id).claim_turn_tx(tx,
                    effect['metadata']['agent_turn_id'], effect_id)
                patch['metadata'] = {**effect['metadata'], 'session_fence': turn['fence']}
            return tx.update('effect', effect_id, patch)
        # Unique nonce prevents repeat-key fast path from bypassing the CAS.
        effect = self.store.mutate(run_id, 'dispatch:' + effect_id + ':' + str(time.time_ns()),
            'effect_dispatching', mark, payload={'effect_id': effect_id})
        try:
            receipt = executor(effect, self.store.read_blob(effect['packet_ref']))
        except Exception as exc:
            receipt = {'status': 'unknown', 'actual_costs': {}, 'error': f'{type(exc).__name__}: {exc}',
                       'reason': 'Executor raised after dispatch; execution cannot be disproved'}
        # A durable receipt precedes settlement; recovery can discover this exact
        # effect binding even if the following SQLite commit is interrupted.
        receipt_ref = self.store.put_blob(receipt)
        envelope = {'effect_id': effect_id, 'packet_hash': effect['packet_hash'], 'receipt_ref': receipt_ref}
        publish_envelope(self.store.root / ('receipt_' + effect_id + '.json'), envelope)
        return self.settle_effect(run_id, effect_id, receipt_ref, 'settle:' + effect_id)

    def cancel_prepared(self, run_id, effect_id, reason, key):
        """Cancel a provably undispatched intent; unknown attempts cannot use this path."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Prepared cancellation requires an explicit reason')
        self.store.verify_history(run_id)
        payload = {'effect_id': effect_id, 'reason': reason}
        def cancel(tx):
            effect = tx.get('effect', effect_id)
            if effect['status'] != 'prepared':
                raise ValueError('Only an undispatched prepared Effect can be cancelled')
            work = tx.get('work', effect['work_id'])
            if work['status'] != 'leased' or work['effect_id'] != effect_id:
                raise ValueError('Prepared cancellation no longer owns its Work lease')
            costs = {resource: 0 for resource in effect['reservations']}
            receipt_ref = tx.blob({'status': 'failed_definite', 'effect_id': effect_id,
                'packet_hash': effect['packet_hash'], 'actual_costs': costs,
                'reason': reason, 'never_dispatched': True,
                'provenance': 'controller_prepared_state_transaction'})
            for resource, amount in effect['held_reservations'].items():
                budget = tx.get('budget', resource)
                tx.update('budget', resource, {'reserved': budget['reserved'] - amount})
            result = tx.update('effect', effect_id, {'status': 'failed_definite',
                'actual_costs': costs, 'held_reservations': {resource: 0 for resource in effect['reservations']},
                'accounting_unknown': False, 'receipt_ref': receipt_ref, 'result_ref': receipt_ref,
                'never_dispatched': True, 'cancellation_reason': reason})
            tx.update('work', work['id'], {'status': 'cancelled', 'outcome_ref': receipt_ref})
            if effect['metadata'].get('agent_turn_id'):
                from .agent_state import AgentState
                AgentState(self.store, run_id).cancel_turn_tx(tx, effect['metadata']['agent_turn_id'], reason)
            self._capacity(tx)
            return result
        return self.store.mutate(run_id, key, 'prepared_effect_cancelled', cancel, payload=payload)

    def settle_effect(self, run_id, effect_id, receipt_ref, key):
        receipt = json.loads(self.store.read_blob(receipt_ref))
        status = receipt.get('status')
        costs = receipt.get('actual_costs', {})
        if status not in {'succeeded', 'failed_definite', 'unknown'} or not isinstance(costs, dict) or any(not finite(v) for v in costs.values()):
            raise ValueError('Unverifiable external status or resource receipt')
        def apply(tx):
            effect = tx.get('effect', effect_id)
            journal_replacement = False
            writer_exit_upgrade = False
            if (effect['status'] in {'succeeded', 'failed_definite'}
                    and effect.get('result_ref') and receipt.get('recovered_from_journal') is True):
                previous_result = json.loads(self.store.read_blob(effect['result_ref']))
                if previous_result.get('recovered_from_journal') is True:
                    binding_fields = ('response', 'sent_packet_sha256', 'sent_prompt_sha256',
                                      'provider_thread_id', 'status')
                    if any(previous_result.get(field) != receipt.get(field) for field in binding_fields):
                        raise ValueError('Journal reconciliation contradicts the known execution result')
                    if (previous_result.get('protocol_valid') is False
                            and receipt.get('protocol_valid') is True):
                        raise ValueError('Journal reconciliation cannot upgrade a rejected protocol')
                    if (previous_result.get('native_writer_quiesced') is True
                            and receipt.get('native_writer_quiesced') is not True):
                        raise ValueError('Journal reconciliation cannot revoke durable writer exit')
                    journal_replacement = True
                    writer_exit_upgrade = (previous_result.get('recovery_requires_fencing') is True
                        and receipt.get('native_writer_quiesced') is True
                        and receipt.get('recovery_requires_fencing') is False)
            if (effect['status'] not in {'dispatching', 'unknown'}
                    and not effect.get('accounting_unknown') and not writer_exit_upgrade):
                raise ValueError('External effect has already settled or never dispatched')
            if effect['status'] in {'succeeded', 'failed_definite'} and status != effect['status']:
                raise ValueError('Accounting reconciliation cannot rewrite a known execution outcome')
            held = dict(effect['held_reservations'])
            actual = dict(effect.get('actual_costs', {}))
            for resource in costs:
                if resource not in effect['reservations']:
                    raise ValueError('Executor reported unreserved resource consumption')
                if resource in actual and costs[resource] != actual[resource]:
                    raise ValueError('Reconciliation contradicts already settled consumption')
                if resource not in actual:
                    budget = tx.get('budget', resource)
                    spent = budget['spent'] + costs[resource]
                    # Unknown execution retains the remaining worst-case hold.
                    remaining = max(0, held[resource] - costs[resource]) if status == 'unknown' else 0
                    reserved = budget['reserved'] - held[resource] + remaining
                    tx.update('budget', resource, {'spent': spent, 'reserved': reserved,
                        'overrun': budget['overrun'] or spent + reserved > budget['limit'] or costs[resource] > effect['reservations'][resource]})
                    held[resource] = remaining
                    actual[resource] = costs[resource]
            # An authenticated final execution receipt need not repeat resource
            # values that were already settled. Release their residual holds;
            # absent measurements keep their own holds and accounting duty.
            if status != 'unknown':
                for resource in actual:
                    if not held.get(resource):
                        continue
                    budget = tx.get('budget', resource)
                    tx.update('budget', resource, {'reserved': budget['reserved'] - held[resource]})
                    held[resource] = 0
            unknown_cost = any(resource not in actual for resource in effect['reservations'])
            # Additional accounting does not replace the actual model/tool
            # result with a receipt that may contain no response at all. A
            # journal-only terminal can later acquire durable writer-exit
            # proof, but it must still attest the exact same native response.
            result_ref = (effect['result_ref'] if effect['status'] in {'succeeded', 'failed_definite'}
                          else receipt_ref)
            if journal_replacement:
                result_ref = receipt_ref
            result = tx.update('effect', effect_id, {'status': status, 'receipt_ref': receipt_ref,
                'result_ref': result_ref, 'actual_costs': actual, 'held_reservations': held,
                'accounting_unknown': unknown_cost,
                'provider_handle': receipt.get('provider_handle', effect.get('provider_handle'))})
            if effect['metadata'].get('agent_turn_id'):
                from .agent_state import AgentState
                AgentState(self.store, run_id).settle_request_tx(tx,
                    effect['metadata']['agent_turn_id'], effect_id, receipt)
            tx.update('work', effect['work_id'], {'status': 'blocked' if status == 'unknown' else ('succeeded' if status == 'succeeded' else 'failed'),
                'outcome_ref': result_ref})
            self._capacity(tx)
            return result
        return self.store.mutate(run_id, key, 'effect_settled', apply, payload={'effect_id': effect_id, 'receipt_ref': receipt_ref})

    def recover(self, run_id):
        self.store.verify_history(run_id)
        recovered = []
        for effect in self.store.list(run_id, 'effect'):
            if effect['status'] not in {'dispatching', 'unknown'}:
                continue
            envelope = read_envelope(self.store.root / ('receipt_' + effect['id'] + '.json'))
            if envelope is not None:
                if envelope['effect_id'] != effect['id'] or envelope['packet_hash'] != effect['packet_hash']:
                    raise ValueError('Recovery receipt belongs to a different attempt/input')
                recovered.append(self.settle_effect(run_id, effect['id'], envelope['receipt_ref'], 'settle:' + effect['id']))
            elif effect['status'] == 'dispatching':
                receipt = self.store.put_blob({'status': 'unknown', 'actual_costs': {}, 'reason': 'blocked_external: no durable receipt/provider query'})
                recovered.append(self.settle_effect(run_id, effect['id'], receipt, 'recovery_unknown:' + effect['id']))
        # Also finish known terminal Work transitions that predate claim cleanup.
        terminal = []
        for claim in self.store.list(run_id, 'registration', kind='work_identity_claim', status='held'):
            owners = self.store.list(run_id, 'work', id=claim['owner_work_id'])
            if not owners:
                continue
            work = owners[0]
            effect = self.store.get(run_id, 'effect', work['effect_id']) if work.get('effect_id') else None
            ready = effect is None and work['status'] in {'failed', 'cancelled', 'superseded'}
            if effect and not effect.get('accounting_unknown'):
                ready |= effect['status'] in {'failed_definite', 'cancelled'} or (
                    effect['status'] == 'succeeded' and bool(self.store.list(run_id, 'registration',
                        id=identity('publication', effect['id']), status='complete')))
            if ready:
                terminal.append((claim['id'], claim['version']))
        if terminal:
            from .work_claims import reconcile_all
            self.store.mutate(run_id, 'recover-claims:' + digest(sorted(terminal)), 'work_claims_reconciled',
                reconcile_all, payload={'terminal_claims': sorted(terminal)})
        return recovered

    def reconcile(self, run_id, effect_id, receipt, key):
        """Explicit authenticated provider/operator recovery, never an executor retry."""
        if receipt.get('reconciliation', {}).get('effect_id') != effect_id or not receipt['reconciliation'].get('provenance'):
            raise ValueError('Provider/operator reconciliation must bind this effect and provenance')
        # Eligibility is checked inside the idempotent settlement transaction.
        # A retry after its successful commit must return that existing result.
        return self.settle_effect(run_id, effect_id, self.store.put_blob(receipt), key)

    def close_issue(self, run_id, issue_id, outcome, reason, key, evidence_ids=()):
        if outcome not in {'resolved', 'declined', 'closed_unresolved'} or not reason:
            raise ValueError('Explicit issue outcome/reason required')
        payload = dict(issue_id=issue_id, outcome=outcome, reason=reason, evidence_ids=list(evidence_ids))
        def apply(tx):
            issue = tx.get('issue', issue_id)
            def related_work(work):
                return work['issue_id'] == issue_id or work.get('inputs', {}).get('issue_id') == issue_id
            associated = [effect for effect in tx.find('effect') if
                effect['issue_id'] == issue_id or effect.get('metadata', {}).get('issue_id') == issue_id
                or related_work(tx.get('work', effect['work_id']))]
            if any(e['status'] in UNSETTLED or e['accounting_unknown'] for e in associated):
                raise ValueError('Closing cannot erase execution/accounting obligations')
            if any(t['status'] not in {'settled', 'rejected', 'aborted'} for t in tx.find('trial')
                   if t.get('issue_id') == issue_id):
                raise ValueError('Closing cannot erase an active trial/restoration obligation')
            for fid in evidence_ids:
                tx.get('fact', fid)
            if outcome == 'resolved' and not evidence_ids:
                raise ValueError('Resolved requires actual supporting evidence, not elapsed time')
            for kind, states in [('work', {'ready', 'blocked'}), ('request', {'created', 'delivered'}),
                                 ('wait', {'waiting', 'disposition_due', 'disposed'}), ('question', {'pending'})]:
                for row in tx.find(kind, issue_id=issue_id):
                    if row['status'] in states:
                        tx.update(kind, row['id'], {'status': 'cancelled' if kind != 'wait' else 'superseded'})
            # Producer work is deliberately not scoped to issue scheduling
            # revisions, but closing the issue still owns its outstanding work.
            for work in tx.find('work', issue_id=None):
                if related_work(work) and work['status'] in {'ready', 'blocked'}:
                    tx.update('work', work['id'], {'status': 'cancelled'})
            self._capacity(tx)
            return tx.update('issue', issue_id, {'status': outcome, 'conclusion_ref': tx.blob(payload)}, expected_version=issue['version'])
        return self.store.mutate(run_id, key, 'issue_closed', apply, payload=payload)
