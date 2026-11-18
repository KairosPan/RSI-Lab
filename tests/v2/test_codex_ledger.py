"""A Codex call's cost and activity, read from what it added to its rollout.

Synthetic rollouts built from tests/v2/native_fixtures.py in a temporary Codex
home; no Codex or model process is run.
"""
import itertools
from pathlib import Path
import shutil
import tempfile
import unittest

from rsi_lab import codex_ledger
from rsi_lab.store import digest
from tests.v2.native_fixtures import (PROFILE, account, child_rollout, collab, compacted, frame, sub_agent_activity,
    sub_agent_started, tool_call, turn_context, usage_row)
from tests.v2.test_session_context import OTHER, THREAD, fixture

CHILD = '33333333-3333-3333-3333-333333333333'
# A child whose one row belongs to the parent's turn t1: counted whenever its rollout can be read.
CHILD_ROLLOUT = child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1)])


class CodexLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / 'workspace'; self.cwd.mkdir()
        self.home = self.root / 'codex-home'
        self.reset_home()

    def reset_home(self):
        shutil.rmtree(self.home, ignore_errors=True)
        (self.home / 'sessions').mkdir(parents=True)

    def day(self, folder='sessions'):
        path = self.home / folder / '2000' / '01' / '01'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, raw, folder='sessions'):
        path = self.day(folder) / f'rollout-2000-01-01T00-00-00-{THREAD}.jsonl'
        path.write_bytes(raw)
        return path

    def write_child(self, raw=CHILD_ROLLOUT, folder='sessions'):
        path = self.day(folder) / f'rollout-2000-01-01T00-00-01-{CHILD}.jsonl'
        path.write_bytes(raw)
        return path

    def write_child_twice(self):
        self.write_child(); self.write_child(folder='archived_sessions')

    def write_child_as_symlink(self):
        outside = self.root / 'elsewhere.jsonl'
        outside.write_bytes(CHILD_ROLLOUT)
        (self.day() / f'rollout-2000-01-01T00-00-01-{CHILD}.jsonl').symlink_to(outside)

    def write_child_of_another_parent(self):
        self.write_child(child_rollout(CHILD, OTHER, [('c1', 't1', 5, 1)]))

    def settle(self, **arguments):
        return codex_ledger.settle(codex_home=self.home, thread_id=THREAD, prefix_bytes=0, **arguments)

    def test_a_resumed_turn_is_charged_its_own_rows_not_the_cumulative_stream_number(self):
        base = fixture(self.cwd)
        first = base + usage_row(THREAD, 't1', 't1', 54394, 5524)
        raw = first + usage_row(THREAD, 't2', 't2', 60000, 100) + usage_row(THREAD, 't2', 't2', 42016, 5055)
        got = codex_ledger.settle(codex_home=self.home, path=self.write(raw), thread_id=THREAD, prefix_bytes=len(first),
                                  resumed=True, stream_usage={'input_tokens': 156410, 'output_tokens': 10679})
        self.assertEqual((got['source'], got['tokens'], got['requests']), ('ledger', 107171, 2))

    def test_children_are_added_through_their_root_turn(self):                      # Review Focus 5
        base = fixture(self.cwd)
        parent = base + sub_agent_started(CHILD, '/root/a') + usage_row(THREAD, 't1', 't1', 83032, 291)
        self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 22828, 37), ('c0', 't0', 999, 9)]))
        got = codex_ledger.settle(codex_home=self.home, path=self.write(parent), thread_id=THREAD, prefix_bytes=0,
                                  resumed=False, stream_usage={'input_tokens': 83032, 'output_tokens': 291})
        self.assertEqual(got['tokens'], 83032 + 291 + 22828 + 37)
        self.assertEqual(got['children'][0]['thread_id'], CHILD)

    def test_an_unreadable_child_makes_usage_unknown(self):                         # Review Focus 2
        base = fixture(self.cwd)
        parent = base + sub_agent_started(CHILD, '/root/a') + usage_row(THREAD, 't1', 't1', 10, 1)
        for prepare in (lambda: None, self.write_child_twice, self.write_child_as_symlink,
                        self.write_child_of_another_parent):
            with self.subTest(prepare=prepare.__name__):
                self.reset_home(); prepare()
                got = codex_ledger.settle(codex_home=self.home, path=self.write(parent), thread_id=THREAD,
                                          prefix_bytes=0, resumed=False,
                                          stream_usage={'input_tokens': 10, 'output_tokens': 1})
                self.assertEqual((got['source'], got['tokens']), ('unknown', None)); self.assertIn(CHILD, got['reason'])

    def test_a_resumed_thread_without_its_rollout_is_never_charged_the_stream(self):
        got = self.settle(path=None, resumed=True, stream_usage={'input_tokens': 20, 'output_tokens': 5})
        self.assertEqual((got['source'], got['tokens']), ('unknown', None)); self.assertIn('resumed', got['reason'])

    def test_a_resumed_thread_without_a_path_is_charged_from_its_own_rollout(self):
        # The strict context reader may refuse a rollout it cannot resume; its usage rows are still readable.
        first = fixture(self.cwd) + usage_row(THREAD, 't1', 't1', 54394, 5524)
        for folder in ('sessions', 'archived_sessions'):
            with self.subTest(folder=folder):
                self.reset_home()
                self.write(first + usage_row(THREAD, 't2', 't2', 102016, 5155) + compacted(None), folder)
                got = codex_ledger.settle(codex_home=self.home, path=None, thread_id=THREAD, prefix_bytes=len(first),
                                          resumed=True, stream_usage={'input_tokens': 156410, 'output_tokens': 10679})
                self.assertEqual((got['source'], got['tokens'], got['requests']), ('ledger', 102016 + 5155, 1))

    def test_a_thread_whose_rollout_is_not_found_once_keeps_the_rules_without_a_path(self):
        raw = fixture(self.cwd) + usage_row(THREAD, 't1', 't1', 10, 1)
        stream = {'input_tokens': 20, 'output_tokens': 5}

        def twice():
            self.write(raw); self.write(raw, 'archived_sessions')

        def as_symlink():
            outside = self.root / 'elsewhere.jsonl'
            outside.write_bytes(raw)
            (self.day() / f'rollout-2000-01-01T00-00-00-{THREAD}.jsonl').symlink_to(outside)
        for prepare in (twice, as_symlink):
            with self.subTest(prepare=prepare.__name__):
                self.reset_home(); prepare()
                got = self.settle(path=None, resumed=True, stream_usage=stream)
                self.assertEqual((got['source'], got['tokens']), ('unknown', None)); self.assertIn('resumed', got['reason'])
        self.reset_home()
        self.assertEqual(self.settle(path=None, resumed=False, stream_usage=stream)['source'], 'stream')

    def test_a_call_that_could_start_sub_agents_is_never_settled_from_the_stream(self):
        # The stream's usage leaves out every sub-agent the call started.
        stream = {'input_tokens': 20, 'output_tokens': 5}
        for case, prepare in (('no rollout', lambda: None), ('no rows', lambda: self.write(fixture(self.cwd)))):
            with self.subTest(case=case):
                self.reset_home()
                path = prepare()
                got = self.settle(path=path, resumed=False, stream_usage=stream, children_possible=True)
                self.assertEqual((got['source'], got['tokens']), ('unknown', None))
                self.assertIn('leaves out sub-agents', got['reason'])
                got = self.settle(path=path, resumed=False, stream_usage=stream)
                self.assertEqual((got['source'], got['tokens']), ('stream', 25))

    def test_a_turn_that_did_not_finish_is_never_charged(self):
        # A request in flight when the turn was stopped wrote no usage row, so the rows read are only a lower bound.
        parent = fixture(self.cwd) + sub_agent_started(CHILD, '/root/a') + usage_row(THREAD, 't1', 't1', 10, 1)
        stream = {'input_tokens': 20, 'output_tokens': 5}

        def rows_and_a_child():
            self.write_child()
            return self.write(parent)
        cases = {'rows and a child': (rows_and_a_child, (2, 15, 2)), 'no rollout, a fresh thread': (lambda: None, (0, 0, 0))}
        for case, (prepare, (requests, inp, out)) in cases.items():
            with self.subTest(case=case):
                self.reset_home()
                path = prepare()
                got = self.settle(path=path, resumed=False, stream_usage=stream, finished=False)
                self.assertEqual((got['source'], got['tokens']), ('unknown', None))
                self.assertEqual(got['reason'], 'the turn did not finish, and requests in flight when it was '
                                                'stopped wrote no usage rows')
                self.assertEqual((got['requests'], got['input_tokens'], got['output_tokens']), (requests, inp, out))
                self.assertNotEqual(self.settle(path=path, resumed=False, stream_usage=stream)['source'], 'unknown')
        self.reset_home()                               # The child cannot be read: both reasons are given.
        got = self.settle(path=self.write(parent), resumed=False, stream_usage=stream, finished=False)
        self.assertEqual((got['source'], got['tokens'], got['requests']), ('unknown', None, 1))
        self.assertTrue(got['reason'].startswith('the turn did not finish'))
        self.assertIn(CHILD, got['reason'])

    def test_fallbacks(self):
        base = fixture(self.cwd)
        stream = {'input_tokens': 20, 'output_tokens': 5}
        self.assertEqual(self.settle(path=None, resumed=False, stream_usage=stream)['tokens'], 25)
        self.assertEqual(self.settle(path=self.write(base), resumed=False, stream_usage=stream)['source'], 'stream')
        self.assertEqual(self.settle(path=self.write(base), resumed=True, stream_usage=stream)['source'], 'unknown')
        self.assertEqual(self.settle(path=None, resumed=False, stream_usage=None)['source'], 'unknown')

    def test_read_turn_reports_tool_inputs_account_and_profile(self):
        base = fixture(self.cwd)
        raw = base + turn_context('t1', PROFILE(self.root, self.home, self.cwd)) \
            + tool_call('c1', 'text(await tools.exec_command({cmd:"ls"}))') \
            + compacted([]) + account('1200.50', 12.5) + usage_row(THREAD, 't1', 't1', 10, 1)
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=len(base))
        self.assertEqual((turn['tool_calls'], turn['compactions'], turn['requests']), (1, 1, 1))
        self.assertEqual(turn['tool_inputs'][0]['name'], 'exec')
        self.assertEqual(turn['account']['credits_balance'], '1200.50')
        self.assertEqual(turn['profile']['file_system']['type'], 'restricted')

    def test_rows_of_another_thread_are_refused(self):
        base = fixture(self.cwd)
        with self.assertRaises(codex_ledger.LedgerUnavailable):
            codex_ledger.read_turn(self.write(base + usage_row(OTHER, 't1', 't1', 1, 1)), thread_id=THREAD,
                                   prefix_bytes=0)

    def test_tool_inputs_are_bounded_and_function_calls_count(self):
        script = 'text(await tools.exec_command({cmd:"' + 'x' * 300 + '"}))'
        raw = fixture(self.cwd) + collab('c0', 'spawn_agent', '{"task_name":"/root/a"}') + b''.join(
            tool_call(f'c{number}', script) for number in range(1, 34))
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
        self.assertEqual((turn['tool_calls'], len(turn['tool_inputs']), turn['tool_inputs_omitted']), (34, 32, 2))
        # A function call's text is its ``arguments``, which ``collab`` writes as '{}'.
        self.assertEqual(turn['tool_inputs'][0], {'name': 'spawn_agent', 'sha256': digest(b'{}'), 'excerpt': '{}'})
        self.assertEqual(turn['tool_inputs'][1], {'name': 'exec', 'sha256': digest(script.encode()),
                                                  'excerpt': script[:200]})

    def test_the_last_account_snapshot_and_turn_context_are_reported(self):
        no_limits = frame('event_msg', {'type': 'token_count', 'info': None, 'rate_limits': None})
        raw = (fixture(self.cwd) + account('1.00', 1.0) + turn_context('t1', PROFILE(self.root, self.home, self.cwd))
               + account('2.00', 2.0, plan_type='plus') + no_limits + turn_context('t1'))
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
        self.assertEqual(turn['account'], {'plan_type': 'plus', 'used_percent': 2.0, 'window_minutes': 300,
                                           'resets_at': 946702800, 'credits_balance': '2.00', 'has_credits': True,
                                           'unlimited': False})
        self.assertIsNone(turn['profile'])
        self.assertEqual((turn['rows'], turn['turn_ids'], turn['children']), ([], [], []))

    def test_only_the_child_rows_of_this_call_count(self):                          # Review Focus 5
        raw = (child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1)])
               + usage_row(THREAD, 't1', 't1', 1000, 1000)                      # the parent's, in the forked history
               + usage_row(CHILD, 'c2', 't1', 2000, 2000, session=OTHER)        # under another parent
               + usage_row(CHILD, 'c3', 't9', 3000, 3000, session=THREAD))      # of an earlier turn
        path = self.write_child(raw)
        got = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t1', 't2'])
        self.assertEqual(got, {'thread_id': CHILD, 'rollout_ref': str(path.resolve()), 'rollout_sha256': digest(raw),
                               'requests': 1, 'input_tokens': 5, 'output_tokens': 1, 'cached_input_tokens': 0})

    def test_children_are_taken_from_every_sub_agent_activity_of_the_turn(self):
        # A sub-agent started in an earlier turn and addressed again in this one writes rows under this turn.
        first = fixture(self.cwd) + sub_agent_started(CHILD, '/root/a') + usage_row(THREAD, 't1', 't1', 10, 1)
        raw = (first + sub_agent_activity(CHILD, '/root/a', 'interacted') + usage_row(THREAD, 't2', 't2', 20, 2)
               + sub_agent_activity(CHILD, '/root/a', 'completed'))
        self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1), ('c2', 't2', 7, 3)]))
        got = codex_ledger.settle(codex_home=self.home, path=self.write(raw), thread_id=THREAD,
                                  prefix_bytes=len(first), resumed=True, stream_usage=None)
        self.assertEqual((got['source'], got['tokens'], got['requests']), ('ledger', 20 + 2 + 7 + 3, 2))
        self.assertEqual(got['turn']['children'], [{'thread_id': CHILD, 'agent_path': '/root/a'}])

    def test_a_child_started_twice_is_counted_once(self):
        parent = fixture(self.cwd) + sub_agent_started(CHILD, '/root/a') * 2 + usage_row(THREAD, 't1', 't1', 10, 1)
        self.write_child()
        got = self.settle(path=self.write(parent), resumed=False, stream_usage=None)
        self.assertEqual((got['source'], got['tokens'], got['requests']), ('ledger', 10 + 1 + 5 + 1, 2))
        self.assertEqual(got['turn']['children'], [{'thread_id': CHILD, 'agent_path': '/root/a'}])

    def test_an_unsafe_child_id_is_never_looked_up(self):
        unsafe = '?' + CHILD[1:]                        # as a file pattern it would find the child's rollout
        self.write_child(child_rollout(unsafe, THREAD, [('c1', 't1', 5, 1)]))
        parent = fixture(self.cwd) + sub_agent_started(unsafe, '/root/a') + usage_row(THREAD, 't1', 't1', 10, 1)
        got = self.settle(path=self.write(parent), resumed=False, stream_usage={'input_tokens': 10, 'output_tokens': 1})
        self.assertEqual((got['source'], got['tokens']), ('unknown', None)); self.assertIn(unsafe, got['reason'])

    def test_a_rollout_that_cannot_be_read_makes_usage_unknown(self):
        base = fixture(self.cwd)
        stream = {'input_tokens': 10, 'output_tokens': 1}
        row = usage_row(THREAD, 't1', 't1', 10, 1)
        cases = {'a row of another thread': (base + usage_row(OTHER, 't1', 't1', 1, 1), 0),
                 'a row of another session': (base + usage_row(THREAD, 't1', 't1', 1, 1, session=OTHER), 0),
                 'a cut final record': (base + row[:-1], 0), 'a prefix inside a record': (base + row, len(base) - 1),
                 'a prefix past the end': (base, len(base) + 1)}
        for (case, (raw, prefix)), resumed in itertools.product(cases.items(), (False, True)):
            with self.subTest(case=case, resumed=resumed):
                with self.assertRaises(codex_ledger.LedgerUnavailable):
                    codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=prefix)
                got = codex_ledger.settle(codex_home=self.home, path=self.write(raw), thread_id=THREAD,
                                          prefix_bytes=prefix, resumed=resumed, stream_usage=stream)
                self.assertEqual((got['source'], got['tokens'], got['turn']), ('unknown', None, None))


if __name__ == '__main__':
    unittest.main()
