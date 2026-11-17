"""Read-only canonicalization of actual adapter publications from local fixtures."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.literature_sources import (canonicalize_publication, source_identity,
    source_identity_registration, READ_KIND, IDENTITY_KIND)
from rsi_lab.store import Store, digest, META
from rsi_literature.adapters import publication as adapter_publication


class LiteratureSourcesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        for run_id in ('r', 'other'):
            self.controller.initialize(run_id, budgets={'tool_calls': {'limit': 20, 'hard': True}})
        text = 'Synthetic original source text 文献.'
        self.passage = {'document_id': 'doc_fixture', 'document_version': 'a' * 64,
            'source_sha256': 'b' * 64, 'text_sha256': 'c' * 64, 'locator': 'block:0', 'text': text,
            'char_range': [0, len(text)], 'byte_range': [0, len(text.encode())],
            'kind': 'paragraph', 'source_metadata': {'fixture': True}}
        self.sequence = 0

    def reading(self, number, passages=None, run_id='r', operation='read_document'):
        result = {'status': 'ok', 'data': {'passages': copy.deepcopy(passages or [self.passage])},
                  'provenance': {'attempt_count': 0}, 'coverage': {}, 'continuation': None, 'errors': []}
        receipt = {'status': 'succeeded', 'result': result}
        effect = {'id': 'reading-' + str(number), 'run_id': run_id, 'status': 'succeeded',
            'result_ref': self.store.put_blob(receipt), 'accounting_unknown': False,
            'metadata': {'adapter_id': 'literature.' + operation}}
        return effect, adapter_publication(effect, receipt)

    def excerpts(self, publication):
        return [row for row in publication['facts'] if row['provenance'].get('delivery_kind') == 'source_excerpt']

    def persist(self, run_id, publication, reads):
        self.sequence += 1
        def apply(tx):
            for row in publication['facts']:
                value = {'kind': row['kind'], 'content_ref': tx.blob(row['content']), 'provenance': row['provenance']}
                existing = tx.find('fact', id=row['id'])
                if existing:
                    self.assertTrue(all(existing[0][key] == item for key, item in value.items()))
                else:
                    tx.insert('fact', row['id'], value)
            for read in reads:
                for record in (source_identity_registration(read), read):
                    body = {key: value for key, value in record.items() if key != 'id'}
                    existing = tx.find('registration', id=record['id'])
                    if existing:
                        self.assertEqual({key: value for key, value in existing[0].items() if key not in META}, body)
                    else:
                        tx.insert('registration', record['id'], body)
        self.store.mutate(run_id, 'persist-' + str(self.sequence), 'fixture_publication', apply)

    def test_two_tool_reads_share_one_original_and_keep_two_bound_read_receipts(self):
        first_effect, first_raw = self.reading(1)
        before, original = self.store.verify_history('r'), copy.deepcopy(first_raw)
        first, reads = canonicalize_publication(self.store, 'r', first_effect, first_raw)
        self.assertEqual(self.store.verify_history('r'), before)
        self.assertEqual(first_raw, original)
        self.assertEqual(set(first), {'facts', 'conclusions', 'data'})
        self.assertEqual(first['data'], first_raw['data'], 'Raw returned passages remain intact')
        canonical = self.excerpts(first)[0]
        self.assertNotEqual(canonical['id'], self.excerpts(first_raw)[0]['id'])
        self.assertFalse(reads[0]['reused'])
        self.assertEqual(reads[0]['original_publication_sha256'], digest(first_raw))
        self.persist('r', first, reads)
        original_fact = self.store.get('r', 'fact', canonical['id'])
        effect, raw = self.reading(2, operation='find_passages')
        second, second_reads = canonicalize_publication(self.store, 'r', effect, raw)
        self.assertEqual(self.excerpts(second), [canonical])
        self.assertTrue(second_reads[0]['reused'])
        self.assertEqual(second_reads[0]['effect_id'], effect['id'])
        self.assertEqual(second_reads[0]['result_ref'], effect['result_ref'])
        self.assertEqual(second_reads[0]['original_fact_id'], self.excerpts(raw)[0]['id'])
        self.assertEqual(second['facts'][0]['content']['data']['passage_evidence_ids'], [canonical['id']])
        self.assertNotEqual(second['facts'][0]['id'], first['facts'][0]['id'])
        self.persist('r', second, second_reads)
        self.assertEqual(self.store.get('r', 'fact', canonical['id']), original_fact)
        records = self.store.list('r', 'registration', kind=READ_KIND, canonical_fact_id=canonical['id'])
        self.assertEqual({record['effect_id'] for record in records}, {first_effect['id'], effect['id']})
        self.assertTrue(all(record['authority'] == 'tool_result_not_model_delivery' for record in records))
        self.assertEqual(len(self.store.list('r', 'registration', kind=IDENTITY_KIND)), 1)
        snapshot = self.store.verify_history('r')
        self.assertEqual(canonicalize_publication(self.store, 'r', effect, raw), (second, second_reads))
        self.assertEqual(self.store.verify_history('r'), snapshot)
        # Replaying the first publication after materialization remains stable too.
        self.assertEqual(canonicalize_publication(self.store, 'r', first_effect, first_raw), (first, reads))

    def test_whitespace_only_source_range_is_preserved_byte_for_byte(self):
        passage = {**self.passage, 'text': ' \n\t', 'char_range': [7, 10], 'byte_range': [7, 10]}
        effect, raw = self.reading(1, [passage])
        normalized, reads = canonicalize_publication(self.store, 'r', effect, raw)
        self.assertEqual(self.excerpts(normalized)[0]['content'], passage)
        self.assertEqual(reads[0]['source_identity']['content_sha256'], digest(passage))
        self.persist('r', normalized, reads)
        effect, raw = self.reading(2, [passage])
        repeated, _ = canonicalize_publication(self.store, 'r', effect, raw)
        self.assertEqual(self.excerpts(repeated), self.excerpts(normalized))

    def test_same_publication_duplicate_sources_collapse_facts_not_read_records(self):
        effect, raw = self.reading(1, [self.passage, self.passage])
        normalized, reads = canonicalize_publication(self.store, 'r', effect, raw)
        self.assertEqual(len(self.excerpts(normalized)), 1)
        self.assertEqual(len(reads), 2)
        self.assertEqual(len({row['id'] for row in reads}), 2)
        fid = reads[0]['canonical_fact_id']
        self.assertTrue(all(row['canonical_fact_id'] == fid for row in reads))
        self.assertEqual(normalized['facts'][0]['content']['data']['passage_evidence_ids'], [fid])
        self.assertEqual(len(normalized['data']['data']['passages']), 2)

    def test_legacy_first_representative_and_provenance_are_retained_then_indexed(self):
        effect, legacy = self.reading(1)
        original = self.excerpts(legacy)[0]
        first = {**copy.deepcopy(original), 'id': 'zz_earlier_legacy'}
        later = {**copy.deepcopy(original), 'id': 'aa_later_legacy'}
        later['provenance']['effect_id'] = 'later-legacy-reading'
        for row in (first, later):
            self.controller.add_fact('r', row['id'], row['kind'], row['content'], row['provenance'], row['id'])
        before = self.store.list('r', 'fact')
        effect, raw = self.reading(3)
        normalized, reads = canonicalize_publication(self.store, 'r', effect, raw)
        self.assertEqual(self.excerpts(normalized), [first])
        self.assertEqual(self.store.list('r', 'fact'), before)
        self.persist('r', normalized, reads)
        with patch.object(self.store, 'list', wraps=self.store.list) as queried:
            self.assertEqual(canonicalize_publication(self.store, 'r', effect, raw), (normalized, reads))
        self.assertFalse(any(call.args[1] == 'fact' and 'id' not in call.kwargs for call in queried.call_args_list))

    def test_run_document_version_locator_source_and_complete_content_are_distinct(self):
        identities = []
        changed_text = 'Different exact source content.'
        variants = [self.passage,
            {**self.passage, 'document_id': 'different-document'},
            {**self.passage, 'document_version': 'd' * 64},
            {**self.passage, 'source_sha256': 'e' * 64},
            {**self.passage, 'locator': 'block:1'},
            {**self.passage, 'source_metadata': {'fixture': 'changed metadata'}},
            {**self.passage, 'text': changed_text, 'char_range': [0, len(changed_text)],
             'byte_range': [0, len(changed_text.encode())]}]
        for number, passage in enumerate(variants):
            effect, raw = self.reading(number, [passage])
            normalized, reads = canonicalize_publication(self.store, 'r', effect, raw)
            identities.append(reads[0]['canonical_fact_id'])
            self.persist('r', normalized, reads)
        effect, raw = self.reading(1, run_id='other')
        other, _ = canonicalize_publication(self.store, 'other', effect, raw)
        identities.append(self.excerpts(other)[0]['id'])
        self.assertEqual(len(identities), len(set(identities)))

    def test_nonexcerpt_publications_are_unchanged_and_inconsistent_excerpt_markers_reject(self):
        effect, raw = self.reading(1)
        row = self.excerpts(raw)[0]
        for marker in ('summary', 'pointer', 'source_image', 'tool_observation'):
            unchanged = {'facts': [{**copy.deepcopy(row), 'provenance': {
                **row['provenance'], 'delivery_kind': marker}}], 'conclusions': [], 'data': {'unchanged': True}}
            self.assertEqual(canonicalize_publication(self.store, 'r', {}, unchanged), (unchanged, []))
            self.assertIsNone(source_identity(unchanged['facts'][0]))
        cases = [
            {**row, 'kind': 'hypothesis'},
            {**row, 'content': {**row['content'], 'document_version': 'wrong'}},
            {**row, 'content': {**row['content'], 'text': ''}},
            {**row, 'content': {**row['content'], 'kind': 'summary'}},
            {**row, 'content': {**row['content'], 'char_range': [False, 1]}},
            {**row, 'provenance': {**row['provenance'], 'summary_of': ['not-original']}},
            {**row, 'provenance': {**row['provenance'], 'locator': 'different'}},
        ]
        for malformed in cases:
            with self.subTest(row=malformed), self.assertRaises(ValueError):
                source_identity(malformed)
        for key, bad in [('effect_id', 'other'), ('result_ref', 'f' * 64), ('adapter_id', 'another.adapter')]:
            invalid = copy.deepcopy(raw)
            self.excerpts(invalid)[0]['provenance'][key] = bad
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'current tool receipt'):
                canonicalize_publication(self.store, 'r', effect, invalid)
        with self.assertRaisesRegex(ValueError, 'same run'):
            canonicalize_publication(self.store, 'other', effect, raw)

    def test_poisoned_index_occupied_fact_and_unbound_pointers_fail_closed(self):
        effect, raw = self.reading(1)
        normalized, reads = canonicalize_publication(self.store, 'r', effect, raw)
        index = source_identity_registration(reads[0])
        self.controller.add_fact('r', 'unrelated', 'source_statement', {'text': 'not this source'},
                                 {'source': 'fixture'}, 'unrelated')
        bad_index = {**index, 'canonical_fact_id': 'unrelated'}
        self.store.mutate('r', 'bad-index', 'fixture', lambda tx: tx.insert('registration', bad_index['id'],
            {key: value for key, value in bad_index.items() if key != 'id'}))
        with self.assertRaisesRegex(ValueError, 'immutable original'):
            canonicalize_publication(self.store, 'r', effect, raw)
        other_effect, other_raw = self.reading(2, run_id='other')
        other_normalized, _ = canonicalize_publication(self.store, 'other', other_effect, other_raw)
        fid = self.excerpts(other_normalized)[0]['id']
        self.controller.add_fact('other', fid, 'source_statement', {'text': 'occupied'},
                                 {'delivery_kind': 'pointer'}, 'occupied')
        with self.assertRaisesRegex(ValueError, 'occupied'):
            canonicalize_publication(self.store, 'other', other_effect, other_raw)
        third, invalid = self.reading(3, [{**self.passage, 'locator': 'block:3'}])
        invalid['facts'][0]['content']['data']['passage_evidence_ids'] = ['outside-publication']
        with self.assertRaisesRegex(ValueError, 'pointers'):
            canonicalize_publication(self.store, 'r', third, invalid)


if __name__ == '__main__':
    unittest.main()
