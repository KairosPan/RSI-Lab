"""Native RSI control/trial mechanisms and package isolation.

The executable here is a numerical fixture. No domain package is needed to
reserve calls, run paired branches, certify measurements or settle a trial.
"""
import ast
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab.branches import BranchExecutor, branch_reservations, make_branch_packet
from rsi_lab.capabilities import digest
from rsi_lab.controller import Controller
from rsi_lab.store import Store
from rsi_lab.trials import TrialEngine, validate_trial_spec


ADAPTER = '''import hashlib,json,sys
p=json.load(sys.stdin)
assert p["schema_version"]=="rsi_trial_input_v2"
x=p["input"]
cost=5 if p["identity"]["arm"]=="candidate" else 10
r={"schema_version":"rsi_trial_output_v2","identity":p["identity"],
   "metrics":{"operation_count":cost},"execution_status":"complete",
   "input_receipt_sha256":hashlib.sha256(json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()}
json.dump(r,sys.stdout)
'''


def specification():
    return {'schema_version':'rsi_trial_v2','intervention_kind':'policy',
        'baseline_versions':{'policy':'sum_loop_v1','capability':'integer_sum_v1','scientific_protocol':'exact_integer_sum_v1'},
        'candidate_versions':{'policy':'sum_cached_v1','capability':'integer_sum_v1','scientific_protocol':'exact_integer_sum_v1'},
        'study_id':'integer_operations','new_study_id':None,
        'applicability':{'task':'deterministic numerical fixture','branch_execution':{
            'profile':'reviewed_local_python_metrics_v1',
            'artifact_sha256':{'baseline':digest(ADAPTER.encode()),'candidate':digest(ADAPTER.encode())},
            'limits':{'wall_seconds':2,'cpu_seconds':1,'output_bytes':8192}}},
        'execution_mode':'isolated_branch',
        'pairs':[{'pair_id':'input1','input_hash':digest({'values':[1,2,3]}),
                  'baseline_run_id':'loop1','candidate_run_id':'cached1'}],
        'minimum_pairs':1,
        'metrics':[{'name':'operation_count','direction':'lower','role':'primary',
                    'min_gain':2,'max_loss':0,'per_pair_no_regression':True}],
        'release_rule':'all_primary',
        'budget':{'model_calls':0,'tool_calls':3,'tokens':0,'cpu_seconds':3,'wall_seconds':6},
        'settlement_reserve':{'tool_calls':1,'cpu_seconds':1,'wall_seconds':2},
        'recovery_owner':'numeric_experiment_controller'}


class GenericMechanismTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/'ledger');self.addCleanup(self.store.close)
        self.controller=Controller(self.store)
        self.controller.initialize('neutral',budgets={
            k:{'limit':v,'hard':k in {'model_calls','tool_calls'}} for k,v in
            {'model_calls':0,'tool_calls':3,'tokens':0,'cpu_seconds':3,'wall_seconds':6}.items()})

    def test_default_control_and_trial_contracts_are_neutral(self):
        self.assertEqual(self.store.run('neutral')['config']['schema_version'],'rsi_control_v2')
        spec=specification()
        self.assertEqual(validate_trial_spec(spec),spec)
        trial=TrialEngine(self.store,'neutral').register('trial',spec,key='register')
        self.assertEqual(json.loads(self.store.read_blob(trial['spec_ref']))['schema_version'],'rsi_trial_v2')

    def test_native_numeric_branches_execute_and_settle_without_domain_tools(self):
        spec=specification();engine=TrialEngine(self.store,'neutral')
        engine.register('trial',spec,key='register');engine.start('trial',key='start')
        adapter=self.root/'numeric_adapter.py';adapter.write_text(ADAPTER)
        effect_ids=[]
        for arm in ('baseline','candidate'):
            packet=make_branch_packet(self.store,'neutral','trial','input1',arm,{'values':[1,2,3]},adapter,
                execution_authorization_sha256=digest(ADAPTER.encode()))
            self.assertEqual(packet['schema_version'],'rsi_branch_packet_v2')
            work=self.controller.enqueue('neutral','lab','numeric:'+arm,'work:'+arm)
            effect=self.controller.prepare_effect('neutral',work['id'],'numeric_branch',packet,
                branch_reservations(self.store,'neutral','trial'),'reserve:'+arm,
                metadata={k:packet[k] for k in ('trial_id','pair_id','arm','run_id')})
            effect=self.controller.dispatch_effect('neutral',effect['id'],BranchExecutor(self.store,'neutral'))
            self.assertEqual(effect['status'],'succeeded')
            receipt=json.loads(self.store.read_blob(effect['result_ref']))
            self.assertEqual(receipt['trial_execution']['response']['schema_version'],'rsi_trial_output_v2')
            self.assertTrue(receipt['trial_execution']['workspace_removed'])
            engine.record_measurement('trial',effect['id'],key='measurement:'+arm)
            effect_ids.append(effect['id'])
        outcome=engine.settle('trial',key='settle')['outcome']
        self.assertEqual(outcome['execution'],'valid');self.assertEqual(outcome['evidence'],'met')
        self.assertEqual(outcome['version_action'],'approve_next')
        self.assertEqual(outcome['known_costs']['tool_calls'],2)
        self.assertEqual(self.store.get('neutral','budget','tool_calls')['reserved'],0)
        self.store.verify_history('neutral')

    def test_shared_mechanisms_have_no_domain_import_or_rule_dependency(self):
        root=Path(__file__).resolve().parents[2]/'rsi_lab'
        for name in ('store','controller','trials','capabilities','branches'):
            source=(root/(name+'.py')).read_text();tree=ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):
                    target=node.module or ''
                    self.assertFalse(target.startswith(('rsi_domains', 'tests', 'scripts')), (name,target))
                elif isinstance(node,ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name.startswith(('rsi_domains', 'tests', 'scripts')), (name,alias.name))


if __name__=='__main__':unittest.main()
