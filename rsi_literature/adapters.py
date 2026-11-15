"""Host-bound literature adapters and exact source publication.

The model chooses literature operations, never provider endpoints, executables,
credential values or filesystem paths. Every operation uses the normal ledger
Effect path, including local reading and optional external research services.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import socket
import time
import types
import urllib.parse

from rsi_lab.adapters import Adapter
from rsi_lab.controller import identity
from rsi_lab.store import digest, encode
from . import catalog, documents, providers, remote


VERSION = '1.0.0'
NATIVE = frozenset(providers.BASES)
OPERATIONS = {
    'search_papers': ({'query'}, {'provider', 'query', 'filters', 'cursor', 'limit'}),
    'get_paper': ({'paper_ref'}, {'provider', 'paper_ref'}),
    'acquire_fulltext': (set(), {'provider', 'paper_ref', 'source_id', 'preferred_format'}),
    'read_document': ({'document_id'}, {'document_id', 'document_version', 'locator', 'cursor', 'max_bytes'}),
    'find_passages': ({'document_ids', 'query'}, {'document_ids', 'query', 'limit', 'max_bytes'}),
    'get_citations': ({'paper_ref', 'direction'}, {'provider', 'paper_ref', 'direction', 'cursor', 'limit'}),
    'build_reading_package': ({'document_id'}, {'provider', 'document_id'}),
    'render_document_page': ({'provider', 'document_id', 'page'}, {'provider', 'document_id', 'page'}),
    'extract_evidence': ({'provider', 'document_ids', 'prompt'}, {'provider', 'document_ids', 'prompt', 'examples', 'parameters', 'cursors'}),
    'run_review': ({'provider'}, {'provider', 'query', 'document_ids', 'action', 'task_id', 'parameters', 'cursors'}),
    'list_providers': (set(), set()),
    'provider_tools': ({'provider'}, {'provider', 'cursor'}),
    'call_provider_tool': ({'provider', 'tool', 'arguments'}, {'provider', 'tool', 'arguments'}),
}
DESCRIPTIONS = {
    'search_papers': 'Search a named literature source. Candidate metadata/abstracts are not full-text reading.',
    'get_paper': 'Resolve a paper identifier and its metadata, versions and accessible source locations.',
    'acquire_fulltext': 'Acquire a provider paper or an explicitly registered local source; return document_id for reading. For remote papers, preferred_format=pdf requests a renderable PDF and xml requests structured text; no fallback to another format.',
    'read_document': 'Read exact bounded source text by document_id and locator/cursor. Continue to read more.',
    'find_passages': 'Find literal text passages in registered documents; not an exhaustive semantic review.',
    'get_citations': 'Traverse references or citing papers with bounded pagination; citation edges are not support judgments.',
    'build_reading_package': 'Get a local reading index or use configured Poppler/GROBID/Paper2Skill parsing. Poppler preserves PDF pages and text coordinates.',
    'render_document_page': 'Render one registered PDF page as an image for visual inspection. Use after text search for figures, tables, formulas or uncertain extraction; rendering alone does not mean the page has been viewed.',
    'extract_evidence': 'Use a configured extraction framework on registered documents; results remain interpretations.',
    'run_review': 'Start/check a configured external review or run a deployed research framework; preserve task handles.',
    'list_providers': 'List nine core and six optional integrations, their configuration requirements and enabled state.',
    'provider_tools': 'Inspect the authorized literature tool schemas for a provider (MCP when applicable).',
    'call_provider_tool': 'Call a host-allowlisted literature tool through MCP.',
}


def input_schema(operation, enabled_providers):
    text = {'type': 'string', 'minLength': 1, 'maxLength': 20000}
    maybe_text = {'type': ['string', 'null']}
    supported = [provider for provider in enabled_providers
                 if operation == 'provider_tools' or operation in catalog.PROVIDERS[provider]['operations']]
    fields = {
        'provider': {'type': 'string', 'enum': supported + (['local'] if operation in {'acquire_fulltext', 'build_reading_package'} else [])},
        'query': text, 'paper_ref': {**text, 'description': 'DOI:..., PMID:..., PMCID:..., arXiv:..., or the selected provider ID'},
        'document_id': text, 'document_version': text, 'source_id': text, 'prompt': text, 'task_id': text, 'tool': text,
        'cursor': maybe_text, 'locator': maybe_text,
        'document_ids': {'type': 'array', 'items': text, 'minItems': 1, 'maxItems': 25},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 25},
        'max_bytes': {'type': 'integer', 'minimum': 4, 'maximum': 65536},
        'page': {'type': 'integer', 'minimum': 1, 'maximum': 1000},
        'preferred_format': {'type': 'string', 'enum': ['pdf', 'xml'],
            'description': 'Remote paper_ref only. Select only provider-returned candidates of this format; XML includes JATS/TEI. Omit to retain XML-first selection.'},
        'direction': {'type': 'string', 'enum': ['references', 'citing']},
        'action': {'type': 'string', 'enum': ['start', 'status', 'result']},
        'filters': {'type': 'object', 'description': 'Provider filters: year_from/year_to/open_access/sort. Semantic Scholar also supports exactly {"match":"title"} with limit=1 and no cursor; it is a single best title match, not keyword search.'},
        'arguments': {'type': 'object', 'description': 'Exact arguments from provider_tools; the tool name must be host-allowlisted'},
        'parameters': {'type': 'object', 'description': 'Provider-specific review/extraction options; no credentials, executable paths or model overrides'},
        'cursors': {'type': 'object', 'additionalProperties': {'type': 'string'}, 'description': 'Document IDs mapped to previously returned reading cursors'},
        'examples': {'type': 'array', 'items': {'type': 'object', 'required': ['text', 'extractions'],
            'properties': {'text': text, 'extractions': {'type': 'array', 'items': {'type': 'object',
                'required': ['extraction_class', 'extraction_text'], 'properties': {
                    'extraction_class': text, 'extraction_text': text, 'attributes': {'type': 'object'}}}}}}},
    }
    required, allowed = OPERATIONS[operation]
    schema = {'type': 'object', 'properties': {key: fields[key] for key in sorted(allowed)},
              'required': sorted(required), 'additionalProperties': False}
    if operation == 'acquire_fulltext':
        schema['oneOf'] = [{'required': ['paper_ref'], 'not': {'required': ['source_id']}},
                           {'required': ['source_id'], 'not': {'anyOf': [{'required': ['paper_ref']},
                                                                         {'required': ['preferred_format']}]}}]
    return schema


def _bound(fn, binding):
    result = types.FunctionType(fn.__code__, fn.__globals__, fn.__name__, (json.loads(encode(binding)),))
    result.__module__, result.__qualname__ = fn.__module__, fn.__qualname__
    return result


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')
    return value


def _string(value, name, maximum=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{name} must be a nonempty bounded string')
    return value


def _validate_input(value, schema, path='inputs'):
    kinds = schema.get('type', [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    types_by_name = {'object': dict, 'array': list, 'string': str, 'integer': int,
                     'boolean': bool, 'null': type(None)}
    if kinds and type(value) not in [types_by_name[kind] for kind in kinds]:
        raise ValueError(path + ' has an invalid JSON type')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(path + ' is outside the allowed values')
    if isinstance(value, str):
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 128 * 1024):
            raise ValueError(path + ' has an invalid length')
    elif type(value) is int:
        if value < schema.get('minimum', value) or value > schema.get('maximum', value):
            raise ValueError(path + ' is outside the allowed numeric range')
    elif isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 1000):
            raise ValueError(path + ' has an invalid item count')
        if 'items' in schema:
            for item in value:
                _validate_input(item, schema['items'], path + '[]')
    elif isinstance(value, dict):
        if not set(schema.get('required', [])) <= set(value):
            raise ValueError(path + ' is missing required fields')
        properties = schema.get('properties', {})
        extra = set(value) - set(properties)
        if schema.get('additionalProperties') is False and extra:
            raise ValueError(path + ' has unexpected fields')
        for key, item in value.items():
            if key in properties:
                _validate_input(item, properties[key], path + '.' + key)
            elif isinstance(schema.get('additionalProperties'), dict):
                _validate_input(item, schema['additionalProperties'], path + '.' + key)


def prepare(inputs, binding):
    operation, config = binding['operation'], binding['config']
    required, allowed = OPERATIONS[operation]
    if not isinstance(inputs, dict) or not required <= set(inputs) or not set(inputs) <= allowed:
        raise ValueError('Invalid fields for literature.' + operation)
    _validate_input(inputs, input_schema(operation, config['enabled_providers']))
    if len(encode(inputs)) > 128 * 1024:
        raise ValueError('Literature inputs exceed 128 KiB')
    value = json.loads(encode(inputs))
    for field in ('query', 'paper_ref', 'document_id', 'prompt', 'task_id', 'source_id', 'tool'):
        if field in value:
            _string(value[field], field)
    if 'provider' in value:
        if value['provider'] not in config['enabled_providers'] and value['provider'] != 'local':
            raise ValueError('Literature provider is not enabled by the host')
    provider = value.get('provider', config['default_provider'])
    if (operation not in {'read_document', 'find_passages', 'list_providers', 'provider_tools'}
            and not (operation == 'acquire_fulltext' and 'source_id' in value)
            and not (operation == 'build_reading_package' and value.get('provider', 'local') == 'local')
            and (provider == 'local' or operation not in catalog.PROVIDERS[provider]['operations'])):
        raise ValueError('Literature provider does not support the requested operation')
    if 'limit' in value:
        _integer(value['limit'], 'limit', 1, 25)
    if 'max_bytes' in value:
        _integer(value['max_bytes'], 'max_bytes', 4, 65536)
    for field in ('arguments', 'filters', 'parameters', 'cursors'):
        if field in value and not isinstance(value[field], dict):
            raise ValueError(field + ' must be an object')
    if 'document_ids' in value:
        if not isinstance(value['document_ids'], list) or not 1 <= len(value['document_ids']) <= 25:
            raise ValueError('document_ids must contain 1–25 registered documents')
        for item in value['document_ids']:
            _string(item, 'document_id', 100)
    if operation == 'acquire_fulltext' and (('paper_ref' in value) == ('source_id' in value)):
        raise ValueError('Supply exactly one of paper_ref or a host-registered source_id')
    if operation == 'acquire_fulltext' and 'source_id' in value and 'preferred_format' in value:
        raise ValueError('preferred_format is only supported for a remote paper_ref, not a local source_id')
    if 'source_id' in value and value['source_id'] not in config.get('local_sources', {}):
        raise ValueError('Local source is not registered by the host')
    if operation == 'get_citations' and value['direction'] not in {'references', 'citing'}:
        raise ValueError('direction must be references or citing')
    if operation == 'run_review' and value.get('action', 'start') not in {'start', 'status', 'result'}:
        raise ValueError('External review action must be start, status or result')
    return {'schema_version': 'rsi_literature_tool_v1', 'operation': operation,
            'binding_sha256': digest(binding), 'inputs': value}


def reservations(packet, binding):
    operation, inputs, config = binding['operation'], packet['inputs'], binding['config']
    provider = inputs.get('provider', config['default_provider'])
    count = 1
    native_op = {'search_papers': 'search', 'acquire_fulltext': 'fulltext_candidates'}.get(operation, operation)
    if provider in NATIVE and operation in {'search_papers', 'get_paper', 'get_citations', 'acquire_fulltext'}:
        count = max(1, providers.operation_max_requests(provider, native_op))
        if operation == 'acquire_fulltext' and 'source_id' not in inputs:
            count += 3  # One download and at most two explicit public redirects.
    elif operation in {'provider_tools', 'call_provider_tool'} and provider in {'asta', 'scite'}:
        count = 3  # initialize, initialized notification, one bounded operation.
    elif provider in {'edison', 'elicit'}:
        count = 3  # Explicit status/result calls are bounded; never poll internally.
    elif operation == 'build_reading_package' and provider == 'paper2skill':
        count = 2  # The upstream reader has distinct prepare and extract stages.
    elif provider == 'poppler' and operation in {'build_reading_package', 'render_document_page'}:
        count = 2  # Inspect the PDF, then extract text or render the selected page.
    return {'tool_calls': count}


def _result(status, data=None, *, provider='local', coverage=None, error=None, attempts=0):
    return {'status': status, 'data': {} if data is None else data,
            'provenance': {'provider': provider, 'attempt_count': attempts},
            'coverage': coverage or {}, 'continuation': {'next_cursor': None, 'complete_for_query': status == 'ok'},
            'errors': [] if error is None else [error]}


def _local_result(value, provider='local'):
    original = value.get('status', 'available')
    status = {'complete': 'ok', 'available': 'ok', 'partial': 'partial', 'unavailable': 'unavailable'}.get(original, original)
    result = _result(status, value, provider=provider, coverage=value.get('coverage', {}))
    result['continuation'] = {'next_cursor': value.get('next_cursor'),
                              'complete_for_query': value.get('complete', status == 'ok')}
    return result


def _public_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Full-text downloads require a public HTTPS URL')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Full-text URL resolves outside the public network')
    return url


def _download(candidate, config):
    url = candidate['url']
    attempts = 0
    for _ in range(3):
        try:
            _public_url(url)
            request_url = url
            if candidate.get('required_configuration'):
                if candidate['required_configuration'] != 'OPENALEX_API_KEY':
                    return None, None, attempts, 'Unsupported full-text authentication requirement'
                key = os.environ.get('OPENALEX_API_KEY')
                if not key:
                    return None, None, attempts, 'Set OPENALEX_API_KEY to use this full-text location'
                if urllib.parse.urlsplit(url).hostname == 'content.openalex.org':
                    separator = '&' if urllib.parse.urlsplit(url).query else '?'
                    request_url += separator + urllib.parse.urlencode({'api_key': key})
            attempts += 1
            response = remote.http_request('GET', request_url, {'User-Agent': 'RSI-Lab-Literature/1.0',
                'Accept': 'application/xml, application/pdf, text/html, text/plain'}, None,
                config['timeout'], config['max_download_bytes'])
        except (ValueError, OSError) as exc:
            return None, None, attempts, getattr(exc, 'code', 'download_transport_or_address_error')
        headers = {key.lower(): item for key, item in response['headers'].items()}
        if response['status'] in {301, 302, 303, 307, 308} and headers.get('location'):
            url = urllib.parse.urljoin(url, headers['location'])
            continue
        if response['status'] != 200:
            return None, None, attempts, 'Full-text endpoint returned HTTP ' + str(response['status'])
        # A provider's proposed type is not the server's actual response type.
        media_type = headers.get('content-type', 'application/octet-stream')
        return response['body'], media_type, attempts, None
    return None, None, attempts, 'Full-text redirect limit reached'


def _acquire(inputs, config, store, client):
    preferred = inputs.get('preferred_format')
    if preferred is not None and preferred not in {'pdf', 'xml'}:
        raise ValueError('preferred_format must be pdf or xml')
    if 'source_id' in inputs and preferred is not None:
        raise ValueError('preferred_format is only supported for a remote paper_ref, not a local source_id')
    if 'source_id' in inputs:
        source = config['local_sources'][inputs['source_id']]
        path = Path(source['path'])
        if path.is_symlink() or path.stat().st_size > config['max_download_bytes']:
            raise ValueError('Registered source is no longer a bounded regular original')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source['sha256']:
            raise ValueError('Host-registered local source changed; register its new version explicitly')
        metadata = {key: source[key] for key in ('source_url', 'paper_ref', 'license') if key in source}
        metadata.update(source_id=inputs['source_id'], provider='local', content_scope=source.get('content_scope', 'unknown'))
        manifest = store.ingest(raw, source['media_type'], metadata)
        return _local_result(store.reading_package(manifest['document_id']))
    provider = inputs.get('provider', config['default_provider'])
    locations = client.fulltext_candidates(provider, inputs['paper_ref'])
    candidates = locations.get('data', {}).get('candidates', [])
    if locations['status'] not in {'ok', 'partial'} or not candidates:
        if locations['status'] == 'ok':
            locations['status'] = 'unavailable'
            locations['errors'].append({'code': 'fulltext_unavailable', 'detail': 'No downloadable full-text candidate was returned', 'retryability': 'no'})
        return locations
    # Explicit preferences select only already returned provider candidates.
    # XML is normally more source-addressable than PDF. One candidate per call
    # keeps both retries and resource reservations visible to the controller.
    matching = candidates
    if preferred is not None:
        accepted = {'xml', 'jats', 'tei'} if preferred == 'xml' else {'pdf'}
        matching = [candidate for candidate in candidates if candidate.get('format') in accepted]
        if not matching:
            return _result('unavailable', {'paper': locations['data'].get('paper'), 'candidates': candidates,
                'requested_format': preferred}, provider=provider,
                attempts=locations.get('provenance', {}).get('attempt_count', 0),
                error={'code': 'fulltext_format_unavailable',
                    'detail': 'The provider returned no ' + preferred + ' candidate; no alternative format was downloaded',
                    'retryability': 'other_source'})
    candidate = sorted(matching, key=lambda row: (
        bool(row.get('required_configuration') and not os.environ.get(row['required_configuration'])),
        row.get('format') not in {'xml', 'jats', 'tei'}, row.get('format') != 'pdf'))[0]
    raw, media_type, attempts, error = _download(candidate, config)
    total = locations.get('provenance', {}).get('attempt_count', 0) + attempts
    if raw is None:
        return _result('unavailable', {'paper': locations['data'].get('paper'), 'candidates': candidates}, provider=provider,
            attempts=total, error={'code': 'fulltext_unavailable', 'detail': error, 'retryability': 'unknown'})
    base_type = media_type.split(';', 1)[0].strip().lower() if isinstance(media_type, str) else ''
    requires_pdf = preferred == 'pdf' or candidate.get('format') == 'pdf'
    if ((requires_pdf and (base_type != 'application/pdf' or not isinstance(raw, bytes) or not raw.lstrip().startswith(b'%PDF-')))
            or (preferred == 'xml' and base_type not in {'application/xml', 'text/xml', 'application/jats+xml', 'application/tei+xml'})):
        return _result('unavailable', {'paper': locations['data'].get('paper'), 'candidates': candidates,
            'requested_format': preferred, 'selected_format': candidate.get('format'), 'response_media_type': media_type},
            provider=provider, attempts=total, error={'code': 'fulltext_format_mismatch',
                'detail': 'The selected full-text response did not match its requested format and content type; no document was registered',
                'retryability': 'other_source'})
    # TEI is XML and uses the existing TEI normalizer; retain the original HTTP
    # media type separately when that server supplies the more specific label.
    acquired_media_type = media_type
    if base_type == 'application/tei+xml':
        media_type = 'application/xml'
    try:
        manifest = store.ingest(raw, media_type, {'provider': provider, 'paper_ref': inputs['paper_ref'],
            'source_url': candidate['url'], 'content_scope': 'unknown',
            'acquired_media_type': acquired_media_type,
            'provider_reported_scope': candidate.get('content_scope', 'unknown')})
    except (ValueError, OSError) as exc:
        return _result('failed', provider=provider, attempts=total,
            error={'code': getattr(exc, 'code', 'source_storage_error'),
                   'detail': 'The acquired source could not be registered', 'retryability': 'no'})
    result = _local_result(store.reading_package(manifest['document_id']), provider)
    result['provenance']['attempt_count'] = total
    result['provenance']['source_requests'] = locations.get('provenance', {}).get('requests', [])
    return result


def _mcp_settings(provider, config):
    spec = catalog.PROVIDERS[provider]
    settings = {'endpoint': spec['endpoint'], 'tools': spec['tools'], 'timeout': config['timeout'],
                'max_response_bytes': config['max_response_bytes'], **config['connectors'].get(provider, {})}
    if provider == 'scite':
        settings.setdefault('headers_env', {'Authorization': settings.get('credential_env', 'SCITE_ACCESS_TOKEN')})
    elif provider == 'asta':
        key = settings.get('credential_env', 'ASTA_TOOL_KEY')
        if os.environ.get(key):
            settings.setdefault('headers_env', {'x-api-key': key})
    # A host may narrow the official literature surface, not expose unrelated writes.
    if not set(settings['tools']) <= set(spec['tools']):
        raise ValueError('MCP tool selection exceeds the approved literature tool surface')
    return settings


def dispatch(operation, inputs, config, store, effect_id):
    provider = inputs.get('provider', config['default_provider'])
    if operation == 'list_providers':
        return _result('ok', {'providers': catalog.provider_catalog(config)})
    if operation == 'read_document':
        if inputs.get('document_version') and store.manifest(inputs['document_id'])['document_version'] != inputs['document_version']:
            raise ValueError('Document version differs from requested original')
        return _local_result(store.read(inputs['document_id'], locator=inputs.get('locator'), cursor=inputs.get('cursor'),
                                       max_bytes=inputs.get('max_bytes', 8000)))
    if operation == 'find_passages':
        return _local_result(store.find(inputs['document_ids'], inputs['query'], limit=inputs.get('limit', 5),
                                       max_bytes=inputs.get('max_bytes', 16000)))
    if operation == 'build_reading_package' and inputs.get('provider', 'local') == 'local':
        return _local_result(store.reading_package(inputs['document_id']))
    if operation == 'acquire_fulltext' and ('source_id' in inputs or provider in NATIVE):
        client = providers.ProviderClient(timeout=config['timeout'], max_response_bytes=config['max_response_bytes'])
        return _acquire(inputs, config, store, client)
    if provider in NATIVE and operation in {'search_papers', 'get_paper', 'get_citations'}:
        client = providers.ProviderClient(timeout=config['timeout'], max_response_bytes=config['max_response_bytes'])
        if operation == 'search_papers':
            return client.search(provider, inputs['query'], filters=inputs.get('filters'), cursor=inputs.get('cursor'), limit=inputs.get('limit', 10))
        if operation == 'get_paper':
            return client.get_paper(provider, inputs['paper_ref'])
        direction = 'cited_by' if inputs['direction'] == 'citing' else 'references'
        return client.get_citations(provider, inputs['paper_ref'], direction, cursor=inputs.get('cursor'), limit=inputs.get('limit', 10))
    if operation in {'provider_tools', 'call_provider_tool'} and provider in {'asta', 'scite'}:
        client = remote.MCPClient(_mcp_settings(provider, config))
        try:
            data = client.list_tools(inputs.get('cursor')) if operation == 'provider_tools' else client.call_tool(inputs['tool'], inputs['arguments'])
            result = _result('failed' if data.get('isError') else 'ok', data, provider=provider, attempts=client.attempt_count,
                coverage={'source': 'MCP tool response', 'original_paper_read': False})
            result['continuation']['next_cursor'] = data.get('next_cursor')
            result['continuation']['complete_for_query'] = not bool(data.get('next_cursor'))
            return result
        except remote.RemoteError as exc:
            result = _result('failed' if exc.dispatched else 'unavailable', provider=provider, attempts=client.attempt_count,
                error={'code': exc.code, 'detail': str(exc), 'retryability': 'unknown'})
            if exc.dispatched and operation == 'call_provider_tool':
                result['execution_unknown'] = True
            return result
    if operation == 'provider_tools':
        return _result('ok', {'provider': provider, 'operations': catalog.PROVIDERS[provider]['operations'],
            'tool_allowlist': config['connectors'].get(provider, {}).get('tools', [])}, provider=provider)
    settings = config['connectors'].get(provider, {})
    if provider in {'grobid', 'poppler', 'paper2skill'} and operation == 'build_reading_package':
        from .parsers import run_parser
        return run_parser(provider, settings, store, inputs['document_id'], effect_id)
    if provider == 'poppler' and operation == 'render_document_page':
        from .poppler import render_document_page
        return render_document_page(settings, {key: value for key, value in inputs.items() if key != 'provider'}, store, effect_id)
    if provider in {'langextract', 'paperqa'}:
        from .worker import run_sdk_worker
        if inputs.get('action', 'start') != 'start' or inputs.get('parameters'):
            return _result('unavailable', provider=provider, error={'code': 'unsupported_operation',
                'detail': 'This local SDK accepts a new bounded request, not a hosted task status or arbitrary parameters', 'retryability': 'no'})
        sdk_inputs = {key: value for key, value in inputs.items() if key not in {'provider', 'action', 'parameters'}}
        return run_sdk_worker(provider, settings, sdk_inputs, store, effect_id)
    if provider in {'elicit', 'edison'}:
        from .services import run_service
        if operation == 'search_papers' and provider == 'elicit':
            if inputs.get('cursor'):
                return _result('unavailable', provider=provider, error={'code': 'unsupported_pagination',
                    'detail': 'Elicit top-k search does not expose this continuation cursor', 'retryability': 'no'})
            service_inputs = {key: value for key, value in inputs.items() if key not in {'provider', 'cursor'}}
            service_inputs['action'] = 'search'
        elif operation == 'run_review':
            if inputs.get('document_ids') or inputs.get('cursors'):
                return _result('unavailable', provider=provider, error={'code': 'unsupported_documents',
                    'detail': 'This hosted review connector does not upload local documents', 'retryability': 'no'})
            service_inputs = {key: value for key, value in inputs.items() if key not in {'provider', 'parameters'}}
            service_inputs.setdefault('action', 'start')
            params = inputs.get('parameters', {})
            if set(params) & {'action', 'task_id', 'session_id', 'query'}:
                raise ValueError('Review parameters cannot replace request identity or action')
            service_inputs.update(params)
            if provider == 'elicit' and 'task_id' in service_inputs:
                service_inputs['session_id'] = service_inputs.pop('task_id')
        else:
            return _result('unavailable', provider=provider, error={'code': 'unsupported_operation',
                'detail': 'Hosted service does not implement ' + operation, 'retryability': 'no'})
        return run_service(provider, settings, service_inputs, store, effect_id)
    if provider == 'starling':
        from .projects import run_project
        return run_project(provider, settings, inputs, store, effect_id)
    return _result('unavailable', provider=provider, error={'code': 'unsupported_operation',
        'detail': 'This provider does not implement ' + operation, 'retryability': 'no'})


def execute(effect, raw, binding):
    packet = json.loads(raw)
    if packet.get('binding_sha256') != digest(binding):
        raise ValueError('Literature execution differs from its host binding')
    config, inputs, operation = binding['config'], packet['inputs'], binding['operation']
    started = time.monotonic()
    result = None
    try:
        store = documents.DocumentStore(config['document_root'])
        result = dispatch(operation, inputs, config, store, effect['id'])
        if operation == 'render_document_page' and result.get('status') in {'ok', 'partial'}:
            # Snapshot the verified artifact into the receipt before publication.
            # Recovery then uses immutable bytes, never a subsequently changed path.
            from .parsers import _read_file
            from rsi_lab.images import image_content_from_bytes
            rendered = result['data']['image']
            expected = store.root / 'jobs' / effect['id'] / 'page.png'
            if Path(rendered['artifact_path']) != expected:
                raise ValueError('Rendered image does not belong to this literature effect')
            image_bytes = _read_file(store.root, 'jobs/' + effect['id'] + '/page.png', maximum=1024 * 1024)
            metadata = {key: rendered[key] for key in ('media_type', 'sha256', 'bytes', 'width', 'height',
                                                      'page', 'source_document_id', 'source_sha256')}
            result['data']['image'] = image_content_from_bytes(image_bytes, metadata)
    except (ValueError, OSError, ImportError) as exc:
        # Expected configuration/contract failures are useful observations for the
        # agent. Unexpected exceptions remain unknown through Controller handling.
        code = getattr(exc, 'code', 'configuration_or_input_error')
        attempts = (result or {}).get('provenance', {}).get('attempt_count', 0)
        result = _result('failed' if attempts else 'unavailable', provider=inputs.get('provider', 'local'), attempts=attempts,
            error={'code': code, 'detail': str(exc) if isinstance(exc, (ValueError, remote.RemoteError)) else type(exc).__name__, 'retryability': 'no'})
    attempts = result.get('provenance', {}).get('attempt_count', 0)
    result.setdefault('provenance', {})['elapsed_seconds'] = time.monotonic() - started
    result['provenance']['adapter_version'] = VERSION
    costs = {'tool_calls': max(1, attempts)}
    return {'status': 'unknown' if result.get('execution_unknown') else 'succeeded',
            'actual_costs': costs, 'result': result,
            'provider_handle': result.get('data', {}).get('task_id') or result.get('data', {}).get('session_id')}


def publication(effect, receipt):
    result = receipt['result']
    if result.get('status') not in {'ok', 'partial', 'unavailable', 'failed'}:
        raise ValueError('Literature provider returned an invalid outcome status')
    provenance = {'effect_id': effect['id'], 'result_ref': effect.get('result_ref'),
                  'adapter_id': effect['metadata']['adapter_id'], **result.get('provenance', {})}
    data = result.get('data', {})
    passages = data.get('passages', []) if isinstance(data, dict) else []
    facts = []
    for index, passage in enumerate(passages):
        facts.append({'id': identity('literature_passage', effect['id'], index), 'kind': 'source_statement',
            'content': passage, 'provenance': {**provenance, 'document_id': passage.get('document_id'),
                'document_version': passage.get('document_version'), 'locator': passage.get('locator'),
                'source_sha256': passage.get('source_sha256'), 'delivery_kind': 'source_excerpt'}})
    observation = json.loads(encode(result))
    if passages:
        observation['data'].pop('passages', None)
        observation['data'].pop('text', None)
        observation['data']['passage_evidence_ids'] = [row['id'] for row in facts]
    image = data.get('image') if isinstance(data, dict) else None
    public_result = result
    if image is not None:
        from rsi_lab.images import validate_image_content
        image = validate_image_content(image)
        image_id = identity('literature_image', effect['id'])
        facts.append({'id': image_id, 'kind': 'source_statement', 'content': image,
            'provenance': {**provenance, 'delivery_kind': 'source_image',
                'document_id': image['source_document_id'], 'source_sha256': image['source_sha256'],
                'page': image['page'], 'image_sha256': image['sha256'],
                'interpretation': 'Rendered source page; visual inspection requires actual image delivery to the model.'}})
        image_metadata = {key: value for key, value in image.items() if key != 'data_base64'}
        observation['data']['image'] = image_metadata
        observation['data']['image_evidence_id'] = image_id
        public_result = json.loads(encode(result))
        public_result['data']['image'] = image_metadata
        public_result['data']['image_evidence_id'] = image_id
    facts.insert(0, {'id': identity('literature_observation', effect['id']), 'kind': 'source_statement',
        'content': observation, 'provenance': {**provenance, 'delivery_kind': 'tool_observation',
            'interpretation': 'A tool response is not a verified scientific conclusion; only delivered source excerpts were read.'}})
    return {'facts': facts, 'conclusions': [], 'data': public_result}


def configuration(document_root, supplied=None):
    supplied = {} if supplied is None else json.loads(encode(supplied))
    allowed = {'default_provider', 'enabled_providers', 'timeout', 'max_response_bytes', 'max_download_bytes', 'connectors', 'local_sources'}
    if not set(supplied) <= allowed:
        raise ValueError('Unknown literature host configuration fields')
    value = {'default_provider': 'europe_pmc', 'enabled_providers': list(catalog.DEFAULT_ENABLED_PROVIDERS), 'timeout': 20,
        'max_response_bytes': 2 * 1024 * 1024, 'max_download_bytes': 32 * 1024 * 1024,
        'connectors': {}, 'local_sources': {}, **supplied, 'document_root': str(Path(document_root).resolve())}
    if not isinstance(value['enabled_providers'], list) or not set(value['enabled_providers']) <= set(catalog.PROVIDERS):
        raise ValueError('Unknown literature provider in host configuration')
    if value['default_provider'] not in value['enabled_providers']:
        raise ValueError('The default provider must be enabled')
    _integer(value['timeout'], 'timeout', 1, 120)
    _integer(value['max_response_bytes'], 'max_response_bytes', 1024, 32 * 1024 * 1024)
    _integer(value['max_download_bytes'], 'max_download_bytes', 1024, 32 * 1024 * 1024)
    if not isinstance(value['connectors'], dict) or not set(value['connectors']) <= set(catalog.PROVIDERS):
        raise ValueError('Unknown provider connector configuration')
    for provider, settings in value['connectors'].items():
        if not isinstance(settings, dict):
            raise ValueError('Connector settings must be an object')
        if provider in NATIVE and 'credential_env' in settings:
            raise ValueError('Native literature providers use their standard environment names, not credential_env overrides')
        def check_secret_bindings(node):
            if isinstance(node, dict):
                forbidden = {'api_key', 'apikey', 'token', 'access_token', 'refresh_token', 'headers',
                             'password', 'secret', 'credentials', 'authorization', 'env'}
                if any(key.lower() in forbidden for key in node):
                    raise ValueError('Bind credentials by environment variable name, never raw values')
                for key, child in node.items():
                    if key == 'headers_env':
                        if not isinstance(child, dict) or any(not isinstance(name, str) or not name.isidentifier() for name in child.values()):
                            raise ValueError('headers_env values must be environment variable names')
                    else:
                        check_secret_bindings(child)
            elif isinstance(node, list):
                for child in node:
                    check_secret_bindings(child)
        check_secret_bindings(settings)
    sources = {}
    for source_id, spec in value['local_sources'].items():
        _string(source_id, 'source_id', 200)
        spec = {'path': spec} if isinstance(spec, str) else dict(spec)
        path = Path(spec['path']).resolve()
        if not path.is_file() or path.stat().st_size > value['max_download_bytes']:
            raise ValueError('Registered local source is absent or exceeds the size limit')
        sources[source_id] = {**spec, 'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'media_type': spec.get('media_type', mimetypes.guess_type(path.name)[0] or 'text/plain')}
    value['local_sources'] = sources
    return value


def make_adapters(document_root, config=None):
    config = configuration(document_root, config)
    source_paths = tuple(str(path) for path in Path(__file__).parent.glob('*.py'))
    adapters = []
    for operation, (required, allowed) in OPERATIONS.items():
        schema = input_schema(operation, config['enabled_providers'])
        # An operation with no enabled provider has no admissible invocation.
        # Keep its implementation available without exposing a dead tool menu.
        if 'provider' in required and not schema['properties']['provider']['enum']:
            continue
        binding = {'operation': operation, 'config': config}
        adapters.append(Adapter(adapter_id='literature.' + operation, version=VERSION,
            effect_kind='literature_' + operation,
            manifest={'scope': DESCRIPTIONS[operation],
                'input_schema': schema,
                'input_contract': {'required': sorted(required), 'allowed': sorted(allowed),
                    'provider_default': config['default_provider'], 'max_limit': 25},
                'output_contract': 'ToolResult with status, provenance, coverage, bounded data, continuation and errors',
                'host_binding_sha256': digest(binding)},
            prepare=_bound(prepare, binding), reservations=_bound(reservations, binding),
            execute=_bound(execute, binding), publication=publication, source_paths=source_paths))
    return adapters
