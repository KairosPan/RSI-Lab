"""Explicit optional SDK bridges; imports and model choices belong to the host.

Contracts checked against the official sources on 2026-11-15:
https://github.com/google/langextract/blob/main/langextract/extraction.py
https://github.com/google/langextract/blob/main/langextract/core/data.py
https://github.com/Future-House/paper-qa/blob/main/src/paperqa/docs.py
https://github.com/Future-House/paper-qa/blob/main/src/paperqa/settings.py

These synchronous entrypoints belong in the host's bounded worker process.
SDK outputs remain interpretations. Nested usage is explicitly unmeasured;
an exception after invocation is unknown, never an invitation to auto-retry.
"""
from __future__ import annotations

import asyncio
import dataclasses
from enum import Enum
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile

from .documents import DocumentError

# LiteLLM otherwise fetches a cost map during import, before an admitted SDK
# invocation. Use its packaged map so importing these bridges stays offline.
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'


class _Unavailable(ValueError):
    pass


def _sdk(module_name, package, state):
    module = importlib.import_module(module_name)
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        version = getattr(module, '__version__', None)
    state['sdk_package'] = {'name': package, 'version': version}
    return module


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 16000:
        raise ValueError(name + ' must be bounded nonempty text')
    return value


def _limit(settings, key, default, maximum):
    value = settings.get(key, default)
    if type(value) is not int or not 4 <= value <= maximum:
        raise ValueError('Invalid host limit: ' + key)
    return value


def _json_value(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    elif dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    # Do not stringify arbitrary SDK objects: repr can contain tokens or paths.
    raise ValueError('SDK returned a value without a supported JSON representation')


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _sources(inputs, settings, document_store):
    ids = inputs.get('document_ids')
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 8 or
            any(not isinstance(did, str) for did in ids) or len(set(ids)) != len(ids)):
        raise ValueError('Provide one to eight distinct registered document IDs')
    cursors = inputs.get('cursors', {})
    if not isinstance(cursors, dict) or not set(cursors).issubset(ids):
        raise ValueError('Cursors must belong to the requested documents')
    per_doc = _limit(settings, 'max_input_bytes', 16000, 65536)
    total = _limit(settings, 'max_total_input_bytes', 65536, 262144)
    sources, pages, reads, next_cursors = [], [], [], {}
    for did in ids:
        if total < 4:
            raise ValueError('Requested documents exceed the host input byte allowance')
        manifest = document_store.manifest(did)
        if manifest['status'] != 'available':
            raise _Unavailable('A requested document has no available normalized text')
        read = document_store.read(did, cursor=cursors.get(did), max_bytes=min(per_doc, total))
        if not read['text'].strip():
            raise _Unavailable('A requested document range has no usable source text')
        total -= len(read['text'].encode('utf-8'))
        sources.append({key: manifest[key] for key in ('document_id', 'document_version',
            'source_sha256', 'text_sha256', 'parser_version', 'content_scope', 'source_metadata')} | {
                'input_char_range': read['char_range'], 'coverage': read['coverage']})
        pages.extend(read['passages'])
        reads.append(read)
        if read.get('next_cursor'):
            next_cursors[did] = read['next_cursor']
    return sources, pages, reads, next_cursors


