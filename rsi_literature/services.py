"""Elicit v2 and bounded Edison Literature jobs, with explicit async lifecycle.

run_service accepts action search/start/status/result. Each call performs one
lifecycle step; it never polls or re-submits a job automatically. The controller
owns effect replay/idempotency. A missing creation receipt is execution_unknown.

Edison is pinned to the inspected edison-client 0.17.0 wire contract. Its normal
SDK retries and automatic parallel blob downloads are deliberately bypassed:
create_task/get_task(lite=True) use a bounded HTTP bridge, and result reads only
the current environment_frame blob through the same bounded transport.

Sources checked 2026-11-15:
https://docs.elicit.com/ (production API v2, /search/papers, /sessions/reports)
https://github.com/EdisonScientific/edison-client-docs/blob/main/README.md
https://pypi.org/project/edison-client/0.17.0/ (clients/rest_client.py,
clients/state_blobs.py, utils/auth.py, models/app.py in the published wheel)
"""
from __future__ import annotations

import gzip
import hashlib
import importlib
import importlib.metadata
import inspect
import io
import json
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timezone

from .remote import http_request


ELICIT_BASE = 'https://elicit.com/api/v2'
EDISON_BASE = 'https://api.platform.edisonscientific.com'
EDISON_SDK_VERSION = '0.17.0'
MAX_BYTES = 8 * 1024 * 1024
_SECRET_KEYS = {'api_key', 'apikey', 'access_token', 'token', 'authorization', 'jwt',
                'password', 'download_url', 'answer_reasoning'}


class ServiceError(Exception):
    def __init__(self, code, detail, *, status='failed', retryability='no', execution_unknown=False):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status
        self.retryability, self.execution_unknown = retryability, execution_unknown


def service_max_requests(provider, action):
    if provider == 'elicit' and action in {'search', 'start', 'status', 'result'}:
        return 1
    if provider == 'edison' and action in {'start', 'status', 'result'}:
        return 3 if action == 'result' else 2
    return 0


def _error(code, detail, **kwargs):
    raise ServiceError(code, detail, **kwargs)


