import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab import input_view
from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller, identity
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode
from rsi_lab.roles import response_schema


def prepare(inputs):
    if set(inputs) != {'candidates'} or not inputs['candidates']:
        raise ValueError('Candidate input required')
    return encode(inputs)


def reservations(packet):
    return {'tool_calls':1}


def execute(effect, packet):
    value = json.loads(packet)
    return {'status':'succeeded','actual_costs':{'tool_calls':1},
            'result':{'evaluations':[{'candidate':v,'score':.75} for v in value['candidates']],
                      'filter_counts':{'accepted':len(value['candidates']),'duplicate':0}}}


def publication(effect, receipt):
    return {'facts':[{'id':'result_'+effect['id'],'kind':'raw_fact','content':receipt['result'],
                     'provenance':{'effect_id':effect['id'],'source':'real_test_adapter'}}],
            'conclusions':[],'data':receipt['result']}


def failed_execute(effect, packet):
    return {'status':'failed_definite','actual_costs':{'tool_calls':1},'error':'Known bounded tool failure'}


class FakeModel:
    """Produces decisions, but exercises the actual packet/receipt/router chain."""
    def __init__(self, action, payload, *, decision=None, review=False, mutate=None):
        self.action,self.payload,self.decision,self.review,self.mutate = action,payload,decision,review,mutate
        self.inputs = []

    def __call__(self,effect,packet):
        envelope=json.loads(packet)
        data=input_view.decode(envelope['prompt'].split('INPUT_DATA_JSON:\n',1)[1],envelope['input_view_mode'])
        self.inputs.append(data)
        task=data['task']; mode=envelope['mode']
        refs=[p['evidence_id'] for p in data['delivered_pages']]
        response={'command':{'mode':mode,'action':self.action,'issue_id':task.get('issue_id'),
            'expected_versions':task['expected_versions'],'evidence_ids':refs,'payload':self.payload},
            'working_memory_delta':'{}'}
        if 'scientific_memory' in task:
            response['scientific_memory_updates']='[]'
        scope=task['constraints'].get('research_loop')
        if scope is not None:
            if mode=='lab':
                response['recommendation_responses']=[{'recommendation_id':r['id'],'expected_version':r['version'],
                    'decision':self.decision or 'decline','reason':'Use the actual source-backed candidate advice',
                    'evidence_ids':refs} for r in scope['recommendations']]
            else:
                response['improvement_reviews']=[{'recommendation_id':r['id'],'expected_version':r['version'],
                    'outcome':'supported_observation','reason':'The real executed batch has the measured score; no control exists',
                    'evidence_ids':refs,'scientific_status':'unverified','causal_benefit':'not_established'}
                    for r in scope['improvement_reviews']]
        if self.mutate:self.mutate(response,data)
        return {'status':'succeeded','terminal_received':True,'protocol_valid':True,
            'actual_costs':{'model_calls':1,'tokens':1,'wall_seconds':0},'response':response,
            'sent_packet_sha256':digest(packet),'sent_prompt_sha256':digest(envelope['prompt'].encode()),
            'usage_complete':True,'usage':{'input_tokens':1,'output_tokens':0},'synthetic':True}


class ImprovementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'run');self.addCleanup(self.store.close)
        self.c=Controller(self.store)
        self.c.initialize('r',budgets={r:{'limit':100,'hard':r.endswith('calls')} for r in
            ['model_calls','tool_calls','tokens','wall_seconds']},config={'execution_mode':'replay',
            'research_features':{'meta_lab':True,'scientific_memory':True},'max_input_bytes':500000})
        adapter=Adapter('candidate_test','v1','candidate_test',{'scope':'test candidates'},prepare,reservations,execute,publication)
        self.rt=Runtime(self.store,'r',adapters=[adapter])
        self.c.add_fact('r','original','raw_fact',{'score':.5,'candidate':'old'},{'source':'original measured data'},'original')
        self.rt.scientific_memory.observe_fact('original')
        self.res={'model_calls':1,'tokens':10,'wall_seconds':1}
        self.advice={'change_kind':'candidate_selection','instruction':'Evaluate candidate new instead of repeating old',
            'capability_id':'candidate_test','capability_version':'v1','expected_outcome':'A novel scored observation',
            'measurement':'accepted count and observed score','falsifier':'No novel observations accepted'}

    def recommend(self):
        w=self.rt.observe('one','new_evidence',0,['original'])
        model=FakeModel('recommend_change',self.advice)
        out=self.rt.run_meta(w['work_id'],model,self.res)
        self.assertEqual(out['proposal']['status'],'applied',out['proposal'])
        return self.rt.improvements.rows()[0],out

    def lab(self,decision='adopt',mutate=None):
        work=next(w for w in self.c.ready('r') if w['kind']=='lab')
        model=FakeModel('request_diagnostic' if decision=='adopt' else 'no_action',
            {'capability_id':'candidate_test','capability_version':'v1','inputs_json':'{"candidates":["new"]}'}
            if decision=='adopt' else {'reason':'Await a bounded next opportunity'},decision=decision,mutate=mutate)
        return self.rt.run_lab(work['id'],model,self.res),model

    def test_real_packet_action_effect_measurement_review_and_replay(self):
        row,first=self.recommend()
        out,model=self.lab()
        self.assertEqual(out['proposal']['status'],'applied',out['proposal'])
        data=model.inputs[0]
        self.assertIn('original',data['required_evidence_ids'])
        self.assertIn(row['recommendation_fact_id'],data['required_evidence_ids'])
        wire=json.loads(self.store.read_blob(out['effect']['packet_ref']))
        self.assertIn('Evaluate candidate new',wire['prompt'])
        row=self.rt.improvements.rows()[0]
        work=self.store.get('r','work',row['tool_work_id'])
        self.assertEqual(work['inputs']['source_proposal_id'],out['proposal']['id'])
        execution=self.rt.execute_adapter('candidate_test',{'candidates':['new']},trigger_id=work['id'],work_id=work['id'])
        row=self.rt.improvements.rows()[0]
        self.assertEqual(row['measurement']['execution_status'],'succeeded')
        self.assertNotIn('batch_best',row['measurement'])
        self.assertNotIn('domain_metrics',row['measurement'])
        self.assertEqual(row['executed_effect_id'],execution['effect']['id'])
        reviewwork=next(w for w in self.c.ready('r') if w['inputs'].get('improvement_review_ids'))
        reviewer=FakeModel('no_action',{'reason':'Review completed without causal inference'},review=True)
        review=self.rt.run_meta(reviewwork['id'],reviewer,self.res)
        self.assertEqual(review['proposal']['status'],'applied',review['proposal'])
        pages={p['evidence_id'] for p in reviewer.inputs[0]['delivered_pages']}
        self.assertTrue(set(row['source_evidence_ids']+row['result_evidence_ids']+[row['measurement_fact_id'],row['recommendation_fact_id']]).issubset(pages))
        before=(len(self.store.list('r','effect')),len(self.store.list('r','work')))
        self.rt.recover();self.rt.recover()
        self.assertEqual(before,(len(self.store.list('r','effect')),len(self.store.list('r','work'))))
        self.assertEqual(self.rt.pending_improvements(),[])
        self.assertEqual(self.rt.improvements.rows()[0]['causal_benefit'],'not_established')
        self.store.verify_history('r')

    def test_missing_explicit_lab_response_rejected_before_diagnostic(self):
        self.recommend()
        out,_=self.lab(mutate=lambda response,data:response.update(recommendation_responses=[]))
        self.assertEqual(out['proposal']['status'],'rejected')
        self.assertEqual(self.store.list('r','work',kind='diagnostic'),[])

    def test_adoption_cannot_bind_another_capability(self):
        self.recommend()
        out,_=self.lab(mutate=lambda response,data:response['command'].update(action='no_action',payload={'reason':'no operation'}))
        self.assertEqual(out['proposal']['status'],'rejected')
        self.assertEqual(self.store.list('r','work',kind='diagnostic'),[])

    def test_defer_revisit_is_bounded(self):
        self.recommend()
        first,_=self.lab('defer');self.assertEqual(first['proposal']['status'],'applied')
        second,_=self.lab('defer');self.assertEqual(second['proposal']['status'],'applied')
        self.assertEqual(self.rt.improvements.rows()[0]['status'],'deferred_closed')
        self.assertEqual(self.rt.pending_improvements(),[])
        self.assertFalse([w for w in self.c.ready('r') if w['kind']=='lab'])

    def test_final_review_cannot_score_and_does_not_reenqueue_defer(self):
        self.recommend()
        work=next(w for w in self.c.ready('r') if w['kind']=='lab')
        self.store.mutate('r','final','final_review',lambda tx:tx.update('work',work['id'],{
            'inputs':{**work['inputs'],'final_review_only':True}}))
        out,model=self.lab('defer')
        self.assertEqual(out['proposal']['status'],'applied')
        self.assertEqual(model.inputs[0]['task']['capability_catalog'],{})
        self.assertEqual(self.rt.pending_improvements(),[])
        self.assertEqual(self.store.list('r','work',kind='diagnostic'),[])

    def test_old_schema_has_no_research_fields_or_actions(self):
        schema=response_schema('discovery')
        self.assertEqual(set(schema['properties']),{'command'})
        self.assertNotIn('recommend_change',json.dumps(schema))

    def test_final_packet_rejects_scoring_even_if_model_ignores_instructions(self):
        self.recommend()
        work=next(w for w in self.c.ready('r') if w['kind']=='lab')
        self.store.mutate('r','final','final_review',lambda tx:tx.update('work',work['id'],{
            'inputs':{**work['inputs'],'final_review_only':True}}))
        out,_=self.lab('adopt')
        self.assertEqual(out['proposal']['status'],'rejected')
        self.assertEqual(self.store.list('r','work',kind='diagnostic'),[])

    def test_false_causal_verification_is_rejected(self):
        self.recommend();self.lab()
        row=self.rt.improvements.rows()[0];work=self.store.get('r','work',row['tool_work_id'])
        self.rt.execute_adapter('candidate_test',{'candidates':['new']},trigger_id=work['id'],work_id=work['id'])
        reviewwork=next(w for w in self.c.ready('r') if w['inputs'].get('improvement_review_ids'))
        model=FakeModel('no_action',{'reason':'Trying an unsupported claim'},mutate=lambda r,d:
            r['improvement_reviews'][0].update(causal_benefit='verified'))
        out=self.rt.run_meta(reviewwork['id'],model,self.res)
        self.assertEqual(out['proposal']['status'],'rejected')
        self.assertEqual(self.rt.improvements.rows()[0]['status'],'executed')

    def test_known_execution_failure_is_measured_and_reviewed_without_retry(self):
        adapter=Adapter('candidate_test','v1','candidate_test',{'scope':'test candidates'},
                        prepare,reservations,failed_execute,publication)
        self.rt=Runtime(self.store,'r',adapters=[adapter])
        self.recommend();self.lab()
        row=self.rt.improvements.rows()[0];work=self.store.get('r','work',row['tool_work_id'])
        out=self.rt.execute_adapter('candidate_test',{'candidates':['new']},trigger_id=work['id'],work_id=work['id'])
        self.assertEqual(out['effect']['status'],'failed_definite')
        row=self.rt.improvements.rows()[0]
        self.assertEqual(row['measurement']['execution_status'],'failed_definite')
        self.assertNotIn('batch_best',row['measurement'])
        self.rt.recover();self.rt.recover()
        self.assertEqual(len(self.store.list('r','effect',kind='candidate_test')),1)
        self.assertEqual(len([w for w in self.store.list('r','work') if w['inputs'].get('improvement_review_ids')]),1)

    def test_shared_memory_update_enters_actual_next_lab_packet_with_originals(self):
        work=self.rt.enqueue_lab('initial',['original'])
        def update(response,data):
            question=next(r for r in data['task']['scientific_memory']['objects'] if r['kind']=='question')
            response['scientific_memory_updates']=json.dumps([{'kind':'claim','id':'local-score-claim',
                'expected_revision':0,'question_ids':[question['id']],
                'content':{'statement':'The recorded original candidate scored 0.5','scope':{'candidate':'old'}},
                'source_evidence_ids':['original'],'object_refs':[]}])
        model=FakeModel('no_action',{'reason':'Recorded a bounded local interpretation'},mutate=update)
        out=self.rt.run_lab(work['id'],model,self.res)
        self.assertEqual(out['proposal']['status'],'applied')
        second=self.rt.enqueue_lab('inspect-memory',['original'])
        nextmodel=FakeModel('no_action',{'reason':'Checked the source-bound memory'})
        self.rt.run_lab(second['id'],nextmodel,self.res)
        data=nextmodel.inputs[0]
        self.assertIn('local-score-claim',[r['id'] for r in data['task']['scientific_memory']['objects']])
        self.assertIn('original',data['required_evidence_ids'])
        self.assertTrue(next(p for p in data['delivered_pages'] if p['evidence_id']=='original')['complete_source'])

    def test_invalid_memory_update_does_not_block_real_adapter_execution(self):
        self.recommend()
        out,_=self.lab(mutate=lambda response,data:response.update(scientific_memory_updates='[{"kind":"evidence"}]'))
        self.assertEqual(out['proposal']['status'],'applied')
        updates=self.store.list('r','registration',kind='scientific_memory_update_result',effect_id=out['effect']['id'])
        self.assertEqual(updates[0]['status'],'rejected')
        row=self.rt.improvements.rows()[0];work=self.store.get('r','work',row['tool_work_id'])
        actual=self.rt.execute_adapter('candidate_test',{'candidates':['new']},trigger_id=work['id'],work_id=work['id'])
        self.assertEqual(actual['effect']['status'],'succeeded')
        self.assertEqual(self.rt.improvements.rows()[0]['measurement']['execution_status'],'succeeded')
        self.assertNotIn('scored_count',self.rt.improvements.rows()[0]['measurement'])

    def test_publication_crash_replay_creates_one_result_review(self):
        self.recommend();self.lab()
        row=self.rt.improvements.rows()[0];work=self.store.get('r','work',row['tool_work_id'])
        original=self.rt.improvements.publication
        def crash(effect,publication):
            original(effect,publication)
            raise KeyboardInterrupt('after durable measurement')
        with patch.object(self.rt.improvements,'publication',side_effect=crash),self.assertRaises(KeyboardInterrupt):
            self.rt.execute_adapter('candidate_test',{'candidates':['new']},trigger_id=work['id'],work_id=work['id'])
        self.rt.recover();self.rt.recover()
        reviews=[w for w in self.store.list('r','work') if w['inputs'].get('improvement_review_ids')]
        self.assertEqual(len(reviews),1)
        self.assertEqual(len(self.store.list('r','effect',kind='candidate_test')),1)

    def test_recommendation_gets_successor_when_lab_packet_is_already_leased(self):
        store=Store(Path(self.tmp.name)/'parallel');self.addCleanup(store.close)
        controller=Controller(store)
        controller.initialize('parallel',max_external=2,
            budgets=self.rt.config['budgets'],config={'execution_mode':'replay',
                'research_features':{'meta_lab':True},'max_input_bytes':500000})
        runtime=Runtime(store,'parallel',adapters=list(self.rt.adapters.by_id.values()))
        controller.add_fact('parallel','original','raw_fact',{'score':.5},
                            {'source':'original measurement'},'original')
        old_work=runtime.enqueue_lab('already-prepared',['original'])
        prepared=runtime.prepare_meta(old_work['id'],self.res)
        review=runtime.observe('parallel-review','new_evidence',0,['original'])
        recommended=runtime.run_meta(review['work_id'],FakeModel('recommend_change',self.advice),self.res)
        self.assertEqual(recommended['proposal']['status'],'applied')
        row=runtime.improvements.rows()[0]
        notified=[w for w in controller.ready('parallel') if w['inputs'].get('recommendation_id')==row['id']]
        self.assertEqual(len(notified),1)
        # The already leased packet has no recommendation response obligation.
        controller.dispatch_effect('parallel',prepared['id'],
            FakeModel('no_action',{'reason':'The old packet had no pending advice'}))
        self.assertEqual(runtime.apply_meta(prepared['id'])['status'],'applied')
        runtime.recover();runtime.recover()
        successor=next(w for w in controller.ready('parallel') if w['kind']=='lab')
        model=FakeModel('no_action',{'reason':'Explicitly declined after source review'},decision='decline')
        outcome=runtime.run_lab(successor['id'],model,self.res)
        self.assertEqual(outcome['proposal']['status'],'applied')
        self.assertEqual(model.inputs[0]['task']['constraints']['research_loop']['recommendations'][0]['id'],row['id'])
        self.assertEqual(runtime.pending_improvements(),[])
        store.verify_history('parallel')

    def test_existing_ready_lab_can_consume_and_cancel_redundant_notification(self):
        existing=self.rt.enqueue_lab('already-ready',['original'])
        row,_=self.recommend()
        notifications=[w for w in self.c.ready('r') if w['inputs'].get('recommendation_id')==row['id']]
        self.assertEqual(len(notifications),1)
        out=self.rt.run_lab(existing['id'],FakeModel('no_action',{'reason':'Decline this advice'},decision='decline'),self.res)
        self.assertEqual(out['proposal']['status'],'applied')
        self.assertEqual(self.store.get('r','work',notifications[0]['id'])['status'],'cancelled')
        self.assertFalse([w for w in self.c.ready('r') if w['kind']=='lab'])

    def test_concurrent_response_cas_rolls_back_diagnostic_and_dependency(self):
        self.recommend()
        original_route=self.rt._route
        interleaved=False
        def route(proposal):
            nonlocal interleaved
            if proposal['command']['mode']=='lab' and not interleaved:
                interleaved=True
                session=self.store.list('r','agent_session',role='lab')[0]
                self.assertEqual(session['status'],'idle')
                self.assertIsNone(session['active_turn_id'])
                second=self.rt.enqueue_lab('second-coordinator',['original'])
                result=self.rt.run_lab(second['id'],FakeModel('no_action',
                    {'reason':'Decline after reconsideration'},decision='decline'),self.res)
                self.assertEqual(result['proposal']['status'],'applied')
            return original_route(proposal)
        dependencies=self.store.list('r','dependency')
        with patch.object(self.rt,'_route',side_effect=route):
            outcome,_=self.lab('adopt')
        self.assertEqual(outcome['proposal']['status'],'blocked_action')
        self.assertIn('stale',outcome['proposal']['error'])
        self.assertEqual(self.rt.improvements.rows()[0]['status'],'declined')
        self.assertEqual(self.store.list('r','work',kind='diagnostic'),[])
        self.assertEqual(self.store.list('r','effect',kind='candidate_test'),[])
        self.assertEqual({d['id'] for d in self.store.list('r','dependency')},{d['id'] for d in dependencies})
        self.assertNotIn('measurement',self.rt.improvements.rows()[0])
        self.store.verify_history('r')

    def test_atomic_adoption_crash_replays_without_repeating_tool_or_reverting_dependency(self):
        self.recommend()
        original=self.rt.improvements.apply_responses
        def crash(proposal,result,**kwargs):
            result=original(proposal,result,**kwargs)
            if kwargs.get('create_result') is not None:
                raise KeyboardInterrupt('after atomic adoption and outbox commit')
            return result
        with patch.object(self.rt.improvements,'apply_responses',side_effect=crash),self.assertRaises(KeyboardInterrupt):
            self.lab('adopt')
        row=self.rt.improvements.rows()[0]
        self.assertEqual(row['status'],'adopted')
        work=self.store.get('r','work',row['tool_work_id'])
        proposal=self.store.get('r','proposal',row['lab_proposal_id'])
        self.assertEqual(proposal['status'],'admitted')
        # Another coordinator can execute the committed outbox before the
        # crashed proposal coordinator comes back to mark its route applied.
        executed=self.rt.execute_adapter('candidate_test',{'candidates':['new']},
            trigger_id=work['id'],work_id=work['id'])
        self.assertEqual(executed['effect']['status'],'succeeded')
        self.rt.recover();self.rt.recover()
        self.assertEqual(self.store.get('r','proposal',proposal['id'])['status'],'applied')
        self.assertEqual(self.store.get('r','dependency',work['inputs']['dependency_id'])['value']['status'],'completed')
        self.assertEqual(len(self.store.list('r','work',kind='diagnostic')),1)
        self.assertEqual(len(self.store.list('r','effect',kind='candidate_test')),1)
        self.assertEqual(len([w for w in self.store.list('r','work') if w['inputs'].get('improvement_review_ids')]),1)
        self.assertEqual(self.store.get('r','budget','tool_calls')['spent'],1)
        self.store.verify_history('r')

    def test_recover_legacy_response_transaction_with_none_result(self):
        self.recommend()
        original_mutate=self.rt._mutate
        original_route=self.rt._route
        def legacy_mutate(key,event,fn,payload):
            if event=='meta_proposal_applied':
                raise KeyboardInterrupt('after legacy response commit')
            if event=='research_responses_applied':
                def discard_result(tx):
                    fn(tx)
                    return None
                return original_mutate(key,event,discard_result,payload)
            return original_mutate(key,event,fn,payload)
        def legacy_route(proposal):
            if proposal['command']['action']!='request_diagnostic':
                return original_route(proposal)
            base=identity('object',proposal['id']);key='route:'+proposal['id']
            payload=proposal['command']['payload']
            self.c.dependency('r',base,payload['capability_id'],{'status':'pending'},key+':dependency')
            return self.c.enqueue('r','diagnostic',base,key+':work',inputs={**payload,
                'issue_id':None,'dependency_id':base,'lab_continuation':True,'source_proposal_id':proposal['id']})
        with patch.object(self.rt,'_route',side_effect=legacy_route),patch.object(self.rt,'_mutate',side_effect=legacy_mutate),self.assertRaises(KeyboardInterrupt):
            self.lab('adopt')
        row=self.rt.improvements.rows()[0]
        self.assertEqual(row['status'],'adopted')
        self.rt.recover();self.rt.recover()
        proposal=self.store.get('r','proposal',row['lab_proposal_id'])
        self.assertEqual(proposal['status'],'applied')
        self.assertEqual(json.loads(self.store.read_blob(proposal['result_ref']))['id'],row['tool_work_id'])
        self.assertEqual(len(self.store.list('r','work',kind='diagnostic')),1)
        self.store.verify_history('r')


if __name__=='__main__':unittest.main()
