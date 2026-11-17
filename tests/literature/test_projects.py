"""Real bounded subprocesses with fake project programs; no model/network calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from rsi_literature.documents import DocumentStore
from rsi_literature.projects import run_project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DocumentStore(self.root / 'documents')
        self.manifest = self.store.ingest(b'Matched control was used in the assay.', 'text/plain',
            {'content_scope': 'abstract', 'paper_id': 'PMID:12345', 'pmid': '12345', 'title': 'Assay study'})
        self.did = self.manifest['document_id']
        self.counter = 0

    def command(self, program):
        self.counter += 1
        script = self.root / ('program_' + str(self.counter) + '.py')
        script.write_text(textwrap.dedent(program))
        return [sys.executable, str(script)]

    def invoke(self, provider, settings, inputs=None, effect_id='project-test'):
        return run_project(provider, {'model': 'host-local-model', 'backend': 'installed-corpus', **settings},
            inputs or {'document_ids': [self.did], 'query': 'Which control?'}, self.store, effect_id)

    def json_command(self):
        return self.command('''
            import json, pathlib, sys
            value = json.loads(pathlib.Path(sys.argv[1]).read_text())
            pathlib.Path(sys.argv[2]).write_text(json.dumps({'model': sys.argv[3], 'input': value,
                'cwd': str(pathlib.Path.cwd()), 'support_text': 'Project-generated support claim'}))
        ''') + ['{input_file}', '{output_file}', '{model}']

    def test_starling_json_file_bridge_exports_only_registered_sources_and_host_model(self):
        out = self.invoke('starling', {'protocol': 'json_file', 'command': self.json_command()})
        self.assertEqual(out['status'], 'ok', out)
        result = out['data']['interpretation']
        self.assertEqual(result['model'], 'host-local-model')
        self.assertEqual(result['input']['documents'][0]['text'], self.store.read(self.did)['text'])
        self.assertEqual(result['input']['documents'][0]['document_id'], self.did)
        self.assertTrue(Path(result['cwd']).is_relative_to(self.store.root / '.project_jobs'))
        self.assertEqual(out['provenance']['integration_kind'], 'deployment_bridge')
        self.assertEqual(out['data']['passages'][0]['source_sha256'], self.manifest['source_sha256'])
        self.assertIsNone(out['provenance']['cost_visibility']['internal_model_calls'])
        self.assertNotIn('Project-generated', json.dumps(out['data']['passages']))


    def test_project_preserves_configured_interpreter_environment(self):
        command = self.command('''
            import json, pathlib, sys
            pathlib.Path(sys.argv[2]).write_text(json.dumps({'prefix': sys.prefix}))
        ''') + ['{input_file}', '{output_file}', '{model}']
        out = self.invoke('starling', {'command': command, 'protocol': 'json_file'})
        self.assertEqual(out['status'], 'ok', out)
        self.assertEqual(out['data']['interpretation']['prefix'], sys.prefix)

    def test_model_command_path_env_injection_rejected_before_process(self):
        command = self.json_command()
        for extra in ({'command': ['evil']}, {'path': '/etc/passwd'}, {'env': {'TOKEN': 'bad'}},
                      {'parameters': {'nested': {'model': 'paid-default'}}}):
            with self.subTest(extra=extra):
                out = self.invoke('starling', {'command': command, 'protocol': 'json_file'},
                    {'document_ids': [self.did], 'query': 'Which control?', **extra})
                self.assertEqual(out['status'], 'failed')
                self.assertEqual(out['provenance']['attempt_count'], 0)


    def test_starling_requires_backend_and_overrides_all_model_defaults(self):
        command = self.command('''
            import json, pathlib, sys
            args = dict(item.split('=', 1) for item in sys.argv[1:])
            output = pathlib.Path(json.loads(args['outputs.extraction']))
            pmids = json.loads(pathlib.Path(json.loads(args['extract.pmids_file'])).read_text())
            assert isinstance(pmids, list)
            spec = json.loads(pathlib.Path(json.loads(args['spec_file'])).read_text())
            assert isinstance(spec['filters'], list)
            output.write_text(json.dumps({'args': args, 'pmids': pmids, 'spec': spec,
                'support_text': 'Model evidence text'}) + '\\n')
        ''')
        inputs = {'document_ids': [self.did], 'prompt': 'Extract control conditions.'}
        blocked = self.invoke('starling', {'command': command, 'backend': None}, inputs)
        self.assertEqual(blocked['status'], 'unavailable')
        out = self.invoke('starling', {'command': command, 'backend': 'installed-corpus'}, inputs)
        self.assertEqual(out['status'], 'ok', out)
        raw = out['data']['interpretation'][0]
        for role in ('main', 'sub', 'validator', 'investigator', 'extractor'):
            self.assertEqual(json.loads(raw['args']['models.' + role]), 'host-local-model')
        self.assertEqual(json.loads(raw['args']['extract.schema_deriver_model']), 'host-local-model')
        self.assertEqual(raw['pmids'], ['12345'])
        self.assertEqual(raw['spec'], {'filters': []})
        self.assertEqual(out['data']['passages'], [])
        self.assertEqual(out['coverage']['registered_sources_used_as'], 'seed_metadata_only')

    def test_starling_native_run_preserves_task_quoting_and_interpretive_output(self):
        command = self.command('''
            import json, pathlib, sys
            args = dict(item.split('=', 1) for item in sys.argv[1:])
            output = pathlib.Path(json.loads(args['outputs.agent_run']))
            output.write_text(json.dumps({'mode': args['mode'], 'task': json.loads(args['task'])}))
        ''')
        question = 'Compare "control=matched"\nand untreated controls.'
        out = self.invoke('starling', {'command': command}, {'query': question})
        self.assertEqual(out['status'], 'ok', out)
        self.assertEqual(out['data']['interpretation'], {'mode': 'run', 'task': question})
        self.assertEqual(out['data']['passages'], [])
        self.assertFalse(out['coverage']['source_text_delivered'])

    def test_starling_empty_native_extraction_cannot_report_success(self):
        command = self.command('''
            import json, pathlib, sys
            args = dict(item.split('=', 1) for item in sys.argv[1:])
            pathlib.Path(json.loads(args['outputs.extraction'])).write_text('')
        ''')
        out = self.invoke('starling', {'command': command},
            {'document_ids': [self.did], 'prompt': 'Extract controls.'})
        self.assertEqual(out['status'], 'failed', out)
        self.assertEqual(out['errors'][0]['code'], 'incomplete_project_output')
        self.assertTrue(out['execution_unknown'])
        self.assertEqual(out['data'], {})


    def test_timeout_is_unknown_and_cannot_relaunch_same_effect(self):
        command = self.command('''
            import time
            time.sleep(5)
        ''') + ['{input_file}', '{output_file}', '{model}']
        settings = {'command': command, 'protocol': 'json_file', 'timeout': 1}
        out = self.invoke('starling', settings)
        self.assertEqual(out['status'], 'failed')
        self.assertTrue(out['execution_unknown'])
        again = self.invoke('starling', settings)
        self.assertTrue(again['execution_unknown'])
        self.assertEqual(again['provenance']['attempt_count'], 0)

    def test_result_cache_replays_bound_completed_job_without_process(self):
        settings = {'command': self.json_command(), 'protocol': 'json_file'}
        first = self.invoke('starling', settings)
        with patch('rsi_literature.projects._execute', side_effect=AssertionError('Do not execute twice')):
            second = self.invoke('starling', settings)
        self.assertEqual(first, second)
        changed = self.invoke('starling', settings, {'document_ids': [self.did], 'query': 'Another question'})
        self.assertEqual(changed['status'], 'failed')

    def test_output_limit_and_symlink_output_are_not_read_as_evidence(self):
        for code in ("pathlib.Path(sys.argv[2]).write_text('x' * 2048)",
                     "pathlib.Path(sys.argv[2]).symlink_to('/etc/passwd')"):
            with self.subTest(code=code):
                command = self.command('import pathlib, sys\n' + code) + ['{input_file}', '{output_file}', '{model}']
                out = self.invoke('starling', {'command': command, 'protocol': 'json_file', 'max_output_bytes': 1024},
                                  effect_id='output-' + str(self.counter))
                self.assertEqual(out['status'], 'failed')
                self.assertTrue(out['execution_unknown'])
                self.assertEqual(out['data'], {})

    def test_missing_model_and_unregistered_document_do_not_invoke_project(self):
        settings = {'command': self.json_command(), 'protocol': 'json_file', 'model': ''}
        out = self.invoke('starling', settings)
        self.assertEqual(out['status'], 'unavailable')
        settings['model'] = 'host-model'
        out = self.invoke('starling', settings, {'document_ids': ['/etc/passwd'], 'query': 'Read file'})
        self.assertEqual(out['status'], 'failed')
        self.assertEqual(out['provenance']['attempt_count'], 0)

    def test_removed_providers_are_rejected_before_sources_jobs_or_processes(self):
        for provider in ('tooluniverse', 'llm_nerre', 'llm_organic_synthesis', 'openscholar', 'paperpilot'):
            with self.subTest(provider=provider), \
                    patch('rsi_literature.projects._execute', side_effect=AssertionError('Do not launch removed project')), \
                    patch.object(self.store, 'read', side_effect=AssertionError('Do not read sources')):
                out = self.invoke(provider, {'command': self.json_command(), 'protocol': 'json_file'})
                self.assertEqual(out['status'], 'unavailable')
                self.assertEqual(out['provenance']['attempt_count'], 0)
                self.assertNotIn('execution_unknown', out)
                self.assertFalse((self.store.root / '.project_jobs').exists())


if __name__ == '__main__':
    unittest.main()
