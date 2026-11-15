"""Publish an existing settled tool failure to its waiting issue; never rerun it.

Default operation is read-only planning. --apply is an explicit host recovery:
it appends the exact existing receipt as execution evidence, optionally attaches
an existing final observation snapshot, and wakes the ordinary Meta workflow.
It neither changes the original automatic-run result nor claims autonomous
recovery. No model executor, adapter executor, or scorer is called here.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from rsi_domains.pablo.adapter import _ReadStore, read_progress
from rsi_lab.controller import Controller, identity
from rsi_lab.store import Store, digest, encode


RECOVERY_KIND = 'host_tool_failure_recovery'


def _snapshot(store, run_id, snapshot_id):
    if snapshot_id is None:
        return None
    fact = store.get(run_id, 'fact', snapshot_id)
    content = json.loads(store.read_blob(fact['content_ref']))
    provenance = fact.get('provenance', {})
    if (not snapshot_id.startswith('campaign_state_') or fact['kind'] != 'raw_fact'
            or not isinstance(provenance, dict) or provenance.get('delivery_kind') != 'measurement_snapshot'
            or not isinstance(content, dict) or content.get('schema_version') != 'pablo_observation_snapshot_v1'):
        raise ValueError('Optional evidence must be an existing original campaign observation snapshot')
    current = read_progress(store, run_id)
    keys = ('initial_count', 'new_evaluations', 'remaining_evaluations', 'candidates', 'scores',
            'observations', 'top20', 'trajectory_sha256')
    if current['remaining_evaluations'] != 0 or any(content.get(key) != current[key] for key in keys):
        raise ValueError('Optional snapshot must match the complete current settled trajectory with no evaluations remaining')
    config = store.run(run_id)['config']
    effects = sorted((effect for effect in store.list(run_id, 'effect')
        if effect['metadata'].get('adapter_id') == 'pablo.evaluate' and effect['status'] == 'succeeded'),
        key=lambda effect: effect['created_seq'])
    receipts = [{'effect_id': effect['id'], 'receipt_ref': effect['result_ref'],
                 'packet_sha256': effect['packet_hash']} for effect in effects]
    if (content.get('initial_source_ref') != config['pablo_initial_ref']
            or content.get('evaluation_receipts') != receipts
            or provenance.get('initial_source_ref') != config['pablo_initial_ref']
            or provenance.get('receipt_refs') != [row['receipt_ref'] for row in receipts]):
        raise ValueError('Optional snapshot provenance differs from the settled initial/evaluation receipts')
    return {'fact_id': snapshot_id, 'content_ref': fact['content_ref'],
            'trajectory_sha256': current['trajectory_sha256'], 'new_evaluations': current['new_evaluations']}


def plan_recovery(store, run_id, effect_id, issue_id, snapshot_id=None):
    history = store.verify_history(run_id)
    rid = identity(RECOVERY_KIND, effect_id)
    intent = {'run_id': run_id, 'effect_id': effect_id, 'issue_id': issue_id, 'snapshot_id': snapshot_id}
    existing = store.list(run_id, 'registration', id=rid)
    if existing:
        record = existing[0]
        plan = json.loads(store.read_blob(record['plan_ref']))
        if record['kind'] != RECOVERY_KIND or plan['intent'] != intent:
            raise ValueError('This effect already has a different recorded host recovery intent')
        if plan['recovery_script_sha256'] != digest(Path(__file__).read_bytes()):
            raise ValueError('Resume host recovery using its exact original script')
        return plan

    effect = store.get(run_id, 'effect', effect_id)
    work = store.get(run_id, 'work', effect['work_id'])
    metadata, inputs = effect.get('metadata', {}), work.get('inputs', {})
    dependency_id = inputs.get('dependency_id')
    if (effect['status'] != 'failed_definite' or effect.get('accounting_unknown') is not False
            or not isinstance(effect.get('actual_costs'), dict)
            or not isinstance(effect.get('held_reservations'), dict)
            or any(value != 0 for value in effect['held_reservations'].values())):
        raise ValueError('Recovery requires a definitely failed, fully accounted, unreserved effect')
    if (work['kind'] != 'diagnostic' or work['status'] != 'failed' or work['effect_id'] != effect_id
            or work.get('outcome_ref') != effect.get('result_ref')
            or not metadata.get('adapter_id') or metadata.get('adapter_id') != inputs.get('capability_id')
            or metadata.get('adapter_version') != inputs.get('capability_version')
            or not dependency_id or metadata.get('dependency_id') != dependency_id
            or inputs.get('issue_id') != issue_id or metadata.get('issue_id') != issue_id):
        raise ValueError('The failed adapter effect must bind its original diagnostic, issue and dependency')
    packet = store.read_blob(effect['packet_ref'])
    if digest(packet) != effect['packet_hash']:
        raise ValueError('The original effect packet hash differs')
    descriptor = json.loads(store.read_blob(metadata['adapter_descriptor_ref']))
    if (digest(descriptor) != metadata['adapter_descriptor_sha256']
            or descriptor['adapter_id'] != metadata['adapter_id']
            or descriptor['version'] != metadata['adapter_version'] or descriptor['effect_kind'] != effect['kind']):
        raise ValueError('The frozen adapter descriptor does not match the failed effect')
    receipt = json.loads(store.read_blob(effect['result_ref']))
    if receipt.get('status') != 'failed_definite' or receipt.get('actual_costs') != effect['actual_costs']:
        raise ValueError('The immutable receipt must confirm the settled failure and exact accounted costs')
    all_effects = store.list(run_id, 'effect')
    if any(row['status'] in {'prepared', 'dispatching', 'unknown'} or row.get('accounting_unknown')
           for row in all_effects):
        raise ValueError('Host recovery requires a quiescent run with no unresolved execution/accounting')
    for budget in store.list(run_id, 'budget'):
        if (not math.isclose(budget['spent'], sum(row.get('actual_costs', {}).get(budget['id'], 0)
                                                for row in all_effects), abs_tol=1e-7)
                or not math.isclose(budget['reserved'], sum(row.get('held_reservations', {}).get(budget['id'], 0)
                                                          for row in all_effects), abs_tol=1e-7)):
            raise ValueError('Recorded costs do not reconcile with the existing budget ledger')
    issue = store.get(run_id, 'issue', issue_id)
    dep = store.get(run_id, 'dependency', dependency_id)
    waits = [row for row in store.list(run_id, 'wait', issue_id=issue_id, dependency_id=dependency_id)
             if row['status'] in {'waiting', 'disposition_due', 'disposed'}]
    if (issue['status'] != 'open' or dep['producer'] != metadata['adapter_id']
            or not isinstance(dep['value'], dict) or dep['value'].get('status') != 'pending'
            or len(waits) != 1 or waits[0]['observed_version'] != dep['revision']
            or waits[0]['producer'] != dep['producer']
            or waits[0]['wake_condition'] not in ({'kind': 'version_changed'}, {'kind': 'status', 'status': 'failed'})):
        raise ValueError('The original open issue must have one pending dependency wait that can wake on this failure')
    snapshot = _snapshot(store, run_id, snapshot_id)
    return {'schema_version': 'campaign_tool_failure_host_recovery_v1', 'intent': intent,
        'registration_id': rid, 'failure_fact_id': identity('host_tool_failure', effect_id),
        'recovery_script_sha256': digest(Path(__file__).read_bytes()),
        'before_history': history, 'effect': effect, 'work': work,
        'issue_sha256': digest(issue), 'dependency': dep, 'wait_id': waits[0]['id'],
        'receipt': receipt, 'snapshot': snapshot,
        'authority': 'Explicit host notification recovery of an existing execution failure; '
                     'not a model response, scientific finding, new experiment, or autonomous success.'}


def recover(store, plan):
    intent, rid = plan['intent'], plan['registration_id']
    run_id, issue_id, effect_id = intent['run_id'], intent['issue_id'], intent['effect_id']
    existing = store.list(run_id, 'registration', id=rid)
    if existing and existing[0]['status'] == 'complete':
        if json.loads(store.read_blob(existing[0]['plan_ref'])) != plan:
            raise ValueError('Completed recovery differs from the requested exact plan')
        return existing[0]
    budgets_before = store.list(run_id, 'budget')
    effects_before = store.list(run_id, 'effect')

    def publish(tx):
        effect, work = tx.get('effect', effect_id), tx.get('work', plan['work']['id'])
        issue, dep = tx.get('issue', issue_id), tx.get('dependency', plan['dependency']['id'])
        if (effect != plan['effect'] or work != plan['work'] or dep != plan['dependency']
                or issue['status'] != 'open' or digest(issue) != plan['issue_sha256']):
            raise ValueError('The reviewed recovery target changed before its atomic publication')
        fid = plan['failure_fact_id']
        tx.insert('fact', fid, {'kind': 'source_statement', 'content_ref': tx.blob({
            'schema_version': 'adapter_execution_failure_notice_v1', 'effect_id': effect_id,
            'adapter_id': effect['metadata']['adapter_id'], 'adapter_version': effect['metadata']['adapter_version'],
            'status': 'failed_definite', 'receipt': plan['receipt'],
            'authority': 'Execution failure only; this is not scientific evidence and does not establish a hypothesis.'}),
            'provenance': {'source': 'settled_adapter_effect', 'delivery_kind': 'execution_receipt',
                'effect_id': effect_id, 'work_id': work['id'], 'receipt_ref': effect['result_ref'],
                'packet_sha256': effect['packet_hash'], 'host_recovery_id': rid,
                'authority': 'execution_receipt_not_scientific_evidence'}})
        added = [fid] + ([plan['snapshot']['fact_id']] if plan['snapshot'] else [])
        for fact_id in added:
            tx.get('fact', fact_id)
        tx.update('issue', issue_id, {'evidence_ids': list(dict.fromkeys(issue['evidence_ids'] + added)),
                                     'evidence_revision': issue['evidence_revision'] + 1})
        value = {**dep['value'], 'status': 'failed', 'effect_id': effect_id, 'receipt_ref': effect['result_ref'],
                 'failure_fact_id': fid, 'host_recovery_id': rid}
        tx.update('dependency', dep['id'], {'value': value, 'content_hash': digest(value),
                                          'revision': dep['revision'] + 1})
        return tx.insert('registration', rid, {'kind': RECOVERY_KIND, 'status': 'published_waiting_for_tick',
            'plan_ref': tx.blob(plan), 'effect_id': effect_id, 'issue_id': issue_id,
            'dependency_id': dep['id'], 'failure_fact_id': fid,
            'snapshot_id': intent['snapshot_id'], 'authority': plan['authority']})

    store.mutate(run_id, rid + ':publish', 'host_tool_failure_published', publish, payload=plan)
    # Only dependency changes wake here. Ordinary runner resumption owns clock
    # expiry; the recovery does not trigger unrelated deadlines as a side effect.
    Controller(store).tick(run_id, now=0)
    wait = store.get(run_id, 'wait', plan['wait_id'])
    dep = store.get(run_id, 'dependency', plan['dependency']['id'])
    trigger = 'dependency:' + dep['id'] + ':' + str(dep['revision'])
    wake_work = store.list(run_id, 'work', issue_id=issue_id, kind='meta_disposition', trigger=trigger)
    if wait['status'] != 'fired' or len(wake_work) != 1:
        raise ValueError('Failure is published but its expected Meta disposition was not woken; inspect without retrying the tool')
    if budgets_before != store.list(run_id, 'budget') or effects_before != store.list(run_id, 'effect'):
        raise ValueError('Unexpected concurrent budget or effect change during host-only recovery')
    result = store.mutate(run_id, rid + ':complete', 'host_tool_failure_notification_completed',
        lambda tx: tx.update('registration', rid, {'status': 'complete', 'wake_work_id': wake_work[0]['id']}),
        payload={'recovery_id': rid, 'wake_work_id': wake_work[0]['id']})
    store.verify_history(run_id)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='Existing campaign directory containing ledger/')
    parser.add_argument('--run-id', default='campaign')
    parser.add_argument('--effect-id', required=True)
    parser.add_argument('--issue-id', required=True)
    parser.add_argument('--snapshot-id', help='Optional exact already-registered final campaign_state_* fact')
    parser.add_argument('--apply', action='store_true', help='Append explicit host recovery events; otherwise plan read-only')
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True) / 'ledger'
    readonly = _ReadStore(root)
    try:
        plan = plan_recovery(readonly, args.run_id, args.effect_id, args.issue_id, args.snapshot_id)
    finally:
        readonly.close()
    if args.apply:
        store = Store(root)
        try:
            result = recover(store, plan)
        finally:
            store.close()
    else:
        result = {'status': 'read_only_plan', 'recovery_id': plan['registration_id'],
            'intent': plan['intent'], 'failure_fact_id': plan['failure_fact_id'],
            'receipt': plan['receipt'], 'snapshot': plan['snapshot'],
            'before_history': plan['before_history'], 'authority': plan['authority']}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
