"""Europe PMC metadata/abstract retrieval through a single durable HTTP effect.

Only a public method-topic catalog is sent externally. No free query, candidate,
score, objective identity, local path, or private memory enters the HTTP request.
"""
from __future__ import annotations

import datetime
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from rsi_lab.adapters import Adapter
from rsi_lab.store import Store, digest, encode

ADAPTER_ID = 'literature.search'
VERSION = '1.0'
EFFECT_KIND = 'literature_search'
PROVIDER = 'Europe PMC Articles REST API'
ENDPOINT = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
DOC_URL = 'https://europepmc.org/RestfulWebService'
TOPICS = {
    'black_box_optimization': '"molecular optimization" AND ("black box" OR "black-box" OR "goal-directed")',
    'sample_efficient_search': '"molecular optimization" AND ("sample efficient" OR "sample-efficient" OR "Bayesian optimization")',
    'molecular_diversity': '"molecular optimization" AND (diversity OR exploration)',
    'duplicate_filtering': '("molecular generation" OR "molecular optimization") AND (duplicates OR canonicalization OR uniqueness)',
    'surrogate_uncertainty': '("molecular optimization" OR "molecular design") AND ("uncertainty" OR "active learning")',
    'matched_molecular_pairs': '"matched molecular pairs" AND (optimization OR "activity cliffs")',
    'representation_bias': '("molecular optimization" OR "molecular generation") AND (representation OR benchmark) AND (bias OR limitations)',
    'budgeted_search': '"molecular optimization" AND ("oracle calls" OR "evaluation budget" OR "sample efficiency")',
}
LIMITS = {'max_results': 5, 'response_bytes': 1048576, 'abstract_chars': 12000,
          'socket_timeout_seconds': 5, 'request_deadline_seconds': 30}
RESERVATIONS = {'model_calls': 0, 'tokens': 0, 'tool_calls': 1, 'wall_seconds': 40, 'cpu_seconds': 5}


