"""Offline orchestration acceptance with synthetic role decisions.

The identity metadata adapter really runs in subprocesses. No chemistry,
docking, live model judgment, blind gap detection or screening benefit is
established by this demo. The fixed role responses exercise the controller's
discovery -> investigation -> executable acceptance -> resumption path.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from lab_harness import input_view
from .capabilities import digest, encoded
from .controller import Controller
from .runtime import Runtime
from .store import Store


IDENTITY_ADAPTER = '''"""Synthetic metadata fixture, not a chemical identity validator."""
import hashlib
import json
import sys

packet = json.load(sys.stdin)
value = packet["input"]
source_hash = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
valid = (isinstance(value.get("compound_id"), str) and bool(value["compound_id"])
         and value.get("prepared_parent_id") == value["compound_id"])
result = {"schema_version": "capability_response_v2", "ok": valid,
          "output": {"consistent": True, "compound_id": value["compound_id"]} if valid else None,
          "identity": value["compound_id"] if valid else None,
          "source_receipts": [{"kind": "input", "sha256": source_hash}],
          "error": None if valid else {"code": "parent_identity_mismatch", "message": "Parent metadata does not match the compound identity"}}
json.dump(result, sys.stdout)
'''


def capability_fixture():
    """A frozen engineering acceptance specification with independent bad input."""
    return {'schema_version': 'capability_request_v2', 'capability_id': 'synthetic_parent_metadata_audit',
        'purpose': 'Check exact parent identifier metadata on synthetic prepared records',
        'scope': {'domain': 'synthetic_identity_metadata', 'identity_rule': 'exact_string_equality'},
        'input_contract': {'compound_id': 'nonempty_string', 'prepared_parent_id': 'string'},
        'output_contract': {'consistent': 'true_on_success', 'compound_id': 'unchanged_input_identity'},
        'scientific_impact': False, 'baseline_scientific_protocol': 'synthetic-protocol-v1',
        'candidate_scientific_protocol': 'synthetic-protocol-v1', 'new_study_id': None,
        'execution_profile': 'reviewed_local_python_json_v1',
        'recovery': {'query_supported': False, 'idempotency_supported': False, 'unknown_result': 'blocked_external'},
        'limits': {'wall_seconds_per_case': 3, 'cpu_seconds_per_case': 1, 'output_bytes_per_case': 8192},
        'cases': [
            {'case_id': 'matching-parent', 'kind': 'normal',
             'input': {'compound_id': 'demo-compound-001', 'prepared_parent_id': 'demo-compound-001'},
             'expected_output': {'consistent': True, 'compound_id': 'demo-compound-001'},
             'expected_identity': 'demo-compound-001', 'expected_error_code': None},
            {'case_id': 'mismatched-parent', 'kind': 'failure',
             'input': {'compound_id': 'demo-compound-001', 'prepared_parent_id': 'another-compound'},
             'expected_output': None, 'expected_identity': None, 'expected_error_code': 'parent_identity_mismatch'}]}


def fixture_delivery(directory):
    """Materialize the exact reviewed bundled fixture; no generated source execution."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    adapter = directory / 'synthetic_parent_metadata_audit.py'
    adapter.write_text(IDENTITY_ADAPTER)
    artifact = digest(IDENTITY_ADAPTER.encode())
    spec = capability_fixture()
    manifest = {'schema_version': 'capability_delivery_v2', 'capability_id': spec['capability_id'],
        'capability_version': 'synthetic-fixture-v1', 'artifact_sha256': artifact,
        'scope': spec['scope'], 'input_contract': spec['input_contract'], 'output_contract': spec['output_contract'],
        'packages': {}, 'engineering_provenance': {'kind': 'bundled_reviewed_synthetic_fixture',
            'source_module': 'docking_rsi.demo.IDENTITY_ADAPTER', 'reviewed_source_sha256': artifact,
            'limitation': 'Exact metadata string comparison; no molecular or chemical-state validation'}}
    return spec, adapter, manifest, artifact


class SyntheticMetaExecutor:
    """A fixed response fixture; never calls or imitates a live language model."""
    def __init__(self, action, payload):
        self.action, self.payload = action, payload

    def __call__(self, effect, packet_bytes):
        started = time.monotonic()
        envelope = json.loads(packet_bytes)
        if effect.get('status') != 'dispatching' or digest(packet_bytes) != effect.get('packet_ref'):
            raise ValueError('Synthetic response fixture still requires a reserved exact dispatched packet')
        view = envelope['prompt'].split('INPUT_DATA_JSON:\n', 1)[1]
        data = input_view.decode(view, envelope['input_view_mode'])
        task = data['task']
        payload = self.payload(data) if callable(self.payload) else self.payload
        response = {'command': {'mode': envelope['mode'], 'action': self.action,
            'issue_id': task.get('issue_id'), 'expected_versions': task['expected_versions'],
            'evidence_ids': list(dict.fromkeys(page['evidence_id'] for page in data['delivered_pages'])),
            'payload': payload}}
        return {'status': 'succeeded', 'terminal_received': True, 'protocol_valid': True,
            'actual_costs': {'model_calls': 0, 'tokens': 0,
                'wall_seconds': time.monotonic() - started},
            'response': response, 'sent_packet_sha256': digest(packet_bytes),
            'sent_prompt_sha256': digest(envelope['prompt'].encode()), 'usage_complete': True,
            'usage': {'input_tokens': 0, 'output_tokens': 0},
            'synthetic': True, 'source': 'fixed_orchestration_fixture',
            'limitation': 'No model executed; decision quality and blind discovery are not evaluated'}


