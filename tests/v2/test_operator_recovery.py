"""Operator reconciliation of unknown model calls, the model timeout setting and CLI error surface."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.__main__ import main as cli_main
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from tests.v2.liveness_support import BUDGETS, Scripted, none, open_run

REPO = Path(__file__).resolve().parents[2]
LIVE = {'execution_mode': 'live', 'model': 'fixture-model', 'effort': 'medium'}
LIVE_RES = {'model_calls': 1, 'tokens': 1000, 'wall_seconds': 10}


def unknown(effect, packet):
    return {'status': 'unknown', 'actual_costs': {'model_calls': 1}, 'reason': 'killed at the executor timeout'}


def operator_receipt(effect_id, **extra):
    return {'status': 'failed_definite', 'actual_costs': {'tokens': 1000, 'wall_seconds': 10},
            'native_writer_quiesced': True, 'error': 'killed at the executor timeout',
            'reconciliation': {'effect_id': effect_id, 'provenance': 'operator: process group killed'}, **extra}


def live_receipt(receipt, call_index):
    receipt = {k: v for k, v in receipt.items() if k != 'synthetic'}
    receipt['terminal_received'] = True
    return receipt


def write_json(testcase, value):
    handle, path = tempfile.mkstemp(suffix='.json')
    os.close(handle)
    testcase.addCleanup(os.unlink, path)
    Path(path).write_text(json.dumps(value))
    return path


def main(argv):
    with contextlib.redirect_stdout(io.StringIO()):
        return cli_main(argv)


class ReconcileCommandTests(unittest.TestCase):
    def timed_out_lab_call(self):
        store, controller, rt = open_run(self, config=LIVE)
        rt.enqueue_lab('lab-1')
        self.assertEqual(rt.run_until_wait(unknown, LIVE_RES)['status'], 'blocked_external')
        effect = store.list('r', 'effect')[0]
        session = store.list('r', 'agent_session', role='lab')[0]
        self.assertEqual(effect['status'], 'unknown')
        return store, controller, rt, effect, session

    def reopen(self, root):
        store = Store(root)
        self.addCleanup(store.close)
        return store, Runtime(store, 'r')

    def test_timed_out_lab_call_can_be_reconciled_rebuilt_and_followed_by_new_work(self):
        store, controller, rt, effect, session = self.timed_out_lab_call()
        root = str(store.root)
        path = write_json(self, operator_receipt(effect['id']))
        store.close()
        command = ['--root', root, '--run', 'r', 'reconcile', '--effect', effect['id'], '--receipt', path]
        self.assertEqual(main(command), 0)
        self.assertEqual(main(command), 0)
        self.assertEqual(main(['--root', root, '--run', 'r', 'agent-rebuild', '--session', session['id'],
                               '--reason', 'timeout']), 0)
        store, rt = self.reopen(root)
        self.assertEqual(store.get('r', 'effect', effect['id'])['status'], 'failed_definite')
        self.assertTrue(all(b['reserved'] == 0 for b in store.list('r', 'budget')))
        self.assertEqual(store.get('r', 'agent_session', session['id'])['status'], 'superseded')
        lab2 = rt.enqueue_lab('lab-2')
        live_scripted = Scripted({'lab': none}, receipt=live_receipt)
        rt.run_until_wait(live_scripted, LIVE_RES)
        self.assertEqual(store.get('r', 'work', lab2['id'])['status'], 'succeeded')
        self.assertEqual(live_scripted.calls, ['lab'])
        store.verify_history('r')

    def test_reconciliation_without_writer_exit_proof_still_refuses_the_discard(self):
        store, controller, rt, effect, session = self.timed_out_lab_call()
        receipt = operator_receipt(effect['id'])
        del receipt['native_writer_quiesced']
        controller.reconcile('r', effect['id'], receipt, 'operator-reconcile')
        rt.recover()
        self.assertEqual(store.get('r', 'effect', effect['id'])['status'], 'failed_definite')
        with self.assertRaises(ValueError):
            rt.agents.discard_completed_turn(session['id'], 'timeout', 'discard-1')
        store.verify_history('r')

    def test_unreconciled_unknown_call_still_refuses_the_discard(self):
        store, controller, rt, effect, session = self.timed_out_lab_call()
        with self.assertRaises(ValueError):
            rt.agents.discard_completed_turn(session['id'], 'timeout', 'discard-1')
        store.verify_history('r')

    def test_reconcile_refuses_an_unreserved_resource_without_a_traceback(self):
        store, controller, rt, effect, session = self.timed_out_lab_call()
        root = str(store.root)
        receipt = operator_receipt(effect['id'])
        receipt['actual_costs'] = {'gpu_hours': 1}
        path = write_json(self, receipt)
        store.close()
        done = subprocess.run([sys.executable, '-m', 'rsi_lab', '--root', root, '--run', 'r', 'reconcile',
                               '--effect', effect['id'], '--receipt', path],
                              capture_output=True, text=True, cwd=REPO)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(json.loads(done.stderr)['status'], 'blocked')
        self.assertNotIn('Traceback', done.stderr)
        store, rt = self.reopen(root)
        self.assertEqual(store.get('r', 'effect', effect['id'])['status'], 'unknown')
        store.verify_history('r')


class TimeoutConfigTests(unittest.TestCase):
    def test_model_timeout_defaults_to_300_and_reads_the_run_config(self):
        from rsi_lab.__main__ import model_timeout
        self.assertEqual(model_timeout({}), 300)
        self.assertEqual(model_timeout({'model_timeout_seconds': 42}), 42)

    def test_invalid_model_timeout_is_refused(self):
        from rsi_lab.__main__ import model_timeout
        for bad in (0, -1, 3601, 'fast', True, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                model_timeout({'model_timeout_seconds': bad})

    def test_cli_passes_the_configured_timeout_to_the_executor(self):
        root = tempfile.mkdtemp(prefix='rsi-timeout-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        config = {'goal': 'Timeout fixture', 'authorized_sources': ['src'], 'execution_mode': 'live',
                  'model': 'm', 'effort': 'medium', 'model_timeout_seconds': 42,
                  'model_reservation': LIVE_RES, 'budgets': BUDGETS}
        path = write_json(self, config)
        self.assertEqual(main(['--root', root, '--run', 'r', 'init', '--config', path]), 0)
        with patch('rsi_lab.__main__.CodexExecutor') as executor_class, \
             patch.object(Runtime, 'run_until_wait', return_value={'status': 'quiescent'}):
            self.assertEqual(main(['--root', root, '--run', 'r', 'run-until-wait']), 0)
        self.assertEqual(executor_class.call_args.kwargs['timeout_seconds'], 42)


class ErrorSurfaceTests(unittest.TestCase):
    def test_sqlite_errors_are_reported_as_blocked_json(self):
        root = tempfile.mkdtemp(prefix='rsi-sqlite-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        Path(root, 'ledger.sqlite3').write_bytes(b'not a database')
        done = subprocess.run([sys.executable, '-m', 'rsi_lab', '--root', root, 'status'],
                              capture_output=True, text=True, cwd=REPO)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(json.loads(done.stderr)['status'], 'blocked')
        self.assertNotIn('Traceback', done.stderr)


if __name__ == '__main__':
    unittest.main()