def _string(value, name, maximum=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _error('invalid_input', name + ' must be a bounded nonempty string')
    return value.strip()


def _int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        _error('invalid_input', f'{name} must be an integer from {low} to {high}')
    return value


def _id(value, name):
    try:
        return str(uuid.UUID(_string(value, name, 64)))
    except ValueError:
        _error('invalid_input', name + ' must be a UUID returned by the service')


def _safe_url(value):
    try:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            return None
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if any(k.lower() in _SECRET_KEYS or k.lower().startswith(('x-amz-', 'x-goog-'))
               or k.lower() in {'signature', 'sig', 'key', 'email'} for k, _ in query):
            return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        return value
    except (ValueError, TypeError):
        return None


def _clean(value, secrets):
    if isinstance(value, dict):
        return {_clean(str(k), secrets): _clean(v, secrets) for k, v in value.items() if str(k).lower() not in _SECRET_KEYS}
    if isinstance(value, list):
        return [_clean(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, '[REDACTED]') if secret else value
        # Includes signed URLs embedded inside report markdown or JSON strings.
        return re.sub(r'https?://[^\s<>"\)]+', lambda m: _safe_url(m.group()) or '[REDACTED_URL]', value)
    return value


def _envelope(provider, effect_id):
    return {'status': 'failed', 'data': {}, 'provenance': {'provider': provider, 'effect_id': effect_id,
            'attempt_count': 0, 'requests': []}, 'coverage': {'interpretive_output': True,
            'original_fulltext_read': False, 'absence_is_negative_evidence': False,
            'signed_urls_redacted': True},
            'continuation': {'next_cursor': None, 'complete_for_query': False}, 'errors': [],
            'execution_unknown': False}


class _Session:
    def __init__(self, provider, settings, result, transport, key):
        self.provider, self.settings, self.result, self.transport = provider, settings, result, transport
        self.secrets, self.key = [key], key
        self.timeout = settings.get('timeout', 30)
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not 0 < self.timeout <= 60:
            _error('invalid_configuration', 'timeout must be from 0 to 60 seconds')
        self.max_bytes = _int(settings.get('max_response_bytes', 2 * 1024 * 1024), 'max_response_bytes', 1024, MAX_BYTES)
        self.max_requests = service_max_requests(provider, settings['_action'])
        self.creation_dispatched = False
        self.creation_accepted = False

    def request(self, method, url, payload=None, *, token=None, creation=False, blob=False):
        records = self.result['provenance']['requests']
        if len(records) >= self.max_requests:
            _error('request_limit', 'Service operation exceeded its admitted HTTP request count')
        if blob:
            parsed = urllib.parse.urlsplit(url)
            allowed = self.settings.get('blob_hosts', ['storage.googleapis.com'])
            if (not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed)
                    or parsed.scheme != 'https' or parsed.hostname not in allowed
                    or parsed.username or parsed.password or parsed.port not in {None, 443}):
                _error('blob_host_not_allowed', 'Configure the exact trusted object-storage host in blob_hosts', status='unavailable', retryability='after_configuration')
        elif not url.startswith((ELICIT_BASE if self.provider == 'elicit' else EDISON_BASE) + '/'):
            _error('invalid_endpoint', 'Service requests must stay on the fixed official API origin')
        headers = {'Accept': 'application/json', 'User-Agent': 'RSI-Lab-literature/1.0'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        body = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        receipt = {'method': method, 'url': _safe_url(url), 'attempted_at': datetime.now(timezone.utc).isoformat(),
                   'status_code': None, 'bytes_received': None, 'response_sha256': None}
        records.append(receipt)
        self.result['provenance']['attempt_count'] = len(records)
        if creation:
            self.creation_dispatched = True
        try:
            reply = self.transport(method, url, headers, body, self.timeout, self.max_bytes)
        except Exception:
            _error('transport_failed', 'No trustworthy service response was received', retryability='reconcile_before_retry' if creation else 'yes', execution_unknown=creation)
        if not isinstance(reply, dict) or type(reply.get('status')) is not int or not isinstance(reply.get('body'), bytes):
            _error('invalid_response', 'Service transport response is malformed', execution_unknown=creation)
        raw, status = reply['body'], reply['status']
        receipt.update(status_code=status, bytes_received=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
        if len(raw) > self.max_bytes:
            _error('response_limit', 'Service response exceeded the byte limit', execution_unknown=creation)
        if not 200 <= status < 300:
            _error('http_' + str(status), 'Service returned HTTP ' + str(status),
                   status='unavailable' if status in {401, 403, 404} else 'failed',
                   retryability='reconcile_before_retry' if creation and status >= 500 else 'yes' if status in {429, 500, 502, 503, 504} else 'after_configuration' if status in {401, 403} else 'no',
                   execution_unknown=creation and status >= 500)
        if blob:
            return raw
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            _error('invalid_response', 'Service returned malformed JSON', execution_unknown=creation)
        if not isinstance(data, dict):
            _error('invalid_response', 'Service response is not an object', execution_unknown=creation)
        return data


def _report_document(session, store, text, remote_id):
    if not isinstance(text, str) or not text:
        return None
    if store is None:
        return None
    # A generated synthesis is an excerpt artifact, never a paper's full text.
    return store.ingest(text.encode(), 'text/markdown', {'provider': session.provider,
        'remote_id': remote_id, 'content_scope': 'excerpt', 'artifact_role': 'generated_report',
        'interpretive_output': True, 'original_source': False})


def _elicit(session, inputs, store):
    action = session.settings['_action']
    if action == 'search':
        allowed = {'action', 'query', 'limit', 'corpus', 'search_mode', 'filters'}
        if set(inputs) - allowed:
            _error('invalid_input', 'Unsupported Elicit search field')
        payload = {'query': _string(inputs.get('query'), 'query'), 'maxResults': _int(inputs.get('limit', 10), 'limit', 1, 100)}
        for field, wire, valid in [('corpus', 'corpus', {'elicit', 'pubmed'}), ('search_mode', 'searchMode', {'semantic', 'keyword'})]:
            if field in inputs:
                if inputs[field] not in valid:
                    _error('invalid_input', 'Invalid ' + field)
                payload[wire] = inputs[field]
        filters = inputs.get('filters')
        if filters is not None:
            if not isinstance(filters, dict) or set(filters) - {'minYear', 'maxYear', 'hasPdf', 'includeKeywords', 'excludeKeywords', 'typeTags', 'journal', 'maxQuartile', 'retracted', 'minEpochS', 'maxEpochS', 'pubmedOnly'}:
                _error('invalid_input', 'Unsupported Elicit search filter')
            if payload.get('searchMode') == 'keyword' and filters:
                _error('invalid_input', 'Keyword search cannot be combined with Elicit filters')
            if payload.get('corpus') == 'pubmed' and filters.get('journal'):
                _error('invalid_input', 'Journal filtering is available only for the Elicit corpus')
            payload['filters'] = filters
        raw = session.request('POST', ELICIT_BASE + '/search/papers', payload, token=session.key)
        rows, warnings = raw.get('papers'), raw.get('warnings', [])
        if not isinstance(rows, list) or len(rows) > payload['maxResults'] or not isinstance(warnings, list):
            _error('invalid_response', 'Elicit search papers/warnings are missing or malformed')
        papers = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('title'), str):
                _error('invalid_response', 'Elicit search returned malformed paper metadata')
            pid = row.get('elicitId') or ('doi:' + row['doi'] if row.get('doi') else 'pmid:' + row['pmid'] if row.get('pmid') else None)
            fulltext = _safe_url(row.get('fullTextUrl'))
            paper = {**row, 'provider': 'elicit', 'provider_id': row.get('elicitId'), 'paper_id': 'elicit:' + pid if pid else None,
                     'content_scope': 'abstract' if row.get('abstract') else 'metadata',
                     'fulltext_candidates': [{'url': fulltext, 'format': 'unknown', 'content_scope': 'fulltext_location', 'retrieved': False}] if fulltext else []}
            papers.append(paper)
        session.result.update(status='partial' if warnings or len(rows) == payload['maxResults'] else 'ok',
                              data={'papers': papers, 'warnings': warnings})
        session.result['coverage'].update(returned=len(rows), search_result_cap=payload['maxResults'],
            corpus_complete=False, provider_pagination_supported=False, interpretive_output=False)
        session.result['continuation']['complete_for_query'] = False  # Top-k semantic search has no exhaustive cursor.
        return
    allowed = {'action', 'query', 'title', 'max_search_papers', 'max_extract_papers'} if action == 'start' else {'action', 'session_id'}
    if set(inputs) - allowed:
        _error('invalid_input', 'Unsupported Elicit report field')
    if action == 'start':
        payload = {'researchQuestion': _string(inputs.get('query'), 'query'), 'isPublic': False,
                   'maxSearchPapers': _int(inputs.get('max_search_papers', 50), 'max_search_papers', 1, 1000),
                   'maxExtractPapers': _int(inputs.get('max_extract_papers', 10), 'max_extract_papers', 1, 100)}
        if payload['maxExtractPapers'] > payload['maxSearchPapers']:
            _error('invalid_input', 'max_extract_papers cannot exceed max_search_papers')
        if 'title' in inputs:
            payload['title'] = _string(inputs['title'], 'title', 500)
        raw = session.request('POST', ELICIT_BASE + '/sessions/reports', payload, token=session.key, creation=True)
        sid = _id(raw.get('sessionId'), 'session_id')
        session.creation_accepted = True
        # Keep the known remote handle even if later state/schema validation
        # fails, so the controller can reconcile it instead of creating again.
        session.result['data'] = {'session_id': sid}
    else:
        sid = _id(inputs.get('session_id'), 'session_id')
        raw = session.request('GET', ELICIT_BASE + '/sessions/reports/' + sid + ('?include=reportBody' if action == 'result' else ''), token=session.key)
        if raw.get('sessionId') != sid:
            _error('identity_mismatch', 'Elicit report identity differs from the requested session')
    state = raw.get('status')
    if state not in {'processing', 'pausedForInsufficientQuota', 'completed', 'failed', 'unknown'}:
        _error('invalid_response', 'Elicit report state is absent or unknown')
    clean = _clean(raw, session.secrets)
    report = clean.get('result') or {}
    body = report.get('reportBody') if isinstance(report, dict) else None
    artifact = _report_document(session, store, body, sid)
    session.result['data'] = {'session_id': sid, 'remote_status': state, 'provider_result': clean,
                              'report_document': artifact}
    session.result['status'] = 'failed' if state == 'failed' else 'unavailable' if state == 'pausedForInsufficientQuota' else 'ok' if state == 'completed' and (action != 'result' or body) else 'partial'
    session.result['coverage'].update(report_body_returned=bool(body), provider_export_status=report.get('exportsStatus') if isinstance(report, dict) else None)
    session.result['continuation'].update(session_id=sid, action='result' if state == 'completed' else 'status',
        poll_after_seconds=60, complete_for_query=state == 'completed' and bool(body),
        automatic_polling_allowed=state == 'processing')
    if state in {'failed', 'pausedForInsufficientQuota', 'unknown'}:
        session.result['errors'].append({'code': 'remote_' + state, 'detail': 'Elicit report state is ' + state, 'retryability': 'after_configuration' if state == 'pausedForInsufficientQuota' else 'no'})


class _SDKResponse:
    """The small httpx response surface used by the pinned SDK lifecycle methods."""
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return self

    def iter_text(self, chunk_size=1024):
        yield json.dumps(self.payload)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _SDKHTTP:
    def __init__(self, session, token):
        self.session, self.token = session, token

    def post(self, path, json=None):
        if path != '/v0.1/crows':
            _error('sdk_protocol_changed', 'SDK attempted an unsupported write')
        return _SDKResponse(self.session.request('POST', EDISON_BASE + path, json, token=self.token, creation=True))

    def stream(self, method, path, params=None):
        if method != 'GET' or not re.fullmatch(r'/v0\.2/trajectories/[0-9a-f-]{36}', path):
            _error('sdk_protocol_changed', 'SDK attempted an unsupported read')
        return _SDKResponse(self.session.request('GET', EDISON_BASE + path + '?' + urllib.parse.urlencode(params or {}), token=self.token))


def _edison(session, inputs, store, module_loader):
    action = session.settings['_action']
    allowed = {'action', 'query', 'mode', 'continued_task_id', 'runtime_timeout'} if action == 'start' else {'action', 'task_id'}
    if set(inputs) - allowed:
        _error('invalid_input', 'Unsupported Edison literature field')
    if action == 'start':
        query = _string(inputs.get('query'), 'query')
        mode = inputs.get('mode', 'literature')
        if mode not in {'literature', 'literature_high'}:
            _error('invalid_input', 'Only literature and literature_high jobs are enabled')
        runtime = {'timeout': _int(inputs.get('runtime_timeout', 1200), 'runtime_timeout', 1, 3600)}
        if 'continued_task_id' in inputs:
            runtime['continued_job_id'] = _id(inputs['continued_task_id'], 'continued_task_id')
    else:
        task_id = _id(inputs.get('task_id'), 'task_id')
    try:
        # Edison imports LiteLLM through its dependencies. Its import-time cost
        # map fetch is outside the admitted request budget; use the bundled map.
        os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
        module = module_loader('edison_client')
        version = getattr(module, '__version__', None) or importlib.metadata.version('edison-client')
    except (ImportError, importlib.metadata.PackageNotFoundError):
        _error('dependency_unavailable', 'Install edison-client==0.17.0 in the host runtime', status='unavailable', retryability='after_configuration')
    if version != EDISON_SDK_VERSION:
        _error('sdk_version_unsupported', 'This bounded adapter requires edison-client==0.17.0', status='unavailable', retryability='after_configuration')
    token_data = session.request('POST', EDISON_BASE + '/auth/login', {'api_key': session.key})
    token = token_data.get('access_token')
    if not isinstance(token, str) or not token:
        _error('authentication_failed', 'Edison did not return an access token', status='unavailable', retryability='after_configuration')
    session.secrets.append(token)
    bridge = _SDKHTTP(session, token)

    class BoundedClient(module.EdisonClient):
        def get_client(self, *args, **kwargs):
            return bridge

    client = BoundedClient(api_key=session.key, cleanup_on_exit=False)
    session.result['provenance']['sdk_version'] = version
    try:
        if action == 'start':
            job = module.JobNames.LITERATURE_HIGH if mode == 'literature_high' else module.JobNames.LITERATURE
            # The published SDK decorates this with tenacity. Unwrap once at
            # the call site instead of mutating a shared/global retry policy.
            raw_id = inspect.unwrap(type(client).create_task)(client, {'name': job, 'query': query, 'runtime_config': runtime})
            task_id = _id(str(raw_id), 'task_id')
            session.creation_accepted = True
            session.result.update(status='partial', data={'task_id': task_id, 'remote_status': 'submitted', 'mode': mode})
            session.result['continuation'].update(task_id=task_id, action='status', poll_after_seconds=30)
            return
        if action == 'status':
            response = inspect.unwrap(type(client).get_task)(client, task_id, lite=True)
            raw = response.model_dump(mode='json') if hasattr(response, 'model_dump') else response
        else:
            raw = session.request('GET', EDISON_BASE + '/v0.2/trajectories/' + task_id + '?history=false&lite=false', token=token)
        if not isinstance(raw, dict):
            _error('invalid_response', 'Edison returned an invalid task response')
        returned_id = raw.get('task_id') or raw.get('id')
        if str(returned_id) != task_id:
            _error('identity_mismatch', 'Edison task identity differs from the requested task')
        state = raw.get('status')
        if not isinstance(state, str) or not state:
            _error('invalid_response', 'Edison task status is missing')
        frame = raw.get('environment_frame')
        if action == 'result' and isinstance(frame, dict) and frame.get('state') is None and frame.get('download_url'):
            blob = session.request('GET', frame['download_url'], blob=True)
            if blob.startswith(b'\x1f\x8b'):
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
                        blob = stream.read(session.max_bytes + 1)
                except (OSError, EOFError):
                    _error('invalid_blob', 'Edison state blob is not valid gzip')
            if len(blob) > session.max_bytes:
                _error('response_limit', 'Expanded Edison state exceeds the byte limit')
            try:
                frame = {**frame, 'state': json.loads(blob)}
            except (ValueError, UnicodeError):
                _error('invalid_blob', 'Edison state blob is not valid JSON')
        # Never hydrate or return model trajectories; the environment contains
        # the delivered answer, contexts and paper metadata rather than history.
        raw = {k: v for k, v in raw.items() if k not in {'agent_state', 'environment_frame'}}
        if isinstance(frame, dict):
            raw['environment_frame'] = frame
        clean = _clean(raw, session.secrets)
        state_data = (frame or {}).get('state', {}) if isinstance(frame, dict) else {}
        answer = state_data.get('state', {}).get('response', {}).get('answer', {}) if isinstance(state_data, dict) else {}
        body = answer.get('formatted_answer') or answer.get('answer') if isinstance(answer, dict) else None
        body = _clean(body, session.secrets)
        artifact = _report_document(session, store, body, task_id)
        success = state.lower() == 'success'
        failed = state.lower() in {'failed', 'failure', 'error', 'cancelled', 'canceled'}
        answer_success = answer.get('has_successful_answer') if isinstance(answer, dict) else None
        session.result.update(status='failed' if failed else 'ok' if success and (action == 'status' or body) and answer_success is not False else 'partial',
            data={'task_id': task_id, 'remote_status': state, 'provider_result': clean, 'report_document': artifact})
        session.result['coverage'].update(report_body_returned=bool(body), agent_history_retrieved=False,
            environment_frame_returned=isinstance(frame, dict), has_successful_answer=answer_success)
        session.result['continuation'].update(task_id=task_id, action='result' if success else 'status', poll_after_seconds=30,
            complete_for_query=success and bool(body), automatic_polling_allowed=not (success or failed))
        if failed:
            session.result['errors'].append({'code': 'remote_job_failed', 'detail': 'Edison reported a failed literature job', 'retryability': 'no'})
        elif answer_success is False:
            session.result['errors'].append({'code': 'answer_unsuccessful', 'detail': 'Edison completed but did not find a successful answer', 'retryability': 'other_query'})
    finally:
        client.close()


def run_service(provider, settings, inputs, document_store=None, effect_id=None, *, transport=None, env=None, module_loader=None):
    """One admitted lifecycle step, with host-only settings and injected tests.

    Credentials: settings.credential_env is an environment *name*, defaulting to
    ELICIT_API_KEY / EDISON_API_KEY. Raw keys and endpoints are never model inputs.
    Elicit: action=search/start/status/result, status/result need session_id.
    Edison: action=start/status/result, status/result need task_id. Start can use
    mode=literature_high and continued_task_id; all other job types are rejected.
    """
    result = _envelope(provider, effect_id)
    session = None
    try:
        if not isinstance(settings, dict) or not isinstance(inputs, dict):
            _error('invalid_input', 'settings and inputs must be objects')
        action = inputs.get('action', 'start')
        if not service_max_requests(provider, action):
            _error('unsupported_operation', 'This service/action is not supported', status='unavailable')
        if any(k in settings for k in ('api_key', 'token', 'headers', 'endpoint')):
            _error('invalid_configuration', 'Use environment-backed credentials and fixed official endpoints')
        name = settings.get('credential_env', provider.upper() + '_API_KEY')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', name):
            _error('invalid_configuration', 'credential_env must name a host environment variable')
        host_env = os.environ if env is None else env
        key = host_env.get(name)
        if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
            _error('configuration_required', 'Configure ' + name + ' on the host', status='unavailable', retryability='after_configuration')
        session = _Session(provider, {**settings, '_action': action}, result, transport or http_request, key)
        if provider == 'elicit':
            _elicit(session, inputs, document_store)
        else:
            _edison(session, inputs, document_store, module_loader or importlib.import_module)
    except ServiceError as exc:
        result['status'] = exc.status
        result['execution_unknown'] = exc.execution_unknown or bool(session and session.creation_dispatched and not session.creation_accepted and exc.code not in {'http_400', 'http_401', 'http_403', 'http_404', 'http_409', 'http_422', 'http_429'})
        result['errors'].append({'code': exc.code, 'detail': exc.detail,
                                 'retryability': 'reconcile_before_retry' if result['execution_unknown'] else exc.retryability})
    except Exception:
        result['status'] = 'failed'
        result['execution_unknown'] = bool(session and session.creation_dispatched and not session.creation_accepted)
        result['errors'].append({'code': 'service_failure', 'detail': 'The service response or installed SDK did not match the supported contract',
            'retryability': 'reconcile_before_retry' if result['execution_unknown'] else 'unknown'})
    return _clean(result, session.secrets if session else [])
