"""Actual non-docking task execution through the unmodified shared kernel."""
import json
import copy
from pathlib import Path
import tempfile
import unittest

from rsi_lab import runtime as core_runtime
from rsi_lab.controller import Controller
from rsi_lab.plans import load_task_plan
from rsi_lab.store import Store, digest
from rsi_domains.docking.plugin import make_adapter as docking_adapter
from rsi_domains.tabular.demo import CSV_TEXT, UNITS, run_demo, task_plan
from rsi_domains.tabular.plugin import ADAPTER_ID, make_adapter, prepare, source_bytes


class PortabilityTests(unittest.TestCase):
    def test_non_docking_workflow_reuses_core_with_both_domain_packages_loaded(self):
        before={p.name:digest(p.read_bytes()) for p in Path(core_runtime.__file__).parent.glob('*.py')}
        with tempfile.TemporaryDirectory() as directory:
            report=run_demo(directory,additional_adapters=[docking_adapter()])
            self.assertEqual(report['status'],'completed')
            self.assertEqual(set(report['loaded_adapters']),{'docking.prerequisites',ADAPTER_ID})
            self.assertEqual(report['issue_status'],'resolved')
            self.assertEqual(report['request_wait'],'fired')
            self.assertTrue(report['wait_restart_exercised'])
            self.assertTrue(report['receipt_publication_restart_exercised'])
            self.assertEqual(report['final_aggregation_execution_count'],1)
            self.assertTrue(report['distinct_branch_workspaces'])
            self.assertEqual(report['aggregation']['record_ids'],['r1','r2','r3','r4'])
            self.assertEqual({r['group']:r['energy_kwh'] for r in report['aggregation']['groups']},
                             {'North':3.5,'South':2.0})
            self.assertGreater(report['branch_metrics'][0]['max_absolute_error_kwh'],0)
            self.assertEqual(report['branch_metrics'][1]['max_absolute_error_kwh'],0)
            self.assertEqual(report['trial_outcome']['execution'],'valid')
            self.assertEqual(report['trial_outcome']['evidence'],'met')
            self.assertEqual(report['trial_outcome']['version_action'],'approve_next')
            self.assertEqual(report['task_selection']['status'],'accepted')
            self.assertEqual(report['task_selection']['count'],2)
            self.assertEqual(report['costs']['model_calls']['spent'],0)
            self.assertEqual(report['costs']['tool_calls']['spent'],7)
            self.assertTrue(all(r['reserved']==0 for r in report['costs'].values()))
            store=Store(directory)
            try:
                plan=load_task_plan(store,'tabular-portability','tabular-energy-task')
                self.assertEqual(plan['success']['min_qualified_items'],2)
                self.assertEqual(plan['metrics'][0]['units'],'kWh')
                self.assertEqual(plan['metrics'][0]['replicates'],['aggregation'])
                effect=store.list('tabular-portability','effect',kind='tabular_aggregation')[0]
                self.assertEqual(effect['metadata']['task_plan_sha256'],digest(plan))
                self.assertEqual(effect['metadata']['adapter_id'],ADAPTER_ID)
                self.assertEqual(len(store.list('tabular-portability','effect',kind='docking_prerequisite_inspection')),0)
                registration=store.get('tabular-portability','capability',report['capability_registration'])
                acceptance_effect=store.get('tabular-portability','effect',registration['effect_id'])
                receipt=json.loads(store.read_blob(acceptance_effect['result_ref']))
                self.assertTrue(receipt['capability_acceptance']['accepted'])
                self.assertEqual(len(receipt['capability_acceptance']['cases']),4)
                self.assertTrue(all(case['passed'] for case in receipt['capability_acceptance']['cases']))
                store.verify_history('tabular-portability')
            finally: store.close()
            again=run_demo(directory,additional_adapters=[docking_adapter()])
            self.assertEqual(again,report)
        after={p.name:digest(p.read_bytes()) for p in Path(core_runtime.__file__).parent.glob('*.py')}
        self.assertEqual(before,after)
        self.assertFalse(hasattr(core_runtime.Runtime,'inspect_docking'))

    def test_domain_capabilities_do_not_leak_between_registries(self):
        from rsi_lab.adapters import AdapterRegistry
        first=AdapterRegistry([docking_adapter()])
        second=AdapterRegistry([make_adapter()])
        mixed=AdapterRegistry([docking_adapter(),make_adapter()])
        self.assertEqual(first.get('docking.prerequisites').adapter_id,'docking.prerequisites')
        with self.assertRaises(ValueError): first.get(ADAPTER_ID)
        with self.assertRaises(ValueError): second.get('docking.prerequisites')
        self.assertEqual(mixed.get(ADAPTER_ID).descriptor(),second.get(ADAPTER_ID).descriptor())
        self.assertEqual(mixed.get('docking.prerequisites').descriptor(),first.get('docking.prerequisites').descriptor())

    def test_changed_dataset_cannot_borrow_registered_task_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset={'dataset_id':'energy-001','csv_text':CSV_TEXT,'units':UNITS}
            artifact=digest(source_bytes());plan=task_plan(Path(directory),dataset,artifact)
            inputs={'dataset':dataset,'capability_artifact_sha256':artifact,'task_plan_ref':digest(plan),'task_plan':plan}
            self.assertEqual(prepare(inputs)['input'],dataset)
            changed={**dataset,'units':{**UNITS,'r1':'kWh'}}
            with self.assertRaisesRegex(ValueError,'frozen TaskPlan'):
                prepare({**inputs,'dataset':changed})
            with self.assertRaisesRegex(ValueError,'accepted capability'):
                prepare({**inputs,'capability_artifact_sha256':'0'*64})
            unsupported=copy.deepcopy(plan)
            unsupported['protocol']['parameters']['unit_factors']['Wh']=.01
            with self.assertRaisesRegex(ValueError,'does not support'):
                prepare({**inputs,'task_plan':unsupported,'task_plan_ref':digest(unsupported)})

    def test_loading_domain_code_does_not_bypass_executable_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(directory)
            try:
                Controller(store).initialize('run',budgets={'tool_calls':{'limit':1,'hard':True}})
                runtime=core_runtime.Runtime(store,'run',adapters=[make_adapter(),docking_adapter()])
                self.assertNotIn(ADAPTER_ID,runtime.capability_catalog())
                self.assertIn('docking.prerequisites',runtime.capability_catalog())
                with self.assertRaisesRegex(ValueError,'verified artifact acceptance'):
                    runtime.execute_adapter(ADAPTER_ID,{},trigger_id='not-yet-accepted')
                self.assertEqual(store.list('run','effect'),[])
                self.assertEqual(store.get('run','budget','tool_calls')['spent'],0)
            finally: store.close()


if __name__ == '__main__': unittest.main()
