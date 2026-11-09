"""Offline formal-contract fixtures. No fixture is a live docking result."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rsi_domains.docking import study
from docking_rsi.store import Store


def plan_fixture(root, count=105, arms=('policy_a',), shard_size=60):
    registry = [{'compound_id': f'C{i:04d}', 'parent_identity': f'parent_{i:04d}',
                 'prepared_smiles': 'C' * (i + 1), 'prepared_state_id': f'state_{i:04d}'} for i in range(count)]
    return {'schema_version': study.SCHEMA, 'study_id': 'study1', 'phase': 'formal',
            'compound_registry': registry,
            'identity_rule': {'name': 'registered_parent_identity', 'version': 'test_v1',
                              'registry_sha256': study.digest(registry)},
            'scientific_protocol': {'version': 'science1', 'profile_sha256': 'a' * 64,
                                    'preparation_sha256': 'b' * 64, 'evaluation_sha256': 'c' * 64},
            'tool_versions': {'vina': '1.2.7', 'preparation': 'fixture1'},
            'arms': [{'arm_id': arm, 'policy_version': arm + '_v1', 'policy_sha256': study.digest(arm),
                      'shards': [{'shard_id': f'shard{index // shard_size}',
                                  'compound_ids': [row['compound_id'] for row in registry[index:index+shard_size]],
                                  'archive_path': str(root / arm / f'shard{index // shard_size}'),
                                  'archive_adapter': 'native_vina_jobs_v2', 'protocol_sha256': 'd' * 64}
                                 for index in range(0, count, shard_size)]} for arm in arms],
            'threshold': -8.0, 'direction': 'lower_or_equal', 'min_compounds': 100,
            'score': {'name': 'qualified_median_vina_affinity', 'units': 'kcal/mol',
                      'seeds': [17, 43], 'pose_rank': 1, 'requires_both_valid': True},
            'budget': {'seed_jobs': count * len(arms) * 2, 'model_calls': 0},
            'stop_rule': 'all_planned_shards_terminal_or_explicit_stop',
            'missing_rule': 'retain_all_missing_no_imputation_no_replacement'}


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'ledger'); self.addCleanup(self.store.close)
        self.store.create_run('run1', {'test_fixture': True, 'execution_mode': 'live'})
        self.plan = plan_fixture(self.root)
        self.archives = {}
        self.reader = patch.object(study, '_read_archive', side_effect=self._fixture_reader)
        self.reader.start(); self.addCleanup(self.reader.stop)

    def _fixture_reader(self, root, plan, arm, shard, binding):
        fixture = self.archives[str(root)]
        if fixture['binding'] != binding:
            raise ValueError('Fixture raw Effect binding mismatch')
        if fixture['profile'] != plan['scientific_protocol'] or fixture['arm'] != arm['arm_id']:
            raise ValueError('Fixture raw scientific profile or arm mismatch')
        return deepcopy(fixture['rows']), deepcopy(fixture['archive'])

    def register(self):
        return study.register_study(self.store, 'run1', self.plan, key='register')

    def effect(self, arm, shard, *, status='prepared', metadata=None):
        eid = f'effect_{arm}_{shard}'
        spec = study.dispatch_spec(self.store, 'run1', 'study1', arm, shard)
        self.store.mutate('run1', eid, 'fixture.effect_prepared', lambda tx: tx.insert('effect', eid, {
            'status': status, 'kind': 'candidate_evaluate', 'packet_hash': study.digest(spec),
            'metadata': metadata if metadata is not None else {'study_binding': spec}, 'receipt_ref': None}),
            payload={'id': eid, 'spec': spec, 'status': status, 'metadata': metadata})
        return eid, spec

    def bind(self, arm, shard):
        eid, spec = self.effect(arm, shard)
        study.bind_shard(self.store, 'run1', 'study1', arm, shard, eid, key='bind_' + eid)
        return eid, spec

    def complete(self, arm, shard, *, fail=False, invalid=(), missing=(), score=-8.0, reconcile=True):
        eid, spec = self.bind(arm, shard)
        binding = study.execution_binding(self.store, 'run1', 'study1', arm, shard)
        _, planned = study._locate(self.plan, arm, shard)
        rows = []
        for cid, identity in study._identity_rows(self.plan, planned).items():
            if cid in missing:
                continue
            rows.append({**identity, 'seed_results': [{'seed': seed, 'status': 'completed',
                'pose_valid': cid not in invalid, 'identity_preserved': True, 'energy': score} for seed in study.SEEDS]})
        archive = {'archive_path': spec['archive_path'], 'archive_sha256': study.digest([arm, shard]),
                   'adapter': planned['archive_adapter'], 'observations_sha256': study.digest(rows)}
        self.archives[spec['archive_path']] = {'rows': rows, 'archive': archive, 'binding': binding,
                                            'profile': deepcopy(self.plan['scientific_protocol']), 'arm': arm}
        receipt = {'result': {'archive_path': spec['archive_path'], 'archive_sha256': archive['archive_sha256'],
                              'study_binding': spec}, 'test_fixture': True}
        self.store.mutate('run1', 'settle_' + eid, 'fixture.effect_settled', lambda tx: tx.update('effect', eid, {
            'status': 'failed_definite' if fail else 'succeeded', 'receipt_ref': tx.blob(receipt)}),
            payload={'effect_id': eid, 'receipt': receipt, 'fail': fail})
        if reconcile:
            return study.reconcile_shard(self.store, 'run1', 'study1', arm, shard, key='reconcile_' + eid)
        return eid

    def test_registration_is_immutable_complete_and_idempotent(self):
        first = self.register()
        self.assertEqual(first, self.register())
        self.assertEqual(len(self.store.list('run1', 'shard')), 2)
        changed = deepcopy(self.plan); changed['threshold'] = -7.0
        with self.assertRaises(ValueError):
            study.register_study(self.store, 'run1', changed, key='register')
        with self.assertRaises(ValueError):
            study.register_study(self.store, 'run1', changed, key='replacement')
        self.assertEqual(study.select_study(self.store, 'run1', 'study1', 'policy_a')['threshold'], -8.0)

    def test_retrospective_outputs_and_threshold_run_manifest_rejected(self):
        with self.assertRaises(ValueError):
            study.register_study(self.store, 'run1', {'threshold': -7, 'runs': ['/old']}, key='legacy')
        path = Path(self.plan['arms'][0]['shards'][0]['archive_path']); path.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'precede'):
            self.register()
        self.assertEqual(self.store.list('run1', 'study'), [])

    def test_all_qualifiers_not_top100_and_exact_threshold(self):
        self.register()
        for shard in self.plan['arms'][0]['shards']:
            self.complete('policy_a', shard['shard_id'])
        result = study.select_study(self.store, 'run1', 'study1', 'policy_a')
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['count'], 105)
        self.assertEqual(len(result['compounds']), 105)
        self.assertEqual({r['score'] for r in result['compounds']}, {-8.0})

    def test_other_task_can_require_three_instead_of_historical_hundred(self):
        from rsi_lab.plans import load_task_plan
        self.plan = plan_fixture(self.root, count=4, shard_size=4)
        self.plan['min_compounds'] = 3
        self.register(); self.complete('policy_a','shard0',invalid=('C0000',))
        result=study.select_study(self.store,'run1','study1','policy_a')
        self.assertEqual((result['status'],result['count'],result['min_compounds']),('accepted',3,3))
        common=load_task_plan(self.store,'run1','study1')
        self.assertEqual(common['success']['min_qualified_items'],3)
        self.assertEqual(common['metrics'][0]['replicates'],['17','43'])

    def test_higher_threshold_direction_is_an_explicit_task_choice(self):
        self.plan=plan_fixture(self.root,count=3,shard_size=3)
        self.plan.update(min_compounds=3,direction='higher_or_equal',threshold=-7.0)
        self.register();self.complete('policy_a','shard0',score=-6.5)
        result=study.select_study(self.store,'run1','study1','policy_a')
        self.assertEqual(result['status'],'accepted');self.assertEqual(result['count'],3)

    def test_legacy_reader_capability_limit_is_explicit_not_a_core_seed_rule(self):
        plan=deepcopy(self.plan);plan['score']['seeds']=[11,29]
        with self.assertRaisesRegex(ValueError,'archive adapter cannot verify'):
            study.validate_plan(plan)
        from rsi_lab import plans
        common=study.to_task_plan(self.plan)
        common['metrics'][0]['replicates']=['11','29']
        plans.validate_task_plan(common)

    def test_legacy_public_import_and_old_study_without_common_ref_remain_readable(self):
        from docking_rsi import study as legacy
        self.assertIs(legacy.register_study,study.register_study)
        # Reproduce an old registration shape without rewriting a historical archive.
        def old_registration(tx):
            row=tx.insert('study','study1',{'study_id':'study1','status':'registered','plan_ref':tx.blob(self.plan),
                'plan_hash':study.digest(self.plan),'phase':'formal','arm_ids':['policy_a']})
            for shard in self.plan['arms'][0]['shards']:
                tx.insert('shard',study._shard_key('study1','policy_a',shard['shard_id']),{'study_id':'study1','arm_id':'policy_a',
                    'shard_id':shard['shard_id'],'status':'planned','plan_hash':study.digest(self.plan),'effect_id':None,'archive_receipt':None})
            return row
        self.store.mutate('run1','legacy-registration','fixture.old_study',old_registration)
        result=legacy.select_study(self.store,'run1','study1','policy_a')
        self.assertEqual(result['status'],'incomplete');self.assertEqual(result['min_compounds'],100)
        self.assertEqual(self.store.list('run1','registration',kind='task_plan'),[])

    def test_effect_intent_before_registration_is_not_prospective(self):
        self.store.mutate('run1', 'early', 'fixture.early_effect', lambda tx: tx.insert('effect', 'early', {
            'status': 'prepared', 'metadata': {'study_binding': {'study_id': 'study1'}}}))
        with self.assertRaisesRegex(ValueError, 'cannot follow'):
            self.register()
        self.assertEqual(self.store.list('run1', 'study'), [])

    def test_replay_ledger_cannot_export_formal_results(self):
        self.store.create_run('replay1', {'execution_mode': 'replay'})
        study.register_study(self.store, 'replay1', self.plan, key='register')
        with self.assertRaisesRegex(ValueError, 'Replay'):
            study.select_study(self.store, 'replay1', 'study1', 'policy_a')

    def test_fewer_than100_is_honest_failure_not_threshold_change(self):
        self.register()
        self.complete('policy_a', 'shard0', invalid=('C0000', 'C0001', 'C0002', 'C0003', 'C0004', 'C0005'))
        self.complete('policy_a', 'shard1')
        result = study.select_study(self.store, 'run1', 'study1', 'policy_a')
        self.assertEqual((result['status'], result['count'], result['shortfall']), ('insufficient_compounds', 99, 1))
        self.assertEqual(len(result['all_observations']), 105)

    def test_100_hits_with_unstarted_shard_remain_incomplete(self):
        self.plan = plan_fixture(self.root, count=101, shard_size=100)
        self.register(); self.complete('policy_a', 'shard0')
        result = study.select_study(self.store, 'run1', 'study1', 'policy_a')
        self.assertEqual((result['status'], result['count']), ('incomplete', 100))
        self.assertFalse(result['formal_contract_satisfied'])
        self.assertEqual(result['shards'][1]['status'], 'planned')
        self.assertEqual(result['all_observations'][-1]['status'], 'unrun')

    def test_failed_shard_retained_and_never_imputed(self):
        self.register(); self.complete('policy_a', 'shard0'); self.complete('policy_a', 'shard1', fail=True)
        result = study.select_study(self.store, 'run1', 'study1', 'policy_a')
        self.assertEqual(result['status'], 'insufficient_compounds')
        self.assertEqual(result['count'], 60)
        self.assertEqual(result['missing_count'], 45)
        self.assertEqual(result['shards'][1]['status'], 'failed')

    def test_missing_raw_jobs_preserve_incomplete_status(self):
        self.register(); self.complete('policy_a', 'shard0', missing=('C0000',)); self.complete('policy_a', 'shard1')
        result = study.select_study(self.store, 'run1', 'study1', 'policy_a')
        self.assertEqual((result['status'], result['count']), ('incomplete', 104))

    def test_cross_arm_mix_cannot_reach100(self):
        self.plan = plan_fixture(self.root, count=120, arms=('a', 'b'))
        self.register(); self.complete('a', 'shard0'); self.complete('b', 'shard1')
        for arm in ('a', 'b'):
            result = study.select_study(self.store, 'run1', 'study1', arm)
            self.assertEqual((result['status'], result['count']), ('incomplete', 60))
        with self.assertRaises(ValueError):
            study.select_study(self.store, 'run1', 'study1', 'a+b')

    def test_effect_binding_must_precede_dispatch_and_match_every_axis(self):
        self.register()
        eid, _ = self.effect('policy_a', 'shard0', status='dispatching')
        with self.assertRaisesRegex(ValueError, 'prepared'):
            study.bind_shard(self.store, 'run1', 'study1', 'policy_a', 'shard0', eid, key='late')
        eid2, _ = self.effect('policy_a', 'shard1', metadata={'study_binding': {'arm_id': 'wrong'}})
        with self.assertRaises(ValueError):
            study.bind_shard(self.store, 'run1', 'study1', 'policy_a', 'shard1', eid2, key='wrong')

    def test_unknown_effect_remains_reserved_and_cannot_be_replaced(self):
        self.register(); eid, _ = self.bind('policy_a', 'shard0')
        self.store.mutate('run1', 'unknown', 'fixture.unknown', lambda tx: tx.update('effect', eid, {'status': 'unknown'}))
        result = study.reconcile_shard(self.store, 'run1', 'study1', 'policy_a', 'shard0', key='reconcile')
        self.assertEqual(result['status'], 'unresolved')
        with self.assertRaises(ValueError):
            study.bind_shard(self.store, 'run1', 'study1', 'policy_a', 'shard0', eid, key='retry')
        with self.assertRaises(ValueError):
            study.execution_binding(self.store, 'run1', 'study1', 'policy_a', 'shard0')

    def test_reconciliation_is_idempotent_but_not_replaced(self):
        self.register(); first = self.complete('policy_a', 'shard0')
        self.assertEqual(first, study.reconcile_shard(self.store, 'run1', 'study1', 'policy_a', 'shard0',
                         key='reconcile_effect_policy_a_shard0'))
        with self.assertRaises(ValueError):
            study.reconcile_shard(self.store, 'run1', 'study1', 'policy_a', 'shard0', key='second_reconcile')

    def test_accounting_receipt_cannot_replace_the_execution_archive_receipt(self):
        self.register()
        eid = self.complete('policy_a', 'shard0', reconcile=False)
        effect = self.store.get('run1', 'effect', eid)
        execution_ref = effect['receipt_ref']
        self.store.mutate('run1', 'cost-reconciled-0', 'fixture.accounting_receipt', lambda tx: tx.update('effect', eid, {
            'result_ref': execution_ref, 'receipt_ref': tx.blob({'status': 'succeeded', 'actual_costs': {'seed_jobs': 120}})}))
        state = study.reconcile_shard(self.store, 'run1', 'study1', 'policy_a', 'shard0', key='reconcile-first')
        self.assertEqual(state['effect_receipt_ref'], execution_ref)
        self.complete('policy_a', 'shard1')
        self.assertEqual(study.select_study(self.store, 'run1', 'study1', 'policy_a')['count'], 105)
        self.store.mutate('run1', 'cost-reconciled-1', 'fixture.final_accounting_receipt', lambda tx: tx.update('effect', eid, {
            'receipt_ref': tx.blob({'status': 'succeeded', 'actual_costs': {'seed_jobs': 120}, 'costs_final': True})}))
        self.assertEqual(study.select_study(self.store, 'run1', 'study1', 'policy_a')['status'], 'accepted')

    def test_observation_or_scientific_profile_tampering_rejected_at_export(self):
        self.register(); self.complete('policy_a', 'shard0')
        path = self.plan['arms'][0]['shards'][0]['archive_path']; fixture = self.archives[path]
        fixture['rows'][0]['seed_results'][0]['energy'] = -20
        with self.assertRaisesRegex(ValueError, 'changed'):
            study.select_study(self.store, 'run1', 'study1', 'policy_a')
        fixture['rows'][0]['seed_results'][0]['energy'] = -8
        fixture['profile']['version'] = 'posthoc'
        with self.assertRaisesRegex(ValueError, 'profile'):
            study.select_study(self.store, 'run1', 'study1', 'policy_a')

    def test_missing_shard_projection_detected_from_history(self):
        self.register()
        self.store.db.execute('DELETE FROM v2_shard WHERE id=?', ('study1/policy_a/shard1',))
        with self.assertRaisesRegex(ValueError, 'projections'):
            study.select_study(self.store, 'run1', 'study1', 'policy_a')

    def test_duplicate_identity_partition_overlap_and_insufficient_budget_rejected(self):
        variants = []
        p = deepcopy(self.plan); p['compound_registry'][1]['parent_identity'] = p['compound_registry'][0]['parent_identity']; variants.append(p)
        p = deepcopy(self.plan); p['arms'][0]['shards'][1]['compound_ids'][0] = 'C0000'; variants.append(p)
        p = deepcopy(self.plan); p['arms'][0]['shards'].pop(); variants.append(p)
        p = deepcopy(self.plan); p['min_compounds'] = 0; variants.append(p)
        p = deepcopy(self.plan); p['budget']['seed_jobs'] = 1; variants.append(p)
        p = deepcopy(self.plan); p['direction'] = 'higher'; variants.append(p)
        p = deepcopy(self.plan); p['threshold'] = True; variants.append(p)
        p = deepcopy(self.plan); p['score']['pose_rank'] = True; variants.append(p)
        p = deepcopy(self.plan); p['score']['requires_both_valid'] = 1; variants.append(p)
        for index, variant in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ValueError):
                study.register_study(self.store, 'run1', variant, key=str(index))

    def test_explicit_stop_prevents_new_dispatch_without_erasing_planned_shards(self):
        self.register(); self.bind('policy_a', 'shard0')
        study.stop_study(self.store, 'run1', 'study1', 'operator stop', key='stop')
        with self.assertRaisesRegex(ValueError, 'Stopped'):
            study.dispatch_spec(self.store, 'run1', 'study1', 'policy_a', 'shard0')
        with self.assertRaisesRegex(ValueError, 'Stopped'):
            study.execution_binding(self.store, 'run1', 'study1', 'policy_a', 'shard0')
        result = study.select_study(self.store, 'run1', 'study1', 'policy_a')
        self.assertEqual(result['status'], 'incomplete'); self.assertEqual(len(result['shards']), 2)


class RawArchiveContractTests(unittest.TestCase):
    def test_every_raw_file_is_manifest_bound_and_mutation_is_rejected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'study_binding.json').write_text('{}')
            (root / 'poses.pdbqt').write_text('raw native result')
            manifest = {'study_binding.json': study.file_hash(root/'study_binding.json')}
            (root/'artifact_hashes.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'all raw'):
                study._verify_manifest(root)
            manifest['poses.pdbqt'] = study.file_hash(root/'poses.pdbqt')
            (root/'artifact_hashes.json').write_text(json.dumps(manifest))
            study._verify_manifest(root)
            (root/'poses.pdbqt').write_text('changed result')
            with self.assertRaisesRegex(ValueError, 'changed'):
                study._verify_manifest(root)


class NativeArchiveAdapterTests(unittest.TestCase):
    """Exercise unmocked native parsing using files, with no docking execution."""
    def setUp(self):
        from tests.chemical_fixtures import annotate
        from tests.test_docking_archive import write_job
        from scripts import run_vina_batch as batch
        from scripts.vina_contract import PROFILE
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'native'
        self.root.mkdir()
        data, frozen = self.root / 'data', self.root / 'frozen'
        (data / 'receptor').mkdir(parents=True); frozen.mkdir()
        candidates = [{'candidate_id': 'C0000', 'smiles': 'C'}, {'candidate_id': 'C0001', 'smiles': 'CC'}]
        chemical = annotate(candidates)
        manifest = {'is_mock': False, 'target_chembl_id': 'fixture_target', 'construct_id': 'fixture_construct',
                    'chemical_preparation': {'method': chemical['method']}, 'candidates': candidates}
        for name, value in [('candidates.json', {'candidates': candidates}), ('manifest.json', manifest),
                            ('chemical_states.json', chemical)]:
            (data / name).write_bytes(study.encoded(value))
        for name in batch.REQUIRED_INPUTS - {'manifest.json', 'candidates.json'}:
            (data / name).write_text('Software fixture receptor evidence only\n')
        scripts = Path(batch.__file__).parent
        for name in ('vina_worker.py', 'run_vina_batch.py', 'vina_contract.py'):
            shutil.copyfile(scripts / name, frozen / name)
        self.protocol = {**PROFILE, 'phase': 'formal', 'center': [0.0] * 3, 'size': [22.0] * 3,
                         'vina_sha256': 'c' * 64, 'vina_version': {'stdout': 'AutoDock Vina v1.2.7', 'returncode': 0},
                         'input_sha256': batch.input_hashes(data), **batch.source_hashes(),
                         'chemical_preparation': {'method': chemical['method']},
                         'posebusters_configs': {'dock': {'modules': [{'chosen_binary_test_output':
                                                                     ['mol_pred_loaded', 'sanitization']}]}}}
        self._write_protocol()
        self.plan = plan_fixture(self.root.parent, count=2, shard_size=2)
        self.plan['compound_registry'] = [{'compound_id': r['candidate_id'],
            'parent_identity': r['canonical_parent_identity'], 'prepared_smiles': r['prepared_smiles'],
            'prepared_state_id': r['prepared_state_id']} for r in candidates]
        self.plan['identity_rule']['registry_sha256'] = study.digest(self.plan['compound_registry'])
        self.plan['scientific_protocol'] = {'version': 'fixture_science1', **study.protocol_contract(self.protocol, manifest)}
        self.arm, self.shard = study._locate(self.plan, 'policy_a', 'shard0')
        self.shard.update(archive_path=str(self.root), protocol_sha256=study.file_hash(self.root / 'protocol.json'))
        for row in candidates:
            for seed in study.SEEDS:
                folder = self.root / 'jobs' / f'{row["candidate_id"]}_s{seed}'
                result = write_job(folder, row, self.protocol, seed, -8.0)
                result['parent_observed_returncode'] = 0
                (folder / 'result.json').write_bytes(study.encoded(result))
        self.binding = {'effect_id': 'fixture_effect', 'effect_packet_hash': 'f' * 64,
                        'binding': {'policy_sha256': self.arm['policy_sha256']}}
        (self.root / 'study_binding.json').write_bytes(study.encoded(self.binding))
        self._manifest()

    def _write_protocol(self):
        for path in (self.root / 'protocol.json', self.root / 'frozen/protocol.json'):
            path.write_bytes(study.encoded(self.protocol))

    def _manifest(self):
        hashes = {str(p.relative_to(self.root)): study.file_hash(p) for p in self.root.rglob('*')
                  if p.is_file() and p.name != 'artifact_hashes.json'}
        (self.root / 'artifact_hashes.json').write_bytes(study.encoded(hashes))

    def read(self):
        return study._read_archive(self.root, self.plan, self.arm, self.shard, self.binding)

    def test_unmocked_adapter_checks_native_energy_and_preserves_two_seeds(self):
        rows, source = self.read()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['score'] for r in study._normalize(rows, study._identity_rows(self.plan, self.shard))], [-8.0, -8.0])
        self.assertEqual(source['archive_sha256'], study.file_hash(self.root / 'artifact_hashes.json'))
        result_path = self.root / 'jobs/C0000_s17/result.json'
        result = json.loads(result_path.read_text()); result['scores']['vina_affinity'] = -99.0
        result_path.write_bytes(study.encoded(result)); self._manifest()
        with self.assertRaisesRegex(ValueError, 'native Vina MODEL 1'):
            self.read()

    def test_native_protocol_phase_and_frozen_code_cannot_be_relabelled(self):
        self.protocol['phase'] = 'development'
        self._write_protocol(); self._manifest()
        self.shard['protocol_sha256'] = study.file_hash(self.root / 'protocol.json')
        with self.assertRaisesRegex(ValueError, 'formal phase'):
            self.read()
        self.protocol['phase'] = 'formal'
        self._write_protocol()
        self.shard['protocol_sha256'] = study.file_hash(self.root / 'protocol.json')
        with (self.root / 'frozen/vina_worker.py').open('a') as stream:
            stream.write('\n# altered fixture\n')
        self._manifest()
        with self.assertRaisesRegex(ValueError, 'Frozen worker source'):
            self.read()

    def test_raw_parent_identity_and_complete_input_registry_are_enforced(self):
        path = self.root / 'data/manifest.json'
        manifest = json.loads(path.read_text())
        manifest['candidates'][0]['canonical_parent_identity'] = 'aliased_parent'
        path.write_bytes(study.encoded(manifest)); self._manifest()
        with self.assertRaises(ValueError):
            self.read()


if __name__ == '__main__':
    unittest.main()
