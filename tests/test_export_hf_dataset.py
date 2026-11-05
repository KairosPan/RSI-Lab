"""Synthetic archive/export fixtures only; never read live scientific archives."""
import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts import export_hf_dataset as export


class PrivateDatasetExportTest(unittest.TestCase):
    def test_archive_preserves_bytes_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'raw.bin'
            original = b'original\x00bytes\r\nnot normalized\n'
            source.write_bytes(original)
            for name in ('first.tar.gz', 'second.tar.gz'):
                export.archive(root / name, [('run/raw.bin', source)])
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual((root / 'first.tar.gz').read_bytes(), (root / 'second.tar.gz').read_bytes())
            with tarfile.open(root / 'first.tar.gz') as tar:
                self.assertEqual(tar.extractfile('run/raw.bin').read(), original)
                record = json.load(tar.extractfile('EXPORT_MEMBER_HASHES.json'))['members'][0]
                self.assertEqual(record['sha256'], hashlib.sha256(original).hexdigest())

    def test_credential_scan_blocks_without_echoing_value_and_unsafe_members_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'fixture.txt'
            fake_credential = 'hf_' + 'X' * 30
            path.write_text(fake_credential)
            with self.assertRaises(ValueError) as result:
                export.checked_bytes(path)
            self.assertNotIn(fake_credential, str(result.exception))
            path.write_text('ordinary fixture')
            with self.assertRaises(ValueError):
                export.archive(root / 'unsafe.tar.gz', [('../escape.txt', path)])
            (root / 'link').symlink_to(path)
            with self.assertRaises(ValueError):
                export.checked_bytes(root / 'link')

    def test_invalid_pose_keeps_raw_energy_but_has_no_qualified_score(self):
        base = {'subset': 'fixture', 'campaign': 'arm1', 'cohort': 'validation', 'study_phase': 'validation',
                'candidate_id': 'C', 'canonical_parent_identity': 'CN', 'prepared_smiles': 'C[NH3+]',
                'prepared_state_id': None, 'selected_for_shortlist': False, 'status': 'completed',
                'identity_preserved': True, 'pose_valid': True, 'selected_model': 1}
        jobs = [{**base, 'seed': 17, 'vina_affinity_kcal_mol': -8.0},
                {**base, 'seed': 43, 'vina_affinity_kcal_mol': -6.0, 'pose_valid': False}]
        result = export.aggregate(jobs)[0]
        self.assertEqual(result['raw_median_vina_affinity'], -7.0)
        self.assertIsNone(result['qualified_median_vina_affinity'])
        self.assertFalse(result['two_seed_valid'])
        with self.assertRaisesRegex(ValueError, 'seed pair'):
            export.aggregate(jobs[:1])
        self.assertEqual(len(export.aggregate(jobs + [{**r, 'campaign': 'arm2'} for r in jobs])), 2)

    def test_manifest_covers_archive_and_detects_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'rows.jsonl').write_text('{"fixture":true}\n')
            export.archive(root / 'assets.tar.gz', [('fixture/rows.jsonl', root / 'rows.jsonl')])
            export.finalize_manifest(root, {'schema_version': 'fixture'}, 'immutable-reference')
            self.assertEqual(export.verify_bundle(root)['files'], 4)
            self.assertEqual(export.read(root / 'dataset_manifest.json')['code_ref'], 'immutable-reference')
            (root / 'rows.jsonl').write_text('{"fixture":false}\n')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                export.verify_bundle(root)


if __name__ == '__main__':
    unittest.main()