class _ReadStore(Store):
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.blobs = self.root / 'objects'
        self.db = sqlite3.connect((self.root / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True, isolation_level=None)
        self.db.row_factory = sqlite3.Row


def _configuration(store, run_id):
    config = store.run(run_id)['config']
    if (config.get('research_features', {}).get('literature') is not True
            or config.get('literature_run_root') != str(store.root)):
        raise ValueError('Literature retrieval requires its frozen enabled feature and run-root binding')
    maximum = config.get('literature_max_calls')
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError('Freeze an explicit literature_max_calls limit between 1 and 100')
    return config


def _cache_source(store, effect, source_packet):
    if (effect['status'] != 'succeeded' or effect.get('accounting_unknown') is not False
            or any(effect.get('held_reservations', {}).values())
            or source_packet.get('retrieval_mode') != 'external'):
        raise ValueError('Cached literature source must be a settled successful original retrieval')
    receipt = json.loads(store.read_blob(effect['result_ref']))
    folder = store.root / 'literature_calls' / effect['id']
    local = _validate_saved(folder, effect, source_packet)
    # Reconciliation adds provenance to the immutable execution receipt; it
    # does not change the response or its measured costs. Accept only that
    # exact, effect-bound enrichment, not arbitrary differences in either copy.
    if (local != {key: value for key, value in receipt.items() if key != 'reconciliation'}
            or ('reconciliation' in receipt
                and receipt['reconciliation'] != _reconciliation(effect['id'], local))):
        raise ValueError('Cached literature receipt differs from the immutable ledger result')
    return receipt


def _reconciliation(effect_id, receipt):
    return {'effect_id': effect_id, 'provenance': {
        'kind': 'complete_local_literature_receipt', 'receipt_sha256': digest(receipt),
        'response_sha256': receipt['result']['response_sha256'], 'network_resent': False}}


def _cancelled_before_dispatch(store, effect):
    """Only the controller's bound zero-cost cancellation releases query quota."""
    if (effect['status'] != 'failed_definite' or effect.get('never_dispatched') is not True
            or effect.get('accounting_unknown') is not False
            or any(effect.get('held_reservations', {}).values())
            or effect.get('actual_costs') != {name: 0 for name in effect['reservations']}):
        return False
    receipt = json.loads(store.read_blob(effect['result_ref']))
    return (receipt.get('status') == 'failed_definite' and receipt.get('never_dispatched') is True
            and receipt.get('effect_id') == effect['id'] and receipt.get('packet_hash') == effect['packet_hash']
            and receipt.get('provenance') == 'controller_prepared_state_transaction'
            and receipt.get('actual_costs') == effect['actual_costs'])


def _packet(store, inputs, exclude_effect=None):
    config = _configuration(store, inputs['run_id'])
    prior = sorted((e for e in store.list(inputs['run_id'], 'effect')
             if e.get('metadata', {}).get('adapter_id') == ADAPTER_ID
             and e['id'] != exclude_effect and not _cancelled_before_dispatch(store, e)), key=lambda e: e['created_seq'])
    if any(e['status'] in {'prepared', 'dispatching', 'unknown'} or e.get('accounting_unknown') for e in prior):
        raise ValueError('An earlier literature request is unresolved; reconcile before another request')
    packets = {e['id']: json.loads(store.read_blob(e['packet_ref'])) for e in prior}
    external = [e for e in prior if packets[e['id']].get('retrieval_mode') == 'external']
    matching = [e for e in external if packets[e['id']]['topic'] == inputs['topic']]
    cached = None
    if matching:
        source = matching[0]
        receipt = _cache_source(store, source, packets[source['id']]) if source['status'] == 'succeeded' else None
        if receipt is None:
            raise ValueError('This topic already has a request that failed; no automatic retry')
        cached = {'effect_id': source['id'], 'packet_ref': source['packet_ref'],
                  'receipt_ref': source['result_ref'], 'response_sha256': receipt['result']['response_sha256'],
                  'source_group_id': receipt['result']['source_group_id']}
    elif len(external) >= config['literature_max_calls']:
        raise ValueError('Frozen literature external-request cap exhausted')
    query = TOPICS[inputs['topic']]
    url = (receipt['result']['request_url'] if cached else ENDPOINT + '?' + urllib.parse.urlencode({
        'query': query, 'format': 'json', 'resultType': 'core', 'pageSize': inputs['max_results'], 'cursorMark': '*'}))
    return {'schema_version': 'literature_search_packet_v2', **inputs, 'provider': PROVIDER,
            'query': query, 'request_url': url, 'limits': LIMITS, 'call_ordinal': len(prior) + 1,
            'retrieval_mode': 'cache' if cached else 'external', 'cached_source': cached,
            'external_request_ordinal': None if cached else len(external) + 1,
            'max_calls': config['literature_max_calls'], 'implementation_sha256': digest(Path(__file__).read_bytes()),
            'outbound_scope': 'Fixed public method query only; local run identity is never transmitted'}


def _prepare(inputs):
    if not isinstance(inputs, dict) or set(inputs) != {'run_root', 'run_id', 'topic', 'max_results'}:
        raise ValueError('Literature input requires exactly run_root, run_id, topic and max_results; no free query')
    if (not isinstance(inputs['run_root'], str) or not Path(inputs['run_root']).is_absolute()
            or str(Path(inputs['run_root']).resolve()) != inputs['run_root']
            or not isinstance(inputs['run_id'], str) or not inputs['run_id']
            or not isinstance(inputs['topic'], str) or inputs['topic'] not in TOPICS or type(inputs['max_results']) is not int
            or not 1 <= inputs['max_results'] <= LIMITS['max_results']):
        raise ValueError('Require canonical run root, known public topic and 1..5 results')
    store = _ReadStore(inputs['run_root'])
    try:
        store.verify_history(inputs['run_id'])
        return _packet(store, inputs)
    finally:
        store.close()


def _authorize(effect, packet, raw):
    store = _ReadStore(packet['run_root'])
    try:
        store.verify_history(packet['run_id'])
        if (effect['status'] != 'dispatching' or effect['kind'] != EFFECT_KIND
                or effect['run_id'] != packet['run_id']
                or effect.get('metadata', {}).get('adapter_id') != ADAPTER_ID
                or effect.get('metadata', {}).get('adapter_version') != VERSION
                or store.get(packet['run_id'], 'effect', effect['id']) != effect
                or digest(raw) != effect['packet_ref'] or digest(raw) != effect['packet_hash']
                or store.read_blob(effect['packet_ref']) != raw):
            raise ValueError('Literature HTTP requires its exact persisted dispatching effect')
        inputs = {k: packet[k] for k in ('run_root', 'run_id', 'topic', 'max_results')}
        if _packet(store, inputs, exclude_effect=effect['id']) != packet:
            raise ValueError('Literature packet or frozen implementation changed after admission')
    finally:
        store.close()


def _write_new(path, raw):
    with path.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(packet):
    """One GET, no redirects/retries. Preserve even truncated/error response bytes."""
    started = time.monotonic()
    raw, status, headers, complete, error = b'', None, {}, False, None
    request = urllib.request.Request(packet['request_url'], headers={
        'Accept': 'application/json', 'Accept-Encoding': 'identity', 'User-Agent': 'RSI-Lab-literature/1.0'})
    opener = urllib.request.build_opener(_NoRedirect())
    stream = None
    try:
        try:
            stream = opener.open(request, timeout=LIMITS['socket_timeout_seconds'])
        except urllib.error.HTTPError as exc:
            stream = exc  # Known HTTP failure still has auditable response bytes.
        status = stream.status if hasattr(stream, 'status') else stream.code
        headers = {k.lower(): v for k, v in stream.headers.items()
                   if k.lower() in {'content-type', 'content-length', 'date', 'retry-after', 'etag'}}
        while True:
            if time.monotonic() - started > LIMITS['request_deadline_seconds']:
                raise TimeoutError('Request deadline reached')
            chunk = stream.read1(min(65536, LIMITS['response_bytes'] + 1 - len(raw)))
            if not chunk:
                complete = True
                break
            raw += chunk
            if len(raw) > LIMITS['response_bytes']:
                error = 'response_byte_limit_exceeded'
                break
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        error = type(exc).__name__ + ': ' + str(exc)
    finally:
        if stream is not None:
            stream.close()
    return {'raw': raw, 'http_status': status, 'headers': headers, 'response_complete': complete,
            'error': error, 'finished_at_utc': _utc_now()}


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)
    def handle_starttag(self, tag, attrs):
        if tag.lower() in {'p', 'br', 'h1', 'h2', 'h3', 'h4', 'title'}:
            self.parts.append('\n')


