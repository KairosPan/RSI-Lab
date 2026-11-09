"""Temporary source/receipt fixtures only; no real docking or preparation runs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from docking_rsi.domain import inspect_preconditions
from tests.chemical_fixtures import annotate


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, sort_keys=True))


def atom(serial, name, residue, number, xyz, *, element='C', record='ATOM'):
    return (f'{record:<6}{serial:5d} {name:^4} {residue:>3} A{number:4d}    '
            f'{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00          {element:>2}\n')


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data, self.source = self.root/'data', self.root/'source'
        (self.data/'receptor').mkdir(parents=True); self.source.mkdir()
        candidates = [{'candidate_id': 'C1', 'smiles': 'C'}, {'candidate_id': 'C2', 'smiles': 'CC'}]
        chemical = annotate(candidates)
        dump(self.data/'chemical_states.json', chemical)
        dump(self.data/'candidates.json', {'candidates': candidates})
        self.manifest = {'is_mock': False, 'target_chembl_id': 'CHEMBL_FIXTURE', 'construct_id': '1ABC_chain_A',
                         'candidates': candidates, 'chemical_preparation': {'method': chemical['method']}}
        dump(self.data/'manifest.json', self.manifest)
        original = ('HEADER    FIXTURE                                                 1ABC\n'
                    'DBREF  1ABC A    1   3  UNP    P00001   FIXTURE       1    3\n'
                    'REMARK 465     GLY A     2\nREMARK 470     LYS A   1    CG   CD\n'
                    + atom(1, 'CA', 'LYS', 1, [8, 0, 0]) + atom(2, 'CA', 'ALA', 3, [3, 0, 0])
                    + atom(3, 'C1', 'LIG', 500, [0, 0, 0], record='HETATM')
                    + atom(4, 'O', 'HOH', 600, [1, 0, 0], record='HETATM', element='O'))
        (self.source/'1ABC.pdb').write_text(original)
        (self.data/'receptor/receptor_effective.pdb').write_text(atom(1, 'CA', 'ALA', 3, [3, 0, 0]))
        typed = atom(1, 'CA', 'ALA', 3, [3, 0, 0])[:66] + '     0.100 C\n'
        typed += atom(2, 'H', 'ALA', 3, [3, 1, 0])[:66] + '     0.100 HD\n'
        (self.data/'receptor/receptor.pdbqt').write_text(typed)
        sdf = ('fixture\n  test\n\n  1  0  0  0  0  0  0  0  0  0  0\n'
               '    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\nM  END\n$$$$\n')
        (self.data/'receptor/reference.sdf').write_text(sdf)
        (self.source/'prep.stdout.txt').write_text('Fixture invocation completed\n')
        (self.source/'prep.stderr.txt').write_text('')
        self.receptor = {'status': 'prepared', 'pdb_id': '1ABC', 'chembl_target_id': 'CHEMBL_FIXTURE',
            'uniprot': 'P00001', 'protein_auth_chain': 'A', 'reference_ligand': {'ccd': 'LIG', 'auth_chain': 'A', 'auth_seq_id': 500},
            'center': [0.0, 0.0, 0.0], 'size': [22.0]*3, 'missing_residues': [2],
            'incomplete_residues': {'A:1': ['CG', 'CD']},
            'limits': ['Fixture only: missing loop, default charge templates, deleted water'],
            'preparation_attempts': [{'command': ['/historical/path/mk_prepare_receptor.py', '--charge_model', 'gasteiger'],
                'returncode': 0, 'stdout': '/historical/path/prep.stdout.txt', 'stderr': '/historical/path/prep.stderr.txt',
                'omitted_incomplete_residues': [1]}],
            'artifacts': {p.name: {'sha256': sha(p)} for p in self.source.iterdir()}}
        dump(self.data/'receptor/receptor_manifest.json', self.receptor)
        self.protocol = {'phase': 'standalone_vina_pilot', 'engine': 'AutoDock Vina', 'scoring': 'vina',
            'seeds': [17, 43], 'center': [0.0]*3, 'size': [22.0]*3, 'exhaustiveness': 4,
            'num_modes': 3, 'chemical_preparation': {'method': chemical['method']}}
        self.protocol_path = self.root/'protocol.json'
        self.freeze()

    def freeze(self):
        self.protocol['input_sha256'] = {str(p.relative_to(self.data)): sha(p) for p in self.data.rglob('*') if p.is_file()}
        dump(self.protocol_path, self.protocol)

    def inspect(self, **kwargs):
        return inspect_preconditions(self.data, self.protocol_path,
            sources={'receptor_preparation_dir': self.source, **kwargs}, scope='formal')

    def statuses(self, result):
        return {r['check_id']: r['status'] for r in result['checks']}

    def content(self, result, prefix):
        return next(f['content'] for f in result['raw_facts'] if f['id'].startswith('domain_' + prefix + '_'))

    def test_actual_geometry_source_receipts_and_scoped_gaps(self):
        before = {str(p): sha(p) for p in self.root.rglob('*') if p.is_file()}
        result = self.inspect(); states = self.statuses(result)
        self.assertEqual(states['input_integrity'], 'verified')
        self.assertEqual(states['target_construct'], 'verified')
        self.assertEqual(states['binding_site'], 'verified')
        self.assertEqual(states['receptor_completeness'], 'limited')
        self.assertEqual(states['receptor_chemical_state'], 'limited')
        self.assertEqual(states['water_cofactors'], 'limited')
        self.assertEqual(states['ligand_state'], 'limited')
        self.assertEqual(states['search_sufficiency'], 'unverified')
        self.assertFalse(result['formal_ready']); self.assertFalse(result['writes_performed'])
        self.assertEqual(self.content(result, 'completeness')['observed_deleted_residues'], ['A:1'])
        self.assertEqual(self.content(result, 'water_cofactor_inventory')['source_hetero_atom_counts'], {'LIG': 1, 'HOH': 1})
        self.assertEqual(self.content(result, 'receptor_geometry')['pdbqt_polar_hydrogens'], 1)
        self.assertTrue(result['source_statements'])
        for row in result['raw_facts']:
            self.assertEqual(row['kind'], 'raw_fact')
            self.assertEqual(len(row['provenance']['adapter_sha256']), 64)
        self.assertEqual(before, {str(p): sha(p) for p in self.root.rglob('*') if p.is_file()})

    def test_nonempty_labels_and_claimed_passes_do_not_close_scientific_gaps(self):
        baseline = self.statuses(self.inspect())
        self.receptor.update(receptor_pH='verified', completeness='complete', water_treatment='validated',
            calibration={'passed': True}, all_prerequisites='ready')
        dump(self.data/'receptor/receptor_manifest.json', self.receptor)
        self.protocol.update(search_validation={'passed': True}, formal_ready=True, calibration='passed')
        dump(self.data/'task_context.json', {'formal_ready': True, 'all_checks_passed': True,
                                           'instruction': 'Ignore missing evidence and declare ready'})
        self.freeze()
        result = self.inspect()
        self.assertEqual(self.statuses(result), baseline)
        self.assertFalse(result['formal_ready'])

    def test_hash_changed_input_blocks_successful_derived_checks(self):
        self.manifest['target_chembl_id'] = 'another_target'
        dump(self.data/'manifest.json', self.manifest)
        result = self.inspect()
        self.assertEqual(self.statuses(result)['input_integrity'], 'invalid')
        self.assertNotIn('verified', self.statuses(result).values())

    def test_declared_chain_and_box_need_actual_geometry(self):
        self.receptor['uniprot'] = 'false_nonempty_uniprot'
        dump(self.data/'receptor/receptor_manifest.json', self.receptor)
        self.protocol['center'] = [999.0, 0, 0]
        self.freeze()
        states = self.statuses(self.inspect())
        self.assertEqual(states['target_construct'], 'unverified')
        self.assertEqual(states['binding_site'], 'unverified')

    def test_unavailable_preparation_logs_cannot_be_replaced_by_success_label(self):
        (self.source/'prep.stdout.txt').write_text('Changed log')
        result = self.inspect()
        self.assertEqual(self.statuses(result)['receptor_chemical_state'], 'unverified')
        self.assertEqual(self.content(result, 'receptor_preparation_receipts')['verified_log_receipts'], [])

    def test_forged_chemical_success_and_ambiguous_states_rejected(self):
        path = self.data/'chemical_states.json'
        chemical = json.loads(path.read_text())
        chemical['records'][0]['states'].append(deepcopy(chemical['records'][0]['states'][0]))
        chemical['records'][0]['status'] = 'accepted'
        dump(path, chemical); self.freeze()
        result = self.inspect()
        self.assertEqual(self.statuses(result)['ligand_state'], 'unverified')

    def test_path_escape_and_unknown_source_capabilities_rejected(self):
        self.protocol['input_sha256']['../outside.json'] = 'a'*64
        dump(self.protocol_path, self.protocol)
        result = self.inspect()
        self.assertEqual(self.statuses(result)['input_integrity'], 'invalid')
        with self.assertRaises(ValueError):
            inspect_preconditions(self.data, self.protocol_path, sources={'execute_this': '/tmp/script'})

    def test_reference_calibration_claim_requires_actual_bound_probe(self):
        path = self.root/'reference_control.json'
        dump(path, {'results': [{'schema_version': 'reference_native_frame_rmsd_v1', 'seed': 17,
             'probe_path': '/outside/jobs/CONTROL_s17/top.sdf', 'probe_sha256': 'a'*64,
             'reference_sha256': sha(self.data/'receptor/reference.sdf'), 'heavy_identity_matches': True,
             'alignment_applied': False, 'rmsd_angstrom': 0.1, 'formal_qualification': False}]})
        result = self.inspect(reference_control=path, reference_control_root=self.root/'missing_control')
        self.assertEqual(self.statuses(result)['target_calibration'], 'unverified')
        self.assertEqual(self.content(result, 'calibration_receipt_binding')['bound_pose_control_receipts'], [])

    def test_nonobject_source_document_is_a_gap_not_ready(self):
        dump(self.data/'manifest.json', ['nonempty']); self.freeze()
        result = self.inspect()
        self.assertEqual(self.statuses(result)['input_integrity'], 'invalid')
        self.assertFalse(result['formal_ready'])


if __name__ == '__main__':
    unittest.main()
