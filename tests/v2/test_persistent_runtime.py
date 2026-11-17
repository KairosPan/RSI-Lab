"""Synthetic native Codex process integration: no network or experiment data.

Only Popen is redirected to a local protocol fixture. The real Runtime,
Controller, CodexExecutor, streaming transport, journal, owned rollout reader,
and SQLite transactions all run unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab import transport
from rsi_lab.controller import Controller, identity
from rsi_lab.journal import read_journal
from rsi_lab.roles import CodexExecutor
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store


PROVIDER = r'''
import json,os,sys,uuid
from pathlib import Path
spec=json.loads(sys.argv[1])
prompt=sys.stdin.read()
cwd=str(Path.cwd().resolve())
thread=spec.get('thread_override') or spec['resume_thread'] or str(uuid.uuid5(uuid.NAMESPACE_URL,cwd))
home=Path(os.environ['CODEX_HOME'])
folder=home/'sessions'/'fixture'
folder.mkdir(parents=True,exist_ok=True)
path=folder/('rollout-fixture-'+thread+'.jsonl')
records=[]
if not path.exists():
 records.append({'type':'session_meta','payload':{'id':thread,'cwd':cwd,'originator':'synthetic-local-provider'}})
records.extend([
 {'type':'turn_context','payload':{'cwd':cwd,'model':'synthetic-model'}},
 {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':prompt}]}},
 {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':spec['output']}]}},
])
with path.open('ab') as f:
 for record in records:
  f.write((json.dumps(record,sort_keys=True)+'\n').encode())
 f.flush();os.fsync(f.fileno())
if spec.get('compact'):
 with path.open('ab') as f:
  f.write((json.dumps({'type':'compacted','payload':{'summary':'Synthetic opaque compaction'}})+'\n').encode());f.flush();os.fsync(f.fileno())
if spec.get('rewrite_prefix'):
 data=path.read_bytes().replace(b'synthetic-local-provider',b'modified--local-provider',1)
 path.write_bytes(data)
events=[{'type':'thread.started','thread_id':thread},{'type':'turn.started'}]
if spec.get('failure'):
 events.append({'type':'turn.failed','error':{'message':'Synthetic known failure'},
                'usage':{'input_tokens':20,'output_tokens':5}})
else:
 events.extend([{'type':'item.completed','item':{'type':'agent_message','text':spec['output']}},
                {'type':'turn.completed','usage':{'input_tokens':20,'output_tokens':5}}])
for event in events:
 print(json.dumps(event),flush=True)
'''


class PersistentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'native-home'
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.home)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.store = Store(self.root / 'run'); self.addCleanup(self.store.close)
        self.c = Controller(self.store)
        self.c.initialize('run', budgets={
            name: {'limit': limit, 'hard': name.endswith('calls')}
            for name, limit in {'model_calls': 30, 'tool_calls': 30,
                                'tokens': 100000, 'wall_seconds': 1000}.items()},
            config={'execution_mode': 'live', 'model': 'synthetic-model', 'effort': 'medium',
                    'task_id': 'synthetic-task', 'goal': 'Inspect synthetic input',
                    'max_open_reviews': 4, 'open_review_interval_batches': 1,
                    'max_effective_context_bytes': 1000000})
        self.rt = Runtime(self.store, 'run')
        self.executor = CodexExecutor(self.store.root / 'calls')
        self.reservations = {'model_calls': 1, 'tokens': 1000, 'wall_seconds': 10}
        self.calls = []
        self.outputs = []
        self.real_popen = subprocess.Popen
        self.process_patch = patch.object(transport.subprocess, 'Popen', side_effect=self.spawn)
        self.process_patch.start(); self.addCleanup(self.process_patch.stop)

    def spawn(self, command, **kwargs):
        self.calls.append(command)
        if not self.outputs:
            raise AssertionError('Unexpected additional model invocation')
        spec = self.outputs.pop(0)
        spec['resume_thread'] = command[-2] if 'resume' in command else None
        return self.real_popen([sys.executable, '-u', '-c', PROVIDER, json.dumps(spec)], **kwargs)

    def output(self, mode, *, delta=None, raw=None, **faults):
        response = {'command': {'mode': mode, 'action': 'no_action', 'issue_id': None,
                    'expected_versions': {'issue_version': 0, 'evidence_revision': 0, 'scheduling_revision': 0},
                    'evidence_ids': [], 'payload': {'reason': 'Synthetic inspection completed'}},
                    'working_memory_delta': json.dumps(delta or {})}
        self.outputs.append({'output': raw if raw is not None else json.dumps(response), **faults})

    def lab(self, trigger):
        work = self.rt.enqueue_lab(trigger)
        return self.rt.run_lab(work['id'], self.executor, self.reservations)

    def session(self, role):
        rows = self.store.list('run', 'agent_session', role=role)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def assert_settled(self):
        for budget in self.store.list('run', 'budget'):
            self.assertEqual(budget['reserved'], 0, budget)
        self.store.verify_history('run')

    def test_lab_resumes_same_native_thread_and_meta_has_own_memory(self):
        self.output('lab', delta={'next_step': 'Check synthetic B', 'attempted_steps': ['Synthetic A rejected']})
        first = self.lab('lab-first')
        self.assertEqual(first['effect']['status'], 'succeeded')
        self.assertEqual(first['proposal']['status'], 'applied', first)
        lab1 = self.session('lab')
        self.assertEqual(lab1['committed_turn'], 1)
        self.assertTrue(Path(lab1['context_manifest']['native_session_ref']).is_file())
        memory = self.rt.agents.read_working_memory(lab1['id'], actor_session_id=lab1['id'])
        self.assertEqual(memory['content']['next_step'], 'Check synthetic B')
        first_prefix = lab1['context_manifest']['native_prefix']
        self.output('lab', delta={'next_step': 'Compare synthetic controls'})
        second = self.lab('lab-second')
        self.assertEqual(second['proposal']['status'], 'applied', second)
        lab2 = self.session('lab')
        self.assertEqual(lab2['id'], lab1['id'])
        self.assertEqual(lab2['provider_thread_id'], lab1['provider_thread_id'])
        self.assertEqual(lab2['committed_turn'], 2)
        self.assertGreater(lab2['context_manifest']['native_prefix']['prefix_bytes'], first_prefix['prefix_bytes'])
        prompt = (self.store.root / 'calls' / second['effect']['id'] / 'prompt.txt').read_text()
        self.assertIn('Check synthetic B', prompt)
        self.assertIn('Synthetic A rejected', prompt)
        self.assertIn('resume', self.calls[1])
        self.assertEqual(self.calls[1][-2], lab1['provider_thread_id'])

        self.output('discovery', delta={'next_step': 'Inspect synthetic source provenance'})
        review = self.rt.observe('meta-inspect', 'batch_boundary', 1)
        meta_result = self.rt.run_meta(review['work_id'], self.executor, self.reservations)
        self.assertEqual(meta_result['proposal']['status'], 'applied', meta_result)
        meta = self.session('meta')
        self.assertNotEqual(meta['provider_thread_id'], lab2['provider_thread_id'])
        self.assertNotEqual(meta['id'], lab2['id'])
        meta_prompt = (self.store.root / 'calls' / meta_result['effect']['id'] / 'prompt.txt').read_text()
        self.assertNotIn('Synthetic A rejected', meta_prompt)
        with self.assertRaisesRegex(ValueError, 'owner'):
            self.rt.agents.read_working_memory(lab2['id'], actor_session_id=meta['id'])
        self.assertEqual(self.store.list('run', 'fact'), [], 'Private notes must not become shared facts')
        for command in self.calls:
            self.assertNotIn('--ephemeral', command)
            self.assertNotIn('--last', command)
        self.assert_settled()

    def test_driver_executes_ready_lab_work(self):
        self.output('lab', delta={'next_step': 'Await the next bounded task'})
        work = self.rt.enqueue_lab('driver-work')
        result = self.rt.run_until_wait(self.executor, self.reservations, max_steps=2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.get('run', 'work', work['id'])['status'], 'succeeded')
        self.assertEqual(self.session('lab')['committed_turn'], 1)
        self.assertEqual(result['status'], 'quiescent')
        self.assert_settled()

    def test_known_invalid_json_commits_native_turn_and_releases_reservations(self):
        self.output('lab', raw='invalid JSON from a completed synthetic turn')
        result = self.lab('invalid-format')
        self.assertEqual(result['effect']['status'], 'succeeded', result)
        self.assertEqual(result['proposal']['status'], 'rejected')
        session = self.session('lab')
        self.assertEqual(session['status'], 'idle')
        self.assertEqual(session['committed_turn'], 1)
        self.assertEqual(self.rt.agents.working_memory(session['id'])['revision'], 0)
        receipt = json.loads(self.store.read_blob(result['effect']['result_ref']))
        self.assertIsNotNone(receipt['parse_error'])
        self.assertTrue(result['proposal']['delivery_ref'])
        self.assertEqual(result['effect']['actual_costs']['tokens'], 25)
        self.assert_settled()
        self.output('lab', delta={'next_step': 'Recover from the invalid response'})
        resumed = self.lab('after-format-error')
        self.assertEqual(resumed['proposal']['status'], 'applied')
        self.assertEqual(self.session('lab')['provider_thread_id'], session['provider_thread_id'])

    def test_certified_failed_turn_closes_session_without_inventing_memory(self):
        self.outputs.append({'output': '', 'failure': True})
        result = self.lab('known-provider-failure')
        self.assertEqual(result['effect']['status'], 'failed_definite', result)
        self.assertIsNone(result['proposal'])
        session = self.session('lab')
        self.assertEqual(session['status'], 'idle')
        self.assertEqual(session['committed_turn'], 1)
        self.assertEqual(self.rt.agents.working_memory(session['id'])['revision'], 0)
        self.assert_settled()
        self.output('lab', delta={'next_step': 'Continue after a known failed turn'})
        resumed = self.lab('after-known-failure')
        self.assertEqual(resumed['effect']['status'], 'succeeded')
        self.assertEqual(self.session('lab')['provider_thread_id'], session['provider_thread_id'])
        self.assertEqual(self.session('lab')['committed_turn'], 2)

    def test_terminal_receipt_recovery_commits_once_without_resending(self):
        self.output('lab', delta={'next_step': 'Continue after durable terminal'})
        work = self.rt.enqueue_lab('crash-before-apply')
        prepared = self.rt.prepare_meta(work['id'], self.reservations)
        completed = self.c.dispatch_effect('run', prepared['id'], self.executor)
        self.assertEqual(completed['status'], 'succeeded')
        self.assertEqual(self.session('lab')['committed_turn'], 0)
        self.assertEqual(self.store.list('run', 'registration', kind='journal_cursor'), [])
        before_count = len(self.calls)
        # Recreate the orchestration object as a restarted host would.
        restarted = Runtime(self.store, 'run')
        with patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('Recovery must never resend')):
            first = restarted.recover()
            second = restarted.recover()
        self.assertTrue(first['proposals'])
        self.assertTrue(second['proposals'])
        self.assertEqual(len(self.calls), before_count)
        self.assertEqual(self.session('lab')['committed_turn'], 1)
        memory = restarted.agents.working_memory(self.session('lab')['id'])
        self.assertEqual(memory['revision'], 1)
        self.assertEqual(memory['content']['next_step'], 'Continue after durable terminal')
        cursors = self.store.list('run', 'registration', kind='journal_cursor')
        self.assertEqual(len(cursors), 1)
        journal = read_journal(cursors[0]['path'])
        self.assertEqual(cursors[0]['applied_seq'], journal['durable_seq'])
        self.assertEqual(cursors[0]['head_hash'], journal['head_hash'])
        self.assert_settled()

    def test_failed_terminal_receipt_recovery_closes_turn_without_resending(self):
        self.outputs.append({'output': '', 'failure': True})
        work = self.rt.enqueue_lab('failed-crash-before-commit')
        prepared = self.rt.prepare_meta(work['id'], self.reservations)
        completed = self.c.dispatch_effect('run', prepared['id'], self.executor)
        self.assertEqual(completed['status'], 'failed_definite')
        self.assertEqual(self.session('lab')['committed_turn'], 0)
        with patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('No failure retry')):
            first = Runtime(self.store, 'run').recover()
            second = Runtime(self.store, 'run').recover()
        self.assertEqual(first['agent_recovery'], [])
        self.assertEqual(second['agent_recovery'], [])
        session = self.session('lab')
        self.assertEqual(session['status'], 'idle')
        self.assertEqual(session['committed_turn'], 1)
        self.assertIsNone(session['active_turn_id'])
        self.assertEqual(self.rt.agents.working_memory(session['id'])['revision'], 0)
        self.assertEqual(len(self.calls), 1)
        self.assert_settled()

    def test_rejected_turn_does_not_block_recovery_after_explicit_rebuild(self):
        self.output('lab', delta={'next_step': 'Preserve admitted memory'})
        self.lab('before-quarantine')
        self.output('lab', delta={'next_step': 'Never admit this memory'},
                    thread_override='40fe0ebf-361b-471d-b010-b7df90145e48')
        with self.assertRaisesRegex(ValueError, '[Pp]rotocol'):
            self.lab('quarantine-wrong-thread')
        old = self.session('lab')
        turn_id = old['active_turn_id']
        effect_id = self.store.get('run', 'agent_turn', turn_id)['effect_ids'][-1]
        self.rt.agents.discard_completed_turn(old['id'], 'Quarantine synthetic wrong thread', 'discard-test')
        new = self.rt.agents.rebuild_session(old['id'], 'Explicit owned-session reconstruction', 'rebuild-test')
        with patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('No quarantined replay')):
            first = Runtime(self.store, 'run').recover()
            second = Runtime(self.store, 'run').recover()
        self.assertEqual(first['agent_recovery'], [])
        self.assertEqual(second['agent_recovery'], [])
        rejected = self.store.list('run', 'proposal', effect_id=effect_id)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['status'], 'rejected')
        self.assertEqual(self.store.get('run', 'agent_turn', turn_id)['status'], 'rejected')
        self.assertEqual(self.store.get('run', 'agent_session', old['id'])['status'], 'superseded')
        self.assertEqual(self.rt.agents.working_memory(new['id'])['content']['next_step'], 'Preserve admitted memory')
        self.assertEqual(len(self.calls), 2)
        self.assert_settled()

    def test_recover_transport_result_before_controller_receipt_without_resending(self):
        self.output('lab', delta={'next_step': 'Resume after outer receipt crash'})
        work = self.rt.enqueue_lab('crash-before-controller-receipt')
        real_replace = os.replace
        class CrashBeforeReceipt(BaseException):
            pass
        def crash_receipt(source, destination, *args, **kwargs):
            destination = Path(destination)
            if destination.parent == self.store.root and destination.name.startswith('receipt_'):
                raise CrashBeforeReceipt('Synthetic host crash before outer receipt')
            return real_replace(source, destination, *args, **kwargs)
        with patch.object(os, 'replace', crash_receipt):
            with self.assertRaises(CrashBeforeReceipt):
                self.rt.run_lab(work['id'], self.executor, self.reservations)
        effect = self.store.list('run', 'effect')[0]
        self.assertEqual(effect['status'], 'dispatching')
        call_dir = self.store.root / 'calls' / effect['id']
        self.assertTrue((call_dir / 'transport_result.json').is_file())
        self.assertFalse((self.store.root / ('receipt_' + effect['id'] + '.json')).exists())
        self.assertEqual(self.session('lab')['committed_turn'], 0)
        with patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('Recovery cannot resend')):
            Runtime(self.store, 'run').recover()
            Runtime(self.store, 'run').recover()
        recovered = self.store.get('run', 'effect', effect['id'])
        self.assertEqual(recovered['status'], 'succeeded')
        self.assertFalse(recovered['accounting_unknown'])
        self.assertEqual(recovered['actual_costs']['tokens'], 25)
        self.assertEqual(self.session('lab')['committed_turn'], 1)
        self.assertEqual(self.rt.agents.working_memory(self.session('lab')['id'])['content']['next_step'],
                         'Resume after outer receipt crash')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.store.list('run', 'registration', kind='journal_cursor')), 1)
        self.assert_settled()

    def test_terminal_only_crash_retains_session_owner_and_unknown_wall_reservation(self):
        self.output('lab', delta={'next_step': 'Uncommitted until native writer is fenced'})
        spec = self.outputs.pop()
        work = self.rt.enqueue_lab('crash-at-terminal-only')
        # Kill a separate host process at the exact point before its durable
        # attempt_finished record. This avoids fabricating or editing a journal
        # after the fact and leaves the real SQLite dispatch transaction intact.
        driver = r'''
import json,os,subprocess,sys
from pathlib import Path
from unittest.mock import patch
from rsi_lab import transport
from rsi_lab.journal import JournalWriter
from rsi_lab.runtime import Runtime
from rsi_lab.roles import CodexExecutor
from rsi_lab.store import Store
from tests.v2.test_persistent_runtime import PROVIDER
root=Path(sys.argv[1]);work_id=sys.argv[2];spec=json.loads(sys.argv[3])
real_popen=subprocess.Popen
original_append=JournalWriter.append
def spawn(command,**kwargs):
 spec['resume_thread']=command[-2] if 'resume' in command else None
 with (root/'synthetic-provider-count.txt').open('a') as f:
  f.write('call\n');f.flush();os.fsync(f.fileno())
 return real_popen([sys.executable,'-u','-c',PROVIDER,json.dumps(spec)],**kwargs)
def abrupt_exit(writer,stream,raw,**kwargs):
 if kwargs.get('kind')=='attempt_finished':
  os._exit(82)
 return original_append(writer,stream,raw,**kwargs)
store=Store(root)
with patch.object(transport.subprocess,'Popen',side_effect=spawn),patch.object(JournalWriter,'append',abrupt_exit):
 Runtime(store,'run').run_lab(work_id,CodexExecutor(root/'calls'),{'model_calls':1,'tokens':1000,'wall_seconds':10})
raise AssertionError('Crash fixture did not reach its exit boundary')
'''
        process = self.real_popen([sys.executable, '-u', '-c', driver, str(self.store.root), work['id'], json.dumps(spec)],
                                  cwd=str(Path(__file__).resolve().parents[2]), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 82, stderr.decode())
        effect = self.store.list('run', 'effect')[0]
        call_dir = self.store.root / 'calls' / effect['id']
        self.assertFalse((call_dir / 'transport_result.json').exists())
        capture = read_journal(call_dir / 'journal.jsonl')
        self.assertNotIn('attempt_finished', [row['kind'] for row in capture['records']])
        self.assertFalse(capture['gaps'])
        with patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('No repeat invocation')):
            Runtime(self.store, 'run').recover()
            Runtime(self.store, 'run').recover()
        current = self.store.get('run', 'effect', effect['id'])
        self.assertTrue(current['accounting_unknown'])
        self.assertEqual(current['actual_costs']['tokens'], 25)
        self.assertNotIn('wall_seconds', current['actual_costs'])
        self.assertEqual(self.store.get('run', 'budget', 'wall_seconds')['reserved'], 10)
        session = self.session('lab')
        self.assertEqual(session['committed_turn'], 0)
        self.assertIsNotNone(session['active_turn_id'])
        self.assertEqual(session['status'], 'recovery_required')
        self.assertEqual(self.rt.agents.working_memory(session['id'])['revision'], 0)
        self.assertEqual((self.store.root / 'synthetic-provider-count.txt').read_text(), 'call\n')
        self.store.verify_history('run')

    def test_changed_native_history_blocks_next_request_before_dispatch(self):
        self.output('lab', delta={'next_step': 'Check integrity before resume'})
        self.lab('before-tamper')
        session = self.session('lab')
        native = Path(session['context_manifest']['native_session_ref'])
        native.write_bytes(native.read_bytes().replace(b'synthetic-local-provider', b'modified--local-provider', 1))
        work = self.rt.enqueue_lab('after-tamper')
        before = len(self.calls)
        result = self.rt.run_until_wait(self.executor, self.reservations, max_steps=1)
        self.assertEqual(len(self.calls), before)
        self.assertIn('session_prefix_mismatch', result.get('reason') or '')
        self.assertEqual(self.store.get('run', 'work', work['id'])['status'], 'blocked')
        self.assertEqual(self.session('lab')['status'], 'recovery_required')
        self.assertEqual(len(self.store.list('run', 'effect')), 1)
        self.assert_settled()

    def test_native_change_after_admission_is_known_zero_cost_without_dispatch(self):
        self.output('lab', delta={'next_step': 'Verify admitted native prefix'})
        self.lab('before-preflight-change')
        session = self.session('lab')
        work = self.rt.enqueue_lab('preflight-change')
        prepared = self.rt.prepare_meta(work['id'], self.reservations)
        native = Path(session['context_manifest']['native_session_ref'])
        native.write_bytes(native.read_bytes().replace(b'synthetic-local-provider', b'modified--local-provider', 1))
        with patch.object(transport.subprocess, 'Popen', side_effect=AssertionError('Preflight cannot dispatch')):
            result = self.c.dispatch_effect('run', prepared['id'], self.executor)
            Runtime(self.store, 'run').recover()
        self.assertEqual(result['status'], 'failed_definite')
        self.assertFalse(result['accounting_unknown'])
        self.assertEqual(result['actual_costs'], {name: 0 for name in self.reservations})
        receipt = json.loads(self.store.read_blob(result['result_ref']))
        self.assertTrue(receipt['never_dispatched'])
        self.assertFalse(receipt['protocol_valid'])
        self.assertIn('session_prefix_mismatch', receipt['error'])
        current = self.session('lab')
        self.assertEqual(current['status'], 'recovery_required')
        self.assertEqual(current['committed_turn'], 1)
        self.assertEqual(self.rt.agents.working_memory(current['id'])['revision'], 1)
        self.assertFalse((self.store.root / 'calls' / result['id']).exists())
        self.assertEqual(len(self.calls), 1)
        self.assert_settled()

    def test_first_turn_compaction_cannot_commit_private_memory(self):
        self.output('lab', delta={'next_step': 'Never trust compacted first-turn history'}, compact=True)
        with self.assertRaisesRegex(ValueError, '[Pp]rotocol|context'):
            self.lab('first-turn-compacted')
        session = self.session('lab')
        self.assertEqual(session['status'], 'recovery_required')
        self.assertEqual(session['committed_turn'], 0)
        self.assertEqual(self.rt.agents.working_memory(session['id'])['revision'], 0)
        effect = self.store.list('run', 'effect')[0]
        self.assertEqual(effect['status'], 'succeeded', 'Known execution and protocol validity are separate')
        self.assertEqual(effect['actual_costs']['tokens'], 25)
        receipt = json.loads(self.store.read_blob(effect['result_ref']))
        self.assertFalse(receipt['protocol_valid'])
        self.assertTrue(receipt['context_manifest']['native_session_error'])
        self.assertEqual(len(self.calls), 1)
        self.assert_settled()

    def test_resume_cannot_rewrite_previously_committed_native_prefix(self):
        self.output('lab', delta={'next_step': 'Original durable plan'})
        self.lab('before-runtime-tamper')
        first = self.session('lab')
        self.output('lab', delta={'next_step': 'Must not replace original plan'}, rewrite_prefix=True)
        with self.assertRaisesRegex(ValueError, 'protocol|prefix|context'):
            self.lab('runtime-tamper')
        current = self.session('lab')
        self.assertEqual(current['committed_turn'], first['committed_turn'])
        self.assertEqual(self.rt.agents.working_memory(current['id'])['content']['next_step'],
                         'Original durable plan')
        self.assertEqual(len(self.calls), 2, 'A known attempted request must not be invisibly retried')
        self.assert_settled()

    def test_resume_compaction_rejected_before_rollout_path_cannot_commit_memory(self):
        self.output('lab', delta={'next_step': 'Preserve the original plan'})
        self.lab('before-compaction')
        first = self.session('lab')
        self.output('lab', delta={'next_step': 'Must not trust opaque compacted history'}, compact=True)
        with self.assertRaisesRegex(ValueError, '[Pp]rotocol|context'):
            self.lab('opaque-compaction')
        current = self.session('lab')
        self.assertEqual(current['committed_turn'], first['committed_turn'])
        self.assertEqual(self.rt.agents.working_memory(current['id'])['content']['next_step'],
                         'Preserve the original plan')
        self.assertEqual(len(self.calls), 2)
        self.assert_settled()

    def test_wrong_provider_thread_cannot_commit_private_memory(self):
        self.output('lab')
        self.lab('before-wrong-thread')
        self.output('lab', delta={'next_step': 'Must not commit'},
                    thread_override='40fe0ebf-361b-471d-b010-b7df90145e48')
        with self.assertRaisesRegex(ValueError, '[Pp]rotocol'):
            self.lab('wrong-thread')
        # Execution may be known, but a protocol mismatch cannot certify the
        # expected session's turn or admit the model's proposed memory update.
        self.assertNotEqual(self.session('lab')['committed_turn'], 2)
        self.assertNotIn('Must not commit', json.dumps(self.rt.agents.working_memory(self.session('lab')['id'])))
        self.assertEqual(len(self.store.list('run', 'proposal')), 1)
        self.assert_settled()


if __name__ == '__main__':
    unittest.main()
