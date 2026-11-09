"""Structure-filter regression fixtures only; no preparation or docking."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('rdkit'), 'Chemistry environment required')
class DatasetStereoTest(unittest.TestCase):
    def test_input_paths_are_explicit_and_no_historical_workspace_is_scanned(self):
        from scripts.build_agentic_dataset import exclusions, parse_args
        arguments = ['--out', 'new-output', '--source', 'source.json',
                     '--source-metadata', 'source.meta.json', '--receptor', 'receptor']
        args = parse_args(arguments)
        self.assertIsNone(args.prior_data)
        self.assertEqual(args.prior_run_root, [])
        self.assertEqual(args.exclude_catalog, [])
        self.assertEqual(exclusions(args)['sources'], [])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(['--out', 'new-output'])

    def test_only_explicit_exclusion_catalog_is_read(self):
        from scripts.build_agentic_dataset import exclusions, parse_args
        with tempfile.TemporaryDirectory() as temp:
            catalog = Path(temp) / 'excluded.json'
            catalog.write_text(json.dumps({'candidates': [{'candidate_id': 'fixture', 'smiles': 'C'}]}))
            args = parse_args(['--out', 'new-output', '--source', 'source.json',
                '--source-metadata', 'source.meta.json', '--receptor', 'receptor',
                '--exclude-catalog', str(catalog)])
            result = exclusions(args)
            self.assertEqual(result['candidate_ids'], ['fixture'])
            self.assertEqual(result['canonical_isomeric_smiles'], ['C'])
            self.assertEqual([row['path'] for row in result['sources']], [str(catalog.resolve())])

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
