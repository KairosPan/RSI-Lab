"""Offline original-evidence lifecycle through real local literature adapters.

The papers below are synthetic JATS documents. Scripted model responses exercise
the actual packet, ledger, role validation and parent/child scheduler; no model,
network provider or scientific scorer is called.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

from rsi_lab.controller import Controller
from rsi_lab.roles import validate_command
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store, digest
from rsi_literature import make_adapters
from rsi_literature.documents import DocumentStore


def paper_xml(name, paragraphs):
    body = ''.join('<p>' + escape(text) + '</p>' for text in paragraphs)
    return ('<article><front><article-meta><article-id pub-id-type="doi">'
            '10.0000/lifecycle-' + name + '</article-id><title-group><article-title>'
            'Synthetic control study ' + name + '</article-title></title-group>'
            '</article-meta></front><body><sec><title>Methods and results</title>'
            + body + '</sec></body></article>').encode()


class LiteratureEvidenceLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        papers = {
            'alpha': ['ALPHA-CONTROL: The authors report a matched solvent control.',
                      'ALPHA-RESULT: The reported response is 12 units.',
                      'ALPHA-LIMIT: This synthetic study does not establish efficacy.'],
            'beta': ['BETA-CONTROL: The authors report an untreated control.',
                     'BETA-RESULT: The reported response is 7 units.',
                     'BETA-LIMIT: The two control designs were not compared directly.'],
        }
        self.local_sources = {}
        for name, paragraphs in papers.items():
            source = self.root / (name + '.xml')
            source.write_bytes(paper_xml(name, paragraphs))
            self.local_sources[name] = {'path': str(source), 'media_type': 'application/xml',
                'content_scope': 'full_text', 'paper_ref': 'DOI:10.0000/lifecycle-' + name,
                'source_url': 'https://fixture.invalid/' + name, 'license': 'CC0'}
        self.config = {'local_sources': self.local_sources}
        self.document_root = self.root / 'documents'
        self.adapters = [adapter for adapter in make_adapters(self.document_root, self.config)
            if adapter.adapter_id in {'literature.acquire_fulltext', 'literature.read_document'}]
        self.store = Store(self.root / 'ledger')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store)
        self.controller.initialize('lifecycle', budgets={
            'model_calls': {'limit': 100, 'hard': True},
            'tool_calls': {'limit': 100, 'hard': True}},
            config={'goal': 'Compare the controls using exact original passages',
                    'max_input_bytes': 65536, 'max_effective_context_bytes': 1048576})
        self.runtime = Runtime(self.store, 'lifecycle', adapters=self.adapters)
        self.sequence = 0
        self.documents = {name: self.call('acquire_fulltext', {'source_id': name})[
            'result']['data']['document_id'] for name in papers}

    def load(self, ref):
        return json.loads(self.store.read_blob(ref))

    def call(self, name, inputs):
        self.sequence += 1
        return self.runtime.execute_adapter('literature.' + name, inputs,
            trigger_id='host-fixture-' + str(self.sequence))

    def task(self, work=None):
        if work is not None and work['inputs'].get('literature_task_id'):
            return self.store.get('lifecycle', 'registration', work['inputs']['literature_task_id'])
        return max(self.store.list('lifecycle', 'registration', kind='literature_task'),
                   key=lambda task: task['created_seq'])

    def ready(self, kind):
        return next(work for work in self.controller.ready('lifecycle') if work['kind'] == kind)

    def command(self, work, action, payload, evidence_ids=()):
        mode = 'literature' if work['kind'] == 'literature' else 'lab'
        command = {'mode': mode, 'action': action, 'issue_id': None,
            'expected_versions': self.runtime.versions(None),
            'evidence_ids': list(evidence_ids), 'payload': payload}
        memory = {}
        if mode == 'literature':
            task = self.task(work)
            command.update(task_id=task['id'], task_revision=task['task_revision'])
        else:
            memory['rationale_summaries'] = [{'kind': 'literature_decision',
                'decision': 'delegate' if action == 'request_literature' else 'not_needed',
                'reason': 'Scripted lifecycle fixture; not an autonomous-routing benchmark.',
                'evidence_ids': list(evidence_ids), 'related_task_ids': []}]
        return {'command': command, 'working_memory_delta': json.dumps(memory)}

    def emit(self, work, action, payload, evidence_ids=()):
        response = self.command(work, action, payload, evidence_ids)
        def executor(effect, raw):
            envelope = json.loads(raw)
            return {'status': 'succeeded', 'actual_costs': {'model_calls': 1},
                'response': response, 'sent_packet_sha256': digest(raw),
                'sent_prompt_sha256': digest(envelope['prompt'].encode())}
        return self.runtime.run_meta(work['id'], executor, {'model_calls': 1})

    def delegate(self, evidence_ids=(), parent_work=None):
        self.sequence += 1
        lab = parent_work or self.controller.enqueue('lifecycle', 'lab', 'parent',
            'parent-fixture-' + str(self.sequence), inputs={
                'agent_instance_id': 'lab-control-comparison', 'branch_id': 'research',
                'evidence_ids': list(evidence_ids), 'required_evidence_ids': list(evidence_ids)})
        result = self.emit(lab, 'request_literature', {
            'question': 'Compare the two reported control designs.',
            'intended_use': 'Choose follow-up questions; do not infer efficacy.',
            'scope': 'Two supplied synthetic papers',
            'deliverables': ['A comparison citing one original control passage per paper'],
            'max_model_calls': 32, 'max_tool_calls': 32}, evidence_ids)
        self.assertEqual(result['proposal']['status'], 'applied', result)
        return self.ready('literature')

    def request_read(self, document_id, max_bytes=20000):
        work = self.ready('literature')
        result = self.emit(work, 'request_diagnostic', {'capability_id': 'literature.read_document',
            'capability_version': '1.0.0', 'inputs_json': json.dumps({
                'document_id': document_id, 'max_bytes': max_bytes})})
        self.assertEqual(result['proposal']['status'], 'applied', result)
        tool = self.ready('diagnostic')
        return self.runtime.execute_adapter(tool['inputs']['capability_id'],
            json.loads(tool['inputs']['inputs_json']), trigger_id=tool['id'], work_id=tool['id'])

    def source_ids(self, document_id=None):
        return [fact['id'] for fact in self.store.list('lifecycle', 'fact')
            if fact['provenance'].get('delivery_kind') == 'source_excerpt'
            and (document_id is None or fact['provenance'].get('document_id') == document_id)]

    def control_id(self, document_id, marker):
        return next(fid for fid in self.source_ids(document_id)
                    if marker in self.load(self.store.get('lifecycle', 'fact', fid)['content_ref'])['text'])

    def cache_payload(self, ids=(), documents=(), query='', limit=16):
        return {'evidence_ids': list(ids), 'document_ids': list(documents), 'query': query, 'limit': limit}

    def result_payload(self, ids):
        return {'answer': 'The reports use different control designs.', 'coverage_status': 'partial',
            'stop_reason': 'Both requested control passages inspected',
            'limitations': ['Synthetic fixture; no scientific efficacy conclusion'],
            'claims': [{'statement': 'The cited paper reports its stated control design.',
                        'evidence_ids': [fid], 'limitations': ['As reported, not replicated']} for fid in ids]}

    def register_paper(self, name, paragraphs):
        # A trusted local fixture registers bytes, not a model-controlled path.
        # The subsequent read still uses the real adapter/effect/publication.
        return DocumentStore(self.document_root).ingest(paper_xml(name, paragraphs),
            'application/xml', {'paper_ref': 'DOI:10.0000/lifecycle-' + name,
                'provider': 'local', 'content_scope': 'full_text'})['document_id']

    def test_repeat_read_reuses_original_identity_but_preserves_both_execution_receipts(self):
        doc = self.documents['alpha']
        first = self.call('read_document', {'document_id': doc, 'max_bytes': 20000})
        original_ids = set(self.source_ids(doc))
        before = {fid: self.store.get('lifecycle', 'fact', fid) for fid in original_ids}
        second = self.call('read_document', {'document_id': doc, 'max_bytes': 20000})
        self.assertGreaterEqual(len(original_ids), 3)
        self.assertEqual(set(self.source_ids(doc)), original_ids)
        self.assertNotEqual(first['effect']['id'], second['effect']['id'])
        for fid in original_ids:
            self.assertEqual(self.store.get('lifecycle', 'fact', fid), before[fid])
            reads = self.store.list('lifecycle', 'registration',
                kind='literature_source_read', canonical_fact_id=fid)
            self.assertEqual({row['effect_id'] for row in reads},
                             {first['effect']['id'], second['effect']['id']})
            for row in reads:
                effect = self.store.get('lifecycle', 'effect', row['effect_id'])
                self.assertEqual(row['result_ref'], effect['result_ref'])
        self.store.verify_history('lifecycle')

    def test_rebuilt_child_reads_cached_support_from_two_papers_then_returns_to_same_lab(self):
        self.delegate()
        self.request_read(self.documents['alpha'])
        self.request_read(self.documents['beta'])
        ids = [self.control_id(self.documents[name], name.upper() + '-CONTROL')
               for name in ('alpha', 'beta')]
        task = self.task()
        old = next(s for s in self.store.list('lifecycle', 'agent_session')
                   if s['agent_instance_id'] == task['agent_instance_id'] and s['status'] == 'idle')
        rebuilt = self.runtime.agents.rebuild_session(old['id'], 'Fixture context boundary', 'fixture-rebuild')
        before_tools = self.store.get('lifecycle', 'budget', 'tool_calls')['spent']
        before_effects = len(self.store.list('lifecycle', 'effect'))
        cached = self.emit(self.ready('literature'), 'read_cached_evidence',
            self.cache_payload(documents=[self.documents['alpha'], self.documents['beta']],
                               query='CONTROL', limit=2))
        self.assertEqual(cached['proposal']['status'], 'applied', cached)
        self.assertEqual(cached['effect']['metadata']['agent_session_id'], rebuilt['id'])
        self.assertEqual(self.store.get('lifecycle', 'budget', 'tool_calls')['spent'], before_tools)
        self.assertEqual(len(self.store.list('lifecycle', 'effect')), before_effects + 1)
        self.assertNotIn(ids[0], {p['evidence_id'] for p in self.load(cached['proposal']['delivery_ref'])},
                         'The rebuilt turn must not receive the older paper merely because it is cached')
        work = self.ready('literature')
        self.assertEqual(set(work['inputs']['required_evidence_ids']), set(ids))
        done = self.emit(work, 'complete_research', self.result_payload(ids), ids)
        self.assertEqual(done['proposal']['status'], 'applied', done)
        pages = self.load(done['proposal']['delivery_ref'])
        self.assertTrue(set(ids) <= {p['evidence_id'] for p in pages if p['complete_source']})
        parent = self.ready('lab')
        self.assertEqual(parent['inputs']['agent_instance_id'], 'lab-control-comparison')
        self.assertEqual(parent['inputs']['branch_id'], 'research')
        self.assertEqual(self.task()['parent_owner_id'], task['parent_owner_id'])
        self.assertTrue(set(ids) <= set(parent['inputs']['required_evidence_ids']))
        accepted = self.emit(parent, 'no_action', {'reason': 'Comparison received'}, ids)
        self.assertEqual(accepted['proposal']['status'], 'applied', accepted)
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual(len(self.store.list('lifecycle', 'registration', kind='literature_task')), 1)
        self.store.verify_history('lifecycle')

    def test_other_tasks_cached_source_does_not_authorize_an_unrelated_child(self):
        self.delegate()
        self.request_read(self.documents['alpha'])
        foreign = self.control_id(self.documents['alpha'], 'ALPHA-CONTROL')
        first_task = self.task()['id']
        done = self.emit(self.ready('literature'), 'complete_research', self.result_payload([foreign]), [foreign])
        self.assertEqual(done['proposal']['status'], 'applied', done)
        self.delegate(parent_work=self.ready('lab'))
        self.assertNotEqual(self.task()['id'], first_task)
        result = self.emit(self.ready('literature'), 'read_cached_evidence', self.cache_payload([foreign]))
        self.assertNotEqual(result['proposal']['status'], 'applied')
        self.assertNotIn(foreign, self.task()['evidence_ids'])
        for work in self.controller.ready('lifecycle'):
            if work['kind'] == 'literature':
                self.assertNotIn(foreign, work['inputs']['required_evidence_ids'])

    def test_summary_is_not_an_original_even_when_fully_delivered(self):
        acquisition = self.store.list('lifecycle', 'fact')[0]['id']
        self.controller.add_fact('lifecycle', 'fixture-summary', 'source_statement',
            {'answer': 'A cached interpretation, not the paper'},
            {'delivery_kind': 'summary', 'summary_of': [acquisition]}, 'summary')
        work = self.delegate(['fixture-summary'])
        result = self.emit(work, 'complete_research', self.result_payload(['fixture-summary']), ['fixture-summary'])
        self.assertEqual(result['proposal']['status'], 'rejected')
        self.assertIn('complete original', result['proposal']['error'])

    def test_cached_summary_cannot_be_selected_as_an_original(self):
        acquisition = self.store.list('lifecycle', 'fact')[0]['id']
        self.controller.add_fact('lifecycle', 'fixture-summary', 'source_statement',
            {'answer': 'A cached interpretation, not the paper'},
            {'delivery_kind': 'summary', 'summary_of': [acquisition]}, 'summary')
        work = self.delegate(['fixture-summary'])
        result = self.emit(work, 'read_cached_evidence', self.cache_payload(['fixture-summary']))
        self.assertNotEqual(result['proposal']['status'], 'applied')
        self.assertIn('not an original', result['proposal']['error'])
        self.assertFalse(self.store.list('lifecycle', 'registration', kind='literature_cached_read'))

    def test_partial_packet_cannot_turn_cached_original_identity_into_claim_authority(self):
        self.delegate()
        self.request_read(self.documents['alpha'])
        work = self.ready('literature')
        fid = self.control_id(self.documents['alpha'], 'ALPHA-CONTROL')
        agent, mode, task, options = self.runtime._context(work)
        agent.builder.page_bytes = 64
        arguments = agent.arguments(mode, task, **options)
        # This source is optional in this admission. Required originals are
        # intentionally never truncated by PacketBuilder's paging strategy.
        arguments['required_evidence_ids'] = []
        packet = agent.builder.build(**arguments, strategy='paged')
        page = next(p for p in packet['delivery_pages'] if p['evidence_id'] == fid)
        self.assertFalse(page['complete_source'])
        with self.assertRaisesRegex(ValueError, 'complete original'):
            validate_command(self.command(work, 'complete_research', self.result_payload([fid]), [fid]),
                             packet, current_versions=self.runtime.versions(None))

    def test_oversized_tool_result_is_read_in_bounded_batches_with_explicit_unread_state(self):
        doc = self.register_paper('many-blocks', [
            f'OBSERVATION-{index:03d}: ' + 'The synthetic report records a control condition. ' * 5
            for index in range(120)])
        self.delegate()
        result = self.request_read(doc, max_bytes=65536)
        self.assertEqual(result['result']['status'], 'ok')
        originals = set(self.source_ids(doc))
        self.assertGreaterEqual(len(originals), 120)
        before_tool_cost = self.store.get('lifecycle', 'budget', 'tool_calls')['spent']
        observed, unread_counts = set(), []
        for _ in range(20):
            work = self.ready('literature')
            _, _, task, _ = self.runtime._context(work)
            state = task['reading_state']
            unread_counts.append(state['unread_count'])
            if state['unread_count'] == 0:
                break
            outcome = self.emit(work, 'read_cached_evidence',
                                self.cache_payload(documents=[doc], limit=16))
            self.assertEqual(outcome['proposal']['status'], 'applied', outcome)
            plan = self.load(outcome['effect']['metadata']['plan_ref'])
            self.assertLessEqual(plan['input_bytes'], 65536)
            self.assertGreater(plan['task']['reading_state']['unread_count'], 0)
            pages = self.load(outcome['proposal']['delivery_ref'])
            sources = [page for page in pages if page['evidence_id'] in originals]
            self.assertTrue(all(page['complete_source'] for page in sources))
            observed.update(page['evidence_id'] for page in sources)
        else:
            self.fail('Cached source batches did not finish within the explicit fixture bound')
        self.assertEqual(observed, originals)
        self.assertEqual(unread_counts[0], len(originals))
        self.assertEqual(unread_counts[-1], 0)
        self.assertTrue(all(later < earlier for earlier, later in zip(unread_counts, unread_counts[1:])))
        replans = self.store.list('lifecycle', 'registration', kind='literature_capacity_replan')
        self.assertTrue(replans, 'A real oversized publication must exercise automatic batching')
        self.assertTrue(any(row['deferred_evidence_ids'] for row in replans))
        self.assertTrue(all(row['models_executed'] == row['tools_executed'] == 0 for row in replans))
        self.assertEqual(self.store.get('lifecycle', 'budget', 'tool_calls')['spent'], before_tool_cost)
        self.assertEqual(len([e for e in self.store.list('lifecycle', 'effect')
            if e['metadata'].get('adapter_id') == 'literature.read_document']), 1)
        self.assertEqual(set(self.source_ids(doc)), originals)
        self.assertEqual(set(self.task()['evidence_ids']) & originals, originals)
        self.store.verify_history('lifecycle')

    def test_single_unrepresentable_original_returns_unresolved_to_parent_without_dispatch(self):
        doc = self.register_paper('one-large-block', ['LARGE-ORIGINAL: ' + 'x' * 60000])
        self.delegate()
        self.request_read(doc, max_bytes=65536)
        large = next(fid for fid in self.source_ids(doc)
            if len(self.load(self.store.get('lifecycle', 'fact', fid)['content_ref'])['text']) > 50000)
        requested = self.emit(self.ready('literature'), 'read_cached_evidence', self.cache_payload([large], limit=1))
        self.assertEqual(requested['proposal']['status'], 'applied', requested)
        work = self.ready('literature')
        effects_before = len(self.store.list('lifecycle', 'effect'))
        budgets_before = {b['id']: (b['spent'], b['reserved']) for b in self.store.list('lifecycle', 'budget')}
        stopped = self.runtime.prepare_meta(work['id'], {'model_calls': 1})
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertIsNone(stopped.get('effect_id'))
        self.assertEqual(len(self.store.list('lifecycle', 'effect')), effects_before)
        self.assertEqual({b['id']: (b['spent'], b['reserved']) for b in self.store.list('lifecycle', 'budget')}, budgets_before)
        self.assertEqual(self.task()['status'], 'completed')
        parent = self.ready('lab')
        summary_id = self.task()['result_ids'][-1]
        summary = self.load(self.store.get('lifecycle', 'fact', summary_id)['content_ref'])
        self.assertEqual(summary['coverage_status'], 'unresolved')
        self.assertEqual(summary['stop_reason'], 'source_context_capacity')
        self.assertEqual(summary['claims'], [])
        self.assertNotIn(large, parent['inputs']['required_evidence_ids'])
        self.assertIn(large, self.task()['evidence_ids'], 'Capacity handling must preserve the unread original')
        self.assertEqual(parent['inputs']['agent_instance_id'], 'lab-control-comparison')
        accepted = self.emit(parent, 'no_action', {'reason': 'Unresolved source capacity received'})
        self.assertEqual(accepted['proposal']['status'], 'applied', accepted)
        self.assertFalse(any(w['status'] == 'blocked' for w in self.store.list('lifecycle', 'work')))
        self.store.verify_history('lifecycle')

    def test_crash_replays_publication_and_cached_route_without_repeat_tool_or_child(self):
        self.delegate()
        with patch.object(self.runtime, 'publish_adapter_effect', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.request_read(self.documents['alpha'])
        settled = [e for e in self.store.list('lifecycle', 'effect')
                   if e['metadata'].get('adapter_id') == 'literature.read_document']
        self.assertEqual(len(settled), 1)
        costs = self.store.get('lifecycle', 'budget', 'tool_calls')['spent']
        self.runtime = Runtime(self.store, 'lifecycle', adapters=self.adapters)
        with patch.object(self.runtime.controller, 'dispatch_effect', side_effect=AssertionError('No replay')):
            self.runtime.recover()
            self.runtime.recover()
        self.assertEqual(self.store.get('lifecycle', 'budget', 'tool_calls')['spent'], costs)
        fid = self.control_id(self.documents['alpha'], 'ALPHA-CONTROL')
        original_mutate = self.runtime._mutate
        def crash_after_route(key, *args, **kwargs):
            if key.startswith('applied:'):
                raise KeyboardInterrupt('Crash after cached-reading route committed')
            return original_mutate(key, *args, **kwargs)
        with patch.object(self.runtime, '_mutate', side_effect=crash_after_route):
            with self.assertRaises(KeyboardInterrupt):
                self.emit(self.ready('literature'), 'read_cached_evidence', self.cache_payload([fid]))
        models = self.store.get('lifecycle', 'budget', 'model_calls')['spent']
        with patch.object(self.runtime.controller, 'dispatch_effect', side_effect=AssertionError('No replay')):
            self.runtime.recover()
            self.runtime.recover()
        self.assertEqual(self.store.get('lifecycle', 'budget', 'model_calls')['spent'], models)
        self.assertEqual(self.store.get('lifecycle', 'budget', 'tool_calls')['spent'], costs)
        self.assertEqual(len(self.store.list('lifecycle', 'registration', kind='literature_task')), 1)
        self.assertEqual(len([w for w in self.controller.ready('lifecycle') if w['kind'] == 'literature']), 1)
        self.assertEqual(len(self.store.list('lifecycle', 'registration',
            kind='literature_source_read', canonical_fact_id=fid)), 1)
        self.store.verify_history('lifecycle')


if __name__ == '__main__':
    unittest.main()
