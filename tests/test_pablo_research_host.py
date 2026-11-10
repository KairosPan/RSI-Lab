"""Research entrypoint accounting, frozen ancestry and domain projection checks."""
import argparse
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_pablo_continuation as fixtures
from scripts import run_pablo_research as host
from rsi_domains.pablo import adapter, oracle
from rsi_domains.pablo.research import project_measurement, registered_adapters, research_policy
from rsi_lab.store import Store, digest, encode


class ResearchHostTests(unittest.TestCase):
    setUp = fixtures.PabloContinuationTests.setUp
    make_parent = fixtures.PabloContinuationTests.make_parent

    def args(self):
        return argparse.Namespace(root=self.child, parent_root=self.parent, parent_run_id='parent',
            run_id='research-child', new_evaluations=2, parent_source_root=None,
            max_model_calls=24, literature_max_calls=3, integration_smoke=False)

    def test_preparation_enables_real_capabilities_and_preserves_parent(self):
        before = {str(p.relative_to(self.parent)): digest(p.read_bytes())
                  for p in self.parent.rglob('*') if p.is_file()}
        with patch.object(oracle, 'score', side_effect=AssertionError('No rescore on import')):
            host.prepare(self.args())
        after = {str(p.relative_to(self.parent)): digest(p.read_bytes())
                 for p in self.parent.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        store = Store(self.child)
        try:
            runtime = host.make_runtime(store, 'research-child')
            self.assertEqual(set(runtime.capability_catalog()), {'pablo.evaluate', 'pablo.lookup', 'literature.search'})
            self.assertEqual(runtime.config['research_features'], host.FEATURES)
            self.assertEqual(runtime.config['research_policy'], research_policy())
            self.assertEqual(runtime.config['research_architecture'], 'pablo_research_v2')
            self.assertEqual(adapter.read_progress(store, 'research-child')['new_evaluations'], 0)
            self.assertEqual(adapter.read_progress(store, 'research-child')['initial_count'], 300)
            self.assertEqual(store.get('research-child', 'budget', 'model_calls')['limit'], 24)
        finally:
            store.close()
        total = json.loads((self.child / 'cumulative-costs.json').read_text())
        self.assertEqual(total['prior_segment_costs']['tool_calls'], 10)
        self.assertEqual(total['current_segment_costs']['tool_calls'], 0)
        manifest = json.loads((self.child / 'manifest.json').read_text())
        self.assertEqual(manifest['research_policy'], research_policy())
        self.assertEqual(manifest['research_architecture'], 'pablo_research_v2')

    def test_final_review_is_enabled_only_after_receipt_derived_domain_quota(self):
        host.prepare(self.args())
        store = Store(self.child)
        try:
            runtime = host.make_runtime(store, 'research-child')
            before = store.verify_history('research-child')
            with self.assertRaisesRegex(ValueError, 'scoring quota'):
                host.mark_final_reviews(runtime)
            self.assertEqual(store.verify_history('research-child'), before)
            self.assertFalse(any(w['inputs'].get('final_review_only') for w in store.list('research-child', 'work')))
            packet = adapter.prepare({'run_root': str(self.child), 'run_id': 'research-child',
                                      'candidates': ['mol-301', 'mol-302']})
            work = runtime.controller.enqueue('research-child', 'diagnostic', 'fixture-final-scores', 'fixture-final-work')
            effect = runtime.controller.prepare_effect('research-child', work['id'], adapter.EFFECT_KIND, packet,
                {'tool_calls': 1}, 'fixture-final-effect', metadata={'adapter_id': adapter.ADAPTER_ID,
                                                                 'adapter_version': adapter.VERSION})
            def execute(effect, raw):
                rows = [{'slot_id': f'evaluation-{i + 1:03d}', 'evaluation': i + 1,
                         'candidate': c, 'source_index': i, 'score': 0.25}
                        for i, c in enumerate(['mol-301', 'mol-302'])]
                folder = self.child / 'pablo_evaluations' / effect['id']
                folder.mkdir(parents=True)
                journal = b''.join(encode({'effect_id': effect['id'], 'packet_sha256': digest(raw), **r}) + b'\n' for r in rows)
                (folder / 'evaluations.jsonl').write_bytes(journal)
                return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
                        'result': {'effect_id': effect['id'], 'packet_sha256': digest(raw),
                                   'evaluation_start': 0, 'evaluations': rows},
                        'execution': {'journal_sha256': digest(journal)}}
            runtime.controller.dispatch_effect('research-child', effect['id'], execute)
            self.assertEqual(adapter.read_progress(store, 'research-child')['remaining_evaluations'], 0)
            host.mark_final_reviews(runtime)
            ready_models = [w for w in runtime.controller.ready('research-child') if w['kind'] in {'lab', 'meta_discovery'}]
            self.assertTrue(ready_models)
            self.assertTrue(all(w['inputs']['final_review_only'] for w in ready_models))
        finally:
            store.close()

    def test_unknown_parent_refused_without_child_creation(self):
        store = Store(self.parent)
        store.mutate('parent', 'fixture-unknown', 'unknown', lambda tx:
            tx.insert('effect', 'unknown', {'status': 'unknown', 'accounting_unknown': True,
                                          'held_reservations': {'tool_calls': 1}}))
        store.close()
        with self.assertRaisesRegex(ValueError, 'unknown execution'):
            host.prepare(self.args())
        self.assertFalse(self.child.exists())

    def test_archive_hash_mismatch_is_rejected_before_execution(self):
        archive = self.base / 'source-archive'
        archive.mkdir()
        (archive / 'source-manifest.json').write_text(json.dumps({'source_hashes': {}}))
        with self.assertRaisesRegex(ValueError, 'archive does not match'):
            host.load_parent(self.parent, 'parent', archive)
        self.assertFalse(self.child.exists())

    def test_empty_queue_does_not_hide_unclosed_issue(self):
        host.prepare(self.args())
        store = Store(self.child)
        try:
            runtime = host.make_runtime(store, 'research-child')
            runtime.controller.open_issue('research-child', 'unresolved', ['fixture'],
                'Review did not finish', 'finalization', ['initial-observations'],
                ['A later review may settle it'], ['A supported closure'], 'open-unresolved')
            def empty_queue(tx):
                for work in tx.find('work'):
                    if work['status'] == 'ready':
                        tx.update('work', work['id'], {'status': 'cancelled'})
            store.mutate('research-child', 'empty-queue-fixture', 'fixture', empty_queue)
            self.assertEqual(runtime.controller.ready('research-child'), [])
            self.assertIn('open issue', host.finalization_problem(runtime))
        finally:
            store.close()


