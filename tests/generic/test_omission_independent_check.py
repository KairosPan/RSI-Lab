"""Independent hand-oracle, tampering and durable checker accounting tests."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from rsi_domains.docking import omission_contract
from rsi_domains.docking import omission_independent_check as checker
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode


def response(value, report):
    return {'schema_version': 'capability_response_v2', 'ok': True, 'output': report,
            'identity': value['identity'], 'source_receipts': [{'kind': 'input', 'sha256': digest(value)}], 'error': None}


class OmissionIndependentTests(unittest.TestCase):
    def setUp(self):
        case = omission_contract.cases()[0]
        self.value, self.report = deepcopy(case['input']), deepcopy(case['expected_output'])

    def test_hand_calculated_normal_fixtures_match_independent_libraries(self):
        for case in omission_contract.cases():
            if case['kind'] != 'normal':
                continue
            with self.subTest(case=case['case_id']):
                result = checker.verify(case['input'], response(case['input'], case['expected_output']))
                self.assertTrue(result['passed'], result['differences'])
                self.assertEqual(result['reference_check']['rdkit_atom_count'], result['reference_check']['raw_atom_count'])
                self.assertEqual(result['reference_check']['rdkit_bond_count'], result['reference_check']['raw_bond_count'])

    def test_legacy_fixed_counts_reference_is_checked_without_requiring_v2000_label(self):
        original = self.value['sources']['reference_sdf']['text']
        lines = original.splitlines()
        lines[3] = lines[3][:6] + '  0' * 9
        text = '\n'.join(lines) + '\n'
        self.value['sources']['reference_sdf'] = {'text': text, 'sha256': digest(text.encode())}
        self.report['input_hashes']['reference_sdf'] = digest(text.encode())
        result = checker.verify(self.value, response(self.value, self.report))
        self.assertTrue(result['passed'], result['differences'])
        self.assertEqual(result['reference_check']['format'], 'legacy_fixed_counts_accepted_by_RDKit')

    def test_atom_subset_duplicate_unknown_position_and_inventory_tampering_rejected(self):
        changes = [lambda r: r['residues'][0]['atoms'].clear(),
                   lambda r: r['residues'].append(deepcopy(r['residues'][0])),
                   lambda r: r['residues'][0]['atoms'][0].update(atom_id='A:36::ARG:FORGED'),
                   lambda r: r['unknown_positions'][0].update(position=[0, 0, 0]),
                   lambda r: r['unknown_positions'].pop(),
                   lambda r: r['inventory'].update(original_heavy_atoms=2),
                   lambda r: r['inventory'].update(original_heavy_atoms=3.0),
                   lambda r: r['residues'][0]['atoms'][0].update(xyz=[0, 0, 1])]
        for index, change in enumerate(changes):
            report = deepcopy(self.report); change(report)
            with self.subTest(index=index):
                result = checker.verify(self.value, response(self.value, report))
                self.assertFalse(result['passed'])
                self.assertTrue(result['differences'])

    def test_distance_tolerance_is_absolute_one_millionth(self):
        near = deepcopy(self.report)
        near['residues'][0]['atoms'][0]['distance_to_reference'] = 0.0000009
        self.assertTrue(checker.verify(self.value, response(self.value, near))['passed'])
        near['residues'][0]['atoms'][0]['distance_to_reference'] = 0.0000011
        self.assertFalse(checker.verify(self.value, response(self.value, near))['passed'])

    def test_original_hash_task_identity_and_receipts_are_required(self):
        answer = response(self.value, self.report)
        answer['identity'] = 'foreign'
        self.assertFalse(checker.verify(self.value, answer)['passed'])
        answer = response(self.value, self.report); answer['source_receipts'][0]['sha256'] = '0' * 64
        self.assertFalse(checker.verify(self.value, answer)['passed'])
        value = deepcopy(self.value); value['sources']['original_pdb']['sha256'] = '0' * 64
        self.assertFalse(checker.verify(value, response(value, self.report))['passed'])

    def test_failed_generated_response_does_not_turn_into_successful_geometry(self):
        failed = {'schema_version': 'capability_response_v2', 'ok': False, 'output': None,
                  'identity': None, 'source_receipts': [{'kind': 'input', 'sha256': digest(self.value)}],
                  'error': {'code': 'unsupported_format', 'message': 'Actual input rejected'}}
        result = checker.verify(self.value, failed)
        self.assertFalse(result['passed'])
        self.assertEqual(result['status'], 'producer_failed')
        self.assertEqual(result['producer_error'], failed['error'])
        self.assertNotIn('reference_check', result)

    def test_metered_result_is_persisted_once_and_source_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); store = Store(root / 'ledger')
            try:
                controller = Controller(store)
                controller.initialize('r', budgets={'tool_calls': {'limit': 5, 'hard': True},
                    'cpu_seconds': {'limit': 100, 'hard': False}, 'wall_seconds': {'limit': 100, 'hard': False}})
                runtime = Runtime(store, 'r')
                source = root / 'original.json'; source.write_bytes(encode(self.value))
                output = root / 'diagnostic.json'; output.write_bytes(encode(response(self.value, self.report)))
                binding = {'input_path': str(source), 'input_sha256': digest(source.read_bytes()),
                           'output_root': str(root), 'capability_id': omission_contract.CAPABILITY_ID, 'version': '1.0'}
                first = checker.metered_execute(runtime, binding, output)
                second = checker.metered_execute(runtime, binding, output)
                self.assertTrue(first['validation']['passed'])
                self.assertEqual(first['effect']['id'], second['effect']['id'])
                self.assertEqual(store.get('r', 'budget', 'tool_calls')['spent'], 1)
                self.assertEqual(len(store.list('r', 'fact')), 1)
                self.assertTrue(Path(first['output_path']).is_file())
                source.write_text('{}')
                with self.assertRaisesRegex(ValueError, 'differs from the registered'):
                    checker.metered_execute(runtime, binding, output)
                store.verify_history('r')
            finally:
                store.close()

    def test_legacy_sdf_fixture_uses_derived_counts_and_all_original_coordinates(self):
        from rsi_domains.docking import omission_contract_v11
        original = omission_contract_v11.cases()[10]['input']
        points, receipt = checker._reference(original['sources']['reference_sdf']['text'])
        line = original['sources']['reference_sdf']['text'].splitlines()[3]
        self.assertEqual(receipt['rdkit_atom_count'], int(line[:3]))
        self.assertEqual(receipt['rdkit_bond_count'], int(line[3:6]))
        self.assertEqual(len(receipt['coordinates']), int(line[:3]))
        self.assertEqual(len(points), receipt['heavy_atom_count'])
        expected, _ = checker._expected(original, '1.1')
        self.assertEqual(len(expected['residues']), len(original['residues']))
        self.assertTrue(all(row['position'] is None for row in expected['unknown_positions']))
