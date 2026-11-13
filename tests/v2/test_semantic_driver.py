"""Real driver/ledger/adapter integration, with scripted roles and local tools."""
import json
import unittest
from dataclasses import replace

from rsi_lab.adapters import Adapter
from rsi_lab.runtime import Runtime
from tests.v2.liveness_support import open_run, Scripted, RES, none, diag
from tests.v2 import test_improvements as feedback


def query_prepare(inputs):
    return dict(inputs)


def query_reservations(packet):
    return {'tool_calls': 1}


def query_execute(effect, packet):
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1}, 'result': json.loads(packet)}


def query_publication(effect, receipt):
    return {'facts': [{'id': 'answer-' + effect['id'], 'kind': 'raw_fact',
                      'content': {**receipt['result'], 'effect_id': effect['id']},
                      'provenance': {'effect_id': effect['id']}}],
            'conclusions': [], 'data': receipt['result']}


def query_progress(effect, publication):
    result = publication['data']
    return {'query': result['query'], 'state': result['state'], 'result': result['answer'],
            'new_scores': 0, 'information_units': 1}


def candidate_progress(effect, publication):
    rows = publication['data']['evaluations']
    return {'query': [r['candidate'] for r in rows], 'state': 'fixture', 'result': rows,
            'new_scores': len(rows), 'information_units': 0}


class SemanticDriverTests(unittest.TestCase):
    def test_duplicate_lookup_reuses_canonical_evidence_without_extra_meta_or_memory(self):
        adapter = Adapter('lookup', '1', 'lookup_tool', {'scope': 'local fixture lookup'},
            query_prepare, query_reservations, query_execute, query_publication, progress=query_progress)
        store, controller, rt = open_run(self, config={'max_open_reviews': 1,
            'open_review_interval_batches': 1, 'research_features': {'scientific_memory': True}}, adapters=[adapter])
        calls = []
        def lab(data):
            calls.append(data)
            if len(calls) <= 2:
                return 'request_diagnostic', diag({'query': ['x'], 'state': 'same-snapshot',
                                                   'answer': ['not_seen']}, 'lookup')
            return none(data)
        def memory_fields(receipt, index):
            receipt['response']['scientific_memory_updates'] = '[]'
            return receipt
        executor = Scripted({'lab': lab, 'discovery': none}, receipt=memory_fields)
        rt.enqueue_lab('start')
        out = rt.run_until_wait(executor, RES, max_steps=12)
        self.assertEqual(out['status'], 'quiescent')
        records = sorted(store.list('r', 'registration', kind='semantic_result'), key=lambda r: r['created_seq'])
        self.assertEqual([r['is_new_result'] for r in records], [True, False])
        self.assertNotEqual(records[0]['effect_id'], records[1]['effect_id'])
        self.assertEqual(records[1]['canonical_evidence_ids'], records[0]['source_evidence_ids'])
        first_fact, second_fact = [r['source_evidence_ids'][0] for r in records]
        self.assertNotEqual(store.get('r', 'fact', first_fact)['content_ref'],
                            store.get('r', 'fact', second_fact)['content_ref'])
        self.assertIn(first_fact, calls[-1]['required_evidence_ids'])
        self.assertNotIn(second_fact, calls[-1]['required_evidence_ids'])
        discoveries = store.list('r', 'work', kind='meta_discovery')
        self.assertEqual(len(discoveries), 1)
        self.assertEqual(discoveries[0]['inputs']['trigger']['entry'], 'open_review')
        self.assertEqual(discoveries[0]['inputs']['trigger']['open_review_charge'], 1)
        self.assertEqual(store.get('r', 'registration', 'discovery_counters')['open_reviews_used'], 1)
        revisions = store.list('r', 'registration', kind='scientific_memory_revision')
        self.assertTrue(any(r['record'].get('source_effect_id') == records[0]['effect_id'] for r in revisions))
        self.assertFalse(any(r['record'].get('source_effect_id') == records[1]['effect_id'] for r in revisions))
        before = store.verify_history('r')
        rt.recover(); rt.recover()
        self.assertEqual(store.verify_history('r'), before)

    def test_failure_trigger_remains_delivered_when_ordinary_quota_is_zero(self):
        store, controller, rt = open_run(self, config={'max_open_reviews': 0})
        controller.add_fact('r', 'failure', 'raw_fact', {'status': 'failed_definite'},
                            {'source': 'bounded local failure receipt'}, 'failure')
        normal = rt.observe('ordinary', 'new_evidence', 100, ['failure'])
        self.assertFalse(normal['admitted'])
        failure = rt.observe('failure', 'tool_failed', 100, ['failure'])
        self.assertTrue(failure['admitted'])
        executor = Scripted({'discovery': none})
        rt.run_until_wait(executor, RES, max_steps=3)
        self.assertIn('failure', [page['evidence_id'] for page in executor.seen[0]['delivered_pages']])
        self.assertEqual(store.get('r', 'registration', 'discovery_counters')['open_reviews_used'], 0)


class PromisedReviewTests(unittest.TestCase):
    setUp = feedback.ImprovementsTests.setUp
    recommend = feedback.ImprovementsTests.recommend
    lab = feedback.ImprovementsTests.lab

    def exercise(self, failed):
        adapter = replace(self.rt.adapters.by_id['candidate_test'], progress=candidate_progress,
                          execute=feedback.failed_execute if failed else feedback.execute)
        self.rt = Runtime(self.store, 'r', adapters=[adapter])
        self.rt.config['max_open_reviews'] = 0
        self.recommend(); self.lab()
        row = self.rt.improvements.rows()[0]
        work = self.store.get('r', 'work', row['tool_work_id'])
        result = self.rt.execute_adapter('candidate_test', {'candidates': ['new']},
                                        trigger_id=work['id'], work_id=work['id'])
        self.assertEqual(result['effect']['status'], 'failed_definite' if failed else 'succeeded')
        reviews = [w for w in self.c.ready('r') if w['inputs'].get('improvement_review_ids')]
        self.assertEqual(len(reviews), 1)
        model = feedback.FakeModel('no_action', {'reason': 'Review actual tool outcome'}, review=True)
        outcome = self.rt.run_meta(reviews[0]['id'], model, self.res)
        self.assertEqual(outcome['proposal']['status'], 'applied')
        self.assertEqual(self.rt.improvements.rows()[0]['status'], 'reviewed')
        self.assertEqual(self.store.get('r', 'registration', 'discovery_counters')['open_reviews_used'], 0)
        self.assertTrue(model.inputs[0]['task']['constraints']['research_loop']['improvement_reviews'])

    def test_promised_success_review_survives_zero_ordinary_quota(self):
        self.exercise(False)

    def test_promised_failure_review_survives_zero_ordinary_quota(self):
        self.exercise(True)


if __name__ == '__main__':
    unittest.main()
