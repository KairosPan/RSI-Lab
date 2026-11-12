"""Trial mechanics fixtures; no live model or scientific observations."""
import copy
import json
import tempfile
import unittest

from rsi_lab.store import Store
from rsi_lab.controller import Controller
from rsi_lab.trials import TrialEngine, settle_trial, validate_trial_spec


def specification(mode='isolated_branch'):
    return {'schema_version': 'rsi_trial_v2', 'intervention_kind': 'policy',
        'baseline_versions': {'policy': 'h0', 'capability': 'tool1', 'scientific_protocol': 'science1'},
        'candidate_versions': {'policy': 'h1', 'capability': 'tool1', 'scientific_protocol': 'science1'},
        'study_id': 'study1', 'new_study_id': None, 'applicability': {'target': 'fixture_only'},
        'execution_mode': mode,
        'pairs': [{'pair_id': f'p{i}', 'input_hash': f'input{i}', 'baseline_run_id': f'base{i}', 'candidate_run_id': f'cand{i}'} for i in (1, 2)],
        'minimum_pairs': 2,
        'metrics': [{'name': 'tokens', 'direction': 'lower', 'role': 'primary', 'min_gain': 10,
                     'max_loss': 0, 'per_pair_no_regression': False},
                    {'name': 'coverage', 'direction': 'higher', 'role': 'guard', 'min_gain': 0,
                     'max_loss': 0, 'per_pair_no_regression': True}],
        'release_rule': 'any_primary', 'budget': {'tool_calls': 4, 'wall_seconds': 100},
        'settlement_reserve': {'wall_seconds': 1}, 'recovery_owner': 'controller'}


def measurements(spec, evidence='a' * 64):
    return [{'schema_version': 'trial_measurement_v2', 'trial_id': 'trial1', 'pair_id': pair['pair_id'],
             'arm': arm, 'run_id': pair[arm + '_run_id'], 'input_hash': pair['input_hash'],
             'versions': spec[arm + '_versions'], 'execution_status': 'complete',
             'metrics': {'tokens': 100 if arm == 'baseline' else 80, 'coverage': 5},
             'costs': {'tool_calls': 1, 'wall_seconds': 0.1}, 'evidence_refs': [evidence]}
            for pair in spec['pairs'] for arm in ('baseline', 'candidate')]


class SettlementTests(unittest.TestCase):
    def test_valid_met_and_future_approval_are_separate_axes(self):
        spec = specification(); outcome = settle_trial(spec, measurements(spec))
        self.assertEqual((outcome['execution'], outcome['evidence'], outcome['version_action']), ('valid', 'met', 'approve_next'))
        self.assertEqual(outcome['metric_results'][0]['mean_gain'], 20)

    def test_complete_not_met_is_not_invalid_or_harm(self):
        spec = specification(); rows = measurements(spec)
        for row in rows: row['metrics']['tokens'] = 100
        outcome = settle_trial(spec, rows)
        self.assertEqual((outcome['execution'], outcome['evidence'], outcome['version_action']), ('valid', 'not_met', 'unchanged'))
        self.assertIn('not proof', outcome['interpretation'])

    def test_missing_arm_or_metric_or_cost_is_insufficient_never_zero(self):
        spec = specification()
        examples = [measurements(spec)[:-1], measurements(spec), measurements(spec)]
        examples[1][0]['metrics']['tokens'] = None
        examples[2][0]['costs']['wall_seconds'] = None
        for rows in examples:
            with self.subTest(rows=rows):
                outcome = settle_trial(spec, rows)
                self.assertEqual((outcome['execution'], outcome['evidence'], outcome['version_action']), ('incomplete', 'insufficient', 'unchanged'))

    def test_duplicate_or_wrong_protocol_measurement_rejected(self):
        spec = specification(); rows = measurements(spec)
        with self.assertRaises(ValueError): settle_trial(spec, rows + [rows[0]])
        rows[0]['versions'] = {**rows[0]['versions'], 'scientific_protocol': 'other'}
        with self.assertRaises(ValueError): settle_trial(spec, rows)

    def test_guard_regression_does_not_hide_in_average(self):
        spec = specification(); rows = measurements(spec)
        rows[1]['metrics']['coverage'] = 4
        rows[3]['metrics']['coverage'] = 6
        self.assertEqual(settle_trial(spec, rows)['evidence'], 'not_met')

    def test_invalid_execution_and_budget_overrun_are_invalid_measurement(self):
        spec = specification(); rows = measurements(spec)
        rows[0]['execution_status'] = 'invalid'
        self.assertEqual(settle_trial(spec, rows)['execution'], 'invalid')
        rows = measurements(spec); rows[0]['costs']['wall_seconds'] = 100
        outcome = settle_trial(spec, rows)
        self.assertEqual(outcome['execution'], 'invalid')
        self.assertEqual(outcome['budget_exceeded'], ['wall_seconds'])

    def test_replay_never_promotes_even_when_arithmetic_met(self):
        spec = specification('replay'); outcome = settle_trial(spec, measurements(spec))
        self.assertEqual(outcome['evidence'], 'met')
        self.assertEqual(outcome['version_action'], 'unchanged')
        self.assertFalse(outcome['eligible_for_next_use'])

    def test_restoration_is_an_unfinished_responsibility_until_confirmed(self):
        spec = specification(); rows = measurements(spec)[:-1]
        self.assertEqual(settle_trial(spec, rows, candidate_applied=True)['version_action'], 'recovery_required')
        outcome = settle_trial(spec, rows, candidate_applied=True, restoration_confirmed=True)
        self.assertEqual((outcome['evidence'], outcome['version_action']), ('insufficient', 'restore_baseline'))

    def test_scientific_intervention_requires_a_new_study(self):
        spec = specification(); spec['candidate_versions']['scientific_protocol'] = 'science2'
        with self.assertRaises(ValueError): validate_trial_spec(spec)
        spec['intervention_kind'] = 'scientific_protocol'
        spec['candidate_versions']['policy'] = spec['baseline_versions']['policy']
        with self.assertRaises(ValueError): validate_trial_spec(spec)
        spec['new_study_id'] = 'study2'
        validate_trial_spec(spec)
        spec['execution_mode'] = 'role_recall'
        with self.assertRaises(ValueError): validate_trial_spec(spec)


class TrialLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name); self.addCleanup(self.store.close)
        self.store.create_run('run1', {'fixture': True})
        self.engine = TrialEngine(self.store, 'run1'); self.spec = specification()
        self.engine.register('trial1', self.spec, key='register')
        self.engine.start('trial1', key='start')

    def effect(self, eid, receipt, metadata, status='succeeded'):
        return self.store.mutate('run1', 'effect-' + eid, 'fixture_effect',
            lambda tx: tx.insert('effect', eid, {'status': status, 'kind': 'fixture', 'metadata': metadata,
                'actual_costs': receipt.get('actual_costs', {}), 'accounting_unknown': False,
                'result_ref': tx.blob(receipt), 'packet_ref': tx.blob({'fixture': True})}),
            payload={'eid': eid, 'receipt': receipt, 'metadata': metadata, 'status': status})

    def add_measurements(self):
        evidence = self.store.put_blob({'fixture': 'raw observation'})
        for index, row in enumerate(measurements(self.spec, evidence)):
            eid = 'e' + str(index)
            self.effect(eid, {'trial_measurement': row, 'actual_costs': row['costs']}, {k: row[k] for k in ('trial_id', 'pair_id', 'arm', 'run_id')})
            self.engine.record_measurement('trial1', eid, key='measure-' + eid)

    def test_registration_settlement_and_idempotency_preserve_event_history(self):
        self.add_measurements()
        result = self.engine.settle('trial1', key='settle')
        self.assertEqual(result['outcome']['version_action'], 'approve_next')
        self.assertEqual(result, self.engine.settle('trial1', key='settle'))
        self.store.verify_history('run1')
        with self.assertRaises(ValueError): self.engine.settle('trial1', key='again')

    def test_unknown_effect_retains_trial_lock(self):
        self.effect('unknown', {}, {'trial_id': 'trial1'}, status='unknown')
        with self.assertRaisesRegex(ValueError, 'responsibilit'): self.engine.settle('trial1', key='settle')
        with self.assertRaisesRegex(ValueError, 'unsettled'): self.engine.register('trial2', self.spec, key='second')

    def test_succeeded_effect_with_accounting_unknown_boolean_retains_lock(self):
        self.add_measurements()
        self.effect('pending-cost', {'actual_costs': {'tool_calls': 0}}, {'trial_id': 'trial1'})
        self.store.mutate('run1', 'unknown-cost', 'fixture_unknown_accounting',
            lambda tx: tx.update('effect', 'pending-cost', {'accounting_unknown': True}), payload={'effect_id': 'pending-cost'})
        with self.assertRaisesRegex(ValueError, 'responsibilit'):
            self.engine.settle('trial1', key='settle')
        self.assertEqual(self.store.get('run1', 'trial', 'trial1')['status'], 'active')

    def test_reconciled_measurement_cost_does_not_rewrite_original_metrics(self):
        evidence = self.store.put_blob({'fixture': 'raw observation'})
        original_ref = None
        for index, row in enumerate(measurements(self.spec, evidence)):
            eid = 'e' + str(index)
            costs = dict(row['costs'])
            if index == 0:
                row['costs']['wall_seconds'] = None
                costs.pop('wall_seconds')
            effect = self.effect(eid, {'trial_measurement': row, 'actual_costs': costs},
                {key: row[key] for key in ('trial_id', 'pair_id', 'arm', 'run_id')})
            if index == 0:
                original_ref = effect['result_ref']
                self.store.mutate('run1', 'mark-cost-unknown', 'fixture_unknown_accounting',
                    lambda tx: tx.update('effect', eid, {'accounting_unknown': True}), payload={'effect_id': eid})
            self.engine.record_measurement('trial1', eid, key='measure-' + eid)
        with self.assertRaisesRegex(ValueError, 'responsibilit'):
            self.engine.settle('trial1', key='settle-pending')
        final_costs = measurements(self.spec, evidence)[0]['costs']
        self.store.mutate('run1', 'reconcile-cost', 'fixture_controller_cost_reconciliation',
            lambda tx: tx.update('effect', 'e0', {'actual_costs': final_costs, 'accounting_unknown': False,
                'receipt_ref': tx.blob({'actual_costs': {'wall_seconds': final_costs['wall_seconds']}})}),
            payload={'costs': final_costs})
        outcome = self.engine.settle('trial1', key='settle-known')['outcome']
        self.assertEqual((outcome['execution'], outcome['version_action']), ('valid', 'approve_next'))
        self.assertEqual(outcome['known_costs']['wall_seconds'], 4 * final_costs['wall_seconds'])
        self.assertEqual(self.store.get('run1', 'effect', 'e0')['result_ref'], original_ref)
        self.assertIsNone(json.loads(self.store.read_blob(original_ref))['trial_measurement']['costs']['wall_seconds'])

    def test_auxiliary_reconciliation_uses_ledger_not_initial_result_receipt(self):
        self.add_measurements()
        self.effect('investigation', {'actual_costs': {'tool_calls': 0}}, {'trial_id': 'trial1'})
        self.store.mutate('run1', 'reconciled-investigation', 'fixture_controller_cost_reconciliation',
            lambda tx: tx.update('effect', 'investigation', {'actual_costs': {'tool_calls': 0, 'wall_seconds': 5},
                'accounting_unknown': False, 'receipt_ref': tx.blob({'actual_costs': {'wall_seconds': 5}})}), payload={'wall_seconds': 5})
        outcome = self.engine.settle('trial1', key='settle')['outcome']
        expected = sum(row['costs']['wall_seconds'] for row in measurements(self.spec)) + 5
        self.assertEqual(outcome['execution'], 'valid')
        self.assertEqual(outcome['known_costs']['wall_seconds'], expected)

    def test_fake_summary_cannot_replace_an_effect_receipt(self):
        row = measurements(self.spec, self.store.put_blob({'fixture': True}))[0]
        self.effect('bad', {'trial_measurement': row, 'actual_costs': {'tool_calls': 0, 'wall_seconds': 0}},
                    {k: row[k] for k in ('trial_id', 'pair_id', 'arm', 'run_id')})
        with self.assertRaisesRegex(ValueError, 'costs'): self.engine.record_measurement('trial1', 'bad', key='measure')

    def test_restoration_receipt_is_required_before_releasing_lock(self):
        self.effect('apply', {'version_application': {'trial_id': 'trial1', 'candidate_versions': self.spec['candidate_versions']}}, {'trial_id': 'trial1'})
        self.engine.mark_candidate_applied('trial1', 'apply', key='apply')
        result = self.engine.settle('trial1', key='settle')
        self.assertEqual(result['status'], 'restoring')
        self.effect('restore', {'version_restoration': {'trial_id': 'trial1', 'baseline_versions': self.spec['baseline_versions']}},
                    {'trial_id': 'trial1', 'trial_operation': 'restore_baseline', 'baseline_versions': self.spec['baseline_versions']})
        self.engine.confirm_restoration('trial1', 'restore', key='restore')
        result = self.engine.settle('trial1', key='settle-after-restore')
        self.assertEqual((result['status'], result['outcome']['version_action']), ('settled', 'restore_baseline'))

    def test_auxiliary_trial_costs_cannot_disappear_from_settlement(self):
        self.add_measurements()
        self.effect('investigation', {'actual_costs': {'tool_calls': 1, 'wall_seconds': 1}}, {'trial_id': 'trial1'})
        outcome = self.engine.settle('trial1', key='settle')['outcome']
        self.assertEqual(outcome['execution'], 'invalid')
        self.assertEqual(outcome['known_costs']['tool_calls'], 5)
        self.assertEqual(outcome['additional_effect_count'], 1)

    def test_missing_auxiliary_costs_are_unknown_never_zero(self):
        self.add_measurements()
        self.effect('investigation', {'actual_costs': {'tool_calls': 0}}, {'trial_id': 'trial1'})
        outcome = self.engine.settle('trial1', key='settle')['outcome']
        self.assertEqual(outcome['execution'], 'incomplete')
        self.assertEqual(outcome['unknown_cost_counts']['wall_seconds'], 1)

    def test_unbound_restoration_receipt_cannot_release_lock(self):
        self.effect('apply', {'version_application': {'trial_id': 'trial1', 'candidate_versions': self.spec['candidate_versions']}}, {'trial_id': 'trial1'})
        self.engine.mark_candidate_applied('trial1', 'apply', key='apply')
        self.engine.settle('trial1', key='settle')
        self.effect('unbound', {'version_restoration': {'trial_id': 'trial1', 'baseline_versions': self.spec['baseline_versions']}}, {'trial_id': 'trial2'})
        with self.assertRaisesRegex(ValueError, 'Restoration'):
            self.engine.confirm_restoration('trial1', 'unbound', key='restore')


