"""The executor of a role's model call: its native settings, its cost from Codex's ledger and the turn's activity.

The Lab Effect is prepared and dispatched by the real Runtime and Controller.
Most tests replace ``transport.invoke``, as tests/v2/test_roles.py does, by a
fixture that writes the call's stream and a synthetic rollout built from
tests/v2/native_fixtures.py. The tests of role files and of refused role calls
run the real transport instead, with ``subprocess.Popen`` replaced by a local
process standing in for Codex or by one that fails if Codex is started. No
Codex, model, network request or credential is used.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi_lab import input_view, native, packets, roles, transport
from rsi_lab.session_context import read_native_snapshot
from rsi_lab.store import digest, encode
from tests.v2.liveness_support import Scripted, none, open_run
from tests.v2.native_fixtures import (CHECK_PY, CHEM, MCP_RESULT, PLAIN, PNG, PROFILE, R, ROLE_LESS_PARTS, ROLES,
                                      SEARCH, SKILL_MD,
                                      TERMINAL, TERMINAL_AND_SUBAGENTS, TOOL_SERVERS, VIEW_IMAGE, WITH_ROLES,
                                      WITH_SKILLS, WITH_TOOL_SERVERS, account, child_rollout, here, mcp_call,
                                      skills_list, sub_agent_started, tool_call, turn_context, usage_row,
                                      viewed_image)
from tests.v2.test_session_context import THREAD, fixture

CHILD = '33333333-3333-3333-3333-333333333333'
CHILD_ROWS = [('c1', 't1', 22828, 37)]
RESERVATION = {'model_calls': 1, 'tokens': 200000, 'wall_seconds': 10}
STREAM_USAGE = {'input_tokens': 10, 'output_tokens': 1}
NONE_YET = {'issue_version': 0, 'evidence_revision': 0, 'scheduling_revision': 0}
RESPONSE = {'command': {'mode': 'lab', 'action': 'no_action', 'issue_id': None, 'expected_versions': NONE_YET,
                        'evidence_ids': [], 'payload': {'reason': 'Synthetic turn'}}}
EXEC = 'text(await tools.exec_command({cmd: "ls"}))'
SAME = object()
# Lines of Codex's skills list: the skill WITH_SKILLS declares, under the workspace's skills folder, and one nobody
# declared, under the owner's.
DECLARED = ('plot-check', 'Checks a plot before it is reported.', 'r0/plot-check/SKILL.md')
SECRET = ('secret-notes', 'Notes of the owner.', 'r1/secret-notes/SKILL.md')
OWNERS = '/u/.agents/skills'
# What transport.invoke returns for a call killed at its deadline before its terminal event.
UNFINISHED = {'response': None, 'usage': None, 'usage_complete': False, 'protocol_valid': False,
              'execution_status': 'unknown', 'terminal_received': False, 'stream_complete': False, 'returncode': -9,
              'error': 'Codex timed out after 300 seconds; remote completion and usage may be unknown'}


# Each rollout gives, after the base fixture, the records an earlier committed turn wrote and those this call adds.
def TWO_TURNS(profile, cwd):
    return usage_row(THREAD, 't1', 't1', 54394, 5524), usage_row(THREAD, 't2', 't2', 102016, 5155)


def PARENT_WITH_CHILD(profile, cwd):
    return b'', (turn_context('t1', profile, cwd=cwd) + sub_agent_started(CHILD, '/root/a') + tool_call('c1', EXEC)
                 + account('1200.50', 12.5) + usage_row(THREAD, 't1', 't1', 83032, 291))


def TURN_WITHOUT_PROFILE(profile, cwd):
    return b'', turn_context('t1', cwd=cwd) + tool_call('c1', EXEC) + usage_row(THREAD, 't1', 't1', 10, 1)


def CONFINED_TURN(profile, cwd):
    return b'', turn_context('t1', profile, cwd=cwd) + usage_row(THREAD, 't1', 't1', 10, 1)


def NO_ROWS(profile, cwd):
    return b'', turn_context('t1', profile, cwd=cwd)


def ACCOUNTED_TURN(profile, cwd):
    return b'', turn_context('t1', profile, cwd=cwd) + account('1200.50', 12.5) + usage_row(THREAD, 't1', 't1', 10, 1)


def UNREADABLE(profile, cwd):
    return b'', b'{"type": "event_msg", "payload": \n'


def listing(*skills):
    """A rollout whose call writes Codex's skills list of ``skills``, with the workspace's skills folder as ``r0`` and
    the owner's own as ``r1``, then a turn under ``profile``."""
    def rollout(profile, cwd):
        return b'', (skills_list([str(cwd / '.agents' / 'skills'), OWNERS], *skills)
                     + turn_context('t1', profile, cwd=cwd) + usage_row(THREAD, 't1', 't1', 10, 1))
    return rollout


def SEARCHED_TWICE(profile, cwd):
    search = 'text(await tools.web_search({query: "%s"}))'
    return (tool_call('c1', search % 'first') + usage_row(THREAD, 't1', 't1', 300, 20),
            tool_call('c2', search % 'second') + usage_row(THREAD, 't2', 't2', 400, 30))


def config_form(settings):
    """A normal-form settings object as a run config writes it: a capability that is off is ``false``."""
    return {name: False if value is None else value for name, value in settings.items()}


def carrying(value):
    """An ``alter`` for ``NativeExecutorTests.execute``: the packet rebuilt as the packet builder builds it, with
    ``value`` as its task's ``constraints.native_capabilities``, and the Effect bound to the rebuilt packet."""
    def alter(dispatching, raw):
        request = json.loads(raw)
        head, rendered = request['prompt'].rsplit('INPUT_DATA_JSON:\n', 1)
        data = input_view.decode(rendered, request['input_view_mode'])
        data['task']['constraints']['native_capabilities'] = value
        request['prompt'] = head + 'INPUT_DATA_JSON:\n' + input_view.render(data, request['input_view_mode'])
        raw = packets.encoded(request)
        return {**dispatching, 'packet_sha256': digest(raw)}, raw
    return alter


