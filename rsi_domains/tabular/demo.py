"""Non-docking portability acceptance using actual CSV calculations.

Meta decisions are explicit fixtures. Capability tests, paired branches and
the final aggregation really execute. This validates shared orchestration and
domain replacement, not autonomous model judgment or general algorithm gains.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from rsi_lab import input_view
from rsi_lab.branches import BranchExecutor, branch_reservations, make_branch_packet
from rsi_lab.controller import Controller
from rsi_lab.plans import load_task_plan, register_task_plan, select_items
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode
from rsi_lab.trials import TrialEngine
from .plugin import ADAPTER_ID, PROTOCOL_PARAMETERS, PROTOCOL_VERSION, make_adapter, source_bytes

CSV_TEXT = 'record_id,group,energy\nr1,North,1500\nr2,North,2\nr3,South,750\nr4,South,1.25\n'
UNITS = {'r1':'Wh','r2':'kWh','r3':'Wh','r4':'kWh'}
EXPECTED_TOTALS = {'North':3.5,'South':2.0}
PROTOCOL = PROTOCOL_VERSION


class ScriptedMeta:
    def __init__(self, action, payload): self.action, self.payload = action, payload
    def __call__(self, effect, raw):
        started = time.monotonic()
        if effect['status'] != 'dispatching' or digest(raw) != effect['packet_ref']:
            raise ValueError('Fixture response still requires its exact persisted invocation')
        envelope=json.loads(raw)
        data=input_view.decode(envelope['prompt'].split('INPUT_DATA_JSON:\n',1)[1],envelope['input_view_mode'])
        return {'status':'succeeded','actual_costs':{'model_calls':0,'tokens':0,'wall_seconds':time.monotonic()-started},
            'sent_packet_sha256':digest(raw),'sent_prompt_sha256':digest(envelope['prompt'].encode()),
            'synthetic':True,'source':'explicit tabular portability fixture',
            'response':{'command':{'mode':envelope['mode'],'action':self.action,
                'issue_id':data['task']['issue_id'],'expected_versions':data['task']['expected_versions'],
                'evidence_ids':list(dict.fromkeys(p['evidence_id'] for p in data['delivered_pages'])),
                'payload':self.payload}}}


def capability_spec():
    good={'dataset_id':'acceptance-data','csv_text':'record_id,group,energy\na,West,500\nb,West,1\n',
          'units':{'a':'Wh','b':'kWh'}}
    expected={'dataset_id':'acceptance-data','record_ids':['a','b'],'row_count':2,
              'groups':[{'group':'West','row_count':2,'energy_kwh':1.5}],
              'row_receipts':[{'record_id':'a','group':'West','unit':'Wh','energy_kwh':.5},
                              {'record_id':'b','group':'West','unit':'kWh','energy_kwh':1.0}],
              'output_unit':'kWh'}
    bads=[('missing-units',{**good,'units':{}},'missing_units'),
          ('unknown-unit',{**good,'units':{'a':'joules','b':'kWh'}},'unsupported_unit'),
          ('duplicate-identity',{**good,'csv_text':'record_id,group,energy\na,West,500\na,West,1\n'},'duplicate_record_id')]
    return {'schema_version':'capability_request_v2','capability_id':ADAPTER_ID,
        'purpose':'Aggregate CSV energy measurements only after explicit unit and record identity validation',
        'scope':{'domain':'tabular_energy_measurements','units':['Wh','kWh'],'maximum_records':1000},
        'input_contract':{'dataset_id':'string','csv_text':'record_id,group,energy','units':'complete per-record unit map'},
        'output_contract':{'groups':'exact kWh totals','record_ids':'preserved input identity list'},
        'scientific_impact':True,'baseline_scientific_protocol':'csv_unit_unknown_v1',
        'candidate_scientific_protocol':PROTOCOL,'new_study_id':'tabular-energy-task',
        'execution_profile':'reviewed_local_python_json_v1',
        'recovery':{'query_supported':False,'idempotency_supported':False,'unknown_result':'blocked_external'},
        'limits':{'wall_seconds_per_case':3,'cpu_seconds_per_case':1,'output_bytes_per_case':16384},
        'cases':[{'case_id':'mixed-units','kind':'normal','input':good,'expected_output':expected,
                  'expected_identity':'acceptance-data','expected_error_code':None}]+
                [{'case_id':name,'kind':'failure','input':value,'expected_output':None,
                  'expected_identity':None,'expected_error_code':code} for name,value,code in bads]}


def materialize_capability(root):
    root.mkdir(parents=True,exist_ok=True)
    path=root/'aggregate_energy.py';path.write_bytes(source_bytes());artifact=digest(path.read_bytes())
    spec=capability_spec()
    manifest={'schema_version':'capability_delivery_v2','capability_id':ADAPTER_ID,'capability_version':'1.0',
        'artifact_sha256':artifact,'scope':spec['scope'],'input_contract':spec['input_contract'],
        'output_contract':spec['output_contract'],'packages':{},
        'engineering_provenance':{'source':'reviewed standalone tabular adapter','reviewed_source_sha256':artifact}}
    return spec,path,manifest,artifact


def task_plan(root, dataset, artifact):
    return {'schema_version':'rsi_task_plan_v1','task_id':'tabular-energy-task',
        'goal':'Compute group energy totals in kWh without guessing units or losing record identities',
        'inputs':[{'input_id':'dataset','sha256':digest(dataset)}],
        'items':[{'item_id':group,'identity':'site:'+group} for group in ['North','South']],
        'protocol':{'version':PROTOCOL,'parameters':json.loads(encode(PROTOCOL_PARAMETERS))},
        'capability_versions':{ADAPTER_ID:'1.0'},
        'metrics':[{'name':'absolute_error_kwh','units':'kWh','direction':'lower_or_equal','threshold':0,
                    'aggregation':'max','replicates':['aggregation']}],
        'success':{'metric':'absolute_error_kwh','min_qualified_items':2},
        'arms':[{'arm_id':'validated','policy_version':'explicit-metadata','policy_sha256':digest('explicit-metadata'),
                 'shards':[{'shard_id':'all-sites','item_ids':['North','South'],
                            'output_path':str((root/'group_totals.json').resolve()),'adapter_id':ADAPTER_ID,
                            'protocol_sha256':digest({'protocol':PROTOCOL,'artifact':artifact})}]}],
        'budget':{'tool_calls':1,'cpu_seconds':1,'wall_seconds':3},
        'stop_rule':'all_planned_shards_terminal_or_explicit_stop',
        'missing_rule':'retain_all_missing_no_imputation_no_replacement'}


def trial_spec(directory):
    candidate=directory/'aggregate_energy.py'
    baseline=directory/'raw_sum.py'
    baseline.write_bytes(candidate.read_bytes().replace(b"ALGORITHM = 'convert_units'",b"ALGORITHM = 'raw_sum'"))
    value={'dataset':{'dataset_id':'heldout-validation','csv_text':'record_id,group,energy\nv1,West,500\nv2,West,1\nv3,East,2\n',
                       'units':{'v1':'Wh','v2':'kWh','v3':'kWh'}},
           'expected_totals_kwh':{'West':1.5,'East':2.0},'expected_record_ids':['v1','v2','v3']}
    baseline_versions={'policy':'fixed-aggregation','capability':'raw-sum-1','scientific_protocol':'csv_raw_values_v1'}
    candidate_versions={**baseline_versions,'capability':'explicit-unit-conversion-1','scientific_protocol':PROTOCOL}
    spec={'schema_version':'rsi_trial_v2','intervention_kind':'capability',
        'baseline_versions':baseline_versions,'candidate_versions':candidate_versions,
        'study_id':'raw-value-check','new_study_id':'tabular-energy-task',
        'applicability':{'scope':'One frozen heldout CSV validates unit conversion correctness; no general performance claim',
            'branch_execution':{'profile':'reviewed_local_python_metrics_v1',
                'artifact_sha256':{'baseline':digest(baseline.read_bytes()),'candidate':digest(candidate.read_bytes())},
                'limits':{'wall_seconds':3,'cpu_seconds':1,'output_bytes':16384}}},
        'execution_mode':'isolated_branch','pairs':[{'pair_id':'heldout-csv','input_hash':digest(value),
            'baseline_run_id':'raw-values','candidate_run_id':'explicit-units'}],'minimum_pairs':1,
        'metrics':[{'name':'max_absolute_error_kwh','direction':'lower','role':'primary','min_gain':1,'max_loss':0,'per_pair_no_regression':True},
                   {'name':'row_loss','direction':'lower','role':'guard','min_gain':0,'max_loss':0,'per_pair_no_regression':True}],
        'release_rule':'all_primary','budget':{'model_calls':0,'tokens':0,'tool_calls':3,'cpu_seconds':5,'wall_seconds':10},
        'settlement_reserve':{'tool_calls':1,'wall_seconds':1},'recovery_owner':'tabular-demo-controller'}
    return spec,value,{'baseline':baseline,'candidate':candidate}


def run_demo(directory, run_id='tabular-portability', *, additional_adapters=()):
    root=Path(directory).resolve();store=Store(root)
    controller=Controller(store)
    try:
        try: store.run(run_id)
        except KeyError: pass
        else:
            saved=store.get(run_id,'registration','tabular_demo_report')
            store.verify_history(run_id)
            return json.loads(store.read_blob(saved['report_ref']))
        controller.initialize(run_id,max_external=1,budgets={k:{'limit':v,'hard':k.endswith('calls')}
            for k,v in {'model_calls':10,'tokens':10000,'tool_calls':20,'cpu_seconds':60,'wall_seconds':120}.items()},
            config={'execution_mode':'replay','goal':'Aggregate the supplied measurement CSV by group in kWh',
                'authorized_sources':['meter_metadata'],'open_review_interval_batches':1,'max_open_reviews':1,
                'wait_timeout_seconds':600,'task_plan_id':'tabular-energy-task'})
        # The new domain executable is not available to Meta before its
        # capability request and actual acceptance have completed.
        runtime=Runtime(store,run_id,adapters=list(additional_adapters))
        controller.add_fact(run_id,'csv-input','raw_fact',{'dataset_id':'energy-001','csv_text':CSV_TEXT,'units':None},
                            {'source':'bundled measurement CSV'},'input')
        discovery=runtime.observe('first-batch','batch_boundary',1)
        decisions=[]
        def decide(work_id,action,payload):
            result=runtime.run_meta(work_id,ScriptedMeta(action,payload),{'model_calls':0,'tokens':0,'wall_seconds':1})
            if result['proposal']['status']!='applied': raise ValueError(result['proposal'])
            decisions.append({'action':action,'effect_id':result['effect']['id'],'synthetic':True})
            return json.loads(store.read_blob(result['proposal']['result_ref']))
        def ready(iid):
            return next(w for w in controller.ready(run_id) if w['kind']=='meta_disposition' and w['issue_id']==iid)['id']
        issue=decide(discovery['work_id'],'propose_issue',{'title':'Measurement units are not evidenced',
            'problem_kind':'missing_information','scope':'energy-001 CSV aggregation',
            'hypothesis':'Adding the raw magnitudes may combine incompatible units',
            'competing_explanations':['All records may already use kWh but the metadata was omitted'],
            'discriminating_check':'Retrieve the record-bound unit map from the declared measurement source',
            'falsifying_observation':'A source receipt establishes identical kWh units for every input identity'})
        iid=issue['id']
        request=decide(ready(iid),'request_evidence',{'source_id':'meter_metadata','source_version':1,
            'object_ids':['energy-001'],'fields':['record_unit_map'],'query':'Retrieve the explicit units for each CSV record',
            'max_pages':1,'max_bytes':4096})
        if store.get(run_id,'wait',request['wait_id'])['status']!='waiting': raise ValueError('Expected a real persisted wait')
        # Recreate the full controller/runtime while waiting, with no model or
        # adapter execution. Delivery below is the concrete wake event.
        store.close();store=Store(root);controller=Controller(store);runtime=Runtime(store,run_id,adapters=list(additional_adapters))
        runtime.recover()
        runtime.ingest_delivery(request['request_id'],{'dataset_id':'energy-001','record_unit_map':UNITS},
                                {'source':'meter_metadata','source_version':1})
        build=decide(ready(iid),'request_capability',{'capability_id':ADAPTER_ID,
            'purpose':'Validate identities and explicit energy units, then aggregate to kWh',
            'input_contract':'CSV plus exact record-to-unit mapping','output_contract':'group totals and preserved row identities',
            'applicability':'Wh/kWh nonnegative bounded energy records','acceptance_checks':[
                'mixed units normalize correctly','missing units rejected','unknown unit rejected','duplicate identity rejected'],
            'affects_scientific_results':True})
        spec,path,manifest,artifact=materialize_capability(root/'engineering')
        accepted=runtime.accept_capability(build['work_id'],spec,path,manifest,artifact)
        if accepted['capability']['status']!='verified': raise ValueError('CSV capability acceptance failed')
        runtime=Runtime(store,run_id,adapters=[make_adapter(),*additional_adapters])
        trial_plan,value,paths=trial_spec(root/'engineering')
        trial=decide(ready(iid),'propose_trial',{'trial_kind':'tool_capability','baseline_version':'raw-sum-1',
            'candidate_version':'explicit-unit-conversion-1','hypothesis':'Explicit conversion corrects mixed-unit totals without losing rows',
            'plan_ref':store.put_blob(trial_plan)})
        engine=TrialEngine(store,run_id);engine.start(trial['trial_id'],key='start-trial')
        branch_receipts=[]
        for arm in ['baseline','candidate']:
            packet=make_branch_packet(store,run_id,trial['trial_id'],'heldout-csv',arm,value,paths[arm],
                                      execution_authorization_sha256=digest(paths[arm].read_bytes()))
            work=controller.enqueue(run_id,'lab','validation:'+arm,'branch-work:'+arm)
            effect=controller.prepare_effect(run_id,work['id'],'isolated_trial_branch',packet,
                branch_reservations(store,run_id,trial['trial_id']),'prepare-branch:'+arm,
                metadata={key:packet[key] for key in ['trial_id','pair_id','arm','run_id']})
            effect=controller.dispatch_effect(run_id,effect['id'],BranchExecutor(store,run_id))
            engine.record_measurement(trial['trial_id'],effect['id'],key='measure:'+arm)
            branch_receipts.append(json.loads(store.read_blob(effect['result_ref'])))
        settled=runtime.settle_trial(trial['trial_id'],key='settle-trial')
        decide(ready(iid),'close_issue',{'resolution':'resolved',
            'reason':'Bound units, executable failure cases and heldout conversion measurements establish the limited aggregation prerequisites'})
        dataset={'dataset_id':'energy-001','csv_text':CSV_TEXT,'units':UNITS}
        plan=task_plan(root,dataset,artifact)
        registered=register_task_plan(store,run_id,plan,key='freeze-task')
        plan_ref=registered.get('plan_ref',registered.get('spec_ref'))
        if not plan_ref: raise ValueError('Task plan registration did not return its immutable plan reference')
        # Simulate process loss after the successful receipt but before domain
        # publication. Recovery must publish that receipt without another call.
        class InterruptedPublication(Runtime):
            def publish_adapter_effect(self, effect):
                raise KeyboardInterrupt('simulated loss before publication')
        runtime=InterruptedPublication(store,run_id,adapters=[make_adapter(),*additional_adapters])
        try:
            runtime.execute_adapter(ADAPTER_ID,{'dataset':dataset,'capability_artifact_sha256':artifact,
                                              'task_plan_ref':plan_ref,'task_plan':load_task_plan(store,run_id,plan['task_id'])},
                                    trigger_id='aggregate-final')
        except KeyboardInterrupt: pass
        else: raise ValueError('The intended receipt/publication recovery fixture did not interrupt')
        store.close();store=Store(root);controller=Controller(store);runtime=Runtime(store,run_id,adapters=[make_adapter(),*additional_adapters])
        runtime.recover()
        effects=store.list(run_id,'effect',kind='tabular_aggregation')
        if len(effects)!=1 or effects[0]['status']!='succeeded': raise ValueError('Recovery repeated or lost the aggregation')
        effect=effects[0];receipt=json.loads(store.read_blob(effect['result_ref']));aggregation=receipt['result']['aggregation']
        (root/'group_totals.json').write_bytes(encode(aggregation))
        actual={row['group']:row['energy_kwh'] for row in aggregation['groups']}
        observations=[{'item_id':group,'identity':'site:'+group,'arm_id':'validated','status':'completed',
            'values':{'absolute_error_kwh':{'aggregation':abs(actual[group]-EXPECTED_TOTALS[group])}},
            'evidence_refs':[effect['result_ref']]} for group in ['North','South']]
        selected=select_items(plan,observations,'validated')
        if selected['status']!='accepted': raise ValueError('Exact frozen aggregation task did not satisfy its contract')
        report={'schema_version':'rsi_tabular_portability_report_v1','status':'completed','run_id':run_id,
            'domain':'tabular energy measurements','meta_decisions':'explicit synthetic fixtures',
            'loaded_adapters':[ADAPTER_ID,*[a.adapter_id for a in additional_adapters]],
            'actual_calculations':['four executable capability acceptance cases','two isolated heldout branch calculations','one final CSV aggregation'],
            'registered_adapter':ADAPTER_ID,'capability_registration':accepted['capability']['id'],
            'trial_outcome':settled['outcome'],'branch_metrics':[r['trial_measurement']['metrics'] for r in branch_receipts],
            'distinct_branch_workspaces':len({r['trial_execution']['workspace_id'] for r in branch_receipts})==2,
            'issue_status':store.get(run_id,'issue',iid)['status'],'request_wait':store.get(run_id,'wait',request['wait_id'])['status'],
            'wait_restart_exercised':True,'receipt_publication_restart_exercised':True,'final_aggregation_execution_count':len(effects),
            'aggregation':aggregation,'task_selection':selected,'task_plan_ref':plan_ref,'decisions':decisions,
            'costs':{row['id']:{'spent':row['spent'],'reserved':row['reserved']} for row in store.list(run_id,'budget')},
            'history':store.verify_history(run_id),
            'limitations':['Fixed Meta fixtures do not measure autonomous premise discovery','One heldout CSV validates only the declared unit-conversion cases',
                           'Process/temp-directory isolation is not an OS/network sandbox']}
        store.mutate(run_id,'tabular-demo-report','portability_demo_completed',
            lambda tx:tx.insert('registration','tabular_demo_report',{'status':'completed','report_ref':tx.blob(report)}),payload=report)
        (root/'portability_report.json').write_bytes(encode(report));store.verify_history(run_id)
        return report
    finally:
        store.close()
