"""Two scripted positive/negative fixtures; no live model calls."""
import json
from pathlib import Path
import tempfile
import unittest

from lab_harness import input_view
from docking_rsi.demo import SyntheticMetaExecutor
from docking_rsi.role_probe import run_probe
from docking_rsi.store import Store


class RoleProbeTests(unittest.TestCase):
    def test_issue_then_local_request_uses_raw_sources_and_frozen_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'protocol.json'
            original = '{"receptor":"fixture-target","ligands":"fixture-library","preparation_receipt":null}\n'
            source.write_text(original)
            observed = []
            def factory(mode, calls_root):
                action = 'propose_issue' if mode == 'discovery' else 'request_evidence'
                payload = ({'title': 'Preparation provenance is unavailable', 'problem_kind': 'missing_information',
                    'scope': 'supplied fixture protocol', 'hypothesis': 'Preparation evidence may not have been recorded',
                    'competing_explanations': ['Receipt exists in another authorized original'],
                    'discriminating_check': 'Read the original protocol receipt section',
                    'falsifying_observation': 'A complete matching preparation receipt is found'} if mode == 'discovery' else {
                    'source_id': 'protocol', 'source_version': 1, 'object_ids': ['protocol.json'],
                    'fields': ['original_text', 'original_source_sha256', 'provenance'],
                    'query': 'Read the exact original protocol.json for later inspection', 'max_pages': 1, 'max_bytes': 4096})
                executor = SyntheticMetaExecutor(action, payload)
                def invoke(effect, packet_bytes):
                    request = json.loads(packet_bytes)
                    data = input_view.decode(request['prompt'].split('INPUT_DATA_JSON:\n', 1)[1], request['input_view_mode'])
                    observed.append(data)
                    return executor(effect, packet_bytes)
                return invoke
            report = run_probe(root / 'probe', {'protocol': source}, executor_factory=factory)
            self.assertEqual(report['execution_mode'], 'role_recall')
            self.assertEqual(report['actual_meta_effect_count'], 2)
            self.assertEqual(report['local_source_delivery']['status'], 'delivered')
            self.assertFalse(report['local_source_delivery']['query_answer_verified'])
            self.assertTrue(report['local_source_delivery']['request_scope_verified'])
            self.assertEqual(report['costs']['model_calls']['limit'], 2)
            self.assertTrue(report['costs']['model_calls']['hard'])
            self.assertEqual(report['costs']['model_calls']['spent'], 0)
            self.assertEqual(source.read_text(), original)
            self.assertNotIn('owner_interpretation', observed[0])
            self.assertNotIn('domain.gaps', json.dumps(observed[0]))
            self.assertNotIn('issue', observed[0]['task'])
            raw = json.loads(observed[0]['delivered_pages'][0]['content'])
            self.assertEqual(raw['original_text'], original)
            store = Store(root / 'probe')
            try:
                case = json.loads(store.read_blob(report['case_spec_ref']))
                contract = case['authorized_source_contracts']['protocol']
                self.assertEqual(contract['kind'], 'single_local_file')
                self.assertFalse(contract['follows_references'])
                self.assertIn('protocol.json', contract['object_identities'])
                self.assertIn('original_text', contract['supported_fields'])
                self.assertEqual(store.run('role-probe')['config']['authorized_source_contracts'], case['authorized_source_contracts'])
                self.assertEqual(store.read_blob(case['sources'][0]['source_ref']), original.encode())
                names = {row['name'] for row in case['code_snapshot']}
                self.assertIn('docking_rsi/runtime.py', names)
                self.assertIn('lab_harness/transport.py', names)
                self.assertIn('lab_harness/input_view.py', names)
                self.assertTrue(all(store.read_blob(row['source_ref']) for row in case['code_snapshot']))
                store.verify_history('role-probe')
            finally:
                store.close()
            repeat = run_probe(root / 'probe', {'protocol': source}, executor_factory=lambda *args: self.fail('Repeated probe called the model'))
            self.assertEqual(repeat['actual_meta_effect_count'], 2)

    def test_no_issue_fixture_stops_after_raw_first_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'manifest.json'; source.write_text('{"fixture":true,"receipt":"available"}')
            calls = []
            def factory(mode, calls_root):
                calls.append(mode)
                return SyntheticMetaExecutor('no_action', {'reason': 'No additional issue proposed from this narrow synthetic fixture'})
            report = run_probe(root / 'probe', {'manifest': source}, executor_factory=factory)
            self.assertEqual(calls, ['discovery'])
            self.assertEqual(report['actual_meta_effect_count'], 1)
            self.assertEqual(report['issues'], [])
            self.assertEqual(report['pending_historical_questions'], [])
            self.assertFalse(report['question_sent_to_current_user'])
            self.assertEqual(report['status'], 'bounded_probe_finished_without_issue_proposal')
            self.assertIn('smoke test', report['interpretation'])

    def test_unknown_invocation_keeps_cost_reservation_and_is_never_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'manifest.json'; source.write_text('{"fixture":true}')
            calls = []
            def factory(mode, calls_root):
                def unknown(effect, packet):
                    calls.append(effect['id'])
                    return {'status': 'unknown', 'actual_costs': {'model_calls': 1, 'wall_seconds': .01},
                            'error': 'Synthetic missing provider completion/usage receipt'}
                return unknown
            first = run_probe(root / 'probe', {'manifest': source}, executor_factory=factory)
            self.assertEqual(first['status'], 'blocked_external')
            self.assertTrue(first['calls'][0]['accounting_unknown'])
            self.assertGreater(first['costs']['tokens']['reserved'], 0)
            self.assertNotIn('tokens', first['calls'][0]['actual_costs'])
            second = run_probe(root / 'probe', {'manifest': source}, executor_factory=factory)
            self.assertEqual(second['status'], 'blocked_external')
            self.assertEqual(len(calls), 1)

    def test_foreign_artifacts_fields_and_revisions_stay_pending_without_delivery_or_wake(self):
        cases = [
            ({'object_ids': ['manifest.json', 'prepare_chemical_states.py']}, 'requested_objects_unavailable'),
            ({'fields': ['prepared_states']}, 'requested_fields_unsupported'),
            ({'query': 'Deliver protocol.json revision with SHA256 ' + 'a' * 64}, 'requested_artifact_hash_unavailable')]
        for changes, expected_reason in cases:
            with self.subTest(reason=expected_reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'protocol.json'
                original = '{"manifest":"another-artifact","script":"another-version"}'
                source.write_text(original)
                request = {'source_id': 'protocol', 'source_version': 1, 'object_ids': ['protocol.json'],
                    'fields': ['original_text', 'original_source_sha256', 'provenance'],
                    'query': 'Retrieve the exact original file', 'max_pages': 6, 'max_bytes': 200000, **changes}
                def factory(mode, calls_root):
                    if mode == 'discovery':
                        return SyntheticMetaExecutor('propose_issue', {
                            'title': 'Potential revision mismatch', 'problem_kind': 'anomaly', 'scope': 'synthetic source provenance',
                            'hypothesis': 'The referenced artifacts may be different revisions',
                            'competing_explanations': ['Differences may only concern metadata'],
                            'discriminating_check': 'Retrieve exact historical artifacts and compare',
                            'falsifying_observation': 'Matching exact artifacts establish unchanged method inputs'})
                    return SyntheticMetaExecutor('request_evidence', request)
                report = run_probe(root / 'probe', {'protocol': source}, executor_factory=factory)
                note = report['local_source_delivery']
                self.assertEqual(note['status'], 'pending')
                self.assertEqual(note['reason_code'], expected_reason)
                self.assertFalse(note['delivered'])
                self.assertEqual(source.read_text(), original)
                store = Store(root / 'probe')
                try:
                    self.assertEqual(store.list('role-probe', 'delivery'), [])
                    self.assertEqual(store.get('role-probe', 'request', note['request_id'])['status'], 'created')
                    waits = store.list('role-probe', 'wait')
                    self.assertEqual(len(waits), 1)
                    self.assertEqual(waits[0]['status'], 'waiting')
                    self.assertEqual(store.get('role-probe', 'dependency', waits[0]['dependency_id'])['value']['status'], 'pending')
                    saved = json.loads(store.read_blob(note['unavailability_ref']))
                    self.assertEqual(saved['reason_code'], expected_reason)
                    self.assertFalse(saved['request_scope_verified'])
                    self.assertEqual(len(store.list('role-probe', 'fact')), 1)
                    store.verify_history('role-probe')
                finally:
                    store.close()


if __name__ == '__main__':
    unittest.main()
