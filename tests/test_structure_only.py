"""Public data projection must not invent or leak biological labels."""
import tempfile
from pathlib import Path
import unittest

from lab_harness import controller as c


class StructureOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.rows = [{'candidate_id': f'C{i}', 'smiles': 'C', 'features': {'hbd': 0}}
                     for i in range(20)]
        c.dump(self.data / 'candidates.json', {'candidates': self.rows})
        c.dump(self.data / 'manifest.json', {'public_input_mode': 'structure_only'})

    def test_missing_labels_are_absent_with_source_evidence(self):
        candidates, evidence = c.freeze_public_data(self.data)
        self.assertNotIn('ic50_nM', candidates[0])
        self.assertNotIn('pIC50', candidates[0])
        self.assertNotIn('ev_ic50_C0', evidence)
        self.assertEqual(evidence['ev_source_C0']['kind'], 'structure_source')
        self.assertEqual(evidence['ev_features_C0']['features']['hbd'], 0)

    def test_activity_values_cannot_enter_structure_only_mode(self):
        self.rows[0]['ic50_nM'] = 10
        c.dump(self.data / 'candidates.json', {'candidates': self.rows})
        with self.assertRaisesRegex(ValueError, 'omit activity'):
            c.freeze_public_data(self.data)

    def test_context_and_unknown_assay_are_preserved(self):
        context = {'formal_ready': False, 'unresolved': ['condition not supplied']}
        c.dump(self.data / 'task_context.json', context)
        _, evidence = c.freeze_public_data(self.data)
        self.assertEqual(evidence['ev_task_context']['context'], context)
        self.assertIsNone(evidence['ev_assay_context']['confidence_score'])

    def test_legacy_mode_still_requires_real_consistent_labels(self):
        c.dump(self.data / 'manifest.json', {})
        with self.assertRaisesRegex(ValueError, 'Missing public structure/IC50'):
            c.freeze_public_data(self.data)


if __name__ == '__main__':
    unittest.main()
