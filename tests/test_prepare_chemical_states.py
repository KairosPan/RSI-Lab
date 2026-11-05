"""Authored chemistry fixtures; no docking, network, or activity-labelled data."""
import importlib.util
import time
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('rdkit'), 'Chemistry environment required')
class ChemicalStatePreparationTest(unittest.TestCase):
    def setUp(self):
        from rdkit import Chem
        from scripts import prepare_chemical_states as prep
        self.Chem, self.prep = Chem, prep

    def prepare(self, source, outputs):
        return self.prep.prepare_record({'candidate_id': 'AUTHORED', 'canonical_smiles': source},
                                        lambda _: [self.Chem.MolFromSmiles(s) for s in outputs])

    def test_charge_change_preserves_parent_and_deterministic_state_id(self):
        record = self.prepare('CC(=O)O', ['CC(=O)[O-]'])
        self.assertEqual(record['status'], 'accepted')
        self.assertEqual(record['source_smiles'], 'CC(=O)O')
        self.assertEqual(record['canonical_parent_identity'], 'CC(=O)O')
        self.assertEqual(record['states'][0]['formal_charge'], -1)
        self.assertNotEqual(record['source_smiles'], record['prepared_smiles'])
        self.assertEqual(record['prepared_state_id'], self.prepare('CC(=O)O', ['CC(=O)[O-]'])['prepared_state_id'])

    def test_ambiguous_zero_and_overflow_are_never_selected(self):
        for outputs, reason in (([], 'zero_predicted_states'),
                                (['CN', 'C[NH3+]'], 'ambiguous_predicted_states'),
                                (['CN'] * (self.prep.MAX_STATES + 1), 'output_state_limit_exceeded')):
            with self.subTest(reason=reason):
                result = self.prepare('CN', outputs)
                self.assertEqual(result['reason'], reason)
                self.assertIsNone(result['prepared_smiles'])
        ambiguous = self.prepare('CN', ['CN', 'C[NH3+]'])
        self.assertEqual(len(ambiguous['states']), 2)

    def test_heavy_identity_and_isotopes_are_preserved(self):
        for source, output in (('CCO', 'CCN'), ('CCO', 'COC'), ('[13CH3]CO', 'CCO'), ('CCO', 'C[13CH2]O')):
            with self.subTest(source=source, output=output):
                result = self.prepare(source, [output])
                self.assertEqual(result['status'], 'rejected')
                self.assertIn('heavy_atom_identity_changed', result['states'][0]['validation_issues'])

    def test_tetrahedral_loss_inversion_and_double_geometry_are_rejected(self):
        cases = [('C[C@H](O)C(=O)O', 'CC(O)C(=O)[O-]'),
                 ('C[C@H](O)C(=O)O', 'C[C@@H](O)C(=O)[O-]'),
                 ('C/C=C/C', 'C/C=C\\C'), ('C/C=C/C', 'CCCC')]
        for source, output in cases:
            with self.subTest(output=output):
                result = self.prepare(source, [output])
                self.assertEqual(result['status'], 'rejected')
                self.assertIn('source_stereochemistry_lost_or_changed', result['states'][0]['validation_issues'])
        self.assertEqual(self.prepare('C[C@H](O)C(=O)O', ['C[C@H](O)C(=O)[O-]'])['status'], 'accepted')

    def test_new_unspecified_stereo_is_rejected(self):
        result = self.prepare('CCC(C)=O', ['CC=C(C)O'])
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('unspecified_potential_stereochemistry', result['states'][0]['validation_issues'])

    def test_bounded_worker_records_error_and_kills_timeout(self):
        row = {'candidate_id': 'AUTHORED', 'canonical_smiles': 'CN'}
        def fail(_):
            raise RuntimeError('authored failure')
        def slow(_):
            time.sleep(5)
            return []
        self.assertEqual(self.prep.bounded_record(row, fail)['reason'], 'enumeration_error')
        start = time.monotonic()
        self.assertEqual(self.prep.bounded_record(row, slow, timeout=0.03)['reason'], 'enumeration_timeout')
        self.assertLess(time.monotonic() - start, 2)

    def test_upstream_unexpected_exception_diagnostic_rejects_even_valid_output(self):
        def hidden_error(mol):
            print('uncought exception authored upstream internal error')
            return [mol]
        result = self.prep.bounded_record({'candidate_id': 'AUTHORED', 'canonical_smiles': 'CN'}, hidden_error)
        self.assertEqual(result['reason'], 'enumeration_error')
        self.assertEqual(result['status'], 'rejected')
        self.assertIsNone(result['prepared_smiles'])
        self.assertEqual(len(result['states']), 1)

    def test_prior_ambiguous_states_and_duplicate_parents_are_excluded(self):
        pool = [{'candidate_id': 'A', 'canonical_smiles': 'CC(=O)O'},
                {'candidate_id': 'B', 'canonical_smiles': 'CN'},
                {'candidate_id': 'C', 'canonical_smiles': 'C[NH3+]'}]
        def fake(row, _):
            outputs = ['CC(=O)O', 'CC(=O)[O-]'] if row['candidate_id'].startswith('prior') else (
                      ['CC(=O)[O-]'] if row['candidate_id'] == 'A' else ['C[NH3+]'])
            return self.prep.prepare_record(row, lambda _: [self.Chem.MolFromSmiles(s) for s in outputs])
        with patch.object(self.prep, 'engine', return_value=object()), \
             patch.object(self.prep, 'method', return_value={'authored_fixture': True}), \
             patch.object(self.prep, 'bounded_record', side_effect=fake):
            rows, audit = self.prep.prepare_pool(pool, {'canonical_isomeric_smiles': ['CC(=O)O']})
        self.assertFalse(rows)
        self.assertEqual(audit['rejection_counts']['prior_prepared_state_overlap'], 1)
        self.assertEqual(audit['rejection_counts']['prepared_state_duplicate_parents'], 2)
        self.assertEqual(audit['prior_identity_audit']['incomplete_count'], 0)

    def test_any_incomplete_prior_enumeration_blocks_selection(self):
        for reason in ('enumeration_error', 'enumeration_timeout', 'enumeration_process_failed',
                       'output_state_limit_exceeded', 'zero_predicted_states'):
            with self.subTest(reason=reason), patch.object(self.prep, 'engine', return_value=object()), \
                 patch.object(self.prep, 'method', return_value={'authored_fixture': True}), \
                 patch.object(self.prep, 'bounded_record', return_value={'candidate_id': 'prior', 'states': [], 'reason': reason}) as bounded:
                with self.assertRaisesRegex(ValueError, 'cohort selection blocked'):
                    self.prep.prepare_pool([{'candidate_id': 'new', 'canonical_smiles': 'CN'}],
                                           {'canonical_isomeric_smiles': ['CCN']})
                self.assertEqual(bounded.call_count, 1)

    def test_pinned_real_engine_acid_base_tautomer_and_stereo(self):
        try:
            scrub = self.prep.engine()
        except ModuleNotFoundError:
            self.skipTest('Optional pinned MolScrub runtime not installed')
        cases = [('CC(=O)O', 'CC(=O)[O-]'), ('CN', 'C[NH3+]'),
                 ('Oc1ccccn1', 'O=c1cccc[nH]1'),
                 ('C[C@H](O)C(=O)O', 'C[C@H](O)C(=O)[O-]')]
        for source, expected in cases:
            with self.subTest(source=source):
                result = self.prep.bounded_record({'candidate_id': 'AUTHORED', 'canonical_smiles': source}, scrub)
                self.assertEqual(result['status'], 'accepted')
                self.assertEqual(result['prepared_smiles'], expected)
        method = self.prep.method()
        self.assertEqual(method['version'], '0.3.2')
        self.assertFalse(method['population_estimates_available'])
        self.assertEqual((method['pH']['low'], method['pH']['high']), (7.5, 8.0))


if __name__ == '__main__':
    unittest.main()
