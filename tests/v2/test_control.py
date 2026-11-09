import json
from pathlib import Path
import tempfile
import unittest

from docking_rsi.store import Store
from docking_rsi.controller import Controller


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.control = Controller(self.store)
        self.control.initialize('r', budgets={'model_calls': {'limit': 10, 'hard': True, 'settlement_reserve': 1},
            'tool_calls': {'limit': 10, 'hard': True}, 'tokens': {'limit': 100, 'hard': False}})

    def issue(self, name='i'):
        self.control.add_fact('r', 'fact'+name, 'raw_fact', {'unit': None}, {'source': 'fixture'}, 'fact'+name)
        return self.control.open_issue('r', name, {'component': name}, 'Units may be missing', {'object':name},
            ['fact'+name], ['Unit may be in separate metadata'], ['A valid source declares its units'], 'issue'+name)

    def effect(self, key='call', issue_id=None, reservations=None):
        w = self.control.enqueue('r', 'diagnostic', key, 'work'+key, issue_id=issue_id)
        return self.control.prepare_effect('r', w['id'], 'tool', b'{"input":1}',
            reservations or {'tool_calls': 1}, 'prepare'+key)

    def test_atomic_rollback_and_idempotent_operation(self):
        def fail(tx):
            tx.insert('fact', 'temporary', {'kind': 'hypothesis'})
            raise RuntimeError('crash before commit')
        with self.assertRaises(RuntimeError):
            self.store.mutate('r', 'bad', 'fixture', fail)
        self.assertEqual(self.store.list('r', 'fact'), [])
        issue = self.issue()
        again = self.control.open_issue('r', 'i', {'component':'i'}, 'Units may be missing', {'object':'i'},
            ['facti'], ['Unit may be in separate metadata'], ['A valid source declares its units'], 'issuei')
        self.assertEqual(issue, again)
        with self.assertRaisesRegex(ValueError, 'different input'):
            self.store.mutate('r', 'issuei', 'issue_discovered', lambda tx: None, payload={'different':True})
        self.store.verify_history('r')

    def test_wait_is_not_external_lease_and_expiry_only_once(self):
        self.issue()
        dep = self.control.dependency('r', 'source', 'data_owner', {'status':'pending'}, 'dep')
        wait = self.control.park('r','i','source','data_owner',dep['revision'],{'kind':'version_changed'},10,'replan','park')
        effect=self.effect()
        self.control.dispatch_effect('r',effect['id'],lambda e,p:{'status':'succeeded','actual_costs':{'tool_calls':1}})
        self.control.tick('r', now=11)
        self.control.tick('r', now=999)
        self.assertEqual(len(self.store.list('r','work',kind='disposition')),1)
        with self.assertRaisesRegex(ValueError,'new wait cycle'):
            self.control.park('r','i','source','data_owner',1,{'kind':'version_changed'},1000,'replan','renew')
        issue=self.store.get('r','issue','i')
        self.assertEqual(issue['evidence_revision'],1)
        self.control.dependency('r','source','data_owner',{'status':'ready'},'delivery')
        self.control.tick('r',now=1001)
        self.assertTrue(any(w['kind']=='meta_disposition' for w in self.control.ready('r')))
        self.assertEqual(self.store.get('r','issue','i')['evidence_revision'],1)
        self.assertEqual(self.store.get('r','wait',wait['id'])['status'],'fired')

    def test_unknown_effect_retains_reservations_and_cannot_retry(self):
        e=self.effect()
        called=[]
        def crash(effect,packet):
            called.append(effect['id']); raise RuntimeError('connection lost')
        result=self.control.dispatch_effect('r',e['id'],crash)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(self.store.get('r','budget','tool_calls')['reserved'],1)
        with self.assertRaisesRegex(ValueError,'no implicit retry'):
            self.control.dispatch_effect('r',e['id'],crash)
        with self.assertRaisesRegex(ValueError,'execution lease'):
            self.effect('second')
        self.control.recover('r')
        self.assertEqual(called,[e['id']])
        self.control.reconcile('r',e['id'],{'status':'failed_definite','actual_costs':{'tool_calls':0},
            'reconciliation':{'effect_id':e['id'],'provenance':'provider confirms never executed'}},'reconciled')
        self.assertEqual(self.store.get('r','budget','tool_calls')['reserved'],0)

    def test_durable_receipt_can_settle_after_crash_without_new_execution(self):
        e=self.effect()
        self.store.mutate('r','dispatchfixture','effect_dispatching',lambda tx:tx.update('effect',e['id'],{'status':'dispatching'}))
        receipt=self.store.put_blob({'status':'succeeded','actual_costs':{'tool_calls':1}})
        (Path(self.tmp.name)/('receipt_'+e['id']+'.json')).write_text(json.dumps({'effect_id':e['id'],'packet_hash':e['packet_hash'],'receipt_ref':receipt}))
        self.control.recover('r'); self.control.recover('r')
        self.assertEqual(self.store.get('r','budget','tool_calls')['spent'],1)
        self.assertEqual(self.store.get('r','effect',e['id'])['status'],'succeeded')

    def test_soft_token_overrun_is_measured_and_missing_cost_is_reconcilable(self):
        e=self.effect(reservations={'model_calls':1,'tokens':20})
        r=self.control.dispatch_effect('r',e['id'],lambda e,p:{'status':'succeeded','actual_costs':{'model_calls':1}})
        self.assertTrue(r['accounting_unknown'])
        self.assertEqual(self.store.get('r','budget','tokens')['reserved'],20)
        self.control.reconcile('r',e['id'],{'status':'succeeded','actual_costs':{'model_calls':1,'tokens':120},
            'reconciliation':{'effect_id':e['id'],'provenance':'complete provider accounting'}},'reconcile_tokens')
        b=self.store.get('r','budget','tokens')
        self.assertEqual(b['spent'],120);self.assertEqual(b['reserved'],0);self.assertTrue(b['overrun'])

    def test_reply_is_bound_to_question_and_never_inferred(self):
        self.issue()
        dep=self.control.dependency('r','answer','user',{'status':'pending'},'dep')
        q=self.control.ask_user('r','i','q','Which units apply?','answer','ask')
        self.control.park('r','i','answer','user',dep['revision'],{'kind':'status','status':'answered'},100,'ask_user','park')
        with self.assertRaises(ValueError):self.control.reply('r','q',q['version'],'','empty')
        with self.assertRaises(ValueError):self.control.reply('r','q',q['version']+1,'angstrom','wrong')
        self.control.reply('r','q',q['version'],'angstrom','reply')
        self.control.tick('r',now=50)
        self.assertEqual(self.store.get('r','issue','i')['evidence_revision'],2)
        self.assertTrue(self.control.ready('r'))

    def test_repeat_fact_does_not_create_new_scientific_revision(self):
        first=self.issue()
        second=self.control.open_issue('r','same',{'component':'i'},'Same explanation',{'object':'i'},['facti'],['Other'],['Check'],'repeat')
        self.assertEqual(first['evidence_revision'],second['evidence_revision'])
        self.assertEqual(second['seen_count'],2)

    def test_stale_dispatch_and_projection_tampering_rejected(self):
        self.issue()
        work=self.control.ready('r')[0]
        self.store.mutate('r','schedule','capacity_changed',lambda tx:tx.update('issue','i',{'scheduling_revision':1}))
        with self.assertRaisesRegex(ValueError,'stale'):
            self.control.prepare_effect('r',work['id'],'model',b'{}',{'model_calls':1},'bad_dispatch')
        self.store.db.execute("UPDATE v2_issue SET document=json_set(document,'$.status','resolved') WHERE id='i'")
        with self.assertRaisesRegex(ValueError,'projections'):
            self.store.verify_history('r')

    def test_source_summary_does_not_change_into_original_delivery(self):
        self.issue();self.control.request('r','i','req','bounded query','authorized','same target','request')
        self.control.deliver('r','req','page',{'summary':'partial'},'source','delivery',kind='summary')
        self.assertEqual(self.store.get('r','delivery','page')['kind'],'summary')

    def test_close_cannot_erase_unknown_external_obligation(self):
        self.issue();e=self.effect(issue_id='i')
        self.control.dispatch_effect('r',e['id'],lambda e,p:{'status':'unknown','actual_costs':{}})
        with self.assertRaisesRegex(ValueError,'obligations'):
            self.control.close_issue('r','i','closed_unresolved','stop','close')


if __name__=='__main__':unittest.main()
