"""Synthetic archive and dispatch-boundary checks; never invoke Vina."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from rsi_domains.docking import live_vina
from rsi_lab.store import digest, encode
from tests.test_docking_archive import write_archive


class LiveVinaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'data'; (self.data / 'receptor').mkdir(parents=True)
        self.candidates = [{'candidate_id': 'fixture_A', 'canonical_smiles': 'C'}]
        (self.data / 'candidates.json').write_bytes(encode({'candidates': self.candidates}))
        (self.data / 'manifest.json').write_bytes(encode({'fixture': True}))
        for name in ('receptor_manifest.json', 'receptor.pdbqt', 'receptor_effective.pdb', 'reference.sdf'):
            (self.data / 'receptor' / name).write_text('Synthetic fixture; never used for a scientific process.\n')
        self.binary = self.root / 'vina-fixture'
        self.binary.write_text('Non-executable fixture; it must never be invoked.\n')
        self.protocol_path = self.root / 'protocol.json'
        self.protocol = {**live_vina.vina_contract.PROFILE,
            'vina_version': {'stdout': 'AutoDock Vina v1.2.7', 'returncode': 0},
            'vina_sha256': digest(self.binary.read_bytes()), 'vina_binary_path': str(self.binary),
            'input_sha256': live_vina.batch_runner.input_hashes(self.data),
            **live_vina.batch_runner.source_hashes()}
        self.protocol_path.write_bytes(encode(self.protocol))
        self.inputs = {'data_dir': str(self.data), 'protocol_path': str(self.protocol_path),
                       'selected_ids': ['fixture_A'], 'output_dir': str(self.root / 'new')}

    def test_prepare_binds_pool_protocol_binary_and_sources_without_invoking(self):
        with patch.object(live_vina.vina_worker, 'engine_receipt', side_effect=AssertionError('Unreserved tool call')):
            packet = live_vina.prepare(self.inputs)
        self.assertEqual(packet['protocol_sha256'], digest(self.protocol_path.read_bytes()))
        self.assertEqual(packet['input_sha256'], self.protocol['input_sha256'])
        self.assertEqual(packet['vina_sha256'], self.protocol['vina_sha256'])
        self.assertEqual(live_vina.reservations(packet)['tool_calls'], 2)
        self.assertFalse(Path(self.inputs['output_dir']).exists())

    def test_missing_or_changed_binary_binding_is_rejected(self):
        self.protocol.pop('vina_binary_path')
        self.protocol_path.write_bytes(encode(self.protocol))
        with self.assertRaisesRegex(ValueError, 'explicitly provide vina_binary_path'):
            live_vina.prepare(self.inputs)
        self.protocol['vina_binary_path'] = str(self.binary)
        self.protocol_path.write_bytes(encode(self.protocol))
        self.binary.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'executable hash differs'):
            live_vina.prepare(self.inputs)

    def test_repeated_or_foreign_identity_and_existing_output_are_rejected(self):
        for ids in (['fixture_A'] * 2, ['unregistered_candidate']):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                live_vina.prepare({**self.inputs, 'selected_ids': ids})
        Path(self.inputs['output_dir']).mkdir()
        with self.assertRaisesRegex(ValueError, 'fresh directory'):
            live_vina.prepare(self.inputs)

    def test_changed_input_after_reservation_fails_without_launch(self):
        packet = live_vina.prepare(self.inputs)
        self.protocol_path.write_text(self.protocol_path.read_text() + '\n')
        raw = encode(packet)
        effect = {'id': 'effect_test', 'status': 'dispatching', 'packet_ref': digest(raw)}
        with patch.object(live_vina.batch_runner, 'run', side_effect=AssertionError('Must not launch')):
            receipt = live_vina.execute(effect, raw)
        self.assertEqual(receipt['status'], 'failed_definite')
        self.assertEqual(receipt['actual_costs']['tool_calls'], 0)
        self.assertIn('changed after reservation', receipt['result']['error'])
        self.assertFalse(Path(self.inputs['output_dir']).exists())

    def test_only_committed_exact_dispatching_packets_can_execute(self):
        raw = encode(live_vina.prepare(self.inputs))
        for effect in ({'status': 'prepared', 'packet_ref': digest(raw)},
                       {'status': 'dispatching', 'packet_ref': '0' * 64}):
            with self.subTest(effect=effect), self.assertRaisesRegex(ValueError, 'exact persisted'):
                live_vina.execute(effect, raw)

    def test_synthetic_native_archive_passes_but_score_tampering_fails(self):
        campaign = write_archive(self.root / 'archive')
        batch = campaign / 'batches/online_001'
        request = json.loads((batch / 'request.json').read_text())
        protocol = json.loads((campaign / 'frozen/protocol.json').read_text())
        candidates = json.loads((campaign / 'frozen/candidates.json').read_text())['candidates']
        candidates = [{**row, 'canonical_smiles': row['smiles']} for row in candidates]
        summary, jobs, records = live_vina.verify_batch(batch, campaign, request, candidates, protocol)
        self.assertEqual(len(jobs), len(request['candidate_ids']) * 2)
        self.assertEqual({row['candidate_id'] for row in records}, set(request['candidate_ids']))
        copied = self.root / 'copied'; shutil.copytree(batch, copied)
        result = next((copied / 'jobs').glob('*/result.json'))
        value = json.loads(result.read_text()); value['scores']['vina_affinity'] -= 100
        result.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'artifact hash mismatch'):
            live_vina.verify_batch(copied, campaign, request, candidates, protocol)

    def test_publication_distinguishes_native_measurement_from_qualification(self):
        result = {'candidate_ids': ['A'], 'jobs': [], 'archive_receipts': [], 'output_dir': '/tmp/example',
                  'protocol_sha256': 'a' * 64, 'input_sha256': {}, 'accounting': {}, 'candidates': [], 'error': None}
        published = live_vina.publication({'id': 'e', 'result_ref': 'b' * 64}, {'result': result})
        self.assertEqual(published['facts'][0]['kind'], 'raw_fact')
        self.assertNotIn('raw_archive_validated', published['facts'][0]['content'])
        self.assertEqual(published['conclusions'][0]['kind'], 'validation')
        self.assertFalse(published['conclusions'][0]['content']['formal_qualification'])
