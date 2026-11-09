"""Task contracts have no fixed domain size, metric direction or replicate IDs."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab import plans
from rsi_lab.store import Store


def fixture(root, *, minimum=2, direction='lower_or_equal'):
    return {'schema_version': plans.SCHEMA, 'task_id': 'tabular1', 'goal': 'Validate site energy totals in kWh',
        'inputs': [{'input_id': 'readings.csv', 'sha256': 'a'*64}],
        'items': [{'item_id': name, 'identity': 'site:' + name} for name in ('east','west','south')],
        'protocol': {'version': 'unit_totals_v1', 'parameters': {'units': 'kWh', 'method': 'exact_decimal_sum'}},
        'capability_versions': {'unit_totals': 'adapter_v1'},
        'metrics': [{'name': 'absolute_error', 'units': 'kWh', 'direction': direction, 'threshold': 0.01,
                     'aggregation': 'max', 'replicates': ['fixture_a', 'fixture_b']}],
        'success': {'metric': 'absolute_error', 'min_qualified_items': minimum},
        'arms': [{'arm_id': arm, 'policy_version': arm+'_v1', 'policy_sha256': plans.digest(arm),
            'shards': [{'shard_id': 'all', 'item_ids': ['east','west','south'],
                'output_path': str(root/arm/'result'), 'adapter_id': 'unit_totals', 'protocol_sha256': 'b'*64}]}
            for arm in ('baseline','candidate')],
        'budget': {'tool_calls': 2, 'wall_seconds': 10}, 'stop_rule': plans.STOP_RULE, 'missing_rule': plans.MISSING_RULE}


def rows(plan, scores=(0.0,0.01,0.02), arm='candidate'):
    return [{'item_id': item['item_id'], 'identity': item['identity'], 'arm_id': arm, 'status':'completed',
        'values': {'absolute_error': {'fixture_a':value, 'fixture_b':value}}, 'evidence_refs':['d'*64]}
        for item,value in zip(plan['items'],scores)]


class TaskPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.store=Store(self.root/'ledger');self.addCleanup(self.store.close)
        self.store.create_run('r',{'execution_mode':'live'});self.plan=fixture(self.root)

    def test_no_fixed_item_count_direction_or_repeat_identity(self):
        plans.validate_task_plan(self.plan)
        result=plans.select_items(self.plan,rows(self.plan),'candidate')
        self.assertEqual((result['status'],result['count']),('accepted',2))
        other=fixture(self.root,minimum=1,direction='higher_or_equal')
        self.assertEqual(plans.select_items(other,rows(other),'candidate')['count'],2)
        other['metrics'][0]['replicates']=['new_repeat']
        plans.validate_task_plan(other)
        with self.assertRaisesRegex(ValueError,'replicate'):
            plans.select_items(other,rows(other),'candidate')

    def test_entire_arm_retained_when_minimum_already_reached(self):
        result=plans.select_items(self.plan,rows(self.plan)[:2],'candidate')
        self.assertEqual((result['status'],result['count']),('incomplete',2))
        self.assertEqual(result['all_observations'][-1]['status'],'unrun')
        failed=rows(self.plan);failed[-1]['status']='failed'
        result=plans.select_items(self.plan,failed,'candidate')
        self.assertEqual((result['status'],result['count']),('accepted',2))
        self.assertEqual(len(result['all_observations']),3)

    def test_cross_arm_identity_and_fabricated_evidence_rejected(self):
        for mutate in (lambda x:x[0].update(arm_id='baseline'),lambda x:x[0].update(identity='different'),
                       lambda x:x[0].update(evidence_refs=[]),lambda x:x.append(deepcopy(x[0]))):
            sample=rows(self.plan);mutate(sample)
            with self.assertRaises(ValueError):plans.select_items(self.plan,sample,'candidate')

    def test_registration_precedes_effect_and_outputs(self):
        Path(self.plan['arms'][0]['shards'][0]['output_path']).mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'precede'):
            plans.register_task_plan(self.store,'r',self.plan,key='register')
        self.assertEqual(self.store.list('r','registration'),[])
        self.plan['arms'][0]['shards'][0]['output_path']=str(self.root/'future')
        self.store.mutate('r','early','fixture',lambda tx:tx.insert('effect','early',{'metadata':{'task_plan_id':'tabular1'}}))
        with self.assertRaisesRegex(ValueError,'cannot follow'):
            plans.register_task_plan(self.store,'r',self.plan,key='register')

    def test_frozen_plan_cannot_change_even_through_legal_projection_mutation(self):
        first=plans.register_task_plan(self.store,'r',self.plan,key='register')
        self.assertEqual(first,plans.register_task_plan(self.store,'r',self.plan,key='register'))
        self.assertEqual(plans.load_task_plan(self.store,'r','tabular1'),self.plan)
        changed=deepcopy(self.plan);changed['success']['min_qualified_items']=1
        with self.assertRaises(ValueError):plans.register_task_plan(self.store,'r',changed,key='register')
        self.store.mutate('r','replace','fixture',lambda tx:tx.update('registration',first['id'],
            {'plan_ref':tx.blob(changed),'plan_hash':plans.digest(changed)}))
        with self.assertRaisesRegex(ValueError,'cannot be replaced'):
            plans.load_task_plan(self.store,'r','tabular1')

    def test_source_partition_and_budget_are_frozen_and_strict(self):
        changes=[lambda p:p['arms'][0]['shards'][0]['item_ids'].pop(),
                 lambda p:p['items'][1].update(identity=p['items'][0]['identity']),
                 lambda p:p['metrics'][0].update(replicates=[['not_an_identity']]),
                 lambda p:p['metrics'][0].update(direction=[]),
                 lambda p:p['success'].update(min_qualified_items=True),
                 lambda p:p['budget'].update(tool_calls=float('nan'))]
        for change in changes:
            plan=deepcopy(self.plan);change(plan)
            with self.assertRaises(ValueError):plans.validate_task_plan(plan)

    def test_missing_measurement_never_counts_as_zero_and_higher_is_supported(self):
        sample=rows(self.plan);sample[0]['values']['absolute_error']['fixture_a']=None
        result=plans.select_items(self.plan,sample,'candidate')
        self.assertEqual(result['status'],'incomplete');self.assertEqual(result['count'],1)
        self.assertIsNone(result['all_observations'][0]['scores']['absolute_error'])
        plan=fixture(self.root,minimum=3,direction='higher_or_equal')
        self.assertEqual(plans.select_items(plan,rows(plan,(0.01,0.1,1)),'candidate')['status'],'accepted')


if __name__=='__main__':unittest.main()
