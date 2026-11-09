"""Synthetic user-message fixtures; never submit an answer to a live run."""
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from rsi_domains.docking.live_reply import bind_and_resume
from rsi_lab.controller import Controller, identity
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store


class LiveReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name); self.addCleanup(self.store.close)
        self.c = Controller(self.store)
        self.c.initialize('r', budgets={'model_calls': {'limit': 2, 'hard': True}}, config={'goal': 'Synthetic reply delivery test'})
        self.rt = Runtime(self.store, 'r')
        for name in ('owner', 'dependent', 'unrelated'):
            fid = 'input-' + name
            self.c.add_fact('r', fid, 'raw_fact', {'scope': 'unknown'}, {'source': 'test fixture'}, 'fact-' + name)
            self.c.open_issue('r', name, {'identity': name}, 'Task scope requires user evidence', name, [fid],
                              ['Scope is already documented'], ['A real user reply supplies the scope'], 'issue-' + name)
        self.dep = self.c.dependency('r', 'dep', 'user', {'status': 'pending'}, 'dependency')
        self.q = self.c.ask_user('r', 'owner', 'q', 'What scope should the synthetic task use?', 'dep', 'question')
        self.q = self.store.mutate('r', 'question-options', 'fixture',
            lambda tx: tx.update('question', 'q', {'options': ['one', 'two']}))
        for name in ('owner', 'dependent'):
            self.c.park('r', name, 'dep', 'user', 1, {'kind': 'version_changed'}, time.time() + 3600,
                        'close_unresolved', 'park-' + name)
        self.kwargs = {'question_id': 'q', 'question_version': self.q['version'],
                       'text': 'Synthetic actual-message fixture: retain explicit exploratory scope.\n不要推断实验条件。',
                       'message_id': 'synthetic-message-17'}

    def test_shared_original_reply_reaches_both_actual_model_packets(self):
        result = bind_and_resume(self.rt, **self.kwargs)
        self.assertEqual(result['attached_issue_ids'], ['dependent', 'owner'])
        fid = identity('reply', 'q')
        fact = self.store.get('r', 'fact', fid)
        original = json.loads(self.store.read_blob(fact['content_ref']))
        self.assertEqual(original, {'source': 'user_reply', 'question_id': 'q',
                                   'question_version': 2, 'text': self.kwargs['text']})
        for iid in ('owner', 'dependent'):
            issue = self.store.get('r', 'issue', iid)
            self.assertEqual(issue['evidence_ids'].count(fid), 1)
            work = next(w for w in self.c.ready('r') if w['issue_id'] == iid)
            agent, mode, task, options = self.rt._context(work)
            planned = agent.prepare(mode, task, **options)
            packet = agent.for_dispatch(planned, mode, task, **options)
            delivered = next(p for p in packet['delivery_pages'] if p['evidence_id'] == fid)
            self.assertEqual(json.loads(delivered['content']), original)
        self.assertNotIn(fid, self.store.get('r', 'issue', 'unrelated')['evidence_ids'])
        self.assertTrue(all(wait['status'] == 'fired' for wait in result['waits']))
        self.assertEqual(self.store.list('r', 'effect'), [])

    def test_repeated_same_message_is_idempotent_and_does_not_tick_again(self):
        result = bind_and_resume(self.rt, **self.kwargs)
        history = self.store.verify_history('r')
        with patch.object(self.c, 'tick', side_effect=AssertionError('Replay must not tick')):
            self.rt.controller = self.c
            self.assertEqual(bind_and_resume(self.rt, **self.kwargs), result)
        self.assertEqual(self.store.verify_history('r'), history)
        for changes in ({'text': 'different'}, {'question_version': 3}, {'question_id': 'another'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bind_and_resume(self.rt, **{**self.kwargs, **changes})
        self.assertEqual(self.store.verify_history('r'), history)

    def test_blank_stale_and_cancelled_questions_never_infer_or_write_reply(self):
        before = self.store.verify_history('r')
        for changes in ({'text': ''}, {'text': '  '}, {'message_id': ''}, {'question_version': 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bind_and_resume(self.rt, **{**self.kwargs, **changes})
        self.assertEqual(self.store.verify_history('r'), before)
        self.c.close_issue('r', 'owner', 'closed_unresolved', 'Synthetic cancellation', 'close-owner')
        with self.assertRaises(ValueError):
            bind_and_resume(self.rt, **self.kwargs)
        self.assertEqual(self.store.list('r', 'fact', id=identity('reply', 'q')), [])
        self.assertEqual(self.store.get('r', 'question', 'q')['status'], 'cancelled')

    def test_crash_after_original_reply_resumes_fanout_without_duplicate_reply(self):
        self.rt.controller = self.c
        original = self.c.reply
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('Synthetic crash after committed reply')
        with patch.object(self.c, 'reply', side_effect=crash), self.assertRaises(RuntimeError):
            bind_and_resume(self.rt, **self.kwargs)
        fid = identity('reply', 'q')
        self.assertIn(fid, self.store.get('r', 'issue', 'owner')['evidence_ids'])
        self.assertNotIn(fid, self.store.get('r', 'issue', 'dependent')['evidence_ids'])
        result = bind_and_resume(self.rt, **self.kwargs)
        self.assertIn(fid, self.store.get('r', 'issue', 'dependent')['evidence_ids'])
        self.assertTrue(all(wait['status'] == 'fired' for wait in result['waits']))
        self.assertEqual(len(self.store.list('r', 'fact', id=fid)), 1)
        self.store.verify_history('r')

    def test_crash_after_tick_replays_fixed_time_without_duplicate_work(self):
        self.rt.controller = self.c
        original = self.c.tick
        times = []
        def crash(run, now=None):
            times.append(now); original(run, now=now)
            raise RuntimeError('Synthetic crash after committed wake')
        with patch.object(self.c, 'tick', side_effect=crash), self.assertRaises(RuntimeError):
            bind_and_resume(self.rt, **self.kwargs)
        before = [w['id'] for w in self.c.ready('r')]
        def observe(run, now=None):
            times.append(now)
            return original(run, now=now)
        with patch.object(self.c, 'tick', side_effect=observe):
            bind_and_resume(self.rt, **self.kwargs)
        self.assertEqual(times[0], times[1])
        self.assertEqual([w['id'] for w in self.c.ready('r')], before)

    def test_dependent_closed_during_delivery_is_skipped_and_not_reopened(self):
        self.rt.controller = self.c
        original = self.c.reply
        def close_dependent(*args, **kwargs):
            result = original(*args, **kwargs)
            self.c.close_issue('r', 'dependent', 'closed_unresolved', 'Synthetic independent cancellation', 'close-dependent')
            return result
        with patch.object(self.c, 'reply', side_effect=close_dependent):
            result = bind_and_resume(self.rt, **self.kwargs)
        self.assertEqual(result['skipped_closed_issue_ids'], ['dependent'])
        self.assertEqual(self.store.get('r', 'issue', 'dependent')['status'], 'closed_unresolved')
        self.assertNotIn(identity('reply', 'q'), self.store.get('r', 'issue', 'dependent')['evidence_ids'])
        self.assertEqual(self.store.get('r', 'question', 'q')['status'], 'answered')

    def test_new_message_cannot_reanswer_a_previously_answered_question(self):
        bind_and_resume(self.rt, **self.kwargs)
        before = self.store.verify_history('r')
        with self.assertRaises(ValueError):
            bind_and_resume(self.rt, **{**self.kwargs, 'message_id': 'synthetic-message-18'})
        self.assertEqual(self.store.verify_history('r'), before)
