"""Boundary receipt tests using local software fixtures, never live execution."""
import copy
import hashlib
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lab_harness import controller as c, intake, versions
from scripts import run_agentic_study as study
from test_meta_cycle import write_meta_receipt


class StudyCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.cleanup)
        source = Path(study.__file__).resolve()
        self.artifacts = {'driver_source': {'path': source, 'title': 'Actual study checkpoint implementation'}}
        self.source_hash = c.sha(source)

    def cleanup(self):
        for path in self.root.rglob('*'):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        self.tmp.cleanup()

    def readiness(self, stage):
        kind = {'before_docking': 'campaign_registration', 'before_validation': 'validation_admission',
                'before_promotion': 'promotion_gate', 'before_next_development': 'selected_policy_handoff'}[stage]
        guard = {'requirement_type': kind, 'stage': stage, 'checkpoint': study.CHECKPOINTS[stage],
                 'implementation': {'driver_source': self.source_hash}}
        registry = {'actual_guard': guard}
        return {'status': 'ready_for_exploratory', 'guard_registry': registry,
                'guard_registry_sha256': hashlib.sha256(intake.encoded(registry)).hexdigest(),
                'evidence_hashes': {'driver_source': self.source_hash},
                'deferred_obligations': [{'guard_id': 'actual_guard', 'stage': stage,
                    'checkpoint': guard['checkpoint'], 'implementation': guard['implementation'],
                    'finding': {'requirement_type': kind}, 'status': 'unresolved_guarded', 'outcome_passed': False}]}

    def receipt_files(self):
        return list((self.root / 'stage_receipts').glob('*.json'))

    def verify(self, stage, **context):
        packet = self.readiness(stage)
        study.verify_checkpoint(self.root, packet, self.artifacts, stage, **context)
        record = c.read(sorted(self.receipt_files())[-1])
        self.assertEqual(record['stage'], stage)
        intake.assert_stage_ready(packet, stage=stage, guard_receipts=record['guard_receipts'])
        for path, digest in record['evidence'].items():
            self.assertTrue(Path(path).is_absolute())
            self.assertEqual(c.sha(path), digest)
        return record

    def workspace(self, *, planned=True):
        root = self.root / 'meta'
        root.mkdir()
        parent = versions.freeze_harness(c.ROOT / 'harness/h0', root / 'parent')
        draft = root / 'draft'
        shutil.copytree(c.ROOT / 'harness/h0', draft)
        (draft / 'lab_planner.md').write_text((draft / 'lab_planner.md').read_text() + '\nSoftware fixture variant.\n')
        candidate = versions.freeze_harness(draft, root / 'candidate')
        shutil.rmtree(draft)
        c.dump(root / 'input.json', {'software_fixture_only': True})
        identity = {'execution_mode': 'live', 'model_attempts': 1, 'model': 'test-model', 'effort': 'medium',
                    'parent_harness_hash': parent['harness_hash'], 'candidate_harness_hash': candidate['harness_hash'],
                    'status': 'candidate_frozen', 'frozen_hashes': {'input.json': c.sha(root / 'input.json')}}
        registration = {'parent_harness_hash': parent['harness_hash'], 'candidate_harness_hash': candidate['harness_hash'],
                        'execution_mode': 'live', 'pairs': [
                            {'baseline': str(self.root / f'baseline_{i}'), 'candidate': str(self.root / f'candidate_{i}')}
                            for i in range(2)]}
        if planned:
            c.dump(root / 'validation_plan.json', registration)
            identity['validation_plan_sha256'] = c.sha(root / 'validation_plan.json')
        c.dump(root / 'meta.json', identity)
        return root, identity, registration

    def test_docking_checkpoint_blocks_without_frozen_campaign_then_records_exact_path(self):
        run = self.root / 'campaign'
        c.dump(run / 'state.json', {'software_fixture_only': True})
        c.dump(run / 'frozen/campaign.json', {'campaign_id': 'software_fixture'})
        with patch.object(study.c, 'assert_frozen', side_effect=ValueError('Frozen source changed')):
            with self.assertRaisesRegex(ValueError, 'Frozen source changed'):
                self.verify('before_docking', run=run)
        self.assertFalse(self.receipt_files())
        with patch.object(study.c, 'assert_frozen') as frozen:
            record = self.verify('before_docking', run=run)
        frozen.assert_called_once_with(run, {'software_fixture_only': True})
        self.assertEqual(record['evidence'], {str(run / 'frozen/campaign.json'): c.sha(run / 'frozen/campaign.json')})

    def test_missing_registration_blocks_validation_then_exact_plan_is_admitted(self):
        root, identity, registration = self.workspace(planned=False)
        with self.assertRaises((KeyError, OSError)):
            self.verify('before_validation', workspace=root, registration=registration)
        self.assertFalse(self.receipt_files())
        c.dump(root / 'validation_plan.json', registration)
        identity['validation_plan_sha256'] = c.sha(root / 'validation_plan.json')
        c.dump(root / 'meta.json', identity)
        record = self.verify('before_validation', workspace=root, registration=registration)
        self.assertIn(str(root / 'validation_plan.json'), record['evidence'])

    def test_candidate_drift_is_rejected_before_any_validation_arm_exists(self):
        root, _, registration = self.workspace()
        source = root / 'candidate/lab_planner.md'
        source.chmod(0o644)
        source.write_text('Changed after registration')
        with self.assertRaisesRegex(ValueError, 'policy differs'):
            self.verify('before_validation', workspace=root, registration=registration)
        self.assertFalse(self.receipt_files())
        self.assertFalse(any(Path(pair[arm]).exists() for pair in registration['pairs'] for arm in ('baseline', 'candidate')))

    def test_existing_arm_or_replay_registration_cannot_pass_validation_admission(self):
        root, identity, registration = self.workspace()
        Path(registration['pairs'][0]['baseline']).mkdir()
        with self.assertRaisesRegex(ValueError, 'precede every'):
            self.verify('before_validation', workspace=root, registration=registration)
        Path(registration['pairs'][0]['baseline']).rmdir()
        registration['execution_mode'] = 'replay'
        c.dump(root / 'validation_plan.json', registration)
        identity['validation_plan_sha256'] = c.sha(root / 'validation_plan.json')
        c.dump(root / 'meta.json', identity)
        with self.assertRaisesRegex(ValueError, 'live registration'):
            self.verify('before_validation', workspace=root, registration=registration)
        self.assertFalse(self.receipt_files())

    def gate_fixture(self):
        root, identity, registration = self.workspace()
        for pair in registration['pairs']:
            for arm in ('baseline', 'candidate'):
                c.dump(Path(pair[arm]) / 'artifact_hashes.json', {'software_fixture_only': True})
        assessment = {'decision': 'accepted', 'eligible_for_next_development_run': True, 'synthetic': False,
                      'validation_plan_sha256': identity['validation_plan_sha256'], 'pairs': copy.deepcopy(registration['pairs'])}
        gate = self.root / 'gate.json'
        c.dump(gate, assessment)
        return root, registration, assessment, gate

    def test_promotion_requires_every_registered_archive_audit_before_receipt(self):
        root, registration, assessment, gate = self.gate_fixture()
        with patch.object(study.meta, 'completed_run', side_effect=ValueError('Archive invalid')) as audited:
            with self.assertRaisesRegex(ValueError, 'Archive invalid'):
                self.verify('before_promotion', workspace=root, assessment=assessment, gate_path=gate)
        self.assertFalse(self.receipt_files())
        self.assertEqual(audited.call_count, 1)
        with patch.object(study.meta, 'completed_run', side_effect=lambda path, phase: (
                Path(path), {}, {'synthetic': False, 'llm_usage_accounting_complete': True})) as audited:
            record = self.verify('before_promotion', workspace=root, assessment=assessment, gate_path=gate)
        self.assertEqual([call.args for call in audited.call_args_list], [
            (pair[arm], 'validation') for pair in registration['pairs'] for arm in ('baseline', 'candidate')])
        self.assertEqual(len(record['evidence']), 6)  # Four archives, plan, actual gate.

    def test_omitted_pair_or_unknown_usage_prevents_promotion(self):
        root, _, assessment, gate = self.gate_fixture()
        omitted = {**assessment, 'pairs': assessment['pairs'][:1]}
        c.dump(gate, omitted)
        with patch.object(study.meta, 'completed_run') as audit:
            with self.assertRaisesRegex(ValueError, 'every registered pair'):
                self.verify('before_promotion', workspace=root, assessment=omitted, gate_path=gate)
            audit.assert_not_called()
        c.dump(gate, assessment)
        with patch.object(study.meta, 'completed_run', side_effect=lambda path, phase: (
                Path(path), {}, {'synthetic': False, 'llm_usage_accounting_complete': False})):
            with self.assertRaisesRegex(ValueError, 'audited live validation'):
                self.verify('before_promotion', workspace=root, assessment=assessment, gate_path=gate)
        self.assertFalse(self.receipt_files())

    def no_change_fixture(self):
        root, identity, _ = self.workspace(planned=False)
        identity['status'] = 'no_change'
        c.dump(root / 'meta.json', identity)
        receipt = write_meta_receipt(root, decision='no_change')
        pointer = {'harness': str(root / 'parent'), 'harness_hash': identity['parent_harness_hash'],
                   'candidate_promoted': False, 'gate': None, 'gate_sha256': None,
                   'reason': 'meta_no_change', 'use': 'new_development_campaign_only'}
        cycle = {'status': 'no_change', 'decision': 'no_change', 'meta_usage': receipt['usage'], 'selected_policy': pointer}
        c.dump(root / 'selected_policy.json', pointer)
        c.dump(root / 'cycle.json', cycle)
        return root, pointer, cycle

    def test_actual_no_change_parent_handoff_needs_no_validation_or_promotion(self):
        root, pointer, _ = self.no_change_fixture()
        record = self.verify('before_next_development', workspace=root)
        self.assertEqual(record['guard_receipts'][0]['status'], 'passed')
        self.assertEqual(pointer['harness'], str(root / 'parent'))
        self.assertFalse((root / 'validation_plan.json').exists())
        self.assertEqual({Path(path).name for path in record['evidence']},
                         {'cycle.json', 'selected_policy.json', 'call_receipt.json'})

    def test_incomplete_cycle_or_false_no_change_cannot_handoff_parent(self):
        root, _, cycle = self.no_change_fixture()
        c.dump(root / 'cycle.json', {**cycle, 'status': 'needs_audit'})
        with self.assertRaisesRegex(ValueError, 'completed, audited Meta cycle'):
            self.verify('before_next_development', workspace=root)
        c.dump(root / 'cycle.json', cycle)
        write_meta_receipt(root, decision='propose_change')
        with self.assertRaisesRegex(ValueError, 'actual no-change'):
            self.verify('before_next_development', workspace=root)
        self.assertFalse(self.receipt_files())

    def test_guard_implementation_drift_blocks_even_when_campaign_check_would_pass(self):
        packet = self.readiness('before_docking')
        packet['evidence_hashes']['driver_source'] = '0' * 64
        with patch.object(study.c, 'assert_frozen') as frozen:
            with self.assertRaisesRegex(ValueError, 'not delivered'):
                study.verify_checkpoint(self.root, packet, self.artifacts, 'before_docking', run=self.root / 'unused')
            frozen.assert_not_called()
        self.assertFalse(self.receipt_files())

    def test_serialized_evidence_capacity_blocks_before_another_model_attempt(self):
        artifacts = {}
        for number in range(3):
            path = self.root / f'escaped_{number}.txt'
            # Each source fits the raw limit, but JSON escaping expands the
            # combined delivered packet past its independently enforced limit.
            path.write_text('\\' * intake.ARTIFACT_LIMIT)
            artifacts[f'escaped_{number}'] = {'path': path, 'title': 'Capacity fixture only'}
        response = {'action': 'inspect', 'artifact_ids': list(artifacts), 'findings': [],
                    'questions': [], 'scientific_limitations': []}
        receipt = {'response': response, 'usage': {'input_tokens': 1, 'output_tokens': 1},
                   'usage_complete': True, 'error': None}
        with patch.object(intake.transport, 'invoke', return_value=receipt) as invoke:
            result = intake.run_intake(goal='Capacity software test', artifacts=artifacts,
                                       out=self.root / 'capacity', max_calls=3)
        self.assertEqual(result['status'], 'not_ready')
        self.assertIn('serialized intake packet exceeds capacity', result['reason'])
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(len(result['calls']), 1)
        self.assertEqual(result['calls'][0]['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
