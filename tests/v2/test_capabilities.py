"""Actual harmless adapter subprocesses; these are engineering, not chemistry tests."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from docking_rsi.capabilities import CapabilityRegistry, digest, encoded, execute_acceptance, validate_request
from docking_rsi.store import Store

ADAPTER = '''import hashlib,json,sys
p=json.load(sys.stdin); x=p["input"]
source=hashlib.sha256(json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
ok=isinstance(x.get("value"),int) and not isinstance(x.get("value"),bool)
r={"schema_version":"capability_response_v2","ok":ok,
   "output":{"doubled":x["value"]*2} if ok else None,
   "identity":x["identity"] if ok else None,
   "source_receipts":[{"kind":"input","sha256":source}],
   "error":None if ok else {"code":"invalid_input","message":"value must be an integer"}}
json.dump(r,sys.stdout)
'''


def specification():
    return {'schema_version': 'capability_request_v2', 'capability_id': 'fixture_double',
        'purpose': 'Exercise executable acceptance only', 'scope': {'domain': 'synthetic_integer_fixture'},
        'input_contract': {'value': 'integer', 'identity': 'string'}, 'output_contract': {'doubled': 'integer'},
        'scientific_impact': False, 'baseline_scientific_protocol': 'science1',
        'candidate_scientific_protocol': 'science1', 'new_study_id': None,
        'execution_profile': 'reviewed_local_python_json_v1',
        'recovery': {'query_supported': False, 'idempotency_supported': False, 'unknown_result': 'blocked_external'},
        'limits': {'wall_seconds_per_case': 2, 'cpu_seconds_per_case': 1, 'output_bytes_per_case': 8192},
        'cases': [{'case_id': 'normal', 'kind': 'normal', 'input': {'value': 3, 'identity': 'source-A'},
                   'expected_output': {'doubled': 6}, 'expected_identity': 'source-A', 'expected_error_code': None},
                  {'case_id': 'failure', 'kind': 'failure', 'input': {'value': 'bad'},
                   'expected_output': None, 'expected_identity': None, 'expected_error_code': 'invalid_input'}]}


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'ledger'); self.addCleanup(self.store.close)
        self.store.create_run('run1', {'fixture': True})
        self.registry = CapabilityRegistry(self.store, 'run1')
        self.spec = specification()
        self.registry.request('build1', self.spec, key='build')
        self.path = self.root / 'adapter.py'; self.path.write_text(ADAPTER)

    def packet(self):
        artifact = digest(self.path.read_bytes())
        manifest = {'schema_version': 'capability_delivery_v2', 'capability_id': self.spec['capability_id'],
            'capability_version': 'fixture-1', 'artifact_sha256': artifact, 'scope': self.spec['scope'],
            'input_contract': self.spec['input_contract'], 'output_contract': self.spec['output_contract'],
            'packages': {}, 'engineering_provenance': {'author': 'test_fixture', 'reviewed_source_sha256': artifact}}
        return self.registry.packet('build1', self.path, manifest, execution_authorization_sha256=artifact)

    def actual_effect(self, packet):
        effect = self.store.mutate('run1', 'reserve-test', 'fixture_reserved_effect', lambda tx: tx.insert('effect', 'effect1',
            {'status': 'dispatching', 'kind': 'capability_acceptance', 'metadata': {'capability_request_id': 'build1'},
             'packet_ref': tx.blob(packet), 'result_ref': None}), payload={'packet': packet})
        receipt = execute_acceptance(effect, self.store.read_blob(effect['packet_ref']))
        self.store.mutate('run1', 'receipt-test', 'fixture_effect_receipt', lambda tx: tx.update('effect', 'effect1',
            {'status': receipt['status'], 'result_ref': tx.blob(receipt)}), payload={'receipt': receipt})
        return receipt

    def test_actual_normal_failure_identity_sources_and_resources_are_verified(self):
        receipt = self.actual_effect(self.packet())
        self.assertTrue(receipt['capability_acceptance']['accepted'])
        self.assertEqual(receipt['actual_costs']['tool_calls'], 2)
        self.assertGreater(receipt['actual_costs']['wall_seconds'], 0)
        result = self.registry.accept('build1', 'effect1', key='accept')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.store.read_blob(result['artifact_ref']), ADAPTER.encode())
        self.assertEqual(result, self.registry.accept('build1', 'effect1', key='accept'))
        self.store.verify_history('run1')

    def test_names_or_prompts_cannot_replace_executable_delivery(self):
        packet = self.packet()
        self.path.write_text('This is not executable Python')
        with self.assertRaises(SyntaxError): self.packet()
        self.path.unlink(); self.path.symlink_to('/tmp/no-adapter')
        with self.assertRaises(ValueError):
            self.registry.packet('build1', self.path, packet['manifest'],
                                 execution_authorization_sha256=packet['execution_authorization_sha256'])

    def test_artifact_authorization_is_exact(self):
        packet = self.packet()
        packet['adapter_source'] += '\n# changed after review'
        with self.assertRaisesRegex(ValueError, 'authorization'):
            execute_acceptance({'status': 'dispatching', 'metadata': {'capability_request_id': 'build1'}}, encoded(packet))

    def test_changed_identity_does_not_pass_even_if_function_output_matches(self):
        self.path.write_text(ADAPTER.replace('x["identity"] if ok else None', '"wrong" if ok else None'))
        receipt = self.actual_effect(self.packet())
        self.assertFalse(receipt['capability_acceptance']['accepted'])
        result = self.registry.accept('build1', 'effect1', key='accept')
        self.assertEqual(result['status'], 'failed_acceptance')
        self.assertEqual(self.store.list('run1', 'capability'), [])

    def test_fake_source_receipt_is_rejected(self):
        self.path.write_text(ADAPTER.replace('"sha256":source', '"sha256":"0"*64'))
        receipt = self.actual_effect(self.packet())
        self.assertFalse(receipt['capability_acceptance']['accepted'])
        self.assertEqual(self.registry.accept('build1', 'effect1', key='accept')['status'], 'failed_acceptance')

    def test_claimed_pass_is_recomputed_from_raw_output(self):
        self.path.write_text(ADAPTER.replace('x["identity"] if ok else None', '"wrong" if ok else None'))
        receipt = self.actual_effect(self.packet())
        for case in receipt['capability_acceptance']['cases']:
            case['checks'] = {name: True for name in case['checks']}
            case['passed'] = True
        receipt['capability_acceptance']['accepted'] = True
        self.store.mutate('run1', 'tampered-receipt', 'fixture_adversarial_receipt', lambda tx: tx.update('effect', 'effect1',
            {'result_ref': tx.blob(receipt)}), payload={'receipt': receipt})
        with self.assertRaisesRegex(ValueError, 'independently'):
            self.registry.accept('build1', 'effect1', key='accept')
        self.assertEqual(self.store.list('run1', 'capability'), [])

    def test_invalid_utf8_is_preserved_and_records_failed_acceptance(self):
        self.path.write_text('import sys\nsys.stdout.buffer.write(bytes([255]))\n')
        receipt = self.actual_effect(self.packet())
        self.assertEqual(receipt['capability_acceptance']['cases'][0]['stdout_hex'], 'ff')
        self.assertEqual(self.registry.accept('build1', 'effect1', key='accept')['status'], 'failed_acceptance')

    def test_adapter_deleting_own_source_is_failed_not_unknown(self):
        self.path.write_text(ADAPTER + '\nfrom pathlib import Path\nPath(__file__).unlink()\n')
        receipt = self.actual_effect(self.packet())
        self.assertFalse(receipt['capability_acceptance']['accepted'])
        self.assertEqual(self.registry.accept('build1', 'effect1', key='accept')['status'], 'failed_acceptance')

    def test_unknown_effect_cannot_register_capability(self):
        packet = self.packet()
        self.store.mutate('run1', 'unknown', 'fixture_unknown', lambda tx: tx.insert('effect', 'effect1',
            {'status': 'unknown', 'metadata': {'capability_request_id': 'build1'}, 'packet_ref': tx.blob(packet), 'result_ref': None}),
            payload={'packet': packet})
        with self.assertRaisesRegex(ValueError, 'succeeded'): self.registry.accept('build1', 'effect1', key='accept')

    def test_failed_inputs_must_be_part_of_prospective_contract(self):
        spec = specification(); spec['cases'][1] = copy.deepcopy(spec['cases'][0]); spec['cases'][1]['case_id'] = 'normal2'
        with self.assertRaisesRegex(ValueError, 'Both'): validate_request(spec)

    def test_scientific_change_cannot_reuse_original_protocol(self):
        spec = specification(); spec['scientific_impact'] = True
        with self.assertRaisesRegex(ValueError, 'new protocol'): validate_request(spec)
        spec['candidate_scientific_protocol'] = 'science2'; spec['new_study_id'] = 'study2'
        validate_request(spec)

    def test_unverified_idempotency_or_query_support_cannot_be_claimed(self):
        spec = specification(); spec['recovery']['query_supported'] = True
        with self.assertRaisesRegex(ValueError, 'query'): validate_request(spec)

    def test_timeout_is_an_actual_failed_acceptance_case(self):
        self.spec['limits']['wall_seconds_per_case'] = 0.05
        self.registry.request('build2', self.spec, key='build2')
        self.path.write_text('import time\ntime.sleep(1)\n')
        packet = self.packet()
        packet['request_spec']['limits']['wall_seconds_per_case'] = 0.05
        packet['request_spec_sha256'] = digest(packet['request_spec'])
        receipt = execute_acceptance({'status': 'dispatching', 'metadata': {'capability_request_id': 'build1'}}, encoded(packet))
        self.assertFalse(receipt['capability_acceptance']['accepted'])
        self.assertTrue(all(case['timed_out'] for case in receipt['capability_acceptance']['cases']))
        self.assertLess(receipt['actual_costs']['wall_seconds'], 1)


if __name__ == '__main__':
    unittest.main()
