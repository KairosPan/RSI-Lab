"""Bounded HTTP MCP calls to explicitly configured literature servers.

OAuth tokens belong to the host environment. Desktop connector authentication
is not copied into RSI-Lab. Tools are restricted to a host-owned read/research
allowlist; server responses never grant execution permissions.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class RemoteError(ValueError):
    def __init__(self, code, message, *, dispatched=False):
        super().__init__(message)
        self.code, self.dispatched = code, dispatched


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_request(method, url, headers, body, timeout, max_bytes):
    """Read at most max_bytes under one deadline, without redirecting.

    read1 avoids read(n)'s repeated socket fills. A socket shutdown watchdog
    also interrupts slow chunk headers/TLS reads whose internal reads can reset
    a socket inactivity timeout. Unknown response/socket wrappers fail closed;
    there is no unbounded read fallback or background request left running.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 120):
        raise ValueError('HTTP timeout must be finite and between 0 and 120 seconds')
    if type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024:
        raise ValueError('HTTP byte limit must be from 1 byte to 32 MiB')
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)
    except urllib.error.HTTPError as response:
        with response:
            return {'status': response.code, 'headers': dict(response.headers), 'body': b''}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Never serialize urllib's exception, which can contain a credential URL.
        raise RemoteError('transport_failed', 'Remote response was not received', dispatched=True) from exc
    try:
        with response:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteError('timeout', 'Remote request exceeded its total time limit', dispatched=True)
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            read = getattr(response, 'read1', None)
            if not callable(read) or not callable(getattr(sock, 'settimeout', None)) or not callable(getattr(sock, 'shutdown', None)):
                # Responses with no body do not need any potentially blocking
                # operation. Otherwise a timeout-capable socket is mandatory.
                if method.upper() == 'HEAD' or response.status in {204, 304} or response.headers.get('Content-Length') == '0':
                    return {'status': response.status, 'headers': dict(response.headers), 'body': b''}
                raise RemoteError('timeout_control_unavailable', 'Cannot enforce a total timeout on this response stream', dispatched=True)
            expired = threading.Event()

            def abort_read():
                expired.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(remaining, abort_read)
            timer.daemon = True
            timer.start()
            chunks, size = [], 0
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or expired.is_set():
                        raise RemoteError('timeout', 'Remote response exceeded its total time limit', dispatched=True)
                    sock.settimeout(remaining)
                    try:
                        chunk = read(min(65536, max_bytes + 1 - size))
                    except (urllib.error.URLError, TimeoutError, OSError) as exc:
                        if expired.is_set() or time.monotonic() >= deadline:
                            raise RemoteError('timeout', 'Remote response exceeded its total time limit', dispatched=True) from exc
                        raise
                    if expired.is_set() or time.monotonic() >= deadline:
                        raise RemoteError('timeout', 'Remote response exceeded its total time limit', dispatched=True)
                    if not isinstance(chunk, bytes):
                        raise RemoteError('invalid_response', 'Remote body reader returned invalid bytes', dispatched=True)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise RemoteError('response_limit', 'Remote response exceeded its byte limit', dispatched=True)
                    # HTTPResponse closes its file/socket as soon as a known
                    # Content-Length is consumed. Do not settimeout on that
                    # now-closed descriptor just to ask for another EOF read.
                    if getattr(response, 'length', None) == 0 and not getattr(response, 'chunked', False):
                        break
                return {'status': response.status, 'headers': dict(response.headers), 'body': b''.join(chunks)}
            finally:
                timer.cancel()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RemoteError('transport_failed', 'Remote response body was not received', dispatched=True) from exc


def validate_endpoint(endpoint):
    parsed = urllib.parse.urlsplit(endpoint)
    local = parsed.hostname in {'localhost', '127.0.0.1', '::1'}
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and local)) or not parsed.hostname:
        raise ValueError('Remote endpoint needs HTTPS (HTTP is allowed only for an explicit local service)')
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError('Put credentials in environment-backed headers, never in endpoint URLs')
    return endpoint


def environment_headers(settings, env=None):
    env = os.environ if env is None else env
    result = {}
    for header, env_name in settings.get('headers_env', {}).items():
        if not re.fullmatch(r'[A-Za-z0-9-]{1,100}', header) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env_name):
            raise ValueError('Invalid environment-backed header binding')
        value = env.get(env_name)
        if not value:
            raise RemoteError('missing_credentials', 'Set environment variable ' + env_name)
        if '\r' in value or '\n' in value:
            raise RemoteError('invalid_credentials', 'Credential contains an invalid header character')
        result[header] = ('Bearer ' + value) if header.lower() == 'authorization' else value
    return result


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[REDACTED]')
                value = value.replace(urllib.parse.quote(secret, safe=''), '[REDACTED]')
                value = value.replace(urllib.parse.quote_plus(secret), '[REDACTED]')
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    return value