def _langextract(settings, inputs, document_store, state):
    if set(inputs) - {'document_ids', 'prompt', 'examples', 'cursors'}:
        raise ValueError('LangExtract accepts document IDs, prompt, examples and optional source cursors')
    model = settings.get('model')
    if not isinstance(model, str) or not model.strip():
        raise _Unavailable('Configure a host LangExtract model explicitly')
    prompt = _text(inputs.get('prompt'), 'prompt')
    example_values = inputs.get('examples')
    if not isinstance(example_values, list) or not 1 <= len(example_values) <= 20:
        raise ValueError('LangExtract requires one to twenty explicit few-shot examples')
    module = _sdk('langextract', 'langextract', state)
    examples = []
    for row in example_values:
        if not isinstance(row, dict) or set(row) != {'text', 'extractions'} or not isinstance(row['extractions'], list):
            raise ValueError('An example contains text and extractions')
        text = _text(row['text'], 'example text')
        extractions = []
        for extraction in row['extractions']:
            if (not isinstance(extraction, dict) or set(extraction) -
                    {'extraction_class', 'extraction_text', 'attributes'}):
                raise ValueError('Unexpected example extraction fields')
            cls = _text(extraction.get('extraction_class'), 'extraction_class')
            quote = _text(extraction.get('extraction_text'), 'extraction_text')
            attributes = extraction.get('attributes', {})
            if not isinstance(attributes, dict) or quote not in text:
                raise ValueError('Examples need literal source quotations and object attributes')
            extractions.append(module.data.Extraction(extraction_class=cls, extraction_text=quote, attributes=attributes))
        examples.append(module.data.ExampleData(text=text, extractions=extractions))
    sources, pages, reads, cursors = _sources(inputs, settings, document_store)
    state.update(sources=sources, cursors=cursors)
    options = settings.get('extract_options', {})
    allowed = {'max_char_buffer', 'batch_length', 'max_workers', 'temperature', 'model_url',
               'extraction_passes', 'context_window_chars', 'language_model_params', 'fence_output', 'use_schema_constraints'}
    if not isinstance(options, dict) or set(options) - allowed:
        raise ValueError('Unsupported host LangExtract extract_options')
    options = {'max_workers': 1, 'extraction_passes': 1, **options}
    rows, grounded = [], []
    for source, read in zip(sources, reads):
        state['sdk_calls_observed'] += 1
        result = module.extract(text_or_documents=read['text'], prompt_description=prompt,
            examples=examples, model_id=model, fetch_urls=False, show_progress=False,
            resolver_params={'suppress_parse_errors': False}, **options)
        rows.append({'document_id': source['document_id'], 'raw_result': _json_value(result)})
        for annotated in result if isinstance(result, list) else [result]:
            for extraction in _get(annotated, 'extractions', []) or []:
                interval = _get(extraction, 'char_interval')
                start, end = _get(interval, 'start_pos'), _get(interval, 'end_pos')
                quote = _get(extraction, 'extraction_text')
                located = (type(start) is int and type(end) is int and 0 <= start < end <= len(read['text'])
                    and isinstance(quote, str) and read['text'][start:end] == quote)
                grounded.append({'document_id': source['document_id'], 'document_version': source['document_version'],
                    'extraction': _json_value(extraction), 'location_status': 'located' if located else 'unlocated',
                    'source_char_range': [read['char_range'][0] + start, read['char_range'][0] + end] if located else None,
                    'authority': 'model_extraction_with_literal_span_check_not_fact_validation'})
    state.update(data={'interpretations': rows, 'grounded_extractions': grounded, 'sources': sources, 'passages': pages},
        sources=sources, cursors=cursors)
    if any(row['location_status'] == 'unlocated' for row in grounded):
        state['limitations'].append('Some extractions have missing or nonmatching source offsets and remain unlocated.')


def _paperqa(settings, inputs, document_store, state):
    if set(inputs) - {'document_ids', 'query', 'cursors'}:
        raise ValueError('PaperQA accepts registered document IDs, query and optional source cursors')
    if any(not isinstance(settings.get(name), str) or not settings[name].strip() for name in ('model', 'embedding')):
        raise _Unavailable('Configure host PaperQA model and embedding explicitly')
    query = _text(inputs.get('query'), 'query')
    module = _sdk('paperqa', 'paper-qa', state)
    sources, pages, reads, cursors = _sources(inputs, settings, document_store)
    state.update(sources=sources, cursors=cursors)
    options = settings.get('settings', {})
    if not isinstance(options, dict) or set(options) - {'llm_config', 'summary_llm_config', 'embedding_config', 'temperature', 'answer'}:
        raise ValueError('Unsupported host PaperQA settings')
    # Disable metadata expansion and multimodal model defaults: the corpus here
    # consists solely of the exact normalized text ranges registered above.
    with tempfile.TemporaryDirectory(prefix='rsi-paperqa-') as directory:
        # IndexSettings defaults to creating ~/.pqa/indexes even when only
        # constructing Settings. Bind all index paths before it is instantiated.
        configured = module.Settings(**options, llm=settings['model'],
            summary_llm=settings.get('summary_model', settings['model']), embedding=settings['embedding'],
            parsing={'use_doc_details': False, 'multimodal': False},
            agent={'index': {'index_directory': str(Path(directory) / 'index'),
                             'paper_directory': directory}})
        docs = module.Docs()

        async def review():
            for source, read in zip(sources, reads):
                path = Path(directory) / (source['document_id'] + '.txt')
                path.write_text(read['text'], encoding='utf-8')
                state['sdk_calls_observed'] += 1
                await docs.aadd(str(path), settings=configured, docname=source['document_id'],
                    citation='Registered source ' + source['document_id'] + '; normalized range ' + str(read['char_range']))
            state['sdk_calls_observed'] += 1
            return await docs.aquery(query, settings=configured)

        # The host runs this synchronous entrypoint in a fresh bounded worker.
        # paper-qa 2026.8.12 exposes aadd/aquery, with no add/query aliases.
        result = asyncio.run(review())
        state['data'] = {'raw_result': _json_value(result), 'sources': sources, 'passages': pages,
            'authority': 'retrieval_augmented_interpretation_not_original_source',
            'citation_mapping': {source['document_id']: source for source in sources}}
    state.update(sources=sources, cursors=cursors)


