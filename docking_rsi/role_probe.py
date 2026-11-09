"""Bounded historical role recall from explicit, read-only original sources.

This runs at most one raw-first discovery call and one disposition call. It
does not provide a domain gap checklist or an Owner interpretation, execute
docking, change a protocol, or contact the current user. A proposed question
is saved for inspection. One case is an invocation smoke test, not an estimate
of diagnostic quality, discovery rate, independence, or screening benefit.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from lab_harness import input_view, transport
from .controller import Controller
from .roles import CodexExecutor
from .runtime import Runtime
from .store import Store, digest, encode

RUN_ID = 'role-probe'
DEFAULT_GOAL = 'Screen the supplied compounds against the receptor using AutoDock Vina.'
MAX_SOURCE_BYTES = 2 * 1024 * 1024
LOCAL_SOURCE_FIELDS = frozenset({'original_text', 'original_source_sha256', 'source_sha256',
                                  'provenance', 'source_ref', 'source_id', 'source_version', 'path'})


def _sources(source_paths):
    if (not isinstance(source_paths, dict) or not source_paths or len(source_paths) > 16
            or any(not isinstance(key, str) or not key.strip() or len(key) > 128 for key in source_paths)):
        raise ValueError('Supply one to sixteen explicitly named source file paths')
    result = []
    for source_id, value in sorted(source_paths.items()):
        path = Path(value).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError('Probe sources must be bounded regular files')
        raw = path.read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError('Probe source exceeds its input capacity')
        text = raw.decode('utf-8')
        result.append({'source_id': source_id, 'source_version': 1, 'path': str(path),
                       'sha256': digest(raw), 'bytes': len(raw), 'raw': raw, 'text': text})
    return result


def _code_snapshot(store):
    """Freeze exact source bytes, never only filenames or the current checkout."""
    paths = [('docking_rsi/' + path.name, path) for path in sorted(Path(__file__).parent.glob('*.py'))]
    paths.extend([('lab_harness/input_view.py', Path(input_view.__file__)),
                  ('lab_harness/transport.py', Path(transport.__file__))])
    root=Path(__file__).resolve().parent.parent
    for package in ('rsi_lab','rsi_domains/docking'):
        paths.extend((str(path.relative_to(root)),path) for path in sorted((root/package).glob('*.py')))
    result = []
    for name, path in paths:
        raw = path.read_bytes()
        result.append({'name': name, 'sha256': digest(raw), 'bytes': len(raw), 'source_ref': store.put_blob(raw)})
    return result


def _source_contract(source):
    return {'kind': 'single_local_file', 'source_version': source['source_version'],
        'object_identities': sorted({source['source_id'], 'source:' + source['source_id'], Path(source['path']).name,
                                    source['path'], source['sha256'], 'sha256:' + source['sha256']}),
        'supported_fields': sorted(LOCAL_SOURCE_FIELDS), 'follows_references': False,
        'scope': 'Exactly this frozen file; referenced files and other revisions require separate explicitly authorized sources'}


def _deliver_local_request(runtime, case):
    """Deliver one exact frozen file only when every requested identity matches.

    A source identifier is an authorized lookup location, not proof that this
    file is the requested artifact. Foreign objects, content hashes or fields
    remain pending with a persistent unavailable receipt; they do not wake the
    issue or turn an unrelated original into a fulfilled evidence request.
    """
    for request in runtime.store.list(RUN_ID, 'request', status='created'):
        source = next((source for source in case['sources'] if source['source_id'] == request['source']), None)
        def unavailable(code, reason, **details):
            note = {'schema_version': 'local_source_unavailable_v2', 'request_id': request['id'],
                'request_version': request['version'], 'source_id': request['source'], 'source_version': request['source_version'],
                'status': 'pending', 'reason_code': code, 'reason': reason, 'delivered': False,
                'request_scope_verified': False, 'query_answer_verified': False, **details}
            note_ref = runtime.store.put_blob(note)
            runtime.store.mutate(RUN_ID, 'probe-source-unavailable:' + note_ref, 'probe_local_source_unavailable',
                lambda tx: tx.insert('registration', 'source-unavailable:' + note_ref,
                    {'kind': 'local_source_unavailable', 'request_id': request['id'], 'status': 'unavailable', 'receipt_ref': note_ref}),
                payload={'receipt_ref': note_ref})
            return {**note, 'unavailability_ref': note_ref}
        if source is None or request['source_version'] != source['source_version']:
            return unavailable('source_version_unavailable', 'Requested source/version is not in the frozen authorized case')
        spec = request['query_spec']
        aliases = set(_source_contract(source)['object_identities'])
        unknown_objects = [value for value in spec['object_ids'] if value not in aliases]
        if not spec['object_ids'] or unknown_objects:
            return unavailable('requested_objects_unavailable',
                'Requested artifacts do not all identify the single frozen source file; no substitute original was delivered',
                requested_object_ids=spec['object_ids'], unavailable_object_ids=unknown_objects,
                available_object_aliases=sorted(aliases))
        unsupported_fields = sorted(set(spec['fields']) - LOCAL_SOURCE_FIELDS)
        if not spec['fields'] or unsupported_fields:
            return unavailable('requested_fields_unsupported',
                'The local source adapter cannot provide the requested fields or perform a semantic investigation',
                requested_fields=spec['fields'], unsupported_fields=unsupported_fields,
                supported_fields=sorted(LOCAL_SOURCE_FIELDS))
        # An exact requested revision can appear in the prose query even when
        # object_ids only names a file. Refuse a different hash instead of
        # treating a same-named current file as the requested historical one.
        hashes = {value.lower() for value in re.findall(r'(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])', spec['query'])}
        unmatched_hashes = sorted(hashes - {source['sha256']})
        if unmatched_hashes:
            return unavailable('requested_artifact_hash_unavailable',
                'The request references artifact hashes outside this frozen file; the adapter cannot certify those revisions',
                unavailable_sha256=unmatched_hashes, available_sha256=source['sha256'])
        raw = runtime.store.read_blob(source['source_ref'])
        provenance = {'source_id': source['source_id'], 'source_path': source['path'],
            'source_version': source['source_version'], 'original_source_sha256': source['sha256'],
            'original_source_ref': source['source_ref'], 'retrieval': 'read-only frozen local source',
            'query_answer_verified': False, 'request_scope_verified': True}
        content = {'source_id': source['source_id'], 'source_version': source['source_version'],
                   'source_sha256': source['sha256'], 'original_source_sha256': source['sha256'],
                   'source_ref': source['source_ref'], 'path': source['path'], 'provenance': provenance,
                   'original_text': raw.decode('utf-8'), 'complete_source': True,
                   'query_answer_verified': False, 'request_scope_verified': True,
                   'requested_fields_delivered': spec['fields'], 'matched_object_ids': spec['object_ids']}
        if len(encode(content)) > spec['max_bytes']:
            return unavailable('source_exceeds_request_bytes',
                'Complete original source exceeds the proposed request bound; no summary substituted')
        delivery = runtime.ingest_delivery(request['id'], content, provenance,
            source_version=source['source_version'])
        return {'status': 'delivered', 'request_id': request['id'], 'delivery_id': delivery['id'],
                'source_sha256': source['sha256'], 'query_answer_verified': False, 'request_scope_verified': True}
    return {'status': 'not_requested'}


def _report(store, runtime, case, case_ref, retrieval):
    status = runtime.status()
    calls = []
    for effect in store.list(RUN_ID, 'effect', kind='meta'):
        packet = json.loads(store.read_blob(effect['metadata']['plan_ref']))
        result_ref = effect.get('result_ref') or effect.get('receipt_ref')
        receipt = json.loads(store.read_blob(result_ref)) if result_ref else {}
        proposals = store.list(RUN_ID, 'proposal', effect_id=effect['id'])
        proposal = proposals[0] if proposals else None
        calls.append({'effect_id': effect['id'], 'mode': effect['metadata']['mode'], 'status': effect['status'],
            'accounting_unknown': effect.get('accounting_unknown', False), 'packet_ref': effect['packet_ref'],
            'packet_plan_ref': effect['metadata']['plan_ref'], 'receipt_ref': result_ref,
            'response': receipt.get('response'), 'proposal_status': proposal['status'] if proposal else None,
            'proposal_error': proposal.get('error') if proposal else None,
            'actual_costs': effect.get('actual_costs', {}), 'held_reservations': effect.get('held_reservations', {}),
            'synthetic_receipt': receipt.get('synthetic', False),
            'delivered_pages': [{key: page[key] for key in ('evidence_id', 'source_version', 'content_sha256', 'complete_source')}
                                for page in packet['delivery_pages']],
            'partial_evidence_ids': packet['partial_evidence_ids'], 'deferred_evidence_ids': packet['deferred_evidence_ids']})
    blocked_packets = [work for work in store.list(RUN_ID, 'work', status='blocked') if work['kind'].startswith('meta_')]
    if status['effects_requiring_reconciliation']:
        outcome = 'blocked_external'
    elif blocked_packets:
        outcome = 'packet_unrepresentable'
    elif any(call['proposal_status'] in {'rejected', 'blocked_stale', 'blocked_action'} for call in calls):
        outcome = 'decision_not_applied'
    elif status['questions']:
        outcome = 'pending_historical_question'
    elif status['issues']:
        outcome = 'bounded_probe_finished_with_issue'
    else:
        outcome = 'bounded_probe_finished_without_issue_proposal'
    current = _code_snapshot(store)
    frozen_hashes = {row['name']: row['sha256'] for row in case['code_snapshot']}
    current_hashes = {row['name']: row['sha256'] for row in current}
    return {'schema_version': 'docking_role_probe_report_v2', 'run_id': RUN_ID, 'execution_mode': 'role_recall',
        'status': outcome, 'case_spec_ref': case_ref, 'case_id': digest(case), 'model': case['model'],
        'goal': case['goal'], 'executor_kind': case['executor_kind'], 'max_meta_calls': 2,
        'actual_meta_effect_count': len(calls), 'calls': calls,
        'issues': status['issues'], 'next_actions': [{'work_id': row['id'], 'kind': row['kind'], 'inputs': row['inputs']}
                                                  for row in status['ready']],
        'pending_historical_questions': status['questions'], 'question_sent_to_current_user': False,
        'local_source_delivery': retrieval, 'docking_executed': False,
        'costs': {row['id']: {'spent': row['spent'], 'reserved': row['reserved'], 'limit': row['limit'],
                            'hard': row['hard'], 'overrun': row['overrun']} for row in status['budgets']},
        'sources': case['sources'], 'code_snapshot': case['code_snapshot'],
        'current_code_matches_frozen': current_hashes == frozen_hashes,
        'reproduction': {'entrypoint': 'docking_rsi.role_probe.run_probe',
            'case_spec_ref': case_ref, 'original_sources': 'Store.read_blob(source_ref)',
            'runtime_sources': 'Store.read_blob(code_snapshot[*].source_ref)',
            'exact_sent_inputs': 'calls[*].packet_ref', 'exact_results': 'calls[*].receipt_ref',
            'repeat_behavior': 'Existing root is read/reconciled only; a fresh root is required for another model experiment'},
        'history_verified': True,
        'interpretation': 'One bounded invocation smoke test; no diagnostic accuracy, discovery-rate, independence, scientific-validity or screening-benefit claim'}


def run_probe(root, source_paths: dict, model='gpt-6-astra', goal=DEFAULT_GOAL, executor_factory=None, *, timeout_seconds=300):
    """Execute a new two-call probe, or inspect/reconcile an existing probe.

    Tests may inject ``executor_factory(mode, calls_root) -> executor``. A
    supplied factory is explicitly labeled as injected, never a real-model
    finding. Unknown executions are not reissued. Only original local source
    snapshots are delivered; new user questions remain saved historical work.
    """
    if not isinstance(goal, str) or not goal.strip() or not isinstance(model, str) or not model.strip():
        raise ValueError('Probe goal and model must be explicit nonempty strings')
    if type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 600:
        raise ValueError('Probe call timeout must be between 1 and 600 seconds')
    sources = _sources(source_paths)
    store = Store(Path(root).resolve())
    try:
        try:
            config = store.run(RUN_ID)['config']
        except KeyError:
            config = None
        if config is not None:
            case_ref = config['case_spec_ref']
            case = json.loads(store.read_blob(case_ref))
            supplied = [(row['source_id'], row['path'], row['sha256']) for row in sources]
            frozen = [(row['source_id'], row['path'], row['sha256']) for row in case['sources']]
            if supplied != frozen or model != case['model'] or goal != case['goal']:
                raise ValueError('Existing probe inputs differ; create a fresh root for another prospective case')
            runtime = Runtime(store, RUN_ID)
            runtime.recover()
            prior = store.list(RUN_ID, 'registration', id='probe_report')
            retrieval = (json.loads(store.read_blob(prior[0]['report_ref']))['local_source_delivery']
                         if prior else {'status': 'not_replayed'})
        else:
            snapshot = _code_snapshot(store)
            frozen_sources = [{key: row[key] for key in ('source_id', 'source_version', 'path', 'sha256', 'bytes')}
                              | {'source_ref': store.put_blob(row['raw'])} for row in sources]
            budgets = {'model_calls': {'limit': 2, 'hard': True},
                'tokens': {'limit': 120000, 'hard': False},
                'wall_seconds': {'limit': 2 * timeout_seconds + 30, 'hard': False},
                'tool_calls': {'limit': 0, 'hard': True}, 'cpu_seconds': {'limit': 0, 'hard': False}}
            case = {'schema_version': 'docking_role_probe_case_v2', 'execution_mode': 'role_recall',
                'model': model, 'goal': goal, 'sources': frozen_sources, 'code_snapshot': snapshot,
                'authorized_source_contracts': {source['source_id']: _source_contract(source) for source in frozen_sources},
                'executor_kind': 'codex' if executor_factory is None else 'injected_test_executor',
                'max_meta_calls': 2, 'call_plan': ['raw_first_discovery', 'one_disposition_if_issue_exists'],
                'timeout_seconds': timeout_seconds, 'budgets': budgets,
                'constraints': {'external_source_queries': False, 'local_source_deliveries': 1,
                                'contact_current_user': False, 'docking': False, 'method_changes': False},
                'assessment': 'Invocation smoke test only; no quality score or inferred benefit'}
            case_ref = store.put_blob(case)
            controller = Controller(store)
            controller.initialize(RUN_ID, budgets=budgets, max_external=1, config={
                'execution_mode': 'role_recall', 'case_spec_ref': case_ref, 'model': model, 'effort': 'medium',
                'goal': goal, 'authorized_sources': [row['source_id'] for row in sources],
                'authorized_source_contracts': case['authorized_source_contracts'],
                'max_input_bytes': 262144, 'open_review_interval_batches': 1,
                'max_open_reviews': 1, 'open_review_fact_limit': 16, 'wait_timeout_seconds': 86400})
            store.mutate(RUN_ID, 'freeze-probe-case', 'role_probe_case_frozen',
                lambda tx: tx.insert('registration', 'probe_case', {'status': 'frozen', 'spec_ref': case_ref, 'spec_sha256': digest(case)}),
                payload={'case_ref': case_ref})
            facts = []
            for source in frozen_sources:
                facts.append(controller.add_fact(RUN_ID, 'source:' + source['source_id'], 'raw_fact',
                    {'source_id': source['source_id'], 'original_text': store.read_blob(source['source_ref']).decode('utf-8'),
                     'original_source_sha256': source['sha256']},
                    {'source_id': source['source_id'], 'source_version': source['source_version'],
                     'path': source['path'], 'source_ref': source['source_ref'], 'source_sha256': source['sha256']},
                    'source-fact:' + source['source_id'])['id'])
            runtime = Runtime(store, RUN_ID)
            discovery = runtime.observe('historical-probe-start', 'batch_boundary', 1, facts)
            factory = executor_factory or (lambda mode, calls_root: CodexExecutor(calls_root, timeout_seconds=timeout_seconds))
            reservations = {'model_calls': 1, 'tokens': 60000, 'wall_seconds': timeout_seconds}
            first = runtime.run_meta(discovery['work_id'], factory('discovery', store.root / 'model_calls'), reservations)
            if (first.get('effect', {}).get('status') == 'succeeded' and first.get('proposal', {}).get('status') == 'applied'
                    and first['proposal']['command']['action'] == 'propose_issue'):
                work = next((row for row in controller.ready(RUN_ID) if row['kind'] == 'meta_disposition'), None)
                if work is not None:
                    runtime.run_meta(work['id'], factory('disposition', store.root / 'model_calls'), reservations)
            retrieval = _deliver_local_request(runtime, case)
        report = _report(store, runtime, case, case_ref, retrieval)
        key = 'probe-report:' + digest(report)
        def save(tx):
            previous = tx.find('registration', id='probe_report')
            data = {'status': report['status'], 'report_ref': tx.blob(report)}
            return tx.update('registration', 'probe_report', data) if previous else tx.insert('registration', 'probe_report', data)
        store.mutate(RUN_ID, key, 'role_probe_report_saved', save, payload={'report': report})
        (store.root / 'role_probe_report.json').write_bytes(encode(report))
        store.verify_history(RUN_ID)
        return report
    finally:
        store.close()
