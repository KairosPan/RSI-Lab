"""Exact frozen-file deliveries for a cooperative live experiment host.

This producer does not answer scientific queries or follow references. It
delivers original UTF-8 bytes and their provenance, through one durable Effect
per request version. Failed lookup attempts remain pending; their receipts are
reused instead of repeatedly charging the same unchanged request.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from rsi_lab.controller import identity
from rsi_lab.store import digest, encode

MAX_SOURCE_BYTES = 2 * 1024 * 1024
FIELDS = frozenset({'original_text', 'original_source_sha256', 'source_sha256',
                    'provenance', 'source_ref', 'source_id', 'source_version', 'path'})
EFFECT_KIND = 'frozen_local_source_retrieval'


def freeze_sources(store, source_paths):
    """Snapshot explicit local originals; no basename or reference discovery."""
    if (not isinstance(source_paths, dict) or not 1 <= len(source_paths) <= 256
            or any(not isinstance(key, str) or not key.strip() or len(key) > 128
                   for key in source_paths)):
        raise ValueError('Supply one to 256 explicitly named source file paths')
    result = []
    for source_id, value in sorted(source_paths.items()):
        path = Path(value).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError('Frozen sources must be bounded regular files')
        with path.open('rb') as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError('Source exceeds its frozen input capacity')
        raw.decode('utf-8')
        result.append({'source_id': source_id, 'source_version': 1, 'path': str(path),
                       'sha256': digest(raw), 'source_ref': store.put_blob(raw), 'bytes': len(raw)})
    return result


def source_contracts(sources):
    """Publish executable lookup boundaries, never a scientific gap checklist."""
    result = {}
    for source in sources:
        sid = source['source_id']
        if sid in result:
            raise ValueError('Source identifiers must be unique')
        result[sid] = {'kind': 'single_frozen_local_file', 'source_version': source['source_version'],
            'object_identities': sorted({sid, 'source:' + sid, Path(source['path']).name,
                                        source['path'], source['sha256'], 'sha256:' + source['sha256']}),
            'supported_fields': sorted(FIELDS), 'follows_references': False,
            'scope': 'Exactly this frozen original. Other files must be separately listed authorized sources.',
            'query_answer_verified': False}
    return result


def _lookup(store, packet):
    req, source = packet['request'], packet['source']
    base = {'request_id': req['id'], 'request_version': req['version'],
            'source_id': req['source'], 'source_version': req['source_version'],
            'delivered': False, 'request_scope_verified': False, 'query_answer_verified': False}

    def unavailable(code, reason, **details):
        return {**base, 'status': 'pending', 'reason_code': code, 'reason': reason, **details}

    if req['source'] not in packet['authorized_sources']:
        return unavailable('source_not_authorized', 'The requested source is not authorized for this run')
    if source is None or source['source_version'] != req['source_version']:
        return unavailable('source_version_unavailable', 'The requested source/version is not frozen for this run')
    spec = req['query_spec']
    aliases = set(source_contracts([source])[source['source_id']]['object_identities'])
    unknown = [value for value in spec['object_ids'] if value not in aliases]
    if not spec['object_ids'] or unknown:
        return unavailable('requested_objects_unavailable', 'Requested identities do not all match this original',
                           unavailable_object_ids=unknown, available_object_aliases=sorted(aliases))
    fields = set(spec['fields']) - FIELDS
    if not spec['fields'] or fields:
        return unavailable('requested_fields_unsupported', 'This producer supplies originals and provenance, not semantic answers',
                           unsupported_fields=sorted(fields), supported_fields=sorted(FIELDS))
    hashes = {value.lower() for value in re.findall(r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])', spec['query'])}
    if hashes - {source['sha256']}:
        return unavailable('requested_artifact_hash_unavailable', 'The query names an artifact hash outside this frozen original',
                           unavailable_sha256=sorted(hashes - {source['sha256']}))
    try:
        raw = store.read_blob(source['source_ref'])
        if digest(raw) != source['sha256'] or len(raw) != source['bytes']:
            raise ValueError('Frozen source metadata differs from the archived bytes')
        original = raw.decode('utf-8')
    except (ValueError, OSError, UnicodeError) as exc:
        return unavailable('source_archive_unavailable', str(exc))
    provenance = {'source_id': source['source_id'], 'source_path': source['path'],
        'source_version': source['source_version'], 'original_source_sha256': source['sha256'],
        'original_source_ref': source['source_ref'], 'retrieval': 'Exact frozen local original',
        'query_answer_verified': False, 'request_scope_verified': True}
    content = {'source_id': source['source_id'], 'source_version': source['source_version'],
        'source_sha256': source['sha256'], 'original_source_sha256': source['sha256'],
        'source_ref': source['source_ref'], 'path': source['path'], 'provenance': provenance,
        'original_text': original, 'complete_source': True, 'query_answer_verified': False,
        'request_scope_verified': True, 'requested_fields_delivered': spec['fields'],
        'matched_object_ids': spec['object_ids']}
    if spec['max_pages'] < 1 or len(encode(content)) > spec['max_bytes']:
        return unavailable('source_exceeds_request_capacity', 'Complete original exceeds the request bound; no summary substituted',
                           required_bytes=len(encode(content)), max_bytes=spec['max_bytes'])
    return {**base, 'status': 'available', 'request_scope_verified': True,
            'content': content, 'provenance': provenance, 'source_sha256': source['sha256']}


def deliver_pending(runtime, sources):
    """Process pending file requests once each, then resume exact deliveries.

    Costs cover source lookup and byte validation; initial experiment setup and
    source freezing are host overhead. Unreconciled Effects are never resent.
    Returns one outcome per pending request, including concrete blocking reasons.
    """
    store, run, controller = runtime.store, runtime.run_id, runtime.controller
    source_contracts(sources)  # Validate uniqueness before creating any effects.
    by_id = {source['source_id']: source for source in sources}
    controller.recover(run)
    outcomes = []
    for req in store.list(run, 'request', status='created'):
        if 'query_spec' not in req:
            continue
        token = identity('source_lookup', req['id'], req['version'])
        existing = [effect for effect in store.list(run, 'effect', kind=EFFECT_KIND)
                    if effect.get('metadata', {}).get('lookup_token') == token]
        if existing:
            effect = existing[0]
        else:
            packet = {'schema_version': 'frozen_source_lookup_v1', 'request': req,
                'source': by_id.get(req['source']),
                'authorized_sources': runtime.config.get('authorized_sources', [])}
            work = controller.enqueue(run, 'evidence_retrieve', token, token + ':work',
                inputs={'request_id': req['id'], 'issue_id': req['issue_id'], 'request_version': req['version']})
            effect = controller.prepare_effect(run, work['id'], EFFECT_KIND, encode(packet),
                {'tool_calls': 1, 'cpu_seconds': 1, 'wall_seconds': 1}, token + ':prepare',
                metadata={'tool_role': 'source', 'lookup_token': token, 'request_id': req['id'], 'request_version': req['version'],
                          'issue_id': req['issue_id'], 'producer_sha256': digest(Path(__file__).read_bytes())})
        if effect['status'] == 'prepared':
            def execute(admitted, raw):
                if admitted['status'] != 'dispatching' or digest(raw) != admitted['packet_ref']:
                    raise ValueError('Lookup requires the exact dispatched packet')
                if digest(Path(__file__).read_bytes()) != admitted['metadata']['producer_sha256']:
                    return {'status': 'failed_definite', 'actual_costs': {'tool_calls': 0, 'cpu_seconds': 0, 'wall_seconds': 0},
                            'error': 'Frozen source producer changed before dispatch'}
                start, cpu = time.monotonic(), time.process_time()
                result = _lookup(store, json.loads(raw))
                return {'status': 'succeeded', 'result': result,
                    'actual_costs': {'tool_calls': 1, 'cpu_seconds': time.process_time() - cpu,
                                     'wall_seconds': time.monotonic() - start}}
            effect = controller.dispatch_effect(run, effect['id'], execute)
        if effect['status'] != 'succeeded' or effect.get('accounting_unknown'):
            outcomes.append({'request_id': req['id'], 'status': 'blocked_external', 'effect_id': effect['id'],
                             'reason': 'Lookup result or accounting remains unknown; no retry was dispatched'})
            continue
        receipt = json.loads(store.read_blob(effect['result_ref']))
        outcome = {**receipt['result'], 'effect_id': effect['id'], 'actual_costs': effect['actual_costs']}
        if outcome['status'] == 'available':
            # Delivery carries only the exact measured lookup result. This may
            # be replayed after a crash without rerunning the source producer.
            delivery = runtime.ingest_delivery(req['id'], outcome['content'], outcome['provenance'],
                                               source_version=req['source_version'])
            outcome.update(status='delivered', delivered=True, delivery_id=delivery['id'])
        outcomes.append(outcome)
    return outcomes