def _text(value, maximum):
    if value is None:
        return '', False
    if not isinstance(value, str):
        raise ValueError('Provider text field has an unexpected type')
    parser = _PlainText(); parser.feed(value); parser.close()
    plain = ''.join(parser.parts).strip()
    return plain[:maximum], len(plain) > maximum


def _parse(raw, packet):
    response = json.loads(raw)
    hits = response.get('hitCount')
    rows = response.get('resultList', {}).get('result')
    if type(hits) is not int or hits < 0 or not isinstance(rows, list) or len(rows) > packet['max_results']:
        raise ValueError('Provider response has no valid bounded search result list')
    if hits < len(rows) or (hits == 0 and rows):
        raise ValueError('Provider hit count contradicts returned records')
    records, incomplete = [], False
    for row in rows:
        source, publication_id = row.get('source'), row.get('id')
        if (not isinstance(source, str) or not source or len(source) > 32
                or not isinstance(publication_id, str) or not publication_id or len(publication_id) > 128):
            raise ValueError('Provider result lacks a bounded publication identity')
        title, title_cut = _text(row.get('title'), 2048)
        abstract, abstract_cut = _text(row.get('abstractText'), LIMITS['abstract_chars'])
        author, author_cut = _text(row.get('authorString'), 2048)
        journal, journal_cut = _text(row.get('journalInfo', {}).get('journal', {}).get('title'), 1024)
        incomplete |= not abstract or abstract_cut or title_cut or author_cut or journal_cut
        doi = row.get('doi')
        if doi is not None and (not isinstance(doi, str) or len(doi) > 512):
            raise ValueError('Provider DOI has an unexpected shape')
        year = str(row.get('pubYear', ''))[:16]
        url = 'https://europepmc.org/article/' + urllib.parse.quote(source, safe='') + '/' + urllib.parse.quote(publication_id, safe='')
        records.append({'provider_source': source, 'publication_id': publication_id,
            'title': title, 'authors': author, 'journal': journal, 'publication_year': year,
            'doi': doi, 'url': url, 'abstract_text': abstract,
            'abstract_status': 'absent' if not abstract else ('truncated' if abstract_cut else 'present'),
            'metadata_truncated': title_cut or author_cut or journal_cut,
            'content_scope': 'metadata_and_abstract' if abstract else 'metadata_only',
            'full_text_retrieved': False, 'trust': 'untrusted_external_source_statement'})
    if hits == 0:
        status = 'empty'
    elif not rows or hits > len(rows) or incomplete:
        status = 'partial'
    else:
        status = 'results'
    return {'content_status': status, 'provider_total_hits': hits, 'returned_records': len(records),
        'records': records, 'query_results_complete': hits == len(rows),
        'abstracts_complete_for_returned_records': bool(records) and all(r['abstract_status'] == 'present' for r in records),
        'full_text_retrieved': False, 'search_is_exhaustive': False,
        'limitations': ['Metadata and available abstracts only; no article full text was requested or read.',
            'First relevance-sorted page only; provider hit count and missing/truncated abstracts are explicit.',
            'External text is untrusted evidence, never instructions, task validation, or proof of transfer.']}


