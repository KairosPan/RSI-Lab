"""Task-owned cached originals and receipt-backed reading coverage.

An index or a previous read is never current citation authority. Retrieval
selects immutable originals for the next ordinary, measured model packet.
"""
from __future__ import annotations

import json
import re

from .store import digest, encode

ORIGINALS = {'source_excerpt', 'source_image', 'original'}


def source_rows(store, run_id, task):
    """Deduplicate this task's own acquired sources, including legacy IDs."""
    from .literature_sources import source_identity
    rows, grouped = [], {}
    for fid in dict.fromkeys(task['evidence_ids']):
        fact = store.get(run_id, 'fact', fid)
        provenance = fact['provenance']
        if not isinstance(provenance, dict) or provenance.get('delivery_kind') not in ORIGINALS:
            continue
        raw = store.read_blob(fact['content_ref'])
        try:
            content = json.loads(raw)
        except (ValueError, UnicodeError):
            content = raw.decode()
        identity = source_identity({'id': fid, 'kind': fact['kind'], 'content': content,
                                    'provenance': provenance})
        key = digest(identity) if identity is not None else digest({'fact_id': fid, 'content_ref': fact['content_ref']})
        if key in grouped:
            grouped[key]['aliases'].append(fid)
            continue
        doc = content if isinstance(content, dict) else {}
        row = {'id': fid, 'aliases': [fid], 'source_key': key, 'fact': fact,
            'document_id': doc.get('document_id') or provenance.get('document_id')
                or provenance.get('source') or fid,
            'locator': doc.get('locator') or provenance.get('locator') or '',
            'text': doc.get('text', '') if isinstance(content, dict) else content,
            'content': content}
        grouped[key] = row
        rows.append(row)
    return rows


def certified_reads(store, run_id, task, rows):
    """Derive coverage from successful owned invocations, not private memory.

    This also authenticates reads made before the coverage index was added.
    Original receipts and facts remain unchanged; no retroactive model call is
    synthesized. A rebuilt session retains the same owner, a new task does not.
    """
    by_id = {alias: row for row in rows for alias in row['aliases']}
    covered, certificates = set(), []
    for effect in store.list(run_id, 'effect'):
        if (effect['kind'] != 'literature' or effect['status'] != 'succeeded'
                or effect.get('accounting_unknown')
                or effect.get('metadata', {}).get('literature_task_id') != task['id']):
            continue
        meta = effect['metadata']
        session = store.get(run_id, 'agent_session', meta['agent_session_id'])
        if (session['agent_instance_id'] != task['agent_instance_id']
                or session['branch_id'] != task['branch_id'] or session['role'] != 'literature'):
            raise ValueError('Literature reading receipt belongs to another owner')
        plan = json.loads(store.read_blob(meta['plan_ref']))
        receipt = json.loads(store.read_blob(effect['result_ref']))
        request = store.read_blob(effect['packet_ref'])
        envelope = json.loads(request)
        if (digest(request) != effect['packet_hash'] or plan['packet_sha256'] != digest(request)
                or receipt.get('sent_packet_sha256') != digest(request)
                or receipt.get('sent_prompt_sha256') != digest(envelope['prompt'].encode())
                or receipt.get('protocol_valid') is False):
            raise ValueError('Literature reading receipt does not authenticate its original request')
        from .input_view import decode
        sent = decode(envelope['prompt'].rsplit('INPUT_DATA_JSON:\n', 1)[1], envelope['input_view_mode'])
        if (encode(plan['envelope']) != encode(envelope)
                or encode(plan['delivery_pages']) != encode(sent['delivered_pages'])
                or envelope['mode'] != 'literature'
                or sent['task']['literature_task']['id'] != task['id']
                or sent['task']['agent_state']['session_id'] != session['id']):
            raise ValueError('Literature reading plan differs from the actual dispatched original pages')
        ids = []
        for page in plan['delivery_pages']:
            row = by_id.get(page['evidence_id'])
            if row is None or not page['complete_source']:
                continue
            fact = store.get(run_id, 'fact', page['evidence_id'])
            raw = store.read_blob(fact['content_ref'])
            if (page['source_version'] != fact['version'] or page['source_sha256'] != digest(raw)
                    or page['content'].encode() != raw or page['content_sha256'] != digest(raw)
                    or page['from_cursor'] != 0 or page['to_cursor'] != len(raw)
                    or page['source'] != encode(fact['provenance']).decode()):
                raise ValueError('Literature cached source changed since its authenticated delivery')
            if page.get('image'):
                if receipt.get('image_delivery_verified') is not True:
                    continue
                from .images import verify_image_transport
                verify_image_transport(store.root / 'calls' / effect['id'],
                                       envelope.get('image_attachments', []), receipt)
            covered.add(row['source_key'])
            ids.append(row['id'])
        if ids:
            certificates.append({'effect_id': effect['id'], 'owner_id': session['owner_id'],
                'session_id': session['id'], 'packet_ref': effect['packet_ref'],
                'result_ref': effect['result_ref'], 'receipt_ref': effect.get('receipt_ref'),
                'evidence_ids': list(dict.fromkeys(ids))})
    return covered, certificates


