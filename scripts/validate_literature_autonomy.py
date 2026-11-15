"""Frozen, source-controlled first-decision probes for autonomous Lab routing.

Probe decisions are separate from the 200 new objective evaluations. All Lab
responses use the real runtime/model. Two prospectively selected positive
probes additionally execute the delegated child and its parent handoff.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rsi_lab.controller import Controller
from rsi_lab import images
from rsi_lab.roles import CodexExecutor, literature_decision_record
from rsi_lab.packets import present_delivery
from rsi_lab.runtime import Runtime
from rsi_lab.session_context import read_native_snapshot
from rsi_lab.store import Store
from rsi_literature import make_adapters
from scripts.literature_autonomy_cases import public_cases, private_oracles, freeze_cases


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def code_identity():
    files = [Path(__file__), ROOT / 'scripts/literature_autonomy_cases.py']
    for folder in ('rsi_lab', 'rsi_literature'):
        files.extend((ROOT / folder).glob('*.py'))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(files))}


def emit(root, event, **data):
    row = {'at': datetime.now(timezone.utc).isoformat(), 'event': event, **data}
    with (root / 'progress.jsonl').open('a') as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    print(json.dumps(row, ensure_ascii=False), flush=True)


def frozen_cases(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Frozen case or private rubric changed: ' + name)
    return (json.loads((root / 'public-cases.json').read_text())['cases'],
            json.loads((root / 'private/oracles.json').read_text())['oracles'])


def context_ready(runtime, work, root):
    def plan():
        agent, mode, task, options = runtime._context(work)
        packet = agent.prepare(mode, task, **options)
        if packet['status'] != 'ready' or packet['deferred_evidence_ids'] or packet['partial_evidence_ids']:
            raise ValueError('Probe requires complete current evidence; no silent narrowing')
        return task, packet
    task, packet = plan()
    session = runtime.store.get('probe', 'agent_session', task['agent_state']['session_id'])
    if session.get('provider_thread_id'):
        prior = session['context_manifest']
        snapshot = read_native_snapshot(prior['native_session_ref'], expected_thread_id=session['provider_thread_id'],
            expected_cwd=str(runtime.store.root / 'agents' / session['id'] / 'workspace'), expected_prefix=prior['native_prefix'])
        if (session['committed_turn'] >= 3 or snapshot['observable_text_history_bytes']
                + packet['input_bytes'] > runtime.config['max_input_bytes']):
            checkpoint = runtime.agents.seal_checkpoint(session['id'], 'probe-checkpoint:' + session['id'])
            new = runtime.agents.rebuild_session(session['id'], 'Bound current evidence and native history; retain owner and costs',
                'probe-rebuild:' + session['id'], checkpoint_id=checkpoint['id'])
            emit(root, 'session_rebuilt', previous=session['id'], current=new['id'])
            plan()


def initialize_case(root, case, literature_config):
    root.mkdir(parents=True, exist_ok=False)
    config = {'goal': case['goal'], 'execution_mode': 'live', 'model': 'gpt-6.1-sol', 'effort': 'high',
        'max_input_bytes': 524288, 'max_effective_context_bytes': 2097152,
        'max_image_bytes': 4194304, 'max_effective_image_bytes': 16777216,
        'max_open_reviews': 0, 'model_reservation': {'model_calls': 1, 'tokens': 100000, 'wall_seconds': 240}}
    budgets = {'model_calls': {'limit': 24, 'hard': True}, 'tool_calls': {'limit': 80, 'hard': True},
        'tokens': {'limit': 4000000, 'hard': False}, 'wall_seconds': {'limit': 3600, 'hard': False}}
    store = Store(root / 'ledger')
    controller = Controller(store)
    controller.initialize('probe', budgets=budgets, config=config)
    evidence = []
    for source in case['evidence']:
        controller.add_fact('probe', source['id'], 'source_statement', source['content'],
            {**source['provenance'], 'record_type': source['kind']}, 'source:' + source['id'])
        evidence.append(source['id'])
    runtime = Runtime(store, 'probe', adapters=make_adapters(root / 'documents', literature_config))
    runtime.enqueue_lab('initial-decision', evidence)
    return runtime


def choose_work(runtime, first_done, full_chain):
    ready = runtime.controller.ready('probe')
    if not first_done:
        return next((row for row in ready if row['kind'] == 'lab'), None)
    if not full_chain:
        return None
    tasks = runtime.store.list('probe', 'registration', kind='literature_task')
    if len(tasks) > 1:
        raise ValueError('Probe disallows starting another child after the first delegated research')
    if not tasks:
        return None
    task = tasks[0]
    if task['status'] == 'completed':
        return next((row for row in ready if row['id'] == task['last_result_work_id']), None)
    for kind in ('diagnostic', 'literature'):
        found = next((row for row in ready if row['kind'] == kind
                      and row['inputs'].get('literature_task_id') == task['id']), None)
        if found:
            return found
    return None


def inspect_case(runtime, case_id, full_chain, error=None):
    store = runtime.store
    effects = sorted(store.list('probe', 'effect'), key=lambda row: row['created_seq'])
    models = [row for row in effects if row['kind'] in {'lab', 'literature', 'meta'}]
    proposals = {row['effect_id']: row for row in store.list('probe', 'proposal')}
    facts = {row['id']: row for row in store.list('probe', 'fact')}
    certificates, certificate_errors = {}, []
    for effect in models:
        try:
            receipt = json.loads(store.read_blob(effect['result_ref']))
            proposal = proposals[effect['id']]
            packet = json.loads(store.read_blob(effect['metadata']['plan_ref']))
            packet['envelope_bytes'] = store.read_blob(effect['packet_ref'])
            if (effect['status'] != 'succeeded' or effect.get('accounting_unknown')
                    or receipt.get('protocol_valid') is not True or not receipt.get('terminal_received')
                    or receipt.get('synthetic') or not receipt.get('provider_thread_id')
                    or proposal.get('status') != 'applied' or packet['deferred_evidence_ids']
                    or packet['partial_evidence_ids'] or effect.get('actual_costs', {}).get('model_calls') != 1):
                raise ValueError('Not a real, fully delivered, applied model turn')
            pages = present_delivery(packet, effect_id=effect['id'],
                sent_packet_sha256=receipt.get('sent_packet_sha256'),
                sent_prompt_sha256=receipt.get('sent_prompt_sha256'), receipt_ref=effect['receipt_ref'],
                sent_image_manifest=receipt.get('sent_image_manifest'),
                sent_image_manifest_sha256=receipt.get('sent_image_manifest_sha256'),
                image_delivery_verified=receipt.get('image_delivery_verified', False))
            if pages != json.loads(store.read_blob(proposal['delivery_ref'])):
                raise ValueError('Applied delivery differs from exact packet and receipt')
            for page in pages:
                fact = facts[page['evidence_id']]
                if page['source_version'] != fact['version']:
                    raise ValueError('Source identity/version changed after delivery')
                if page.get('image'):
                    if page['image'] != images.validate_image_reference(json.loads(store.read_blob(fact['content_ref']))):
                        raise ValueError('Delivered image differs from its registered original')
                elif page['source_sha256'] != fact['content_ref']:
                    raise ValueError('Source contents changed after delivery')
            if packet['envelope'].get('image_attachments'):
                images.verify_image_transport(store.root / 'calls' / effect['id'],
                    packet['envelope']['image_attachments'], receipt)
                if not (receipt.get('context_manifest') or {}).get('native_image_inputs_verified'):
                    raise ValueError('Native image delivery was not certified')
            certificates[effect['id']] = {page['evidence_id'] for page in pages if page['complete_source']}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            certificate_errors.append({'effect_id': effect['id'], 'error': str(exc)})
    first = next((row for row in models if row['kind'] == 'lab'), None)
    decision, command, certified = None, None, False
    if first and first.get('result_ref'):
        receipt = json.loads(store.read_blob(first['result_ref']))
        proposal = proposals.get(first['id'], {})
        packet = json.loads(store.read_blob(first['metadata']['plan_ref']))
        command = proposal.get('command')
        certified = first['id'] in certificates
        if certified:
            decision = literature_decision_record(receipt['response'], packet)
    tasks = store.list('probe', 'registration', kind='literature_task')
    returns, chains = [], []
    sessions = {row['id']: row for row in store.list('probe', 'agent_session')}
    for task in tasks:
        completions = [row for row in models if row['id'] in certificates and row['kind'] == 'literature'
            and row['metadata'].get('literature_task_id') == task['id']
            and proposals[row['id']]['command']['action'] == 'complete_research']
        parents = [row for row in models if row['id'] in certificates and row['kind'] == 'lab'
            and row['work_id'] == task.get('last_result_work_id')
            and proposals[row['id']]['command']['action'] == 'no_action'
            and sessions[row['metadata']['agent_session_id']]['owner_id'] == task['parent_owner_id']
            and set(task['result_ids']).issubset(certificates[row['id']])]
        source_tools = []
        for tool in effects:
            if (tool['status'] != 'succeeded' or tool.get('accounting_unknown')
                    or tool['metadata'].get('literature_task_id') != task['id']
                    or not tool['metadata'].get('adapter_id', '').startswith('literature.')):
                continue
            result = json.loads(store.read_blob(tool['result_ref'])).get('result') or {}
            data = result.get('data') or {}
            if result.get('status') not in {'ok', 'partial'}:
                continue
            for fid, fact in facts.items():
                provenance = fact['provenance']
                if provenance.get('effect_id') != tool['id']:
                    continue
                original = (provenance.get('delivery_kind') == 'source_excerpt'
                    or (provenance.get('delivery_kind') == 'tool_observation'
                        and isinstance(data.get('paper'), dict) and data['paper'].get('abstract')))
                if original and any(fid in certificates[row['id']] and any(
                    fid in claim['evidence_ids'] for claim in proposals[row['id']]['command']['payload']['claims'])
                    for row in completions):
                    source_tools.append({'effect_id': tool['id'], 'evidence_id': fid})
        sufficient = any(proposals[row['id']]['command']['payload']['coverage_status']
                         == 'sufficient_for_requested_use' for row in completions)
        chains.append({'task_id': task['id'], 'child_completed': task['status'] == 'completed' and bool(completions),
            'parent_received': bool(parents), 'original_source_deliveries': source_tools,
            'coverage_sufficient': sufficient,
            'passed': task['status'] == 'completed' and bool(completions and parents and source_tools)
                and sufficient and not certificate_errors})
        returns.extend(parents)
    route = 'invalid'
    if certified:
        route = ('delegate' if command['action'] == 'request_literature' else
            'direct_literature' if command['action'] == 'request_diagnostic'
                and command['payload']['capability_id'].startswith('literature.') else 'no_external_research')
    return {'case_id': case_id, 'first_decision_certified': bool(certified), 'route': route,
        'first_command': command, 'decision_record': decision, 'model_calls': len(models),
        'full_chain_selected': full_chain, 'child_tasks': [{'id': row['id'], 'status': row['status'],
            'result_ids': row['result_ids']} for row in tasks], 'parent_handoff_applied': bool(returns),
        'full_chain_passed': bool(chains) and all(row['passed'] for row in chains),
        'chain_audits': chains, 'certificate_errors': certificate_errors,
        'error': error, 'budgets': store.list('probe', 'budget'), 'history': store.verify_history('probe'),
        'unresolved_effects': [row['id'] for row in effects if row['status'] in {'prepared', 'dispatching', 'unknown'}],
        'scope': 'first_lab_decision_and_selected_child_chain' if full_chain else 'first_lab_decision_only',
        'child_scope_note': 'Unselected child intents are recorded but not executed in this first-decision probe.'}


def run_case(root, case, literature_config, full_chain):
    outcome = root / 'result.json'
    if outcome.exists():
        old = json.loads(outcome.read_text())
        runtime = Runtime(Store(root / 'ledger'), 'probe', adapters=make_adapters(root / 'documents', literature_config))
        try:
            runtime.recover()
            result = inspect_case(runtime, case['case_id'], full_chain, old.get('error'))
            write(outcome, result)
            return result
        finally:
            runtime.store.close()
    if root.exists():
        store = Store(root / 'ledger')
        runtime = Runtime(store, 'probe', adapters=make_adapters(root / 'documents', literature_config))
        runtime.recover()
    else:
        runtime = initialize_case(root, case, literature_config)
    error = None
    try:
        executor = CodexExecutor(runtime.store.root / 'calls', timeout_seconds=240)
        for _ in range(70):
            effects = runtime.store.list('probe', 'effect')
            if any(row['status'] in {'prepared', 'dispatching', 'unknown'} for row in effects):
                raise ValueError('Unsettled effect requires reconciliation, never an automatic resend')
            first_done = any(row['kind'] == 'lab' for row in effects)
            work = choose_work(runtime, first_done, full_chain)
            if work is None:
                break
            if work['kind'] == 'diagnostic':
                result = runtime.execute_adapter(work['inputs']['capability_id'],
                    json.loads(work['inputs']['inputs_json']), trigger_id=work['id'], work_id=work['id'])
                emit(root, 'tool', operation=work['inputs']['capability_id'], effect_id=result['effect']['id'],
                    status=(result.get('result') or {}).get('status'))
            else:
                context_ready(runtime, work, root)
                result = runtime.run_meta(work['id'], executor, runtime.config['model_reservation'])
                proposal = result.get('proposal') or {}
                emit(root, 'model', role=work['kind'], effect_id=result.get('effect', {}).get('id'),
                    status=proposal.get('status'), action=(proposal.get('command') or {}).get('action'))
                if proposal.get('status') != 'applied':
                    raise ValueError('Real model output failed command admission')
        else:
            raise ValueError('Probe driver step bound exhausted')
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
        emit(root, 'blocked', error=error)
    finally:
        result = inspect_case(runtime, case['case_id'], full_chain, error)
        write(outcome, result)
        runtime.store.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--literature-config', required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--initialize-only', action='store_true')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config = json.loads(Path(args.literature_config).read_text())
    identity = {'code': code_identity(), 'literature_config': config, 'model': 'gpt-6.1-sol', 'effort': 'high'}
    if not root.exists():
        freeze_cases(root)
        cases, oracles = frozen_cases(root)
        selected = [row['case_id'] for row in cases if oracles[row['case_id']]['external_research_needed']][:2]
        write(root / 'identity.json', {**identity, 'full_chain_case_ids': selected})
    elif not args.resume:
        raise ValueError('Use a fresh evaluation root or explicit --resume')
    frozen = json.loads((root / 'identity.json').read_text())
    if any(frozen[key] != value for key, value in identity.items()):
        raise ValueError('Frozen evaluator code or configuration changed; do not relabel a rerun')
    if args.initialize_only:
        return 0
    cases, oracles = frozen_cases(root)
    results = []
    for case in cases:
        result = run_case(root / 'cases' / case['case_id'], case, config,
                          case['case_id'] in frozen['full_chain_case_ids'])
        expected = oracles[case['case_id']]['external_research_needed']
        result['expected_external_research'] = expected
        result['routing_correct'] = result['first_decision_certified'] and (
            result['route'] in {'delegate', 'direct_literature'}) == expected
        results.append(result)
        emit(root, 'probe_completed', case_id=case['case_id'], count=len(results),
            route=result['route'], certified=result['first_decision_certified'])
        write(root / 'results.json', results)
    positives = [row for row in results if row['expected_external_research']]
    negatives = [row for row in results if not row['expected_external_research']]
    summary = {'model': 'gpt-6.1-sol', 'effort': 'high', 'case_count': len(results),
        'certified_first_decisions': sum(row['first_decision_certified'] for row in results),
        'correct_routes': sum(row['routing_correct'] for row in results),
        'positive_count': len(positives), 'negative_count': len(negatives),
        'true_positive': sum(row['routing_correct'] for row in positives),
        'true_negative': sum(row['routing_correct'] for row in negatives),
        'delegations': sum(row['route'] == 'delegate' for row in results),
        'direct_literature': sum(row['route'] == 'direct_literature' for row in results),
        'total_model_calls': sum(row['model_calls'] for row in results),
        'full_chain_case_ids': frozen['full_chain_case_ids'],
        'selected_parent_handoffs': sum(row['parent_handoff_applied'] for row in results if row['full_chain_selected']),
        'selected_full_chains_passed': sum(row['full_chain_passed'] for row in results if row['full_chain_selected']),
        'limits': ['Routing quality is distinct from scientific answer correctness.',
            'These 20 probes are not counted as any of the 200 new objective evaluations.',
            'Only the prospectively selected two positive cases execute child research in this probe set.']}
    write(root / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if (summary['certified_first_decisions'] == len(results)
                 and summary['selected_full_chains_passed'] == len(frozen['full_chain_case_ids'])) else 1


if __name__ == '__main__':
    raise SystemExit(main())
