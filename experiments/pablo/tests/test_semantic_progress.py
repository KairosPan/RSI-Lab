import copy
import unittest

from pablo_task.research import project_progress, registered_adapters
from rsi_lab.store import digest


def semantic_key(description):
    return digest({key: description[key] for key in ('query', 'state', 'result')})


class SemanticProgressTests(unittest.TestCase):
    def project(self, aid, data, eid='one'):
        return project_progress({'id': eid, 'metadata': {'adapter_id': aid}}, {'data': data})

    def test_all_research_adapters_register_trusted_progress(self):
        self.assertEqual({a.adapter_id for a in registered_adapters() if a.progress is project_progress},
                         {'pablo.evaluate', 'pablo.lookup', 'literature.search'})

    def test_duplicate_only_score_receipt_and_cumulative_count_do_not_claim_progress(self):
        data = {'initial_count': 1300, 'evaluation_start': 87, 'new_evaluations': 87,
                'filters': [{'candidate': 'C', 'canonical': 'C', 'status': 'duplicate'}], 'evaluations': []}
        self.assertEqual(self.project('pablo.evaluate', data)['new_scores'], 0)
        self.assertEqual(self.project('pablo.evaluate', data)['information_units'], 0)
        data['evaluations'] = [{'candidate': 'CC', 'score': .2, 'evaluation': 88}]
        data['new_evaluations'] = 88
        self.assertEqual(self.project('pablo.evaluate', data)['new_scores'], 1)

    def test_lookup_semantics_ignore_effect_ids_aliases_order_and_duplicate_query_rows(self):
        data = {'trajectory_sha256': 'snapshot', 'as_of_observations': 1387,
                'effect_id': 'first', 'packet_sha256': 'first-packet',
                'rows': [{'candidate': 'OCC', 'canonical': 'CCO', 'status': 'pending_or_unknown',
                          'score': None, 'effect_id': 'pending-one', 'work_ids': ['old']}]}
        original = self.project('pablo.lookup', data)
        changed = copy.deepcopy(data)
        changed.update(effect_id='second', packet_sha256='second-packet')
        changed['rows'][0].update(candidate='CCO', effect_id='pending-two', work_ids=['new'])
        changed['rows'].append(dict(changed['rows'][0]))
        self.assertEqual(semantic_key(original), semantic_key(self.project('pablo.lookup', changed, 'two')))
        changed['rows'][0].update(status='already_evaluated', score=.3, observation_ordinal=1388)
        self.assertNotEqual(semantic_key(original), semantic_key(self.project('pablo.lookup', changed)))

    def test_literature_cache_slices_reuse_original_response_semantics(self):
        data = {'provider': 'EuropePMC', 'topic': 'molecular_diversity', 'query': 'bounded method query',
                'response_sha256': 'original-response', 'records': [{'id': 'a'}, {'id': 'b'}],
                'provider_total_hits': 12, 'content_status': 'partial', 'cache_hit': False}
        original = self.project('literature.search', data)
        cached = {**data, 'cache_hit': True, 'records': [{'id': 'a'}], 'effect_id': 'cache-two'}
        reused = self.project('literature.search', cached, 'two')
        self.assertEqual(semantic_key(original), semantic_key(reused))
        self.assertEqual(reused['information_units'], 0)
        self.assertEqual(original['information_units'], 1)


if __name__ == '__main__':
    unittest.main()
