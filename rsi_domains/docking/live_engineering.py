"""Metered, text-only engineering proposals for an admitted capability request.

The engineer returns a structured artifact proposal. This module neither writes
the proposed source as an executable nor runs or accepts it. A host review and
the independent capability acceptance runner remain necessary afterward.
"""
from __future__ import annotations

import json
from pathlib import Path

from rsi_lab import transport
from rsi_lab.controller import identity
from rsi_lab.roles import _validate
from rsi_lab.store import digest, encode

EFFECT_KIND = 'engineering_model'
SCHEMA_VERSION = 'engineering_text_request_v1'
MAX_PROMPT_BYTES = 512 * 1024


def _check_schema(schema):
    """Accept the closed JSON subset checked by the existing role validator."""
    if not isinstance(schema, dict):
        raise ValueError('Engineering schema must be a closed JSON schema object')
    supported = {'type', 'properties', 'required', 'additionalProperties', 'items',
                 'enum', 'anyOf', 'minimum', 'description', 'title'}
    if set(schema) - supported:
        raise ValueError('Engineering schema uses an unvalidated keyword')
    if 'anyOf' in schema:
        if set(schema) - {'anyOf', 'description', 'title'} or not schema['anyOf']:
            raise ValueError('Engineering union must be a nonempty closed anyOf')
        for child in schema['anyOf']:
            _check_schema(child)
        return
    types = schema.get('type')
    types = types if isinstance(types, list) else [types]
    if not types or any(t not in {'object', 'array', 'string', 'integer', 'boolean', 'null'} for t in types):
        raise ValueError('Unsupported engineering JSON type')
    if 'object' in types:
        if (not isinstance(schema.get('properties'), dict)
                or schema.get('additionalProperties') is not False
                or set(schema.get('required', [])) != set(schema['properties'])):
            raise ValueError('Engineering objects must require all and only their declared properties')
        for child in schema['properties'].values():
            _check_schema(child)
    if 'array' in types:
        _check_schema(schema.get('items'))


def _execute(store, calls_root, effect, packet_bytes):
    if effect['status'] != 'dispatching' or digest(packet_bytes) != effect['packet_ref']:
        raise ValueError('Engineering requires the exact persisted dispatched input')
    packet = json.loads(packet_bytes)
    if packet.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Unsupported engineering request')
    folder = calls_root / effect['id']
    result = transport.invoke(packet['prompt'], packet['response_schema'], folder,
        packet['model'], packet['effort'], timeout=packet['timeout_seconds'])
    # Audit transport independently, retaining every original artifact in the
    # immutable object archive rather than relying only on writable file paths.
    original_artifacts = {name: store.put_blob((folder / name).read_bytes())
                          for name in sorted(transport.ARTIFACTS) if (folder / name).is_file()}
    actual_prompt = (folder / 'prompt.txt').read_bytes()
    expected_prompt = (transport.PREAMBLE + packet['prompt']).encode()
    output, usage, complete, errors = transport._events((folder / 'events.jsonl').read_text())
    transport_certified = (actual_prompt == expected_prompt and result.get('error') is None
        and result.get('usage_complete') is True and complete and not errors
        and usage == result.get('usage') and result.get('returncode') == 0)
    costs = {'model_calls': 1, 'wall_seconds': result['wall_seconds']}
    measured = result.get('usage')
    if (isinstance(measured, dict)
            and all(type(measured.get(k)) is int for k in ('input_tokens', 'output_tokens'))):
        costs['tokens'] = measured['input_tokens'] + measured['output_tokens']
    schema_error = None
    if transport_certified:
        try:
            _validate(result['response'], packet['response_schema'])
            if json.loads(output) != result['response']:
                raise ValueError('Engineering response differs from the raw completed message')
        except (ValueError, KeyError, TypeError) as exc:
            schema_error = str(exc)
    status = 'unknown' if not transport_certified else 'failed_definite' if schema_error else 'succeeded'
    return {'status': status, 'actual_costs': costs,
        'response': result.get('response') if status == 'succeeded' else None,
        'usage': measured, 'usage_complete': transport_certified,
        'sent_packet_sha256': digest(packet_bytes), 'sent_prompt_sha256': digest(actual_prompt),
        'transport_artifacts': original_artifacts,
        'transport_directory': str(folder), 'schema_error': schema_error,
        'error': result.get('error') or schema_error or ('; '.join(errors) if errors else None),
        'authority': 'Text-only proposal; no source execution or capability acceptance',
        'unknown_usage_is_zero': False}