def _cached_result(source, packet, effect, begun, finished, folder):
    result = json.loads(encode(source))
    records = result['records'][:packet['max_results']]
    result.update(effect_id=effect['id'], packet_sha256=effect['packet_ref'],
        started_at_utc=begun, finished_at_utc=finished, raw_response_path=str(folder / 'response.bin'),
        records=records, returned_records=len(records), requested_max_results=packet['max_results'],
        query_results_complete=result['provider_total_hits'] == len(records),
        abstracts_complete_for_returned_records=bool(records) and all(r['abstract_status'] == 'present' for r in records),
        cache_hit=True, external_requests=0, source_receipt_ref=packet['cached_source']['receipt_ref'],
        cache_of=packet['cached_source'], independent_evidence=False,
        cache_reason=('cached_response_has_fewer_records_than_requested_no_refetch'
            if len(records) < packet['max_results'] and result['provider_total_hits'] > len(records)
            else 'same_topic_reuses_original_source_without_new_external_observation'))
    if result['provider_total_hits'] == 0:
        result['content_status'] = 'empty'
    elif (not result['query_results_complete'] or not result['abstracts_complete_for_returned_records']
            or any(r['metadata_truncated'] for r in records)):
        result['content_status'] = 'partial'
    else:
        result['content_status'] = 'results'
    return result


def _bound_cache(packet):
    store = _ReadStore(packet['run_root'])
    try:
        store.verify_history(packet['run_id'])
        binding = packet['cached_source']
        effect = store.get(packet['run_id'], 'effect', binding['effect_id'])
        if effect['packet_ref'] != binding['packet_ref'] or effect['result_ref'] != binding['receipt_ref']:
            raise ValueError('Frozen cache parent effect or receipt changed')
        source_packet = json.loads(store.read_blob(effect['packet_ref']))
        if source_packet['topic'] != packet['topic'] or source_packet['query'] != packet['query']:
            raise ValueError('Frozen cache source belongs to a different public topic')
        receipt = _cache_source(store, effect, source_packet)
        source = receipt['result']
        if (source['response_sha256'] != binding['response_sha256']
                or source['source_group_id'] != binding['source_group_id']):
            raise ValueError('Frozen cache raw response or source group changed')
        raw = (store.root / 'literature_calls' / effect['id'] / 'response.bin').read_bytes()
        return source, raw
    finally:
        store.close()


