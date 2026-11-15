"""Independent, read-only verification of the fresh molecular campaign.

Does not import the campaign runner or call its audit, execute an oracle, invoke
a model/provider, or mutate the ledger. Reads only each call's owned native
prefix. --prefix validates settled work without claiming task completion.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rsi_domains.pablo.oracle import canonicalize, fingerprint
from rsi_lab.images import verify_image_transport
from rsi_lab.journal import read_journal, raw_stream
from rsi_lab.packets import present_delivery
from rsi_lab.roles import literature_decision_record
from rsi_lab.session_context import snapshot_from_bytes, verify_snapshot_images
from rsi_lab.store import Store, digest

RUN = 'campaign'
MODEL, EFFORT = 'gpt-6.1-sol', 'high'
INITIAL_HASH = 'ef8b0dd3cda1c3b3f520e21a311d94f0ef324e9ab1bc3ab2385e3b4ee4e5a6c1'
SCORE_ID = 'pablo.evaluate'
PUBLIC_SOURCE = 'registered_public_facts'


class ReadOnlyStore(Store):
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.blobs = self.root / 'objects'
        self.db = sqlite3.connect((self.root / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')
        self.db.execute('BEGIN')

    def put_blob(self, value):
        raise RuntimeError('Independent audit cannot write ledger objects')

    def mutate(self, *args, **kwargs):
        raise RuntimeError('Independent audit cannot mutate the ledger')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def native_call(store, effect, receipt, packet, session):
    folder = store.root / 'calls' / effect['id']
    invocation = json.loads((folder / 'invocation.json').read_bytes())
    transport_raw = (folder / 'transport_result.json').read_bytes()
    transport = json.loads(transport_raw)
    prompt = (folder / 'prompt.txt').read_bytes()
    require(prompt == packet['prompt'].encode(), 'Transport prompt differs from admitted request')
    require(digest(transport_raw) == receipt['receipt_sha256'], 'Transport receipt hash changed')
    require(invocation['model'] == MODEL and invocation['effort'] == EFFORT
        and invocation.get('dispatched') is True and transport.get('dispatched') is True
        and transport.get('protocol_valid') is True and transport.get('terminal_received') is True
        and transport.get('response') == receipt['response'], 'Actual transport/model identity is uncertified')
    command = invocation['command']
    require(command[command.index('--model') + 1] == MODEL
        and 'model_reasoning_effort="high"' in command and '--ignore-user-config' in command
        and 'web_search="disabled"' in command, 'Actual CLI model/effort/tool policy differs')
    for feature in ('shell_tool', 'unified_exec', 'apps', 'plugins', 'browser_use', 'computer_use'):
        require(any(command[i:i + 2] == ['--disable', feature] for i in range(len(command))),
                'Native tool policy missing: ' + feature)
    require(any(command[i:i + 2] == ['--enable', 'skip_host_skill_discovery']
                for i in range(len(command))), 'Host skill discovery was not disabled')
    journal = receipt['journal']
    require(Path(journal['path']) == folder / 'journal.jsonl', 'Journal is outside this call')
    recorded = read_journal(journal['path'])
    require(recorded['head_hash'] == journal['head_hash'] and recorded['durable_seq'] == journal['durable_seq']
        and not recorded.get('gaps'), 'Durable execution journal changed')
    stdout_events = [json.loads(line) for line in raw_stream(recorded, 'stdout').splitlines() if line.strip()]
    stdout_items = [event.get('item', {}).get('type') for event in stdout_events
                    if str(event.get('type', '')).startswith('item.')]
    require(all(kind in {'agent_message', 'reasoning'} for kind in stdout_items),
            'Actual provider stream records a native tool or unsupported item')
    context, thread = receipt['context_manifest'], receipt['provider_thread_id']
    native = Path(context['native_session_ref'])
    require(not native.is_symlink() and native.name.endswith('-' + thread + '.jsonl')
        and session['provider_thread_id'] == thread and transport['provider_thread_id'] == thread,
        'Native transcript does not belong to the effect session')
    prefix = context['native_prefix']
    require(type(prefix['prefix_bytes']) is int and 0 < prefix['prefix_bytes'] <= 32 * 1024 * 1024,
            'Native prefix exceeds audit bound')
    with native.open('rb') as stream:
        raw = stream.read(prefix['prefix_bytes'])
    require(digest(raw) == prefix['prefix_sha256'], 'Native prefix hash changed')
    cwd = str(store.root / 'agents' / session['id'] / 'workspace')
    require(invocation['cwd'] == cwd, 'Transport workspace does not belong to this session')
    previous = (effect['metadata']['agent_session'].get('context_manifest') or {}).get('native_prefix')
    snapshot = snapshot_from_bytes(raw, expected_thread_id=thread, expected_cwd=cwd, expected_prefix=previous)
    prior_count = len(raw[:previous['prefix_bytes']].splitlines()) if previous else 0
    current = [json.loads(line) for line in raw.splitlines()[prior_count:]]
    response_types = [row['payload'].get('type') for row in current if row['type'] == 'response_item']
    event_types = [str(row['payload'].get('type', '')) for row in current if row['type'] == 'event_msg']
    require(all(kind in {'message', 'reasoning'} for kind in response_types)
        and not any(marker in kind.lower() for kind in event_types
            for marker in ('tool_call', 'exec_command', 'web_search', 'browser', 'file_change', 'apply_patch')),
        'Actual native transcript records shell/file/web/tool execution')
    messages = [item['text'] for row in current if row['type'] == 'response_item'
        and row['payload'].get('role') == 'user' for item in row['payload'].get('content', [])
        if item.get('type') == 'input_text']
    require(prompt.decode() in messages, 'Exact current prompt absent from owned native turn')
    contexts = [row['payload'] for row in current if row['type'] == 'turn_context']
    require(contexts and all(row.get('model') == MODEL and row.get('effort') == EFFORT for row in contexts),
            'Native turn model or effort differs from requested model')
    expected_images = packet.get('image_attachments', [])
    verify_snapshot_images(snapshot, expected_images, after_record=prior_count)
    if expected_images:
        require(receipt.get('image_delivery_verified') is True
            and context.get('native_image_inputs_verified') is True, 'Images lack actual visual delivery')
        verify_image_transport(folder, expected_images, receipt)
    return {'provider_thread_id': thread, 'native_prefix_sha256': prefix['prefix_sha256'],
            'native_prefix_bytes': prefix['prefix_bytes'], 'exact_native_prompt': True,
            'native_tool_items_observed': 0, 'stdout_tool_items_observed': 0,
            'native_response_types': sorted(set(response_types)), 'native_event_types': sorted(set(event_types))}


def audit(root):
    store = ReadOnlyStore(root / 'ledger')
    try:
        load = lambda ref: json.loads(store.read_blob(ref))
        history = store.verify_history(RUN)
        config = store.run(RUN)['config']
        manifest = load(config['campaign_manifest_ref'])
        errors, checks = [], {}
        effects = sorted(store.list(RUN, 'effect'), key=lambda row: row['created_seq'])
        by_effect = {row['id']: row for row in effects}
        works = {row['id']: row for row in store.list(RUN, 'work')}
        facts = {row['id']: row for row in store.list(RUN, 'fact')}
        proposals = {row['effect_id']: row for row in store.list(RUN, 'proposal')}
        by_proposal = {row['id']: row for row in proposals.values()}
        sessions = {row['id']: row for row in store.list(RUN, 'agent_session')}
        owners = {row['id']: row for row in store.list(RUN, 'agent_owner')}
        turns = {row['id']: row for row in store.list(RUN, 'agent_turn')}
        registrations = store.list(RUN, 'registration')
        tasks = [row for row in registrations if row.get('kind') == 'literature_task']

        def check(name, action):
            try:
                action()
                checks[name] = True
            except (ValueError, KeyError, TypeError, OSError, IndexError) as exc:
                checks[name] = False
                errors.append({'scope': name, 'error': str(exc)})

        raw = (root / 'inputs/original-source.csv').read_bytes()
        require(digest(raw) == INITIAL_HASH == manifest['initial_input_sha256'], 'Not the original initialization source')
        rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))[:100]
        initial = {'candidates': [canonicalize(row['smile']) for row in rows],
                   'scores': [float(row['adip']) for row in rows]}
        require(len(set(initial['candidates'])) == 100 and None not in initial['candidates']
            and initial == load(config['pablo_initial_ref']), 'Initial first-100 observations differ')
        checks['original_first_100'] = True

        def frozen():
            require(manifest == json.loads((root / 'campaign-manifest.json').read_bytes())
                and config == json.loads((root / 'run-config.json').read_bytes()), 'Frozen host files differ')
            require(config['model'] == MODEL and config['effort'] == EFFORT
                and config['pablo_max_new_evaluations'] == 200 and config['max_external'] == 1
                and config['pablo_run_root'] == str(store.root), 'Frozen task configuration differs')
            require(digest(json.loads((root / 'literature-config.json').read_bytes())) ==
                manifest['literature_configuration_sha256'], 'Literature configuration changed')
            require(config['pablo_oracle_fingerprint'] == manifest['artifacts']['oracle_fingerprint'] == fingerprint(),
                    'Oracle implementation/dependency fingerprint changed')
            for key in ('source_sha256', 'local_dependency_sha256'):
                for name, expected in manifest['artifacts'][key].items():
                    path = Path(name)
                    require((digest(path.read_bytes()) if path.is_file() else None) == expected,
                            'Frozen source/dependency changed: ' + name)
        check('frozen_implementation_and_configuration', frozen)
        check('private_oracle_not_registered_as_source', lambda: require(
            config['authorized_sources'] == [PUBLIC_SOURCE]
            and set(config['authorized_source_contracts']) == {PUBLIC_SOURCE}, 'Private source access exposed'))

        candidates, scores = list(initial['candidates']), list(initial['scores'])
        observations, scored, score_states = [], [], {}
        for effect in effects:
            if effect['metadata'].get('adapter_id') != SCORE_ID or effect['status'] != 'succeeded':
                continue
            try:
                packet, receipt = load(effect['packet_ref']), load(effect['result_ref'])
                result = receipt['result']
                work = works[effect['work_id']]
                proposal = by_proposal[work['inputs']['source_proposal_id']]
                author = by_effect[proposal['effect_id']]
                inputs = {key: packet[key] for key in ('run_root', 'run_id', 'candidates')}
                require(not effect.get('accounting_unknown') and effect['kind'] == 'pablo_evaluation'
                    and receipt['status'] == 'succeeded' and result['effect_id'] == effect['id']
                    and result['packet_sha256'] == effect['packet_hash'] == effect['packet_ref']
                    and packet['oracle_fingerprint'] == config['pablo_oracle_fingerprint']
                    and packet['initial_ref'] == config['pablo_initial_ref'] and packet['max_new_evaluations'] == 200,
                    'Scorer execution is not bound to the frozen effect and objective')
                require(work['kind'] == 'diagnostic' and work['inputs']['lab_continuation'] is True
                    and proposal['status'] == 'applied' and author['kind'] == 'lab'
                    and proposal['command']['mode'] == 'lab' and proposal['command']['action'] == 'request_diagnostic'
                    and proposal['command']['payload']['capability_id'] == SCORE_ID
                    and proposal['command']['payload']['capability_version'] == '1.0'
                    and json.loads(proposal['command']['payload']['inputs_json']) == inputs
                    and json.loads(work['inputs']['inputs_json']) == inputs
                    and load(author['result_ref'])['response']['command'] == proposal['command'],
                    'Score candidates are not from the applied Lab request')
                require(result['evaluation_start'] == packet['evaluation_start'] == len(observations)
                    and packet['trajectory_sha256'] == digest({'candidates': candidates, 'scores': scores})
                    and not packet['pending_effect_ids'], 'Score batch does not extend the exact prior trajectory')
                seen, expected = set(candidates), []
                for index, value in enumerate(inputs['candidates']):
                    canonical = canonicalize(value)
                    if canonical is not None and canonical not in seen:
                        if len(observations) + len(expected) < 200:
                            expected.append({'source_index': index, 'canonical': canonical})
                        seen.add(canonical)
                require(expected == packet['accepted'] and len(result['evaluations']) == len(expected),
                        'Scored candidates differ from valid unique candidates admitted by remaining allowance')
                for row, admitted in zip(result['evaluations'], expected):
                    ordinal = len(observations) + 1
                    require(row['evaluation'] == ordinal and row['slot_id'] == f'evaluation-{ordinal:03d}'
                        and row['candidate'] == admitted['canonical'] and row['source_index'] == admitted['source_index']
                        and type(row['score']) in (float, int) and math.isfinite(row['score']),
                        'Invalid candidate, ordinal or measured score')
                    observations.append(row); candidates.append(row['candidate']); scores.append(row['score'])
                folder = store.root / 'pablo_evaluations' / effect['id']
                journal = (folder / 'evaluations.jsonl').read_bytes()
                require(json.loads((folder / 'receipt.json').read_bytes()) == receipt
                    and [json.loads(line) for line in journal.splitlines()] == [
                        {'effect_id': effect['id'], 'packet_sha256': effect['packet_hash'], **row}
                        for row in result['evaluations']]
                    and digest(journal) == receipt['execution']['journal_sha256']
                    and receipt['execution']['scored_candidates'] == len(expected), 'Score journal/receipt changed')
                require(effect['actual_costs']['tool_calls'] == 1 and effect['actual_costs']['model_calls'] == 0,
                        'Scorer costs differ from the recorded non-model operation')
                scored.append({'effect_id': effect['id'], 'author_effect_id': author['id'],
                    'count': len(expected), 'journal_sha256': digest(journal), 'settled_seq': effect['updated_seq'],
                    'receipt_ref': effect['result_ref'], 'packet_sha256': effect['packet_hash']})
                score_states[effect['id']] = len(observations)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append({'scope': 'score', 'effect_id': effect['id'], 'error': str(exc)})
        checks['settled_scoring_trajectory_certified'] = not any(row['scope'] == 'score' for row in errors)

        models, pending_models, model_pages, finals, decisions = [], [], {}, [], []
        for effect in effects:
            if effect['kind'] not in {'lab', 'literature', 'meta'}:
                continue
            proposal = proposals.get(effect['id'], {})
            if effect['status'] in {'prepared', 'dispatching', 'unknown'} or (
                    effect['status'] == 'succeeded' and proposal.get('status') not in {'applied', 'rejected', 'blocked_stale', 'blocked_action'}):
                pending_models.append(effect['id'])
                continue
            row = {'effect_id': effect['id'], 'role': effect['kind'], 'valid': False}
            try:
                packet, receipt = load(effect['packet_ref']), load(effect['result_ref'])
                session = sessions[effect['metadata']['agent_session_id']]
                turn = turns[effect['metadata']['agent_turn_id']]
                owner = owners[session['owner_id']]
                require(effect['status'] == 'succeeded' and not effect.get('accounting_unknown')
                    and receipt.get('protocol_valid') is True and receipt.get('terminal_received') is True
                    and not receipt.get('synthetic') and not receipt.get('never_dispatched')
                    and effect['actual_costs']['model_calls'] == 1
                    and packet['model'] == MODEL and packet['effort'] == EFFORT
                    and proposal['status'] == 'applied' and receipt['response']['command'] == proposal['command'],
                    'Model effect lacks a real, applied sol/high terminal')
                require(turn['status'] == 'committed' and turn['terminal_ref'] == effect['result_ref']
                    and turn['session_id'] == session['id'] and turn['owner_id'] == session['owner_id']
                    and effect['id'] in turn['effect_ids'] and session['owner'] == owner['scope'],
                    'Committed model turn or persistent owner binding differs')
                row.update(native_call(store, effect, receipt, packet, session))
                plan = load(effect['metadata']['plan_ref'])
                plan['envelope_bytes'] = store.read_blob(effect['packet_ref'])
                require(not plan['partial_evidence_ids'] and not plan['deferred_evidence_ids'],
                        'Model source context was partially delivered')
                pages = present_delivery(plan, effect_id=effect['id'], sent_packet_sha256=receipt['sent_packet_sha256'],
                    sent_prompt_sha256=receipt['sent_prompt_sha256'], receipt_ref=effect['receipt_ref'],
                    sent_image_manifest=receipt.get('sent_image_manifest'),
                    sent_image_manifest_sha256=receipt.get('sent_image_manifest_sha256'),
                    image_delivery_verified=receipt.get('image_delivery_verified', False))
                require(pages == load(proposal['delivery_ref']), 'Applied delivery differs from sent packet')
                for page in pages:
                    if page['evidence_id'] in facts and not page.get('image'):
                        fact = facts[page['evidence_id']]
                        require(page['complete_source'] and page['source_sha256'] == fact['content_ref']
                            and page['source_version'] == fact['version']
                            and page['content'].encode() == store.read_blob(fact['content_ref']),
                            'Delivered fact differs from immutable source bytes')
                if effect['kind'] == 'lab':
                    decision = literature_decision_record(receipt['response'], plan)
                    require(decision is not None, 'Lab decision rationale is missing')
                    decisions.append({'effect_id': effect['id'], 'action': proposal['command']['action'], **decision})
                    current = [json.loads(page['content']) for page in pages
                               if page['evidence_id'].startswith('campaign_state_')]
                    require(len(current) == 1, 'Lab did not receive exactly one current lossless snapshot')
                    snapshot = current[0]
                    prior = [score for score in scored if score['settled_seq'] < effect['created_seq']]
                    count = sum(score['count'] for score in prior)
                    require(snapshot['initial_count'] == 100 and snapshot['new_evaluations'] == count
                        and snapshot['remaining_evaluations'] == 200 - count
                        and snapshot['candidates'] == candidates[:100 + count] and snapshot['scores'] == scores[:100 + count]
                        and snapshot['observations'] == observations[:count]
                        and snapshot['evaluation_receipts'] == [{key: score[key] for key in
                            ('effect_id', 'receipt_ref', 'packet_sha256')} for score in prior]
                        and snapshot['trajectory_sha256'] == digest({'candidates': candidates[:100 + count],
                            'scores': scores[:100 + count]}), 'Lab snapshot differs from all prior actual scores')
                    require('adip' not in snapshot and 'objective' not in snapshot,
                            'Private objective label entered the host observation snapshot')
                    row['new_observations_delivered'] = count
                    if count == 200 and proposal['command']['action'] == 'no_action':
                        finals.append({'effect_id': effect['id'], 'owner_id': owner['id'],
                            'reason': proposal['command']['payload']['reason']})
                model_pages[effect['id']] = pages
                row.update(valid=True, owner_id=owner['id'], session_id=session['id'])
            except (ValueError, KeyError, TypeError, OSError, IndexError) as exc:
                errors.append({'scope': 'model', 'effect_id': effect['id'], 'error': str(exc)})
            models.append(row)
        certified = {row['effect_id'] for row in models if row['valid']}
        checks['all_settled_model_turns_certified'] = all(row['valid'] for row in models)
        checks['all_score_authors_certified'] = all(row['author_effect_id'] in certified for row in scored)

        child_details = []
        for task in tasks:
            try:
                parent = owners[task['parent_owner_id']]
                child_sessions = [session for session in sessions.values()
                                  if session['agent_instance_id'] == task['agent_instance_id']]
                require(parent['scope']['role'] == 'lab' and parent['scope']['branch_id'] == task['branch_id']
                    and child_sessions and all(session['role'] == 'literature'
                        and session['branch_id'] == task['branch_id'] for session in child_sessions),
                    'Child session scope differs from its Lab-owned task')
                child_effects = [effect for effect in effects if effect['metadata'].get('literature_task_id') == task['id']]
                require(all(effect['kind'] == 'literature' or effect['metadata'].get('adapter_id', '').startswith('literature.')
                            for effect in child_effects), 'Child escaped registered literature tool scope')
                require(all(sum(effect.get('actual_costs', {}).get(resource, 0)
                    + effect.get('held_reservations', {}).get(resource, 0) for effect in child_effects)
                    <= task['max_' + resource] for resource in ('model_calls', 'tool_calls'))
                    and len(child_effects) <= task['max_steps'], 'Child quota reset or overrun')
                handoffs = [row for row in models if row['valid'] and row['role'] == 'lab'
                    and by_effect[row['effect_id']]['work_id'] == task.get('last_result_work_id')
                    and row['owner_id'] == parent['id']]
                child_details.append({'task_id': task['id'], 'parent_owner_id': parent['id'],
                    'status': task['status'], 'session_count': len(child_sessions),
                    'handoff_certified': bool(handoffs), 'result_ids': task['result_ids']})
            except (ValueError, KeyError, TypeError) as exc:
                errors.append({'scope': 'child', 'task_id': task['id'], 'error': str(exc)})
        checks['child_ownership_scope_and_quotas'] = not any(row['scope'] == 'child' for row in errors)
        rebuilds = []
        for session in sessions.values():
            if not session.get('supersedes_session'):
                continue
            previous = sessions[session['supersedes_session']]
            checkpoint = store.get(RUN, 'agent_checkpoint', previous['checkpoint_ref'])
            correct = (session['owner_id'] == previous['owner_id'] == checkpoint['owner_id']
                and session['session_generation'] == previous['session_generation'] + 1
                and checkpoint['session_id'] == previous['id'])
            rebuilds.append({'session_id': session['id'], 'previous_session': previous['id'],
                             'owner_id': session['owner_id'], 'valid': correct})
        checks['rebuilds_preserve_owner'] = all(row['valid'] for row in rebuilds)
        budgets = store.list(RUN, 'budget')
        checks['global_costs_reconcile_without_resets'] = all(
            math.isclose(budget['spent'], sum(effect.get('actual_costs', {}).get(budget['id'], 0)
                for effect in effects), abs_tol=1e-7)
            and math.isclose(budget['reserved'], sum(effect.get('held_reservations', {}).get(budget['id'], 0)
                for effect in effects), abs_tol=1e-7)
            and (not budget['hard'] or budget['spent'] + budget['reserved'] <= budget['limit']) for budget in budgets)
        publications = {row['effect_id']: row for row in registrations if row.get('kind') == 'adapter_publication'}
        checks['published_scores_match_measured_receipts'] = all(
            any(fact['kind'] == 'raw_fact' and fact['content_ref'] == digest(load(by_effect[score['effect_id']]['result_ref'])['result'])
                and fact.get('provenance', {}).get('effect_id') == score['effect_id']
                and fact['provenance'].get('adapter_id') == SCORE_ID for fact in facts.values())
            for score in scored if publications.get(score['effect_id'], {}).get('status') == 'complete')
        pending_publications = [effect['id'] for effect in effects if effect['status'] == 'succeeded'
            and effect['metadata'].get('adapter_id') and publications.get(effect['id'], {}).get('status') != 'complete']
        pending_effects = [effect['id'] for effect in effects if effect['status'] in {'prepared', 'dispatching', 'unknown'}
                           or effect.get('accounting_unknown')]
        pending_work = [{key: work[key] for key in ('id', 'kind', 'status')} for work in works.values()
                        if work['status'] not in {'succeeded', 'failed', 'cancelled'}]
        issues = store.list(RUN, 'issue')
        active_waits = [row['id'] for row in store.list(RUN, 'wait')
                        if row['status'] in {'waiting', 'disposition_due'}]
        completion = {'exactly_200_new_scores': len(observations) == 200,
            'exactly_300_valid_unique': len(candidates) == len(set(candidates)) == 300,
            'final_lab_received_all_scores': bool(finals), 'all_model_effects_finished': not pending_models,
            'children_finished_and_returned': len(child_details) == len(tasks)
                and all(row['status'] == 'completed' and row['handoff_certified'] for row in child_details),
            'no_unresolved_effects': not pending_effects, 'all_publications_finished': not pending_publications,
            'no_pending_work': not pending_work,
            'all_meta_issues_terminal': all(row['status'] != 'open' for row in issues),
            'no_active_waits': not active_waits}
        recoveries = [row for row in registrations if row.get('kind') == 'host_tool_failure_recovery']
        initial_audit = None
        initial_path = root / 'independent-initial-audit.json'
        if initial_path.is_file():
            original_raw = initial_path.read_bytes()
            original = json.loads(original_raw)
            historical = store.db.execute('SELECT event_hash FROM events WHERE run_id=? AND seq=?',
                (RUN, original['history']['events'])).fetchone()
            require(historical and historical[0] == original['history']['last_event_hash'],
                    'Initial independent audit is not bound to this ledger history')
            initial_audit = {'path': initial_path.name, 'sha256': digest(original_raw),
                'events': original['history']['events'], 'workflow_passed': original['workflow_passed'],
                'completion_checks': original['completion_checks'], 'trajectory': original['trajectory']}
        if recoveries:
            checks['host_recovery_did_not_change_scored_trajectory'] = bool(initial_audit) and (
                initial_audit['trajectory']['trajectory_sha256'] == digest({'candidates': candidates, 'scores': scores}))
        prefix_valid = all(checks.values()) and not errors
        complete = prefix_valid and all(completion.values())
        return {'schema_version': 'independent_literature_campaign_audit_v1',
            'audited_at': datetime.now(timezone.utc).isoformat(), 'run_root': str(root),
            'read_only_consistent_snapshot': True, 'history': history,
            'prefix_valid': prefix_valid, 'workflow_passed': complete,
            'completion_kind': ('recovered_with_explicit_host_intervention' if recoveries else 'automatic')
                if complete else 'incomplete',
            'host_recovery_count': len(recoveries), 'host_recoveries': recoveries,
            'initial_automatic_workflow_passed': initial_audit['workflow_passed'] if initial_audit else None,
            'initial_automatic_audit': initial_audit,
            'checks': checks, 'completion_checks': completion, 'errors': errors,
            'trajectory': {'initial_count': 100, 'new_count': len(observations), 'unique_count': len(set(candidates)),
                'initial_best_score': max(initial['scores']), 'current_best_score': max(scores),
                'trajectory_sha256': digest({'candidates': candidates, 'scores': scores})},
            'score_batches': scored, 'models': models, 'pending_model_effects': pending_models,
            'lab_decisions': decisions, 'literature_tasks': child_details, 'final_lab_dispositions': finals,
            'session_count': len(sessions), 'owner_count': len(owners), 'rebuilds': rebuilds,
            'budgets': budgets, 'pending_effects': pending_effects, 'pending_publications': pending_publications,
            'pending_work': pending_work, 'issues': [{key: row.get(key) for key in
                ('id', 'status', 'resolution', 'reason', 'evidence_ids')} for row in issues], 'active_waits': active_waits,
            'limits': ['No model, scoring or provider calls were made by this auditor.',
                'Scientific explanations and literature decisions are model-authored interpretations, not independently validated conclusions.',
                'Zero delegated tasks is reported as zero; it does not certify a child workflow in this campaign.',
                'Only native transcript prefixes explicitly owned by ledger sessions were read.']}
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--prefix', action='store_true')
    parser.add_argument('--output-stem', default='independent-audit')
    args = parser.parse_args()
    require(Path(args.output_stem).name == args.output_stem, 'Output stem must be a basename')
    root = args.root.resolve(strict=True)
    require(args.output_stem != 'independent-initial-audit'
        or not (root / 'independent-initial-audit.json').exists(), 'The first automatic audit must remain unchanged')
    result = audit(root)
    (root / (args.output_stem + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    state = result['trajectory']
    text = ['# Independent campaign audit', '',
        f"Snapshot prefix valid: {result['prefix_valid']}; final workflow passed: {result['workflow_passed']}.",
        f"Completion kind: {result['completion_kind']}. Explicit host recoveries: {result['host_recovery_count']}. Initial automatic workflow passed: {result['initial_automatic_workflow_passed']}.",
        f"Original observations: 100. New measured evaluations: {state['new_count']}/200. Unique molecules: {state['unique_count']}.",
        f"Initial best score: {state['initial_best_score']}; current best score: {state['current_best_score']}.",
        f"Certified settled model turns: {sum(row['valid'] for row in result['models'])}; pending model effects: {len(result['pending_model_effects'])}.",
        f"Literature tasks: {len(result['literature_tasks'])}; sessions: {result['session_count']}; owner-preserving rebuilds: {len(result['rebuilds'])}.", '',
        *[f'- {key}: {value}' for key, value in {**result['checks'], **result['completion_checks']}.items()], '',
        *result['limits']]
    if result['errors']:
        text.extend(['', 'Errors:', *[json.dumps(row, ensure_ascii=False) for row in result['errors']]])
    (root / (args.output_stem + '.md')).write_text('\n'.join(text) + '\n')
    print(json.dumps({'prefix_valid': result['prefix_valid'], 'workflow_passed': result['workflow_passed'],
        'completion_kind': result['completion_kind'], 'host_recovery_count': result['host_recovery_count'],
        'new_count': state['new_count'], 'errors': result['errors'], 'output': str(root / (args.output_stem + '.json'))}, ensure_ascii=False))
    return 0 if result['prefix_valid'] and (args.prefix or result['workflow_passed']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
