"""MCP protocol identity and bounded HTTP reads, entirely without networking."""
import io
import http.client
import json
import socket
import types
import unittest
import urllib.error
from unittest.mock import patch

from rsi_literature.remote import MCPClient, RemoteError, http_request


def reply(result=None, *, rpc_id=1, status=200, headers=None, raw=None):
    return {'status': status, 'headers': headers or {}, 'body': raw if raw is not None else
            json.dumps({'jsonrpc': '2.0', 'id': rpc_id, 'result': result or {}}).encode()}


def initialization(session='session-123'):
    return reply({'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'offline', 'version': '1'}},
                 headers={'Mcp-Session-Id': session} if session else {})


class Transport:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append({'method': method, 'url': url, 'headers': headers, 'body': json.loads(body)})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class MCPTests(unittest.TestCase):
    def client(self, transport, **settings):
        return MCPClient({'endpoint': 'https://example.org/mcp', 'tools': ['search'], **settings},
                         transport=transport, env={'SECRET_ENV': 'host/secret+token'})

    def test_initialization_session_and_reused_client(self):
        transport = Transport(initialization(), reply(status=202, raw=b''),
                              reply({'content': [{'type': 'text', 'text': 'a paper'}]}, rpc_id=3),
                              reply({'content': []}, rpc_id=4))
        client = self.client(transport)
        self.assertEqual(client.call_tool('search', {'query': 'first'})['content'][0]['text'], 'a paper')
        client.call_tool('search', {'query': 'second'})
        self.assertEqual([c['body']['method'] for c in transport.calls],
                         ['initialize', 'notifications/initialized', 'tools/call', 'tools/call'])
        self.assertNotIn('id', transport.calls[1]['body'])
        self.assertNotIn('Mcp-Session-Id', transport.calls[0]['headers'])
        self.assertEqual(transport.calls[2]['headers']['Mcp-Session-Id'], 'session-123')
        self.assertEqual(client.attempt_count, 4)

    def test_allowlist_rejects_before_network_and_filters_inventory(self):
        transport = Transport(initialization(), reply(status=202, raw=b''),
                              reply({'tools': [{'name': 'search'}, {'name': 'write'}], 'nextCursor': 'next'}, rpc_id=3))
        client = self.client(transport)
        with self.assertRaises(RemoteError) as caught:
            client.call_tool('write', {})
        self.assertFalse(caught.exception.dispatched)
        self.assertEqual(client.attempt_count, 0)
        inventory = client.list_tools()
        self.assertEqual(inventory['tools'], [{'name': 'search'}])
        self.assertEqual(inventory['next_cursor'], 'next')

    def test_tool_inventory_missing_list_is_not_empty_success(self):
        client = self.client(Transport(initialization(), reply(status=202, raw=b''), reply({}, rpc_id=3)))
        with self.assertRaises(RemoteError) as caught:
            client.list_tools()
        self.assertEqual(caught.exception.code, 'invalid_response')

    def test_sse_supports_notifications_multiline_data_and_crlf(self):
        sse = (': keepalive\r\n\r\nevent: message\r\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\r\n\r\n'
               'event: message\r\ndata: {"jsonrpc":"2.0","id":3,\r\ndata: "result":{"content":[]}}\r\n\r\n').encode()
        client = self.client(Transport(initialization(), reply(status=202, raw=b''),
            reply(raw=sse, headers={'Content-Type': 'text/event-stream; charset=utf-8'})))
        self.assertEqual(client.call_tool('search', {})['content'], [])

    def test_wrong_or_duplicate_rpc_identity_is_unknown_after_dispatch(self):
        cases = [reply({}, rpc_id=99), reply({}, rpc_id=True),
                 reply(raw=b'data: {"jsonrpc":"2.0","id":3,"result":{}}\n\ndata: {"jsonrpc":"2.0","id":3,"result":{}}\n\n',
                       headers={'content-type': 'text/event-stream'})]
        for last in cases:
            with self.subTest(last=last):
                client = self.client(Transport(initialization(), reply(status=202, raw=b''), last))
                with self.assertRaises(RemoteError) as caught:
                    client.call_tool('search', {})
                self.assertEqual(caught.exception.code, 'invalid_response')
                self.assertTrue(caught.exception.dispatched)
                self.assertEqual(client.attempt_count, 3)

    def test_server_cannot_change_or_inject_session_binding(self):
        for session in ('different', 'bad\r\nheader'):
            client = self.client(Transport(initialization(), reply(status=202, raw=b''),
                reply({}, rpc_id=3, headers={'Mcp-Session-Id': session})))
            with self.assertRaises(RemoteError) as caught:
                client.call_tool('search', {})
            self.assertEqual(caught.exception.code, 'session_mismatch')
        client = self.client(Transport(initialization(None), reply(status=202, raw=b''),
            reply({}, rpc_id=3, headers={'Mcp-Session-Id': 'unexpected'})))
        with self.assertRaises(RemoteError):
            client.call_tool('search', {})

    def test_credentials_are_redacted_in_values_keys_and_encoded_urls(self):
        raw_result = {'host/secret+token': ['host/secret+token', 'https://example.org/?value=host%2Fsecret%2Btoken', 'session-123']}
        transport = Transport(initialization(), reply(status=202, raw=b''), reply(raw_result, rpc_id=3))
        client = self.client(transport, headers_env={'Authorization': 'SECRET_ENV'})
        result = client.call_tool('search', {})
        self.assertEqual(transport.calls[0]['headers']['Authorization'], 'Bearer host/secret+token')
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('session-123', json.dumps(result))

    def test_http_rejection_is_known_but_transport_failure_is_unknown(self):
        for status, code in ((401, 'authorization_required'), (403, 'authorization_required'), (429, 'rate_limited')):
            client = self.client(Transport(initialization(), reply(status=202, raw=b''), reply(status=status)))
            with self.assertRaises(RemoteError) as caught:
                client.call_tool('search', {})
            self.assertFalse(caught.exception.dispatched)
            self.assertEqual(caught.exception.code, code)
        client = self.client(Transport(initialization(), reply(status=202, raw=b''), TimeoutError('credential secret')))
        with self.assertRaises(RemoteError) as caught:
            client.call_tool('search', {})
        self.assertTrue(caught.exception.dispatched)
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(client.attempt_count, 3)

    def test_rpc_error_does_not_echo_provider_error_or_arbitrary_code(self):
        body = json.dumps({'jsonrpc': '2.0', 'id': 3, 'error': {'code': 'secret', 'message': 'secret'}}).encode()
        client = self.client(Transport(initialization(), reply(status=202, raw=b''), reply(raw=body)))
        with self.assertRaises(RemoteError) as caught:
            client.call_tool('search', {})
        self.assertEqual(caught.exception.code, 'rpc_error')
        self.assertNotIn('secret', str(caught.exception))

    def test_protocol_and_transport_schema_are_checked(self):
        for initial in (reply({'protocolVersion': 'unexpected', 'capabilities': {}}),
                        reply(raw=b'not JSON'), {'status': 200, 'body': 'not bytes'}):
            client = self.client(Transport(initial))
            with self.assertRaises(RemoteError):
                client.list_tools()
            self.assertFalse(client.initialized)
            self.assertEqual(client.attempt_count, 1)

    def test_configuration_and_argument_errors_do_not_dispatch(self):
        transport = Transport()
        for endpoint in ('http://example.org/mcp', 'https://user:pass@example.org/mcp', 'https://example.org/mcp?token=secret'):
            with self.assertRaises(ValueError):
                self.client(transport, endpoint=endpoint)
        with self.assertRaises(RemoteError):
            self.client(transport, headers_env={'Authorization': 'MISSING'})
        for timeout in (float('nan'), 0, True, 121):
            with self.assertRaises(ValueError):
                self.client(transport, timeout=timeout)
        client = self.client(transport)
        with self.assertRaises(ValueError):
            client.call_tool('search', 'bad')
        with self.assertRaises(ValueError):
            client.list_tools(123)
        self.assertEqual(transport.calls, [])


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class Socket:
    def __init__(self):
        self.timeouts, self.shutdowns = [], []

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, how):
        self.shutdowns.append(how)