def _validate_saved(folder, effect, packet):
    receipt = json.loads((folder / 'receipt.json').read_bytes())
    result = receipt['result']
    if (result['effect_id'] != effect['id'] or result['packet_sha256'] != effect['packet_ref']
            or result['request_url'] != packet['request_url'] or result['query'] != packet['query']
            or result['provider'] != PROVIDER or receipt['status'] not in {'succeeded', 'failed_definite', 'unknown'}):
        raise ValueError('Archived literature receipt binding differs from its admitted effect')
    costs = receipt.get('actual_costs', {})
    if (set(costs) != set(RESERVATIONS) or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in costs.values())
            or costs['tool_calls'] != 1 or costs['model_calls'] != 0 or costs['tokens'] != 0):
        raise ValueError('Archived literature receipt has invalid measured costs')
    if receipt['status'] == 'succeeded' and (result['http_status'] != 200 or result['response_complete'] is not True):
        raise ValueError('Successful literature receipt needs a complete HTTP 200 response')
    raw = (folder / 'response.bin').read_bytes()
    if len(raw) != result['response_bytes'] or digest(raw) != result['response_sha256']:
        raise ValueError('Archived literature response bytes changed')
    if receipt['status'] == 'succeeded':
        if packet['retrieval_mode'] == 'cache':
            source, original_raw = _bound_cache(packet)
            expected = _cached_result(source, packet, effect, result['started_at_utc'], result['finished_at_utc'], folder)
            if raw != original_raw or result != expected:
                raise ValueError('Cached literature result differs from its immutable parent')
        elif any(result[k] != v for k, v in _parse(raw, packet).items()):
            raise ValueError('Published literature records differ from the archived raw response')
    return receipt


def _execute(effect, packet_bytes):
    started, cpu = time.monotonic(), time.process_time()
    packet = json.loads(packet_bytes)
    _authorize(effect, packet, packet_bytes)
    folder = Path(packet['run_root']) / 'literature_calls' / effect['id']
    folder.parent.mkdir(exist_ok=True)
    try:
        folder.mkdir()
    except FileExistsError:
        if (folder / 'receipt.json').is_file():
            return _validate_saved(folder, effect, packet)
        return {'status': 'unknown', 'actual_costs': {},
                'reason': 'Literature request may already have been sent; no complete local receipt, no automatic retry'}
    begun = _utc_now()
    _write_new(folder / 'binding.json', encode({'effect_id': effect['id'], 'packet_sha256': digest(packet_bytes),
                                              'request_url': packet['request_url'], 'started_at_utc': begun}))
    if packet['retrieval_mode'] == 'cache':
        source, raw = _bound_cache(packet)
        _write_new(folder / 'response.bin', raw)
        result = _cached_result(source, packet, effect, begun, _utc_now(), folder)
        receipt = {'status': 'succeeded', 'actual_costs': {'model_calls': 0, 'tokens': 0, 'tool_calls': 1,
            'wall_seconds': time.monotonic() - started, 'cpu_seconds': time.process_time() - cpu}, 'result': result}
        _write_new(folder / 'receipt.json', encode(receipt))
        return receipt
    fetched = _fetch(packet)
    _write_new(folder / 'response.bin', fetched['raw'])
    result = {'effect_id': effect['id'], 'packet_sha256': digest(packet_bytes), 'provider': PROVIDER,
        'topic': packet['topic'], 'query': packet['query'], 'request_url': packet['request_url'],
        'started_at_utc': begun, 'finished_at_utc': fetched['finished_at_utc'],
        'http_status': fetched['http_status'], 'response_headers': fetched['headers'],
        'response_complete': fetched['response_complete'], 'response_bytes': len(fetched['raw']),
        'response_sha256': digest(fetched['raw']), 'raw_response_path': str(folder / 'response.bin'),
        'full_text_retrieved': False, 'content_status': 'failed', 'records': [], 'error': fetched['error'],
        'cache_hit': False, 'external_requests': 1, 'source_effect_id': effect['id'], 'source_receipt_ref': None,
        'source_group_id': 'literature_source_' + digest({'run_id': effect['run_id'], 'effect_id': effect['id'],
                                                       'response_sha256': digest(fetched['raw'])})[:24],
        'retrieval_started_at_utc': begun, 'retrieved_at_utc': fetched['finished_at_utc'],
        'independent_evidence': False, 'requested_max_results': packet['max_results']}
    if fetched['error'] and fetched['error'] != 'response_byte_limit_exceeded':
        status = 'unknown'  # A dispatched request without a complete reply must not be resent silently.
        result['content_status'] = 'unknown'
    elif fetched['http_status'] != 200 or not fetched['response_complete']:
        status = 'failed_definite'
        result['error'] = result['error'] or 'HTTP response was not a complete 200 response'
    else:
        try:
            result.update(_parse(fetched['raw'], packet))
            status = 'succeeded'
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            status = 'failed_definite'
            result['error'] = 'Invalid provider response: ' + str(exc)
    receipt = {'status': status, 'actual_costs': {'model_calls': 0, 'tokens': 0, 'tool_calls': 1,
        'wall_seconds': time.monotonic() - started, 'cpu_seconds': time.process_time() - cpu}, 'result': result}
    _write_new(folder / 'receipt.json', encode(receipt))
    return receipt


