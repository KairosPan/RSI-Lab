"""A Codex call's cost and activity, read from what it added to its rollout.

Synthetic rollouts built from tests/v2/native_fixtures.py in a temporary Codex
home; no Codex or model process is run.
"""
import base64
import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import urllib.parse

from rsi_lab import codex_ledger, native
from rsi_lab.store import digest, encode
from tests.v2.native_fixtures import (MCP_RESULT, PNG, PROFILE, ROLE_LESS_PARTS, ROLES, TERMINAL,
    TERMINAL_AND_SUBAGENTS, VIEW_IMAGE, WITH_SKILLS, account, child_rollout, collab, compacted, frame, mcp_call, skills_list, skills_list_text,
    sub_agent_activity, sub_agent_meta, sub_agent_started, tool_call, turn_context, usage_row, viewed_image,
    web_search)
from tests.v2.test_session_context import OTHER, THREAD, fixture

CHILD = '33333333-3333-3333-3333-333333333333'
EARLIER_CHILD = '44444444-4444-4444-4444-444444444444'
# A child whose one row belongs to the parent's turn t1: counted whenever its rollout can be read.
CHILD_ROLLOUT = child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1)])
ARGS = json.dumps({'query': 'CCO'}, ensure_ascii=False, sort_keys=True)
# A viewed JPEG of 3,003 bytes; PNG is one of 68.
JPEG = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xff' + bytes(3000)).decode()
NO_IMAGES = {'paths': [], 'paths_omitted': 0, 'count': 0, 'bytes': 0}
OUTSIDE = 'Codex showed images outside the folders this role may read: '
# The one skill WITH_SKILLS declares, as a line of Codex's skills list under its first root.
DECLARED = ('plot-check', 'Checks a plot before it is reported.', 'r0/plot-check/SKILL.md')
# A skill nobody declared, in the owner's own folder: its name must never reach a receipt or an error.
SECRET = ('secret-notes', 'Notes of the owner.', 'r1/secret-notes/SKILL.md')
OWNERS = '/u/.agents/skills'
NOT_DECLARED = 'Codex showed skills other than the declared ones: '
NOT_SKILLED = 'Codex showed a skills list to a role without skills: '
WITH_SKILLS_AND_SUBAGENTS = {**WITH_SKILLS, 'subagents': {'max_concurrent': 3}}


def viewed_at(path, *, turn='t1', thread=THREAD):
    """An ``ImageView`` event whose ``path`` is ``path`` exactly as given, without the image tool's output. A path
    that UTF-8 cannot carry, such as one holding a lone surrogate, is written as a JSON escape."""
    return (json.dumps({'timestamp': '2000-01-01T00:00:00Z', 'type': 'event_msg', 'payload': {
        'type': 'item_completed', 'thread_id': thread, 'turn_id': turn,
        'item': {'type': 'ImageView', 'id': 'exec-view', 'path': path}}}) + '\n').encode()


