"""Paid-service lifecycle contracts; no credentials or network required."""
import gzip
import json
import types
import unittest

from rsi_literature.services import run_service, service_max_requests


SID = '5ad08bfb-cbe0-4911-a8c3-309760d33029'
SECRET = 'HOST_API_SECRET'
TOKEN = 'AUTHENTICATED_JWT_SECRET'


def response(data, status=200):
    return {'status': status, 'body': data if isinstance(data, bytes) else json.dumps(data).encode(), 'headers': {}}


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append({'method': method, 'url': url, 'headers': headers, 'body': body,
                           'timeout': timeout, 'max_bytes': max_bytes})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSDKClient:
    def __init__(self, api_key, cleanup_on_exit):
        self.api_key, self.closed = api_key, False

    @property
    def client(self):
        return self.get_client()

    def create_task(self, task):
        return self.client.post('/v0.1/crows', json=task).json()['trajectory_id']

    def get_task(self, task_id, lite=False):
        assert lite is True, 'must never automatically hydrate SDK state blobs'
        with self.client.stream('GET', '/v0.2/trajectories/' + task_id, params={'history': False, 'lite': True}) as result:
            result.raise_for_status()
            return json.loads(''.join(result.iter_text()))

    def close(self):
        self.closed = True


SDK = types.SimpleNamespace(__version__='0.17.0', EdisonClient=FakeSDKClient,
    JobNames=types.SimpleNamespace(LITERATURE='job-futurehouse-paperqa3', LITERATURE_HIGH='job-futurehouse-paperqa3-high'))


class Store:
    def __init__(self):
        self.rows = []

    def ingest(self, raw, media_type, metadata):
        self.rows.append((raw, media_type, metadata))
        return {'document_id': 'doc_test', 'content_scope': metadata['content_scope']}


