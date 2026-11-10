"""Start a PABLO segment, verifying a narrowly supported historical schema-repair chain.

The parent's ledger, budgets, source files, and native sessions remain unchanged.
Only observations and role-owned unverified working memory cross this boundary.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts import run_pablo_rsi as host
from rsi_lab.controller import Controller, identity
from rsi_lab.plans import register_task_plan
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode
from rsi_domains.docking.live_sources import freeze_sources, source_contracts
from rsi_domains.pablo import adapter, oracle


class ReadOnlySnapshot(Store):
    """Read a closed WAL database without creating a journal or shared memory."""
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.blobs = self.root / 'objects'
        path = self.root / 'ledger.sqlite3'
        if not path.is_file() or not self.blobs.is_dir():
            raise ValueError('A complete existing parent ledger is required')
        if Path(str(path) + '-wal').exists():
            raise ValueError('Parent WAL is still present; close/reconcile its writer before continuing')
        self.db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True,
                                  isolation_level=None)
        self.db.row_factory = sqlite3.Row


def checked_source_hashes(values):
    if not isinstance(values, dict) or not values:
        raise ValueError('Parent has no frozen source manifest')
    for name, expected in values.items():
        path = (REPO / name).resolve()
        if not path.is_relative_to(REPO) or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError('Parent frozen source differs from current code: ' + name)


def _sum_costs(rows):
    total = {}
    for row in rows:
        for name, value in row.items():
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Historical costs must be explicit finite nonnegative values')
            total[name] = total.get(name, 0) + value
    return total


def _event_proof(store, run_id, seq, key, event_type, kind, entity_id):
    row = store.db.execute('SELECT * FROM events WHERE run_id=? AND seq=?', (run_id, seq)).fetchone()
    if row is None or row['operation_key'] != key or row['event_type'] != event_type:
        raise ValueError('Rejected proposal lacks its authenticated repair event chain')
    changes = json.loads(store.read_blob(row['changes_ref']))
    if not any(c['kind'] == kind and c['document']['id'] == entity_id for c in changes):
        raise ValueError('Repair event does not change its claimed entity')
    return {'seq': seq, 'event_hash': row['event_hash'], 'operation_key': key, 'event_type': event_type}


def _settled_meta_source(store, run_id, effect_id, issue_id):
    effect = store.get(run_id, 'effect', effect_id)
    if (effect['kind'] != 'meta' or effect['issue_id'] != issue_id or effect['status'] != 'succeeded'
            or effect.get('accounting_unknown') is not False
            or any(effect.get('held_reservations', {}).values())
            or set(effect.get('reservations', {})) - set(effect.get('actual_costs', {}))):
        raise ValueError('Historical rejection repair requires a settled successful Meta effect')
    _sum_costs([effect['actual_costs']])
    receipt = json.loads(store.read_blob(effect['result_ref']))
    packet = json.loads(store.read_blob(effect['packet_ref']))
    if (receipt.get('status') != 'succeeded'
            or any(receipt.get(k) is not True for k in ('terminal_received', 'protocol_valid', 'native_writer_quiesced'))
            or receipt.get('recovery_requires_fencing')
            or receipt.get('sent_packet_sha256') != effect['packet_ref']
            or receipt.get('sent_prompt_sha256') != digest(packet['prompt'].encode('utf-8'))):
        raise ValueError('Historical rejection repair lacks a certified terminal receipt')
    # Accounting-only reconciliation may differ from the original terminal receipt.
    store.read_blob(effect['receipt_ref'])
    work = store.get(run_id, 'work', effect['work_id'])
    turn = store.get(run_id, 'agent_turn', effect['metadata']['agent_turn_id'])
    session = store.get(run_id, 'agent_session', effect['metadata']['agent_session_id'])
    settled = turn.get('request_results', {}).get(effect_id, {})
    if (work['status'] != 'succeeded' or work.get('effect_id') != effect_id or work['issue_id'] != issue_id
            or turn['status'] != 'committed' or turn['work_id'] != work['id']
            or effect_id not in turn['effect_ids'] or turn['terminal_ref'] != effect['result_ref']
            or turn['session_id'] != session['id'] or turn['owner_id'] != session['owner_id']
            or settled.get('status') != 'succeeded'
            or session['status'] not in {'idle', 'superseded'} or session['active_turn_id'] is not None):
        raise ValueError('Historical rejection repair has an unsettled Work or AgentTurn')
    return effect, receipt, work, turn


def rejection_dispositions(store, run_id):
    """Recognize only the observed pre-route empty-query rejection and its complete core repair.

    This never changes rejected to applied. Unknown execution, other schema errors,
    partial routes, missing raw evidence, and unresolved obligations remain blockers.
    """
    dispositions, archived = [], {}
    proposals = store.list(run_id, 'proposal')
    for proposal in proposals:
        if proposal['status'] == 'applied':
            continue
        pid, iid = proposal['id'], proposal.get('issue_id')
        if (proposal['status'] != 'rejected' or proposal.get('command') is not None
                or proposal.get('result_ref') is not None or not iid
                or proposal.get('error') != 'Command text cannot be empty'
                or proposal['created_seq'] != proposal['updated_seq'] or proposal['version'] != 1):
            raise ValueError('Parent has an unapplied proposal without a verified historical repair')
        effect, receipt, work, turn = _settled_meta_source(store, run_id, proposal['effect_id'], iid)
        command = receipt.get('response', {}).get('command', {})
        if (command.get('action') != 'request_evidence' or command.get('issue_id') != iid
                or command.get('payload', {}).get('query') != ''):
            raise ValueError('Only a pre-route empty evidence query has a supported historical disposition')
        events = [_event_proof(store, run_id, proposal['created_seq'], 'admit:' + pid,
                              'meta_proposal_admitted', 'proposal', pid)]
        if any(row[0] == 'route:' + pid or row[0].startswith('route:' + pid + ':') for row in
               store.db.execute('SELECT operation_key FROM events WHERE run_id=?', (run_id,))):
            raise ValueError('Rejected proposal already has route side effects')
        store.read_blob(proposal['delivery_ref'])
        issue = store.get(run_id, 'issue', iid)
        if issue['status'] not in {'resolved', 'declined', 'closed_unresolved'}:
            raise ValueError('Historical rejection issue remains open or unresolved by disposition')
        registrations = store.list(run_id, 'registration', id='repair:' + iid)
        if (len(registrations) != 1 or registrations[0].get('proposal_id') != pid
                or registrations[0].get('reason') != proposal['error']
                or registrations[0]['created_seq'] != proposal['created_seq']):
            raise ValueError('Historical rejection has no matching core repair registration')
        registration = registrations[0]
        repair_work = store.list(run_id, 'work', issue_id=iid, trigger='repair:' + pid)
        if len(repair_work) != 1 or repair_work[0]['created_seq'] != proposal['created_seq']:
            raise ValueError('Historical rejection has no unique core repair Work')
        repaired = [p for p in proposals if p['effect_id'] == repair_work[0].get('effect_id')
                    and p['issue_id'] == iid and p['status'] == 'applied']
        if len(repaired) != 1:
            raise ValueError('Historical rejection has no applied repair proposal')
        repair = repaired[0]
        repair_effect, repair_receipt, repaired_work, repair_turn = _settled_meta_source(
            store, run_id, repair['effect_id'], iid)
        fixed = repair['command']
        expected = copy.deepcopy(command)
        query = fixed.get('payload', {}).get('query')
        expected['payload']['query'] = query
        if (not isinstance(query, str) or not query.strip() or fixed != expected
                or repair_receipt.get('response', {}).get('command') != fixed
                or repaired_work['id'] != repair_work[0]['id']
                or repair['created_seq'] <= proposal['created_seq']):
            raise ValueError('Core repair did not apply the same evidence request with a nonempty query')
        events.append(_event_proof(store, run_id, repair['updated_seq'], 'applied:' + repair['id'],
                                   'meta_proposal_applied', 'proposal', repair['id']))
        routed = json.loads(store.read_blob(repair['result_ref']))
        request = store.get(run_id, 'request', routed['request_id'])
        if (request['id'] != identity('object', repair['id']) or request['issue_id'] != iid
                or request['query_spec'] != fixed['payload'] or request['query'] != query
                or request['status'] not in {'delivered', 'cancelled'} or not request['delivery_ids']):
            raise ValueError('Applied repair has no completed evidence request')
        events.append(_event_proof(store, run_id, request['created_seq'], 'route:' + repair['id'] + ':request',
                                   'evidence_requested', 'request', request['id']))
        deliveries, facts = [], []
        for did in request['delivery_ids']:
            delivery = store.get(run_id, 'delivery', did)
            content = json.loads(store.read_blob(delivery['content_ref']))
            fact = store.get(run_id, 'fact', did)
            fact_content = json.loads(store.read_blob(fact['content_ref']))
            if (delivery['request_id'] != request['id'] or delivery['kind'] != 'raw'
                    or delivery['status'] != 'materialized' or delivery['source_version'] != request['source_version']
                    or delivery['created_seq'] <= repair['updated_seq']
                    or did not in issue['evidence_ids'] or fact['kind'] != 'raw_fact'
                    or fact_content != {'delivery_kind': 'raw', 'content': content}
                    or not isinstance(content, dict) or not isinstance(content.get('original_text'), str)
                    or not content['original_text']
                    or digest(content['original_text'].encode('utf-8')) != content.get('original_source_sha256')
                    or delivery['provenance'].get('source_id') != request['source']
                    or delivery['provenance'].get('original_source_sha256') != content['original_source_sha256']):
                raise ValueError('Applied repair lacks its original raw evidence delivery')
            if store.read_blob(content['original_source_sha256']) != content['original_text'].encode('utf-8'):
                raise ValueError('Repair evidence does not match its frozen original')
            events.append(_event_proof(store, run_id, delivery['created_seq'], 'ingest:' + did,
                                       'evidence_delivered', 'delivery', did))
            events.append(_event_proof(store, run_id, fact['created_seq'], 'attach:' + did,
                                       'delivered_evidence_attached', 'fact', did))
            deliveries.append(delivery); facts.append(fact)
        closings = [p for p in proposals if p['issue_id'] == iid and p['status'] == 'applied'
                    and p['command'].get('action') == 'close_issue'
                    and p['created_seq'] > max(d['created_seq'] for d in deliveries)]
        if len(closings) != 1:
            raise ValueError('Historical repair has no unique subsequent applied issue closure')
        closing = closings[0]
        close_effect, close_receipt, close_work, close_turn = _settled_meta_source(store, run_id, closing['effect_id'], iid)
        close_command = closing['command']
        conclusion = json.loads(store.read_blob(issue['conclusion_ref']))
        if (close_command['issue_id'] != iid or close_command['payload']['resolution'] != issue['status']
                or close_receipt.get('response', {}).get('command') != close_command
                or not set(request['delivery_ids']).issubset(close_command['evidence_ids'])
                or json.loads(store.read_blob(closing['result_ref'])) != issue
                or conclusion['issue_id'] != iid or conclusion['outcome'] != issue['status']
                or conclusion['reason'] != close_command['payload']['reason']):
            raise ValueError('Historical issue closure does not bind its repaired evidence')
        events.append(_event_proof(store, run_id, issue['updated_seq'], 'route:' + closing['id'],
                                   'issue_closed', 'issue', iid))
        events.append(_event_proof(store, run_id, closing['updated_seq'], 'applied:' + closing['id'],
                                   'meta_proposal_applied', 'proposal', closing['id']))
        related_work = [w for w in store.list(run_id, 'work')
                        if w.get('issue_id') == iid or w.get('inputs', {}).get('issue_id') == iid]
        if any(w['status'] not in {'succeeded', 'failed', 'cancelled'} for w in related_work):
            raise ValueError('Historical repair has outstanding Work obligations')
        for kind, terminal in [('request', {'delivered', 'cancelled', 'superseded'}),
                               ('wait', {'fired', 'superseded'}), ('question', {'answered', 'cancelled'}),
                               ('trial', {'settled', 'rejected', 'aborted'})]:
            if any(r['status'] not in terminal for r in store.list(run_id, kind, issue_id=iid)):
                raise ValueError('Historical repair has outstanding ' + kind + ' obligations')
        nodes = [('proposal', p) for p in (proposal, repair, closing)] + [('registration', registration), ('issue', issue), ('request', request)]
        nodes += [('effect', e) for e in (effect, repair_effect, close_effect)]
        nodes += [('agent_turn', t) for t in (turn, repair_turn, close_turn)]
        nodes += [('work', w) for w in related_work] + [('delivery', d) for d in deliveries] + [('fact', f) for f in facts]
        chain = {'schema_version': 'pablo_historical_rejection_disposition_v1', 'run_root': str(store.root),
                 'run_id': run_id, 'original_proposal': proposal, 'original_status': 'rejected',
                 'disposition': 'pre_route_empty_query_repaired_and_issue_closed',
                 'nodes': [{'kind': kind, 'id': row['id'], 'sha256': digest(row),
                            'created_seq': row['created_seq'], 'updated_seq': row['updated_seq']} for kind, row in nodes],
                 'events': events, 'repair_proposal_id': repair['id'], 'close_proposal_id': closing['id'],
                 'effect_result_refs': {e['id']: e['result_ref'] for e in (effect, repair_effect, close_effect)},
                 'cost_policy': 'All source and repair effects remain charged to their original segment ledger'}
        chain_ref = digest(chain)
        archived[chain_ref] = encode(chain)
        dispositions.append({'run_root': str(store.root), 'run_id': run_id, 'proposal_id': pid,
            'source_effect_id': effect['id'], 'issue_id': iid, 'original_status': 'rejected',
            'disposition': chain['disposition'], 'repair_chain_sha256': chain_ref})
    return dispositions, archived


def load_parent(root, run_id):
    """Validate a closed parent while holding its existing host lock read-only."""
    root = Path(root).resolve(strict=True)
    with (root / 'host.lock').open('rb') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Parent host is still running') from exc
        store = ReadOnlySnapshot(root)
        try:
            manifest_raw = (root / 'manifest.json').read_bytes()
            result_raw = (root / 'result.json').read_bytes()
            manifest, result = json.loads(manifest_raw), json.loads(result_raw)
            checked_source_hashes(manifest['source_hashes'])
            history = store.verify_history(run_id)
            run = store.run(run_id)
            config = run['config']
            if config.get('model') != 'gpt-6.1-sol' or config.get('effort') != 'medium':
                raise ValueError('Continuation requires the requested gpt-6.1-sol / medium binding')
            if config.get('pablo_oracle_fingerprint') != oracle.fingerprint():
                raise ValueError('Parent evaluator implementation or dependencies changed')
            lifecycle = store.get(run_id, 'registration', 'run_lifecycle')
            if lifecycle['status'] != 'finalizing' or result.get('status') != 'completed' or result.get('run_id') != run_id:
                raise ValueError('Only an explicitly completed, finalized parent can start a new segment')
            effects = store.list(run_id, 'effect')
            if any(e['status'] not in {'succeeded', 'failed_definite', 'cancelled'}
                   or e.get('accounting_unknown') or any(e.get('held_reservations', {}).values()) for e in effects):
                raise ValueError('Parent has unknown execution or unsettled accounting')
            budgets = store.list(run_id, 'budget')
            if any(b['reserved'] != 0 for b in budgets):
                raise ValueError('Parent budget reservations remain outstanding')
            if (store.list(run_id, 'issue', status='open') or store.list(run_id, 'question', status='pending')
                    or store.list(run_id, 'work', status='blocked')):
                raise ValueError('Parent has open issues, questions, or blocked work')
            dispositions, repair_evidence = rejection_dispositions(store, run_id)
            active_work = [w for w in store.list(run_id, 'work') if w['status'] in {'ready', 'leased'}]
            if any(w['status'] != 'ready' or w['kind'] != 'lab'
                   or not w['inputs'].get('source_effect_id') for w in active_work):
                raise ValueError('Only an unexecuted final Lab continuation may remain in the parent')
            if any(w['status'] in {'waiting', 'disposition_due', 'disposed'} for w in store.list(run_id, 'wait')):
                raise ValueError('Parent has an unresolved wait')
            progress = adapter.read_progress(store, run_id)
            maximum = config['pablo_max_new_evaluations']
            if (progress['new_evaluations'] != maximum or progress['remaining_evaluations'] != 0
                    or result.get('new_evaluations') != maximum
                    or result.get('initial_count') != progress['initial_count']):
                raise ValueError('Parent completion report differs from authenticated observations')
            values = progress['candidates']
            if (len(values) != len(set(values)) or any(oracle.canonicalize(s) != s for s in values)
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in progress['scores'])):
                raise ValueError('Inherited observations must be canonical, unique, and finite')
            memories = {}
            for owner in store.list(run_id, 'agent_owner'):
                scope = owner['scope']
                if scope['role'] not in {'lab', 'meta'} or scope['role'] in memories or scope['branch_id'] != 'main':
                    raise ValueError('Parent must have one main-branch owner per role')
                session = store.get(run_id, 'agent_session', owner['session_id'])
                if session['status'] != 'idle' or session['active_turn_id']:
                    raise ValueError('Parent current agent sessions must be idle')
                memory = store.get(run_id, 'working_memory', owner['memory_head_ref']) if owner['memory_head_ref'] else None
                if memory and (memory['owner_id'] != owner['id'] or memory['revision'] != owner['memory_revision']):
                    raise ValueError('Parent private memory head has inconsistent ownership')
                checkpoint = store.get(run_id, 'agent_checkpoint', session['checkpoint_ref'])
                if checkpoint['owner_id'] != owner['id'] or checkpoint['memory_head_ref'] != owner['memory_head_ref']:
                    raise ValueError('Parent final checkpoint differs from its current private memory')
                memories[scope['role']] = {'owner': owner, 'session': session, 'memory': memory, 'checkpoint': checkpoint}
            if set(memories) != {'lab', 'meta'}:
                raise ValueError('Both parent Lab and Meta owners are required')
            evidence = {config['pablo_initial_ref']: store.read_blob(config['pablo_initial_ref'])}
            evidence.update(repair_evidence)
            for disposition in manifest.get('continuation_lineage', {}).get('rejection_dispositions', []):
                ref = disposition['repair_chain_sha256']
                evidence[ref] = store.read_blob(ref)
            scoring = []
            for effect in effects:
                if effect.get('metadata', {}).get('adapter_id') != adapter.ADAPTER_ID:
                    continue
                if effect['status'] != 'succeeded':
                    raise ValueError('Unsuccessful historical scoring must be explicitly reconciled before continuation')
                for ref in (effect['packet_ref'], effect['result_ref']):
                    evidence[ref] = store.read_blob(ref)
                receipt = json.loads(evidence[effect['result_ref']])
                journal = root / 'pablo_evaluations' / effect['id'] / 'evaluations.jsonl'
                if journal.exists():
                    raw = journal.read_bytes()
                    if digest(raw) != receipt['execution']['journal_sha256']:
                        raise ValueError('Parent scoring journal hash differs from its receipt')
                    evidence[digest(raw)] = raw
                scoring.append({'effect_id': effect['id'], 'packet_ref': effect['packet_ref'],
                                'result_ref': effect['result_ref'], 'run_root': str(root)})
            return {'root': str(root), 'run_id': run_id, 'config': config, 'config_hash': run['config_hash'],
                    'history': history, 'manifest': manifest, 'manifest_sha256': digest(manifest_raw),
                    'result': result, 'result_sha256': digest(result_raw), 'progress': progress,
                    'budgets': budgets, 'memories': memories, 'scoring_sources': scoring,
                    'evidence_objects': evidence, 'rejection_dispositions': dispositions}
        finally:
            store.close()


def make_lineage(parent, target):
    inherited = parent['manifest'].get('continuation_lineage', {})
    segments = copy.deepcopy(inherited.get('cost_segments', []))
    segment = {'run_root': parent['root'], 'run_id': parent['run_id'],
               'history': parent['history'], 'actual_costs': {b['id']: b['spent'] for b in parent['budgets']},
               'new_evaluations': parent['progress']['new_evaluations']}
    if any((row['run_root'], row['run_id']) == (segment['run_root'], segment['run_id']) for row in segments):
        raise ValueError('Parent segment is already present in its own cost ancestry')
    segments.append(segment)
    prior_attempts = copy.deepcopy(parent['manifest'].get('prior_attempts', []))
    original_count = inherited.get('original_initial_count', parent['progress']['initial_count'])
    total_history = len(parent['progress']['candidates'])
    return {'schema_version': 'pablo_continuation_lineage_v2', 'parent_root': parent['root'],
            'parent_run_id': parent['run_id'], 'parent_config_sha256': parent['config_hash'],
            'parent_history': parent['history'], 'parent_manifest_sha256': parent['manifest_sha256'],
            'parent_result_sha256': parent['result_sha256'],
            'inherited_trajectory_sha256': parent['progress']['trajectory_sha256'],
            'original_initial_count': original_count,
            'historical_generated_evaluations': total_history - original_count,
            'inherited_observations': total_history, 'segment_new_evaluations': target,
            'cost_segments': segments, 'prior_attempts': prior_attempts,
            'prior_attempt_costs': _sum_costs(p.get('actual_costs', {}) for p in prior_attempts),
            'inherited_segment_costs': _sum_costs(s['actual_costs'] for s in segments),
            'cost_identity': 'run_root plus run_id; prior attempts retain their full summary path and effect IDs',
            'source_receipts': parent['scoring_sources'],
            'rejection_dispositions': copy.deepcopy(inherited.get('rejection_dispositions', [])) + parent['rejection_dispositions'],
            'private_memory_policy': 'Role-preserving unverified import; historical IDs grant no current read authority',
            'native_session_policy': 'Fresh owned sessions; no parent provider thread or queued work copied'}


def import_memory(runtime, role, source, parent):
    binding = {'model': runtime.config['model'], 'effort': runtime.config['effort'], 'transport': 'codex_exec_text_v1'}
    session = runtime.agents.ensure_session(role, runtime_binding=binding)
    memory = source['memory']
    content = {} if memory is None else runtime.agents._memory_delta(memory['content'], {})
    provenance = {'parent_root': parent['root'], 'parent_run_id': parent['run_id'],
                  'parent_history': parent['history'], 'parent_owner_id': source['owner']['id'],
                  'parent_session_id': source['session']['id'], 'parent_checkpoint_id': source['checkpoint']['id'],
                  'parent_memory_head_ref': source['owner']['memory_head_ref'],
                  'parent_memory_revision': source['owner']['memory_revision'],
                  'parent_memory_sha256': None if memory is None else digest(memory),
                  'parent_memory_blob_ref': None if memory is None else runtime.store.put_blob(memory),
                  'verification': 'unverified_working_state',
                  'reference_scope': 'Historical only; current task and delivered facts override old counts and IDs'}
    token = 'continuation-memory:' + role
    def apply(tx):
        owner = tx.get('agent_owner', session['owner_id'])
        if owner['memory_revision'] or owner['memory_head_ref'] or session['committed_turn'] or session['provider_thread_id']:
            raise ValueError('Memory import requires a fresh unused target owner')
        registration = tx.insert('registration', token, {'kind': 'private_memory_import',
            'status': 'imported' if memory is not None else 'empty_parent_memory', 'role': role,
            'target_owner_id': owner['id'], 'provenance': provenance})
        if memory is not None:
            rid = identity('memory_import', runtime.run_id, role, provenance['parent_memory_sha256'])
            tx.insert('working_memory', rid, {'owner_id': owner['id'], 'owner': owner['scope'],
                'revision': 1, 'parent_ref': None, 'content': content,
                'source_turn_id': None, 'source_call_id': None, 'source_effect_id': None,
                'source_import_id': registration['id'], 'ledger_snapshot_seq': tx.seq,
                'context_manifest_hash': None, 'schema_version': 'agent_working_memory_v1',
                'verification': 'unverified_working_state', 'import_provenance': provenance})
            tx.update('agent_owner', owner['id'], {'memory_revision': 1, 'memory_head_ref': rid})
        return registration
    return runtime.store.mutate(runtime.run_id, token, 'private_memory_imported', apply,
                                payload={'role': role, 'target_session_id': session['id'], 'provenance': provenance})


def prepare(args):
    if not args.root.is_absolute() or not args.parent_root.is_absolute():
        raise ValueError('New and parent run roots must be absolute paths')
    root = args.root.resolve()
    if root.exists() or root == args.parent_root.resolve() or args.run_id == args.parent_run_id:
        raise ValueError('Continuation needs a fresh directory and distinct run identity')
    parent = load_parent(args.parent_root, args.parent_run_id)
    lineage = make_lineage(parent, args.new_evaluations)
    initial = {name: parent['progress'][name] for name in ('candidates', 'scores')}
    count = len(initial['candidates'])
    root.mkdir(parents=True, exist_ok=False)
    initial_path = root / 'initial-observations.json'
    host.write_json(initial_path, initial)
    example = json.dumps({'run_root': str(root), 'run_id': args.run_id, 'candidates': ['YOUR_NEW_VALID_SMILES']})
    goal = (f'Continue the same fixed hidden molecular score optimization task, higher in [0,1] is better. '
        f'This new segment inherits {count} actual canonical unique observations: '
        f'{lineage["original_initial_count"]} original initialization observations and '
        f'{lineage["historical_generated_evaluations"]} scored in prior segments. Obtain exactly '
        f'{args.new_evaluations} ADDITIONAL valid, novel, finite scores. None of the inherited observations '
        'count toward this segment. The complete inherited table and current score receipts are authoritative. '
        'Imported private memory is historical and unverified: old evaluated/remaining counts, completion '
        'statements, paths and evidence IDs refer to the parent, not this segment. In particular a stale '
        '190-evaluated/10-remaining or 200-completed statement does not override the inherited complete table. '
        'Old evidence IDs confer no current citation authority. Use the current root, run, plan, and delivered facts. '
        'The oracle identity and formula intentionally remain hidden, not a missing prerequisite. '
        'Lab should adapt its search from the inherited observations and its own prior strategy, proposing '
        'batches of up to 10 molecules (absolute tool limit 20). Only Lab may request molecular scoring using '
        f'{adapter.ADAPTER_ID} version {adapter.VERSION}; inputs_json must encode an object shaped like {example}. '
        'Invalid or duplicate proposals do not count as new evaluations. Meta independently reviews actual '
        'results and mechanism issues without submitting molecules. No supported issue may legitimately lead '
        'Meta to no_action. The host delivers only explicitly frozen sources and does not automatically build '
        'arbitrary capabilities or policy trials. Retain private plans and failed hypotheses; complete the '
        'requested number from real remaining counts, without claiming biological efficacy or causal Meta benefit.')
    task_file = root / 'task-contract.txt'
    task_file.write_text(goal + '\n', encoding='utf-8')
    store = Store(root)
    try:
        for expected, raw in parent['evidence_objects'].items():
            if store.put_blob(raw) != expected:
                raise ValueError('Inherited source archive changed during import')
        initial_ref = store.put_blob(initial)
        lineage_ref = store.put_blob(lineage)
        sources = freeze_sources(store, {'initial_observations': initial_path, 'task_contract': task_file})
        config = copy.deepcopy(parent['config'])
        config.pop('schema_version', None)
        budgets = config.pop('budgets')
        max_external = config.pop('max_external')
        config.update(task_id=args.run_id, task_plan_id=args.run_id, goal=goal,
            pablo_run_root=str(root), pablo_initial_ref=initial_ref,
            pablo_max_new_evaluations=args.new_evaluations,
            pablo_continuation_lineage_ref=lineage_ref,
            authorized_sources=[s['source_id'] for s in sources],
            authorized_source_contracts=source_contracts(sources), frozen_sources=sources)
        Controller(store).initialize(args.run_id, budgets=budgets, max_external=max_external, config=config)
        plan = host.task_plan(root, args.run_id, initial_ref, args.new_evaluations, goal)
        plan['protocol']['parameters'].update(initial_count=count,
            original_initial_count=lineage['original_initial_count'],
            historical_generated_evaluations=lineage['historical_generated_evaluations'],
            global_observation_ordinal_offset=count,
            target_total_observations=count + args.new_evaluations,
            inherited_trajectory_sha256=lineage['inherited_trajectory_sha256'],
            parent_history_sha256=parent['history']['last_event_hash'],
            slot_semantics=f'This segment owns positions 001..{args.new_evaluations:03d}; these are not predetermined molecules. '
                f'The inherited {count} observations remain historical; completion yields {count + args.new_evaluations} total observations. '
                f'Global observation ordinal equals {count} plus local evaluation number ({count + 1}..{count + args.new_evaluations}). '
                'Each admitted packet binds segment positions to novel canonical SMILES before scoring.')
        plan['arms'][0]['shards'][0]['protocol_sha256'] = digest(plan['protocol'])
        register_task_plan(store, args.run_id, plan, key='freeze-task-plan')
        runtime = Runtime(store, args.run_id, adapters=[adapter.make_adapter()])
        runtime.controller.add_fact(args.run_id, 'initial-observations', 'raw_fact', initial,
            {'source': 'Verified completed parent receipts', 'parent_root': parent['root'],
             'parent_run_id': parent['run_id'], 'parent_history': parent['history'],
             'trajectory_sha256': lineage['inherited_trajectory_sha256'], 'lineage_ref': lineage_ref,
             'scope': 'Historical observations excluded from this segment evaluation count'}, 'initial-fact')
        runtime.controller.add_fact(args.run_id, 'task-contract', 'source_statement',
            {'contract': goal, 'new_evaluations_required': args.new_evaluations},
            {'source': 'Explicit continuation task contract', 'sha256': digest(goal)}, 'contract-fact')
        for role in ('lab', 'meta'):
            import_memory(runtime, role, parent['memories'][role], parent)
        runtime.enqueue_lab('continuation-start', ['initial-observations', 'task-contract'])
        runtime.observe('continuation-input', 'input_changed', 0, ['initial-observations', 'task-contract'])
        host.write_json(root / 'run-config.json', store.run(args.run_id)['config'])
        host.write_json(root / 'task-plan.json', plan)
        host.write_json(root / 'lineage.json', lineage)
        tracked = subprocess.check_output(['git', 'ls-files', '*.py'], cwd=REPO, text=True).splitlines()
        paths = {REPO / name for name in tracked} | set((REPO / 'rsi_domains/pablo').glob('*.py'))
        paths.update({Path(__file__).resolve(), Path(host.__file__).resolve()})
        paths.update(REPO / name for name in parent['manifest']['source_hashes'])
        manifest = {'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
            'source_hashes': {str(p.relative_to(REPO)): digest(p.read_bytes()) for p in sorted(paths)},
            'model': config['model'], 'effort': config['effort'], 'target_new_evaluations': args.new_evaluations,
            'task_aware': False, 'algorithm': parent['manifest'].get('algorithm'),
            'parent_manifest_sha256': parent['manifest_sha256'], 'continuation_lineage': lineage,
            'prior_attempts': lineage['prior_attempts'], 'inherited_observations': count,
            'python': sys.version, 'preparation_rescored_historical_molecules': False}
        host.write_json(root / 'manifest.json', manifest)
        host.log(root, 'continuation_prepared', parent_root=parent['root'], inherited_observations=count,
                 target_new_evaluations=args.new_evaluations, model=config['model'], effort=config['effort'])
    finally:
        store.close()
    write_cumulative(root, args.run_id)


def write_cumulative(root, run_id):
    root = Path(root).resolve()
    lineage = json.loads((root / 'lineage.json').read_text())
    store = ReadOnlySnapshot(root)
    try:
        config = store.run(run_id)['config']
        if digest(lineage) != config['pablo_continuation_lineage_ref']:
            raise ValueError('Continuation lineage file changed')
        store.verify_history(run_id)
        current = {b['id']: b['spent'] for b in store.list(run_id, 'budget')}
        report = {'updated_at_unix': time.time(), 'run_id': run_id,
            'current_segment_costs': current,
            'prior_segment_costs': lineage['inherited_segment_costs'],
            'prior_attempt_costs': lineage['prior_attempt_costs'],
            'cumulative_known_costs': _sum_costs([current, lineage['inherited_segment_costs'], lineage['prior_attempt_costs']]),
            'prior_attempts': lineage['prior_attempts'], 'cost_segments': lineage['cost_segments'],
            'scope': 'Observed ledger costs plus explicitly archived prior attempts; host overhead excluded, not zero',
            'updates': 'At preparation and host exit; live segment budgets remain in progress.json'}
        host.write_json(root / 'cumulative-costs.json', report)
        return report
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--parent-root', type=Path)
    parser.add_argument('--parent-run-id', default='pablo-200')
    parser.add_argument('--run-id', default='pablo-200-continuation1')
    parser.add_argument('--new-evaluations', type=int, default=200)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.new_evaluations <= 10000:
        parser.error('--new-evaluations must be between 1 and 10000')
    if not args.resume:
        if args.parent_root is None:
            parser.error('--parent-root is required for a new segment')
        prepare(args)
    if args.prepare_only:
        return 0
    result = host.drive(args)
    write_cumulative(args.root, args.run_id)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
