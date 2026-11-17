"""Generic, transaction-owned identity claims for diagnostic outboxes.

Adapters supply identities, never scheduling authority. Claims and Work must be
committed in one Store.mutate transaction. Unknown execution retains ownership;
only a complete publication can establish identity coverage. Coverage is a
receipt relationship, not proof of scientific or causal benefit.
Identity providers must encode experimental conditions or replicate identifiers
when those require distinct executions; the kernel never guesses equivalence.
"""
from .store import digest, encode


KIND = 'work_identity_claim'


def normalize(identities):
    if (not isinstance(identities, list) or len(identities) > 1000
            or any(not isinstance(value, str) or not value or len(value) > 4096
                   for value in identities) or len(encode(identities)) > 65536):
        raise ValueError('Claim identities require at most 1000 nonempty bounded strings within 64 KiB')
    return sorted(set(identities))


def claim_id(run_id, adapter_id, value):
    return 'work_claim_' + digest([run_id, adapter_id, value])


def _view(row):
    return {key: row.get(key) for key in ('id', 'adapter_id', 'identity', 'status',
            'owner_work_id', 'generation', 'effect_id', 'result_ref', 'publication_ref')}


def acquire(tx, adapter_id, work_id, identities):
    """Atomically acquire every identity or none; never partially route inputs.

    Call immediately before inserting the diagnostic Work, in the same write
    transaction. A competing writer cannot acquire between this check and the
    outbox commit. Replaying the same owner/identity is idempotent.
    """
    if not isinstance(adapter_id, str) or not adapter_id or not isinstance(work_id, str) or not work_id:
        raise ValueError('Adapter and owner Work identities are required')
    identities = normalize(identities)
    reconcile_all(tx)
    rows, conflicts, uncovered = [], [], []
    for value in identities:
        cid = claim_id(tx.run_id, adapter_id, value)
        found = tx.find('registration', id=cid)
        row = found[0] if found else None
        if row and (row.get('kind') != KIND or row.get('adapter_id') != adapter_id
                    or row.get('identity') != value):
            raise ValueError('Identity claim address belongs to a different claim')
        rows.append((cid, value, row))
        if row and row['status'] in {'held', 'covered'} and row['owner_work_id'] != work_id:
            conflicts.append(_view(row))
        elif not row or row['status'] == 'released':
            uncovered.append(value)
    if conflicts:
        return {'status': 'waiting_existing' if any(r['status'] == 'held' for r in conflicts) else 'covered',
                'all_or_none': True, 'claim_ids': [], 'conflicts': conflicts,
                'owner_work_ids': sorted({r['owner_work_id'] for r in conflicts}),
                'uncovered_identities': uncovered,
                'attribution': 'Existing operations retain their original owners; no new execution was admitted'}
    ids = []
    for cid, value, row in rows:
        if row and row['owner_work_id'] == work_id:
            # An already terminal owner cannot silently reacquire a released
            # identity. Controller Work status remains authoritative on replay.
            if row['status'] == 'released':
                raise ValueError('A released claim needs a new Work identity for retry')
            ids.append(cid)
            continue
        document = {'kind': KIND, 'adapter_id': adapter_id, 'identity': value,
                    'owner_work_id': work_id, 'status': 'held',
                    'generation': 1 if row is None else row['generation'] + 1,
                    'effect_id': None, 'result_ref': None, 'publication_ref': None,
                    'release_reason': None}
        if row is None:
            tx.insert('registration', cid, document)
        else:
            tx.update('registration', cid, document)
        ids.append(cid)
    return {'status': 'acquired', 'all_or_none': True, 'claim_ids': ids,
            'conflicts': [], 'owner_work_ids': [work_id] if ids else [],
            'uncovered_identities': []}


def reconcile_all(tx):
    """Release known failed/cancelled owners; retain unknown effects and costs.

    Successful execution alone does not cover identities. Publication must be
    complete and provide the adapter's explicit covered_claim_identities subset.
    Successful unproduced/clipped identities are released, not falsely covered.
    """
    claims = tx.find('registration', kind=KIND, status='held')
    owners = {}
    for row in claims:
        owners.setdefault(row['owner_work_id'], []).append(row)
    changed = []
    for wid, owned in owners.items():
        works = tx.find('work', id=wid)
        if not works:
            # An orphan indicates an interrupted/corrupt integration. Preserve
            # its ownership rather than granting a duplicate execution.
            continue
        work = works[0]
        eid = work.get('effect_id')
        effect = tx.get('effect', eid) if eid else None
        publication = None
        covered = set()
        reason = None
        if effect is not None:
            if effect.get('accounting_unknown') or effect['status'] in {'prepared', 'dispatching', 'unknown'}:
                continue
            if effect['status'] in {'failed_definite', 'cancelled'}:
                reason = 'known_terminal_failure_or_cancellation'
            elif effect['status'] == 'succeeded':
                publications = tx.find('registration', kind='adapter_publication', effect_id=eid, status='complete')
                if len(publications) != 1:
                    continue
                publication = publications[0]
                covered = set(normalize(publication.get('covered_claim_identities', [])))
                if not covered.issubset({row['identity'] for row in owned}):
                    raise ValueError('Publication claims coverage of identities not owned by this Work')
                reason = 'complete_publication'
            else:
                continue
        elif work['status'] in {'failed', 'cancelled', 'superseded'}:
            reason = 'known_terminal_work_without_execution'
        else:
            continue
        for row in owned:
            status = 'covered' if row['identity'] in covered else 'released'
            updated = tx.update('registration', row['id'], {
                'status': status, 'effect_id': eid,
                'result_ref': effect.get('result_ref') if effect else work.get('outcome_ref'),
                'publication_ref': publication.get('publication_ref') if publication else None,
                'release_reason': reason if status == 'released' else None})
            changed.append(_view(updated))
    return changed


def snapshot(store, run_id, *, adapter_id=None, max_bytes=48000):
    """Bounded operational projection; omitted identities remain fully guarded."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
        raise ValueError('Claim snapshot byte bound must be between 1 and 65536')
    rows = sorted((row for row in store.list(run_id, 'registration', kind=KIND)
                   if row['status'] in {'held', 'covered'} and
                   (adapter_id is None or row['adapter_id'] == adapter_id)),
                  key=lambda row: (row['created_seq'], row['id']))
    shown = []
    for row in rows:
        view = _view(row)
        if len(encode(shown + [view])) > max_bytes:
            break
        shown.append(view)
    return {'authority': 'controller_operational_state_not_scientific_evidence',
            'claims': shown, 'complete': len(shown) == len(rows),
            'total_claims': len(rows), 'omitted_claims': len(rows) - len(shown)}
