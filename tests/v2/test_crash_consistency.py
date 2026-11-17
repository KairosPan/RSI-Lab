"""Crash consistency of blob, receipt-envelope and run-initialization writes."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.__main__ import main
from rsi_lab.controller import Controller
from rsi_lab.store import Store, Transaction, digest, encode

B = {'tool_calls': {'limit': 5, 'hard': True}}


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.c = Controller(self.store)


class BlobTests(Base):
    def test_torn_remnant_at_the_final_path_is_replaced(self):
        raw = encode({'k': 'v' * 200}); key = digest(raw)
        (self.store.blobs / key).write_bytes(raw[:17])
        self.assertEqual(self.store.put_blob(raw), key)
        self.assertEqual(self.store.read_blob(key), raw)

    def test_interrupted_write_leaves_nothing_behind(self):
        raw = b'x' * 4096; key = digest(raw)
        with patch.object(os, 'replace', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.store.put_blob(raw)
        self.assertEqual(list(self.store.blobs.iterdir()), [])
        self.assertEqual(self.store.put_blob(raw), key)
        self.assertEqual(self.store.read_blob(key), raw)

    def test_torn_blob_referenced_by_history_is_repaired(self):
        self.c.initialize('r', budgets=B)
        content = {'dataset': 'd', 'rows': 'x' * 300}
        provenance = {'source': 'test'}
        self.c.add_fact('r', 'f1', 'raw_fact', content, provenance, 'fact')
        # verify_history reads the event payload blob, which carries the fact content.
        C = encode({'fact_id': 'f1', 'kind': 'raw_fact', 'content': content, 'provenance': provenance})
        payload_ref = self.store.db.execute(
            "SELECT payload_ref FROM events WHERE run_id='r' AND operation_key='fact'").fetchone()[0]
        self.assertEqual(payload_ref, digest(C))
        path = self.store.blobs / payload_ref
        path.chmod(0o644)
        path.write_bytes(C[:23])
        with self.assertRaises(ValueError):
            self.store.verify_history('r')
        self.store.put_blob(C)
        self.store.verify_history('r')


class ReceiptTests(Base):
    def setUp(self):
        super().setUp()
        self.c.initialize('r', budgets=B)

    def left_dispatching(self):
        work = self.c.enqueue('r', 'diagnostic', 't', 'w')
        effect = self.c.prepare_effect('r', work['id'], 'tool', {'x': 1}, {'tool_calls': 1}, 'p')
        def dies(effect, packet): raise KeyboardInterrupt('killed while the tool ran')
        with self.assertRaises(KeyboardInterrupt):
            self.c.dispatch_effect('r', effect['id'], dies)
        return effect

    def test_empty_envelope_is_treated_as_no_receipt(self):
        effect = self.left_dispatching()
        (self.root / ('receipt_' + effect['id'] + '.json')).write_bytes(b'')
        self.c.recover('r'); self.c.recover('r')
        self.assertEqual(self.store.get('r', 'effect', effect['id'])['status'], 'unknown')
        self.store.verify_history('r')

    def test_envelope_missing_fields_is_treated_as_no_receipt(self):
        effect = self.left_dispatching()
        (self.root / ('receipt_' + effect['id'] + '.json')).write_bytes(b'{"effect_id": "x"}')
        self.c.recover('r'); self.c.recover('r')
        self.assertEqual(self.store.get('r', 'effect', effect['id'])['status'], 'unknown')
        self.store.verify_history('r')

    def test_receipt_is_not_visible_until_complete(self):
        work = self.c.enqueue('r', 'diagnostic', 't', 'w')
        effect = self.c.prepare_effect('r', work['id'], 'tool', {'x': 1}, {'tool_calls': 1}, 'p')
        real_replace = os.replace
        def crash_receipt(source, destination, *args, **kwargs):
            if Path(destination).name.startswith('receipt_'):
                raise KeyboardInterrupt('killed while publishing the receipt')
            return real_replace(source, destination, *args, **kwargs)
        def succeeds(effect, packet):
            return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1}}
        with patch.object(os, 'replace', crash_receipt), self.assertRaises(KeyboardInterrupt):
            self.c.dispatch_effect('r', effect['id'], succeeds)
        self.assertFalse((self.root / ('receipt_' + effect['id'] + '.json')).exists())
        self.c.recover('r')
        self.assertEqual(self.store.get('r', 'effect', effect['id'])['status'], 'unknown')
        self.store.verify_history('r')


class InitializeTests(Base):
    def test_run_and_budgets_commit_together(self):
        with patch.object(Transaction, 'insert', side_effect=RuntimeError('crash')), self.assertRaises(RuntimeError):
            self.c.initialize('r', budgets=B)
        with self.assertRaises(KeyError):
            self.store.run('r')
        self.c.initialize('r', budgets=B)
        self.store.verify_history('r')

    def test_initialize_is_idempotent_for_identical_arguments(self):
        first = self.c.initialize('r', budgets=B, max_external=1, config={'goal': 'g'})
        self.assertEqual(self.c.initialize('r', budgets=B, max_external=1, config={'goal': 'g'}), first)
        self.assertEqual(self.store.verify_history('r')['events'], 1)

    def test_initialize_refuses_a_different_configuration(self):
        self.c.initialize('r', budgets=B)
        with self.assertRaises(ValueError):
            self.c.initialize('r', budgets={'tool_calls': {'limit': 6, 'hard': True}})

    def test_run_left_without_budgets_is_completed(self):
        self.store.create_run('r', {'schema_version': 'rsi_control_v2', 'budgets': B, 'max_external': 1})
        self.c.initialize('r', budgets=B, max_external=1)
        self.assertEqual(self.store.get('r', 'budget', 'tool_calls')['limit'], 5)
        self.store.verify_history('r')

    def test_cli_init_twice_prints_the_same_result(self):
        cli_root = self.root / 'cli'
        path = self.root / 'config.json'
        path.write_text(json.dumps({'budgets': B}))
        outputs = []
        for _ in range(2):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(['--root', str(cli_root), '--run', 'r', 'init', '--config', str(path)])
            self.assertEqual(code, 0)
            outputs.append(stdout.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(json.loads(outputs[0]))


if __name__ == '__main__':
    unittest.main()
