"""Shared fixtures for driver liveness tests: scripted roles and probe adapters.

Adapter callbacks are module-level functions because the registry pins their
source and rejects closures. Nothing here calls a model or the network.
"""
from __future__ import annotations

import json
import shutil
import tempfile

from rsi_lab import input_view
from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest

BUDGETS = {name: {'limit': limit, 'hard': name.endswith('calls')} for name, limit in
           {'model_calls': 10000, 'tool_calls': 10000, 'tokens': 10 ** 9,
            'cpu_seconds': 10 ** 6, 'wall_seconds': 10 ** 6}.items()}
RES = {'model_calls': 0, 'tokens': 0, 'wall_seconds': 1}
ISSUE = {'title': 'Units are not evidenced', 'problem_kind': 'missing_information', 'scope': 'dataset-1',
         'hypothesis': 'Magnitudes may mix units', 'competing_explanations': ['All rows already share one unit'],
         'discriminating_check': 'Fetch the unit map', 'falsifying_observation': 'A receipt shows one unit'}
EXECUTIONS = []


def open_run(testcase, *, config=None, budgets=None, adapters=(), run_id='r'):
    root = tempfile.mkdtemp(prefix='rsi-liveness-')
    testcase.addCleanup(shutil.rmtree, root, ignore_errors=True)
    store = Store(root)
    testcase.addCleanup(store.close)
    controller = Controller(store)
    base = {'execution_mode': 'replay', 'goal': 'Liveness fixture', 'authorized_sources': ['src'],
            'open_review_interval_batches': 1, 'max_open_reviews': 4, 'wait_timeout_seconds': 600}
    base.update(config or {})
    controller.initialize(run_id, max_external=1, budgets=budgets or BUDGETS, config=base)
    return store, controller, Runtime(store, run_id, adapters=list(adapters))


class Scripted:
    """Fixed role responses keyed by mode; records every call and its decoded input."""

    def __init__(self, by_mode, *, receipt=None):
        self.by_mode, self.receipt = by_mode, receipt
        self.calls, self.seen = [], []

    def __call__(self, effect, raw):
        envelope = json.loads(raw)
        data = input_view.decode(envelope['prompt'].split('INPUT_DATA_JSON:\n', 1)[1], envelope['input_view_mode'])
        mode = envelope['mode']
        index = len(self.calls)
        self.calls.append(mode)
        self.seen.append(data)
        action, payload = self.by_mode[mode](data)
        receipt = {'status': 'succeeded', 'terminal_received': True, 'protocol_valid': True,
                   'actual_costs': {'model_calls': 0, 'tokens': 0, 'wall_seconds': 0.0},
                   'sent_packet_sha256': digest(raw), 'sent_prompt_sha256': digest(envelope['prompt'].encode()),
                   'synthetic': True, 'usage': {'input_tokens': 0, 'output_tokens': 0}, 'usage_complete': True,
                   'response': {'command': {'mode': mode, 'action': action, 'issue_id': data['task'].get('issue_id'),
                                            'expected_versions': data['task']['expected_versions'],
                                            'evidence_ids': list(dict.fromkeys(
                                                p['evidence_id'] for p in data['delivered_pages'])),
                                            'payload': payload}}}
        return self.receipt(receipt, index) if self.receipt else receipt


def by_seq(rows):
    return sorted(rows, key=lambda row: row['created_seq'])


def none(data):
    return 'no_action', {'reason': 'n/a'}


def diag(inputs, capability_id='probe'):
    return {'capability_id': capability_id, 'capability_version': '1', 'inputs_json': json.dumps(inputs)}


def strict_prepare(inputs):
    if not isinstance(inputs, dict) or set(inputs) != {'record_id'}:
        raise ValueError('Probe adapter requires exactly record_id')
    return {'record_id': inputs['record_id']}


def lenient_prepare(inputs):
    return dict(inputs)


def one_tool_call(packet):
    return {'tool_calls': 1}


def record_execution(effect, packet_bytes):
    EXECUTIONS.append(effect['id'])
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1}, 'result': json.loads(packet_bytes)}


def die_after_execution(effect, packet_bytes):
    EXECUTIONS.append(effect['id'])
    raise KeyboardInterrupt('simulated process death during tool execution')


def one_fact(effect, receipt):
    return {'facts': [{'id': 'probe_' + effect['id'][-16:], 'kind': 'raw_fact', 'content': receipt['result'],
                       'provenance': {'effect_id': effect['id']}}], 'conclusions': [], 'data': receipt['result']}


def broken_publication(effect, receipt):
    raise TypeError('broken publication')


def _probe(adapter_id, effect_kind, prepare, execute, publication):
    return Adapter(adapter_id=adapter_id, version='1', effect_kind=effect_kind, manifest={'scope': 'probe'},
                   prepare=prepare, reservations=one_tool_call, execute=execute, publication=publication)


def validating_adapter():
    return _probe('probe', 'probe_tool', strict_prepare, record_execution, one_fact)


def lenient_adapter():
    return _probe('probe', 'probe_tool', lenient_prepare, record_execution, one_fact)


def broken_publication_adapter():
    return _probe('probe_bad', 'probe_bad_tool', strict_prepare, record_execution, broken_publication)


def dying_adapter():
    return _probe('probe_dying', 'probe_dying_tool', strict_prepare, die_after_execution, one_fact)
