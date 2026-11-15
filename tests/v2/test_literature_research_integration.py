"""Offline integration of Literature, shared science and Meta/Lab feedback.

Scripted decisions exercise the real packet/router/adapter publication chain;
no external model, paper provider, or molecular evaluator is invoked.
"""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

from rsi_lab import images, input_view
from rsi_lab.adapters import Adapter
from rsi_lab.controller import Controller, identity
from rsi_lab.runtime import Runtime
from rsi_lab.literature_context import LITERATURE_DECISION_POLICY_VERSION
from rsi_lab.store import Store, digest
from tests.v2.test_image_evidence import content, png


def prepare(inputs):
    return dict(inputs)


def reservations(packet):
    return {'tool_calls': 1}


def execute(effect, raw):
    return {'status': 'succeeded', 'actual_costs': {'tool_calls': 1},
            'result': {'image': content(), 'caption': 'Offline renderer fixture; no scientific claim.'}}


def publication(effect, receipt):
    return {'facts': [
        {'id': identity('figure', effect['id']), 'kind': 'source_statement',
         'content': receipt['result']['image'],
         'provenance': {'effect_id': effect['id'], 'delivery_kind': 'source_image'}},
        {'id': identity('caption', effect['id']), 'kind': 'source_statement',
         'content': {'caption': receipt['result']['caption']},
         'provenance': {'effect_id': effect['id'], 'delivery_kind': 'source_excerpt'}}],
        'conclusions': [], 'data': {'fixture': True}}


class LiteratureResearchIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('r', budgets={'model_calls': {'limit': 12, 'hard': True},
            'tool_calls': {'limit': 12, 'hard': True}}, config={'execution_mode': 'replay',
                'research_features': {'scientific_memory': True, 'meta_lab': True},
                'research_policy': {'recommendation_kinds': ['fixture_review'],
                                    'recommendation_guidance': 'Review actual executed fixture results.'},
                'max_input_bytes': 500000})
        self.adapter = Adapter('literature.render_document_page', '1.0', 'literature_render_document_page',
            {'scope': 'Render an offline registered page fixture',
             'literature_decision_policy': LITERATURE_DECISION_POLICY_VERSION},
            prepare, reservations, execute, publication)
        self.runtime = Runtime(self.store, 'r', adapters=[self.adapter])
        self.controller.add_fact('r', 'protocol', 'source_statement', {'question': 'Inspect the registered page'},
            {'source': 'offline fixture'}, 'protocol')

    def emit(self, work, action, payload):
        def fixture_model(effect, raw):
            envelope = json.loads(raw)
            data = input_view.decode(envelope['prompt'].rsplit('INPUT_DATA_JSON:\n', 1)[1],
                                     envelope['input_view_mode'])
            task, mode = data['task'], envelope['mode']
            refs = [page['evidence_id'] for page in data['delivered_pages']]
            command = {'mode': mode, 'action': action, 'issue_id': task.get('issue_id'),
                'expected_versions': task['expected_versions'], 'evidence_ids': refs, 'payload': payload}
            memory = {}
            if mode == 'lab':
                memory['rationale_summaries'] = [{'kind': 'literature_decision', 'decision': 'delegate',
                    'reason': 'Offline integration fixture delegates a bounded page inspection.',
                    'evidence_ids': refs, 'related_task_ids': []}]
            else:
                delegated = task['literature_task']
                command.update(task_id=delegated['id'], task_revision=delegated['task_revision'])
            response = {'command': command, 'working_memory_delta': json.dumps(memory)}
            if 'scientific_memory' in task:
                response['scientific_memory_updates'] = '[]'
            if 'research_loop' in task['constraints']:
                response['recommendation_responses' if mode == 'lab' else 'improvement_reviews'] = []
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1}, 'response': response,
                    'sent_packet_sha256': digest(raw), 'sent_prompt_sha256': digest(envelope['prompt'].encode())}
        outcome = self.runtime.run_meta(work['id'], fixture_model, {'model_calls': 1})
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        return outcome

    def render_through_child(self):
        lab = self.runtime.enqueue_lab('start', ['protocol'])
        _, _, task, _ = self.runtime._context(lab)
        self.assertIn('scientific_memory', task)
        self.assertIn('research_loop', task['constraints'])
        self.assertIn('research_policy', task['constraints'])
        self.assertIn('literature_context', task)
        self.emit(lab, 'request_literature', {'question': 'Inspect the page relevant to the supplied question',
            'intended_use': 'Read the supplied visual source', 'scope': 'The registered fixture page',
            'deliverables': ['A source-backed observation'], 'capability_ids': [self.adapter.adapter_id],
            'max_model_calls': 3, 'max_tool_calls': 2})
        child = next(work for work in self.controller.ready('r') if work['kind'] == 'literature')
        agent, mode, child_task, options = self.runtime._context(child)
        self.assertNotIn('scientific_memory', child_task)
        for field in ('research_features', 'research_policy', 'research_loop', 'final_review_only'):
            self.assertNotIn(field, child_task['constraints'])
        child_plan = agent.prepare(mode, child_task, **options)
        self.assertEqual(set(child_plan['envelope']['response_schema']['properties']),
                         {'command', 'working_memory_delta'})
        decision = self.emit(child, 'request_diagnostic', {'capability_id': self.adapter.adapter_id,
            'capability_version': self.adapter.version, 'inputs_json': '{"page":2}'})
        for field in ('research_features', 'research_policy', 'final_review_only'):
            self.assertNotIn(field, decision['effect']['metadata'])
        self.assertEqual(self.store.list('r', 'registration', kind='scientific_memory_update_result', role='literature'), [])
        diagnostic = next(work for work in self.controller.ready('r') if work['kind'] == 'diagnostic')
        outcome = self.runtime.execute_adapter(self.adapter.adapter_id, {'page': 2},
                                               trigger_id=diagnostic['id'], work_id=diagnostic['id'])
        self.assertEqual(outcome['effect']['status'], 'succeeded')
        effect = outcome['effect']
        published = self.store.get('r', 'registration', identity('publication', effect['id']))
        return effect, json.loads(self.store.read_blob(published['publication_ref']))

    def test_child_image_publication_keeps_original_bytes_and_indexes_actual_reference(self):
        effect, published = self.render_through_child()
        image_id = identity('figure', effect['id'])
        receipt = json.loads(self.store.read_blob(effect['result_ref']))
        self.assertEqual(receipt['result']['image'], content())
        self.assertEqual(published['facts'][0]['content'], content())
        fact = self.store.get('r', 'fact', image_id)
        actual = json.loads(self.store.read_blob(fact['content_ref']))
        self.assertEqual(actual, images.image_reference(content()))
        self.assertEqual(self.store.read_blob(actual['blob_ref']), png())
        records = self.runtime.scientific_memory.read_history(kind='evidence')
        self.assertTrue(any(any(ref['fact_id'] == image_id and ref['content_sha256'] == fact['content_ref']
                                for ref in record['source_refs']) for record in records))
        child = next(work for work in self.controller.ready('r') if work['kind'] == 'literature')
        agent, mode, task, options = self.runtime._context(child)
        plan = agent.prepare(mode, task, **options)
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['envelope']['image_attachments'][0]['sha256'], digest(png()))
        # The parent's source text must retain the actual fact hash as required
        # by ScientificMemory.apply_updates, alongside its separate PNG input.
        parent = self.runtime.enqueue_lab('parent-source-contract', [image_id])
        agent, mode, task, options = self.runtime._context(parent)
        parent_plan = agent.prepare(mode, task, **options)
        page = next(row for row in parent_plan['delivery_pages'] if row['evidence_id'] == image_id)
        self.assertEqual(page['source_sha256'], fact['content_ref'])
        self.assertEqual(digest(page['content'].encode()), fact['content_ref'])
        self.assertEqual(parent_plan['envelope']['image_attachments'][0]['sha256'], digest(png()))
        # Exercise the actual ScientificMemory update guard with an exact,
        # locally recorded fixture receipt; this is not a real model invocation.
        response = {'scientific_memory_updates': '[]'}
        def record_fixture(tx):
            packet_ref = tx.blob(parent_plan['envelope_bytes'])
            receipt_ref = tx.blob({'status': 'succeeded', 'protocol_valid': True, 'response': response,
                'sent_packet_sha256': packet_ref,
                'sent_prompt_sha256': digest(parent_plan['envelope']['prompt'].encode())})
            return tx.insert('effect', 'image-memory-update-fixture', {'kind': 'lab', 'status': 'succeeded',
                'accounting_unknown': False, 'packet_ref': packet_ref, 'result_ref': receipt_ref,
                'receipt_ref': receipt_ref, 'metadata': {'plan_ref': tx.blob({
                    key: value for key, value in parent_plan.items() if key != 'envelope_bytes'})}})
        model_fixture = self.store.mutate('r', 'image-memory-update-fixture', 'offline_receipt_fixture', record_fixture)
        updated = self.runtime.scientific_memory.apply_updates(model_fixture, parent_plan, response)
        self.assertEqual(updated['status'], 'unchanged', updated)
        before = self.store.verify_history('r')
        self.runtime.publish_adapter_effect(effect)
        self.assertEqual(self.store.verify_history('r'), before)

    def test_changed_png_blob_fails_replay_without_creating_scientific_records(self):
        effect, published = self.render_through_child()
        blob = self.store.blobs / digest(png())
        blob.chmod(0o644)
        blob.write_bytes(b'tampered PNG bytes')
        before = len(self.runtime.scientific_memory.read_history())
        with self.assertRaisesRegex(ValueError, 'Blob content changed'):
            self.runtime.scientific_memory.observe_publication(effect, published)
        self.assertEqual(len(self.runtime.scientific_memory.read_history()), before)

    def test_original_publication_contract_rejects_image_and_text_changes(self):
        effect, published = self.render_through_child()
        for index in (0, 1):
            forged = copy.deepcopy(published)
            if index == 0:
                forged['facts'][index]['content']['page'] = 3
            else:
                forged['facts'][index]['content']['caption'] = 'A forged ordinary source'
            before = self.store.verify_history('r')
            with self.assertRaisesRegex(ValueError, 'registered adapter publication'):
                self.runtime.scientific_memory.observe_publication(effect, forged)
            self.assertEqual(self.store.verify_history('r'), before)


    def target(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / 'target')
        self.addCleanup(store.close)
        Controller(store).initialize('next', budgets={'model_calls': {'limit': 2, 'hard': True}},
            config={'research_features': {'scientific_memory': True}, 'max_input_bytes': 500000})
        return Runtime(store, 'next')

    def assert_imported_image_context(self, target):
        found = []
        for fact in target.store.list('next', 'fact'):
            value = json.loads(target.store.read_blob(fact['content_ref']))
            if isinstance(value, dict) and value.get('format') == 'rsi_image_evidence_ref_v1':
                found.append((fact, value))
        self.assertEqual(len(found), 1)
        fact, reference = found[0]
        self.assertEqual(reference, images.image_reference(content()))
        self.assertEqual(target.store.read_blob(reference['blob_ref']), png())
        work = target.enqueue_lab('inspect-transferred-source', [fact['id']])
        agent, mode, task, options = target._context(work)
        plan = agent.prepare(mode, task, **options)
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['envelope']['image_attachments'][0]['sha256'], digest(png()))
        self.assertEqual(target.store.list('next', 'effect'), [])
        target.store.verify_history('next')

    def test_bundle_transfers_png_and_builds_runtime_image_context(self):
        self.render_through_child()
        source = self.runtime.scientific_memory
        bundle = source.export_bundle()
        self.assertEqual(set(bundle['data']['image_blobs']), {digest(png())})
        self.assertEqual(base64.b64decode(bundle['data']['image_blobs'][digest(png())]), png())
        target = self.target()
        imported = target.scientific_memory.import_bundle(bundle)
        self.assertEqual(imported, target.scientific_memory.import_bundle(bundle))
        self.assertEqual(target.scientific_memory.read_history(), source.read_history())
        # A repeated trusted transfer can restore an absent immutable PNG file.
        (target.store.blobs / digest(png())).unlink()
        self.assertEqual(imported, target.scientific_memory.import_bundle(bundle))
        self.assert_imported_image_context(target)
        onward = self.target()
        onward.scientific_memory.import_bundle(target.scientific_memory.export_bundle())
        self.assert_imported_image_context(onward)

    def test_image_bundle_rejects_missing_tampered_or_unreferenced_png_before_fact_import(self):
        self.render_through_child()
        bundle = self.runtime.scientific_memory.export_bundle()
        for mutation in ('missing', 'tampered', 'extra'):
            forged = copy.deepcopy(bundle)
            if mutation == 'missing':
                forged['data'].pop('image_blobs')
            elif mutation == 'tampered':
                forged['data']['image_blobs'][digest(png())] = base64.b64encode(png(red=0)).decode()
            else:
                forged['data']['image_blobs'][digest(png(red=0))] = base64.b64encode(png(red=0)).decode()
            forged['sha256'] = digest(forged['data'])
            target = self.target()
            before = target.store.verify_history('next')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                target.scientific_memory.import_bundle(forged)
            self.assertEqual(target.store.verify_history('next'), before)
            self.assertEqual(target.store.list('next', 'fact'), [])
            self.assertFalse((target.store.blobs / digest(png())).exists())

    def test_export_rejects_missing_or_changed_source_png(self):
        self.render_through_child()
        blob = self.store.blobs / digest(png())
        blob.unlink()
        with self.assertRaises(FileNotFoundError):
            self.runtime.scientific_memory.export_bundle()
        self.store.put_blob(png())
        blob.chmod(0o644)
        blob.write_bytes(b'changed source PNG')
        with self.assertRaisesRegex(ValueError, 'Blob content changed'):
            self.runtime.scientific_memory.export_bundle()

    def test_historical_image_publication_imports_png_without_new_effects(self):
        effect, original = self.render_through_child()
        receipt = self.store.read_blob(effect['result_ref'])
        target = self.target()
        first = target.scientific_memory.import_history(self.store, 'r', [effect['id']])
        self.assertEqual(first['historical_effects'], 1)
        self.assertEqual(first, target.scientific_memory.import_history(self.store, 'r', [effect['id']]))
        self.assertEqual(self.store.read_blob(effect['result_ref']), receipt)
        registered = self.store.get('r', 'registration', identity('publication', effect['id']))
        self.assertEqual(json.loads(self.store.read_blob(registered['publication_ref'])), original)
        investigation = target.scientific_memory.read_history(kind='investigation')[0]
        self.assertTrue(investigation['content']['origin']['historical'])
        self.assertIsNone(investigation['source_effect_id'])
        self.assert_imported_image_context(target)

    def test_historical_image_import_rejects_missing_or_changed_png_without_target_writes(self):
        effect, _ = self.render_through_child()
        blob = self.store.blobs / digest(png())
        blob.unlink()
        for missing in (True, False):
            if not missing:
                self.store.put_blob(png())
                blob.chmod(0o644)
                blob.write_bytes(b'changed historical source PNG')
            target = self.target()
            before = target.store.verify_history('next')
            with self.subTest(missing=missing), self.assertRaises((FileNotFoundError, ValueError)):
                target.scientific_memory.import_history(self.store, 'r', [effect['id']])
            self.assertEqual(target.store.verify_history('next'), before)
            self.assertEqual(target.store.list('next', 'fact'), [])


if __name__ == '__main__':
    unittest.main()