class ControllerRecoveryIntegrationTests(unittest.TestCase):
    def test_real_version_marker_restoration_uses_reserved_settlement_path(self):
        # A harmless policy marker exercises actual local apply/restore Effects;
        # this does not purport to execute a scientific intervention.
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path
            store = Store(Path(directory) / 'ledger')
            self.addCleanup(store.close)
            controller = Controller(store)
            controller.initialize('run1', budgets={
                'tool_calls': {'limit': 10, 'hard': True, 'settlement_reserve': 1},
                'wall_seconds': {'limit': 100, 'hard': False, 'settlement_reserve': 1}})
            engine = TrialEngine(store, 'run1')
            spec = specification()
            spec['budget']['tool_calls'] = 6
            spec['settlement_reserve']['tool_calls'] = 1
            engine.register('trial1', spec, key='register')
            engine.start('trial1', key='start')
            marker = Path(directory) / 'policy-version.txt'
            marker.write_text(spec['baseline_versions']['policy'])
            work = controller.enqueue('run1', 'lab', 'apply-fixture', 'enqueue-apply')
            pair = spec['pairs'][0]
            effect = controller.prepare_effect('run1', work['id'], 'local_marker_fixture', {'version': 'candidate'},
                {'tool_calls': 1, 'wall_seconds': 1}, 'reserve-apply', metadata={'trial_id': 'trial1',
                    'pair_id': pair['pair_id'], 'arm': 'candidate', 'run_id': pair['candidate_run_id']})
            def apply(effect, packet):
                marker.write_text(spec['candidate_versions']['policy'])
                return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1, 'wall_seconds': .001},
                    'version_application': {'trial_id': 'trial1', 'candidate_versions': spec['candidate_versions']}}
            controller.dispatch_effect('run1', effect['id'], apply)
            engine.mark_candidate_applied('trial1', effect['id'], key='applied')
            self.assertEqual(engine.settle('trial1', key='settle')['status'], 'restoring')
            work = controller.enqueue('run1', 'trial_settle', 'restore-fixture', 'enqueue-restore')
            effect = controller.prepare_effect('run1', work['id'], 'local_marker_restore', {'version': 'baseline'},
                {'tool_calls': 1, 'wall_seconds': 1}, 'reserve-restore', metadata={'trial_id': 'trial1',
                    'trial_operation': 'restore_baseline', 'baseline_versions': spec['baseline_versions']})
            def restore(effect, packet):
                marker.write_text(spec['baseline_versions']['policy'])
                return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1, 'wall_seconds': .001},
                    'version_restoration': {'trial_id': 'trial1', 'baseline_versions': spec['baseline_versions']}}
            controller.dispatch_effect('run1', effect['id'], restore)
            engine.confirm_restoration('trial1', effect['id'], key='restored')
            result = engine.settle('trial1', key='settle-restored')
            self.assertEqual(result['outcome']['version_action'], 'restore_baseline')
            self.assertEqual(result['outcome']['known_costs']['tool_calls'], 2)
            self.assertEqual(result['outcome']['recovery_effect_count'], 1)
            self.assertEqual(marker.read_text(), spec['baseline_versions']['policy'])
            store.verify_history('run1')


if __name__ == '__main__':
    unittest.main()
