"""Actual driver ordering, publication barriers, and unfrozen Lab wakeups."""
import json
import unittest
from unittest.mock import patch

from rsi_lab import input_view
from tests.v2 import test_improvements as fixtures


class LabSchedulingTests(unittest.TestCase):
    setUp = fixtures.ImprovementsTests.setUp

    def request_tool(self, work):
        result = self.rt.run_lab(work['id'], fixtures.FakeModel('request_diagnostic', {
            'capability_id': 'candidate_test', 'capability_version': 'v1',
            'inputs_json': json.dumps({'candidates': ['new']})}), self.res)
        self.assertEqual(result['proposal']['status'], 'applied')
        return self.store.list('r', 'work', kind='diagnostic')[-1]

    def wake(self, trigger, inputs, *, priority=10, key=None):
        return self.store.mutate('r', key or 'wake:' + trigger, 'fixture_wake',
            lambda tx: self.c.wake_lab(tx, trigger, inputs=inputs, priority=priority),
            {'trigger': trigger, 'inputs': inputs, 'priority': priority})

    def test_driver_executes_newly_queued_tool_before_older_meta_and_lab_wakes(self):
        original = self.rt.enqueue_lab('original', ['original'])
        meta = self.rt.observe('older-meta', 'input_changed', 0, ['original'])
        # A legacy same-scope ready notification can predate the queued tool.
        lab = self.c.enqueue('r', 'lab', 'older-lab', 'older-lab',
                             inputs={'evidence_ids': ['original']})
        tool = self.request_tool(original)
        self.assertLess(self.store.get('r', 'work', meta['work_id'])['created_seq'], tool['created_seq'])
        self.assertLess(lab['created_seq'], tool['created_seq'])
        with patch.object(fixtures.FakeModel, '__call__', side_effect=AssertionError('Tool must run before another model')):
            result = self.rt.run_until_wait(fixtures.FakeModel('no_action', {'reason': 'unused'}),
                                            self.res, max_steps=1)
        self.assertEqual(result['steps'][0]['work_id'], tool['id'])
        effect = self.store.get('r', 'effect', result['steps'][0]['effect_id'])
        self.assertEqual(effect['kind'], 'candidate_test')
        self.assertEqual(effect['status'], 'succeeded')
        ready = {w['id'] for w in self.c.ready('r')}
        self.assertIn(lab['id'], ready)
        self.assertIn(meta['work_id'], ready)
        self.assertIn('result_' + effect['id'], self.store.get('r', 'work', lab['id'])['inputs']['required_evidence_ids'])

    def test_owner_lab_waits_for_publication_but_other_branch_remains_eligible(self):
        original = self.rt.enqueue_lab('original', ['original'])
        tool = self.request_tool(original)
        owner = self.rt.enqueue_lab('owner-next', ['original'])
        other = self.c.enqueue('r', 'lab', 'other-branch', 'other-branch',
                               inputs={'branch_id': 'independent', 'evidence_ids': ['original']})
        self.assertNotIn(owner['id'], {w['id'] for w in self.c.ready('r')})
        self.assertIn(other['id'], {w['id'] for w in self.c.ready('r')})
        with self.assertRaisesRegex(ValueError, 'Lab awaits'):
            self.rt.prepare_meta(owner['id'], self.res)
        with patch.object(self.rt, 'publish_adapter_effect', side_effect=RuntimeError('hold-publication')):
            with self.assertRaisesRegex(RuntimeError, 'hold-publication'):
                self.rt.execute_adapter('candidate_test', {'candidates': ['new']},
                    trigger_id=tool['id'], work_id=tool['id'])
        current = self.store.get('r', 'work', tool['id'])
        effect = self.store.get('r', 'effect', current['effect_id'])
        self.assertEqual(effect['status'], 'succeeded')
        self.assertEqual(current['status'], 'succeeded')
        self.assertNotIn(owner['id'], {w['id'] for w in self.c.ready('r')})
        with self.assertRaisesRegex(ValueError, 'Lab awaits'):
            self.rt.prepare_meta(owner['id'], self.res)
        decision = self.rt.run_lab(other['id'], fixtures.FakeModel('no_action', {'reason': 'Independent branch'}), self.res)
        self.assertEqual(decision['proposal']['status'], 'applied')
        self.rt.publish_adapter_effect(effect)
        self.assertIn(owner['id'], {w['id'] for w in self.c.ready('r')})

    def test_legacy_tool_publication_wakes_its_original_branch_and_agent(self):
        scope = {'branch_id': 'other', 'agent_instance_id': 'branch-lab'}
        source = self.c.enqueue('r', 'lab', 'legacy-source', 'legacy-source',
                                inputs={**scope, 'evidence_ids': ['original']})
        tool = self.request_tool(source)
        # Persist the old outbox shape, whose owner is only in source_proposal_id.
        old_inputs = {key: value for key, value in tool['inputs'].items() if key != 'lab_scope'}
        self.store.mutate('r', 'legacy-shape', 'fixture_legacy_tool',
            lambda tx: tx.update('work', tool['id'], {'inputs': old_inputs}))
        owner = self.wake('owner-waits', {**scope, 'evidence_ids': ['original']})
        other = self.wake('main-free', {'evidence_ids': ['original']})
        self.assertNotIn(owner['id'], {w['id'] for w in self.c.ready('r')})
        self.assertIn(other['id'], {w['id'] for w in self.c.ready('r')})
        result = self.rt.execute_adapter('candidate_test', {'candidates': ['new']},
            trigger_id=tool['id'], work_id=tool['id'])
        result_id = 'result_' + result['effect']['id']
        owner = self.store.get('r', 'work', owner['id'])
        other = self.store.get('r', 'work', other['id'])
        self.assertIn(owner['id'], {w['id'] for w in self.c.ready('r')})
        self.assertIn(result_id, owner['inputs']['required_evidence_ids'])
        self.assertNotIn(result_id, other['inputs'].get('required_evidence_ids', []))

    def test_wakeups_merge_all_evidence_but_never_cross_branch_or_agent(self):
        first = self.wake('first', {'evidence_ids': ['a'], 'required_evidence_ids': ['a'],
                                    'source_effect_id': 'effect-a'})
        second = self.wake('second', {'evidence_ids': ['b', 'a'], 'required_evidence_ids': ['b'],
                                      'recommendation_id': 'recommendation-b',
                                      'branch_id': 'main', 'agent_instance_id': 'lab'})
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['inputs']['evidence_ids'], ['a', 'b'])
        self.assertEqual(second['inputs']['required_evidence_ids'], ['a', 'b'])
        self.assertEqual(second['inputs']['source_effect_ids'], ['effect-a'])
        self.assertEqual(second['inputs']['recommendation_ids'], ['recommendation-b'])
        self.assertEqual(len(second['inputs']['wakeups']), 2)
        branch = self.wake('branch', {'branch_id': 'other', 'evidence_ids': ['c']})
        agent = self.wake('agent', {'agent_instance_id': 'other-lab', 'evidence_ids': ['d']})
        self.assertEqual(len({first['id'], branch['id'], agent['id']}), 3)

    def test_wakeup_replay_keeps_original_request_identity_after_aggregation(self):
        first = self.wake('stable', {'evidence_ids': ['a']})
        self.wake('more', {'required_evidence_ids': ['b']})
        again = self.wake('stable', {'evidence_ids': ['a']}, key='replay-through-another-operation')
        self.assertEqual(again['id'], first['id'])
        self.assertEqual(again['inputs']['required_evidence_ids'], ['b'])
        for inputs, priority in (({'evidence_ids': ['different']}, 10), ({'evidence_ids': ['a']}, 5)):
            with self.subTest(inputs=inputs, priority=priority):
                with self.assertRaisesRegex(ValueError, 'identity reused'):
                    self.wake('stable', inputs, priority=priority, key='wrong-' + str(priority))

    def test_same_trigger_is_independent_for_each_branch_and_agent(self):
        main = self.wake('shared-source', {'evidence_ids': ['a']}, key='shared-main')
        branch = self.wake('shared-source', {'branch_id': 'other', 'evidence_ids': ['b']}, key='shared-branch')
        agent = self.wake('shared-source', {'agent_instance_id': 'other-lab', 'evidence_ids': ['c']}, key='shared-agent')
        self.assertEqual(len({main['id'], branch['id'], agent['id']}), 3)
        for label, original, inputs in (
            ('main', main, {'evidence_ids': ['a']}),
            ('branch', branch, {'branch_id': 'other', 'evidence_ids': ['b']}),
            ('agent', agent, {'agent_instance_id': 'other-lab', 'evidence_ids': ['c']}),
        ):
            with self.subTest(scope=label):
                again = self.wake('shared-source', inputs, key='repeat-shared-' + label)
                self.assertEqual(again['id'], original['id'])
                self.assertEqual(again['inputs']['evidence_ids'], original['inputs']['evidence_ids'])

    def test_frozen_and_special_lab_requests_are_never_coalesced(self):
        normal = self.wake('normal', {'evidence_ids': ['original']})
        final = self.wake('final', {'evidence_ids': ['original'], 'final_review_only': True})
        repair = self.wake('repair', {'evidence_ids': ['original'], 'repair_of': 'old-proposal'})
        self.assertEqual(len({normal['id'], final['id'], repair['id']}), 3)
        prepared = self.rt.prepare_meta(normal['id'], self.res)
        later = self.wake('after-freeze', {'evidence_ids': ['original']})
        self.assertNotEqual(later['id'], normal['id'])
        self.assertEqual(self.store.get('r', 'effect', prepared['id'])['packet_ref'], prepared['packet_ref'])

    def test_work_version_cas_refuses_a_packet_frozen_before_wakeup_merge(self):
        self.c.add_fact('r', 'new-source', 'raw_fact', {'later': True}, {'source': 'later observation'}, 'new-source')
        work = self.rt.enqueue_lab('initial', ['original'])
        original_prepare = self.c.prepare_effect
        def merge_then_prepare(*args, **kwargs):
            self.wake('intervening', {'evidence_ids': ['new-source'], 'required_evidence_ids': ['new-source']})
            return original_prepare(*args, **kwargs)
        # Runtime owns its own Controller instance, sharing this same Store.
        with patch.object(self.rt.controller, 'prepare_effect', side_effect=merge_then_prepare):
            with self.assertRaisesRegex(ValueError, 'Work changed after packet preparation'):
                self.rt.prepare_meta(work['id'], self.res)
        self.assertEqual(self.store.list('r', 'effect'), [])
        self.assertEqual(self.store.get('r', 'work', work['id'])['status'], 'ready')
        prepared = self.rt.prepare_meta(work['id'], self.res)
        packet = json.loads(self.store.read_blob(prepared['packet_ref']))
        view = input_view.decode(packet['prompt'].split('INPUT_DATA_JSON:\n', 1)[1], packet['input_view_mode'])
        self.assertIn('new-source', view['required_evidence_ids'])

    def test_atomic_prepare_rechecks_a_tool_queued_while_building_the_lab_packet(self):
        work = self.rt.enqueue_lab('initial', ['original'])
        original_prepare = self.c.prepare_effect

        def queue_then_prepare(*args, **kwargs):
            self.c.enqueue('r', 'diagnostic', 'queued-during-prepare', 'queue-race', inputs={
                'capability_id': 'candidate_test', 'capability_version': 'v1',
                'inputs_json': '{"candidates":["new"]}', 'lab_continuation': True,
                'lab_scope': self.c.lab_scope('r', work['inputs'])})
            return original_prepare(*args, **kwargs)

        with patch.object(self.rt.controller, 'prepare_effect', side_effect=queue_then_prepare):
            with self.assertRaisesRegex(ValueError, 'Lab awaits'):
                self.rt.prepare_meta(work['id'], self.res)
        self.assertEqual(self.store.list('r', 'effect'), [])
        self.assertEqual(self.store.get('r', 'work', work['id'])['status'], 'ready')
        self.assertTrue(all(b['reserved'] == 0 for b in self.store.list('r', 'budget')))
        # The kernel guard also protects direct callers that bypass Runtime.
        with self.assertRaisesRegex(ValueError, 'Lab awaits'):
            self.c.prepare_effect('r', work['id'], 'lab', b'bounded packet', self.res, 'direct-prepare')

    def test_blocked_tool_wait_does_not_report_quiescence(self):
        original = self.rt.enqueue_lab('original', ['original'])
        tool = self.request_tool(original)
        self.rt.enqueue_lab('waiting', ['original'])
        self.store.mutate('r', 'blocked-tool', 'fixture_block',
            lambda tx: tx.update('work', tool['id'], {'status': 'blocked'}))
        result = self.rt.run_until_wait(fixtures.FakeModel('no_action', {'reason': 'unused'}),
                                        self.res, max_steps=1)
        self.assertEqual(result['status'], 'awaiting_producer_or_deadline')
        self.assertIn('Lab awaits', result['reason'])
