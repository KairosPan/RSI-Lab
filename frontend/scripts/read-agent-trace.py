#!/usr/bin/env python3
"""Read-only RSI trace projection. Source ledger and immutable objects are never written.

WAL-mode SQLite may create shared-memory files even with mode=ro. We instead
copy a stable DB/WAL pair into a temporary directory, then query that copy in
a read transaction. A changing source fails closed after three attempts.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

MAX_DB = 64 * 1024 * 1024
MAX_BLOB = 64 * 1024 * 1024
PREVIEW = 24000
MODEL_KINDS = {'meta', 'engineering_model', 'lab', 'lab_model'}
EFFECT_ID = re.compile(r'effect_[a-f0-9]{24}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
SECRETS = re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|(?i:Bearer\s+)[A-Za-z0-9._~+/-]{24,}')


def encoded(value):
    return clean(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False))


def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def clean(text):
    return SECRETS.sub('[REDACTED CREDENTIAL]', str(text))


def utc(value):
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat() if value is not None else None


def write_json_atomic(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def fingerprint(path):
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError('Symlink source files are unsupported')
    s = path.stat()
    if not path.is_file() or s.st_size > MAX_DB:
        raise ValueError('Ledger snapshot exceeds the reader limit')
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def read_ledger(root):
    source = root / 'ledger.sqlite3'
    wal = root / 'ledger.sqlite3-wal'
    for _ in range(3):
        before = [fingerprint(p) for p in (source, wal)]
        if before[0] is None:
            raise ValueError('Ledger missing')
        with tempfile.TemporaryDirectory(prefix='rsi-trace-read-') as temporary:
            copied = Path(temporary) / 'ledger.sqlite3'
            try:
                shutil.copyfile(source, copied)
                if before[1] is not None:
                    shutil.copyfile(wal, Path(str(copied) + '-wal'))
            except FileNotFoundError:
                continue
            if before != [fingerprint(p) for p in (source, wal)]:
                continue
            db = sqlite3.connect(copied.as_uri() + '?mode=ro', uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                runs = [dict(r) for r in db.execute('SELECT * FROM runs')]
                if len(runs) != 1:
                    raise ValueError('A single-run ledger is required')
                run = runs[0]
                events = [dict(r) for r in db.execute('SELECT * FROM events WHERE run_id=? ORDER BY seq', (run['id'],))]
                kinds = ('effect', 'proposal', 'registration', 'fact', 'issue', 'wait', 'budget')
                tables = {kind: [json.loads(r[0]) for r in db.execute(
                    'SELECT document FROM v2_' + kind + ' WHERE run_id=? ORDER BY id', (run['id'],))] for kind in kinds}
                available = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                # Older traces may lack orchestration projections. Missing is
                # distinct from an existing table with no recorded rows.
                for kind in ('work', 'dependency', 'question'):
                    tables[kind] = [json.loads(r[0]) for r in db.execute(
                        'SELECT document FROM v2_' + kind + ' WHERE run_id=? ORDER BY id', (run['id'],))] if 'v2_' + kind in available else None
                db.execute('ROLLBACK')
            finally:
                db.close()
            if before != [fingerprint(p) for p in (source, wal)]:
                continue
            return run, events, tables
    raise ValueError('Ledger changed during snapshot; retry later')


class Trace:
    def __init__(self, root, mode='ledger'):
        self.root = Path(root).resolve(strict=True)
        self.mode = mode
        self.run, self.events, self.tables = read_ledger(self.root)
        self.by_seq = {e['seq']: e for e in self.events}
        self.effects = sorted(self.tables['effect'], key=lambda e: e['created_seq'])
        self.by_effect = {e['id']: e for e in self.effects}
        self.cache = {}
        self.proposals = {}
        self.links = {}
        for p in self.tables['proposal']:
            self.proposals.setdefault(p['effect_id'], []).append(p)
            if p.get('status') != 'applied' or not p.get('result_ref'):
                continue
            result = self.blob_json(p['result_ref'])
            for key in ('build_intent_id', 'request_id', 'work_id'):
                if result.get(key):
                    self.links[result[key]] = p['effect_id']

    def blob(self, reference):
        if not isinstance(reference, str) or not SHA.fullmatch(reference):
            raise ValueError('Invalid content reference')
        if reference not in self.cache:
            path = self.root / 'objects' / reference
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BLOB:
                raise ValueError('Immutable object is unavailable or too large')
            raw = path.read_bytes()
            if hash_bytes(raw) != reference:
                raise ValueError('Immutable object hash mismatch')
            self.cache[reference] = raw
        return self.cache[reference]

    def blob_json(self, reference):
        return json.loads(self.blob(reference))

    def data(self, e):
        packet = self.blob_json(e['packet_ref'])
        receipt = self.blob_json(e['result_ref']) if e.get('result_ref') else {}
        return packet, receipt

    def requester(self, e):
        metadata = e.get('metadata', {})
        values = [metadata.get(k) for k in ('build_intent_id', 'capability_build_id', 'request_id', 'dependency_id')]
        request = metadata.get('capability_request_id', '')
        if request.startswith('acceptance:'):
            values.append(request[len('acceptance:'):])
        for value in values:
            if value in self.links:
                return self.links[value]
        return None

    def usage(self, e, receipt):
        actual = e.get('actual_costs', {})
        provider = receipt.get('usage') or {}
        return {'tokens': actual.get('tokens'), 'inputTokens': provider.get('input_tokens'),
                'outputTokens': provider.get('output_tokens'), 'cachedInputTokens': provider.get('cached_input_tokens'),
                'wallSeconds': actual.get('wall_seconds'), 'cpuSeconds': actual.get('cpu_seconds'),
                'toolCalls': actual.get('tool_calls'), 'modelCalls': actual.get('model_calls'),
                'reservedTokens': e.get('held_reservations', {}).get('tokens', 0),
                'accountingUnknown': e.get('accounting_unknown', False), 'costUsd': None}

    def event_basis(self, sequence):
        event = self.by_seq.get(sequence)
        if not event:
            return None
        return {'eventSequence': event['seq'], 'eventType': event['event_type'],
                'eventHash': event['event_hash'], 'payloadRef': event.get('payload_ref'),
                'changesRef': event.get('changes_ref'), 'at': utc(event['created_at'])}

    def row_basis(self, row):
        return {'recordId': row['id'], 'createdSequence': row.get('created_seq'),
                'updatedSequence': row.get('updated_seq'),
                'createdEvent': self.event_basis(row.get('created_seq')),
                'updatedEvent': self.event_basis(row.get('updated_seq'))}

    def lab_orchestration(self):
        """Project persisted orchestration, without running admission or ticking time.

        Work.status is a ledger value, not a fresh eligibility decision. In
        particular Meta/engineering ready rows do not make the Lab dispatchable.
        """
        config = json.loads(self.run['config'])
        latest = self.events[-1] if self.events else None
        snapshot_at = latest['created_at'] if latest else None
        complete = self.tables.get('work') is not None and self.tables.get('dependency') is not None
        registrations = {r['id']: r for r in self.tables['registration']}
        dependencies = {d['id']: d for d in self.tables.get('dependency') or []}
        plans = []
        for row in self.tables['registration']:
            if row.get('kind') != 'task_plan' or not row.get('plan_ref'):
                continue
            plan = self.blob_json(row['plan_ref'])
            plans.append({'id': plan.get('task_id'), 'registrationId': row['id'], 'status': row.get('status'),
                'planRef': row['plan_ref'], 'planHash': row.get('plan_hash'), 'objective': plan.get('goal'),
                'itemCount': len(plan.get('items', [])), 'armIds': [a['arm_id'] for a in plan.get('arms', [])],
                'metrics': plan.get('metrics', []), 'success': plan.get('success'),
                'protocolVersion': plan.get('protocol', {}).get('version'),
                'capabilityVersions': plan.get('capability_versions', {}),
                'stopRule': plan.get('stop_rule'), 'missingRule': plan.get('missing_rule'),
                'provenance': self.row_basis(row)})
        case = self.blob_json(config['case_ref']) if config.get('case_ref') else {}
        works = []
        for row in self.tables.get('work') or []:
            if row.get('status') in ('succeeded', 'failed', 'cancelled', 'superseded'):
                continue
            inputs = row.get('inputs', {})
            intent = registrations.get(inputs.get('build_intent_id'), {})
            proposal = intent.get('proposal', {})
            kind = row.get('kind', '')
            role = ('engineering' if kind == 'capability_build' else 'meta' if kind in ('meta_discovery', 'meta_disposition', 'disposition')
                    else 'lab' if kind in ('lab', 'lab_model', 'lab_step') else 'task_tool' if kind == 'diagnostic' and inputs.get('capability_id') == 'docking.vina_batch'
                    else 'other')
            works.append({'id': row['id'], 'kind': kind, 'status': row.get('status'), 'role': role,
                'issueId': row.get('issue_id') or inputs.get('issue_id'), 'effectId': row.get('effect_id'),
                'attempts': row.get('attempts'), 'priority': row.get('priority'), 'inputs': inputs,
                'expectedVersions': row.get('expected_versions', {}), 'outcomeRef': row.get('outcome_ref'),
                'trigger': row.get('trigger'), 'capabilityId': proposal.get('capability_id') or inputs.get('capability_id'),
                'purpose': proposal.get('purpose'), 'acceptanceChecks': proposal.get('acceptance_checks', []),
                'requesterCallId': self.links.get(intent.get('id')), 'provenance': self.row_basis(row)})
        works.sort(key=lambda row: row['provenance']['updatedSequence'] or 0, reverse=True)
        waits = []
        for row in self.tables['wait']:
            if row.get('status') not in ('waiting', 'disposition_due', 'disposed'):
                continue
            dep = dependencies.get(row.get('dependency_id'))
            intent = registrations.get(row.get('dependency_id'), {})
            proposal = intent.get('proposal', {})
            deadline = row.get('deadline_at')
            waits.append({'id': row['id'], 'status': row.get('status'), 'issueId': row.get('issue_id'),
                'dependencyId': row.get('dependency_id'), 'producer': row.get('producer'),
                'observedVersion': row.get('observed_version'), 'wakeCondition': row.get('wake_condition'),
                'deadlineAt': utc(deadline), 'deadlinePassedAtSnapshot': bool(deadline is not None and snapshot_at is not None and deadline <= snapshot_at),
                'expiryAction': row.get('expiry_action'), 'dispositionAttempted': row.get('disposition_attempted'),
                'dispositionWorkId': row.get('disposition_work_id'), 'waitEpoch': row.get('wait_epoch'),
                'purpose': proposal.get('purpose'), 'capabilityId': proposal.get('capability_id'),
                'dependency': {'id': dep['id'], 'producer': dep.get('producer'), 'revision': dep.get('revision'),
                    'value': dep.get('value'), 'contentHash': dep.get('content_hash'), 'provenance': self.row_basis(dep)} if dep else None,
                'provenance': self.row_basis(row)})
        issues = [{'id': q['id'], 'status': q.get('status'), 'scope': q.get('scope'), 'hypothesis': q.get('hypothesis'),
            'evidenceIds': q.get('evidence_ids', []), 'competingExplanations': q.get('competing_explanations', []),
            'falsifiers': q.get('falsifiers', []), 'conclusionRef': q.get('conclusion_ref'),
            'version': q.get('version'), 'evidenceRevision': q.get('evidence_revision'),
            'schedulingRevision': q.get('scheduling_revision'), 'provenance': self.row_basis(q)} for q in self.tables['issue']]
        open_issues = [q for q in issues if q['status'] in ('open', 'investigating', 'parked')]
        unknown = []
        for row in self.effects:
            if row.get('status') not in ('unknown', 'dispatching') and not row.get('accounting_unknown'):
                continue
            metadata = row.get('metadata', {})
            unknown.append({'id': row['id'], 'kind': row['kind'], 'status': row['status'],
                'workId': row.get('work_id'), 'issueId': row.get('issue_id') or metadata.get('issue_id'),
                'buildIntentId': metadata.get('build_intent_id'), 'buildWorkId': metadata.get('build_work_id'),
                'accountingUnknown': row.get('accounting_unknown', False), 'heldReservations': row.get('held_reservations', {}),
                'actualCosts': row.get('actual_costs', {}), 'providerHandle': row.get('provider_handle'),
                'packetRef': row.get('packet_ref'), 'receiptRef': row.get('result_ref'),
                'provenance': self.row_basis(row)})
        checkpoints = [r for r in self.tables['registration'] if r.get('kind') == 'driver_checkpoint']
        checkpoint = None
        if checkpoints:
            row = max(checkpoints, key=lambda r: r.get('updated_seq', 0))
            result = self.blob_json(row['result_ref'])
            checkpoint = {'id': row['id'], 'status': row.get('status'), 'reason': result.get('reason'),
                'steps': result.get('steps', []), 'resultRef': row['result_ref'],
                'stale': bool(latest and row.get('updated_seq', 0) < latest['seq']),
                'note': 'Historical driver checkpoint; later ledger events may supersede its scheduling observation.',
                'provenance': self.row_basis(row)}
        ready = [w for w in works if w['status'] == 'ready']
        lab_calls = sum(e['kind'] in ('lab', 'lab_model') for e in self.effects)
        vina_calls = sum(e['kind'] == 'docking_vina_batch' or e.get('metadata', {}).get('adapter_id') == 'docking.vina_batch' for e in self.effects)
        awaiting = any(w['producer'] == 'engineering_executor' and w['status'] == 'waiting' for w in waits)
        status = 'unknown' if not complete else 'awaiting_capability' if awaiting else 'needs_reconciliation' if unknown else 'investigating' if open_issues else 'recorded'
        labels = {'unknown': 'Orchestration unavailable', 'awaiting_capability': 'Awaiting capability',
                  'needs_reconciliation': 'Needs reconciliation', 'investigating': 'Investigation recorded', 'recorded': 'Orchestration recorded'}
        requirements = []
        for w in ready:
            if w['role'] == 'engineering' and w['purpose']:
                requirements.append({'label': 'Requested capability work', 'detail': w['purpose'],
                    'checks': w['acceptanceChecks'], 'issueIds': [w['issueId']] if w['issueId'] else [],
                    'workIds': [w['id']], 'effectIds': [w['requesterCallId']] if w['requesterCallId'] else [], 'basis': w['provenance']})
        for e in unknown:
            requirements.append({'label': 'External-call accounting remains open',
                'detail': 'The recorded result is unresolved and its held reservations remain. This is a separate reconciliation duty, not evidence that all other work is globally blocked or automatically recoverable.',
                'issueIds': [e['issueId']] if e['issueId'] else [], 'workIds': [e['workId']] if e['workId'] else [],
                'effectIds': [e['id']], 'basis': e['provenance']})
        host_kinds = {'experiment_stop', 'budget_amendment', 'host_external_capacity_extension', 'host_independent_successor_review', 'explicit_user_reply_delivery'}
        host_records = [{**r, 'provenance': self.row_basis(r)} for r in self.tables['registration'] if r.get('kind') in host_kinds]
        workflow_types = {'work_enqueued', 'issue_parked', 'dependency_updated', 'engineering_requested', 'meta_proposal_applied', 'effect_settled', 'driver_checkpoint'}
        timeline = [self.event_basis(e['seq']) for e in self.events if e['event_type'] in workflow_types][-8:]
        return {'schemaVersion': 'lab_orchestration_v1', 'status': status, 'label': labels[status],
            'note': 'Persisted investigation, work and waiting state; not a claim that Lab or Vina is executing. Scientific review and external-call accounting remain separate.',
            'snapshotAt': utc(snapshot_at), 'heartbeat': False, 'snapshotBasis': self.event_basis(latest['seq']) if latest else None,
            'projectionComplete': complete,
            'task': {'objective': config.get('goal') or case.get('goal'), 'plans': plans, 'caseRef': config.get('case_ref'),
                     'plannedItems': len(case['planned_ids']) if isinstance(case.get('planned_ids'), list) else None,
                     'plannedSeedJobs': case.get('planned_seed_jobs'), 'formalSelection': case.get('formal_selection')},
            'readiness': {
                'scientific': {'status': 'unresolved' if open_issues else 'not_established',
                    'label': 'Open scientific questions' if open_issues else 'Admission not established by this projection',
                    'issueIds': [q['id'] for q in open_issues],
                    'note': 'Open issue records and requests are evidence of ongoing review; this reader does not run scientific admission checks.'},
                'execution': {'status': status, 'readyWorkByKind': dict(Counter(w['kind'] for w in ready)),
                    'labReadyWorkCount': sum(w['role'] == 'lab' for w in ready),
                    'vinaReadyWorkCount': sum(w['role'] == 'task_tool' for w in ready),
                    'unknownEffectIds': [e['id'] for e in unknown], 'automaticResumeConfirmed': False,
                    'note': 'Ready is the saved Work status, not a new dispatch decision. Meta and engineering Work are not Lab or Vina admission.'}},
            'counts': {'pendingWorks': len(works), 'readyWorks': len(ready), 'activeWaits': len(waits),
                       'openIssues': len(open_issues), 'unknownEffects': len(unknown), 'labModelCalls': lab_calls, 'vinaEffects': vina_calls},
            'stages': [
                {'id': 'plan', 'label': 'Frozen task plan', 'status': 'recorded' if plans else 'not_recorded', 'count': len(plans), 'basisIds': [p['registrationId'] for p in plans]},
                {'id': 'review', 'label': 'Scientific review', 'status': 'unresolved' if open_issues else 'not_established', 'count': len(open_issues), 'basisIds': [q['id'] for q in open_issues]},
                {'id': 'lab', 'label': 'Lab model calls', 'status': 'recorded' if lab_calls else 'not_recorded', 'count': lab_calls},
                {'id': 'vina', 'label': 'Vina execution Effects', 'status': 'recorded' if vina_calls else 'not_recorded', 'count': vina_calls},
                {'id': 'output', 'label': 'Screening output', 'status': 'not_assessed', 'count': None,
                 'note': 'This projection does not validate final screening output or infer it from model/tool counts.'}],
            'pendingWorks': works, 'activeWaits': waits, 'issues': issues, 'unknownEffects': unknown,
            'activeWaitScopeNote': 'Unterminated waiting obligations include waiting, disposition_due and disposed. Disposed means its one disposition attempt occurred; the controller still permits a later dependency change to wake it.',
            'latestDriverCheckpoint': checkpoint, 'resumeRequirements': requirements, 'recentTimeline': timeline,
            'hostInterventions': host_records, 'budgets': self.tables['budget'],
            'limits': ['No runtime Store, recovery, tick, admission or model/tool execution is invoked.',
                       'Deadlines are compared only with snapshot time; elapsed time does not record expiry or disposition.',
                       'Historical stop/checkpoint and host overrides remain visible without treating them as current global status.',
                       'Budget overrun flags are saved observations; a past overrun does not prove current capacity exhaustion.']}

    def call(self, e):
        packet, receipt = self.data(e)
        proposals = self.proposals.get(e['id'], [])
        proposal = proposals[-1] if proposals else {}
        action = (proposal.get('command') or {}).get('action')
        metadata = e.get('metadata', {})
        agent = 'meta' if e['kind'] == 'meta' else 'engineer' if e['kind'] == 'engineering_model' else 'lab' if e['kind'] in {'lab', 'lab_model'} else 'tools'
        category = 'model' if e['kind'] in MODEL_KINDS else 'tool'
        title = action or packet.get('name') or metadata.get('adapter_id') or e['kind']
        summary = (proposal.get('command') or {}).get('payload', {}).get('purpose')
        if not summary:
            summary = receipt.get('error') or metadata.get('scope') or packet.get('build_intent', {}).get('purpose') or ''
        if not summary and category == 'tool':
            summary = packet.get('request', {}).get('query') or receipt.get('authority') or ''
        requester = self.requester(e)
        return {'id': e['id'], 'agentId': agent, 'category': category, 'kind': e['kind'],
                'title': clean(title), 'mode': metadata.get('mode'), 'model': packet.get('model'),
                'action': action, 'status': e['status'], 'outcomeStatus': proposal.get('status'),
                'createdAt': utc(self.by_seq[e['created_seq']]['created_at']),
                'finishedAt': utc(self.by_seq[e['updated_seq']]['created_at']) if e['status'] not in ('prepared', 'dispatching') else None,
                'sequence': e['created_seq'], 'usage': self.usage(e, receipt),
                'summary': clean(summary)[:650], 'issueId': e.get('issue_id') or metadata.get('issue_id'),
                'requestedBy': 'meta' if requester else None, 'requesterCallId': requester,
                'requestLinkBasis': 'Exact recorded request/build intent identity' if requester else None,
                'tool': {'name': title} if category == 'tool' else None,
                'detailUrl': ('/data/agent-trace/calls/' if self.mode == 'recorded' else '/api/trace/calls/') + e['id'] + ('.json' if self.mode == 'recorded' else '')}

    def index(self):
        calls = [self.call(e) for e in self.effects]
        agents = []
        definitions = [
            ('controller', 'Controller', 'service', '调度与预算', '代码控制器管理请求、预算、等待、验证和恢复。'),
            ('lab', 'Lab agent', 'agent', '任务执行', '在已准入的科学协议内选择候选并推进筛选；本轮尚无 Lab 模型调用。'),
            ('meta', 'Meta agent', 'agent', '发现 · 调查 · 处置', '同一个 Meta-agent 按需切换发现与处置模式；这些模式不是独立 agent。'),
            ('engineer', 'Engineering agent', 'agent', '能力建设', '真实工程模型提出代码，宿主审查与实际测试单独记录。'),
            ('tools', 'Tool executor', 'service', '诊断与验证', '实际来源读取、准备诊断、能力验收和宿主工具调用。'),
            ('memory', 'Evidence ledger', 'service', '证据与记录', '保存原始事实、来源陈述、验证结论及不可变引用。')]
        for aid, name, kind, role, description in definitions:
            own = [c for c in calls if c['agentId'] == aid]
            linked = [c for c in calls if c['category'] == 'tool' and c['requestedBy'] == aid]
            unknown = sum(c['status'] == 'unknown' or c['usage']['accountingUnknown'] for c in own)
            status = 'unknown' if unknown else 'awaiting' if aid == 'lab' and not own else 'recorded'
            agents.append({'id': aid, 'name': name, 'kind': kind, 'role': role, 'description': description,
                'status': status, 'lastActivity': own[-1]['finishedAt'] if own else utc(self.events[-1]['created_at']) if aid in {'controller', 'memory'} and self.events else None,
                'counts': {'calls': len(own), 'tools': len(own) if aid == 'tools' else len(linked),
                           'tokens': sum(c['usage']['tokens'] or 0 for c in own), 'unknown': unknown,
                           'toolUnits': sum(c['usage']['toolCalls'] or 0 for c in (own if aid == 'tools' else linked)),
                           'events': len(self.events) if aid == 'controller' else len(self.tables['fact']) if aid == 'memory' else 0},
                'emptyReason': '没有记录到该 agent 的对话；不会用示例消息补齐。' if not own else None})
        latest = self.events[-1] if self.events else None
        summary = {'effects': len(calls), 'modelCalls': sum(c['category'] == 'model' for c in calls),
            'toolEffects': sum(c['category'] == 'tool' for c in calls),
            'toolCalls': sum(c['usage']['toolCalls'] or 0 for c in calls),
            'knownTokens': sum(c['usage']['tokens'] or 0 for c in calls),
            'reservedUnknownTokens': sum(c['usage']['reservedTokens'] for c in calls if c['status'] == 'unknown' or c['usage']['accountingUnknown']),
            'unknownEffects': sum(c['status'] == 'unknown' or c['usage']['accountingUnknown'] for c in calls),
            'wallSeconds': sum(c['usage']['wallSeconds'] or 0 for c in calls),
            'cpuSeconds': sum(c['usage']['cpuSeconds'] or 0 for c in calls), 'costUsd': None,
            'toolUnitNote': 'Tool calls 为账本计费单位；不同工具包含的内部检查数量不同。',
            'costNote': '仅含账本已测调用；未知 token、宿主会话和美元费用不能记为零。'}
        return {'schemaVersion': 'agent_trace_v1',
            'run': {'id': self.run['id'], 'title': 'RSI-Lab · ' + self.run['id'],
                'source': self.mode, 'sourceLabel': '已归档快照' if self.mode == 'recorded' else '读取本地账本',
                'updatedAt': utc(latest['created_at']) if latest else None, 'eventCount': len(self.events),
                'lastEventHash': latest['event_hash'] if latest else None,
                'status': 'unresolved' if summary['unknownEffects'] else 'recorded', 'heartbeat': False,
                'usage': summary, 'description': '探索任务的真实调用记录；不是当前进程心跳。'},
            'summary': summary, 'agents': agents, 'calls': calls, 'labOrchestration': self.lab_orchestration(),
            'events': [{'id': 'event_' + str(e['seq']), 'sequence': e['seq'], 'kind': e['event_type'],
                        'createdAt': utc(e['created_at']), 'agentId': 'controller', 'payloadRef': e['payload_ref']} for e in self.events],
            'facts': [{'id': f['id'], 'kind': f['kind'], 'contentRef': f['content_ref'],
                       'createdAt': utc(self.by_seq[f['created_seq']]['created_at'])} for f in self.tables['fact']],
            'issues': [{'id': q['id'], 'status': q['status'], 'scope': q.get('scope'), 'hypothesis': clean(q.get('hypothesis', ''))} for q in self.tables['issue']],
            'budgets': self.tables['budget'],
            'notes': ['节点表示执行职责；服务节点不是语言模型 agent。', '记录时间不代表当前心跳；刷新不会启动任何模型或工具。',
                      '仅展示记录的输入、最终响应和工具回执，不展示或推测模型隐藏思维链。']}

    def artifact_refs(self, e, packet, receipt):
        refs = {'packet.json': e['packet_ref']}
        if e.get('result_ref'):
            refs['receipt.json'] = e['result_ref']
        for group in ('artifacts', 'transport_artifacts'):
            for name, ref in receipt.get(group, {}).items():
                if isinstance(ref, str) and SHA.fullmatch(ref):
                    refs[name] = ref
        return refs

    def model_text(self, e, packet, receipt):
        artifacts = receipt.get('transport_artifacts', {})
        prompt = self.blob(artifacts['prompt.txt']).decode() if artifacts.get('prompt.txt') else None
        response = self.blob(artifacts['raw_response.txt']).decode() if artifacts.get('raw_response.txt') else None
        folder = self.root / ('engineering_calls' if e['kind'] == 'engineering_model' else 'model_calls') / e['id']
        if prompt is None:
            path = folder / 'prompt.txt'
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BLOB:
                raw = path.read_bytes()
                if hash_bytes(raw) == receipt.get('sent_prompt_sha256'):
                    prompt = raw.decode()
        if response is None:
            path = folder / 'raw_response.txt'
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BLOB:
                raw = path.read_text()
                try:
                    if receipt.get('response') is not None and json.loads(raw) == receipt['response']:
                        response = raw
                except (ValueError, TypeError):
                    pass
        return prompt or packet.get('prompt', ''), response, prompt is not None

    def detail(self, eid):
        if not EFFECT_ID.fullmatch(eid) or eid not in self.by_effect:
            raise ValueError('Unknown call identity')
        e = self.by_effect[eid]
        packet, receipt = self.data(e)
        call = self.call(e)
        messages, tool, truncation = [], None, {}
        refs = self.artifact_refs(e, packet, receipt)
        artifacts = [{'name': name, 'sha256': ref, 'bytes': len(self.blob(ref)),
                      'downloadUrl': '/api/trace/artifacts/' + eid + '/' + ref,
                      'availability': 'Requires the local server with RSI_LAB_RUN_DIR'} for name, ref in refs.items()]
        if e['kind'] in MODEL_KINDS:
            prompt, response, captured = self.model_text(e, packet, receipt)
            messages.append({'role': 'user', 'label': '完整调用输入 / 上下文' if captured else '已保存的请求输入', 'content': clean(prompt), 'format': 'text',
                             'source': 'recorded transport prompt' if captured else 'packet prompt; transport prompt unavailable',
                             'transportPromptAvailable': captured, 'truncated': False, 'redacted': clean(prompt) != prompt})
            if response is not None and response.strip():
                messages.append({'role': 'assistant', 'label': '模型原始响应', 'content': clean(response), 'format': 'json',
                                 'source': 'recorded final response', 'truncated': False, 'redacted': clean(response) != response})
            elif receipt.get('response') is not None:
                response = json.dumps(receipt['response'], ensure_ascii=False, indent=2)
                messages.append({'role': 'assistant', 'label': '已归档结构化响应', 'content': clean(response), 'format': 'json',
                                 'source': 'receipt response, serialized for display', 'truncated': False})
            else:
                messages.append({'role': 'system', 'label': '未取得最终响应', 'content': clean(receipt.get('error') or 'No final response is recorded.'), 'format': 'text'})
        else:
            def preview(name, value):
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
                text = clean(text)
                truncation[name] = {'truncated': len(text) > PREVIEW, 'characters': len(text), 'shownCharacters': min(len(text), PREVIEW)}
                return text[:PREVIEW]
            stdout = receipt.get('stdout') or ''
            stderr = receipt.get('stderr') or ''
            if receipt.get('artifacts', {}).get('stdout.txt'):
                stdout = self.blob(receipt['artifacts']['stdout.txt']).decode('utf-8', errors='replace')
            if receipt.get('artifacts', {}).get('stderr.txt'):
                stderr = self.blob(receipt['artifacts']['stderr.txt']).decode('utf-8', errors='replace')
            result = receipt.get('result', receipt.get('capability_acceptance', {'status': receipt.get('status'), 'returncode': receipt.get('returncode'), 'error': receipt.get('error')}))
            tool = {'name': call['title'], 'args': preview('args', packet), 'result': preview('result', result),
                    'stdout': preview('stdout', stdout), 'stderr': preview('stderr', stderr),
                    'error': clean(receipt.get('error')) if receipt.get('error') else None,
                    'returncode': receipt.get('returncode'), 'truncation': truncation}
        actions = [{'id': p['id'], 'status': p['status'], 'error': p.get('error'),
                    'command': p.get('command'), 'result': self.blob_json(p['result_ref']) if p.get('result_ref') else None}
                   for p in self.proposals.get(eid, [])]
        related = [other['id'] for other in self.effects if self.requester(other) == eid]
        return {**call, 'messages': messages, 'tool': tool, 'actions': actions, 'relatedCallIds': related,
                'artifacts': artifacts, 'provenance': {'packetSha256': e['packet_ref'], 'receiptSha256': e.get('result_ref'),
                    'source': self.mode, 'originalEffect': e['id'], 'usageComplete': receipt.get('usage_complete'),
                    'heldReservations': e.get('held_reservations'), 'metadata': e.get('metadata'),
                    'scope': 'Observed final messages and receipts; proposals are not executed tool calls.'}}

    def artifact(self, eid, ref):
        if not EFFECT_ID.fullmatch(eid) or eid not in self.by_effect or not SHA.fullmatch(ref):
            raise ValueError('Unknown artifact identity')
        e = self.by_effect[eid]
        packet, receipt = self.data(e)
        if ref not in self.artifact_refs(e, packet, receipt).values():
            raise ValueError('Artifact is not bound to this call')
        raw = self.blob(ref)
        if SECRETS.search(raw.decode('utf-8', errors='replace')):
            raise ValueError('Credential-bearing artifact download is unavailable')
        return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--mode', choices=('ledger', 'recorded'), default='ledger')
    parser.add_argument('--call')
    parser.add_argument('--artifact')
    parser.add_argument('--export', type=Path)
    args = parser.parse_args()
    trace = Trace(args.run, args.mode)
    if args.artifact:
        if not args.call:
            raise ValueError('Artifact requires a call identity')
        sys.stdout.buffer.write(trace.artifact(args.call, args.artifact))
    elif args.export:
        args.export.mkdir(parents=True, exist_ok=True)
        calls = args.export / 'calls'
        calls.mkdir(exist_ok=True)
        index = trace.index()
        for e in trace.effects:
            write_json_atomic(calls / (e['id'] + '.json'), trace.detail(e['id']))
        write_json_atomic(args.export / 'index.json', index)
        print(encoded({'calls': len(trace.effects), 'events': len(trace.events), 'output': str(args.export)}))
    else:
        print(encoded(trace.detail(args.call) if args.call else trace.index()))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        print('Trace source is unavailable: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
