"""Prospective application contract tests with labeled, non-Vina fixtures."""
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest, encode
from rsi_domains.docking import live_task_contract as contract
from rsi_domains.docking import live_vina

CALLS = []


def prepare(inputs):
    return {**inputs, 'protocol_sha256': digest(Path(inputs['protocol_path']).read_bytes())}


def reservations(packet):
    return {'tool_calls': 2 * len(packet['selected_ids'])}


def execute(effect, raw):
    packet = json.loads(raw); CALLS.append(effect['id'])
    Path(packet['output_dir']).mkdir(parents=True)
    ids = packet['selected_ids']
    status = 'unknown' if 'UNKNOWN' in ids else 'failed_definite' if 'FAIL' in ids else 'succeeded'
    jobs = [] if status != 'succeeded' else [{'candidate_id': cid, 'seed': seed, 'status': 'completed',
                                            'pose_valid': cid != 'B'} for cid in ids for seed in (17, 43)]
    return {'status': status, 'actual_costs': {'tool_calls': len(ids) * 2},
            'result': {'jobs': jobs, 'candidate_ids': ids, 'error': 'fixture failure' if status == 'failed_definite' else None}}


def publication(effect, receipt):
    return {'facts': [{'id': 'fixture-' + effect['id'], 'kind': 'raw_fact', 'content': receipt['result'],
                       'provenance': {'fixture': True, 'effect_id': effect['id']}}],
            'conclusions': [], 'data': receipt['result']}


def adapter():
    return Adapter(adapter_id=live_vina.ADAPTER_ID, version=live_vina.VERSION, effect_kind='docking_vina_batch',
                   manifest={'scope': 'Synthetic contract fixture; never a real Vina result'},
                   prepare=prepare, reservations=reservations, execute=execute, publication=publication)


class LiveTaskContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'data'; self.data.mkdir()
        candidates = [{'candidate_id': cid, 'canonical_smiles': 'identity:' + cid} for cid in ('A', 'B', 'FAIL', 'UNKNOWN')]
        raw = encode({'candidates': candidates}); (self.data / 'candidates.json').write_bytes(raw)
        self.protocol_path = self.root / 'protocol.json'
        self.protocol_path.write_bytes(encode({'protocol_id': 'fixture-v1', 'seeds': [17, 43],
                                               'input_sha256': {'candidates.json': digest(raw)}}))
        self.store = Store(self.root / 'ledger'); self.addCleanup(self.store.close)
        self.case = {'data_dir': str(self.data), 'protocol_path': str(self.protocol_path),
            'goal': 'Exercise coverage and retain failures; synthetic fixture',
            'planned_ids': [r['candidate_id'] for r in candidates], 'planned_seed_jobs': 8,
            'sources': [{'source_id': 'protocol.json', 'sha256': digest(self.protocol_path.read_bytes())}],
            'budgets': {'tool_calls': {'limit': 100, 'hard': True}}}
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets=self.case['budgets'], config={'case_ref': self.store.put_blob(self.case)})
        self.runtime = Runtime(self.store, 'r', adapters=[adapter()])
        CALLS.clear()

    def register(self):
        return contract.register_contract(self.runtime, self.case)

    def work(self, ids, name, **overrides):
        inputs = {'data_dir': str(self.data), 'protocol_path': str(self.protocol_path), 'selected_ids': ids,
                  'output_dir': str(self.store.root / 'screen' / name), **overrides}
        return self.controller.enqueue('r', 'diagnostic', name, 'work:' + name,
            inputs={'capability_id': live_vina.ADAPTER_ID, 'capability_version': live_vina.VERSION,
                    'inputs_json': json.dumps(inputs)})

    def test_register_preserves_original_config_and_complete_arm_before_outputs(self):
        before = self.store.run('r')['config']
        row = self.register()
        plan = contract.plans.load_task_plan(self.store, 'r', contract.TASK_ID)
        self.assertEqual([r['item_id'] for r in plan['items']], self.case['planned_ids'])
        self.assertEqual(plan['success']['min_qualified_items'], 4)
        self.assertEqual(plan['missing_rule'], contract.plans.MISSING_RULE)
        self.assertEqual(self.store.run('r')['config'], before)
        self.assertEqual(row, self.register())
        self.assertEqual(contract.coverage_report(self.runtime)['coverage']['status'], 'incomplete')
        self.assertEqual(CALLS, [])

    def test_registration_refuses_preexisting_output_and_foreign_execution(self):
        (self.store.root / 'screen').mkdir()
        with self.assertRaisesRegex(ValueError, 'precede'):
            self.register()
        (self.store.root / 'screen').rmdir()
        self.store.mutate('r', 'foreign', 'fixture', lambda tx: tx.insert('effect', 'foreign',
            {'kind': 'docking_vina_batch', 'metadata': {}}))
        with self.assertRaisesRegex(ValueError, 'precede every Vina'):
            self.register()

    def test_disjoint_dynamic_batches_bind_plan_and_do_not_reexecute_after_publication(self):
        self.register()
        first = self.work(['A', 'B'], 'first')
        result = contract.checked_execute(self.runtime, first)
        self.assertEqual(result['effect']['metadata']['task_plan_id'], contract.TASK_ID)
        self.assertEqual(result['effect']['metadata']['task_plan_sha256'],
                         digest(contract.plans.load_task_plan(self.store, 'r', contract.TASK_ID)))
        again = contract.checked_execute(self.runtime, first)
        self.assertEqual(again['effect']['id'], result['effect']['id'])
        self.assertEqual(len(CALLS), 1)
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            contract.checked_execute(self.runtime, self.work(['B'], 'repeat'))
        report = contract.coverage_report(self.runtime)
        self.assertEqual(report['terminal_items'], 2)
        self.assertEqual(report['scientifically_completed_items'], 2)
        self.assertEqual(report['two_valid_pose_items'], 1)
        self.assertEqual(len(report['items']), 4)
        self.assertEqual(report['coverage']['status'], 'incomplete')

    def test_wrong_identity_protocol_input_and_output_scope_fail_before_tool(self):
        self.register()
        wrong_protocol = self.root / 'other.json'; wrong_protocol.write_bytes(self.protocol_path.read_bytes())
        options = [(['FOREIGN'], {}), (['A'], {'protocol_path': str(wrong_protocol)}),
                   (['A'], {'output_dir': str(self.root / 'elsewhere')}),
                   (['A'], {'output_dir': str(self.store.root / 'screen' / 'nested' / 'batch')})]
        for index, (ids, changes) in enumerate(options):
            with self.subTest(index=index), self.assertRaises(ValueError):
                contract.checked_execute(self.runtime, self.work(ids, str(index), **changes))
        (self.data / 'candidates.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'input bytes changed'):
            contract.checked_execute(self.runtime, self.work(['A'], 'changed'))
        self.assertEqual(CALLS, [])

    def test_definite_failure_is_retained_and_unknown_does_not_count_as_terminal(self):
        self.register()
        contract.checked_execute(self.runtime, self.work(['A', 'B'], 'normal'))
        contract.checked_execute(self.runtime, self.work(['FAIL'], 'failed'))
        contract.checked_execute(self.runtime, self.work(['UNKNOWN'], 'unknown'))
        report = contract.coverage_report(self.runtime)
        self.assertEqual(report['terminal_items'], 3)
        self.assertEqual(report['scientifically_completed_items'], 2)
        self.assertEqual(report['coverage']['status'], 'incomplete')
        failed = next(r for r in report['items'] if r['item_id'] == 'FAIL')
        self.assertEqual(failed['effect_status'], 'failed_definite')
        self.assertFalse(failed['scientific_jobs_completed'])
        self.assertEqual(failed['batch_failure'], 'fixture failure')
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            contract.checked_execute(self.runtime, self.work(['FAIL'], 'failure-retry'))
        self.assertFalse(report['formal_qualification'])
        unknown = next(e for e in self.store.list('r', 'effect') if e['status'] == 'unknown')
        self.controller.reconcile('r', unknown['id'],
            {'status': 'failed_definite', 'actual_costs': {'tool_calls': 2},
             'result': {'jobs': [], 'error': 'Synthetic definite failure after reconciliation'},
             'reconciliation': {'effect_id': unknown['id'], 'provenance': 'Synthetic test receipt'}},
            'fixture-reconcile')
        completed = contract.coverage_report(self.runtime)
        self.assertEqual(completed['coverage']['status'], 'accepted')
        self.assertEqual(completed['terminal_items'], 4)
        self.assertEqual(completed['scientifically_completed_items'], 2)
        self.assertEqual(completed['two_valid_pose_items'], 1)
        self.assertFalse(completed['formal_qualification'])

    def test_unbound_effect_is_detected_even_with_a_valid_task_plan(self):
        self.register()
        self.runtime.execute_adapter(live_vina.ADAPTER_ID,
            {'data_dir': str(self.data), 'protocol_path': str(self.protocol_path), 'selected_ids': ['A'],
             'output_dir': str(self.store.root / 'screen' / 'bypass')}, trigger_id='bypass')
        with self.assertRaisesRegex(ValueError, 'outside the frozen host task contract'):
            contract.coverage_report(self.runtime)
        with self.assertRaisesRegex(ValueError, 'outside the frozen host task contract'):
            contract.checked_execute(self.runtime, self.work(['B'], 'blocked-after-bypass'))
