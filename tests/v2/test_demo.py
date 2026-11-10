"""Integration assertions for the explicitly synthetic offline workflow."""
import json
from pathlib import Path
import tempfile
import unittest

from tests.v2.fixtures import run_demo
from rsi_lab.store import Store


class OfflineDemoTests(unittest.TestCase):
    def test_discovery_investigation_actual_acceptance_and_resumption(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_demo(directory)
            self.assertEqual(report['status'], 'completed')
            self.assertEqual(report['issue_status'], 'resolved')
            self.assertEqual(report['request_wait'], {'before_delivery': 'waiting', 'after_delivery': 'fired'})
            self.assertEqual(report['capability_wait'], 'fired')
            self.assertEqual([row['action'] for row in report['decisions']],
                             ['propose_issue', 'request_evidence', 'request_capability', 'close_issue'])
            self.assertTrue(all(row['synthetic'] for row in report['decisions']))
            self.assertTrue(report['actual_acceptance']['accepted'])
            self.assertEqual({row['kind'] for row in report['actual_acceptance']['cases']}, {'normal', 'failure'})
            self.assertTrue(all(all(row['checks'].values()) for row in report['actual_acceptance']['cases']))
            self.assertGreater(report['actual_acceptance']['costs']['wall_seconds'], 0)
            self.assertEqual(report['costs']['model_calls']['spent'], 0)
            self.assertEqual(report['costs']['tool_calls']['spent'], 3)
            self.assertTrue(all(row['reserved'] == 0 for row in report['costs'].values()))
            self.assertEqual(report['unreconciled_effect_count'], 0)
            self.assertFalse(report['live_model_executed'])
            marker = json.loads((Path(directory) / 'synthetic_resumed.json').read_bytes())
            self.assertTrue(marker['fixture_only'])
            store = Store(directory)
            try:
                before = len(store.list('offline-demo', 'effect'))
                self.assertEqual(before, 6)
                store.verify_history('offline-demo')
            finally:
                store.close()
            # Completing the script a second time never reruns the adapter.
            self.assertEqual(run_demo(directory), report)
            store = Store(directory)
            try:
                self.assertEqual(len(store.list('offline-demo', 'effect')), before)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