def file_url(path):
    """``path`` as Codex writes it in an ``ImageView`` item: a ``file://`` URL, percent-encoded."""
    return 'file://' + urllib.parse.quote(str(path))


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
                               'requests': 1, 'input_tokens': 5, 'output_tokens': 1, 'cached_input_tokens': 0,
                               'role': None, 'forked_history': True, 'model': None, 'effort': None, 'searches': [],
                               'searches_omitted': 0, 'tool_server_calls': [], 'tool_server_calls_omitted': 0,
                               'images_viewed': NO_IMAGES, 'image_view_paths': [], 'skills_lists': []})

    def test_children_are_taken_from_every_sub_agent_activity_of_the_turn(self):
        # A sub-agent started in an earlier turn and addressed again in this one writes rows under this turn.
        first = fixture(self.cwd) + sub_agent_started(CHILD, '/root/a') + usage_row(THREAD, 't1', 't1', 10, 1)
        raw = (first + sub_agent_activity(CHILD, '/root/a', 'interacted') + usage_row(THREAD, 't2', 't2', 20, 2)
               + sub_agent_activity(CHILD, '/root/a', 'completed'))
        self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1), ('c2', 't2', 7, 3)]))
        got = codex_ledger.settle(codex_home=self.home, path=self.write(raw), thread_id=THREAD,
                                  prefix_bytes=len(first), resumed=True, stream_usage=None)
        self.assertEqual((got['source'], got['tokens'], got['requests']), ('ledger', 20 + 2 + 7 + 3, 2))
        self.assertEqual(got['turn']['children'], [{'thread_id': CHILD, 'agent_path': '/root/a',
                                                    'started_in_call': False, 'model': None, 'effort': None}])

    def test_a_child_started_twice_is_counted_once(self):
        parent = fixture(self.cwd) + sub_agent_started(CHILD, '/root/a') * 2 + usage_row(THREAD, 't1', 't1', 10, 1)
        self.write_child()
        got = self.settle(path=self.write(parent), resumed=False, stream_usage=None)
        self.assertEqual((got['source'], got['tokens'], got['requests']), ('ledger', 10 + 1 + 5 + 1, 2))
        self.assertEqual(got['turn']['children'], [{'thread_id': CHILD, 'agent_path': '/root/a',
                                                    'started_in_call': True, 'model': None, 'effort': None}])

    def test_children_say_whether_this_call_started_them(self):
        raw = (fixture(self.cwd) + sub_agent_started(CHILD, '/root/a', model='m', effort='low')
               + sub_agent_activity(CHILD, '/root/a', 'completed')            # null model and effort
               + sub_agent_activity(EARLIER_CHILD, '/root/old', 'interacted') + usage_row(THREAD, 't1', 't1', 10, 1))
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
        self.assertEqual(turn['children'], [
            {'thread_id': CHILD, 'agent_path': '/root/a', 'started_in_call': True, 'model': 'm', 'effort': 'low'},
            {'thread_id': EARLIER_CHILD, 'agent_path': '/root/old', 'started_in_call': False, 'model': None,
             'effort': None}])
        # A model or effort that is not text is not recorded.
        raw = fixture(self.cwd) + sub_agent_started(CHILD, '/root/a', model=7, effort=['low'])
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
        self.assertEqual(turn['children'], [{'thread_id': CHILD, 'agent_path': '/root/a', 'started_in_call': True,
                                             'model': None, 'effort': None}])

    def test_child_names_its_role_and_whether_it_inherited_history(self):
        for role, forked in (('challenge', False), ('local', True), (None, False)):
            with self.subTest(role=role, forked=forked):
                self.reset_home()
                self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1)], role=role, forked=forked))
                child = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t1'])
                self.assertEqual((child['role'], child['forked_history']), (role, forked))

    def test_a_role_that_is_not_text_is_none_and_the_child_still_counts(self):
        parent = fixture(self.cwd) + sub_agent_started(CHILD, '/root/a') + usage_row(THREAD, 't1', 't1', 10, 1)
        for written in ('', 7, ['challenge']):
            with self.subTest(written=written):
                self.reset_home()
                self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1)], role=written))
                got = self.settle(path=self.write(parent), resumed=False, stream_usage=None)
                self.assertEqual((got['source'], got['tokens']), ('ledger', 10 + 1 + 5 + 1))
                self.assertIsNone(got['children'][0]['role'])

    def test_child_searches_are_this_calls_only(self):
        raw = (child_rollout(CHILD, THREAD, [('c0', 't0', 3, 1), ('c1', 't1', 5, 1)])
               + web_search('earlier query', 'c0')                             # the sub-agent's earlier turn
               + web_search('first query', 'c1') + web_search('second query', 'c1', shape='WebSearch')
               + frame('event_msg', {'type': 'item_completed', 'turn_id': 'c1', 'item': {
                   'type': 'Extension', 'kind': 'other.tool', 'query': 'not a search'}})
               + frame('event_msg', {'type': 'item_completed', 'turn_id': 'c1', 'item': {
                   'type': 'WebSearch', 'query': None}}))
        self.write_child(raw)
        child = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t1'])
        self.assertEqual((child['searches'], child['searches_omitted']), (['first query', 'second query'], 0))

    def test_more_searches_than_the_limit_are_counted_not_kept(self):
        queries = ['query %02d ' % number + 'x' * 300 for number in range(23)]
        self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1)])
                         + b''.join(web_search(query, 'c1') for query in queries))
        child = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t1'])
        self.assertEqual(codex_ledger.MAX_SEARCHES, 20)
        self.assertEqual((len(child['searches']), child['searches_omitted']), (20, 3))
        self.assertEqual(child['searches'], [query[:200] for query in queries[:20]])

    def test_a_child_addressed_again_keeps_its_role(self):                    # Review Focus 3
        first = (fixture(self.cwd) + sub_agent_started(CHILD, '/root/a', model='m', effort='low')
                 + usage_row(THREAD, 't1', 't1', 10, 1))
        raw = first + sub_agent_activity(CHILD, '/root/a', 'interacted') + usage_row(THREAD, 't2', 't2', 20, 2)
        self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1), ('c2', 't2', 7, 3)], role='challenge',
                                       forked=False)
                         + web_search('earlier query', 'c1') + web_search('this query', 'c2'))
        got = codex_ledger.settle(codex_home=self.home, path=self.write(raw), thread_id=THREAD,
                                  prefix_bytes=len(first), resumed=True, stream_usage=None)
        self.assertEqual((got['source'], got['tokens']), ('ledger', 20 + 2 + 7 + 3))
        [entry] = got['children']
        self.assertEqual((entry['role'], entry['started_in_call']), ('challenge', False))
        self.assertEqual(entry['requests'], 1)
        self.assertEqual((entry['input_tokens'], entry['output_tokens']), (7, 3))
        self.assertEqual((entry['agent_path'], entry['model'], entry['effort'], entry['forked_history']),
                         ('/root/a', None, None, False))
        self.assertEqual(entry['searches'], ['this query'])

    def test_a_child_addressed_again_records_the_model_and_effort_of_its_own_turn(self):
        first = (fixture(self.cwd) + sub_agent_started(CHILD, '/root/a', model='m', effort='low')
                 + usage_row(THREAD, 't1', 't1', 10, 1))
        raw = first + sub_agent_activity(CHILD, '/root/a', 'interacted') + usage_row(THREAD, 't2', 't2', 20, 2)
        self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1), ('c2', 't2', 7, 3), ('c3', 't9', 1, 1)])
                         + turn_context('c1', model='earlier-model', effort='low')      # an earlier call's turn
                         + turn_context('c2', model='first-model', effort='medium')
                         + turn_context('c2', model='child-model', effort='high')       # the last of this call's
                         + turn_context('c3', model='other-model', effort='xhigh'))     # a turn not counted here
        got = codex_ledger.settle(codex_home=self.home, path=self.write(raw), thread_id=THREAD,
                                  prefix_bytes=len(first), resumed=True, stream_usage=None)
        [entry] = got['children']
        self.assertEqual((entry['started_in_call'], entry['model'], entry['effort']), (False, 'child-model', 'high'))
        self.assertEqual(got['turn']['children'][0]['model'], None)          # The lead's records name none.
        child = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t2'])
        self.assertEqual((child['model'], child['effort']), ('child-model', 'high'))

    def test_a_child_without_its_own_turn_context_keeps_the_started_items_model_and_effort(self):
        parent = (fixture(self.cwd) + sub_agent_started(CHILD, '/root/a', model='m', effort='low')
                  + usage_row(THREAD, 't1', 't1', 10, 1))
        cases = {'none at all': (b'', ('m', 'low')),
                 'only an earlier turn': (turn_context('c0', model='earlier-model', effort='high'), ('m', 'low')),
                 'a model that is not text': (turn_context('c1', model=7, effort='high'), ('m', 'high')),
                 'no effort': (turn_context('c1', model='child-model', effort=None), ('child-model', 'low'))}
        for case, (written, expected) in cases.items():
            with self.subTest(case=case):
                self.reset_home()
                self.write_child(child_rollout(CHILD, THREAD, [('c0', 't0', 3, 1), ('c1', 't1', 5, 1)]) + written)
                got = self.settle(path=self.write(parent), resumed=False, stream_usage=None)
                [entry] = got['children']
                self.assertEqual((entry['started_in_call'], entry['model'], entry['effort']), (True, *expected))

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

    def test_the_lead_records_its_tool_server_calls(self):
        # A call before the prefix belongs to an earlier call of the session.
        first = (fixture(self.cwd) + mcp_call('exec-1110', 'chem', 'lookup', {'query': 'C'}, turn='t0')
                 + usage_row(THREAD, 't0', 't0', 10, 1))
        raw = (first + mcp_call('exec-1111', 'chem', 'lookup', {'query': 'CCO'}, turn='t1')
               + mcp_call('exec-1112', 'papers', 'fetch', {'doi': '10.0000/invented'}, turn='t1', status='failed',
                          result=None)
               + usage_row(THREAD, 't1', 't1', 10, 1))
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=len(first))
        self.assertEqual(turn['tool_server_calls'][0], {
            'server': 'chem', 'tool': 'lookup', 'status': 'completed', 'call_id': 'exec-1111',
            'arguments_sha256': hashlib.sha256(ARGS.encode()).hexdigest(), 'arguments_excerpt': ARGS,
            'result_bytes': len(encode(MCP_RESULT)), 'duration_seconds': 0.000439666})
        self.assertEqual((turn['tool_server_calls'][1]['result_bytes'], turn['tool_server_calls_omitted']), (None, 0))
        self.assertEqual([(call['call_id'], call['server'], call['status']) for call in turn['tool_server_calls']],
                         [('exec-1111', 'chem', 'completed'), ('exec-1112', 'papers', 'failed')])
        self.assertEqual((turn['images_viewed'], turn['image_view_paths']), (NO_IMAGES, []))

    def test_more_calls_than_the_limit_are_counted_not_kept(self):
        long = {'query': 'C' * 300}
        raw = fixture(self.cwd) + mcp_call('exec-long', 'chem', 'lookup', long, turn='t1') + b''.join(
            mcp_call('exec-%02d' % number, 'chem', 'lookup', {'query': 'CCO'}, turn='t1') for number in range(34))
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
        self.assertEqual(codex_ledger.MAX_TOOL_SERVER_CALLS, 32)
        self.assertEqual((len(turn['tool_server_calls']), turn['tool_server_calls_omitted']), (32, 3))
        self.assertEqual([call['call_id'] for call in turn['tool_server_calls']],
                         ['exec-long'] + ['exec-%02d' % number for number in range(31)])
        text = json.dumps(long, ensure_ascii=False, sort_keys=True)
        self.assertEqual({key: turn['tool_server_calls'][0][key] for key in ('arguments_sha256', 'arguments_excerpt')},
                         {'arguments_sha256': hashlib.sha256(text.encode()).hexdigest(),
                          'arguments_excerpt': text[:200]})
        self.assertTrue(all(len(call['arguments_excerpt']) <= 200 for call in turn['tool_server_calls']))

    def test_a_tool_server_call_field_of_the_wrong_type_is_none(self):
        # Arguments that are already text are hashed as they are.
        odd = {'type': 'McpToolCall', 'id': 7, 'server': ['chem'], 'tool': None, 'arguments': '{"query":"CCO"}',
               'status': 1, 'result': 'similarity 0.42', 'duration': [0, 439666]}
        raw = fixture(self.cwd) + frame('event_msg', {'type': 'item_completed', 'thread_id': THREAD, 'turn_id': 't1',
                                                      'item': odd})
        [call] = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)['tool_server_calls']
        self.assertEqual(call, {'server': None, 'tool': None, 'status': None, 'call_id': None,
                                'arguments_sha256': hashlib.sha256(b'{"query":"CCO"}').hexdigest(),
                                'arguments_excerpt': '{"query":"CCO"}', 'result_bytes': None,
                                'duration_seconds': None})
        durations = {(1, 500000000): 1.5, (True, 0): None, (0, 1.5): None, (-1, 0): None, (0, 10 ** 9): None,
                     ('0', 0): None}
        for (secs, nanos), seconds in durations.items():
            with self.subTest(secs=secs, nanos=nanos):
                raw = fixture(self.cwd) + mcp_call('exec-1', 'chem', 'lookup', {}, turn='t1', duration=(secs, nanos))
                [call] = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)['tool_server_calls']
                self.assertEqual(call['duration_seconds'], seconds)

    def test_viewed_images_are_listed_with_their_count_and_bytes(self):
        raw = (fixture(self.cwd) + viewed_image('exec-2221', '/w/plot.png', PNG, turn='t1')
               + viewed_image('exec-2222', '/w/fit.jpg', JPEG, turn='t1') + usage_row(THREAD, 't1', 't1', 10, 1))
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
        self.assertEqual(turn['images_viewed'], {'paths': ['file:///w/plot.png', 'file:///w/fit.jpg'],
                                                 'paths_omitted': 0, 'count': 2, 'bytes': 68 + 3003})
        self.assertEqual((turn['tool_server_calls'], turn['tool_server_calls_omitted']), ([], 0))

    def test_more_viewed_images_than_the_limit_are_counted_not_kept(self):
        raw = fixture(self.cwd) + b''.join(viewed_image('exec-%02d' % number, '/w/plot-%02d.png' % number, PNG,
                                                        turn='t1') for number in range(34))
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
        self.assertEqual(codex_ledger.MAX_IMAGE_PATHS, 32)
        self.assertEqual(turn['images_viewed'], {'paths': ['file:///w/plot-%02d.png' % number for number in range(32)],
                                                 'paths_omitted': 2, 'count': 34, 'bytes': 34 * 68})
        # Every path is kept for the check of the folders it lies in, never for a receipt.
        self.assertEqual(len(turn['image_view_paths']), 34)

    def test_an_image_in_a_result_between_agents_is_not_counted_as_viewed(self):
        # Codex returns a viewed image only in its exec tool's output; a function_call_output carries results between
        # agents, and the rollout reader refuses an image there.
        between = frame('response_item', {'type': 'function_call_output', 'call_id': 'c9', 'output': [
            {'type': 'input_text', 'text': 'Found it.'}, {'type': 'input_image', 'image_url': JPEG}]})
        raw = fixture(self.cwd) + between + viewed_image('exec-2221', '/w/plot.png', PNG, turn='t1')
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=0)
        self.assertEqual(turn['images_viewed'], {'paths': ['file:///w/plot.png'], 'paths_omitted': 0, 'count': 1,
                                                 'bytes': 68})
        self.write_child(sub_agent_meta(CHILD, THREAD) + turn_context('c1') + between.replace(b'c9', b'c8')
                         + usage_row(CHILD, 'c1', 't1', 5, 1, session=THREAD))
        child = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t1'])
        self.assertEqual(child['images_viewed'], NO_IMAGES)

    def test_a_child_records_only_this_calls_turns(self):
        def turn_of(turn, tag, thread=CHILD):
            return (turn_context(turn) + mcp_call('exec-' + tag, 'chem', 'lookup', {'query': tag}, turn=turn,
                                                  thread=thread)
                    + viewed_image('exec-v' + tag, '/w/%s.png' % tag, PNG, turn=turn, thread=thread))
        # Forked from the lead's history, which holds the lead's own records of t1; then an earlier call's turn c0.
        history = frame('session_meta', {'id': THREAD, 'cwd': str(self.cwd)}) + turn_of('t1', 'lead', THREAD)
        self.write_child(sub_agent_meta(CHILD, THREAD, forked=True) + history + turn_of('c0', 'c0')
                         + turn_of('c1', 'c1') + usage_row(CHILD, 'c0', 't0', 3, 1, session=THREAD)
                         + usage_row(CHILD, 'c1', 't1', 5, 1, session=THREAD))
        child = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t1'])
        self.assertEqual(([c['call_id'] for c in child['tool_server_calls']], child['images_viewed']['count']),
                         (['exec-c1'], 1))
        self.assertEqual(child['images_viewed'], {'paths': ['file:///w/c1.png'], 'paths_omitted': 0, 'count': 1,
                                                  'bytes': 68})
        self.assertEqual((child['tool_server_calls_omitted'], child['image_view_paths']), (0, ['file:///w/c1.png']))

    def settle_call(self, records, settings, *, earlier=None, turn='t1', rows=True, finished=True, **options):
        """``settle_call`` of a session call whose turn adds ``records`` and, with ``rows``, one usage row of ``turn``
        to the base rollout; its folder holds an empty stream. ``earlier``, the records of a committed turn before
        it, makes it a resumed call whose prefix ends with them. ``finished`` says whether the turn reached its
        terminal event."""
        folder = self.root / 'call'
        folder.mkdir(exist_ok=True)
        (folder / 'events.jsonl').write_text('')
        first = fixture(self.cwd) + (earlier or b'')
        session = {'id': 'session'} if earlier is None else {
            'id': 'session', 'provider_thread_id': THREAD,
            'context_manifest': {'native_prefix': {'prefix_bytes': len(first)}}}
        path = self.write(first + records + (usage_row(THREAD, turn, turn, 10, 1) if rows else b''))
        return codex_ledger.settle_call(folder, settings, session=session, codex_home=self.home, path=path,
                                        thread_id=THREAD, stream_usage=None, run_root=self.root, finished=finished,
                                        **options)

    def test_every_viewed_image_must_lie_in_a_folder_the_role_may_read(self):
        readable, elsewhere = self.root / 'data', self.root / 'elsewhere'
        readable.mkdir(); elsewhere.mkdir()
        (elsewhere / 'secret.png').write_bytes(b'')
        (self.cwd / 'link.png').symlink_to(elsewhere / 'secret.png')
        settings = {**VIEW_IMAGE, 'terminal': {'readable_roots': [str(readable)]}}
        inside = file_url(self.cwd / 'plot.png')
        cases = {'in the workspace': ([inside], None),
                 'percent-encoded in the workspace': ([file_url(self.cwd / 'my plot.png')], None),
                 'a plain absolute path in the workspace': ([str(self.cwd / 'plot.png')], None),
                 'in a readable root': ([file_url(readable / 'figures' / 'a.png')], None),
                 'outside both': ([inside, file_url(elsewhere / 'secret.png')], '1 of 2'),
                 'a symlink in the workspace to outside': ([file_url(self.cwd / 'link.png')], '1 of 1'),
                 'a percent-encoded way out': ([file_url(self.cwd) + '/%2E%2E/elsewhere/secret.png'], '1 of 1'),
                 'a plain path is not percent-decoded': (
                     [str(elsewhere) + '/%2E%2E/' + self.cwd.name + '/plot.png'], '1 of 1'),
                 'another scheme': (['https://images.example.org/plot.png'], '1 of 1'),
                 'a file URL with a host': (['file://images.example.org' + urllib.parse.quote(str(self.cwd))
                                             + '/plot.png'], '1 of 1'),
                 'a file URL with a fragment': ([inside + '#/../../elsewhere/secret.png'], '1 of 1'),
                 'a relative path': (['plot.png'], '1 of 1'), 'not text': ([7], '1 of 1'),
                 'a lone surrogate': ([inside[:-len('.png')] + '\ud800.png',
                                       str(self.cwd / 'plot\ud800.png')], '2 of 2'),
                 'a percent-encoded null byte': ([file_url(self.cwd) + '/plot%00.png'], '1 of 1'),
                 'the last of more than are listed': ([inside] * 33 + [file_url(elsewhere / 'secret.png')],
                                                      '1 of 34')}
        for case, (paths, counts) in cases.items():
            with self.subTest(case=case):
                got = self.settle_call(b''.join(viewed_at(path) for path in paths), settings, workspace=self.cwd)
                self.assertEqual(got['images_error'], None if counts is None else OUTSIDE + counts)
                self.assertEqual(got['receipt']['native_activity']['images_viewed']['paths'],
                                 [path if isinstance(path, str) else None for path in paths[:32]])
        # Without the workspace no folder is known, and every viewed image counts as outside.
        self.assertEqual(self.settle_call(viewed_at(inside) * 2, settings)['images_error'], OUTSIDE + '2 of 2')
        self.assertIsNone(self.settle_call(b'', settings)['images_error'])

    def test_a_sub_agents_viewed_images_are_checked_too(self):
        settings = {**VIEW_IMAGE, 'subagents': {'max_concurrent': 3}}
        outside = file_url(self.root / 'elsewhere' / 'secret.png')
        # The sub-agent's earlier turn c0 is not this call's and is not checked.
        self.write_child(child_rollout(CHILD, THREAD, [('c0', 't0', 3, 1), ('c1', 't1', 5, 1)])
                         + viewed_at(outside, turn='c0', thread=CHILD)
                         + viewed_at(file_url(self.cwd / 'plot.png'), turn='c1', thread=CHILD)
                         + viewed_at(outside, turn='c1', thread=CHILD))
        got = self.settle_call(sub_agent_started(CHILD, '/root/a') + viewed_at(file_url(self.cwd / 'fit.png')),
                               settings, workspace=self.cwd)
        self.assertEqual(got['images_error'], OUTSIDE + '1 of 3')
        [entry] = got['receipt']['child_threads']
        self.assertEqual(entry['images_viewed']['paths'], [file_url(self.cwd / 'plot.png'), outside])
        self.assertNotIn('image_view_paths', entry)

    @unittest.skipIf(os.geteuid() == 0, 'root searches a folder whatever its mode')
    def test_a_path_that_cannot_be_looked_at_lies_outside_and_a_deleted_one_where_it_was(self):
        elsewhere, locked = self.root / 'elsewhere', self.cwd / 'locked'
        elsewhere.mkdir(); locked.mkdir()
        (elsewhere / 'secret.png').write_bytes(b'')
        (locked / 'link.png').symlink_to(elsewhere / 'secret.png')
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o700)
        # Resolved without looking into the folder, the link would seem to lie in the workspace.
        got = self.settle_call(viewed_at(file_url(locked / 'link.png')), VIEW_IMAGE, workspace=self.cwd)
        self.assertEqual(got['images_error'], OUTSIDE + '1 of 1')
        # A plot deleted after it was viewed, with its folder, still lies where it was.
        got = self.settle_call(viewed_at(file_url(self.cwd / 'plots' / 'gone.png')), VIEW_IMAGE, workspace=self.cwd)
        self.assertIsNone(got['images_error'])

    def test_a_sub_agent_addressed_again_has_its_images_recorded_and_checked(self):
        settings = {**VIEW_IMAGE, 'subagents': {'max_concurrent': 3}}
        outside = str(self.root / 'elsewhere' / 'secret.png')
        # Started in the session's earlier turn t1, addressed again in this call's turn t2. What it viewed in t1 was
        # checked with that call.
        self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1), ('c2', 't2', 7, 3)])
                         + turn_context('c1') + viewed_image('exec-v1', str(self.root / 'old.png'), PNG, turn='c1',
                                                             thread=CHILD)
                         + turn_context('c2') + viewed_image('exec-v2', outside, PNG, turn='c2', thread=CHILD))
        got = self.settle_call(sub_agent_activity(CHILD, '/root/a', 'interacted'), settings, workspace=self.cwd,
                               earlier=sub_agent_started(CHILD, '/root/a') + usage_row(THREAD, 't1', 't1', 10, 1),
                               turn='t2')
        [entry] = got['receipt']['child_threads']
        self.assertFalse(entry['started_in_call'])
        self.assertEqual(entry['images_viewed'], {'paths': ['file://' + outside], 'paths_omitted': 0, 'count': 1,
                                                  'bytes': 68})
        self.assertEqual(got['images_error'], OUTSIDE + '1 of 1')

    def test_a_sub_agent_that_was_not_read_makes_a_viewing_call_invalid(self):
        settings = {**VIEW_IMAGE, 'subagents': {'max_concurrent': 3}}
        without = {**TERMINAL, 'subagents': {'max_concurrent': 3}}
        unchecked = "Codex's sub-agents could not be checked for images outside the folders this role may read: "
        one = sub_agent_started(CHILD, '/root/a')
        outside = viewed_at(file_url(self.root / 'elsewhere' / 'secret.png'))
        # Each case: the turn's records, whether it added usage rows, whether CHILD's rollout exists (EARLIER_CHILD
        # never has one), and how many of the sub-agents named were not read.
        cases = {'its rollout cannot be read': (one, True, False, '1 of 1'),
                 'the turn added no usage rows, so none was looked for': (one, False, True, '1 of 1'),
                 'one of two cannot be read': (one + sub_agent_started(EARLIER_CHILD, '/root/b'), True, True,
                                               '1 of 2')}
        for case, (records, rows, readable, counts) in cases.items():
            with self.subTest(case=case):
                self.reset_home()
                if readable:
                    self.write_child()
                got = self.settle_call(records, settings, rows=rows, workspace=self.cwd)
                self.assertEqual(got['images_error'], unchecked + counts)
                # After the images seen outside, when there are some.
                got = self.settle_call(records + outside, settings, rows=rows, workspace=self.cwd)
                self.assertEqual(got['images_error'], OUTSIDE + '1 of 1; ' + unchecked + counts)
                # The same call of a role without image viewing gets no images_error.
                self.assertIsNone(self.settle_call(records + outside, without, rows=rows,
                                                   workspace=self.cwd)['images_error'])
        # Every sub-agent the turn names was read: nothing is reported.
        self.reset_home(); self.write_child()
        self.assertIsNone(self.settle_call(sub_agent_started(CHILD, '/root/a'), settings,
                                           workspace=self.cwd)['images_error'])

    def test_a_role_without_image_viewing_gets_no_images_error(self):
        outside = viewed_at(file_url(self.root / 'elsewhere' / 'secret.png'))
        for settings in (TERMINAL, {**TERMINAL, 'subagents': {'max_concurrent': 3}}):
            for options in ({'workspace': self.cwd}, {}):
                with self.subTest(settings=settings, options=options):
                    got = self.settle_call(outside, settings, **options)
                    self.assertIsNone(got['images_error'])
                    self.assertNotIn('images_viewed', got['receipt']['native_activity'])

    def ws_root(self):
        """The workspace's skills folder as Codex writes it among the roots of a skills list."""
        return str(self.cwd / '.agents' / 'skills')

    def declared_list(self):
        return skills_list([self.ws_root()], DECLARED)

    def test_a_list_is_parsed(self):
        text = skills_list_text([self.ws_root()], DECLARED, ('odd', 'Shows (file: x) inside.', 'r0/odd/SKILL.md'))
        parsed = codex_ledger.parse_skills_list(text)
        self.assertEqual(parsed['roots'], {'r0': self.ws_root()})
        self.assertEqual(parsed['skills'][0], {'name': 'plot-check', 'description': 'Checks a plot before it is '
                                               'reported.', 'root': 'r0', 'file': 'plot-check/SKILL.md'})
        # The description is greedy: the last "(file: …)" is the skill's file.
        self.assertEqual(parsed['skills'][1], {'name': 'odd', 'description': 'Shows (file: x) inside.', 'root': 'r0',
                                               'file': 'odd/SKILL.md'})
        # A list of no skills is readable; it lists none.
        self.assertEqual(codex_ledger.parse_skills_list(skills_list_text([self.ws_root()]))['skills'], [])
        for broken in (text.replace('### Skill roots', '### Roots'), text.replace('r0/odd', 'r1/odd'),
                       text.replace('- odd: ', '- odd '), text.replace('</skills_instructions>', ''),
                       text.replace('### Available skills', '### Skills found'), skills_list_text([], DECLARED),
                       text.replace('- `r0` = ', '- r0 = '), text.replace('<skills_instructions>', '<skills>'),
                       text.replace('### Available skills', '- `r0` = `/u/.codex/skills`\n### Available skills')):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                codex_ledger.parse_skills_list(broken)
        # A line naming a skill's file anywhere but as a skill line of "Available skills" may be a skill Codex listed
        # in a form not foreseen: the list cannot be read, never compared as clean.
        declared = skills_list_text([self.ws_root()], DECLARED)
        line = 'secret-notes: Notes of the owner. (file: r0/secret-notes/SKILL.md)'
        for probe in ('  - ' + line, '* ' + line, '\t- ' + line, '#### More\n- ' + line, line,
                      '#### (file: r0/secret-notes/SKILL.md)'):
            with self.subTest(probe=probe):
                text = declared.replace('\n</skills_instructions>', '\n' + probe + '\n</skills_instructions>')
                with self.assertRaises(ValueError):
                    codex_ledger.parse_skills_list(text)
                self.assertEqual(codex_ledger.compare_skills([text], declared=['plot-check'], workspace=self.cwd),
                                 {'shown': [], 'unexpected': 0, 'folders': [], 'missing': [], 'unreadable': 1})
        for place in ('## Skills\n', '### Skill roots\n'):
            with self.subTest(place=place), self.assertRaises(ValueError):
                codex_ledger.parse_skills_list(declared.replace(place, place + '- ' + line + '\n'))

    def test_the_skills_lists_are_read_where_codex_wrote_them(self):
        first, second = skills_list_text([self.ws_root()], DECLARED), skills_list_text([self.ws_root()])

        def message(role, *texts):
            return frame('response_item', {'type': 'message', 'role': role,
                                            'content': [{'type': 'input_text', 'text': text} for text in texts]})
        # Not lists: another role, a part that does not hold the tag, a part that is not text.
        others = (message('user', first) + message('developer', 'Permissions.')
                  + frame('response_item', {'type': 'message', 'role': 'developer', 'content': [
                      {'type': 'input_text', 'text': 7}, {'type': 'input_image', 'image_url': first}]}))
        # Lists: every text part of a developer message that holds the tag anywhere, each on its own, wherever it
        # stands: the role's instructions first, then the list, then the permissions; two lists in one message; a part
        # with the tag after white space or after other text, which cannot be read; an output_text part.
        hidden = ' ' + first, 'Before. ' + second
        lists = (message('developer', 'You check plots.', second, '<permissions instructions>')
                 + message('developer', first, second) + message('developer', *hidden)
                 + frame('response_item', {'type': 'message', 'role': 'developer', 'content': [
                     {'type': 'output_text', 'text': first}]}))
        prefix = (fixture(self.cwd) + message('developer', first)
                  + b'not a record ' + codex_ledger.SKILLS_LIST_START.encode() + b'\n'
                  + usage_row(THREAD, 't1', 't1', 10, 1))
        raw = prefix + others + lists + usage_row(THREAD, 't2', 't2', 10, 1)
        found = [second, first, second, *hidden, first]
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=len(prefix))
        # The prefix is read only for the copies a sub-agent started in the call may carry.
        self.assertEqual((turn['skills_lists'], turn['earlier_skills_lists']), (found, []))
        raw = prefix + others + lists + sub_agent_started(CHILD, '/root/a') + usage_row(THREAD, 't2', 't2', 10, 1)
        turn = codex_ledger.read_turn(self.write(raw), thread_id=THREAD, prefix_bytes=len(prefix))
        self.assertEqual((turn['skills_lists'], turn['earlier_skills_lists']), (found, [first]))
        self.assertEqual(codex_ledger.compare_skills(found, declared=['plot-check'], workspace=self.cwd)['unreadable'],
                         2)
        # A sub-agent's are those of its whole rollout, the forked copy of its parent's included.
        self.write_child(CHILD_ROLLOUT + message('developer', first) + others
                         + message('developer', 'You check plots.', second))
        child = codex_ledger.read_child(self.home, CHILD, parent_thread_id=THREAD, root_turn_ids=['t1'])
        self.assertEqual(child['skills_lists'], [first, second])

    def test_lists_in_the_part_orders_codex_writes(self):
        # The lead and a sub-agent without a role: the list first, then the permissions and collaboration parts. A
        # sub-agent with a declared role: the role's instructions first, then the list, then the same parts.
        instructions = (ROLES['local']['instructions'],)
        roles = {'max_concurrent': 3, 'roles': ROLES}
        lead = skills_list([self.ws_root()], DECLARED, after=ROLE_LESS_PARTS)
        started = sub_agent_started(CHILD, '/root/local')

        def role_child(*records, forked=False):
            return child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1)], role='local', forked=forked) + b''.join(records)
        self.write_child(role_child(skills_list([self.ws_root()], DECLARED, before=instructions, after=ROLE_LESS_PARTS)))
        got = self.settle_call(lead + started, {**WITH_SKILLS, 'subagents': roles}, workspace=self.cwd)
        self.assertIsNone(got['skills_error'])
        self.assertEqual(got['receipt']['native_activity']['skills_shown'], ['plot-check'])
        [entry] = got['receipt']['child_threads']
        self.assertEqual((entry['role'], entry['skills_shown']), ('local', ['plot-check']))
        # Under a role without skills, the role sub-agent's list in its second part is seen, and the lead's in its
        # first.
        owners = ('secret-notes', 'Notes of the owner.', 'r0/secret-notes/SKILL.md')
        self.write_child(role_child(skills_list([OWNERS], owners, before=instructions, after=ROLE_LESS_PARTS)))
        without = {**TERMINAL, 'subagents': roles}
        got = self.settle_call(started, without, workspace=self.cwd)
        self.assertEqual(got['skills_error'], NOT_SKILLED + CHILD + ': 1 skills from ' + OWNERS)
        got = self.settle_call(skills_list([OWNERS], owners, after=ROLE_LESS_PARTS), without, workspace=self.cwd)
        self.assertEqual(got['skills_error'], NOT_SKILLED + THREAD + ': 1 skills from ' + OWNERS)
        # A role sub-agent forked with a lead history that holds an old list carries that message as it was; its own
        # message, without skills, holds no list.
        old = skills_list(['/u/.codex/skills'], ('personal-notes', 'x', 'r0/personal-notes/SKILL.md'),
                          after=ROLE_LESS_PARTS)
        self.write_child(role_child(old, frame('response_item', {'type': 'message', 'role': 'developer', 'content': [
            {'type': 'input_text', 'text': text} for text in instructions + ROLE_LESS_PARTS]}), forked=True))
        got = self.settle_call(started, without, earlier=old + usage_row(THREAD, 't0', 't0', 10, 1),
                               workspace=self.cwd)
        self.assertIsNone(got['skills_error'])
        self.assertNotIn('notes', json.dumps(got['receipt']))

    def test_a_list_as_declared_is_recorded(self):
        got = codex_ledger.compare_skills([skills_list_text([self.ws_root()], DECLARED)], declared=['plot-check'],
                                          workspace=str(self.cwd))
        self.assertEqual(got, {'shown': ['plot-check'], 'unexpected': 0, 'folders': [], 'missing': [], 'unreadable': 0})
        # Compared by name, root and file, never by description: Codex may collapse its whitespace.
        got = codex_ledger.compare_skills([skills_list_text([self.ws_root()], ('plot-check', 'Checks  a plot.',
                                                                              'r0/plot-check/SKILL.md'))],
                                          declared=['plot-check'], workspace=self.cwd)
        self.assertEqual((got['shown'], got['unexpected']), (['plot-check'], 0))
        # Another file, a second line of the same name, an unreadable list: each is counted; shown is the last
        # readable list's.
        lists = [skills_list_text([self.ws_root()], ('plot-check', 'x', 'r0/plot-check/README.md')),
                 skills_list_text([self.ws_root()], DECLARED, DECLARED), 'not a list']
        self.assertEqual(codex_ledger.compare_skills(lists, declared=['plot-check'], workspace=self.cwd),
                         {'shown': ['plot-check'], 'unexpected': 2, 'folders': [self.ws_root()],
                          'missing': ['plot-check'], 'unreadable': 1})
        # Without a workspace nothing lies in it.
        self.assertEqual(codex_ledger.compare_skills(lists[1:2], declared=['plot-check'], workspace=None)['unexpected'],
                         2)
        # With the call settled, the receipt records what was shown on the thread the call started.
        got = self.settle_call(self.declared_list(), WITH_SKILLS, workspace=self.cwd)
        self.assertIsNone(got['skills_error'])
        activity = got['receipt']['native_activity']
        self.assertEqual(activity['skills_shown'], ['plot-check'])
        self.assertNotIn('skills_unexpected', activity)
        self.assertNotIn('skills_lists', json.dumps(got['receipt']))

    def test_an_extra_skill_is_counted_with_its_folder_never_named(self):
        got = self.settle_call(skills_list([self.ws_root(), OWNERS], DECLARED, SECRET), WITH_SKILLS,
                               workspace=self.cwd)
        self.assertEqual(got['skills_error'], NOT_DECLARED + THREAD + ': 1 unexpected from ' + OWNERS)
        self.assertNotIn('secret-notes', json.dumps(got['receipt']))
        activity = got['receipt']['native_activity']
        self.assertEqual(activity['skills_unexpected'], {'count': 1, 'folders': [OWNERS]})
        self.assertEqual(activity['skills_shown'], ['plot-check'])
        # Two folders, each named once.
        got = self.settle_call(skills_list([self.ws_root(), OWNERS, '/opt/machine/skills'], DECLARED, SECRET,
                                           ('more-notes', 'x', 'r1/more-notes/SKILL.md'),
                                           ('machine-notes', 'x', 'r2/machine-notes/SKILL.md')),
                               WITH_SKILLS, workspace=self.cwd)
        self.assertEqual(got['skills_error'], NOT_DECLARED + THREAD + ': 3 unexpected from ' + OWNERS
                         + ', /opt/machine/skills')
        self.assertEqual(got['receipt']['native_activity']['skills_unexpected'],
                         {'count': 3, 'folders': [OWNERS, '/opt/machine/skills']})

    def test_a_missing_or_unreadable_list_or_none_at_all_invalidates(self):
        broken = self.declared_list().replace(b'### Skill roots', b'### Roots')
        cases = {'a list without plot-check': (skills_list([self.ws_root()]), 'missing plot-check', []),
                 'a list that cannot be read': (broken, '1 lists unreadable', []),
                 'no list': (b'', 'no list', []),
                 'an extra skill and a missing one': (skills_list([self.ws_root(), OWNERS], SECRET),
                                                      '1 unexpected from ' + OWNERS + '; missing plot-check', []),
                 'a rollout that cannot be read': (b'{"type": "event_msg", "payload": \n', 'rollout unreadable',
                                                   None)}
        for case, (records, part, shown) in cases.items():
            with self.subTest(case=case):
                got = self.settle_call(records, WITH_SKILLS, workspace=self.cwd)
                self.assertEqual(got['skills_error'], NOT_DECLARED + THREAD + ': ' + part)
                self.assertEqual(got['receipt']['native_activity']['skills_shown'], shown)
                # A role without skills is shown nothing: what was not read is not its error.
                self.assertEqual(self.settle_call(records, TERMINAL, workspace=self.cwd)['skills_error'],
                                 None if case in ('no list', 'a rollout that cannot be read') else NOT_SKILLED + THREAD
                                 + ': ' + {'a list without plot-check': '0 skills',
                                           'a list that cannot be read': '1 lists unreadable',
                                           'an extra skill and a missing one': '1 skills from ' + OWNERS}[case])
        # A call that did not finish may have stopped before Codex wrote the list.
        got = self.settle_call(b'', WITH_SKILLS, workspace=self.cwd, finished=False)
        self.assertIsNone(got['skills_error'])
        self.assertEqual(got['receipt']['native_activity']['skills_shown'], [])
        # A call that was never dispatched showed nothing.
        self.assertIsNone(self.settle_call(b'', WITH_SKILLS, workspace=self.cwd, dispatched=False)['skills_error'])

    def test_a_list_for_a_role_without_skills_invalidates(self):
        two = skills_list(['/u/.codex/skills', OWNERS], ('personal-notes', 'x', 'r0/personal-notes/SKILL.md'),
                          SECRET)
        for settings in (TERMINAL, native.OFF):
            with self.subTest(settings=settings):
                got = self.settle_call(two, settings, workspace=self.cwd)
                self.assertEqual(got['skills_error'], NOT_SKILLED + THREAD + ': 2 skills from /u/.codex/skills, '
                                 + OWNERS)
                self.assertNotIn('notes', got['skills_error'])
                self.assertNotIn('notes', json.dumps(got['receipt']))
                self.assertNotIn('skills_shown', json.dumps(got['receipt']))
                self.assertNotIn('skills_unexpected', json.dumps(got['receipt']))
                # With no list, nothing is reported, and the receipt is as before.
                got = self.settle_call(b'', settings, workspace=self.cwd)
                self.assertIsNone(got['skills_error'])
                self.assertEqual(set(got['receipt']), {'token_source', 'token_reason', 'stream_usage', 'ledger',
                                                       'account_snapshot'} | ({'native_capabilities',
                                                       'native_activity', 'child_threads'} if settings == TERMINAL
                                                                              else set()))

    def test_a_resumed_thread_that_got_a_second_list(self):
        earlier = self.declared_list() + usage_row(THREAD, 't1', 't1', 10, 1)
        cases = {'the declared list again': (self.declared_list(), None, False),
                 'a list adding an undeclared skill': (skills_list([self.ws_root(), OWNERS], DECLARED, SECRET),
                                                       NOT_DECLARED + THREAD + ': 1 unexpected from ' + OWNERS, True),
                 'a list missing the declared skill': (skills_list([self.ws_root()]),
                                                       NOT_DECLARED + THREAD + ': missing plot-check', False),
                 'no list, as when its skills did not change': (b'', None, False)}
        for case, (records, error, unexpected) in cases.items():
            with self.subTest(case=case):
                got = self.settle_call(records, WITH_SKILLS, earlier=earlier, turn='t2', workspace=self.cwd)
                self.assertEqual(got['skills_error'], error)
                activity = got['receipt']['native_activity']
                self.assertNotIn('skills_shown', activity)
                self.assertEqual(activity.get('skills_unexpected'),
                                 {'count': 1, 'folders': [OWNERS]} if unexpected else None)
                self.assertNotIn('secret-notes', json.dumps(got['receipt']))

    def test_a_sub_agent_thread_is_checked(self):
        started = self.declared_list() + sub_agent_started(CHILD, '/root/a')
        cases = {'its own list as declared': (self.declared_list(), None, ['plot-check']),
                 'no list': (b'', NOT_DECLARED + CHILD + ': no list', []),
                 'a list adding an undeclared skill': (skills_list([self.ws_root(), OWNERS], DECLARED, SECRET),
                                                       NOT_DECLARED + CHILD + ': 1 unexpected from ' + OWNERS,
                                                       ['plot-check'])}
        for case, (own, error, shown) in cases.items():
            with self.subTest(case=case):
                self.reset_home(); self.write_child(CHILD_ROLLOUT + own)
                got = self.settle_call(started, WITH_SKILLS_AND_SUBAGENTS, workspace=self.cwd)
                self.assertEqual(got['skills_error'], error)
                [entry] = got['receipt']['child_threads']
                self.assertEqual(entry['skills_shown'], shown)
                self.assertEqual(entry.get('skills_unexpected'),
                                 {'count': 1, 'folders': [OWNERS]} if error and 'unexpected' in error else None)
                self.assertNotIn('skills_lists', entry)
                self.assertNotIn('secret-notes', json.dumps(got['receipt']))
                # The lead's own list is checked and recorded beside it.
                self.assertEqual(got['receipt']['native_activity']['skills_shown'], ['plot-check'])
        # The sub-agent's rollout cannot be read, or the turn added no usage rows and it was never looked for.
        # Under a role without skills too: what such a sub-agent was shown cannot be checked.
        for rows in (True, False):
            with self.subTest(rows=rows):
                self.reset_home()
                got = self.settle_call(started, WITH_SKILLS_AND_SUBAGENTS, rows=rows, workspace=self.cwd)
                self.assertEqual(got['skills_error'], NOT_DECLARED + CHILD + ': rollout unreadable')
                got = self.settle_call(sub_agent_started(CHILD, '/root/a'), TERMINAL_AND_SUBAGENTS, rows=rows,
                                       workspace=self.cwd)
                self.assertEqual(got['skills_error'], NOT_SKILLED + CHILD + ': rollout unreadable')
        # A sub-agent named by an id that is not text cannot be read.
        unnamed = frame('event_msg', {'type': 'item_completed', 'item': {
            'type': 'SubAgentActivity', 'kind': 'started', 'agent_thread_id': ['x'], 'agent_path': '/root/b'}})
        got = self.settle_call(self.declared_list() + unnamed, WITH_SKILLS_AND_SUBAGENTS, workspace=self.cwd)
        self.assertEqual(got['skills_error'], NOT_DECLARED + "['x']: rollout unreadable")
        self.assertEqual(self.settle_call(unnamed, TERMINAL_AND_SUBAGENTS, workspace=self.cwd)['skills_error'],
                         NOT_SKILLED + "['x']: rollout unreadable")
        # Under a role without skills the lead's own unread rollout is not checked, and a sub-agent only addressed
        # again whose rollout cannot be read is not either.
        self.reset_home()
        self.assertIsNone(self.settle_call(b'{"type": "event_msg", "payload": \n', TERMINAL_AND_SUBAGENTS,
                                           workspace=self.cwd)['skills_error'])
        self.assertIsNone(self.settle_call(sub_agent_activity(CHILD, '/root/a', 'interacted'), TERMINAL_AND_SUBAGENTS,
                                           earlier=sub_agent_started(CHILD, '/root/a')
                                           + usage_row(THREAD, 't1', 't1', 10, 1),
                                           turn='t2', workspace=self.cwd)['skills_error'])
        # Both threads at fault are named, the lead first.
        self.reset_home(); self.write_child()
        got = self.settle_call(sub_agent_started(CHILD, '/root/a'), WITH_SKILLS_AND_SUBAGENTS, workspace=self.cwd)
        self.assertEqual(got['skills_error'], NOT_DECLARED + THREAD + ': no list; ' + CHILD + ': no list')
        # A sub-agent only addressed again: its list was checked by the call that started it.
        self.reset_home()
        self.write_child(child_rollout(CHILD, THREAD, [('c1', 't1', 5, 1), ('c2', 't2', 7, 3)]))
        got = self.settle_call(sub_agent_activity(CHILD, '/root/a', 'interacted'), WITH_SKILLS_AND_SUBAGENTS,
                               earlier=started + usage_row(THREAD, 't1', 't1', 10, 1), turn='t2', workspace=self.cwd)
        self.assertIsNone(got['skills_error'])
        [entry] = got['receipt']['child_threads']
        self.assertNotIn('skills_shown', entry)

    def test_a_forked_copy_of_an_old_lead_list_is_not_the_sub_agents_own(self):     # Review Focus 1
        # The lead's thread was started before roles without skills were shown none: its prefix holds the owner's
        # personal skills, and a sub-agent forked with its history carries a copy.
        old = skills_list(['/u/.codex/skills'], ('personal-notes', 'Notes of the owner.', 'r0/personal-notes/SKILL.md'))
        earlier = old + usage_row(THREAD, 't1', 't1', 10, 1)

        def forked(*records):
            history = frame('session_meta', {'id': THREAD, 'cwd': str(self.cwd)}) + old
            return (sub_agent_meta(CHILD, THREAD, forked=True) + history + b''.join(records)
                    + usage_row(CHILD, 'c2', 't2', 5, 1, session=THREAD))
        other = skills_list(['/u/.codex/skills'], ('personal-notes', 'Notes of the owner, edited.',
                                                   'r0/personal-notes/SKILL.md'))
        cases = {'the copy alone': (forked(), None),
                 'the copy and a list of its own': (forked(other), NOT_SKILLED + CHILD + ': 1 skills from '
                                                    '/u/.codex/skills'),
                 'another list instead': (sub_agent_meta(CHILD, THREAD, forked=True) + other
                                          + usage_row(CHILD, 'c2', 't2', 5, 1, session=THREAD),
                                          NOT_SKILLED + CHILD + ': 1 skills from /u/.codex/skills'),
                 # A fresh list of its own that equals the lead's old one: only a forked sub-agent carries copies,
                 # and each list in the lead's prefix excuses one copy, no more.
                 'the same text, not forked': (sub_agent_meta(CHILD, THREAD, forked=False) + old
                                               + usage_row(CHILD, 'c2', 't2', 5, 1, session=THREAD),
                                               NOT_SKILLED + CHILD + ': 1 skills from /u/.codex/skills'),
                 'the copy and the same text again': (forked(old), NOT_SKILLED + CHILD + ': 1 skills from '
                                                      '/u/.codex/skills')}
        for case, (rollout, error) in cases.items():
            with self.subTest(case=case):
                self.reset_home(); self.write_child(rollout)
                got = self.settle_call(sub_agent_started(CHILD, '/root/a'), TERMINAL_AND_SUBAGENTS, earlier=earlier,
                                       turn='t2', workspace=self.cwd)
                self.assertEqual(got['skills_error'], error)
                self.assertNotIn('personal-notes', json.dumps(got['receipt']))
        # A lead whose prefix holds the list twice excuses two copies.
        self.reset_home(); self.write_child(forked(old))
        got = self.settle_call(sub_agent_started(CHILD, '/root/a'), TERMINAL_AND_SUBAGENTS,
                               earlier=old + earlier, turn='t2', workspace=self.cwd)
        self.assertIsNone(got['skills_error'])
        # Under a role with skills every list is compared, copies included.
        self.reset_home(); self.write_child(forked(self.declared_list()))
        got = self.settle_call(sub_agent_started(CHILD, '/root/a'), WITH_SKILLS_AND_SUBAGENTS, earlier=earlier,
                               turn='t2', workspace=self.cwd)
        self.assertEqual(got['skills_error'], NOT_DECLARED + CHILD + ': 1 unexpected from /u/.codex/skills; '
                                                                     'missing plot-check')
        self.assertEqual(got['receipt']['child_threads'][0]['skills_shown'], ['plot-check'])

    def test_root_spellings_and_extra_lines(self):                                   # Review Focus 2
        link = self.root / 'link'
        link.symlink_to(self.cwd)
        texts = {'through a symlink to the workspace': skills_list_text([str(link / '.agents' / 'skills')], DECLARED),
                 'resolved': skills_list_text([str(self.cwd.resolve() / '.agents' / 'skills')], DECLARED),
                 'with a trailing slash': skills_list_text([self.ws_root() + '/'], DECLARED),
                 'a prose line inside Available skills': skills_list_text([self.ws_root()], DECLARED).replace(
                     '### Available skills\n', '### Available skills\nUse them when relevant.\n'),
                 'the lines of a later section': skills_list_text([self.ws_root()], DECLARED).replace(
                     '\n</skills_instructions>', '\n### How to use skills\n- Discovery: open the file named above.'
                     '\n- Scope: one skill at a time.\n</skills_instructions>'),
                 'a root no skill is listed under': skills_list_text([self.ws_root(), OWNERS], DECLARED)}
        for case, text in texts.items():
            with self.subTest(case=case):
                self.assertEqual(codex_ledger.compare_skills([text], declared=['plot-check'], workspace=self.cwd),
                                 {'shown': ['plot-check'], 'unexpected': 0, 'folders': [], 'missing': [],
                                  'unreadable': 0})
        # The workspace's .codex/skills is not its .agents/skills; neither is a relative root.
        for root in (str(self.cwd / '.codex' / 'skills'), 'workspace/.agents/skills'):
            with self.subTest(root=root):
                self.assertEqual(codex_ledger.compare_skills([skills_list_text([root], DECLARED)],
                                                             declared=['plot-check'], workspace=self.cwd),
                                 {'shown': [], 'unexpected': 1, 'folders': [root], 'missing': ['plot-check'],
                                  'unreadable': 0})


if __name__ == '__main__':
    unittest.main()
