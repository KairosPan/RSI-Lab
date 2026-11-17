"""A checkpoint segment may finish without finishing the original experiment."""
import copy
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from experiments.pablo import run as host
from experiments.pablo.tests import test_host_preflight as fixtures
from rsi_lab.store import Store, digest


class CheckpointTests(unittest.TestCase):
    def parent(self):
        return {'config': {'pablo_max_new_evaluations': 400},
                'progress': {'initial_count': 1300, 'candidates': ['fixture'] * 1387},
                'boundary': {'status': 'paused'}}

    def test_paused_87_plus_113_targets_200_of_400(self):
        parent = self.parent()
        before = copy.deepcopy(parent)
        value = host.continuation_checkpoint(parent, 113)
        self.assertEqual(value['baseline_observations'], 1300)
        self.assertEqual(value['completed_before_segment'], 87)
        self.assertEqual(value['checkpoint_new_evaluations'], 200)
        self.assertEqual(value['target_new_evaluations'], 400)
        self.assertEqual(parent, before)

    def test_later_segment_retains_the_original_target(self):
        parent = self.parent()
        parent['config']['research_campaign'] = host.continuation_checkpoint(parent, 113)
        parent['progress']['candidates'] = ['fixture'] * 1500
        parent['boundary']['status'] = 'completed'
        value = host.continuation_checkpoint(parent, 200)
        self.assertEqual(value['completed_before_segment'], 200)
        self.assertEqual(value['checkpoint_new_evaluations'], 400)
        with self.assertRaises(ValueError):
            host.continuation_checkpoint(parent, 201)

    def completed_campaign(self):
        parent = self.parent()
        parent['config']['research_campaign'] = host.continuation_checkpoint(parent, 113)
        parent['progress'].update(candidates=['fixture'] * 1700, remaining_evaluations=0)
        parent['boundary']['status'] = 'completed'
        return parent

    def test_completed_campaign_requires_explicit_new_target_and_preserves_parent(self):
        parent = self.completed_campaign()
        before = copy.deepcopy(parent)
        with self.assertRaisesRegex(ValueError, 'outstanding research target'):
            host.continuation_checkpoint(parent, 400)
        value = host.continuation_checkpoint(parent, 400, new_campaign=True)
        self.assertEqual((value['baseline_observations'], value['target_new_evaluations'],
                          value['completed_before_segment'], value['checkpoint_new_evaluations']),
                         (1700, 400, 0, 400))
        self.assertEqual(value['start_mode'], 'new_campaign')
        self.assertEqual(value['parent_campaign_sha256'], digest(parent['config']['research_campaign']))
        self.assertEqual(parent, before)

    def test_new_campaign_cannot_skip_an_unfinished_target_or_paused_boundary(self):
        for count, status, remaining in ((1500, 'completed', 0), (1700, 'paused', 0),
                                          (1700, 'completed', 1), (1701, 'completed', 0),
                                          (1700, 'unknown', 0), (1700, 'completed', False)):
            parent = self.completed_campaign()
            parent['progress'].update(candidates=['fixture'] * count, remaining_evaluations=remaining)
            parent['boundary']['status'] = status
            with self.subTest(count=count, status=status, remaining=remaining), \
                    self.assertRaisesRegex(ValueError, 'completed research campaign'):
                host.continuation_checkpoint(parent, 400, new_campaign=True)

    def test_new_campaign_rejects_missing_or_invalid_old_target_and_new_counts(self):
        for campaign in (None, [], {}, {'baseline_observations': True, 'target_new_evaluations': 400},
                          {'baseline_observations': 1700, 'target_new_evaluations': 0}):
            parent = self.completed_campaign()
            parent['config']['research_campaign'] = campaign
            with self.subTest(campaign=campaign), self.assertRaises(ValueError):
                host.continuation_checkpoint(parent, 400, new_campaign=True)
        for count in (0, -1, True, 1.5, '400'):
            with self.subTest(new_evaluations=count), self.assertRaises(ValueError):
                host.continuation_checkpoint(self.completed_campaign(), count, new_campaign=True)
        for flag in (None, 1, 'yes'):
            with self.subTest(new_campaign=flag), self.assertRaisesRegex(ValueError, 'explicit boolean'):
                host.continuation_checkpoint(self.completed_campaign(), 400, new_campaign=flag)

    def test_new_campaign_retains_parent_actual_resource_balance(self):
        parent = self.budget_parent(completed=True)
        parent['progress'].update(candidates=['fixture'] * 1700, remaining_evaluations=0)
        host.continuation_checkpoint(parent, 400, new_campaign=True)
        budgets = host.continuation_budgets(parent, 467)
        self.assertEqual(budgets['model_calls']['limit'], 467)
        self.assertEqual(budgets['tokens']['limit'], 67321278)
        provenance = host.resource_budget_provenance(parent, budgets, host.resource_additions(), 24)
        self.assertEqual(provenance['allocation_mode'], 'carry_research_campaign')
        self.assertEqual(provenance['resources']['model_calls']['parent_spent_applied'], 133)

    def test_paused_continuation_preserves_remaining_resource_budget(self):
        parent = self.parent()
        parent['config']['budgets'] = {'model_calls': {'limit': 600, 'hard': True},
                                      'tokens': {'limit': 100000000, 'hard': False}}
        parent['budgets'] = [{'id': 'model_calls', 'spent': 133, 'reserved': 0},
                             {'id': 'tokens', 'spent': 32678722, 'reserved': 0}]
        before = copy.deepcopy(parent)
        result = host.continuation_budgets(parent, 467)
        self.assertEqual(result['model_calls']['limit'], 467)
        self.assertEqual(result['tokens']['limit'], 67321278)
        self.assertEqual(parent, before)
        with self.assertRaises(ValueError):
            host.continuation_budgets(parent, 468)
        parent['budgets'][0]['reserved'] = 1
        with self.assertRaises(ValueError):
            host.continuation_budgets(parent, 467)

    def test_new_campaign_starts_from_completed_legacy_observations(self):
        parent = self.parent()
        parent.pop('boundary')
        parent['progress']['candidates'] = ['fixture'] * 1300
        value = host.continuation_checkpoint(parent, 400)
        self.assertEqual(value['baseline_observations'], 1300)
        self.assertEqual(value['completed_before_segment'], 0)
        self.assertEqual(value['checkpoint_new_evaluations'], 400)

    def budget_parent(self, *, completed=False):
        parent = self.parent()
        parent.update(run_id='parent', root='/frozen/parent')
        parent['config']['budgets'] = {'model_calls': {'limit': 600, 'hard': True},
                                      'tokens': {'limit': 100000000, 'hard': False},
                                      'tool_calls': {'limit': 1000, 'hard': True}}
        parent['budgets'] = [{'id': 'model_calls', 'spent': 133, 'reserved': 0},
                             {'id': 'tokens', 'spent': 32678722, 'reserved': 0},
                             {'id': 'tool_calls', 'spent': 150, 'reserved': 0}]
        if completed:
            parent['config']['research_campaign'] = host.continuation_checkpoint(parent, 113)
            parent['boundary']['status'] = 'completed'
        return parent

    def test_explicit_extensions_add_to_remaining_budget_without_refunding_spend(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                parent = self.budget_parent(completed=completed)
                before = copy.deepcopy(parent)
                result = host.continuation_budgets(parent, 667, additional_model_calls=200,
                    additional_tokens=50000000, additional_tool_calls=500)
                self.assertEqual(result['model_calls']['limit'], 667)
                self.assertEqual(result['tokens']['limit'], 117321278)
                self.assertEqual(result['tool_calls']['limit'], 1350)
                self.assertEqual(parent, before)
                with self.assertRaisesRegex(ValueError, 'plus explicit addition'):
                    host.continuation_budgets(parent, 668, additional_model_calls=200)

    def test_model_cap_can_leave_authorized_allowance_unused_and_records_it(self):
        parent = self.budget_parent(completed=True)
        additions = host.resource_additions(200, 50000000, 500)
        budgets = host.continuation_budgets(parent, 650, additional_model_calls=200,
                                          additional_tokens=50000000, additional_tool_calls=500)
        provenance = host.resource_budget_provenance(parent, budgets, additions, 64)
        self.assertEqual(provenance['allocation_mode'], 'carry_research_campaign')
        model = provenance['resources']['model_calls']
        self.assertEqual((model['parent_spent'], model['parent_spent_applied'], model['parent_remaining']), (133, 133, 467))
        self.assertEqual((model['additional_allowance'], model['available_after_extension']), (200, 667))
        self.assertEqual((model['segment_limit'], model['unused_available']), (650, 17))
        self.assertEqual(provenance['final_review_max_calls'], 64)

    def test_soft_resource_overrun_consumes_extension_instead_of_being_forgiven(self):
        parent = self.budget_parent()
        parent['budgets'][1]['spent'] = 102000000
        result = host.continuation_budgets(parent, 467, additional_tokens=50000000)
        self.assertEqual(result['tokens']['limit'], 48000000)
        details = host.resource_budget_provenance(parent, result, host.resource_additions(0, 50000000, 0), 24)
        self.assertEqual(details['resources']['tokens']['parent_overrun'], 2000000)

    def test_completed_legacy_experiment_remains_an_explicit_new_campaign(self):
        parent = self.budget_parent()
        parent.pop('boundary')
        budgets = host.continuation_budgets(parent, 600)
        self.assertEqual(budgets['tokens']['limit'], 100000000)
        details = host.resource_budget_provenance(parent, budgets, host.resource_additions(), 24)
        self.assertEqual(details['allocation_mode'], 'new_legacy_campaign')
        self.assertEqual(details['resources']['tokens']['parent_spent'], 32678722)
        self.assertEqual(details['resources']['tokens']['parent_spent_applied'], 0)

    def test_additions_are_explicit_bounded_integers(self):
        for name, upper in (('additional_model_calls', 1000), ('additional_tokens', 1000000000),
                            ('additional_tool_calls', 10000)):
            for invalid in (-1, upper + 1, True, 1.5, '200', None):
                with self.subTest(name=name, invalid=invalid), self.assertRaises(ValueError):
                    host.continuation_budgets(self.budget_parent(), 467, **{name: invalid})
        parent = self.budget_parent()
        parent['budgets'][0]['reserved'] = 1
        with self.assertRaisesRegex(ValueError, 'settled resource reservations'):
            host.continuation_budgets(parent, 667, additional_model_calls=200)

    def test_cli_defaults_and_explicit_resume_overrides(self):
        base = ['pablo-host', '--root', '/unused/run', '--run-id', 'child', '--prepare-only']
        with patch('sys.argv', base + ['--parent-root', '/unused/parent']), patch.object(host, 'prepare') as prepare:
            self.assertEqual(host.main(), 0)
        args = prepare.call_args.args[0]
        self.assertEqual((args.additional_model_calls, args.additional_tokens, args.additional_tool_calls), (0, 0, 0))
        self.assertEqual(args.final_review_max_calls, 24)
        self.assertFalse(args.new_campaign)
        for option, value in (('--additional-model-calls', '0'), ('--additional-tokens', '50000000'),
                              ('--additional-tool-calls', '500'), ('--final-review-max-calls', '24'),
                              ('--max-model-calls', '600')):
            with self.subTest(option=option), patch('sys.argv', base + ['--resume', option, value]), \
                    patch.object(host, 'prepare') as prepare, patch.object(host, 'drive') as drive, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                host.main()
            self.assertEqual(stopped.exception.code, 2)
            prepare.assert_not_called(); drive.assert_not_called()

    def test_cli_new_campaign_is_forwarded_only_for_fresh_preparation(self):
        base = ['pablo-host', '--root', '/unused/run', '--run-id', 'child', '--prepare-only', '--new-campaign']
        with patch('sys.argv', base + ['--parent-root', '/unused/parent']), patch.object(host, 'prepare') as prepare:
            self.assertEqual(host.main(), 0)
        self.assertTrue(prepare.call_args.args[0].new_campaign)
        with patch('sys.argv', base + ['--resume']), patch.object(host, 'prepare') as prepare, \
                patch.object(host, 'drive') as drive, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as stopped:
            host.main()
        self.assertEqual(stopped.exception.code, 2)
        prepare.assert_not_called(); drive.assert_not_called()

    def test_cli_rejects_invalid_extensions_and_final_review_bounds(self):
        base = ['pablo-host', '--root', '/unused/run', '--run-id', 'child', '--parent-root', '/unused/parent', '--prepare-only']
        for option, value in (('--additional-model-calls', '-1'), ('--additional-tokens', '1000000001'),
                              ('--additional-tool-calls', '10001'), ('--final-review-max-calls', '0'),
                              ('--final-review-max-calls', '129')):
            with self.subTest(option=option, value=value), patch('sys.argv', base + [option, value]), \
                    patch.object(host, 'prepare') as prepare, contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as stopped:
                host.main()
            self.assertEqual(stopped.exception.code, 2)
            prepare.assert_not_called()


class PreparedBudgetTests(unittest.TestCase):
    setUp = fixtures.HostPreflightTests.setUp
    open_run = fixtures.HostPreflightTests.open_run
    prepared_child = fixtures.HostPreflightTests.prepared_child
    assert_no_execution = fixtures.HostPreflightTests.assert_no_execution

    def test_new_campaign_freezes_parent_target_lineage_and_actual_new_quota(self):
        def complete(parent):
            parent['config']['research_campaign'] = {
                'baseline_observations': 0, 'target_new_evaluations': 1,
                'completed_before_segment': 0, 'checkpoint_new_evaluations': 1,
                'segment_new_evaluations': 1}
            parent['boundary'] = {'status': 'completed'}
            parent['progress']['remaining_evaluations'] = 0
        args = self.prepared_child(new_campaign=True, parent_transform=complete)
        store = Store(args.root)
        self.addCleanup(store.close)
        config = store.run('child')['config']
        manifest = json.loads((args.root / 'manifest.json').read_text())
        lineage = json.loads((args.root / 'lineage.json').read_text())
        plan = json.loads((args.root / 'task-plan.json').read_text())
        campaign = config['research_campaign']
        self.assertEqual((campaign['baseline_observations'], campaign['target_new_evaluations'],
                          campaign['completed_before_segment']), (1, 2, 0))
        self.assertEqual(campaign, manifest['research_campaign'])
        self.assertEqual(campaign, lineage['research_campaign'])
        self.assertEqual(campaign, plan['protocol']['parameters']['research_campaign'])
        self.assertEqual(campaign['parent_campaign_sha256'], digest(lineage['parent_research_campaign']))
        self.assertEqual(lineage['cost_segments'][-1]['run_id'], 'parent')
        self.assertEqual(lineage['resource_budget_provenance']['allocation_mode'], 'carry_research_campaign')
        self.assertEqual(plan['protocol']['parameters']['target_total_observations'], 3)
        self.assertEqual(config['pablo_max_new_evaluations'], 2)
        self.assertIn('explicitly requested new campaign starts from 1 inherited observations', config['goal'])
        self.assert_no_execution(store, 'child')

    def test_extended_budget_and_review_limit_are_frozen_in_all_provenance_and_task_plan(self):
        args = self.prepared_child(max_model_calls=224, additional_model_calls=200,
                                  additional_tokens=50000000, additional_tool_calls=500,
                                  final_review_max_calls=64)
        store = Store(args.root)
        self.addCleanup(store.close)
        config = store.run('child')['config']
        manifest = json.loads((args.root / 'manifest.json').read_text())
        lineage = json.loads((args.root / 'lineage.json').read_text())
        plan = json.loads((args.root / 'task-plan.json').read_text())
        provenance = config['resource_budget_provenance']
        self.assertEqual(provenance, lineage['resource_budget_provenance'])
        self.assertEqual(provenance, manifest['resource_budget_provenance'])
        self.assertEqual(provenance['additional_allowances'],
                         {'model_calls': 200, 'tokens': 50000000, 'tool_calls': 500})
        self.assertEqual(config['final_review_max_calls'], 64)
        self.assertEqual(plan['budget']['new_oracle_evaluations'], args.new_evaluations)
        for resource, policy in config['budgets'].items():
            self.assertEqual(plan['budget'][resource], policy['limit'])
            self.assertEqual(store.get('child', 'budget', resource)['limit'], policy['limit'])
            self.assertEqual(provenance['resources'][resource]['segment_limit'], policy['limit'])
        self.assertEqual(config['budgets']['tool_calls']['limit'], 540)
        self.assert_no_execution(store, 'child')