class MCPClient:
    def __init__(self, settings, *, transport=None, env=None):
        self.settings = settings
        self.endpoint = validate_endpoint(settings['endpoint'])
        self.transport = transport or http_request
        self.headers = environment_headers(settings, env)
        self.secrets = [value.removeprefix('Bearer ') for value in self.headers.values()]
        self.timeout = settings.get('timeout', 30)
        self.max_bytes = settings.get('max_response_bytes', 2 * 1024 * 1024)
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or not 0 < self.timeout <= 120):
            raise ValueError('MCP timeout must be finite and between 0 and 120 seconds')
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 32 * 1024 * 1024:
            raise ValueError('MCP byte limit must be from 1 byte to 32 MiB')
        self.session_id = None
        self.protocol = '2025-03-26'
        self.next_id = 0
        self.attempt_count = 0
        self.initialized = False

    def _request(self, method, params, notification=False):
        self.next_id += 1
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            message['id'] = self.next_id
        headers = {**self.headers, 'Content-Type': 'application/json',
                   'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': self.protocol}
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode()
        self.attempt_count += 1
        try:
            reply = self.transport('POST', self.endpoint, headers, body, self.timeout, self.max_bytes)
        except RemoteError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RemoteError('transport_failed', 'Remote response was not received', dispatched=True) from exc
        if (not isinstance(reply, dict) or type(reply.get('status')) is not int
                or not isinstance(reply.get('headers', {}), dict) or not isinstance(reply.get('body'), bytes)):
            raise RemoteError('invalid_response', 'Invalid MCP transport response', dispatched=True)
        status = reply['status']
        if status in {401, 403}:
            raise RemoteError('authorization_required', 'The server requires host authentication/access')
        if status == 429:
            raise RemoteError('rate_limited', 'The server returned HTTP 429; retry separately within budget')
        if not 200 <= status < 300:
            raise RemoteError('http_error', 'The server returned HTTP ' + str(status), dispatched=status >= 500)
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in reply.get('headers', {}).items()):
            raise RemoteError('invalid_response', 'Invalid MCP response headers', dispatched=True)
        normalized_headers = {key.lower(): value for key, value in reply.get('headers', {}).items()}
        new_session = normalized_headers.get('mcp-session-id')
        if new_session is not None:
            if not re.fullmatch(r'[!-~]{1,512}', new_session) or self.session_id is not None and self.session_id != new_session:
                raise RemoteError('session_mismatch', 'MCP server changed or malformed its session binding', dispatched=True)
            if self.session_id is None and method != 'initialize':
                raise RemoteError('session_mismatch', 'MCP server introduced a session outside initialization', dispatched=True)
            self.session_id = new_session
            self.secrets.append(new_session)
        raw = reply['body']
        if len(raw) > self.max_bytes:
            raise RemoteError('response_limit', 'Remote response exceeded its byte limit', dispatched=True)
        if notification:
            return None
        try:
            if 'text/event-stream' in normalized_headers.get('content-type', ''):
                envelopes = []
                for event in raw.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
                    data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
                    if data:
                        envelopes.append(json.loads(data))
                matches = [item for item in envelopes if isinstance(item, dict) and item.get('id') == message['id']]
                if len(matches) != 1:
                    raise ValueError('Missing or duplicate RPC reply')
                envelope = matches[0]
            else:
                envelope = json.loads(raw)
            if (not isinstance(envelope, dict) or envelope.get('jsonrpc') != '2.0'
                    or type(envelope.get('id')) is not int or envelope.get('id') != message['id']):
                raise ValueError('Mismatched RPC identity')
            if 'error' in envelope:
                error = envelope['error']
                code = error.get('code') if isinstance(error, dict) else None
                raise RemoteError('rpc_error', 'Remote RPC error' + (' ' + str(code) if type(code) is int else ''), dispatched=True)
            if not isinstance(envelope.get('result'), dict):
                raise ValueError('Missing MCP result object')
            return redact(envelope['result'], self.secrets)
        except (KeyError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            if isinstance(exc, RemoteError):
                raise
            raise RemoteError('invalid_response', 'Invalid or mismatched MCP response', dispatched=True) from exc

    def initialize(self):
        if self.initialized:
            return
        result = self._request('initialize', {'protocolVersion': self.protocol, 'capabilities': {},
            'clientInfo': {'name': 'rsi-lab-literature', 'version': '1.0.0'}})
        if result.get('protocolVersion') != self.protocol or not isinstance(result.get('capabilities'), dict):
            raise RemoteError('unsupported_protocol', 'Server did not negotiate the supported MCP protocol', dispatched=True)
        self._request('notifications/initialized', {}, notification=True)
        self.initialized = True

    def list_tools(self, cursor=None):
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 8192):
            raise ValueError('MCP cursor must be bounded nonempty text')
        self.initialize()
        result = self._request('tools/list', {'cursor': cursor} if cursor else {})
        if (not isinstance(result.get('tools'), list) or any(not isinstance(tool, dict) or not isinstance(tool.get('name'), str) for tool in result['tools'])
                or result.get('nextCursor') is not None and not isinstance(result['nextCursor'], str)):
            raise RemoteError('invalid_response', 'MCP tool inventory is malformed', dispatched=True)
        allowed = set(self.settings.get('tools', []))
        return {'tools': [tool for tool in result.get('tools', []) if tool.get('name') in allowed],
                'next_cursor': result.get('nextCursor'), 'allowlist': sorted(allowed)}

    def call_tool(self, name, arguments):
        if name not in self.settings.get('tools', []):
            raise RemoteError('tool_not_allowed', 'Tool is outside this literature provider allowlist')
        if not isinstance(arguments, dict):
            raise ValueError('MCP arguments must be an object')
        self.initialize()
        return self._request('tools/call', {'name': name, 'arguments': arguments})