class NativeExecutorTests(unittest.TestCase):
    def setUp(self):
        # An empty home and Codex home, and no machine skills folder: the Runtime and the executor look for skills
        # there, and nothing of this machine's own may be read.
        places = Path(tempfile.mkdtemp(prefix='rsi-native-places-'))
        self.addCleanup(shutil.rmtree, places, ignore_errors=True)
        (places / 'home').mkdir()
        self.codex_home = places / 'codex-home'
        self.codex_home.mkdir()
        for patcher in (patch.dict(os.environ, {'HOME': str(places / 'home'), 'CODEX_HOME': str(self.codex_home)}),
                        patch.object(native, 'MACHINE_SKILLS', places / 'machine-skills')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, *, settings, rollout=CONFINED_TURN, child=None, stream_usage=STREAM_USAGE, resumed=False,
                packet_settings=SAME, transport_extra=None, stream_items=(), workspace_files=(), profile=None,
                finished=True, answer=json.dumps(RESPONSE), arrange=None, alter=None, child_records=None):
        """Prepare a Lab turn under ``packet_settings`` (``settings`` unless given) and run it through the Controller
        with the executor, which sees ``settings`` in the Effect. Returns the executor's receipt; a ``ValueError``
        it raises is raised here. ``profile(run_root, codex_home, workspace)`` replaces the confined profile.
        ``child`` holds the usage rows of the sub-agent ``CHILD``'s rollout, and ``child_records(workspace)`` the
        records that follow them.
        ``answer`` is the text of the turn's last message. A call that is not ``finished`` was killed before its
        answer and terminal event (``UNFINISHED``). ``arrange(store)`` runs once the Effect is prepared, before
        it is dispatched. ``alter(dispatching, raw)`` returns the Effect and packet the executor is handed."""
        packet_settings = settings if packet_settings is SAME else packet_settings
        config = {'native_capabilities': {'lab': config_form(packet_settings)}} if packet_settings else {}
        store, controller, runtime = open_run(self, config=config)
        self.store = store
        codex_home = Path(tempfile.mkdtemp(prefix='rsi-native-home-'))
        self.addCleanup(shutil.rmtree, codex_home, ignore_errors=True)
        sessions = codex_home / 'sessions' / 'fixture'
        sessions.mkdir(parents=True)
        path = sessions / ('rollout-fixture-' + THREAD + '.jsonl')

        def workspace_of(effect):
            return store.root / 'agents' / effect['metadata']['agent_session_id'] / 'workspace'

        def records(workspace):
            # The profile names the Codex home as it was created, before any resolution.
            return rollout((profile or PROFILE)(store.root, codex_home, workspace), workspace)

        if resumed:
            def first_turn(effect, raw):
                """A scripted earlier turn whose committed rollout is the base fixture and the rollout's first part."""
                workspace = workspace_of(effect)
                path.write_bytes(fixture(workspace) + records(workspace)[0])
                after = read_native_snapshot(path, expected_thread_id=THREAD, expected_cwd=workspace,
                                             native=packet_settings)
                prefix = {key: after[key] for key in ('prefix_bytes', 'prefix_sha256', 'provider_thread_id')}
                return {**Scripted({'lab': none})(effect, raw), 'provider_thread_id': THREAD,
                        'context_manifest': {'native_session_ref': str(path), 'native_prefix': prefix}}
            out = runtime.run_lab(runtime.enqueue_lab('lab-1')['id'], first_turn, RESERVATION)
            self.assertEqual(out['proposal']['status'], 'applied', out)
        effect = runtime.prepare_meta(runtime.enqueue_lab('lab-2')['id'], RESERVATION)
        self.assertEqual(effect['status'], 'prepared', effect)
        workspace = workspace_of(effect)
        for make in workspace_files:
            workspace.mkdir(parents=True, exist_ok=True)
            make(workspace)
        if arrange is not None:
            arrange(store)
        self.calls, receipts, raised = [], [], []

        def invoke(prompt, schema, folder, model, effort, timeout, **options):
            self.calls.append(options)
            on = native.any_on(options.get('native'))
            folder.mkdir(parents=True)
            (folder / 'prompt.txt').write_bytes(((native.PREAMBLE if on else transport.PREAMBLE) + prompt).encode())
            invocation = {'backend': 'codex', 'model': model, 'effort': effort}
            (folder / 'invocation.json').write_text(json.dumps({**invocation, 'native': options['native']} if on
                                                               else invocation))
            events = ([{'type': 'thread.started', 'thread_id': THREAD}, {'type': 'turn.started'}]
                      + [{'type': 'item.completed', 'item': item} for item in stream_items]
                      + ([{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': answer}},
                          {'type': 'turn.completed', 'usage': stream_usage}] if finished else []))
            (folder / 'events.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
            with path.open('ab') as stream:
                stream.write((b'' if resumed else fixture(workspace)) + records(workspace)[1])
            if child is not None:
                (sessions / ('rollout-fixture-' + CHILD + '.jsonl')).write_bytes(
                    child_rollout(CHILD, THREAD, child, cwd=workspace)
                    + (b'' if child_records is None else child_records(workspace)))
            result = {'response': RESPONSE, 'usage': stream_usage, 'usage_complete': True, 'error': None,
                      'protocol_valid': True, 'parse_error': None, 'execution_status': 'succeeded',
                      'terminal_received': True, 'stream_complete': True, 'wall_seconds': .1, 'returncode': 0,
                      'descendants_stopped': True, 'backend': 'codex', 'provider_thread_id': THREAD,
                      'session_id': options['session']['session_id'], 'codex_home': str(codex_home.resolve()),
                      'workspace': options['session']['workspace'], 'journal': None, 'dispatched': True,
                      'native_session_ref': str(path), 'native_session_sha256': digest(path.read_bytes()),
                      'native_session_error': None, **({'native': options['native']} if on else {}),
                      **({} if finished else UNFINISHED), **(transport_extra or {})}
            (folder / 'transport_result.json').write_text(json.dumps(result))
            return result

        def executor(dispatching, raw):
            if packet_settings != settings:
                metadata = {key: value for key, value in dispatching['metadata'].items()
                            if key != 'native_capabilities'}
                dispatching = {**dispatching, 'metadata': {**metadata, **(
                    {'native_capabilities': settings} if settings is not None else {})}}
            if alter is not None:
                dispatching, raw = alter(dispatching, raw)
            try:
                receipts.append(roles.CodexExecutor(store.root / 'calls')(dispatching, raw))
            except ValueError as exc:
                raised.append(exc)
                raise
            return receipts[-1]

        with patch.object(roles.transport, 'invoke', side_effect=invoke):
            self.effect = controller.dispatch_effect('r', effect['id'], executor)
        if raised:
            raise raised[0]
        return receipts[0]

    def test_a_native_turn_is_charged_from_the_ledger_with_its_children(self):
        receipt = self.execute(settings=TERMINAL_AND_SUBAGENTS, rollout=PARENT_WITH_CHILD, child=CHILD_ROWS,
                               stream_usage={'input_tokens': 83032, 'output_tokens': 291})
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertEqual(receipt['actual_costs']['tokens'], 83032 + 291 + 22828 + 37)
        self.assertEqual(receipt['actual_costs']['model_calls'], 1)
        self.assertEqual(receipt['token_source'], 'ledger')
        self.assertIsNone(receipt['token_reason'])
        self.assertEqual(receipt['child_threads'][0]['thread_id'], CHILD)
        self.assertEqual(receipt['native_activity']['tool_calls'], 1)
        self.assertEqual(receipt['account_snapshot']['plan_type'], 'pro')
        self.assertEqual(receipt['ledger'], {'requests': 2, 'input_tokens': 83032 + 22828, 'output_tokens': 291 + 37,
                                             'cached_input_tokens': 0, 'turn_ids': ['t1']})
        self.assertEqual(receipt['stream_usage'], {'input_tokens': 83032, 'output_tokens': 291})
        self.assertEqual(receipt['native_capabilities'], TERMINAL_AND_SUBAGENTS)
        self.assertEqual(receipt['native_activity']['provider_requests'], 1)
        self.assertTrue(receipt['protocol_valid'])
        self.assertIsNone(receipt['error'])
        self.assertTrue(receipt['usage_complete'])
        self.assertEqual(self.effect['actual_costs']['tokens'], 83032 + 291 + 22828 + 37)

    def test_a_tool_free_resumed_turn_is_also_charged_from_the_ledger(self):
        receipt = self.execute(settings=None, resumed=True, rollout=TWO_TURNS,
                               stream_usage={'input_tokens': 156410, 'output_tokens': 10679})
        self.assertEqual(receipt['actual_costs']['tokens'], 102016 + 5155)
        self.assertNotIn('native_activity', receipt)
        self.assertEqual(receipt['token_source'], 'ledger')
        self.assertEqual(receipt['usage'], {'input_tokens': 156410, 'output_tokens': 10679})

    def test_unknown_usage_leaves_tokens_unsettled(self):
        receipt = self.execute(settings=TERMINAL_AND_SUBAGENTS, rollout=PARENT_WITH_CHILD, child=None)
        self.assertNotIn('tokens', receipt['actual_costs'])
        self.assertFalse(receipt['usage_complete']); self.assertEqual(receipt['token_source'], 'unknown')
        self.assertIn(CHILD, receipt['token_reason'])
        self.assertTrue(self.effect['accounting_unknown'])

    def test_a_terminal_turn_without_the_profile_is_invalid(self):
        receipt = self.execute(settings=TERMINAL, rollout=TURN_WITHOUT_PROFILE)
        self.assertFalse(receipt['protocol_valid']); self.assertIn('Permission profile was not in force', receipt['error'])

    def test_packet_and_effect_settings_must_agree(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported exact model request envelope'):
            self.execute(settings=TERMINAL, packet_settings=SEARCH)

    def test_the_executor_compares_the_request_form(self):
        full = {**WITH_TOOL_SERVERS, 'tool_servers': {**TOOL_SERVERS, 'chem': here(CHEM)}}
        # A packet carrying the Effect's full settings instead of their request form is refused.
        with self.assertRaisesRegex(ValueError, 'Unsupported exact model request envelope'):
            self.execute(settings=full, alter=carrying(full))
        self.assertEqual(self.calls, [])
        # The packet as the Runtime prepared it, with the request form, is sent with the Effect's full settings.
        with patch.dict(os.environ, {'CHEM_API_KEY': 'invented-secret-4f1c', 'PAPERS_TOKEN': 'invented-token-7d2a'}):
            receipt = self.execute(settings=full)
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        self.assertEqual(self.calls[0]['native'], full)
        self.assertEqual(receipt['native_capabilities'], native.receipt_form(full))
        # carrying rebuilds that packet byte for byte, so the refusal above is the settings', not the rebuilding's.
        raw = self.store.read_blob(self.effect['packet_ref'])
        self.assertEqual(carrying(native.request_form(full))(self.effect, raw)[1], raw)
        # With everything off the packet may not name native settings at all.
        with self.assertRaisesRegex(ValueError, 'Unsupported exact model request envelope'):
            self.execute(settings=None, alter=carrying(None))

    def test_surviving_descendants_block_quiescence(self):
        self.assertFalse(self.execute(settings=TERMINAL, transport_extra={'descendants_stopped': False})['native_writer_quiesced'])

    def test_the_preamble_must_match_the_effects_settings(self):
        for settings, packet_settings in ((SEARCH, None), (None, SEARCH)):
            with self.subTest(settings=settings), \
                    self.assertRaisesRegex(ValueError, 'Unsupported exact model request envelope'):
                self.execute(settings=settings, packet_settings=packet_settings)

    def test_a_native_call_gets_its_settings_and_the_run_root(self):
        receipt = self.execute(settings=TERMINAL)
        self.assertEqual(self.calls[0]['native'], TERMINAL)
        self.assertEqual(Path(self.calls[0]['run_root']), self.store.root)
        self.assertEqual(receipt['native_capabilities'], TERMINAL)
        self.assertTrue(receipt['protocol_valid'])
        self.assertTrue(receipt['native_writer_quiesced'])

    def test_everything_off_calls_todays_transport_and_quiesces_as_today(self):
        receipt = self.execute(settings=None, transport_extra={'descendants_stopped': False})
        self.assertEqual(set(self.calls[0]), {'session', 'journal_metadata'})
        self.assertTrue(receipt['native_writer_quiesced'])
        for key in ('native_capabilities', 'native_activity', 'child_threads'):
            self.assertNotIn(key, receipt)
        self.assertEqual(receipt['actual_costs']['tokens'], 11)
        self.assertEqual(receipt['token_source'], 'ledger')
        # The turn was read, but Codex recorded no account snapshot in it.
        self.assertIsNone(receipt['account_snapshot'])

    def test_every_read_turn_records_its_account_snapshot(self):
        for settings in (None, SEARCH, TERMINAL):
            with self.subTest(settings=settings):
                receipt = self.execute(settings=settings, rollout=ACCOUNTED_TURN)
                self.assertEqual(receipt['account_snapshot']['plan_type'], 'pro')
                self.assertEqual(receipt['account_snapshot']['credits_balance'], '1200.50')
                self.assertEqual(receipt['token_source'], 'ledger')

    def test_a_turn_that_cannot_be_read_records_no_account_snapshot(self):
        for settings in (None, TERMINAL):
            with self.subTest(settings=settings):
                receipt = self.execute(settings=settings, rollout=UNREADABLE)
                self.assertEqual(receipt['token_source'], 'unknown')
                self.assertNotIn('account_snapshot', receipt)
                self.assertEqual('native_activity' in receipt, settings is not None)

    def test_a_fresh_thread_without_usage_rows_is_charged_the_stream(self):
        receipt = self.execute(settings=None, rollout=NO_ROWS, stream_usage={'input_tokens': 40, 'output_tokens': 2})
        self.assertEqual(receipt['actual_costs']['tokens'], 42)
        self.assertEqual(receipt['token_source'], 'stream')
        self.assertEqual(receipt['ledger'], {'requests': 0, 'input_tokens': 0, 'output_tokens': 0,
                                             'cached_input_tokens': 0, 'turn_ids': []})
        self.assertTrue(receipt['usage_complete'])

    def test_a_call_with_sub_agents_on_is_never_charged_the_stream(self):
        stream = {'input_tokens': 40, 'output_tokens': 2}
        receipt = self.execute(settings=TERMINAL_AND_SUBAGENTS, rollout=NO_ROWS, stream_usage=stream)
        self.assertNotIn('tokens', receipt['actual_costs'])
        self.assertEqual(receipt['token_source'], 'unknown')
        self.assertIn('leaves out sub-agents', receipt['token_reason'])
        receipt = self.execute(settings=TERMINAL, rollout=NO_ROWS, stream_usage=stream)
        self.assertEqual((receipt['token_source'], receipt['actual_costs']['tokens']), ('stream', 42))
        self.assertIsNone(receipt['token_reason'])

    def test_a_resumed_turn_without_usage_rows_is_unknown(self):
        def without_rows(profile, cwd):
            return usage_row(THREAD, 't1', 't1', 54394, 5524), b''
        receipt = self.execute(settings=None, resumed=True, rollout=without_rows,
                               stream_usage={'input_tokens': 156410, 'output_tokens': 10679})
        self.assertNotIn('tokens', receipt['actual_costs'])
        self.assertEqual(receipt['token_source'], 'unknown')
        self.assertFalse(receipt['usage_complete'])
        self.assertEqual(receipt['stream_usage'], {'input_tokens': 156410, 'output_tokens': 10679})
        self.assertTrue(self.effect['accounting_unknown'])

    def test_a_turn_that_did_not_finish_leaves_its_tokens_unknown(self):
        # Requests in flight at the kill wrote no usage rows: the rows read are a lower bound, not the call's cost.
        cases = {'a resumed call with everything off': (
                     {'settings': None, 'resumed': True, 'rollout': TWO_TURNS}, 102016 + 5155,
                     {'requests': 1, 'input_tokens': 102016, 'output_tokens': 5155, 'cached_input_tokens': 0,
                      'turn_ids': ['t2']}),
                 'a native call with a sub-agent': (
                     {'settings': TERMINAL_AND_SUBAGENTS, 'rollout': PARENT_WITH_CHILD, 'child': CHILD_ROWS},
                     83032 + 291 + 22828 + 37,
                     {'requests': 2, 'input_tokens': 83032 + 22828, 'output_tokens': 291 + 37,
                      'cached_input_tokens': 0, 'turn_ids': ['t1']})}
        for case, (arguments, tokens, ledger) in cases.items():
            with self.subTest(case=case):
                receipt = self.execute(**arguments, finished=False)
                self.assertEqual(receipt['status'], 'unknown')
                self.assertNotIn('tokens', receipt['actual_costs'])
                self.assertEqual(receipt['token_source'], 'unknown')
                self.assertIn('the turn did not finish', receipt['token_reason'])
                self.assertIn('requests in flight when it was stopped wrote no usage rows', receipt['token_reason'])
                self.assertEqual(receipt['ledger'], ledger)
                self.assertFalse(receipt['usage_complete'])
                self.assertNotIn('tokens', self.effect['actual_costs'])
                self.assertTrue(self.effect['accounting_unknown'])
                self.assertEqual(self.effect['status'], 'unknown')
            with self.subTest(case=case + ', finished'):
                receipt = self.execute(**arguments)
                self.assertEqual((receipt['status'], receipt['token_source']), ('succeeded', 'ledger'))
                self.assertIsNone(receipt['token_reason'])
                self.assertEqual(receipt['actual_costs']['tokens'], tokens)
                self.assertEqual(receipt['ledger'], ledger)
                self.assertFalse(self.effect['accounting_unknown'])

    def test_a_native_turn_that_ends_on_a_progress_note_settles_with_a_parse_error(self):     # Review Focus 1
        note = 'Running the listing first.'
        with self.assertRaises(ValueError) as caught:
            transport._json(note)
        parse_error = f'{type(caught.exception).__name__}: {caught.exception}'     # as transport.invoke records it

        def listed(profile, cwd):
            return b'', turn_context('t1', profile, cwd=cwd) + tool_call('c1', EXEC) + usage_row(THREAD, 't1', 't1', 300, 20)
        command = {'type': 'command_execution', 'command': 'ls', 'exit_code': 0, 'status': 'completed'}
        receipt = self.execute(settings=TERMINAL, rollout=listed, answer=note, stream_items=[command],
                               transport_extra={'response': None, 'parse_error': parse_error})
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertIsNone(receipt['response'])
        self.assertEqual(receipt['parse_error'], parse_error)
        self.assertIsNone(receipt['error'])
        self.assertTrue(receipt['protocol_valid'])
        self.assertEqual((receipt['token_source'], receipt['actual_costs']['tokens']), ('ledger', 320))
        self.assertEqual(self.effect['actual_costs']['tokens'], 320)
        self.assertEqual(receipt['native_activity']['items'], {'command_execution': 1, 'agent_message': 1})
        self.assertEqual((receipt['native_activity']['tool_calls'], receipt['native_activity']['provider_requests']),
                         (1, 1))

    def test_a_resumed_native_session_with_tool_records_runs_and_is_read_with_its_settings(self):
        receipt = self.execute(settings=SEARCH, resumed=True, rollout=SEARCHED_TWICE)
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        self.assertTrue(receipt['protocol_valid'])
        self.assertIsNone(receipt['context_manifest']['native_session_error'])
        self.assertIn('native_prefix', receipt['context_manifest'])
        self.assertEqual(receipt['actual_costs']['tokens'], 430)
        self.assertEqual(receipt['native_activity']['tool_calls'], 1)

    def test_activity_is_recorded_and_never_charged_as_tool_calls(self):
        receipt = self.execute(settings=TERMINAL_AND_SUBAGENTS, rollout=PARENT_WITH_CHILD, child=CHILD_ROWS)
        self.assertNotIn('tool_calls', receipt['actual_costs'])
        self.assertNotIn('tool_calls', self.effect['actual_costs'])
        self.assertEqual(self.store.get('r', 'budget', 'tool_calls')['spent'], 0)
        self.assertEqual(receipt['native_activity']['tool_inputs'][0]['excerpt'], EXEC)
        self.assertEqual(receipt['native_activity']['tool_inputs_omitted'], 0)
        self.assertEqual(receipt['native_activity']['compactions'], 0)

    def test_a_journal_less_call_is_read_with_its_settings(self):
        command = {'type': 'command_execution', 'command': 'ls', 'exit_code': 0, 'status': 'completed'}
        receipt = self.execute(settings=TERMINAL, stream_items=[command])
        self.assertIsNone(receipt['error'])
        self.assertEqual(receipt['native_activity']['commands'], [{'command': 'ls', 'exit_code': 0,
                                                                   'status': 'completed'}])
        self.assertEqual(receipt['native_activity']['items']['command_execution'], 1)

    def test_the_profile_must_deny_the_run_root_and_the_codex_home_offline(self):
        def entry(path, access):
            return {'path': {'type': 'path', 'path': str(path)}, 'access': access}

        def network_open(run_root, codex_home, workspace):
            return {**PROFILE(run_root, codex_home, workspace), 'network': 'enabled'}

        def home_writable(run_root, codex_home, workspace):
            return {'type': 'managed', 'network': 'restricted', 'file_system': {'type': 'restricted', 'entries': [
                entry(run_root, 'deny'), entry(codex_home, 'write'), entry(workspace, 'write')]}}

        def run_root_missing(run_root, codex_home, workspace):
            return {'type': 'managed', 'network': 'restricted', 'file_system': {'type': 'restricted', 'entries': [
                entry(codex_home, 'deny'), entry(workspace, 'write')]}}

        def unrestricted(run_root, codex_home, workspace):
            return {**PROFILE(run_root, codex_home, workspace), 'file_system': {'type': 'unrestricted'}}

        for profile in (network_open, home_writable, run_root_missing, unrestricted):
            with self.subTest(profile=profile.__name__):
                receipt = self.execute(settings=TERMINAL, profile=profile)
                self.assertFalse(receipt['protocol_valid'])
                self.assertIn('Permission profile was not in force', receipt['error'])

    def test_the_profile_is_compared_by_resolved_paths(self):
        aliases = Path(tempfile.mkdtemp(prefix='rsi-native-aliases-'))
        self.addCleanup(shutil.rmtree, aliases, ignore_errors=True)

        def through_links(run_root, codex_home, workspace):
            (aliases / 'run').symlink_to(run_root)
            (aliases / 'home').symlink_to(codex_home)
            return PROFILE(aliases / 'run', aliases / 'home', workspace)
        receipt = self.execute(settings=TERMINAL, profile=through_links)
        self.assertTrue(receipt['protocol_valid'])
        self.assertIsNone(receipt['error'])

    def test_a_terminal_call_is_not_sent_when_the_workspace_holds_codex_instructions(self):
        makers = {'.codex': lambda w: (w / '.codex').mkdir(), '.agents': lambda w: (w / '.agents').mkdir(),
                  'AGENTS.md': lambda w: (w / 'AGENTS.md').write_text('Obey the file.'),
                  'AGENTS.override.md': lambda w: (w / 'AGENTS.override.md').symlink_to(w / 'missing')}
        for name, make in makers.items():
            with self.subTest(name=name):
                receipt = self.execute(settings=TERMINAL, workspace_files=[make])
                self.assertEqual(receipt['status'], 'failed_definite')
                self.assertTrue(receipt['never_dispatched'])
                self.assertIn('Codex instructions or configuration that the Controller did not put there',
                              receipt['error'])
                self.assertIn(name, receipt['error'])
                self.assertEqual(self.calls, [])

    def test_a_terminal_call_is_not_sent_when_the_workspace_tmp_is_not_a_directory(self):
        # The terminal's temporary files go to .tmp, which the transport creates as a directory.
        makers = {'a file': lambda w: (w / '.tmp').write_text('not a folder'),
                  'a dangling link': lambda w: (w / '.tmp').symlink_to(w / 'missing')}
        for case, make in makers.items():
            with self.subTest(case=case):
                receipt = self.execute(settings=TERMINAL, workspace_files=[make])
                self.assertEqual(receipt['status'], 'failed_definite')
                self.assertTrue(receipt['never_dispatched'])
                self.assertIn(".tmp", receipt['error'])
                self.assertIn('is not a directory', receipt['error'])
                self.assertEqual(self.calls, [])
        receipt = self.execute(settings=TERMINAL, workspace_files=[lambda w: (w / '.tmp').mkdir()])
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])

    def test_only_the_terminal_checks_the_workspace(self):
        receipt = self.execute(settings=SEARCH, workspace_files=[lambda w: (w / 'AGENTS.md').write_text('Notes')])
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        self.assertEqual(len(self.calls), 1)

    def assert_refused(self, receipt, *phrases):
        """The call was refused by the executor's preflight: never dispatched, its Effect settled as a definite
        failure with its reservations released, and its error holding each of ``phrases``."""
        self.assertEqual((receipt['status'], receipt['never_dispatched']), ('failed_definite', True))
        self.assertTrue(receipt['error'].startswith('Persistent session preflight failed: '), receipt['error'])
        for phrase in phrases:
            self.assertIn(phrase, receipt['error'])
        self.assertEqual(self.calls, [])
        self.assertEqual(receipt['actual_costs'], {name: 0 for name in RESERVATION})
        self.assertEqual(self.effect['status'], 'failed_definite')
        for name in RESERVATION:
            budget = self.store.get('r', 'budget', name)
            self.assertEqual((budget['reserved'], budget['spent']), (0, 0), name)

    def workspace(self):
        """The workspace of the session of the last call ``execute`` made."""
        return self.store.root / 'agents' / self.effect['metadata']['agent_session_id'] / 'workspace'

    @staticmethod
    def put_skill_files(store):
        for raw in (SKILL_MD, CHECK_PY):
            store.put_blob(raw)

    @staticmethod
    def agents_notes(workspace):
        (workspace / '.agents').mkdir()
        (workspace / '.agents' / 'notes.md').write_text('Notes left in .agents.', encoding='utf-8')

    def files_in_workspace(self):
        workspace = self.workspace()
        return sorted(str(path.relative_to(workspace)) for path in workspace.rglob('*') if path.is_file())

    def test_a_call_with_skills_writes_them_and_replaces_agents(self):
        receipt = self.execute(settings=WITH_SKILLS, arrange=self.put_skill_files, workspace_files=[self.agents_notes],
                               rollout=listing(DECLARED))
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        self.assertTrue(receipt['protocol_valid'], receipt['error'])
        self.assertEqual(receipt['native_activity']['skills_shown'], ['plot-check'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['native'], WITH_SKILLS)
        self.assertEqual(Path(self.calls[0]['session']['workspace']), self.workspace())
        self.assertEqual(self.files_in_workspace(), ['.agents/skills/plot-check/SKILL.md',
                                                     '.agents/skills/plot-check/scripts/check.py'])
        self.assertEqual((self.workspace() / '.agents/skills/plot-check/SKILL.md').read_bytes(), SKILL_MD)
        self.assertEqual((self.workspace() / '.agents/skills/plot-check/scripts/check.py').read_bytes(), CHECK_PY)

    def test_a_list_other_than_the_declared_one_makes_the_call_invalid(self):
        receipt = self.execute(settings=WITH_SKILLS, arrange=self.put_skill_files, rollout=listing(DECLARED, SECRET))
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], 'Codex showed skills other than the declared ones: ' + THREAD
                         + ': 1 unexpected from ' + OWNERS)
        self.assertEqual((receipt['token_source'], receipt['actual_costs']['tokens']), ('ledger', 11))
        self.assertEqual(self.effect['actual_costs']['tokens'], 11)
        self.assertEqual(receipt['native_activity']['skills_shown'], ['plot-check'])
        self.assertEqual(receipt['native_activity']['skills_unexpected'], {'count': 1, 'folders': [OWNERS]})
        self.assertNotIn('secret-notes', json.dumps(receipt))
        # Joined with the other findings of the settlement.
        receipt = self.execute(settings=WITH_SKILLS, arrange=self.put_skill_files, rollout=listing(DECLARED, SECRET),
                               profile=lambda *places: None)
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], 'Permission profile was not in force; Codex showed skills other than the '
                                           'declared ones: ' + THREAD + ': 1 unexpected from ' + OWNERS)
        # A fresh thread with no list at all.
        receipt = self.execute(settings=WITH_SKILLS, arrange=self.put_skill_files)
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], 'Codex showed skills other than the declared ones: ' + THREAD + ': no list')
        # A role without skills is shown none, whether something else is on or everything is off.
        for settings in (TERMINAL, None):
            with self.subTest(settings=settings):
                receipt = self.execute(settings=settings, rollout=listing(SECRET))
                self.assertEqual(receipt['status'], 'succeeded')
                self.assertFalse(receipt['protocol_valid'])
                self.assertEqual(receipt['error'], 'Codex showed a skills list to a role without skills: ' + THREAD
                                 + ': 1 skills from ' + OWNERS)
                self.assertEqual(receipt['actual_costs']['tokens'], 11)
                self.assertNotIn('secret-notes', json.dumps(receipt))
                self.assertNotIn('skills_shown', json.dumps(receipt))

    def test_a_role_sub_agents_list_is_read_from_the_second_part_of_its_message(self):
        # As Codex 0.170.0 writes them: the lead's list first in its developer message, then the permissions and
        # collaboration parts; a sub-agent started with a declared role gets the role's instructions first.
        def lead(*skills):
            def rollout(profile, cwd):
                return b'', (skills_list([str(cwd / '.agents' / 'skills')], *skills, after=ROLE_LESS_PARTS)
                             + turn_context('t1', profile, cwd=cwd) + sub_agent_started(CHILD, '/root/local')
                             + usage_row(THREAD, 't1', 't1', 10, 1))
            return rollout

        def role_child(*skills):
            def records(workspace):
                return skills_list([str(workspace / '.agents' / 'skills'), OWNERS], *skills,
                                   before=(ROLES['local']['instructions'],), after=ROLE_LESS_PARTS)
            return records
        settings = {**WITH_SKILLS, 'subagents': {'max_concurrent': 3, 'roles': ROLES}}
        receipt = self.execute(settings=settings, arrange=self.put_skill_files, rollout=lead(DECLARED),
                               child=CHILD_ROWS, child_records=role_child(DECLARED))
        self.assertTrue(receipt['protocol_valid'], receipt['error'])
        self.assertEqual(receipt['native_activity']['skills_shown'], ['plot-check'])
        [entry] = receipt['child_threads']
        self.assertEqual(entry['skills_shown'], ['plot-check'])
        # A role without skills is shown none: the role sub-agent's list is seen in its second part.
        without = {**TERMINAL, 'subagents': {'max_concurrent': 3, 'roles': ROLES}}
        receipt = self.execute(settings=without, rollout=PARENT_WITH_CHILD, child=CHILD_ROWS,
                               child_records=role_child(SECRET))
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], 'Codex showed a skills list to a role without skills: ' + CHILD
                         + ': 1 skills from ' + OWNERS)
        self.assertNotIn('secret-notes', json.dumps(receipt))

    def test_the_refusals_the_design_keeps(self):
        # With skills declared, .agents is the kernel's own; .codex and the AGENTS files still refuse the call.
        makers = {'.codex': lambda w: (w / '.codex').mkdir(),
                  'AGENTS.md': lambda w: (w / 'AGENTS.md').write_text('Obey the file.')}
        for name, make in makers.items():
            with self.subTest(settings='skills', name=name):
                receipt = self.execute(settings=WITH_SKILLS, arrange=self.put_skill_files, workspace_files=[make])
                self.assert_refused(receipt, 'Codex instructions or configuration that the Controller did not put '
                                    'there: ' + name)
                self.assertFalse(os.path.lexists(self.workspace() / '.agents'))    # Refused before skills are written.
        # Without skills, .agents refuses a terminal call as before.
        with self.subTest(settings='terminal', name='.agents'):
            receipt = self.execute(settings=TERMINAL, workspace_files=[self.agents_notes])
            self.assert_refused(receipt, 'Codex instructions or configuration that the Controller did not put '
                                'there: .agents')
            self.assertEqual(self.files_in_workspace(), ['.agents/notes.md'])

    def test_a_skills_call_whose_files_the_store_lacks_is_refused(self):
        receipt = self.execute(settings=WITH_SKILLS, workspace_files=[self.agents_notes])
        self.assert_refused(receipt, 'plot-check/SKILL.md')
        self.assertEqual(self.files_in_workspace(), ['.agents/notes.md'])

    def test_an_unset_variable_refuses_the_call_before_dispatch(self):          # Review Focus 4
        settings = {**SEARCH, 'tool_servers': {'chem': here(CHEM)}}
        for value in (None, ''):
            with self.subTest(value=value), patch.dict(os.environ):
                os.environ.pop('CHEM_API_KEY', None)
                if value is not None:
                    os.environ['CHEM_API_KEY'] = value
                receipt = self.execute(settings=settings)
                self.assert_refused(receipt, 'Native settings name environment variables that are not set: '
                                    'CHEM_API_KEY')
        with patch.dict(os.environ, {'CHEM_API_KEY': 'invented-secret-4f1c'}):
            receipt = self.execute(settings=settings)
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        self.assertEqual(len(self.calls), 1)

    def test_a_codex_home_with_skills_refuses_a_skills_call(self):
        def skills_of_its_own(store):
            # Made after the Runtime started, which would have refused to start in such a home.
            self.put_skill_files(store)
            (self.codex_home / 'skills' / 'x').mkdir(parents=True)
        receipt = self.execute(settings=WITH_SKILLS, arrange=skills_of_its_own)
        self.assert_refused(receipt, 'separate Codex home', str(self.codex_home / 'skills') + ' holds entries')
        self.assertNotIn("'x'", receipt['error'])
        self.assertFalse(os.path.lexists(self.workspace() / '.agents'))
        # A call without skills goes ahead in the same home.
        receipt = self.execute(settings=TERMINAL)
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])

    @unittest.skipIf(os.geteuid() == 0, 'root lists a folder whatever its mode')
    def test_a_skills_folder_that_cannot_be_listed_refuses_a_skills_call(self):
        folder = self.codex_home / 'skills'

        def unreadable(store):
            self.put_skill_files(store)
            folder.mkdir(mode=0o000)
            self.addCleanup(folder.chmod, 0o700)
        receipt = self.execute(settings=WITH_SKILLS, arrange=unreadable)
        self.assert_refused(receipt, 'Permission denied', str(folder))
        self.assertFalse(os.path.lexists(self.workspace() / '.agents'))

    def test_executor_writes_role_files_and_names_them(self):
        # The real transport runs; a local process stands in for Codex, as in tests/v2/test_native_recovery.py.
        store, controller, runtime = open_run(self, config={'native_capabilities': {'lab': config_form(WITH_ROLES)}})
        codex_home = Path(tempfile.mkdtemp(prefix='rsi-native-home-'))
        self.addCleanup(shutil.rmtree, codex_home, ignore_errors=True)
        effect = runtime.prepare_meta(runtime.enqueue_lab('lab-1')['id'], RESERVATION)
        self.assertEqual(effect['status'], 'prepared', effect)
        self.assertEqual(effect['metadata']['native_capabilities'], WITH_ROLES)
        roles_dir = (store.root / 'agents' / effect['metadata']['agent_session_id'] / 'roles').resolve()
        events = [{'type': 'thread.started', 'thread_id': THREAD}, {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(RESPONSE)}},
                  {'type': 'turn.completed', 'usage': STREAM_USAGE}]
        wire = ''.join(json.dumps(event) + '\n' for event in events).encode()
        real_popen = subprocess.Popen
        found = []

        def spawn(command, **options):
            found.append(sorted(path.name for path in roles_dir.iterdir()))     # What Codex finds as it starts.
            workspace = Path(options['cwd'])
            path = codex_home / 'sessions' / 'fixture' / ('rollout-fixture-' + THREAD + '.jsonl')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fixture(workspace) + usage_row(THREAD, 't1', 't1', 10, 1))
            code = 'import sys; sys.stdin.read(); sys.stdout.buffer.write(%r); sys.stdout.buffer.flush()' % wire
            return real_popen([sys.executable, '-u', '-c', code], **options)
        receipts, raised = [], []

        def executor(dispatching, raw):
            try:
                receipts.append(roles.CodexExecutor(store.root / 'calls')(dispatching, raw))
            except ValueError as exc:
                raised.append(exc)
                raise
            return receipts[-1]
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home)}), \
                patch.object(transport, 'codex_version', return_value='0.170.0'), \
                patch.object(transport.subprocess, 'Popen', side_effect=spawn):
            controller.dispatch_effect('r', effect['id'], executor)
        self.assertEqual(raised, [])
        self.assertEqual(receipts[0]['status'], 'succeeded', receipts[0]['error'])
        self.assertTrue(receipts[0]['protocol_valid'], receipts[0]['error'])
        self.assertEqual(found, [['challenge.toml', 'local.toml']])
        for name, role in ROLES.items():
            self.assertEqual(tomllib.loads((roles_dir / (name + '.toml')).read_text(encoding='utf-8')),
                             {'name': name, 'description': role['description'],
                              'developer_instructions': role['instructions']})
        command = json.loads((store.root / 'calls' / effect['id'] / 'invocation.json').read_text(encoding='utf-8'))[
            'command']
        named = [command[i + 1] for i, part in enumerate(command[:-1])
                 if part == '-c' and command[i + 1].startswith('agents.') and '.config_file=' in command[i + 1]]
        self.assertEqual(named, ['agents.' + name + '.config_file=' + native.toml_string(str(roles_dir / (name + '.toml')))
                                 for name in ('challenge', 'local')])

    def test_a_call_without_roles_gets_no_role_folder(self):
        receipt = self.execute(settings=PLAIN)
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        self.assertEqual(set(self.calls[0]), {'session', 'journal_metadata', 'native', 'run_root'})
        self.assertEqual(self.calls[0]['native'], PLAIN)
        self.assertEqual(receipt['native_capabilities'], PLAIN)
        roles_dir = self.store.root / 'agents' / self.effect['metadata']['agent_session_id'] / 'roles'
        self.assertFalse(os.path.lexists(roles_dir))
        # The same place holds the role files of a call that declares roles.
        receipt = self.execute(settings=WITH_ROLES, rollout=PARENT_WITH_CHILD, child=CHILD_ROWS)
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        roles_dir = self.store.root / 'agents' / self.effect['metadata']['agent_session_id'] / 'roles'
        self.assertEqual(sorted(path.name for path in roles_dir.iterdir()), ['challenge.toml', 'local.toml'])

    def test_receipt_names_roles_by_digest(self):
        receipt = self.execute(settings=WITH_ROLES, rollout=PARENT_WITH_CHILD, child=CHILD_ROWS)
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        local = receipt['native_capabilities']['subagents']['roles']['local']
        self.assertEqual(local, {'description': R['description'],
                                 'instructions_sha256': hashlib.sha256(R['instructions'].encode('utf-8')).hexdigest()})
        self.assertNotIn('instructions', local)
        self.assertEqual(receipt['native_capabilities'], native.receipt_form(WITH_ROLES))
        self.assertEqual(self.effect['metadata']['native_capabilities'], WITH_ROLES)    # The Effect keeps the text.
        receipt = self.execute(settings=PLAIN, rollout=PARENT_WITH_CHILD, child=CHILD_ROWS)
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        self.assertNotIn('roles', receipt['native_capabilities']['subagents'])
        self.assertEqual(receipt['native_capabilities'], PLAIN)

    def test_child_threads_record_each_sub_agent_with_or_without_roles(self):
        for settings in (PLAIN, WITH_ROLES):
            with self.subTest(roles='roles' in settings['subagents']):
                receipt = self.execute(settings=settings, rollout=PARENT_WITH_CHILD, child=CHILD_ROWS)
                self.assertEqual(receipt['token_source'], 'ledger')
                [entry] = receipt['child_threads']
                self.assertEqual(set(entry), {'thread_id', 'agent_path', 'started_in_call', 'model', 'effort',
                                              'rollout_ref', 'rollout_sha256', 'requests', 'input_tokens',
                                              'output_tokens', 'cached_input_tokens', 'role', 'forked_history',
                                              'searches', 'searches_omitted'})
                self.assertEqual({key: entry[key] for key in set(entry) - {'rollout_ref', 'rollout_sha256'}},
                                 {'thread_id': CHILD, 'agent_path': '/root/a', 'started_in_call': True, 'model': None,
                                  'effort': None, 'requests': 1, 'input_tokens': 22828, 'output_tokens': 37,
                                  'cached_input_tokens': 0, 'role': None, 'forked_history': True, 'searches': [],
                                  'searches_omitted': 0})

    def test_receipts_carry_the_records_only_for_roles_that_have_the_tool(self):
        servers = {'chem': here(CHEM)}
        script = 'text(await tools.mcp__chem__lookup({query:"CCO"}));'

        def lead(*, child=False, call=False, image=False):
            def rollout(profile, cwd):
                return b'', (turn_context('t1', profile, cwd=cwd)
                             + (sub_agent_started(CHILD, '/root/a') if child else b'')
                             + (tool_call('c1', script) + mcp_call('exec-1111', 'chem', 'lookup', {'query': 'CCO'},
                                                                   turn='t1') if call else b'')
                             + (viewed_image('exec-2222', str(cwd / 'plot.png'), PNG, turn='t1') if image else b'')
                             + usage_row(THREAD, 't1', 't1', 10, 1))
            return rollout

        def child_records(workspace):
            return (turn_context('c1', cwd=workspace)
                    + mcp_call('exec-c1', 'chem', 'lookup', {'query': 'CCO'}, turn='c1', thread=CHILD)
                    + viewed_image('exec-c2', str(workspace / 'fit.png'), PNG, turn='c1', thread=CHILD))
        arguments = json.dumps({'query': 'CCO'}, ensure_ascii=False, sort_keys=True)

        def called(call_id):
            return {'server': 'chem', 'tool': 'lookup', 'status': 'completed', 'call_id': call_id,
                    'arguments_sha256': digest(arguments.encode()), 'arguments_excerpt': arguments,
                    'result_bytes': len(encode(MCP_RESULT)), 'duration_seconds': 0.000439666}

        def viewed(name):
            return {'paths': ['file://' + str(self.workspace() / name)], 'paths_omitted': 0, 'count': 1, 'bytes': 68}
        records = ('tool_server_calls', 'tool_server_calls_omitted', 'images_viewed')
        cases = {'tool servers': ({'settings': {**SEARCH, 'tool_servers': servers}, 'rollout': lead(call=True)},
                                  records[:2]),
                 'image viewing': ({'settings': VIEW_IMAGE, 'rollout': lead(image=True)}, records[2:]),
                 'neither': ({'settings': TERMINAL_AND_SUBAGENTS, 'rollout': PARENT_WITH_CHILD, 'child': CHILD_ROWS},
                             ()),
                 'both, with a sub-agent': ({'settings': {**TERMINAL_AND_SUBAGENTS, 'view_image': True,
                                                          'tool_servers': servers},
                                             'rollout': lead(child=True, call=True, image=True), 'child': CHILD_ROWS,
                                             'child_records': child_records}, records)}
        for case, (arguments_of_call, recorded) in cases.items():
            with self.subTest(case=case), patch.dict(os.environ, {'CHEM_API_KEY': 'invented-secret-4f1c'}):
                receipt = self.execute(**arguments_of_call)
                self.assertEqual((receipt['status'], receipt['error']), ('succeeded', None))
                self.assertTrue(receipt['protocol_valid'])
                self.assertNotIn('invented-secret-4f1c', json.dumps(receipt, ensure_ascii=False))
                activity = receipt['native_activity']
                self.assertEqual({name for name in records if name in activity}, set(recorded))
                if 'tool_server_calls' in recorded:
                    self.assertEqual((activity['tool_server_calls'], activity['tool_server_calls_omitted']),
                                     ([called('exec-1111')], 0))
                if 'images_viewed' in recorded:
                    self.assertEqual(activity['images_viewed'], viewed('plot.png'))
                for entry in receipt['child_threads']:
                    self.assertEqual({name for name in records if name in entry}, set(recorded))
                    self.assertNotIn('image_view_paths', entry)
                    if recorded == records:
                        self.assertEqual((entry['tool_server_calls'], entry['tool_server_calls_omitted'],
                                          entry['images_viewed']), ([called('exec-c1')], 0, viewed('fit.png')))
                self.assertEqual(len(receipt['child_threads']), 1 if 'child' in arguments_of_call else 0)

    def test_a_viewed_image_outside_the_folders_the_role_may_read_makes_the_call_invalid(self):
        def viewing(where):
            def rollout(profile, cwd):
                return b'', (turn_context('t1', profile, cwd=cwd) + viewed_image('exec-2222', str(where(cwd)), PNG,
                                                                                  turn='t1')
                             + usage_row(THREAD, 't1', 't1', 10, 1))
            return rollout
        # The session's role files lie beside its workspace, in the run root the terminal is denied.
        outside = viewing(lambda cwd: cwd.parent / 'roles' / 'plot.png')
        receipt = self.execute(settings=VIEW_IMAGE, rollout=outside)
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], 'Codex showed images outside the folders this role may read: 1 of 1')
        self.assertEqual((receipt['token_source'], receipt['actual_costs']['tokens']), ('ledger', 11))
        # Joined with the other finding of the settlement.
        receipt = self.execute(settings=VIEW_IMAGE, rollout=outside, profile=lambda *places: None)
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], 'Permission profile was not in force; Codex showed images outside the '
                                           'folders this role may read: 1 of 1')
        receipt = self.execute(settings=VIEW_IMAGE, rollout=viewing(lambda cwd: cwd / 'plot.png'))
        self.assertTrue(receipt['protocol_valid'])
        self.assertIsNone(receipt['error'])

    def test_a_sub_agent_that_was_not_read_makes_a_viewing_call_invalid(self):
        # The turn started CHILD, whose rollout is not there: what it viewed cannot be checked.
        # Nor can the skills list it was shown, which a role without skills must not have.
        unread = 'Codex showed a skills list to a role without skills: ' + CHILD + ': rollout unreadable'
        receipt = self.execute(settings={**TERMINAL_AND_SUBAGENTS, 'view_image': True}, rollout=PARENT_WITH_CHILD)
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], "Codex's sub-agents could not be checked for images outside the folders "
                                           'this role may read: 1 of 1; ' + unread)
        self.assertEqual((receipt['token_source'], receipt['child_threads']), ('unknown', []))
        # Without image viewing the same call is invalid for its skills alone; its usage is unknown.
        receipt = self.execute(settings=TERMINAL_AND_SUBAGENTS, rollout=PARENT_WITH_CHILD)
        self.assertFalse(receipt['protocol_valid'])
        self.assertEqual(receipt['error'], unread)
        self.assertEqual(receipt['token_source'], 'unknown')

    def dispatch_refused_roles(self, *, arrange=None, alter=None):
        """Dispatch a Lab call whose Effect declares ``ROLES`` through the Controller, the executor and the real
        transport, with a ``Popen`` that fails if Codex is started, and check that the call settles as a definite
        failure that was never dispatched, its reservations released. ``arrange(session_folder)`` prepares the
        session's folder first; ``alter(dispatching, raw)`` returns the Effect and packet the executor is handed.
        Returns the executor's receipt."""
        store, controller, runtime = open_run(self, config={'native_capabilities': {'lab': config_form(WITH_ROLES)}})
        self.store = store
        effect = runtime.prepare_meta(runtime.enqueue_lab('lab-2')['id'], RESERVATION)
        self.assertEqual(effect['status'], 'prepared', effect)
        if arrange is not None:
            arrange(store.root / 'agents' / effect['metadata']['agent_session_id'])
        receipts = []

        def executor(dispatching, raw):
            if alter is not None:
                dispatching, raw = alter(dispatching, raw)
            receipts.append(roles.CodexExecutor(store.root / 'calls')(dispatching, raw))
            return receipts[-1]
        with patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('Codex started')) as popen:
            settled = controller.dispatch_effect('r', effect['id'], executor)
        popen.assert_not_called()
        # The executor returned a receipt; it raised nothing. Else the Controller's receipt says what it raised.
        self.assertEqual(len(receipts), 1, store.read_blob(settled['receipt_ref']))
        receipt = receipts[0]
        self.assertEqual((receipt['status'], receipt['never_dispatched']), ('failed_definite', True))
        self.assertEqual(receipt['actual_costs'], {name: 0 for name in RESERVATION})
        self.assertEqual(settled['status'], 'failed_definite')
        for name in RESERVATION:
            budget = store.get('r', 'budget', name)
            self.assertEqual((budget['reserved'], budget['spent']), (0, 0), name)
        self.assertFalse((store.root / 'calls' / effect['id']).exists())
        return receipt

    def test_executor_refuses_roles_without_a_persistent_session(self):
        def without_session(dispatching, raw):
            # The same Effect, but for the persistent session its call would run in.
            metadata = {key: value for key, value in dispatching['metadata'].items() if key != 'agent_session'}
            return {**dispatching, 'metadata': metadata}, raw
        with patch.object(roles.transport, 'invoke', side_effect=AssertionError('The call was dispatched')) as invoke:
            receipt = self.dispatch_refused_roles(alter=without_session)
        invoke.assert_not_called()
        self.assertEqual(receipt['error'], 'Sub-agent roles need a persistent session')
        self.assertEqual(list((self.store.root / 'agents').glob('*/roles')), [])

    def test_a_role_file_that_cannot_be_written_refuses_the_call(self):
        elsewhere = Path(tempfile.mkdtemp(prefix='rsi-native-elsewhere-'))
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)

        def linked_folder(session):
            session.mkdir(parents=True, exist_ok=True)
            (session / 'roles').symlink_to(elsewhere, target_is_directory=True)

        def linked_file(session):
            (session / 'roles').mkdir(parents=True)
            (session / 'roles' / 'local.toml').symlink_to(elsewhere / 'outside.toml')
        for case, arrange in (('a symlinked role folder', linked_folder), ('a symlinked role file', linked_file)):
            with self.subTest(case=case):
                receipt = self.dispatch_refused_roles(arrange=arrange)
                self.assertTrue(receipt['error'].startswith('Persistent session preflight failed: '), receipt['error'])
                self.assertIn('symlink', receipt['error'])
                self.assertEqual(list(elsewhere.iterdir()), [])

    def test_role_text_that_cannot_be_utf8_refuses_the_call(self):
        # A run config cannot carry a lone surrogate into an Effect; here the packet and the Effect are made to.
        settings = {**WITH_ROLES, 'subagents': {'max_concurrent': 3, 'roles': {
            **ROLES, 'local': {**R, 'instructions': 'Refine \ud800.'}}}}

        def lone_surrogate(dispatching, raw):
            request = json.loads(raw)
            self.assertEqual(request['prompt'].count('"instructions":"Refine."'), 1)
            request['prompt'] = request['prompt'].replace('"instructions":"Refine."',
                                                          '"instructions":"Refine \\ud800."')
            raw = json.dumps(request, ensure_ascii=False).encode()
            return {**dispatching, 'packet_sha256': digest(raw),
                    'metadata': {**dispatching['metadata'], 'native_capabilities': settings}}, raw
        receipt = self.dispatch_refused_roles(alter=lone_surrogate)
        self.assertTrue(receipt['error'].startswith('Persistent session preflight failed: '), receipt['error'])
        self.assertIn('cannot be encoded as UTF-8', receipt['error'])
        self.assertEqual(list((self.store.root / 'agents').glob('*/roles/*')), [])


if __name__ == '__main__':
    unittest.main()
