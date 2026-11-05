"""Standalone Vina worker tests. The PDBQT round trip uses fabricated pose remarks."""
import argparse
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
try:
    import run_vina_batch as batch
    import vina_contract as contract
    import vina_worker as worker
finally:
    sys.path.pop(0)


class VinaWorkerTests(unittest.TestCase):
    def test_vina_command_pins_scoring_resources_box_seed_and_native_output(self):
        root = Path('/fixture')
        protocol = {**contract.PROFILE, 'center': [1.0, 2.0, 3.0], 'size': [22.0] * 3}
        argv = worker.vina_argv(root / 'vina', protocol, root / 'data', root / 'job', 43)
        opts = dict(zip(argv[1::2], argv[2::2], strict=True))
        self.assertEqual(argv[0], '/fixture/vina')
        self.assertEqual(opts, {'--scoring': 'vina', '--receptor': '/fixture/data/receptor/receptor.pdbqt',
            '--ligand': '/fixture/job/ligand.pdbqt', '--out': '/fixture/job/poses.pdbqt', '--cpu': '2',
            '--exhaustiveness': '4', '--num_modes': '3', '--seed': '43', '--center_x': '1.0', '--center_y': '2.0',
            '--center_z': '3.0', '--size_x': '22.0', '--size_y': '22.0', '--size_z': '22.0'})
        with self.assertRaisesRegex(ValueError, 'seed'):
            worker.vina_argv(root / 'vina', protocol, root / 'data', root / 'job', 99)

    def test_worker_timeout_wins_over_partial_written_result_and_retains_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / 'job'
            engine = {'engine': contract.ENGINE, 'vina_version': {'returncode': 0, 'stdout': 'AutoDock Vina v1.2.7'}, 'vina_sha256': 'a' * 64}
            class Process:
                pid = 123
                returncode = -9
                def __init__(self, *args, **kwargs):
                    worker.dump(job / 'result.json', {'status': 'completed', 'cpu_seconds': 1.0})
                def wait(self, timeout=None):
                    if timeout is not None:
                        raise subprocess.TimeoutExpired('fixture', timeout)
                    return self.returncode
            with mock.patch.object(batch.subprocess, 'Popen', Process), mock.patch.object(batch.os, 'killpg') as kill:
                result = batch.execute_job(job, {'candidate': {'candidate_id': 'A'}, 'seed': 17,
                                                 'protocol': contract.PROFILE}, engine)
            kill.assert_called_once()
            self.assertEqual(result['status'], 'worker_timeout')
            self.assertEqual(result['worker_reported_status'], 'completed')
            self.assertIsNone(result['cpu_seconds'])
            self.assertEqual(result['vina_sha256'], engine['vina_sha256'])

    def test_request_cannot_retry_failed_reserved_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / 'data'
            data.mkdir()
            worker.dump(data / 'candidates.json', {'candidates': [{'candidate_id': 'A', 'smiles': 'C'}]})
            worker.dump(root / 'protocol.json', contract.PROFILE)
            req = {'schema_version': contract.BATCH_SCHEMA, 'phase': 'online', 'campaign_id': 'pilot',
                   'request_id': 'batch1', 'candidate_ids': ['A'], 'input_state_version': 0, 'controller_authorized': True,
                   'protocol_sha256': '0' * 64, 'candidates_sha256': '1' * 64, 'manifest_sha256': '2' * 64}
            path = root / 'request.json'
            worker.dump(path, req)
            args = argparse.Namespace(data=data, protocol=root / 'protocol.json', campaign=root / 'worker',
                                      vina=root / 'vina', request=path, locked_shortlist=None)
            engine = {'engine': contract.ENGINE, 'vina_sha256': 'a' * 64, 'vina_version': {'returncode': 0, 'stdout': 'AutoDock Vina v1.2.7'}}
            def failed(job, spec, receipt):
                return {**receipt, 'candidate_id': 'A', 'seed': spec['seed'], 'status': 'preparation_failed', 'scores': None, 'pose_valid': None, 'cpu_seconds': 0.0}
            with mock.patch.object(batch, 'validate_inputs', return_value=engine), mock.patch.object(batch, 'execute_job', side_effect=failed), contextlib.redirect_stdout(io.StringIO()):
                folder = batch.run(args)
                summary = worker.read(folder / 'batch_summary.json')
                self.assertEqual(summary['status'], 'completed_with_failures')
                self.assertEqual(summary['remaining_candidate_slots'], 7)
                self.assertIsNone(summary['candidates'][0]['qualified_median_vina_affinity'])
                req.update(request_id='batch2', input_state_version=1)
                worker.dump(path, req)
                with self.assertRaisesRegex(ValueError, 'repeated reservation'):
                    batch.run(args)

    def test_meeko_roundtrip_preserves_stereo_and_exports_first_model(self):
        try:
            from rdkit import Chem
            import meeko
        except ImportError:
            self.skipTest('Run with the scientific worker environment (RDKit/Meeko)')
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            prep = worker.prepare_ligand('C[C@H](O)CC', job)
            ligand = (job / 'ligand.pdbqt').read_text()
            self.assertIn('REMARK SMILES ', ligand)
            synthetic_output = ''.join(f'MODEL {i}\nREMARK VINA RESULT: {score:.3f} 0.000 0.000\n{ligand}ENDMDL\n'
                                       for i, score in ((1, -6.3), (2, -6.1)))
            (job / 'poses.pdbqt').write_text(synthetic_output)
            scores = contract.parse_vina_poses(synthetic_output)
            self.assertEqual(worker.export_first_pose(job, scores), prep['canonical_smiles'])
            poses = list(Chem.SDMolSupplier(str(job / 'top.sdf'), removeHs=False))
            self.assertEqual(len(poses), 1)
            self.assertEqual(float(poses[0].GetProp('vina_affinity')), -6.3)
            self.assertEqual(poses[0].GetProp('vina_model'), '1')


if __name__ == '__main__':
    unittest.main()
