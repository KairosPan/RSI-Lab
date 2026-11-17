"""Real registered adapters through the ledger; models and remote APIs are fixtures."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from rsi_literature import make_adapters
from rsi_literature.adapters import OPERATIONS
from rsi_literature.documents import DocumentStore
from rsi_literature.catalog import PROVIDERS


TEXT = 'The assay used a matched control.\n\nThe reported outcome was a small response.'


def response(data=None, status='ok'):
    return {'status': status, 'data': data or {}, 'provenance': {'attempt_count': 1},
        'coverage': {'fixture': True}, 'continuation': None, 'errors': []}


class AdapterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'paper.txt'
        self.source.write_text(TEXT)
        self.config = {'enabled_providers': list(PROVIDERS),
            'local_sources': {'paper': {'path': str(self.source), 'content_scope': 'excerpt',
                'source_url': 'https://repository.example.org/official-paper',
                'paper_ref': 'DOI:10.1000/fixture', 'license': 'CC-BY-4.0'}}}
        self.document_root = self.root / 'literature'
        self.adapters = make_adapters(self.document_root, self.config)
        self.store = Store(self.root / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('integration', budgets={
            'tool_calls': {'limit': 100, 'hard': True}, 'model_calls': {'limit': 20, 'hard': True}},
            config={'goal': 'Find the assay control with traceable original evidence', 'max_input_bytes': 131072})
        self.runtime = Runtime(self.store, 'integration', adapters=self.adapters)
        self.sequence = 0

    def call(self, operation, inputs):
        self.sequence += 1
        return self.runtime.execute_adapter('literature.' + operation, inputs,
                                            trigger_id='integration-' + str(self.sequence))

    def acquire(self):
        acquired = self.call('acquire_fulltext', {'source_id': 'paper'})
        self.assertEqual(acquired['result']['status'], 'ok', acquired)
        return acquired['result']['data']['document_id']

    def ready(self, kind):
        return next(w for w in self.controller.ready('integration') if w['kind'] == kind)

    def emit(self, work, action, payload, evidence_ids=()):
        mode = 'literature' if work['kind'] == 'literature' else 'lab'
        command = {'mode': mode, 'action': action, 'payload': payload, 'issue_id': None,
            'expected_versions': self.runtime.versions(None), 'evidence_ids': list(evidence_ids)}
        if mode == 'literature':
            task = self.store.list('integration', 'registration', kind='literature_task')[0]
            command.update(task_id=task['id'], task_revision=task['task_revision'])
        memory = {}
        if mode == 'lab':
            memory['rationale_summaries'] = [{
                'kind': 'literature_decision',
                'decision': 'delegate' if action == 'request_literature' else 'not_needed',
                'reason': 'Scripted adapter fixture; this does not test autonomous judgment.',
                'evidence_ids': list(evidence_ids), 'related_task_ids': []}]
        def executor(effect, packet):
            envelope = json.loads(packet)
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1},
                'response': {'command': command, 'working_memory_delta': json.dumps(memory)},
                'sent_packet_sha256': digest(packet), 'sent_prompt_sha256': digest(envelope['prompt'].encode())}
        return self.runtime.run_meta(work['id'], executor, {'model_calls': 1})

    def test_local_acquire_read_find_package_and_publication_recovery(self):
        doc = self.acquire()
        metadata = DocumentStore(self.document_root).manifest(doc)['source_metadata']
        self.assertEqual(metadata['provider'], 'local')
        self.assertEqual(metadata['source_url'], 'https://repository.example.org/official-paper')
        self.assertEqual(metadata['paper_ref'], 'DOI:10.1000/fixture')
        self.assertEqual(metadata['license'], 'CC-BY-4.0')
        observation = self.store.list('integration', 'fact')[0]
        content = self.store.read_blob(observation['content_ref']).decode()
        self.assertNotIn('The assay used', content, 'Acquisition must not implicitly deliver the original text')
        read = self.call('read_document', {'document_id': doc, 'max_bytes': 20})
        self.assertEqual(read['result']['status'], 'partial')
        self.assertEqual(read['result']['data']['text'], TEXT[:20])
        passages = [f for f in self.store.list('integration', 'fact')
                    if f['provenance'].get('delivery_kind') == 'source_excerpt']
        self.assertTrue(passages)
        self.assertEqual(json.loads(self.store.read_blob(passages[0]['content_ref']))['char_range'], [0, 20])
        package = self.call('build_reading_package', {'document_id': doc})
        self.assertFalse(package['result']['data']['source_text_delivered'])
        with patch.object(self.runtime, 'publish_adapter_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.call('find_passages', {'document_ids': [doc], 'query': 'matched control', 'max_bytes': 128})
        before = len(self.store.list('integration', 'effect'))
        resumed = Runtime(self.store, 'integration', adapters=make_adapters(self.document_root, self.config))
        with patch.object(resumed.controller, 'dispatch_effect', side_effect=AssertionError('Recovery may not reexecute')):
            resumed.recover()
            resumed.recover()
        self.assertEqual(len(self.store.list('integration', 'effect')), before)
        self.assertEqual(resumed.pending_adapter_publications(), [])
        self.assertEqual(len(self.store.list('integration', 'registration', kind='adapter_publication', status='complete')), 4)
        self.store.verify_history('integration')

    def test_remote_format_preference_is_ledgered_and_invalid_formats_create_no_effect(self):
        candidates = [{'format': 'pdf', 'url': 'https://example.org/paper.pdf'},
                      {'format': 'xml', 'url': 'https://example.org/paper.xml'}]
        raw = b'<article><body><p>Full structured source.</p></body></article>'
        with patch('rsi_literature.adapters.providers.ProviderClient') as client, patch(
                'rsi_literature.adapters._download', return_value=(raw, 'application/xml', 1, None)) as download:
            client.return_value.fulltext_candidates.return_value = response({'paper': {}, 'candidates': candidates})
            result = self.call('acquire_fulltext', {'paper_ref': 'PMCID:PMC1', 'preferred_format': 'xml'})
            self.assertEqual(result['result']['status'], 'ok', result)
            self.assertEqual(result['result']['provenance']['attempt_count'], 2)
            self.assertEqual(download.call_args.args[0], candidates[1])
            before = len(self.store.list('integration', 'effect'))
            for inputs in ({'paper_ref': 'PMCID:PMC1', 'preferred_format': 'html'},
                           {'source_id': 'paper', 'preferred_format': 'pdf'}):
                with self.assertRaises(ValueError):
                    self.call('acquire_fulltext', inputs)
            self.assertEqual(len(self.store.list('integration', 'effect')), before)
            self.assertEqual(download.call_count, 1)
        self.store.verify_history('integration')

    def test_full_capability_catalog_and_schema_belong_to_child_and_fit_its_packet(self):
        self.assertEqual(len(self.adapters), 13)
        self.assertEqual(set(self.runtime.capability_catalog()), {'literature.' + name for name in OPERATIONS})
        for adapter in self.adapters:
            contract = adapter.manifest['input_contract']
            self.assertTrue(set(contract['required']).issubset(contract['allowed']))
            schema = adapter.manifest['input_schema']
            self.assertEqual(schema['type'], 'object')
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(set(schema['properties']), set(contract['allowed']))
            self.assertEqual(set(schema['required']), set(contract['required']))
            with self.assertRaises(ValueError):
                adapter.prepare({'command': 'unregistered execution'})
        citation_adapter = next(a for a in self.adapters if a.adapter_id == 'literature.get_citations')
        self.assertEqual(citation_adapter.manifest['input_schema']['properties']['direction']['enum'], ['references', 'citing'])
        with self.assertRaises(ValueError):
            citation_adapter.prepare({'paper_ref': 'PMID:12345', 'direction': 'forward'})
        lab = self.runtime.enqueue_lab('catalog-check')
        packet = self.runtime.prepare_meta(lab['id'], {'model_calls': 1})
        self.assertEqual(packet['status'], 'prepared', packet)
        plan = json.loads(self.store.read_blob(packet['metadata']['plan_ref']))
        self.assertEqual(plan['task']['capability_catalog'], {})
        self.assertEqual(plan['task']['literature_context']['available_tool_count'], 13)
        self.assertLessEqual(plan['input_bytes'], 131072)
        delegated = self.emit(lab, 'request_literature', {
            'question': 'Compare the relevant methods and their evidence.',
            'intended_use': 'Plan a follow-up experiment', 'scope': 'Registered source collection',
            'deliverables': ['Source-backed comparison and coverage limits'],
            'max_model_calls': 4, 'max_tool_calls': 8})
        self.assertEqual(delegated['proposal']['status'], 'applied', delegated)
        child_packet = self.runtime.prepare_meta(self.ready('literature')['id'], {'model_calls': 1})
        self.assertEqual(child_packet['status'], 'prepared', child_packet)
        child_plan = json.loads(self.store.read_blob(child_packet['metadata']['plan_ref']))
        self.assertEqual(set(child_plan['task']['capability_catalog']), {'literature.' + name for name in OPERATIONS})
        self.assertLessEqual(child_plan['input_bytes'], 131072)

    def test_lab_child_actual_tool_sources_return_to_same_lab_owner(self):
        doc = self.acquire()
        seeds = [f['id'] for f in self.store.list('integration', 'fact')]
        lab = self.controller.enqueue('integration', 'lab', 'named-parent', 'named-parent-work',
            inputs={'agent_instance_id': 'lab-control-review', 'branch_id': 'research-branch',
                    'evidence_ids': seeds, 'required_evidence_ids': seeds})
        delegate = self.emit(lab, 'request_literature', {'question': 'Which assay control is reported?',
            'intended_use': 'Plan a replication', 'scope': doc, 'deliverables': ['Control with original text'],
            'max_model_calls': 5, 'max_tool_calls': 8}, seeds)
        self.assertEqual(delegate['proposal']['status'], 'applied', delegate)
        child = self.ready('literature')
        request = self.emit(child, 'request_diagnostic', {'capability_id': 'literature.read_document',
            'capability_version': '1.0.0', 'inputs_json': json.dumps({'document_id': doc, 'max_bytes': 38})}, seeds)
        self.assertEqual(request['proposal']['status'], 'applied', request)
        tool = self.ready('diagnostic')
        result = self.runtime.execute_adapter(tool['inputs']['capability_id'], json.loads(tool['inputs']['inputs_json']),
            trigger_id=tool['id'], work_id=tool['id'])
        self.assertEqual(result['effect']['status'], 'succeeded')
        child = self.ready('literature')
        original_ids = [fid for fid in child['inputs']['evidence_ids']
            if self.store.get('integration', 'fact', fid)['provenance'].get('delivery_kind') == 'source_excerpt']
        self.assertTrue(original_ids)
        completion = self.emit(child, 'complete_research', {'answer': 'The assay used a matched control.',
            'coverage_status': 'partial', 'stop_reason': 'Requested source passage inspected',
            'limitations': ['Only the selected passage was read'], 'claims': [{
                'statement': 'The authors report using a matched control.', 'evidence_ids': original_ids,
                'limitations': ['Reported result, not independently replicated']}]}, original_ids)
        self.assertEqual(completion['proposal']['status'], 'applied', completion)
        parent = self.ready('lab')
        self.assertEqual(parent['inputs']['agent_instance_id'], 'lab-control-review')
        self.assertEqual(parent['inputs']['branch_id'], 'research-branch')
        effect = self.runtime.prepare_meta(parent['id'], {'model_calls': 1})
        packet = json.loads(self.store.read_blob(effect['metadata']['plan_ref']))
        self.assertTrue(set(original_ids).issubset({page['evidence_id'] for page in packet['delivery_pages']}))
        self.assertEqual(self.store.list('integration', 'registration', kind='literature_task')[0]['status'], 'completed')

    def test_unconfigured_service_produces_publishable_status_without_pending_publication(self):
        with patch.dict(os.environ, {}, clear=True), patch('rsi_literature.remote.http_request',
                side_effect=AssertionError('Missing credentials must not dispatch')):
            result = self.call('run_review', {'provider': 'edison', 'query': 'Which assay control?'})
        self.assertEqual(result['effect']['status'], 'succeeded', result)
        self.assertEqual(result['result']['status'], 'unavailable', result)
        self.assertTrue(result['result']['errors'])
        self.assertEqual(self.runtime.pending_adapter_publications(), [])
        self.runtime.recover()
        self.assertEqual(self.runtime.pending_adapter_publications(), [])

    def test_sdk_root_route_strips_transport_fields_and_preserves_exact_inputs(self):
        doc = self.acquire()
        cases = [
            ('extract_evidence', {'provider': 'langextract', 'document_ids': [doc], 'prompt': 'Extract controls',
                'examples': [], 'cursors': {}}, {'document_ids': [doc], 'prompt': 'Extract controls', 'examples': [], 'cursors': {}}),
            ('run_review', {'provider': 'paperqa', 'action': 'start', 'query': 'Which control?', 'document_ids': [doc]},
                {'query': 'Which control?', 'document_ids': [doc]}),
        ]
        for operation, inputs, expected in cases:
            with self.subTest(provider=inputs['provider']), patch('rsi_literature.worker.run_sdk_worker', return_value=response()) as worker:
                result = self.call(operation, inputs)
                self.assertEqual(result['result']['status'], 'ok', result)
                self.assertEqual(worker.call_args.args[0], inputs['provider'])
                self.assertEqual(worker.call_args.args[2], expected)
        with patch('rsi_literature.worker.run_sdk_worker', side_effect=AssertionError('Status must not rerun local SDK')):
            result = self.call('run_review', {'provider': 'paperqa', 'action': 'status', 'task_id': 'existing'})
        self.assertEqual(result['result']['status'], 'unavailable')

    def test_invalid_provider_or_action_types_are_rejected_before_an_effect_exists(self):
        cases = [
            ('provider_tools', {'provider': 'local'}),
            ('run_review', {'provider': 'edison', 'action': ['start'], 'query': 'Which control?'}),
            ('get_citations', {'provider': 'europe_pmc', 'paper_ref': 'PMID:12345', 'direction': ['citing']}),
            ('provider_tools', {'provider': ['asta']}),
            ('extract_evidence', {'provider': 'paperqa', 'document_ids': ['doc_id'], 'prompt': 'Extract controls'}),
            ('search_papers', {'provider': 'paperqa', 'query': 'assay controls'}),
        ]
        for operation, inputs in cases:
            with self.subTest(operation=operation, inputs=inputs):
                with self.assertRaises(ValueError):
                    self.call(operation, inputs)
                self.assertEqual(self.store.list('integration', 'effect'), [])
                self.assertEqual(self.store.get('integration', 'budget', 'tool_calls')['reserved'], 0)

    def test_hosted_service_routes_do_not_silently_drop_task_identity(self):
        cases = [
            ('search_papers', {'provider': 'elicit', 'query': 'control assay', 'limit': 3},
                {'action': 'search', 'query': 'control assay', 'limit': 3}),
            ('run_review', {'provider': 'elicit', 'query': 'Review controls', 'parameters': {'max_search_papers': 10, 'max_extract_papers': 3}},
                {'action': 'start', 'query': 'Review controls', 'max_search_papers': 10, 'max_extract_papers': 3}),
            ('run_review', {'provider': 'elicit', 'action': 'status', 'task_id': 'session-123'},
                {'action': 'status', 'session_id': 'session-123'}),
            ('run_review', {'provider': 'edison', 'query': 'Review controls', 'parameters': {'mode': 'literature'}},
                {'action': 'start', 'query': 'Review controls', 'mode': 'literature'}),
            ('run_review', {'provider': 'edison', 'action': 'result', 'task_id': 'task-123'},
                {'action': 'result', 'task_id': 'task-123'}),
        ]
        for operation, inputs, expected in cases:
            with self.subTest(inputs=inputs), patch('rsi_literature.services.run_service', return_value=response({'task_id': 'task-123'})) as service:
                result = self.call(operation, inputs)
                self.assertEqual(result['result']['status'], 'ok', result)
                self.assertEqual(service.call_args.args[2], expected)

    def test_forward_citations_map_to_provider_cited_by_contract(self):
        with patch('rsi_literature.providers.ProviderClient.get_citations', return_value=response({'papers': []})) as citations:
            result = self.call('get_citations', {'provider': 'europe_pmc', 'paper_ref': 'PMID:12345',
                                                'direction': 'citing', 'limit': 4})
        self.assertEqual(result['result']['status'], 'ok')
        self.assertEqual(citations.call_args.args, ('europe_pmc', 'PMID:12345', 'cited_by'))
        self.assertEqual(citations.call_args.kwargs, {'cursor': None, 'limit': 4})

    def test_sdk_unknown_result_retains_effect_reconciliation_without_source_publication(self):
        unknown = response(status='failed')
        unknown['execution_unknown'] = True
        with patch('rsi_literature.worker.run_sdk_worker', return_value=unknown):
            result = self.call('run_review', {'provider': 'paperqa', 'query': 'Which control?', 'document_ids': ['doc-placeholder']})
        self.assertEqual(result['effect']['status'], 'unknown', result)
        self.assertIsNone(result['result'])
        self.assertEqual(self.store.list('integration', 'fact'), [])
        with patch('rsi_literature.worker.run_sdk_worker', side_effect=AssertionError('Unknown must not be resent')):
            self.runtime.recover()
        self.assertEqual(self.store.get('integration', 'effect', result['effect']['id'])['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