class PabloResearchProjectionTests(unittest.TestCase):
    def effect(self, aid):
        return {'metadata': {'adapter_id': aid}}

    def test_score_direction_and_batch_statistics_are_pablo_domain_owned(self):
        data = {'filter_counts': {'accepted': 2, 'duplicate': 1}, 'new_evaluations': 12,
                'remaining_evaluations': 188, 'evaluations': [{'score': 0.2}, {'score': 0.6}]}
        original = copy.deepcopy(data)
        metrics = project_measurement(self.effect('pablo.evaluate'), {'data': data})
        self.assertEqual(metrics['scored_count'], 2)
        self.assertAlmostEqual(metrics['batch_best'], 0.6)
        self.assertAlmostEqual(metrics['batch_mean'], 0.4)
        self.assertEqual(metrics['score_direction'], 'higher')
        self.assertEqual(metrics['measurement_scope'], 'current_evaluation_batch')
        self.assertIn('no causal benefit', metrics['interpretation_limit'])
        metrics['filter_counts']['accepted'] = 999
        self.assertEqual(data, original)
        self.assertEqual(project_measurement(self.effect('other.lower_is_better'), {'data': data}), {})
        self.assertEqual(project_measurement(self.effect('literature.search'), {'data': data}), {})

    def test_lookup_does_not_treat_retrieved_scores_as_new_evaluations(self):
        metrics = project_measurement(self.effect('pablo.lookup'), {'data': {
            'filter_counts': {'duplicate': 2}, 'new_evaluations': 12, 'remaining_evaluations': 188,
            'rows': [{'score': 0.9}], 'evaluations': [{'score': 0.9}]}})
        self.assertEqual(metrics['new_evaluations'], 12)
        self.assertEqual(metrics['filter_counts'], {'duplicate': 2})
        self.assertEqual(metrics['measurement_scope'], 'exact_lookup_snapshot')
        self.assertNotIn('scored_count', metrics)
        self.assertNotIn('batch_best', metrics)
        self.assertNotIn('batch_mean', metrics)
        self.assertEqual(project_measurement(self.effect('pablo.lookup'), {'data': {'rows': [{'score': 0.9}]}}), {})

    def test_empty_and_malformed_batches_do_not_invent_successful_scores(self):
        empty = project_measurement(self.effect('pablo.evaluate'), {'data': {'evaluations': []}})
        self.assertEqual(empty['scored_count'], 0)
        self.assertNotIn('batch_best', empty)
        for value in [float('nan'), float('inf'), True, '0.5']:
            with self.assertRaisesRegex(ValueError, 'finite score'):
                project_measurement(self.effect('pablo.evaluate'), {'data': {'evaluations': [{'score': value}]}})
        with self.assertRaisesRegex(ValueError, 'nonnegative integers'):
            project_measurement(self.effect('pablo.evaluate'), {'data': {'remaining_evaluations': -1}})

    def test_adapters_bind_projection_source_without_modifying_legacy_adapter(self):
        by_id = {a.adapter_id: a for a in registered_adapters()}
        for aid in ['pablo.evaluate', 'pablo.lookup']:
            self.assertIs(by_id[aid].measure, project_measurement)
            descriptor = by_id[aid].descriptor()
            self.assertEqual(descriptor['callbacks']['measure']['qualname'], 'project_measurement')
            self.assertIn(str(Path(__file__).resolve().parents[1] / 'rsi_domains/pablo/research.py'), descriptor['implementation_sources'])
        self.assertIsNone(by_id['literature.search'].measure)
        self.assertIsNone(adapter.make_adapter().measure)
        policy = research_policy()
        self.assertEqual(policy['recommendation_kinds'], ['candidate_selection', 'search_strategy', 'novelty_screening', 'evidence_gathering'])
        self.assertTrue(policy['final_review_guidance'].startswith('Only when final_review_only is true'))
        policy['recommendation_kinds'].clear()
        self.assertEqual(len(research_policy()['recommendation_kinds']), 4)