class Timer:
    instances = []

    def __init__(self, interval, callback):
        self.interval, self.callback, self.cancelled, self.started = interval, callback, False, False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class Response:
    status = 200

    def __init__(self, chunks, clock, delays=None, sock=True, headers=None):
        self.chunks, self.clock, self.delays = list(chunks), clock, list(delays or [])
        self.socket = Socket() if sock else None
        self.fp = types.SimpleNamespace(raw=types.SimpleNamespace(_sock=self.socket))
        self.headers, self.reads, self.closed = headers or {}, [], False

    def read1(self, size):
        self.reads.append(size)
        if self.delays:
            self.clock.now += self.delays.pop(0)
        item = self.chunks.pop(0) if self.chunks else b''
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item()
        return item

    def read(self, size):
        raise AssertionError('Unbounded read fallback must never run')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        Timer.instances = []
        self.time_patch = patch('rsi_literature.remote.time.monotonic', self.clock)
        self.timer_patch = patch('rsi_literature.remote.threading.Timer', Timer)
        self.time_patch.start(); self.timer_patch.start()
        self.addCleanup(self.time_patch.stop); self.addCleanup(self.timer_patch.stop)

    def call(self, response, timeout=1, max_bytes=100, open_delay=0):
        def open_response(*args, **kwargs):
            self.clock.now += open_delay
            if isinstance(response, Exception):
                raise response
            return response
        with patch('rsi_literature.remote.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = open_response
            return http_request('GET', 'https://example.org/api?api_key=secret', {}, None, timeout, max_bytes)

    def test_bounded_chunks_share_one_deadline_including_open(self):
        response = Response([b'abc', b'def', b''], self.clock, delays=[.1, .1, .1])
        result = self.call(response, open_delay=.2)
        self.assertEqual(result['body'], b'abcdef')
        self.assertEqual(response.reads, [101, 98, 95])
        self.assertAlmostEqual(response.socket.timeouts[0], .8)
        self.assertAlmostEqual(response.socket.timeouts[-1], .6)
        self.assertTrue(response.closed)
        self.assertTrue(Timer.instances[0].cancelled)

    def test_trickling_stream_cannot_reset_total_time_budget(self):
        response = Response([b'a', b'b', b'c'], self.clock, delays=[.6, .6, .6], headers={'Content-Type': 'text/event-stream'})
        with self.assertRaises(RemoteError) as caught:
            self.call(response)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertTrue(caught.exception.dispatched)
        self.assertEqual(len(response.reads), 2)
        self.assertTrue(Timer.instances[0].cancelled)

    def test_known_content_length_does_not_touch_closed_socket_after_final_chunk(self):
        response = Response([], self.clock)
        response.length = 4
        def final_chunk():
            response.length = 0
            response.socket.settimeout = lambda _: self.fail('Socket is already closed by HTTPResponse')
            return b'body'
        response.chunks = [final_chunk]
        self.assertEqual(self.call(response)['body'], b'body')
        self.assertEqual(len(response.reads), 1)

    def test_real_stdlib_response_handles_content_length_and_chunked_eof(self):
        # A local socket pair exercises HTTPResponse/read1/socket lifetime with
        # no listener, DNS lookup, external request or third-party dependency.
        for framing in (b'Content-Length: 4\r\n\r\nbody',
                        b'Transfer-Encoding: chunked\r\n\r\n4\r\nbody\r\n0\r\n\r\n'):
            with self.subTest(framing=framing):
                reader, writer = socket.socketpair()
                try:
                    writer.sendall(b'HTTP/1.1 200 OK\r\n' + framing)
                    writer.shutdown(socket.SHUT_WR)
                    response = http.client.HTTPResponse(reader)
                    response.begin()
                    self.assertEqual(self.call(response)['body'], b'body')
                finally:
                    reader.close()
                    writer.close()

    def test_socket_watchdog_interrupts_internal_reads(self):
        def fire_watchdog():
            Timer.instances[0].callback()
            return b'a'
        response = Response([fire_watchdog], self.clock)
        with self.assertRaises(RemoteError) as caught:
            self.call(response)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(len(response.socket.shutdowns), 1)
        self.assertTrue(Timer.instances[0].cancelled)

    def test_missing_socket_or_read1_never_falls_back_to_blocking_read(self):
        for missing in ('socket', 'read1'):
            response = Response([b'body'], self.clock, sock=missing != 'socket')
            if missing == 'read1':
                response.read1 = None
            with self.assertRaises(RemoteError) as caught:
                self.call(response)
            self.assertEqual(caught.exception.code, 'timeout_control_unavailable')
            self.assertEqual(response.reads, [])
            self.assertTrue(response.closed)

    def test_known_empty_body_needs_no_socket(self):
        response = Response([], self.clock, sock=False, headers={'Content-Length': '0'})
        self.assertEqual(self.call(response)['body'], b'')
        self.assertEqual(response.reads, [])

    def test_size_limit_applies_before_returning_partial_data(self):
        response = Response([b'1234', b'56'], self.clock)
        with self.assertRaises(RemoteError) as caught:
            self.call(response, max_bytes=5)
        self.assertEqual(caught.exception.code, 'response_limit')
        self.assertEqual(response.reads, [6, 2])
        self.assertTrue(Timer.instances[0].cancelled)

    def test_read_and_open_failures_remain_dispatched_and_sanitized(self):
        for response in (Response([TimeoutError('api_key=secret')], self.clock),
                         urllib.error.URLError('api_key=secret')):
            with self.assertRaises(RemoteError) as caught:
                self.call(response)
            self.assertTrue(caught.exception.dispatched)
            self.assertNotIn('secret', str(caught.exception))

    def test_open_consuming_budget_prevents_any_body_read(self):
        response = Response([b'body'], self.clock)
        with self.assertRaises(RemoteError) as caught:
            self.call(response, open_delay=2)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(response.reads, [])

    def test_http_error_body_is_not_read_and_handle_is_closed(self):
        body = io.BytesIO(b'credential secret')
        error = urllib.error.HTTPError('https://example.org/', 401, 'Denied', {}, body)
        result = self.call(error)
        self.assertEqual(result['status'], 401)
        self.assertEqual(result['body'], b'')
        self.assertTrue(body.closed)


if __name__ == '__main__':
    unittest.main()
