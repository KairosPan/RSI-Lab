"""The executor of a role's model call: its native settings, its cost from Codex's ledger and the turn's activity.

The Lab Effect is prepared and dispatched by the real Runtime and Controller.
``transport.invoke`` is replaced, as tests/v2/test_roles.py replaces it, by a
fixture that writes the call's stream and a synthetic rollout built from
tests/v2/native_fixtures.py. No Codex, model, network request or credential is
used.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rsi_lab import native, roles, transport
from rsi_lab.session_context import read_native_snapshot
from rsi_lab.store import digest
from tests.v2.liveness_support import Scripted, none, open_run
from tests.v2.native_fixtures import (PROFILE, SEARCH, TERMINAL, TERMINAL_AND_SUBAGENTS, account, child_rollout,
                                      sub_agent_started, tool_call, turn_context, usage_row)
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


def SEARCHED_TWICE(profile, cwd):
    search = 'text(await tools.web_search({query: "%s"}))'
    return (tool_call('c1', search % 'first') + usage_row(THREAD, 't1', 't1', 300, 20),
            tool_call('c2', search % 'second') + usage_row(THREAD, 't2', 't2', 400, 30))


def config_form(settings):
    """A normal-form settings object as a run config writes it: a capability that is off is ``false``."""
    return {name: False if value is None else value for name, value in settings.items()}


class NativeExecutorTests(unittest.TestCase):
    def execute(self, *, settings, rollout=CONFINED_TURN, child=None, stream_usage=STREAM_USAGE, resumed=False,
                packet_settings=SAME, transport_extra=None, stream_items=(), workspace_files=(), profile=None,
                finished=True, answer=json.dumps(RESPONSE)):
        """Prepare a Lab turn under ``packet_settings`` (``settings`` unless given) and run it through the Controller
        with the executor, which sees ``settings`` in the Effect. Returns the executor's receipt; a ``ValueError``
        it raises is raised here. ``profile(run_root, codex_home, workspace)`` replaces the confined profile.
        ``answer`` is the text of the turn's last message. A call that is not ``finished`` was killed before its
        answer and terminal event (``UNFINISHED``)."""
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
                    child_rollout(CHILD, THREAD, child, cwd=workspace))
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
                                             'cached_input_tokens': 0})
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
                                             'cached_input_tokens': 0})
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
                     {'requests': 1, 'input_tokens': 102016, 'output_tokens': 5155, 'cached_input_tokens': 0}),
                 'a native call with a sub-agent': (
                     {'settings': TERMINAL_AND_SUBAGENTS, 'rollout': PARENT_WITH_CHILD, 'child': CHILD_ROWS},
                     83032 + 291 + 22828 + 37,
                     {'requests': 2, 'input_tokens': 83032 + 22828, 'output_tokens': 291 + 37,
                      'cached_input_tokens': 0})}
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

    def test_only_the_terminal_checks_the_workspace(self):
        receipt = self.execute(settings=SEARCH, workspace_files=[lambda w: (w / 'AGENTS.md').write_text('Notes')])
        self.assertEqual(receipt['status'], 'succeeded', receipt['error'])
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
