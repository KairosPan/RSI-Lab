"""Structure-filter regression fixtures only; no preparation or docking."""
import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec('rdkit'), 'Chemistry environment required')
class DatasetStereoTest(unittest.TestCase):
    def collect(self, smiles):
        from scripts.build_agentic_dataset import collect
        row = {'activity_id': 1, 'molecule_chembl_id': 'TEST_STRUCTURE',
               'parent_molecule_chembl_id': 'TEST_STRUCTURE', 'canonical_smiles': smiles,
               'target_chembl_id': 'CHEMBL301', 'assay_chembl_id': 'TEST_ASSAY',
               'assay_description': 'Authored filter regression fixture'}
        return collect([row], {'candidate_ids': [], 'parent_ids': [], 'canonical_isomeric_smiles': []})

    def test_potential_tetrahedral_sites_missed_by_legacy_chiral_center_search(self):
        pool, _, rejected = self.collect('CNc1ncc2c(-c3ccc(N4CCNCC4)cc3)nn(CC3CCC(N)CC3)c2n1')
        self.assertFalse(pool)
        self.assertEqual(rejected[0]['reason'], 'unspecified_potential_stereochemistry')
        self.assertEqual([r['type'] for r in rejected[0]['stereo_issues']], ['Atom_Tetrahedral'] * 2)

    def test_unspecified_imine_geometry_is_rejected(self):
        pool, _, rejected = self.collect('N=c1[nH]c(=O)[nH]c2c1cnn2-c1ccc(S(N)(=O)=O)cc1')
        self.assertFalse(pool)
        self.assertEqual(rejected[0]['stereo_issues'][0]['type'], 'Bond_Double')

    def test_specified_double_bond_is_retained_and_unspecified_variant_rejected(self):
        pool, _, rejected = self.collect('COc1ccc(/C=C/c2ccc(OC)cc2)cc1')
        self.assertEqual(len(pool), 1)
        self.assertFalse(rejected)
        pool, _, rejected = self.collect('COc1ccc(C=Cc2ccc(OC)cc2)cc1')
        self.assertFalse(pool)
        self.assertEqual(rejected[0]['reason'], 'unspecified_potential_stereochemistry')


if __name__ == '__main__':
    unittest.main()
