"""Fail-closed outer-study admission; never launches models or docking."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_agentic_study as study


class AgenticStudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'goal': 'explore', 'artifacts': {}, 'model': 'real-model', 'effort': 'medium',
                       'harness': str(self.root / 'h0'), 'vina': '/unused/vina',
                       'worker_python': '/unused/python', 'intake_max_calls': 5}
        study.c.dump(self.root / 'study_config.json', self.config)

    def test_ready_intake_summary_cannot_skip_real_calls(self):
        study.c.dump(self.root / 'intake/result.json', {'status': 'ready_for_exploratory'})
        with patch.object(study.intake, 'run_intake') as intake, \
                patch.object(study, 'run_campaign') as lab:
            result = study.run(self.root)
        self.assertEqual(result['status'], 'needs_audit')
        self.assertIn('summary-only', result['error'])
        intake.assert_not_called()
        lab.assert_not_called()

    def test_existing_meta_summary_blocks_before_new_model_calls(self):
        study.c.dump(self.root / 'meta_01/cycle.json', {'status': 'no_change'})
        with patch.object(study.intake, 'run_intake') as intake, \
                patch.object(study, 'execute_cycle') as meta:
            result = study.run(self.root)
        self.assertEqual(result['status'], 'needs_audit')
        intake.assert_not_called()
        meta.assert_not_called()

    def test_completed_study_summary_is_not_automatically_reaccepted(self):
        identity = {'config_sha256': study.c.sha(self.root / 'study_config.json'),
                    'driver_sha256': study.c.sha(study.__file__), 'runtime_hash': study.runtime_hash()}
        study.c.dump(self.root / 'study_state.json', {'identity': identity, 'status': 'completed'})
        with self.assertRaisesRegex(ValueError, 'summary-only automatic restart'):
            study.run(self.root)

    def test_runtime_change_during_intake_blocks_lab_initialization(self):
        with patch.object(study, 'runtime_hash', side_effect=['before', 'after']), \
                patch.object(study.intake, 'run_intake', return_value={'status': 'ready_for_exploratory'}), \
                patch.object(study.c, 'init') as init:
            result = study.run(self.root)
        self.assertEqual(result['status'], 'needs_audit')
        self.assertIn('changed during intake', result['error'])
        init.assert_not_called()

    def test_development_identity_checks_every_requested_axis(self):
        protocol = self.root / 'protocol.json'
        study.c.dump(protocol, {'frozen': True})
        run = self.root / 'campaign'
        expected = {'harness_hash': 'h0', 'protocol_hash': study.c.sha(protocol), 'model': 'model',
                    'effort': 'medium', 'phase': 'development', 'execution_mode': 'live',
                    'runtime_hash': 'runtime', 'source_data_path': str(self.root / 'data')}
        kwargs = dict(data=self.root / 'data', protocol=protocol, harness=self.root / 'h0',
                      model='model', effort='medium', expected_runtime='runtime')
        with patch.object(study.c, 'assert_frozen'), \
                patch.object(study.versions, 'load_harness', return_value={'harness_hash': 'h0'}):
            for field in expected:
                with self.subTest(field=field):
                    study.c.dump(run / 'state.json', {**expected, field: 'other'})
                    with self.assertRaises(ValueError):
                        study.verify_development_identity(run, **kwargs)
            study.c.dump(run / 'state.json', expected)
            self.assertEqual(study.verify_development_identity(run, **kwargs), expected)

    def test_fresh_no_change_flow_still_runs_both_real_entrypoints(self):
        for name in study.COHORTS:
            study.c.dump(self.root / 'data' / name / 'assay_details.json', {})
        study.c.dump(self.root / 'intake_artifacts/capabilities.json', {})
        calls = []

        def intake(**kwargs):
            calls.append('intake')
            result = {'status': 'ready_for_exploratory', 'findings': [], 'scientific_limitations': []}
            study.c.dump(self.root / 'intake/result.json', result)
            return result

        def freeze(args):
            study.c.dump(args.out, {'input_sha256': {}})

        def init(args):
            state = {'harness_hash': 'h0', 'protocol_hash': study.c.sha(args.protocol),
                     'model': args.model, 'effort': args.effort, 'phase': args.phase,
                     'execution_mode': args.execution_mode, 'runtime_hash': 'runtime',
                     'source_data_path': str(args.data), 'status': 'ready'}
            study.c.dump(Path(args.out) / 'state.json', state)

        def campaign(path, **kwargs):
            calls.append(Path(path).name)
            report = {'phase': 'development', 'harness_hash': 'h0', 'shortlist': [],
                      'paired_pose_valid_candidates': 0, 'mean_qualified_docking_energy_kcal_mol': None,
                      'observed_input_plus_output_tokens': 1, 'model_calls': [{'model_called': True}]}
            study.c.dump(Path(path) / 'evaluation.json', report)
            study.c.dump(Path(path) / 'artifact_hashes.json', {})
            return {'status': 'completed'}

        def cycle(**kwargs):
            calls.append('meta')
            workspace = kwargs['workspace']
            study.c.dump(workspace / 'selected_policy.json', {'harness': self.config['harness'], 'harness_hash': 'h0'})
            return {'status': 'no_change', 'decision': 'no_change'}

        with patch.object(study, 'runtime_hash', return_value='runtime'), \
                patch.object(study.intake, 'run_intake', side_effect=intake), \
                patch.object(study.worker_adapter, 'freeze_protocol', side_effect=freeze), \
                patch.object(study.worker_adapter, 'input_hashes', return_value={}), \
                patch.object(study.versions, 'load_harness', return_value={'harness_hash': 'h0'}), \
                patch.object(study.c, 'assert_frozen'), patch.object(study.c, 'init', side_effect=init), \
                patch.object(study, 'verify_checkpoint') as checkpoint, \
                patch.object(study, 'run_campaign', side_effect=campaign), \
                patch.object(study, 'execute_cycle', side_effect=cycle), \
                patch.object(study.meta, 'completed_run', side_effect=lambda path, *a: (path, {}, study.c.read(Path(path) / 'evaluation.json'))):
            result = study.run(self.root)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(calls, ['intake', 'development_01', 'meta', 'development_02'])
        self.assertFalse(result['formal_qualification_assessed'])
        self.assertEqual(checkpoint.call_count, 3)


if __name__ == '__main__':
    unittest.main()
