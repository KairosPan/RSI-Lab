import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab import input_view
from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
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


if __name__=='__main__':unittest.main()