def run_demo(directory, run_id='offline-demo'):
    """Run the complete offline mechanism check, preserving all real receipts.

    Completed repetitions return the stored report. An interrupted demonstration
    is left for explicit runtime recovery/status inspection instead of replaying
    its scripted steps or retrying an unknown external Effect.
    """
    root = Path(directory).resolve()
    store = Store(root)
    try:
        try:
            store.run(run_id)
        except KeyError:
            pass
        else:
            store.verify_history(run_id)
            try:
                complete = store.get(run_id, 'registration', 'offline_demo_report')
            except KeyError as exc:
                raise ValueError('This demo run already exists and is incomplete; recover and inspect it before further dispatch') from exc
            return json.loads(store.read_blob(complete['report_ref']))
        controller = Controller(store)
        controller.initialize(run_id, max_external=1, config={
            'execution_mode': 'replay', 'synthetic_decisions': True,
            'goal': 'Audit a synthetic docking handoff metadata fixture and resume its local workflow',
            'authorized_sources': ['fixture_source'], 'open_review_interval_batches': 1, 'max_open_reviews': 1,
            'max_input_bytes': 131072, 'wait_timeout_seconds': 600}, budgets={
                'model_calls': {'limit': 10, 'hard': True, 'settlement_reserve': 1},
                'tool_calls': {'limit': 6, 'hard': True, 'settlement_reserve': 1},
                'tokens': {'limit': 20000, 'hard': False, 'settlement_reserve': 100},
                'cpu_seconds': {'limit': 10, 'hard': False, 'settlement_reserve': 1},
                'wall_seconds': {'limit': 60, 'hard': False, 'settlement_reserve': 2}})
        runtime = Runtime(store, run_id)
        fact = controller.add_fact(run_id, 'synthetic_handoff', 'raw_fact',
            {'fixture': True, 'compound_id': 'demo-compound-001',
             'prepared_parent_id': 'demo-compound-001', 'identity_audit_receipt': None},
            {'source': 'bundled_synthetic_handoff', 'chemical_validation': False}, 'demo-initial-fact')
        discovery = runtime.observe('synthetic-batch-boundary', 'batch_boundary', 1, [fact['id']])
        decisions = []

        def ready(kind, issue_id=None):
            rows = [row for row in controller.ready(run_id) if row['kind'] == kind
                    and (issue_id is None or row['issue_id'] == issue_id)]
            if len(rows) != 1:
                raise ValueError('Demo expected exactly one eligible ' + kind + ' Work')
            return rows[0]

        def decision(work_id, action, payload):
            result = runtime.run_meta(work_id, SyntheticMetaExecutor(action, payload),
                {'model_calls': 0, 'tokens': 0, 'wall_seconds': 1})
            proposal = result.get('proposal')
            if not proposal or proposal['status'] != 'applied':
                raise ValueError('Synthetic demo proposal did not apply: ' + json.dumps(proposal))
            decisions.append({'action': action, 'effect_id': result['effect']['id'],
                              'proposal_id': proposal['id'], 'synthetic': True})
            return json.loads(store.read_blob(proposal['result_ref']))

        issue = decision(discovery['work_id'], 'propose_issue', {
            'title': 'Synthetic parent metadata has no audit receipt',
            'problem_kind': 'omitted_prerequisite', 'scope': 'one bundled synthetic handoff',
            'hypothesis': 'No executable metadata audit has been evidenced for this fixture',
            'competing_explanations': ['A valid audit exists but its receipt was omitted'],
            'discriminating_check': 'Retrieve fixture source metadata and registered audit evidence',
            'falsifying_observation': 'A matching verified audit receipt already exists'})
        iid = issue['id']
        requested = decision(ready('meta_disposition', iid)['id'], 'request_evidence', {
            'source_id': 'fixture_source', 'source_version': 1, 'object_ids': ['demo-compound-001'],
            'fields': ['parent_identity_metadata', 'audit_capability'],
            'query': 'Retrieve the exact bundled fixture metadata and audit capability receipt',
            'max_pages': 1, 'max_bytes': 4096})
        waiting_before_delivery = store.get(run_id, 'wait', requested['wait_id'])['status']
        runtime.ingest_delivery(requested['request_id'],
            {'fixture': True, 'compound_id': 'demo-compound-001', 'prepared_parent_id': 'demo-compound-001',
             'audit_capability': None, 'existing_audit_receipt': None},
            {'source_id': 'fixture_source', 'source_version': 1, 'fixture': True})
        waiting_after_delivery = store.get(run_id, 'wait', requested['wait_id'])['status']
        build = decision(ready('meta_disposition', iid)['id'], 'request_capability', {
            'capability_id': 'synthetic_parent_metadata_audit',
            'purpose': 'Produce executable exact-string identity metadata evidence for the synthetic fixture',
            'input_contract': 'compound_id and prepared_parent_id strings',
            'output_contract': 'unchanged compound identity, consistency result, and input source hash',
            'applicability': 'Bundled synthetic identifier metadata; no chemical identity interpretation',
            'acceptance_checks': ['matching parent succeeds', 'mismatched parent fails explicitly',
                                  'identity preserved', 'source receipt verified', 'observed resource usage'],
            'affects_scientific_results': False})
        spec, path, manifest, artifact = fixture_delivery(root / 'engineering_fixture')
        acceptance = runtime.accept_capability(build['work_id'], spec, path, manifest, artifact)
        capability = acceptance.get('capability')
        if not capability or capability['status'] != 'verified':
            raise ValueError('The real synthetic adapter did not pass its frozen acceptance cases')
        receipt = json.loads(store.read_blob(acceptance['effect']['result_ref']))
        decision(ready('meta_disposition', iid)['id'], 'close_issue', {
            'resolution': 'resolved', 'reason': 'The exact bundled metadata fixture has actual executable normal/failure, identity, provenance and cost receipts; no chemistry conclusion follows'})
        # A genuine subsequent local action demonstrates resumed execution. It
        # checks the preserved acceptance artifact and records a handoff marker;
        # it does not manufacture a Vina result or formal compound list.
        work = controller.enqueue(run_id, 'lab', 'after-verified-fixture', 'demo-resume-lab',
                                  inputs={'capability_registration': capability['id'], 'synthetic': True})
        effect = controller.prepare_effect(run_id, work['id'], 'synthetic_local_handoff',
            {'capability_id': capability['id'], 'artifact_sha256': artifact},
            {'tool_calls': 1, 'wall_seconds': 1}, 'demo-prepare-resume')
        def resume(effect, packet_bytes):
            started = time.monotonic()
            pinned = json.loads(packet_bytes)
            actual = store.read_blob(capability['artifact_ref'])
            if digest(actual) != pinned['artifact_sha256']:
                raise ValueError('Verified executable artifact changed before resumed handoff')
            marker = {'schema_version': 'synthetic_resumption_v2', 'capability_registration': capability['id'],
                      'artifact_sha256': digest(actual), 'fixture_only': True, 'docking_executed': False}
            marker_path = root / 'synthetic_resumed.json'
            marker_path.write_bytes(encoded(marker))
            return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1, 'wall_seconds': time.monotonic() - started},
                    'resumption': marker, 'artifact_sha256': digest(marker_path.read_bytes())}
        resumed = controller.dispatch_effect(run_id, effect['id'], resume)
        status = runtime.status()
        report = {'schema_version': 'docking_rsi_offline_demo_v2', 'run_id': run_id,
            'status': 'completed' if resumed['status'] == 'succeeded' else 'incomplete',
            'scope': 'Offline orchestration integration with scripted synthetic Meta decisions and actual local adapter processes',
            'not_evaluated': ['live model judgment quality', 'blind discovery of chemical prerequisites',
                              'docking or scientific validity', 'screening quality or cost improvement'],
            'decisions': decisions, 'issue_id': iid, 'issue_status': store.get(run_id, 'issue', iid)['status'],
            'request_wait': {'before_delivery': waiting_before_delivery, 'after_delivery': waiting_after_delivery},
            'capability_wait': store.get(run_id, 'wait', build['wait_id'])['status'],
            'capability_registration': capability['id'], 'capability_effect_id': acceptance['effect']['id'],
            'actual_acceptance': {'accepted': receipt['capability_acceptance']['accepted'],
                'cases': [{'case_id': case['case_id'], 'kind': case['kind'], 'passed': case['passed'],
                           'checks': case['checks'], 'input_sha256': case['input_sha256'],
                           'output_sha256': case['output_sha256']} for case in receipt['capability_acceptance']['cases']],
                'costs': receipt['actual_costs'], 'isolation': receipt['capability_acceptance']['isolation']},
            'resumed_effect_id': resumed['id'], 'docking_executed': False,
            'unreconciled_effect_count': len(status['effects_requiring_reconciliation']),
            'costs': {row['id']: {'spent': row['spent'], 'reserved': row['reserved'], 'hard': row['hard'],
                                'overrun': row['overrun']} for row in status['budgets']},
            'history': status['history']}
        store.mutate(run_id, 'demo-report', 'offline_demo_completed',
            lambda tx: tx.insert('registration', 'offline_demo_report', {'status': 'completed', 'report_ref': tx.blob(report)}),
            payload={'report': report})
        (root / 'demo_report.json').write_bytes(encoded(report))
        store.verify_history(run_id)
        return report
    finally:
        store.close()