def recover_receipt(run_root, run_id, effect_id):
    """Read a complete known local receipt for explicit Controller.reconcile; never resend."""
    store = _ReadStore(run_root)
    try:
        store.verify_history(run_id)
        _configuration(store, run_id)
        effect = store.get(run_id, 'effect', effect_id)
        if effect['kind'] != EFFECT_KIND or effect.get('metadata', {}).get('adapter_id') != ADAPTER_ID:
            raise ValueError('Recovery effect belongs to another adapter')
        packet = json.loads(store.read_blob(effect['packet_ref']))
        if packet['implementation_sha256'] != digest(Path(__file__).read_bytes()):
            raise ValueError('Recovery requires the original literature implementation')
        folder = Path(run_root) / 'literature_calls' / effect_id
        if not (folder / 'receipt.json').is_file():
            return None
        receipt = _validate_saved(folder, effect, packet)
        if receipt['status'] == 'unknown':
            return None
        return {**receipt, 'reconciliation': _reconciliation(effect_id, receipt)}
    finally:
        store.close()


def _publication(effect, receipt):
    if receipt['status'] != 'succeeded':
        raise ValueError('Unknown or failed literature requests cannot publish evidence')
    result = receipt['result']
    if result['effect_id'] != effect['id'] or result['packet_sha256'] != effect['packet_ref']:
        raise ValueError('Literature publication is not bound to its admitted effect')
    return {'facts': [{'id': 'literature_result_' + digest(effect['id'])[:24], 'kind': 'source_statement',
        'content': result, 'provenance': {'effect_id': effect['id'], 'adapter_id': ADAPTER_ID,
        'adapter_version': VERSION, 'provider': PROVIDER, 'request_url': result['request_url'],
        'query': result['query'], 'response_sha256': result['response_sha256'],
        'retrieved_at_utc': result['retrieved_at_utc'], 'execution_ref': effect['result_ref'],
        'source_group_id': result['source_group_id'], 'source_effect_id': result['source_effect_id'],
        'source_receipt_ref': result['source_receipt_ref'] or effect['result_ref'],
        'cache_hit': result['cache_hit'], 'external_requests': result['external_requests'],
        'independent_evidence': False,
        'trust': 'untrusted_external_source_statement', 'full_text_retrieved': False,
        'query_answer_verified': False, 'current_task_validation': False}}],
        'conclusions': [], 'data': result}


# Keep descriptor callbacks thin: their bytecode is stable across repeated calls.
def prepare(inputs):
    return _prepare(inputs)


def reservations(packet):
    return dict(RESERVATIONS)


def execute(effect, packet_bytes):
    return _execute(effect, packet_bytes)


def publication(effect, receipt):
    return _publication(effect, receipt)


def make_adapter():
    return Adapter(adapter_id=ADAPTER_ID, version=VERSION, effect_kind=EFFECT_KIND,
        manifest={'domain': 'scholarly_method_retrieval', 'provider': PROVIDER, 'official_documentation': DOC_URL,
            'scope': 'Fixed public molecular-optimization method topics; not arbitrary literature queries',
            'input_contract': {'run_root': 'exact frozen absolute run directory', 'run_id': 'exact run identity',
                'topic': sorted(TOPICS), 'max_results': 'integer 1..5'},
            'output_contract': 'Untrusted metadata and available abstracts with exact response provenance; never full text',
            'limits': LIMITS, 'cache_policy': 'Same topic reuses its successful settled original receipt, never independent evidence',
            'external_request_cap': 'literature_max_calls counts original external requests; cache calls use total tool budget',
            'retry_policy': 'Unknown or failed retrieval is never retried automatically'},
        prepare=prepare, reservations=reservations, execute=execute, publication=publication)
