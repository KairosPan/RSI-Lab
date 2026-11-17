"""Canonical original excerpts, separate from tool-read and model-delivery events.

Preparation is read-only. The caller stores the unchanged original publication,
normalized facts, source identity registrations and read registrations in the
same publication transaction. These records never certify model delivery.
"""
from __future__ import annotations

import json

from .store import digest, encode

IDENTITY_SCHEMA = 'literature_source_identity_v1'
READ_SCHEMA = 'literature_source_read_v1'
IDENTITY_KIND = 'literature_source_identity'
READ_KIND = 'literature_source_read'
_SOURCE_FIELDS = ('document_id', 'document_version', 'source_sha256', 'locator')


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(char in '0123456789abcdef' for char in value)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _copy(value):
    return json.loads(encode(value))


def source_identity(row):
    """Return the exact original identity; a malformed declared excerpt raises.

    Content means the complete structured passage, including its text, range,
    source metadata and parser information. Similar text alone is insufficient.
    Author abstracts remain originals; summaries and pointers are never upgraded.
    """
    if not isinstance(row, dict):
        raise ValueError('A source fact must be an object')
    provenance = row.get('provenance')
    if not isinstance(provenance, dict) or provenance.get('delivery_kind') != 'source_excerpt':
        return None
    content = row.get('content')
    if (row.get('kind') != 'source_statement' or not isinstance(content, dict)
            or not isinstance(content.get('text'), str) or not content['text']
            or provenance.get('summary_of') or row.get('summary_of')
            or any(content.get(key) in ('summary', 'pointer') for key in ('delivery_kind', 'source_kind', 'kind'))):
        raise ValueError('A source_excerpt must be an original attributed passage, not an interpretation or pointer')
    if (any(not _text(content.get(key)) or content[key] != provenance.get(key) for key in _SOURCE_FIELDS)
            or not _hash(content['document_version']) or not _hash(content['source_sha256'])):
        raise ValueError('Source excerpt document, version, locator and hash bindings disagree')
    if 'text_sha256' in content and not _hash(content['text_sha256']):
        raise ValueError('Source excerpt text hash is malformed')
    for key, length in (('char_range', len(content['text'])), ('byte_range', len(content['text'].encode()))):
        if key in content:
            bounds = content[key]
            if (not isinstance(bounds, list) or len(bounds) != 2 or any(type(x) is not int for x in bounds)
                    or bounds[0] < 0 or bounds[1] - bounds[0] != length):
                raise ValueError('Source excerpt range differs from its exact original text')
    return {**{key: content[key] for key in _SOURCE_FIELDS}, 'content_sha256': digest(content)}


def _source_index_id(run_id, identity):
    return 'literature_source_identity_' + digest([run_id, identity])[:32]


def _canonical_id(run_id, identity):
    return 'literature_source_' + digest([run_id, identity])[:32]


def source_identity_registration(read_record):
    """Return an insert-if-absent registration; callers remove ``id`` for tx.insert.

    Existing registrations must equal these fields. Never update an index to
    point at a later reading. Insert this atomically with the canonical fact and
    the separate read record; each read record also has an ``id`` to remove.
    """
    identity, run_id = read_record['source_identity'], read_record['source_run_id']
    if (read_record.get('kind') != READ_KIND or read_record.get('schema_version') != READ_SCHEMA
            or read_record.get('source_identity_sha256') != digest(identity)
            or not _text(run_id) or not _text(read_record.get('canonical_fact_id'))):
        raise ValueError('Source identity registration requires a bound canonical read record')
    return {'id': _source_index_id(run_id, identity), 'kind': IDENTITY_KIND,
            'schema_version': IDENTITY_SCHEMA, 'source_run_id': run_id,
            'source_identity_sha256': digest(identity), 'source_identity': _copy(identity),
            'canonical_fact_id': read_record['canonical_fact_id']}