def build(runtime, build_work_id, engineering_prompt, response_schema):
    """Call one real engineer, or recover the same saved proposal without retry.

    The caller supplies a bounded evidence-grounded prompt and a closed output
    schema, commonly containing source code and implementation limitations. A
    new prompt/schema is a separate metered revision; an unknown previous call
    for this build must be reconciled before a revision can run. The original
    capability Work stays available for subsequent reviewed acceptance.
    """
    if (not isinstance(engineering_prompt, str) or not engineering_prompt.strip()
            or len(engineering_prompt.encode()) > MAX_PROMPT_BYTES):
        raise ValueError('Engineering prompt must be explicit and at most 512 KiB')
    _check_schema(response_schema)
    if response_schema.get('type') != 'object':
        raise ValueError('Engineering response needs a closed object root')
    store, run, controller = runtime.store, runtime.run_id, runtime.controller
    work = store.get(run, 'work', build_work_id)
    if work['kind'] != 'capability_build' or not work['inputs'].get('build_intent_id'):
        raise ValueError('Engineering requires an admitted capability-build Work')
    intent = store.get(run, 'registration', work['inputs']['build_intent_id'])
    if intent.get('kind') != 'capability_build_request' or intent.get('status') != 'requested':
        raise ValueError('Engineering requires a still-pending capability-build intent')
    controller.recover(run)
    packet = {'schema_version': SCHEMA_VERSION, 'build_work_id': build_work_id,
        'build_intent_id': intent['id'], 'build_intent': intent['proposal'],
        'prompt': engineering_prompt, 'response_schema': response_schema,
        'model': runtime.config.get('engineering_model', runtime.config.get('model', 'gpt-6-astra')),
        'effort': runtime.config.get('engineering_effort', runtime.config.get('effort', 'medium')),
        'timeout_seconds': runtime.config.get('engineering_timeout_seconds', 300)}
    if (type(packet['timeout_seconds']) not in (int, float)
            or not 1 <= packet['timeout_seconds'] <= 600):
        raise ValueError('Engineering timeout must be between 1 and 600 seconds')
    token = identity('engineering', build_work_id, digest(packet))
    effects = [effect for effect in store.list(run, 'effect', kind=EFFECT_KIND)
               if effect.get('metadata', {}).get('build_work_id') == build_work_id]
    matched = [effect for effect in effects if effect['metadata']['engineering_token'] == token]
    if matched:
        effect = matched[0]
    else:
        if any(effect['status'] in {'prepared', 'dispatching', 'unknown'} or effect.get('accounting_unknown')
               for effect in effects):
            raise ValueError('Prior engineering call requires reconciliation; no automatic revised attempt')
        producer = controller.enqueue(run, 'capability_build', token, token + ':work',
            inputs={'issue_id': intent['issue_id'], 'engineering_for_work_id': build_work_id})
        estimates = {'model_calls': 1, 'tokens': runtime.config.get('engineering_token_estimate', 60000),
                     'wall_seconds': packet['timeout_seconds']}
        effect = controller.prepare_effect(run, producer['id'], EFFECT_KIND, encode(packet), estimates,
            token + ':prepare', metadata={'engineering_token': token, 'build_work_id': build_work_id,
                'build_intent_id': intent['id'], 'issue_id': intent['issue_id'],
                'source_sha256': digest(Path(__file__).read_bytes())})
    if effect['status'] == 'prepared':
        def execute(admitted, raw):
            if admitted['metadata']['source_sha256'] != digest(Path(__file__).read_bytes()):
                return {'status': 'failed_definite', 'actual_costs': dict.fromkeys(admitted['reservations'], 0),
                        'error': 'Engineering executor changed before dispatch; no model call made'}
            return _execute(store, store.root / 'engineering_calls', admitted, raw)
        effect = controller.dispatch_effect(run, effect['id'], execute)
    receipt = json.loads(store.read_blob(effect['result_ref'])) if effect.get('result_ref') else None
    return {'status': effect['status'], 'effect': effect, 'receipt': receipt,
        'response': receipt.get('response') if receipt and effect['status'] == 'succeeded'
                    and not effect.get('accounting_unknown') else None,
        'review_required': True, 'source_executed': False, 'capability_accepted': False}
