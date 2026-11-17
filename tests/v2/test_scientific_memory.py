"""Real ledger, exact packets and receipts; no model or external service calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.packets import PacketBuilder, make_evidence
from rsi_lab.scientific_memory import ScientificMemory
from rsi_lab.store import Store, digest, encode


class ScientificMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'source')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={}, config={'task_id': 'test-task', 'goal': 'Compare measured alternatives'})
        self.n = 0
        self.provider_calls = []

        def project(store, run, effect, publication):
            self.provider_calls.append((store, run, effect['id'] if effect else None))
            fact = publication['facts'][0]
            rows = fact['content'].get('observations', [])
            return {'identities': [{'identity': row['candidate'], 'conditions': {'protocol': 'frozen-v1'},
                                   'fact_id': fact['id'], 'locator': 'observations/' + str(i)} for i, row in enumerate(rows)],
                    'experience': {'summary': 'These measured alternatives did not improve the baseline',
                                   'scope': {'candidate_ids': [r['candidate'] for r in rows]}, 'outcome': 'negative'}}
        self.project = project
        self.memory = ScientificMemory(self.store, 'r', identity_provider=project)
        self.question = self.memory.ensure_question()['id']
        self.add_fact('f1', {'observations': [{'candidate': 'A', 'score': 0.2}], 'baseline': 0.3})

    def tx(self, fn):
        self.n += 1
        return self.store.mutate('r', 'fixture:' + str(self.n), 'fixture_created', fn)

    def add_fact(self, fid, content):
        self.controller.add_fact('r', fid, 'raw_fact', content, {'source': 'synthetic instrument'}, 'fact:' + fid)

    def update(self, oid='claim-A', kind='claim', revision=0, sources=None, content=None, refs=None):
        content = content or {'statement': 'A did not exceed the measured baseline',
                              'scope': {'candidate_ids': ['A']}, 'judgment': 'tentative', 'outcome': 'negative'}
        return {'kind': kind, 'id': oid, 'expected_revision': revision, 'question_ids': [self.question],
                'content': content, 'source_evidence_ids': ['f1'] if sources is None else sources,
                'object_refs': [] if refs is None else refs}

    def model_call(self, updates, *, evidence_ids=('f1',), context=None, partial=False, response_override=False):
        catalog = {}
        for fid in evidence_ids:
            f = self.store.get('r', 'fact', fid)
            catalog[fid] = make_evidence(fid, self.store.read_blob(f['content_ref']).decode(),
                kind=f['kind'], source=encode(f['provenance']).decode())
        context = self.memory.prepare_context('lab', list(evidence_ids), budget=100000) if context is None else context
        task = {'scientific_memory': context}
        packet = PacketBuilder(catalog, max_input_bytes=1000000, page_bytes=8).build(
            mode='lab', instruction='Use source-bound science', schema={}, task=task,
            evidence_ids=list(evidence_ids), required_evidence_ids=[] if partial else list(evidence_ids),
            strategy='paged' if partial else 'complete')
        response = updates if response_override else {'command': {'action': 'no_action'},
                    'scientific_memory_updates': json.dumps(updates)}
        self.n += 1
        eid = 'model-' + str(self.n)
        def apply(tx):
            pref = tx.blob(packet['envelope_bytes'])
            plan = {k: v for k, v in packet.items() if k != 'envelope_bytes'}
            receipt = {'status': 'succeeded', 'protocol_valid': True, 'response': response,
                       'sent_packet_sha256': pref, 'sent_prompt_sha256': digest(packet['envelope']['prompt'].encode())}
            rr = tx.blob(receipt)
            return tx.insert('effect', eid, {'kind': 'lab', 'status': 'succeeded', 'accounting_unknown': False,
                'result_ref': rr, 'receipt_ref': rr, 'packet_ref': pref,
                'metadata': {'plan_ref': tx.blob(plan)}})
        effect = self.tx(apply)
        return effect, packet, response

    def apply(self, update, **kwargs):
        e, p, r = self.model_call([update], **kwargs)
        return self.memory.apply_updates(e, p, r)

    def publication(self, fid='result-1', candidate='B'):
        self.n += 1
        eid = 'instrument-' + str(self.n)
        result = {'observations': [{'candidate': candidate, 'score': 0.1}], 'baseline': 0.3}
        def apply(tx):
            rr = tx.blob({'status': 'succeeded', 'result': result})
            pub = {'facts': [{'id': fid, 'kind': 'raw_fact', 'content': result,
                'provenance': {'effect_id': eid, 'execution_ref': rr, 'adapter_id': 'test.measure'}}],
                'conclusions': [], 'data': result}
            fact = pub['facts'][0]
            tx.insert('fact', fid, {'kind': fact['kind'], 'content_ref': tx.blob(result), 'provenance': fact['provenance']})
            e = tx.insert('effect', eid, {'kind': 'diagnostic', 'status': 'succeeded', 'accounting_unknown': False,
                'result_ref': rr, 'receipt_ref': rr, 'packet_ref': tx.blob({'candidate': candidate}), 'metadata': {}})
            tx.insert('registration', 'publication-' + eid, {'kind': 'adapter_publication', 'effect_id': eid,
                'status': 'published', 'publication_ref': tx.blob(pub)})
            return {'effect': e, 'publication': pub}
        value = self.tx(apply)
        return value['effect'], value['publication']

    def target(self):
        store = Store(Path(self.tmp.name) / ('target-' + str(self.n)))
        self.n += 1
        self.addCleanup(store.close)
        Controller(store).initialize('next', budgets={}, config={'task_id': 'test-task', 'goal': 'Continue same task'})
        return store, ScientificMemory(store, 'next', identity_provider=self.project)

    def test_six_kinds_source_bound_and_model_cannot_self_validate(self):
        e, pub = self.publication()
        self.memory.observe_publication(e, pub)
        self.assertEqual(self.apply(self.update())['status'], 'applied')
        decision = self.update('decision-A', 'decision', content={'decision': 'Try another alternative', 'scope': {'candidate_ids': ['A']}})
        self.assertEqual(self.apply(decision)['status'], 'applied')
        kinds = {r['kind'] for r in self.memory.read_history()}
        self.assertEqual(kinds, {'question', 'evidence', 'claim', 'investigation', 'method_experience', 'decision'})
        for record in self.memory.read_history():
            self.assertEqual(record['support_check']['status'], 'pending')
            self.assertIsNone(record['validated_scope'])
        invalid = self.update('forged', content={'statement': 'Proven', 'scope': {}, 'validated': True})
        self.assertEqual(self.apply(invalid)['status'], 'rejected')
        self.assertFalse(self.memory._head('forged'))
        self.store.verify_history('r')

    def test_publication_is_idempotent_and_records_negative_experience_without_model(self):
        e, pub = self.publication()
        first = self.memory.observe_publication(e, pub)
        count = len(self.memory.read_history())
        self.assertEqual(first, self.memory.observe_publication(e, pub))
        self.assertEqual(len(self.memory.read_history()), count)
        exp = self.memory.read_history(kind='method_experience')[0]
        self.assertEqual(exp['content']['outcome'], 'negative')
        self.assertEqual(exp['authority'], 'domain_observation')
        self.assertEqual(exp['content']['scope'], {'candidate_ids': ['B']})
        self.assertEqual(self.provider_calls[-1], (self.store, 'r', e['id']))
        self.assertEqual(self.memory.lookup_exact(['B'], {'protocol': 'frozen-v1'})[0]['source_evidence_id'], 'result-1')
        self.assertEqual(self.memory.lookup_exact(['B'], {'protocol': 'another'}), [])
        with self.assertRaises(ValueError):
            self.memory.lookup_exact(['B'], {})

    def test_unknown_effect_or_forged_publication_cannot_become_evidence(self):
        e, pub = self.publication()
        bad = copy.deepcopy(pub); bad['facts'][0]['content']['baseline'] = 999
        with self.assertRaises(ValueError):
            self.memory.observe_publication(e, bad)
        self.tx(lambda tx: tx.update('effect', e['id'], {'accounting_unknown': True}))
        with self.assertRaises(ValueError):
            self.memory.observe_publication(e, pub)
        self.assertEqual(self.memory.read_history(kind='evidence'), [])

    def test_actual_delivery_not_catalog_or_partial_page_grants_source_authority(self):
        self.add_fact('secret', {'result': 'not delivered'})
        self.assertEqual(self.apply(self.update(sources=['secret']))['status'], 'rejected')
        self.assertEqual(self.apply(self.update(), partial=True)['status'], 'rejected')
        e, packet, response = self.model_call([self.update()])
        tampered = copy.deepcopy(response); tampered['scientific_memory_updates'] = '[]'
        with self.assertRaises(ValueError):
            self.memory.apply_updates(e, packet, tampered)
        packet['delivery_pages'][0]['content'] = 'spoof'
        with self.assertRaises(ValueError):
            self.memory.apply_updates(e, packet, response)

    def test_append_only_cas_and_negative_revision_remain_retrievable(self):
        first = self.apply(self.update())
        self.assertEqual(first['status'], 'applied')
        old = self.memory.get('claim-A')
        update = self.update(revision=1, content={'statement': 'A may work in another scope',
            'scope': {'candidate_ids': ['A'], 'condition': 'different'}, 'judgment': 'tentative'})
        self.assertEqual(self.apply(update)['status'], 'applied')
        self.assertEqual(self.memory.get('claim-A', 1), old)
        self.assertEqual(self.apply(update)['status'], 'rejected')
        context = self.memory.prepare_context('lab', ['f1'], query={'object_ids': ['claim-A']}, budget=100000)
        self.assertEqual({r['revision'] for r in context['objects'] if r['id'] == 'claim-A'}, {1, 2})
        self.assertEqual(len(self.memory.read_history('claim-A')), 2)

    def test_updates_replay_and_invalid_batch_do_not_partially_write(self):
        e, p, r = self.model_call([self.update()])
        first = self.memory.apply_updates(e, p, r)
        self.assertEqual(first, self.memory.apply_updates(e, p, r))
        self.assertEqual(len(self.memory.read_history('claim-A')), 1)
        e, p, r = self.model_call([self.update('valid'), self.update('bad', sources=['missing'])])
        self.assertEqual(self.memory.apply_updates(e, p, r)['status'], 'rejected')
        self.assertFalse(self.memory._head('valid'))
        changed_kind = self.update('claim-A', kind='decision', revision=1,
                                   content={'decision': 'Change object kind', 'scope': {}})
        e, p, r = self.model_call([self.update('valid-before-kind-change'), changed_kind])
        self.assertEqual(self.memory.apply_updates(e, p, r)['status'], 'rejected')
        self.assertFalse(self.memory._head('valid-before-kind-change'))
        self.assertEqual(self.memory.get('claim-A')['revision'], 1)
        e, p, r = self.model_call(None, response_override=True)
        self.assertEqual(self.memory.apply_updates(e, p, r)['status'], 'rejected')
        self.store.verify_history('r')

    def test_rejected_update_feedback_is_delivered_only_to_same_role(self):
        self.apply(self.update(sources=['missing']))
        lab = self.memory.prepare_context('lab', ['f1'])
        meta = self.memory.prepare_context('meta', ['f1'])
        self.assertEqual(lab['last_update_result']['status'], 'rejected')
        self.assertIn('not fully delivered', lab['last_update_result']['reason'])
        self.assertIsNone(meta['last_update_result'])

    def test_source_change_and_new_observation_mark_review_without_overwriting_judgment(self):
        self.apply(self.update())
        original = self.memory.get('claim-A')
        self.add_fact('corrected', {'observations': [{'candidate': 'A', 'score': 0.4}]})
        self.memory.mark_source_changed('f1', replacement_id='corrected', reason='Instrument correction')
        self.assertEqual(self.memory._freshness(original), 'needs_review')
        self.assertEqual(self.memory.get('claim-A'), original)
        # A proposal dispatched before correction remains an old-snapshot interpretation.
        updated = self.update(revision=1)
        result = self.apply(updated)
        self.assertEqual(result['objects'][0]['freshness'], 'needs_review')
        e, pub = self.publication()
        self.memory.observe_publication(e, pub)
        self.assertTrue(self.memory._rows('scientific_memory_review_pending'))

    def test_reference_closure_and_raw_first_exclude_model_interpretations(self):
        self.apply(self.update())
        ref = {'id': 'claim-A', 'revision': 1, 'relation': 'challenges'}
        update = self.update('claim-B', refs=[ref])
        self.assertEqual(self.apply(update)['status'], 'applied')
        self.memory.mark_source_changed('f1', reason='Source now disputed')
        self.assertEqual(self.memory._freshness(self.memory.get('claim-B')), 'needs_review')
        context = self.memory.prepare_context('meta', ['f1'], query={'exclude_interpretations': True}, budget=100000)
        self.assertFalse(any(r['authority'] == 'model_interpretation' for r in context['objects']))
        context = self.memory.prepare_context('lab', [], query={'object_ids': ['claim-B']}, budget=100000)
        self.assertIn('f1', context['required_evidence_ids'])
        self.assertTrue(context['needs_review'])

    def test_original_sources_count_toward_bounded_selection_and_omissions_are_explicit(self):
        self.add_fact('large', {'body': 'z' * 20000})
        self.memory.observe_fact('large', projection={})
        context = self.memory.prepare_context('lab', [], budget=4096)
        self.assertLessEqual(len(encode(context)), 4096)
        self.assertNotIn('large', context['required_evidence_ids'])
        self.assertGreater(context['omission_count'], 0)
        context = self.memory.prepare_context('lab', ['large'], budget=8192)
        self.assertIn('large', context['required_evidence_ids'])
        self.assertEqual(context['additional_source_bytes'], 0)

    def test_new_question_and_claim_can_be_committed_together(self):
        question = self.update('subquestion', 'question', sources=[], content={'question': 'Does A generalize?'})
        claim = self.update(); claim['question_ids'] = ['subquestion']
        e, p, r = self.model_call([question, claim])
        self.assertEqual(self.memory.apply_updates(e, p, r)['status'], 'applied')

    def test_read_only_context_is_the_first_real_context_and_registers_nothing(self):
        e, pub = self.publication()
        self.memory.observe_publication(e, pub)
        self.apply(self.update())
        bundle = self.memory.export_bundle()
        # A fresh run, a run with imported history, and one whose budget makes the selection order decisive.
        for imported, budget, omitted in ((False, 100000, False), (True, 100000, False), (True, 1400, True)):
            with self.subTest(imported=imported, budget=budget):
                store, target = self.target()
                if imported:
                    target.import_bundle(bundle)
                before = store.verify_history('next')
                preview = target.prepare_context('lab', [], budget=budget, read_only=True)
                self.assertEqual(store.verify_history('next'), before)
                self.assertEqual(preview['status'], 'ready')
                self.assertEqual(preview['omission_count'] > 0, omitted)
                self.assertEqual(preview, target.prepare_context('lab', [], budget=budget))
                # Once the question is registered the two calls read the same history.
                after = store.verify_history('next')
                self.assertEqual(target.prepare_context('meta', [], budget=budget, read_only=True),
                                 target.prepare_context('meta', [], budget=budget))
                self.assertEqual(store.verify_history('next'), after)

    def test_export_import_preserve_revisions_and_source_scope_exactly(self):
        e, pub = self.publication()
        self.memory.observe_publication(e, pub)
        self.apply(self.update())
        self.memory.mark_source_changed('f1', reason='Review required')
        bundle = self.memory.export_bundle()
        target_store, target = self.target()
        first = target.import_bundle(bundle)
        self.assertEqual(first, target.import_bundle(bundle))
        self.assertEqual(target.read_history(), self.memory.read_history())
        self.assertEqual(target._freshness(target.get('claim-A')), 'needs_review')
        match = target.lookup_exact(['B'], {'protocol': 'frozen-v1'})[0]
        self.assertTrue(match['source_evidence_id'].startswith('sm_import_fact_'))
        self.assertEqual(target_store.list('next', 'effect'), [])
        bad = copy.deepcopy(bundle); bad['data']['records'][0]['content']['forged'] = True
        with self.assertRaises(ValueError):
            target.import_bundle(bad)
        target_store.verify_history('next')

    def test_import_old_receipts_uses_original_provider_context_and_no_new_effects(self):
        effect, publication = self.publication()
        target_store, target = self.target()
        first = target.import_history(self.store, 'r')
        second = target.import_history(self.store, 'r')
        self.assertEqual(first, second)
        self.assertEqual(self.provider_calls[-1], (self.store, 'r', effect['id']))
        self.assertEqual(first['historical_effects'], 1)
        self.assertEqual(target_store.list('next', 'effect'), [])
        exp = target.read_history(kind='method_experience')[0]
        self.assertTrue(exp['content']['origin']['historical'])
        self.assertIsNone(exp['source_effect_id'])
        self.assertEqual(len(target.lookup_exact(['B'], {'protocol': 'frozen-v1'})), 1)
        self.assertEqual(target.review_report()['pending_count'], 0)
        self.assertEqual(target.review_report()['historical_pending_count'], 1)
        target_store.verify_history('next')

    def test_current_result_priority_and_explicit_interpretation_projection(self):
        old, old_pub = self.publication('old-result', 'OLD')
        self.memory.observe_publication(old, old_pub)
        e, pub = self.publication('current-result', 'NEW')
        self.memory.observe_publication(e, pub)
        current_inv = next(r for r in self.memory.read_history(kind='investigation') if r['source_effect_id'] == e['id'])
        original = copy.deepcopy(current_inv)
        context = self.memory.prepare_context('lab', ['current-result'], budget=4500)
        self.assertEqual(context['objects'][0]['id'], current_inv['id'])
        final = self.memory.prepare_context('lab', [], query={'final_review_only': True}, budget=100000)
        self.assertEqual(final['objects'][0]['kind'], 'investigation')
        decision = self.update('review-current', kind='decision', sources=['current-result'],
            refs=[{'id': current_inv['id'], 'revision': 1, 'relation': 'derived_from'}],
            content={'decision': 'Continue elsewhere', 'scope': {'candidate_ids': ['NEW']},
                     'result_interpretation': 'NEW did not improve the measured baseline'})
        self.assertEqual(self.apply(decision, evidence_ids=['current-result'])['status'], 'applied')
        self.assertEqual(self.memory.get(current_inv['id']), original)
        report = self.memory.review_report()
        self.assertEqual(report['interpreted_count'], 1)
        self.assertEqual(report['current_pending_count'], 1)
        self.assertEqual(report['pending'][0]['source_evidence_ids'], ['old-result'])
        target_store, target = self.target()
        target.import_bundle(self.memory.export_bundle())
        self.assertEqual(target.review_report()['interpreted_count'], 1)
        self.assertEqual(target.review_report()['historical_pending_count'], 1)

    def test_explicit_deferral_is_visible_without_forcing_unbounded_final_review(self):
        e, pub = self.publication()
        self.memory.observe_publication(e, pub)
        inv = self.memory.read_history(kind='investigation')[0]
        decision = self.update('defer-result', kind='decision', sources=['result-1'],
            refs=[{'id': inv['id'], 'revision': 1, 'relation': 'context'}],
            content={'decision': 'Defer broader comparison', 'scope': {'candidate_ids': ['B']},
                     'result_interpretation': 'Need a matched control before interpretation', 'result_disposition': 'deferred'})
        self.assertEqual(self.apply(decision, evidence_ids=['result-1'])['status'], 'applied')
        report = self.memory.review_report()
        self.assertEqual(report['pending_count'], 0)
        self.assertEqual(report['interpreted_count'], 0)
        self.assertEqual(report['deferred_count'], 1)
        self.assertEqual(report['deferred'][0]['status'], 'deferred')

    def test_interpretation_uses_current_decision_revision_across_replay_and_import(self):
        e, pub = self.publication()
        self.memory.observe_publication(e, pub)
        inv = self.memory.read_history(kind='investigation')[0]
        refs = [{'id': inv['id'], 'revision': 1, 'relation': 'derived_from'}]
        target_store, target = self.target()
        contents = [
            {'decision': 'Record the result interpretation', 'scope': {},
             'result_interpretation': 'B did not improve the measured baseline'},
            {'decision': 'Withdraw the prior interpretation until controls are available', 'scope': {}},
            {'decision': 'Defer the interpretation', 'scope': {},
             'result_interpretation': 'Matched controls are still missing', 'result_disposition': 'deferred'},
            {'decision': 'Record a revised interpretation', 'scope': {},
             'result_interpretation': 'The result only describes the delivered experimental scope'}]
        expected_states = ['interpreted', 'awaiting_interpretation', 'deferred', 'interpreted']
        for previous_revision, (content, expected) in enumerate(zip(contents, expected_states)):
            with self.subTest(revision=previous_revision + 1):
                update = self.update('review-current', kind='decision', revision=previous_revision,
                    sources=['result-1'], refs=refs, content=content)
                effect, packet, response = self.model_call([update], evidence_ids=['result-1'])
                result = self.memory.apply_updates(effect, packet, response)
                self.assertEqual(result['status'], 'applied')
                self.assertEqual(result, self.memory.apply_updates(effect, packet, response))
                bundle = self.memory.export_bundle()
                imported = target.import_bundle(bundle)
                self.assertEqual(imported, target.import_bundle(bundle))
                for memory in (self.memory, target):
                    state = memory._interpretation(memory.get(inv['id']))
                    self.assertEqual(state['status'], expected)
                    self.assertEqual(state['decision_refs'], [] if expected == 'awaiting_interpretation' else
                                     [{'id': 'review-current', 'revision': previous_revision + 1}])
                    report = memory.review_report(include_historical=True)
                    self.assertEqual(report['pending_count'], int(expected == 'awaiting_interpretation'))
                    self.assertEqual(report['deferred_count'], int(expected == 'deferred'))
                    self.assertEqual(report['interpreted_count'], int(expected == 'interpreted'))
                    context = memory.prepare_context('lab', [], query={'object_ids': [inv['id']]}, budget=100000)
                    card = next(r for r in context['objects'] if r['id'] == inv['id'])
                    self.assertEqual(card['interpretation']['status'], expected)
        # Superseded index rows remain available for audit, including the
        # original interpretation and the later deferral.
        for memory in (self.memory, target):
            rows = memory._rows('scientific_memory_interpretation')
            self.assertEqual(len(rows), 3)
            self.assertEqual({r['decision_ref']['revision'] for r in rows}, {1, 3, 4})
        self.store.verify_history('r')
        target_store.verify_history('next')

    def test_interpretation_reopens_when_its_additional_source_needs_review(self):
        e, pub = self.publication()
        self.memory.observe_publication(e, pub)
        inv = self.memory.read_history(kind='investigation')[0]
        decision = self.update('review-with-control', kind='decision', sources=['result-1', 'f1'],
            refs=[{'id': inv['id'], 'revision': 1, 'relation': 'derived_from'}],
            content={'decision': 'Interpret against the supplied control', 'scope': {},
                     'result_interpretation': 'The control supports this bounded comparison'})
        self.assertEqual(self.apply(decision, evidence_ids=['result-1', 'f1'])['status'], 'applied')
        self.assertEqual(self.memory.review_report()['interpreted_count'], 1)
        self.memory.mark_source_changed('f1', reason='The additional control requires correction')
        # The investigation's own result is unchanged; the decision dependency
        # must still reopen its review instead of permanently closing it.
        self.assertEqual(self.memory._freshness(inv), 'current')
        self.assertEqual(self.memory._freshness(self.memory.get('review-with-control')), 'needs_review')
        report = self.memory.review_report()
        self.assertEqual(report['interpreted_count'], 0)
        self.assertEqual(report['pending_count'], 1)
        self.assertEqual(report['pending'][0]['id'], inv['id'])
        self.assertEqual(len(self.memory._rows('scientific_memory_interpretation')), 1)

    def test_interpretation_requires_current_and_available_dependency_closure(self):
        self.assertEqual(self.apply(self.update('control-claim'))['status'], 'applied')
        e, pub = self.publication()
        self.memory.observe_publication(e, pub)
        inv = self.memory.read_history(kind='investigation')[0]
        # Incorporate the newly observed result so this claim is initially
        # current, then use it as a decision's separate dependency.
        claim = self.update('control-claim', revision=1, sources=['f1', 'result-1'])
        self.assertEqual(self.apply(claim, evidence_ids=['f1', 'result-1'])['status'], 'applied')
        decision = self.update('review-with-claim', kind='decision', sources=['result-1'],
            refs=[{'id': inv['id'], 'revision': 1, 'relation': 'derived_from'},
                  {'id': 'control-claim', 'revision': 2, 'relation': 'context'}],
            content={'decision': 'Interpret using the supplied claim', 'scope': {},
                     'result_interpretation': 'This decision depends on the recorded comparison'})
        self.assertEqual(self.apply(decision, evidence_ids=['result-1', 'f1'])['status'], 'applied')
        self.assertEqual(self.memory.review_report()['interpreted_count'], 1)
        missing = self.store.get('r', 'fact', 'f1')['content_ref']
        read_blob = self.store.read_blob
        def unavailable(key):
            if key == missing:
                raise FileNotFoundError('Synthetic unavailable dependency source')
            return read_blob(key)
        with patch.object(self.store, 'read_blob', side_effect=unavailable):
            self.assertEqual(self.memory.review_report()['interpreted_count'], 0)
            self.assertEqual(self.memory.review_report()['pending_count'], 1)
        self.assertEqual(self.memory.review_report()['interpreted_count'], 1)
        revised = self.update('control-claim', revision=2, sources=['f1', 'result-1'],
            content={'statement': 'The prior comparison needs a different scope', 'scope': {}, 'judgment': 'tentative'})
        self.assertEqual(self.apply(revised, evidence_ids=['result-1', 'f1'])['status'], 'applied')
        self.assertEqual(self.memory._freshness(inv), 'current')
        self.assertEqual(self.memory.review_report()['interpreted_count'], 0)
        self.assertEqual(self.memory.review_report()['pending_count'], 1)


if __name__ == '__main__':
    unittest.main()
