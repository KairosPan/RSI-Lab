"""Real local adapters and ledger transfers; no model or external provider calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.literature_sources import canonicalize_publication
from rsi_lab.runtime import Runtime
from rsi_lab.scientific_memory import ScientificMemory, _key
from rsi_lab.store import Store, digest
from rsi_literature import make_adapters


class ScientificMemoryLiteratureSourceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        source = self.root / 'fixture.txt'
        source.write_text('Synthetic fixture paragraph with an observed control.\n\n'
                          'Second synthetic paragraph; no scientific paper is claimed.')
        self.store = Store(self.root / 'ledger')
        self.addCleanup(self.store.close)
        Controller(self.store).initialize('r', budgets={'tool_calls': {'limit': 30, 'hard': True}},
            config={'research_features': {'scientific_memory': True}, 'goal': 'Inspect local observations'})
        self.runtime = Runtime(self.store, 'r', adapters=make_adapters(self.root / 'documents', {
            'enabled_providers': ['europe_pmc'],
            'local_sources': {'fixture': {'path': str(source), 'content_scope': 'excerpt',
                                         'paper_ref': 'local-synthetic-fixture'}}}))
        self.memory = self.runtime.scientific_memory
        self.n = 0
        acquired, _ = self.call('acquire_fulltext', {'source_id': 'fixture'})
        self.document_id = acquired['result']['data']['document_id']

    def call(self, operation, inputs):
        self.n += 1
        output = self.runtime.execute_adapter('literature.' + operation, inputs, trigger_id='call-' + str(self.n))
        self.assertEqual(output['effect']['status'], 'succeeded')
        self.assertEqual(output['result']['status'], 'ok', output['result'])
        record = self.store.list('r', 'registration', kind='adapter_publication',
                                 effect_id=output['effect']['id'])[0]
        return output, json.loads(self.store.read_blob(record['publication_ref']))

    def read(self):
        return self.call('read_document', {'document_id': self.document_id, 'max_bytes': 32768})

    def excerpts(self, publication):
        return [row for row in publication['facts'] if row['provenance'].get('delivery_kind') == 'source_excerpt']

    def evidence(self, memory, fact_id):
        return [record for record in memory.read_history(kind='evidence')
                if any(ref['fact_id'] == fact_id for ref in record['source_refs'])]

    def target(self):
        self.n += 1
        store = Store(self.root / ('target-' + str(self.n)))
        self.addCleanup(store.close)
        Controller(store).initialize('target', budgets={}, config={'goal': 'Continue local observations'})
        return ScientificMemory(store, 'target')

    def patch_registration(self, rid, fields):
        self.n += 1
        return self.store.mutate('r', 'tamper-' + str(self.n), 'fixture_modified',
                                 lambda tx: tx.update('registration', rid, fields))

    def test_two_actual_reads_keep_one_original_record_and_separate_investigations(self):
        first, publication = self.read()
        sources = self.excerpts(publication)
        self.assertEqual(len(sources), 2)
        snapshots = {row['id']: (self.store.get('r', 'fact', row['id']),
                               self.evidence(self.memory, row['id'])) for row in sources}
        second, second_publication = self.read()
        self.assertEqual(self.excerpts(second_publication), sources)
        self.assertNotEqual(first['effect']['id'], second['effect']['id'])
        for fid, (fact, evidence) in snapshots.items():
            self.assertEqual(self.store.get('r', 'fact', fid), fact)
            self.assertEqual(self.evidence(self.memory, fid), evidence)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]['revision'], 1)
            self.assertEqual(evidence[0]['source_effect_id'], first['effect']['id'])
            self.assertEqual(evidence[0]['source_refs'][0]['content_sha256'], fact['content_ref'])
            reads = self.store.list('r', 'registration', kind='literature_source_read', canonical_fact_id=fid)
            self.assertEqual({row['effect_id'] for row in reads}, {first['effect']['id'], second['effect']['id']})
            self.assertTrue(all(row['authority'] == 'tool_result_not_model_delivery' for row in reads))
        investigations = self.memory.read_history(kind='investigation')
        self.assertTrue({first['effect']['id'], second['effect']['id']}.issubset(
            {row['source_effect_id'] for row in investigations}))
        self.assertEqual(self.store.list('r', 'registration', kind='scientific_memory_source_change'), [])
        before = self.store.verify_history('r')
        for output, published in ((first, publication), (second, second_publication)):
            self.memory.observe_publication(output['effect'], published)
            self.runtime.publish_adapter_effect(output['effect'])
        self.assertEqual(self.store.verify_history('r'), before)

    def test_incremental_bundle_transfer_preserves_original_owner_and_source_identity(self):
        first, first_publication = self.read()
        first_bundle = self.memory.export_bundle()
        target = self.target()
        target.import_bundle(first_bundle)
        source_ids = {row['id'] for row in self.excerpts(first_publication)}
        originals = {fid: self.evidence(target, fid) for fid in source_ids}
        aliases_before = {row['source_ref']['fact_id']: row for row in target.store.list(
            'target', 'registration', kind='scientific_memory_source_alias') if row['source_ref']['fact_id'] in source_ids}
        second, _ = self.read()
        second_bundle = self.memory.export_bundle()
        self.assertNotEqual(first_bundle['sha256'], second_bundle['sha256'])
        target.import_bundle(second_bundle)
        for fid in source_ids:
            self.assertEqual(self.evidence(target, fid), originals[fid])
            self.assertEqual(originals[fid][0]['origin_run_id'], 'r')
            self.assertEqual(originals[fid][0]['source_refs'][0]['run_id'], 'r')
            aliases = target.store.list('target', 'registration', kind='scientific_memory_source_alias')
            self.assertEqual([row for row in aliases if row['source_ref']['fact_id'] == fid], [aliases_before[fid]])
            local = target.store.get('target', 'fact', aliases_before[fid]['local_fact_id'])
            self.assertEqual(local['provenance']['original_source']['fact_id'], fid)
            self.assertEqual(local['provenance']['original_provenance']['effect_id'], first['effect']['id'])
        self.assertTrue(any(row['source_effect_id'] == second['effect']['id']
                            for row in target.read_history(kind='investigation')))
        self.assertEqual(target.store.list('target', 'registration', kind='scientific_memory_source_change'), [])
        before = target.store.verify_history('target')
        target.import_bundle(second_bundle)
        self.assertEqual(target.store.verify_history('target'), before)

    def test_history_import_in_either_order_keeps_first_wrapper_and_distinct_read_origins(self):
        first, first_publication = self.read()
        second, _ = self.read()
        source_ids = {row['id'] for row in self.excerpts(first_publication)}
        for order in ((first, second), (second, first)):
            with self.subTest(first_effect=order[0]['effect']['id']):
                target = self.target()
                target.import_history(self.store, 'r', [order[0]['effect']['id']])
                initial = {row['id']: row for row in target.store.list('target', 'fact')
                    if row['provenance'].get('original_provenance', {}).get('delivery_kind') == 'source_excerpt'}
                evidence = {fid: self.evidence(target, fid) for fid in initial}
                self.assertEqual(len(initial), len(source_ids))
                target.import_history(self.store, 'r', [order[1]['effect']['id']])
                for fid, original in initial.items():
                    self.assertEqual(target.store.get('target', 'fact', fid), original)
                    self.assertEqual(self.evidence(target, fid), evidence[fid])
                    provenance = original['provenance']
                    self.assertEqual(provenance['historical_scientific_source']['run_id'], 'r')
                    self.assertTrue(provenance['historical_scientific_source']['historical'])
                    self.assertEqual(provenance['historical_scientific_source']['effect_id'], order[0]['effect']['id'])
                    self.assertEqual(provenance['original_provenance']['effect_id'], first['effect']['id'])
                investigations = target.read_history(kind='investigation')
                self.assertEqual({row['content']['origin']['effect_id'] for row in investigations},
                                 {first['effect']['id'], second['effect']['id']})
                self.assertTrue(all(row['source_effect_id'] is None for row in investigations))
                self.assertEqual(target.store.list('target', 'registration', kind='scientific_memory_source_change'), [])
                before = target.store.verify_history('target')
                target.import_history(self.store, 'r', [first['effect']['id'], second['effect']['id']])
                self.assertEqual(target.store.verify_history('target'), before)

    def mixed_history(self):
        # Run the actual pre-canonicalization adapter publication contract once;
        # its per-effect IDs and original provenance remain in the real ledger.
        with patch('rsi_lab.literature_sources.canonicalize_publication',
                   side_effect=lambda store, run, effect, pub: (copy.deepcopy(pub), [])):
            legacy, legacy_publication = self.read()
        current, current_publication = self.read()
        self.assertEqual(self.excerpts(legacy_publication), self.excerpts(current_publication))
        self.assertTrue(all(row['id'].startswith('literature_passage_')
                            for row in self.excerpts(legacy_publication)))
        return legacy, current, legacy_publication

    def test_mixed_legacy_canonical_history_imports_both_orders_without_new_source_revision(self):
        legacy, current, publication = self.mixed_history()
        source_ids = {row['id'] for row in self.excerpts(publication)}
        for order in ((legacy, current), (current, legacy)):
            with self.subTest(first_effect=order[0]['effect']['id']):
                target = self.target()
                target.import_history(self.store, 'r', [order[0]['effect']['id']])
                facts = {row['id']: row for row in target.store.list('target', 'fact')
                    if row['provenance'].get('original_provenance', {}).get('delivery_kind') == 'source_excerpt'}
                evidence = {fid: self.evidence(target, fid) for fid in facts}
                target.import_history(self.store, 'r', [order[1]['effect']['id']])
                self.assertEqual(len(facts), len(source_ids))
                for fid, original in facts.items():
                    self.assertEqual(target.store.get('target', 'fact', fid), original)
                    self.assertEqual(self.evidence(target, fid), evidence[fid])
                    self.assertEqual(evidence[fid][0]['revision'], 1)
                    self.assertEqual(original['provenance']['original_provenance']['effect_id'], legacy['effect']['id'])
                    self.assertEqual(original['provenance']['historical_scientific_source']['effect_id'], order[0]['effect']['id'])
                investigations = target.read_history(kind='investigation')
                self.assertEqual({row['content']['origin']['effect_id'] for row in investigations},
                                 {legacy['effect']['id'], current['effect']['id']})
                self.assertEqual(target.store.list('target', 'registration', kind='scientific_memory_source_change'), [])
                before = target.store.verify_history('target')
                target.import_history(self.store, 'r', [legacy['effect']['id'], current['effect']['id']])
                self.assertEqual(target.store.verify_history('target'), before)
        # Default registration enumeration must not rely on chronological IDs.
        self.target().import_history(self.store, 'r')

    def test_reverse_legacy_import_rejects_unbridged_prior_origin_and_changed_first_provenance(self):
        legacy, current, _ = self.mixed_history()
        target = self.target()
        target.import_history(self.store, 'r', [current['effect']['id']])
        registration = self.store.list('r', 'registration', kind='adapter_publication',
                                      effect_id=current['effect']['id'])[0]
        fields = ('source_identity_policy', 'source_read_ids', 'original_publication_ref')
        self.patch_registration(registration['id'], dict(zip(fields, (None, [], None))))
        before = target.store.verify_history('target')
        with self.assertRaisesRegex(ValueError, 'forged first-read origin'):
            target.import_history(self.store, 'r', [legacy['effect']['id']])
        self.assertEqual(target.store.verify_history('target'), before)
        self.patch_registration(registration['id'], {key: registration[key] for key in fields})
        source = next(row for row in target.store.list('target', 'fact')
                      if row['provenance'].get('original_provenance', {}).get('delivery_kind') == 'source_excerpt')
        forged = copy.deepcopy(source['provenance'])
        forged['original_provenance']['effect_id'] = 'invented-first-owner'
        content = json.loads(target.store.read_blob(source['content_ref']))
        target = self.target()
        Controller(target.store).add_fact('target', source['id'], source['kind'], content, forged,
                                         'forged-first-provenance-fixture')
        before = target.store.verify_history('target')
        with self.assertRaisesRegex(ValueError, 'differs from its immutable original'):
            target.import_history(self.store, 'r', [legacy['effect']['id']])
        self.assertEqual(target.store.verify_history('target'), before)

    def test_reverse_legacy_import_requires_legacy_effect_own_receipt_and_adapter(self):
        legacy, current, _ = self.mixed_history()
        target = self.target()
        target.import_history(self.store, 'r', [current['effect']['id']])
        effect = legacy['effect']
        empty_ref = self.store.put_blob({'status': 'succeeded', 'result': {'status': 'ok', 'data': {'passages': []}}})
        changes = [{'result_ref': empty_ref}, {'metadata': {**effect['metadata'], 'adapter_id': 'different.adapter'}}]
        for number, changed in enumerate(changes):
            with self.subTest(fields=changed):
                self.store.mutate('r', 'forge-owned-receipt-' + str(number), 'fixture_modified',
                                  lambda tx: tx.update('effect', effect['id'], changed))
                before = target.store.verify_history('target')
                with self.assertRaisesRegex(ValueError, 'owned receipt or canonical reverse bridge'):
                    target.import_history(self.store, 'r', [effect['id']])
                self.assertEqual(target.store.verify_history('target'), before)
                self.store.mutate('r', 'restore-owned-receipt-' + str(number), 'fixture_modified',
                    lambda tx: tx.update('effect', effect['id'], {key: effect[key] for key in changed}))

    def test_missing_or_forged_current_read_bridge_rejects_before_memory_write(self):
        first, _ = self.read()
        second, published = self.read()
        eid = second['effect']['id']
        registration = self.store.list('r', 'registration', kind='adapter_publication', effect_id=eid)[0]
        read = self.store.get('r', 'registration', registration['source_read_ids'][0])
        index = self.store.list('r', 'registration', kind='literature_source_identity',
                                canonical_fact_id=read['canonical_fact_id'])[0]
        first_registration = self.store.list('r', 'registration', kind='adapter_publication',
                                             effect_id=first['effect']['id'])[0]
        cases = [
            (registration, {'source_read_ids': []}),
            (registration, {'source_read_ids': ['missing-read']}),
            (registration, {'source_identity_policy': None}),
            (registration, {'source_identity_policy': 'unrecognized-policy'}),
            (registration, {'original_publication_ref': first_registration['original_publication_ref']}),
            (read, {'effect_id': first['effect']['id']}),
            (read, {'result_ref': first['effect']['result_ref']}),
            (read, {'original_publication_sha256': 'f' * 64}),
            (read, {'source_identity_sha256': 'f' * 64}),
            (read, {'source_identity': {**read['source_identity'], 'locator': 'forged'}}),
            (index, {'canonical_fact_id': 'nonexistent'}),
        ]
        for row, changes in cases:
            with self.subTest(kind=row['kind'], changes=changes):
                self.patch_registration(row['id'], changes)
                before = self.store.verify_history('r')
                with self.assertRaises(ValueError):
                    self.memory.observe_publication(second['effect'], published)
                self.assertEqual(self.store.verify_history('r'), before)
                target = self.target()
                with self.assertRaises(ValueError):
                    target.import_history(self.store, 'r', [eid])
                self.assertEqual(target.store.list('target', 'fact'), [])
                self.patch_registration(row['id'], {key: row[key] for key in changes})

    def test_consistent_bridge_cannot_claim_a_passage_missing_from_actual_tool_receipt(self):
        _, first_publication = self.read()
        receipt = {'status': 'succeeded', 'result': {'status': 'ok', 'data': {'passages': []}}}
        receipt_ref = self.store.put_blob(receipt)
        effect = {'id': 'forged-current-receipt', 'run_id': 'r', 'kind': 'diagnostic',
            'status': 'succeeded', 'accounting_unknown': False, 'result_ref': receipt_ref,
            'metadata': {'adapter_id': 'literature.read_document'}}
        original = {'facts': [], 'conclusions': [], 'data': {}}
        for number, row in enumerate(self.excerpts(first_publication)):
            row = copy.deepcopy(row)
            row['id'] = 'forged-original-' + str(number)
            row['provenance'].update(effect_id=effect['id'], result_ref=receipt_ref)
            original['facts'].append(row)
        published, reads = canonicalize_publication(self.store, 'r', effect, original)
        def fixture(tx):
            stored = tx.insert('effect', effect['id'], {key: value for key, value in effect.items()
                                                       if key not in {'id', 'run_id'}})
            for read in reads:
                tx.insert('registration', read['id'], {key: value for key, value in read.items() if key != 'id'})
            tx.insert('registration', 'forged-publication', {'kind': 'adapter_publication',
                'effect_id': effect['id'], 'adapter_id': 'literature.read_document',
                'publication_ref': tx.blob(published), 'original_publication_ref': tx.blob(original),
                'source_read_ids': [read['id'] for read in reads],
                'source_identity_policy': 'literature_source_identity_v1'})
            return stored
        stored = self.store.mutate('r', 'forged-consistent-bridge', 'fixture_modified', fixture)
        before = self.store.verify_history('r')
        with self.assertRaisesRegex(ValueError, 'bound to the current receipt'):
            self.memory.observe_publication(stored, published)
        self.assertEqual(self.store.verify_history('r'), before)
        target = self.target()
        with self.assertRaisesRegex(ValueError, 'bound to the current receipt'):
            target.import_history(self.store, 'r', [stored['id']])
        self.assertEqual(target.store.list('target', 'fact'), [])

    def test_unbridged_cross_effect_source_and_forged_historical_owner_reject(self):
        first, _ = self.read()
        second, publication = self.read()
        eid = second['effect']['id']
        registration = self.store.list('r', 'registration', kind='adapter_publication', effect_id=eid)[0]
        self.patch_registration(registration['id'], {'source_identity_policy': None,
            'source_read_ids': [], 'original_publication_ref': None})
        with self.assertRaisesRegex(ValueError, 'current publication read bridge'):
            self.memory.observe_publication(second['effect'], publication)
        self.patch_registration(registration['id'], {key: registration[key] for key in
            ('source_identity_policy', 'source_read_ids', 'original_publication_ref')})
        row = self.excerpts(publication)[0]
        old = self.store.get('r', 'fact', row['id'])
        fid = _key('history_fact', 'r', row['id'], old['content_ref'])
        for forged_origin in ({'run_id': 'wrong-owner', 'historical': True},
                              {'run_id': 'r', 'historical': True, 'effect_id': first['effect']['id'],
                               'receipt_ref': 'f' * 64, 'publication_ref': registration['publication_ref']}):
            target = self.target()
            Controller(target.store).add_fact('target', fid, row['kind'], row['content'],
                {'historical_scientific_source': forged_origin, 'original_provenance': old['provenance']}, 'forged-fixture')
            before = target.store.verify_history('target')
            with self.assertRaisesRegex(ValueError, 'owner|publication binding'):
                target.import_history(self.store, 'r', [eid])
            self.assertEqual(target.store.verify_history('target'), before)


if __name__ == '__main__':
    unittest.main()