def reading_state(store, run_id, task, selected_ids=()):
    rows = source_rows(store, run_id, task)
    covered, certificates = certified_reads(store, run_id, task, rows)
    docs = {}
    for row in rows:
        doc = docs.setdefault(row['document_id'], {'document_id': row['document_id'],
            'cached_original_count': 0, 'previously_delivered_count': 0, 'unread_count': 0})
        doc['cached_original_count'] += 1
        read = row['source_key'] in covered
        doc['previously_delivered_count'] += int(read)
        doc['unread_count'] += int(not read)
    index = sorted(rows, key=lambda row: (row['source_key'] in covered, row['document_id'], row['id']))[:16]
    return {'schema_version': 'literature_reading_state_v1', 'task_id': task['id'],
        'cached_original_count': len(rows), 'previously_delivered_count': len(covered),
        'unread_count': len(rows) - len(covered), 'verified_read_receipt_count': len(certificates),
        'documents': list(docs.values())[:16], 'omitted_document_count': max(0, len(docs) - 16),
        'source_index': [{'evidence_id': row['id'], 'document_id': row['document_id'],
            'locator': str(row['locator'])[:200], 'previously_delivered': row['source_key'] in covered}
            for row in index],
        'indexed_source_count': len(index), 'omitted_source_count': max(0, len(rows) - len(index)),
        'selected_for_this_request_count': len(set(selected_ids)),
        'authority': 'Pointers and authenticated reading coverage only; not current source delivery. '
            'Use read_cached_evidence to retrieve exact claim-relevant originals from one or more documents. '
            'An unlisted cached source remains queryable. Do not reread a whole document merely to restore citations.'}


def select_cached(store, run_id, task, payload):
    rows = source_rows(store, run_id, task)
    by_id = {alias: row for row in rows for alias in row['aliases']}
    limit = payload['limit']
    exact = payload['evidence_ids']
    documents = payload['document_ids']
    if any(fid not in by_id for fid in exact):
        raise ValueError('Cached source is outside this Literature task or is not an original')
    if any(doc not in {row['document_id'] for row in rows} for doc in documents):
        raise ValueError('Cached document is outside this Literature task')
    selected = list(dict.fromkeys(by_id[fid]['id'] for fid in exact))
    if len(selected) > limit:
        raise ValueError('Cached source limit cannot silently discard requested exact IDs')
    if exact:
        return selected
    terms = list(dict.fromkeys(re.findall(r'\w+', payload['query'].casefold())))
    covered, _ = certified_reads(store, run_id, task, rows)
    ranked = []
    for row in rows:
        if documents and row['document_id'] not in documents:
            continue
        text = (str(row['locator']) + ' ' + row['text']).casefold()
        score = sum(text.count(term) for term in terms)
        if terms and not score:
            continue
        ranked.append(((-score, row['source_key'] in covered, row['id']), row))
    # Preserve coverage of each requested document rather than allowing one
    # verbose paper to fill the entire cross-paper retrieval result.
    groups = {}
    for _, row in sorted(ranked, key=lambda item: item[0]):
        groups.setdefault(row['document_id'], []).append(row['id'])
    while groups and len(selected) < limit:
        for doc in list(groups):
            selected.append(groups[doc].pop(0))
            if not groups[doc]:
                del groups[doc]
            if len(selected) == limit:
                break
    return selected
