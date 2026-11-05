"""Supervisor transitions without model calls or scientific execution."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from lab_harness import autonomous as a
from lab_harness import controller as c


class AutonomousTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'campaign'
        self.run.mkdir()
        self.vina = self.root / 'vina'
        self.vina.write_text('test executable, never invoked')
        self.vina.chmod(0o700)
        c.dump(self.run / 'frozen/campaign.json', {'campaign_id': 'campaign'})
        self.state = {'execution_mode': 'live', 'status': 'ready', 'pending_request': None,
                      'locked_shortlist_sha256': None, 'llm_calls': [], 'usage_tokens': 0,
                      'requests': {}, 'source_data_path': str(self.root / 'data')}
        c.dump(self.run / 'state.json', self.state)
        self.freeze = patch.object(c, 'assert_frozen')
        self.freeze.start()
        self.addCleanup(self.freeze.stop)

    def call(self):
        return a.run_campaign(self.run, vina=self.vina, worker_python=sys.executable)

    def pending(self, phase='online'):
        request = {'request_id': 'request_1', 'phase': phase, 'candidate_ids': ['A']}
        self.state.update(status='awaiting_docking', pending_request='request_1')
        self.state['requests']['request_1'] = {'request': request, 'canonical_hash': c.canonical_hash(request)}
        c.dump(self.run / 'pending/request_1/request.json', request)
        c.dump(self.run / 'state.json', self.state)
        return self.root / 'campaign.execution'

    def test_real_decision_entrypoint_and_both_worker_phases_reach_finalization(self):
        actions = []
        def turn(args):
            self.assertIsNone(args.response)
            state = c.read(self.run / 'state.json')
            state['llm_calls'].append({'status': 'accepted', 'model_called': True})
            actions.append('model')
            if len(state['llm_calls']) == 1:
                state.update(status='awaiting_docking', pending_request='online')
            else:
                state.update(status='shortlist_locked', locked_shortlist_sha256='locked')
            c.dump(self.run / 'state.json', state)
        def dispatch(run, state, execution, vina, python):
            actions.append('worker_' + state['pending_request'])
            return execution / state['pending_request']
        def ingest(args):
            actions.append('ingest')
            state = c.read(self.run / 'state.json')
            state['last_ingested'] = state['pending_request']
            state.update(pending_request=None, status='ready')
            c.dump(self.run / 'state.json', state)
        def finalize(args):
            actions.append('finalize')
            state = c.read(self.run / 'state.json')
            if state.get('last_ingested') != 'terminal':
                state.update(status='awaiting_docking', pending_request='terminal')
            else:
                state['status'] = 'completed'
                c.dump(self.run / 'evaluation.json', {'status': 'completed'})
            c.dump(self.run / 'state.json', state)
        with patch.object(c, 'turn', side_effect=turn), patch.object(a, '_execute_pending', side_effect=dispatch), \
                patch.object(c, 'ingest', side_effect=ingest), patch.object(c, 'finalize', side_effect=finalize):
            result = self.call()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(actions, ['model', 'worker_online', 'ingest', 'model', 'finalize',
                                   'worker_terminal', 'ingest', 'finalize'])
        self.assertFalse(Path(result['execution_root']).is_relative_to(self.run))

    def test_completed_campaign_is_not_mutated_or_called_again(self):
        self.state['status'] = 'completed'
        c.dump(self.run / 'state.json', self.state)
        c.dump(self.run / 'evaluation.json', {'status': 'completed'})
        before = {str(p): p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        with patch.object(c, 'turn') as model, patch.object(c, 'finalize') as finalize, \
                patch.object(a, '_execute_pending') as worker:
            self.assertEqual(self.call()['status'], 'completed')
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.run.rglob('*') if p.is_file()})
        model.assert_not_called()
        finalize.assert_not_called()
        worker.assert_not_called()

    def test_replay_rejected(self):
        self.state['execution_mode'] = 'replay'
        c.dump(self.run / 'state.json', self.state)
        with patch.object(c, 'turn') as model, self.assertRaisesRegex(ValueError, 'live campaign'):
            self.call()
        model.assert_not_called()

    def test_unresolved_model_call_stops_before_any_new_action(self):
        self.state['llm_calls'] = [{'status': 'running'}]
        c.dump(self.run / 'state.json', self.state)
        with patch.object(c, 'turn') as model, patch.object(a, '_execute_pending') as worker:
            result = self.call()
        self.assertEqual(result['status'], 'needs_audit')
        model.assert_not_called()
        worker.assert_not_called()

    def test_completed_raw_batch_is_imported_without_redispatch(self):
        execution = self.pending()
        batch = execution / 'worker/online/batches/request_1'
        c.dump(batch / 'artifact_hashes.json', {})
        with patch.object(a.subprocess, 'Popen') as spawn:
            found = a._execute_pending(self.run, self.state, execution, str(self.vina), sys.executable)
        self.assertEqual(found, batch)
        spawn.assert_not_called()

    def test_unknown_dispatch_is_not_resent(self):
        execution = self.pending()
        c.dump(execution / 'dispatches/request_1/receipt.json', {'status': 'dispatched'})
        with patch.object(a.subprocess, 'Popen') as spawn, self.assertRaisesRegex(a.AuditRequired, 'no implicit retry'):
            a._execute_pending(self.run, self.state, execution, str(self.vina), sys.executable)
        spawn.assert_not_called()

    def test_terminal_dispatch_uses_frozen_worker_and_locked_shortlist(self):
        execution = self.pending('terminal')
        batch = execution / 'worker/terminal/batches/request_1'
        class Process:
            pid = 12345
            def wait(self):
                c.dump(batch / 'artifact_hashes.json', {})
                return 0
        with patch.object(a.subprocess, 'Popen', return_value=Process()) as spawn:
            found = a._execute_pending(self.run, self.state, execution, str(self.vina), sys.executable)
        command = spawn.call_args.args[0]
        self.assertEqual(command[1], str(self.run / 'frozen/run_vina_batch.py'))
        self.assertEqual(command[-2:], ['--locked-shortlist', str(self.run / 'locked_shortlist.json')])
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(found, batch)

    def test_no_progress_stops_instead_of_spinning(self):
        with patch.object(c, 'turn') as model:
            result = self.call()
        self.assertEqual(result['status'], 'needs_audit')
        self.assertIn('no progress', result['error'])
        self.assertEqual(model.call_count, 1)


if __name__ == '__main__':
    unittest.main()
