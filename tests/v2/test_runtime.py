import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from tests.v2.fixtures import SyntheticMetaExecutor, fixture_delivery, inspection_adapter
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'ledger'); self.addCleanup(self.store.close)
        self.c = Controller(self.store)
        self.c.initialize('r', budgets={k: {'limit': v, 'hard': k.endswith('calls')} for k,v in
            {'model_calls': 20, 'tool_calls': 20, 'tokens': 10000, 'cpu_seconds': 20, 'wall_seconds': 100}.items()},
            config={'execution_mode': 'replay', 'authorized_sources': ['source'], 'max_open_reviews': 1,
                    'open_review_interval_batches': 1})
        self.rt = Runtime(self.store, 'r')
        self.c.add_fact('r', 'fact', 'raw_fact', {'receipt': 'unexpected missing provenance'}, {'source': 'actual input'}, 'fact')
        self.c.open_issue('r', 'issue', 'gap', 'Receipt may be missing', 'current prepared object', ['fact'],
                          ['Capability may be unavailable'], ['A valid provenance receipt'], 'issue')
        self.res = {'model_calls': 1, 'tokens': 1000, 'wall_seconds': 2}

    def decision(self, action, payload):
        work = next(w for w in self.c.ready('r') if w['kind'] in {'meta_disposition', 'disposition'})
        result = self.rt.run_meta(work['id'], SyntheticMetaExecutor(action, payload), self.res)
        proposal = result['proposal']
        self.assertEqual(proposal['status'], 'applied', proposal)
        return json.loads(self.store.read_blob(proposal['result_ref']))

    def request(self):
        return self.decision('request_evidence', {'source_id': 'source', 'source_version': 2,
            'object_ids': ['a'], 'fields': ['receipt'], 'query': 'Read the exact preparation receipt',
            'max_pages': 1, 'max_bytes': 1024})

    def test_source_version_and_bounds_survive_role_request(self):
        requested = self.request(); rid = requested['request_id']
        self.assertEqual(self.store.get('r', 'request', rid)['source_version'], 2)
        with self.assertRaises(ValueError): self.rt.ingest_delivery(rid, {}, {'source':'source'}, source_version=1)
        with self.assertRaises(ValueError): self.rt.ingest_delivery(rid, {}, {'source':'source'}, source_version=2, page=1)
        with self.assertRaises(ValueError): self.rt.ingest_delivery(rid, {'huge':'x'*2000}, {'source':'source'}, source_version=2)
        self.rt.ingest_delivery(rid, {'valid':False}, {'source':'source'}, source_version=2)
        self.assertEqual(self.store.get('r', 'work', requested['work_id'])['status'], 'succeeded')
        self.assertEqual(len([w for w in self.c.ready('r') if w['kind']=='meta_disposition']), 1)

    def test_question_is_saved_and_exact_version_reply_resumes(self):
        out = self.decision('ask_user', {'question':'Which experimental conditions apply?', 'options':['documented procedure','exploratory'],
            'reason_user_needed':'Neither authorized source nor current receipt identifies the intended procedure'})
        q = self.store.get('r','question',out['question_id'])
        self.assertEqual(q['status'],'pending')
        self.assertEqual(self.c.ready('r'),[])
        self.c.reply('r',q['id'],q['version'],'exploratory','message-1')
        self.c.tick('r')
        self.assertTrue(self.c.ready('r'))
        self.assertEqual(self.store.get('r','wait',out['wait_id'])['status'],'fired')

    def test_open_review_quota_does_not_block_anomaly_or_disposition(self):
        first = self.rt.observe('batch1','batch_boundary',1)
        self.assertTrue(first['admitted'])
        self.assertFalse(self.rt.observe('batch2','batch_boundary',2)['admitted'])
        self.assertTrue(self.rt.observe('failed','tool_failed',2,['fact'])['admitted'])
        self.assertTrue(any(w['kind']=='meta_disposition' for w in self.c.ready('r')))
        self.assertEqual(first,self.rt.observe('batch1','batch_boundary',1))

    def test_packet_unrepresentable_records_reason_without_call(self):
        self.rt.config = {**self.rt.config,'max_input_bytes':100}
        work = self.c.ready('r')[0]
        with patch.object(SyntheticMetaExecutor,'__call__') as invoke:
            result = self.rt.run_meta(work['id'], SyntheticMetaExecutor('unused',{}), self.res)
        invoke.assert_not_called()
        self.assertEqual(result['work']['status'],'blocked')
        self.assertEqual(json.loads(self.store.read_blob(result['work']['outcome_ref']))['status'],'packet_unrepresentable')
        self.assertEqual(self.store.list('r','effect'),[])

    def test_failed_capability_acceptance_is_delivered_and_wakes_unresolved_issue(self):
        request = self.decision('request_capability', {'capability_id':'synthetic_parent_metadata_audit',
            'purpose':'Audit metadata','input_contract':'metadata','output_contract':'identity receipt',
            'applicability':'synthetic metadata only','acceptance_checks':['normal','failure','identity'],
            'affects_scientific_results':False})
        spec, path, manifest, artifact = fixture_delivery(Path(self.tmp.name)/'fixture')
        spec['cases'][0]['expected_identity'] = 'not-the-source'
        result = self.rt.accept_capability(request['work_id'],spec,path,manifest,artifact)
        self.assertEqual(result['capability']['status'],'failed_acceptance')
        self.assertEqual(self.store.get('r','issue','issue')['status'],'open')
        self.assertEqual(self.store.get('r','wait',request['wait_id'])['status'],'fired')
        self.rt.recover()
        self.assertEqual(len(self.store.list('r','effect',kind='capability_acceptance')),1)
        self.assertEqual(self.store.list('r','capability'),[])

    def test_partial_route_can_resume_own_changes_without_stale_rejection(self):
        work = self.c.ready('r')[0]
        effect = self.rt.prepare_meta(work['id'],self.res)
        self.c.dispatch_effect('r',effect['id'],SyntheticMetaExecutor('ask_user',{
            'question':'Which procedure?','options':['unknown','documented'], 'reason_user_needed':'Procedure goal is user-owned'}))
        original = self.rt._route
        def interrupted(proposal):
            original(proposal)
            raise KeyboardInterrupt('crash after park committed')
        with patch.object(self.rt,'_route',side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.rt.apply_meta(effect['id'])
        result = self.rt.apply_meta(effect['id'])
        self.assertEqual(result['status'],'applied')
        self.assertEqual(len(self.store.list('r','question')),1)
        self.assertEqual(len(self.store.list('r','wait')),1)
        self.rt.recover(); self.store.verify_history('r')

    def test_inspection_fact_reuse_and_interrupted_publication_recover(self):
        # The source-backed fixture uses real dispatch, publication, and recovery.
        self.rt = Runtime(self.store, 'r', adapters=[inspection_adapter()])
        report={'facts':[{'id':'inspection_fact','kind':'raw_fact','content':{'rows':12},
                          'provenance':{'source':'fixture receipt'}}]}
        with patch.object(self.rt,'publish_adapter_effect',side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.rt.execute_adapter('synthetic_inspection', report, trigger_id='one')
        self.rt.recover()
        self.rt.execute_adapter('synthetic_inspection', report, trigger_id='two')
        self.assertEqual(len(self.store.list('r','fact',id='inspection_fact')),1)
        self.assertEqual(self.store.get('r','budget','tool_calls')['spent'],2)
        self.store.verify_history('r')

    def test_cooperative_driver_passes_waiting_issue_to_other_eligible_work(self):
        requested=self.request()
        self.rt.observe('new-batch','batch_boundary',1)
        result=self.rt.run_until_wait(SyntheticMetaExecutor('no_action',{'reason':'No new issue in the delivered facts'}),self.res,max_steps=3)
        self.assertEqual(result['status'],'awaiting_producer_or_deadline')
        self.assertEqual(len(result['steps']),1)
        self.assertEqual(self.store.get('r','wait',requested['wait_id'])['status'],'waiting')

    def test_cooperative_driver_never_resends_unknown_or_fills_silence(self):
        def unknown(effect,packet):
            return {'status':'unknown','actual_costs':{'model_calls':1},'reason':'lost provider receipt'}
        result=self.rt.run_until_wait(unknown,self.res,max_steps=3)
        self.assertEqual(result['status'],'blocked_external')
        with patch.object(SyntheticMetaExecutor,'__call__') as invoke:
            again=self.rt.run_until_wait(SyntheticMetaExecutor('unused',{}),self.res)
        invoke.assert_not_called()
        self.assertEqual(again['status'],'blocked_external')
        self.assertEqual(len(self.store.list('r','effect')),1)


if __name__ == '__main__': unittest.main()