def canonicalize_publication(store, run_id, effect, publication):
    """Return ``(publication, read_records)`` without changing the store or input.

    The original publication and effect receipt remain authoritative tool output.
    Only strict source_excerpt facts and their explicit passage_evidence_ids are
    normalized. Other facts, conclusions and raw returned passages are preserved.
    Run this in the publication transaction/snapshot and persist the prepared
    records in that same transaction to avoid competing first representatives.
    """
    if (not isinstance(publication, dict) or set(publication) != {'facts', 'conclusions', 'data'}
            or not isinstance(publication['facts'], list) or not isinstance(publication['conclusions'], list)):
        raise ValueError('A publication requires exactly facts, conclusions and data')
    normalized = _copy(publication)
    all_rows = normalized['facts'] + normalized['conclusions']
    if (any(not isinstance(row, dict) or not _text(row.get('id')) for row in all_rows)
            or len({row['id'] for row in all_rows}) != len(all_rows)):
        raise ValueError('Publication fact and conclusion identities must be distinct')
    identities = {row['id']: source_identity(row) for row in normalized['facts']}
    if any(source_identity(row) is not None for row in normalized['conclusions']):
        raise ValueError('Original excerpts cannot be published as conclusions')
    if not any(value is not None for value in identities.values()):
        return normalized, []
    if (effect.get('run_id') != run_id or effect.get('status') != 'succeeded'
            or effect.get('accounting_unknown') or not _text(effect.get('id'))
            or not _hash(effect.get('result_ref')) or not _text(effect.get('metadata', {}).get('adapter_id'))):
        raise ValueError('Source normalization requires a settled tool Effect in the same run')
    original_ref = digest(publication)
    representatives, legacy_rows, read_records, mapping = {}, None, [], {}

    def stored_source(fid):
        fact = store.get(run_id, 'fact', fid)
        return {'id': fid, 'kind': fact['kind'], 'content': json.loads(store.read_blob(fact['content_ref'])),
                'provenance': fact['provenance']}

    def representative(identity, incoming):
        nonlocal legacy_rows
        token = digest(identity)
        if token in representatives:
            return representatives[token]
        indices = store.list(run_id, 'registration', id=_source_index_id(run_id, identity))
        if indices:
            index = indices[0]
            if (index.get('kind') != IDENTITY_KIND or index.get('schema_version') != IDENTITY_SCHEMA
                    or index.get('source_run_id') != run_id or index.get('source_identity') != identity
                    or index.get('source_identity_sha256') != token):
                raise ValueError('Canonical source identity index is inconsistent')
            row = stored_source(index['canonical_fact_id'])
            if source_identity(row) != identity:
                raise ValueError('Canonical source index does not bind its immutable original')
        else:
            if legacy_rows is None:
                # One scan supports historical per-effect/index facts. Indexed
                # sources avoid scanning or rewriting the old fact collection.
                legacy_rows = sorted((fact for fact in store.list(run_id, 'fact')
                    if isinstance(fact.get('provenance'), dict)
                    and fact['provenance'].get('delivery_kind') == 'source_excerpt'),
                    key=lambda fact: (fact['created_seq'], fact['id']))
            row = None
            for old in legacy_rows:
                if all(old['provenance'].get(key) == identity[key] for key in _SOURCE_FIELDS):
                    candidate = stored_source(old['id'])
                    if source_identity(candidate) == identity:
                        row = candidate
                        break
            if row is None:
                fid = _canonical_id(run_id, identity)
                if store.list(run_id, 'fact', id=fid):
                    raise ValueError('Canonical source identifier is occupied by different immutable evidence')
                row = {**incoming, 'id': fid}
        representatives[token] = row
        return row

    for row in normalized['facts']:
        identity = identities[row['id']]
        if identity is None:
            continue
        provenance = row['provenance']
        if (provenance.get('effect_id') != effect['id'] or provenance.get('result_ref') != effect['result_ref']
                or provenance.get('adapter_id') != effect['metadata']['adapter_id']):
            raise ValueError('Original excerpt does not belong to the current tool receipt')
        canonical = representative(identity, row)
        mapping[row['id']] = canonical['id']
        read_records.append({'id': 'literature_read_' + digest([run_id, effect['id'], row['id']])[:32],
            'kind': READ_KIND, 'schema_version': READ_SCHEMA, 'source_run_id': run_id,
            'effect_id': effect['id'], 'result_ref': effect['result_ref'],
            'adapter_id': effect['metadata']['adapter_id'], 'original_publication_sha256': original_ref,
            'original_fact_id': row['id'], 'canonical_fact_id': canonical['id'],
            'source_identity_sha256': digest(identity), 'source_identity': identity,
            'reused': (canonical['provenance'].get('effect_id') != effect['id']
                       or canonical['id'] != _canonical_id(run_id, identity)),
            'authority': 'tool_result_not_model_delivery'})

    def remap_ids(container):
        if not isinstance(container, dict) or 'passage_evidence_ids' not in container:
            return
        ids = container['passage_evidence_ids']
        known = set(mapping.values())
        if (not isinstance(ids, list) or any(not isinstance(fid, str) or fid not in mapping and fid not in known for fid in ids)):
            raise ValueError('Passage evidence pointers must refer to this publication original excerpts')
        container['passage_evidence_ids'] = list(dict.fromkeys(mapping.get(fid, fid) for fid in ids))

    facts, seen = [], {}
    for original in normalized['facts']:
        identity = identities[original['id']]
        row = _copy(representatives[digest(identity)]) if identity is not None else original
        if row['id'] in seen:
            if identity is None or seen[row['id']] != row:
                raise ValueError('Canonical original conflicts with another publication fact')
            continue
        if identity is None and row.get('provenance', {}).get('delivery_kind') == 'tool_observation':
            observation = row.get('content')
            if isinstance(observation, dict):
                remap_ids(observation.get('data'))
        seen[row['id']] = row
        facts.append(row)
    if set(seen).intersection(row['id'] for row in normalized['conclusions']):
        raise ValueError('Canonical original conflicts with a publication conclusion')
    normalized['facts'] = facts
    remap_ids(normalized['data'])
    if isinstance(normalized['data'], dict):
        remap_ids(normalized['data'].get('data'))
    return normalized, read_records