def run_sdk(provider, settings, inputs, document_store, effect_id):
    """Run one admitted host SDK operation and return a source-bound ToolResult."""
    state = {'status': 'ok', 'data': {}, 'errors': [], 'sdk_calls_observed': 0,
        'sources': [], 'cursors': {}, 'limitations': [
            'SDK-internal model, embedding and HTTP usage is not fully metered by this bridge.',
            'SDK completion and source alignment do not establish scientific correctness.']}
    try:
        if provider not in {'langextract', 'paperqa', 'paperqa2'}:
            raise _Unavailable('No registered SDK bridge for this provider')
        if not isinstance(settings, dict) or not isinstance(inputs, dict):
            raise ValueError('Host settings and tool inputs must be JSON objects')
        _text(effect_id, 'effect_id')
        output_limit = _limit(settings, 'max_output_bytes', 65536, 1048576)
        if provider == 'langextract':
            _langextract(settings, inputs, document_store, state)
        else:
            _paperqa(settings, inputs, document_store, state)
        encoded = json.dumps(state['data'], ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > output_limit:
            state['status'] = 'partial'
            state['data'] = {'omitted': 'output_capacity', 'result_sha256': hashlib.sha256(encoded).hexdigest(),
                             'bytes': len(encoded)}
            state['limitations'].append('SDK output exceeded the host output limit; no omitted evidence was delivered.')
    except (ImportError, _Unavailable) as exc:
        state['status'] = 'failed' if state['sdk_calls_observed'] else 'unavailable'
        state['execution_unknown'] = bool(state['sdk_calls_observed'])
        state['errors'] = [{'code': 'sdk_unavailable', 'message':
            'Optional SDK or its configuration is unavailable' if isinstance(exc, ImportError) else str(exc)}]
    except (ValueError, DocumentError) as exc:
        state['status'] = 'failed'
        state['execution_unknown'] = bool(state['sdk_calls_observed'])
        state['errors'] = [{'code': 'sdk_error' if state['sdk_calls_observed'] else 'invalid_input',
            'message': 'SDK failed after invocation; reconcile before retrying' if state['sdk_calls_observed'] else str(exc)}]
    except Exception:
        state['status'] = 'failed' if state['sdk_calls_observed'] else 'unavailable'
        state['execution_unknown'] = bool(state['sdk_calls_observed'])
        # Provider exceptions can contain raw authorization headers or secrets.
        state['errors'] = [{'code': 'sdk_error', 'message': 'SDK did not produce a certified result; no automatic retry'}]
    if state['cursors'] and state['status'] == 'ok':
        state['status'] = 'partial'
    return {'status': state['status'], 'data': state['data'],
        'provenance': {'provider': provider, 'effect_id': effect_id, 'sources': state['sources'],
            'sdk_package': state.get('sdk_package'),
            'cost_visibility': {'sdk_calls_observed': state['sdk_calls_observed'], 'internal_model_calls': None,
                'internal_embedding_calls': None, 'internal_http_calls': None, 'accounting': 'opaque_nested_sdk'}},
        'coverage': {'sources': state['sources'], 'original_fulltext_verified': False,
            'all_requested_ranges_complete': bool(state['sources']) and not state['cursors'],
            'limitations': state['limitations']},
        'continuation': {'document_cursors': state['cursors']} if state['cursors'] else None,
        'errors': state['errors'], **({'execution_unknown': True} if state.get('execution_unknown') else {})}