class ServiceTests(unittest.TestCase):
    def call(self, provider, inputs, transport, **kwargs):
        return run_service(provider, kwargs.pop('settings', {}), inputs, transport=transport,
            env={provider.upper() + '_API_KEY': SECRET}, module_loader=lambda _: SDK, **kwargs)

    def test_missing_credentials_and_unknown_service_do_not_dispatch(self):
        transport = Transport()
        self.assertEqual(run_service('elicit', {}, {'action': 'search', 'query': 'test'}, transport=transport, env={})['status'], 'unavailable')
        self.assertEqual(run_service('unknown', {}, {}, transport=transport, env={})['status'], 'unavailable')
        self.assertEqual(transport.calls, [])

    def test_elicit_v2_search_preserves_warnings_and_fulltext_scope(self):
        transport = Transport(response({'papers': [{'title': 'A paper', 'elicitId': 'p1', 'abstract': 'A summary',
            'fullTextUrl': 'https://example.org/paper.pdf'}], 'warnings': [{'message': 'A query term was ignored'}]}))
        result = self.call('elicit', {'action': 'search', 'query': 'a question', 'limit': 2}, transport)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(transport.calls[0]['url'], 'https://elicit.com/api/v2/search/papers')
        self.assertEqual(json.loads(transport.calls[0]['body'])['maxResults'], 2)
        self.assertFalse(result['data']['papers'][0]['fulltext_candidates'][0]['retrieved'])
        self.assertFalse(result['coverage']['corpus_complete'])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_elicit_empty_search_must_have_explicit_papers(self):
        self.assertEqual(self.call('elicit', {'action': 'search', 'query': 'a'}, Transport(response({})))['status'], 'failed')
        result = self.call('elicit', {'action': 'search', 'query': 'a'}, Transport(response({'papers': []})))
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['coverage']['absence_is_negative_evidence'])

    def test_elicit_creation_is_private_and_only_submits_once(self):
        transport = Transport(response({'sessionId': SID, 'status': 'processing'}, 202))
        result = self.call('elicit', {'action': 'start', 'query': 'question'}, transport, effect_id='eff_1')
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(json.loads(transport.calls[0]['body'])['isPublic'])
        self.assertEqual(result['data']['session_id'], SID)
        self.assertEqual(result['provenance']['effect_id'], 'eff_1')
        self.assertEqual(len(transport.calls), 1)

    def test_elicit_creation_timeout_or_malformed_receipt_is_unknown(self):
        for item in (TimeoutError('secret=' + SECRET), response({}, 202), response({}, 503)):
            with self.subTest(item=type(item)):
                transport = Transport(item)
                result = self.call('elicit', {'action': 'start', 'query': 'question'}, transport)
                self.assertTrue(result['execution_unknown'])
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn(SECRET, json.dumps(result))

    def test_elicit_explicit_rejection_is_not_unknown(self):
        for status in (400, 401, 403, 429):
            result = self.call('elicit', {'action': 'start', 'query': 'question'}, Transport(response({}, status)))
            self.assertFalse(result['execution_unknown'])

    def test_accepted_session_handle_survives_unrecognized_state(self):
        result = self.call('elicit', {'action': 'start', 'query': 'q'}, Transport(response({'sessionId': SID, 'status': 'new_state'}, 202)))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['data']['session_id'], SID)
        self.assertFalse(result['execution_unknown'])

    def test_elicit_status_and_result_are_distinct_and_report_is_interpretive(self):
        store = Store()
        transport = Transport(response({'sessionId': SID, 'status': 'completed', 'result': {
            'reportBody': '# Report\nA cited synthesis', 'pdfUrl': 'https://storage.googleapis.com/report?X-Goog-Signature=SIGNED_SECRET'}}))
        result = self.call('elicit', {'action': 'result', 'session_id': SID}, transport, document_store=store)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(transport.calls[0]['url'].endswith('?include=reportBody'))
        self.assertEqual(store.rows[0][2]['artifact_role'], 'generated_report')
        self.assertFalse(store.rows[0][2]['original_source'])
        self.assertNotIn('SIGNED_SECRET', json.dumps(result))
        self.assertTrue(result['continuation']['complete_for_query'])

    def test_elicit_completed_without_body_is_partial_for_result(self):
        result = self.call('elicit', {'action': 'result', 'session_id': SID}, Transport(response({'sessionId': SID, 'status': 'completed'})))
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['continuation']['complete_for_query'])

    def test_elicit_quotapaused_stops_normal_polling(self):
        result = self.call('elicit', {'action': 'status', 'session_id': SID}, Transport(response({'sessionId': SID, 'status': 'pausedForInsufficientQuota'})))
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['continuation']['automatic_polling_allowed'])

    def test_elicit_identity_mismatch_is_rejected(self):
        result = self.call('elicit', {'action': 'status', 'session_id': SID}, Transport(response({'sessionId': 'other', 'status': 'completed'})))
        self.assertEqual(result['errors'][0]['code'], 'identity_mismatch')

    def test_invalid_fields_no_dispatch(self):
        transport = Transport()
        cases = [('elicit', {'action': 'start', 'query': 'q', 'isPublic': True}),
                 ('elicit', {'action': 'search', 'query': 'q', 'search_mode': 'keyword', 'filters': {'hasPdf': True}}),
                 ('edison', {'action': 'start', 'query': 'q', 'mode': 'molecules'}),
                 ('edison', {'action': 'status', 'task_id': '../secret'})]
        for provider, inputs in cases:
            result = self.call(provider, inputs, transport)
            self.assertEqual(result['status'], 'failed')
        self.assertEqual(transport.calls, [])

    def test_edison_start_uses_sdk_with_bounded_auth_and_no_loop(self):
        transport = Transport(response({'access_token': TOKEN}), response({'trajectory_id': SID}))
        result = self.call('edison', {'action': 'start', 'query': 'a question', 'mode': 'literature_high'}, transport)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['provenance']['attempt_count'], 2)
        self.assertEqual(json.loads(transport.calls[1]['body'])['name'], SDK.JobNames.LITERATURE_HIGH)
        self.assertEqual(transport.calls[1]['headers']['Authorization'], 'Bearer ' + TOKEN)
        self.assertEqual(result['data']['task_id'], SID)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_edison_creation_timeout_marks_unknown_but_auth_failure_does_not(self):
        transport = Transport(response({'access_token': TOKEN}), TimeoutError('oops'))
        result = self.call('edison', {'action': 'start', 'query': 'q'}, transport)
        self.assertTrue(result['execution_unknown'])
        self.assertEqual(len(transport.calls), 2)
        result = self.call('edison', {'action': 'start', 'query': 'q'}, Transport(TimeoutError('oops')))
        self.assertFalse(result['execution_unknown'])

    def test_edison_sdk_status_uses_lite(self):
        transport = Transport(response({'access_token': TOKEN}), response({'id': SID, 'status': 'running'}))
        result = self.call('edison', {'action': 'status', 'task_id': SID}, transport)
        self.assertEqual(result['status'], 'partial')
        self.assertIn('lite=True', transport.calls[1]['url'])
        self.assertEqual(len(transport.calls), 2)

    def test_edison_result_fetches_only_environment_blob_and_redacts_signed_url(self):
        frame_state = {'state': {'response': {'answer': {'formatted_answer': 'Cited report',
                        'has_successful_answer': True, 'answer_reasoning': 'not evidence'}}}}
        transport = Transport(response({'access_token': TOKEN}), response({'id': SID, 'status': 'success',
            'agent_state': [{'state': None, 'download_url': 'https://storage.googleapis.com/history?token=HISTORY_SECRET'}],
            'environment_frame': {'state': None, 'download_url': 'https://storage.googleapis.com/current?X-Goog-Signature=SIGNED_SECRET'}}),
            response(gzip.compress(json.dumps(frame_state).encode())))
        store = Store()
        result = self.call('edison', {'action': 'result', 'task_id': SID}, transport, document_store=store)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['provenance']['attempt_count'], 3)
        self.assertNotIn('Authorization', transport.calls[2]['headers'])
        self.assertNotIn('agent_state', result['data']['provider_result'])
        self.assertNotIn('SIGNED_SECRET', json.dumps(result))
        self.assertNotIn('HISTORY_SECRET', json.dumps(result))
        self.assertNotIn('answer_reasoning', json.dumps(result))
        self.assertEqual(store.rows[0][0], b'Cited report')

    def test_edison_result_blocks_unconfigured_blob_origin_and_gzip_bomb(self):
        for url, blob, code in [('http://127.0.0.1/secret', None, 'blob_host_not_allowed'),
                               ('https://storage.googleapis.com/current', gzip.compress(b'x' * 2048), 'response_limit')]:
            replies = [response({'access_token': TOKEN}), response({'id': SID, 'status': 'success',
                'environment_frame': {'state': None, 'download_url': url}})]
            if blob:
                replies.append(response(blob))
            result = self.call('edison', {'action': 'result', 'task_id': SID}, Transport(*replies), settings={'max_response_bytes': 1024})
            self.assertEqual(result['errors'][0]['code'], code)

    def test_edison_completed_but_unsuccessful_answer_is_partial(self):
        transport = Transport(response({'access_token': TOKEN}), response({'id': SID, 'status': 'success',
            'environment_frame': {'state': {'state': {'response': {'answer': {
                'answer': 'No answer found', 'has_successful_answer': False}}}}}}))
        result = self.call('edison', {'action': 'result', 'task_id': SID}, transport)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['errors'][0]['code'], 'answer_unsuccessful')

    def test_missing_or_changed_sdk_is_unavailable_before_auth(self):
        transport = Transport()
        def missing(_):
            raise ImportError('missing')
        result = run_service('edison', {}, {'action': 'start', 'query': 'q'}, env={'EDISON_API_KEY': SECRET}, transport=transport, module_loader=missing)
        self.assertEqual(result['status'], 'unavailable')
        wrong = types.SimpleNamespace(__version__='0.99.0')
        result = run_service('edison', {}, {'action': 'start', 'query': 'q'}, env={'EDISON_API_KEY': SECRET}, transport=transport, module_loader=lambda _: wrong)
        self.assertEqual(result['errors'][0]['code'], 'sdk_version_unsupported')
        self.assertEqual(transport.calls, [])

    def test_raw_key_configuration_is_rejected(self):
        result = self.call('elicit', {'action': 'search', 'query': 'q'}, Transport(), settings={'api_key': SECRET})
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_request_admission_bounds(self):
        self.assertEqual(service_max_requests('elicit', 'start'), 1)
        self.assertEqual(service_max_requests('edison', 'start'), 2)
        self.assertEqual(service_max_requests('edison', 'status'), 2)
        self.assertEqual(service_max_requests('edison', 'result'), 3)


if __name__ == '__main__':
    unittest.main()
