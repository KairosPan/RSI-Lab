import argparse
import contextlib
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness import controller as c, meta
from scripts import run_vina_batch as batch
from tests.chemical_fixtures import METHOD, annotate


class ChemicalPreparationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.rows = [{'candidate_id': f'C{i:02d}', 'smiles': 'C' * (i + 1),
                      'structure': {'formal_charge': 0, 'HBD': 0, 'HBA': 0}} for i in range(20)]
        self.registry = annotate(self.rows)
        self.manifest = {'public_input_mode': 'structure_only', 'is_mock': False,
                         'candidates': copy.deepcopy(self.rows), 'chemical_preparation': {'method': METHOD}}
        self.write()

    def write(self):
        c.dump(self.data / 'candidates.json', {'candidates': self.rows})
        c.dump(self.data / 'manifest.json', self.manifest)
        c.dump(self.data / 'chemical_states.json', self.registry)

    def test_lab_receives_prepared_features_parent_identity_and_preparation_evidence(self):
        rows, evidence = c.freeze_public_data(self.data)
        self.assertEqual(rows[0]['prepared_state_id'], self.registry['records'][0]['prepared_state_id'])
        self.assertEqual(rows[0]['features']['formal_charge'], 0)
        self.assertEqual(rows[0]['canonical_parent_identity'], 'C')
        self.assertEqual(evidence['ev_chemical_preparation']['method'], METHOD)
        self.assertIn('not a shared assay condition', evidence['ev_chemical_preparation']['limitations'][0])
        self.assertEqual(evidence['ev_chemical_state_C00']['method_evidence_id'], 'ev_chemical_preparation')
        self.assertNotIn('method', evidence['ev_chemical_state_C00'])
        self.assertEqual(sum('method' in item for item in evidence.values()), 1)

    def test_ambiguous_state_cannot_enter_single_state_evaluator(self):
        self.registry['records'][0]['states'] *= 2
        self.write()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            c.freeze_public_data(self.data)

    def test_accepted_label_cannot_override_state_validation_or_unique_count(self):
        original = copy.deepcopy(self.registry)
        mutations = [
            ('issues', ['source_stereochemistry_lost_or_changed']),
            ('issues', None), ('count', 2), ('count', True), ('count', None),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                self.registry = copy.deepcopy(original)
                record = self.registry['records'][0]
                if field == 'issues':
                    record['states'][0]['validation_issues'] = value
                else:
                    record['unique_state_count'] = value
                self.write()
                with self.assertRaisesRegex(ValueError, 'one unique state and no validation issues'):
                    c.freeze_public_data(self.data)

    def test_mismatched_prepared_identity_and_parent_alias_are_rejected(self):
        original = copy.deepcopy(self.rows)
        self.rows[0]['prepared_smiles'] = 'N'
        self.write()
        with self.assertRaisesRegex(ValueError, 'provenance'):
            c.freeze_public_data(self.data)
        self.rows = original
        for table in (self.rows, self.manifest['candidates'], self.registry['records']):
            table[1]['canonical_parent_identity'] = table[0]['canonical_parent_identity']
        self.write()
        with self.assertRaisesRegex(ValueError, 'Duplicate parent'):
            c.freeze_public_data(self.data)

    def test_registry_required_and_method_cannot_change_to_assay_claim(self):
        (self.data / 'chemical_states.json').unlink()
        with self.assertRaisesRegex(ValueError, 'chemical_states.json'):
            c.freeze_public_data(self.data)
        self.registry = copy.deepcopy(self.registry)
        self.manifest = copy.deepcopy(self.manifest)
        for method in (self.registry['method'], self.manifest['chemical_preparation']['method']):
            method['pH']['interpretation'] = 'measured_common_assay_pH'
        self.write()
        with self.assertRaisesRegex(ValueError, 'exploratory'):
            c.freeze_public_data(self.data)

    def test_protocol_freezes_method_and_registry_without_pool_specific_method_identity(self):
        receptor = self.data / 'receptor'
        c.dump(receptor / 'receptor_manifest.json', {'center': batch.CENTER, 'size': batch.SIZE})
        for name in ('receptor.pdbqt', 'receptor_effective.pdb', 'reference.sdf'):
            (receptor / name).write_text('unit fixture')
        args = argparse.Namespace(data=self.data, vina=self.root / 'vina', out=self.root / 'protocol.json',
                                  protocol_id='test', pose_config=c.ROOT / 'scripts/vina_pose_checks.json')
        engine = {'engine': 'AutoDock Vina', 'vina_version': {'returncode': 0, 'stdout': 'AutoDock Vina v1.2.7'},
                  'vina_sha256': 'a' * 64}
        with patch.object(batch.worker, 'engine_receipt', return_value=engine), \
                patch.object(batch, 'versions', return_value={}), contextlib.redirect_stdout(io.StringIO()):
            protocol = batch.freeze_protocol(args)
        self.assertEqual(protocol['chemical_preparation'], {'method': METHOD})
        self.assertEqual(protocol['input_sha256']['chemical_states.json'], c.sha(self.data / 'chemical_states.json'))
        other_pool = copy.deepcopy(protocol)
        other_pool['input_sha256']['chemical_states.json'] = 'b' * 64
        self.assertEqual(meta.scientific_protocol(protocol), meta.scientific_protocol(other_pool))

    def test_init_archives_registry_and_rejects_unpinned_registry(self):
        protocol = {**batch.contract.PROFILE, 'tau': None, 'm_min': None, 'chemical_preparation': {'method': METHOD},
                    'vina_version': {'returncode': 0, 'stdout': 'AutoDock Vina v1.2.7'}, 'vina_sha256': 'a' * 64,
                    **batch.source_hashes(), 'input_sha256': {name: c.sha(self.data / name)
                    for name in ('candidates.json', 'manifest.json', 'chemical_states.json')}}
        c.dump(self.root / 'protocol.json', protocol)
        args = argparse.Namespace(data=self.data, protocol=self.root / 'protocol.json', out=self.root / 'campaign',
                                  harness=c.ROOT / 'harness/h0', model='unused', effort='medium',
                                  phase='development', execution_mode='live')
        with contextlib.redirect_stdout(io.StringIO()):
            state = c.init(args)
        self.assertEqual(state['frozen_hashes']['chemical_states.json'], c.sha(self.data / 'chemical_states.json'))
        protocol['input_sha256'].pop('chemical_states.json')
        c.dump(self.root / 'protocol.json', protocol)
        args.out = self.root / 'unpinned'
        with self.assertRaisesRegex(ValueError, 'bind the chemical'):
            c.init(args)


if __name__ == '__main__':
    unittest.main()
