"""Bounded, read-only literature providers; no ledger or filesystem access.

The four public operations return JSON-compatible evidence envelopes. Fulltext
candidates are locations, never evidence that a document was downloaded/read.
No provider failures are converted to an empty successful scientific result.

Transport injection: callable(method, url, headers, timeout, max_response_bytes)
returns {"status": int, "headers": dict, "body": bytes}. It must not redirect.
The default transport bounds bytes/time and rate-limits within this process.
Host configuration: NCBI_API_KEY / NCBI_EMAIL (optional), OPENALEX_API_KEY
(needed for continuation/content), SEMANTIC_SCHOLAR_API_KEY (optional), and
UNPAYWALL_EMAIL (required). Secrets are never included in result envelopes.

API references checked 2026-11-15:
https://dev.europepmc.org/RestfulWebService
https://www.ncbi.nlm.nih.gov/books/NBK25499/
https://pmc.ncbi.nlm.nih.gov/tools/cites-citedby/
https://help.openalex.org/api/ and https://help.openalex.org/access/fulltext/
https://api.semanticscholar.org/api-docs/graph (including single best-title match)
https://unpaywall.org/api (search endpoint retired 2026-09-18)
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone


BASES = {
    'europe_pmc': 'https://www.ebi.ac.uk/europepmc/webservices/rest/',
    'pubmed': 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/',
    'openalex': 'https://api.openalex.org/',
    'semantic_scholar': 'https://api.semanticscholar.org/graph/v1/',
    'unpaywall': 'https://api.unpaywall.org/v2/',
}
ALIASES = {'pubmed_pmc': 'pubmed', 'pmc': 'pubmed', 'europepmc': 'europe_pmc'}
SUPPORTED = {
    name: frozenset({'search', 'get_paper', 'get_citations', 'fulltext_candidates'})
    for name in BASES if name != 'unpaywall'
}
SUPPORTED['unpaywall'] = frozenset({'get_paper', 'fulltext_candidates'})
MAX_LIMIT = 100
MAX_TEXT = 32000
_RATE_LOCK = threading.Lock()
_LAST_REQUEST = {}
_SECRET_KEYS = {'api_key', 'apikey', 'key', 'token', 'access_token', 'email',
                'signature', 'x-amz-signature', 'authorization'}
_S2_FIELDS = 'title,abstract,year,authors,externalIds,url,openAccessPdf,citationCount,referenceCount'


class ProviderError(Exception):
    def __init__(self, code, detail, *, status='failed', retryability='unknown'):
        super().__init__(detail)
        self.code, self.detail = code, detail
        self.status, self.retryability = status, retryability


def operation_max_requests(provider, operation):
    """Upper bound for admission before dispatch, including metadata resolution.

    No automatic retries. Unpaywall search/citations are explicitly unsupported.
    """
    provider = ALIASES.get(provider, provider)
    if provider not in SUPPORTED or operation not in SUPPORTED[provider]:
        return 0
    if provider == 'pubmed':
        return 3 if operation == 'get_citations' else 2
    if operation == 'get_citations' and provider in {'europe_pmc', 'openalex'}:
        return 2
    return 1


def _fail(code, detail, **kwargs):
    raise ProviderError(code, detail, **kwargs)


def _text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail('invalid_input', f'{name} must be a nonempty string of at most {maximum} characters', retryability='no')
    return value.strip()


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        _fail('invalid_input', f'{name} must be an integer between {low} and {high}', retryability='no')
    return value


def _count(value):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if type(value) is int and value >= 0 else None


def _doi(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:)', '', value.strip(), flags=re.I)
    return value if len(value) <= 512 and re.fullmatch(r'10\.\d{4,9}/[^\s?#]+', value) else None


def _pmid(value):
    value = re.sub(r'^(?:PMID:|pubmed:)', '', str(value), flags=re.I)
    return value if re.fullmatch(r'[1-9]\d{0,11}', value) else None


def _pmcid(value):
    value = re.sub(r'^PMCID:', '', str(value), flags=re.I).upper()
    return value if re.fullmatch(r'PMC[1-9]\d{0,11}', value) else None


def _public_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme not in {'https', 'http'} or not parts.hostname or parts.username or parts.password:
            return None
        # A returned location containing credentials is not persisted. Hosts
        # supply their own credentials at fetch time, never through model text.
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if any(k.lower() in _SECRET_KEYS or k.lower().startswith('x-amz-') for k, _ in pairs):
            return None
        return value
    except ValueError:
        return None


def _safe_url(value):
    parts = urllib.parse.urlsplit(value)
    query = urllib.parse.urlencode([(k, '[REDACTED]' if k.lower() in _SECRET_KEYS else v)
                                    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)])
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ''))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _cursor(context, state):
    raw = json.dumps({'v': 1, 'context': _hash(context), 'state': state}, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _uncursor(value, context, default):
    if value is None:
        return default
    value = _text(value, 'cursor', 8192)
    try:
        data = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
    except (ValueError, UnicodeError):
        _fail('invalid_cursor', 'Cursor is not a valid continuation', retryability='no')
    if (not isinstance(data, dict) or set(data) != {'v', 'context', 'state'}
            or data['v'] != 1 or data['context'] != _hash(context)):
        _fail('invalid_cursor', 'Cursor belongs to a different operation, query, filters, or page size', retryability='no')
    return data['state']


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _fail('redirect_refused', 'Provider redirect was refused', retryability='no')


def http_transport(method, url, headers, timeout, max_response_bytes):
    """Fetch exactly one HTTPS API URL; callers cannot supply arbitrary origins."""
    request = urllib.request.Request(url, method=method, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect())
    start = time.monotonic()
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Never echo the provider's error body (it can contain credentials).
        result = {'status': exc.code, 'headers': dict(exc.headers), 'body': b''}
        exc.close()
        return result
    with response:
        chunks, size = [], 0
        while True:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                _fail('timeout', 'Provider response exceeded the time budget', retryability='yes')
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = getattr(response, 'read1', response.read)(min(65536, max_response_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_response_bytes:
                _fail('response_too_large', 'Provider response exceeded the byte limit', retryability='no')
        return {'status': response.status, 'headers': dict(response.headers), 'body': b''.join(chunks)}


def _paper(provider, provider_id, *, title=None, abstract=None, doi=None, pmid=None,
           pmcid=None, year=None, authors=None, url=None, candidates=None, **extra):
    if not isinstance(provider_id, str) or not provider_id:
        _fail('invalid_response', 'Provider returned a paper without an identity', retryability='unknown')
    title = title if isinstance(title, str) else None
    abstract = abstract if isinstance(abstract, str) else None
    result = {'provider': provider, 'provider_id': provider_id, 'paper_id': provider + ':' + provider_id,
              'title': title[:4096] if title else None, 'abstract': abstract[:MAX_TEXT] if abstract else None,
              'abstract_truncated': bool(abstract and len(abstract) > MAX_TEXT),
              'doi': _doi(doi), 'pmid': _pmid(pmid) if pmid else None, 'pmcid': _pmcid(pmcid),
              'year': _count(year), 'authors': (authors or [])[:500], 'url': _public_url(url),
              'content_scope': 'abstract' if abstract else 'metadata',
              'fulltext_candidates': candidates or []}
    result.update(extra)
    return result


def _candidate(url, fmt, *, license=None, availability='provider_reported', version=None):
    url = _public_url(url)
    if not url:
        return None
    return {'url': url, 'format': fmt, 'media_type': {'pdf': 'application/pdf', 'xml': 'application/xml',
             'tei': 'application/xml', 'html': 'text/html'}.get(fmt, 'application/octet-stream'),
            'content_scope': 'fulltext_location', 'availability': availability,
            'retrieved': False, 'license': license if isinstance(license, str) else None,
            'version': version if isinstance(version, str) else None}


def _locations(locations, *, pdf_key='pdf_url', landing_key='landing_page_url'):
    output, seen = [], set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        for key, fmt in ((pdf_key, 'pdf'), (landing_key, 'html')):
            candidate = _candidate(location.get(key), fmt, license=location.get('license'), version=location.get('version'))
            if candidate and candidate['url'] not in seen:
                seen.add(candidate['url'])
                output.append(candidate)
    return output


def _epmc_paper(item):
    if not isinstance(item, dict):
        _fail('invalid_response', 'Europe PMC paper is not an object')
    pid, source = item.get('id'), item.get('source')
    if not isinstance(pid, str) or not isinstance(source, str) or not re.fullmatch(r'[A-Z]+', source):
        _fail('invalid_response', 'Europe PMC paper has no source/id')
    candidates = []
    pmc = _pmcid(item.get('pmcid'))
    if pmc and item.get('isOpenAccess') == 'Y':
        candidates.append(_candidate(BASES['europe_pmc'] + pmc + '/fullTextXML', 'xml'))
    urls = item.get('fullTextUrlList', {}).get('fullTextUrl', [])
    for row in urls if isinstance(urls, list) else []:
        if isinstance(row, dict) and row.get('availability') in {'Open access', 'Free'}:
            candidate = _candidate(row.get('url'), 'pdf' if row.get('documentStyle') == 'pdf' else 'html')
            if candidate:
                candidates.append(candidate)
    authors = item.get('authorList', {}).get('author', [])
    names = [a.get('fullName') for a in authors if isinstance(a, dict) and isinstance(a.get('fullName'), str)]
    return _paper('europe_pmc', source + ':' + pid, title=item.get('title'), abstract=item.get('abstractText'),
                  doi=item.get('doi'), pmid=pid if source == 'MED' else item.get('pmid'), pmcid=pmc,
                  year=item.get('pubYear'), authors=names, url='https://europepmc.org/article/' + source + '/' + pid,
                  candidates=candidates, cited_by_count=_count(item.get('citedByCount')))


def _openalex_paper(item):
    if not isinstance(item, dict) or not re.fullmatch(r'https://openalex.org/W\d+', str(item.get('id'))):
        _fail('invalid_response', 'OpenAlex work identity is missing or invalid')
    abstract, index = None, item.get('abstract_inverted_index')
    if isinstance(index, dict):
        words = {}
        for word, offsets in index.items():
            if isinstance(word, str) and isinstance(offsets, list):
                for offset in offsets:
                    if type(offset) is int and 0 <= offset < MAX_TEXT:
                        words[offset] = word
        abstract = ' '.join(words[x] for x in sorted(words)) or None
    ids = item.get('ids') or {}
    pmid = str(ids.get('pmid') or '').rstrip('/').rsplit('/', 1)[-1]
    pmcid = str(ids.get('pmcid') or '').rstrip('/').rsplit('/', 1)[-1]
    locations = [item.get('best_oa_location')] + (item.get('locations') or [])
    candidates = _locations([x for x in locations if isinstance(x, dict) and x.get('is_oa') is True])
    contents, flags = item.get('content_urls') or {}, item.get('has_content') or {}
    if isinstance(contents, dict) and isinstance(flags, dict):
        for key, fmt in (('pdf', 'pdf'), ('grobid_xml', 'tei')):
            if flags.get(key) is True:
                candidate = _candidate(contents.get(key), fmt)
                if candidate:
                    candidate['required_configuration'] = 'OPENALEX_API_KEY'
                    candidates.append(candidate)
    names = [a['author']['display_name'] for a in item.get('authorships') or []
             if isinstance(a, dict) and isinstance(a.get('author'), dict) and isinstance(a['author'].get('display_name'), str)]
    return _paper('openalex', item['id'].rsplit('/', 1)[-1], title=item.get('title') or item.get('display_name'),
                  abstract=abstract, doi=item.get('doi'), pmid=pmid, pmcid=pmcid,
                  year=item.get('publication_year'), authors=names, url=item['id'], candidates=candidates,
                  cited_by_count=_count(item.get('cited_by_count')))


def _s2_paper(item):
    if not isinstance(item, dict) or not isinstance(item.get('paperId'), str) or not item['paperId']:
        _fail('invalid_response', 'Semantic Scholar paper identity is missing')
    ids, oa = item.get('externalIds') or {}, item.get('openAccessPdf') or {}
    candidate = _candidate(oa.get('url'), 'pdf') if isinstance(oa, dict) else None
    names = [a['name'] for a in item.get('authors') or [] if isinstance(a, dict) and isinstance(a.get('name'), str)]
    return _paper('semantic_scholar', item['paperId'], title=item.get('title'), abstract=item.get('abstract'),
                  doi=ids.get('DOI'), pmid=ids.get('PubMed'), pmcid=ids.get('PubMedCentral'),
                  year=item.get('year'), authors=names, url=item.get('url'), candidates=[candidate] if candidate else [],
                  cited_by_count=_count(item.get('citationCount')))


def _unpaywall_paper(item):
    if not isinstance(item, dict) or not _doi(item.get('doi')):
        _fail('invalid_response', 'Unpaywall DOI identity is missing')
    locations = [item.get('best_oa_location')] + (item.get('oa_locations') or [])
    candidates = _locations(locations, pdf_key='url_for_pdf', landing_key='url_for_landing_page')
    names = []
    for author in item.get('z_authors') or []:
        if isinstance(author, dict):
            name = ' '.join(str(author.get(k) or '') for k in ('given', 'family')).strip()
            if name:
                names.append(name)
    return _paper('unpaywall', _doi(item['doi']), title=item.get('title'), doi=item['doi'],
                  year=item.get('year'), authors=names, url=item.get('doi_url'), candidates=candidates,
                  oa_status=item.get('oa_status') if isinstance(item.get('oa_status'), str) else None)


def _xml(raw):
    if b'<!ENTITY' in raw.upper() or b'<!DOCTYPE' in raw.upper() and b'[' in raw.split(b'<!DOCTYPE', 1)[-1].split(b'>', 1)[0]:
        _fail('invalid_response', 'XML entity declarations are not accepted')
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        _fail('invalid_response', 'Provider returned malformed XML')


def _words(node):
    return ' '.join(' '.join(node.itertext()).split()) if node is not None else None


def _pubmed_papers(raw):
    root = _xml(raw)
    if root.tag != 'PubmedArticleSet' or root.find('.//ERROR') is not None:
        _fail('invalid_response', 'Provider returned an unexpected PubMed XML document')
    output = []
    for article in root.findall('PubmedArticle'):
        pid = article.findtext('./MedlineCitation/PMID')
        if not _pmid(pid):
            _fail('invalid_response', 'PubMed record has no valid PMID')
        ids = {node.get('IdType'): node.text for node in article.findall('./PubmedData/ArticleIdList/ArticleId')}
        pmc = _pmcid(ids.get('pmc'))
        candidates = []
        if pmc:
            url = BASES['pubmed'] + 'efetch.fcgi?' + urllib.parse.urlencode({'db': 'pmc', 'id': pmc[3:], 'retmode': 'xml'})
            candidates.append(_candidate(url, 'xml', availability='pmcid_link_unverified'))
        data = article.find('./MedlineCitation/Article')
        if data is None:
            _fail('invalid_response', 'PubMed article metadata is absent')
        abstract = '\n'.join((node.get('Label', '') + ': ' if node.get('Label') else '') + (_words(node) or '')
                             for node in data.findall('./Abstract/AbstractText')) or None
        names = []
        for node in data.findall('./AuthorList/Author'):
            name = node.findtext('CollectiveName') or ' '.join(filter(None, [node.findtext('ForeName'), node.findtext('LastName')]))
            if name:
                names.append(name)
        date = data.findtext('./Journal/JournalIssue/PubDate/Year') or data.findtext('./ArticleDate/Year')
        if not date:
            match = re.search(r'\b(\d{4})\b', data.findtext('./Journal/JournalIssue/PubDate/MedlineDate') or '')
            date = match.group(1) if match else None
        output.append(_paper('pubmed', pid, title=_words(data.find('ArticleTitle')), abstract=abstract,
                             doi=ids.get('doi'), pmid=pid, pmcid=pmc, year=date, authors=names,
                             url='https://pubmed.ncbi.nlm.nih.gov/' + pid + '/', candidates=candidates))
    return output


class ProviderClient:
    """Stateless provider operations with explicit pagination and host credentials."""

    def __init__(self, transport=None, *, env=None, timeout=20, max_response_bytes=8 * 1024 * 1024):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('timeout must be finite and in (0, 60]')
        if type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 32 * 1024 * 1024:
            raise ValueError('max_response_bytes must be between 1 KiB and 32 MiB')
        if transport is not None and not callable(transport):
            raise ValueError('transport must be callable')
        self.transport = transport or http_transport
        self.env = os.environ if env is None else env
        self.timeout, self.max_response_bytes = timeout, max_response_bytes
        self._rate_limit = transport is None

    def search(self, provider, query, filters=None, cursor=None, limit=10):
        return self._run(provider, 'search', query=query, filters=filters, cursor=cursor, limit=limit)

    def get_paper(self, provider, paper_ref):
        return self._run(provider, 'get_paper', paper_ref=paper_ref)

    def get_citations(self, provider, paper_ref, direction='references', cursor=None, limit=10):
        return self._run(provider, 'get_citations', paper_ref=paper_ref, direction=direction, cursor=cursor, limit=limit)

    def fulltext_candidates(self, provider, paper_ref):
        return self._run(provider, 'fulltext_candidates', paper_ref=paper_ref)

    def _run(self, provider, operation, **args):
        provider = ALIASES.get(provider, provider) if isinstance(provider, str) else None
        records = []
        result = {'status': 'failed', 'data': {}, 'provenance': {'provider': provider, 'requests': records,
                  'attempt_count': 0}, 'coverage': {'scope': 'provider_index', 'fulltext_retrieved': False,
                  'absence_is_negative_evidence': False},
                  'continuation': {'next_cursor': None, 'complete_for_query': False}, 'errors': []}
        try:
            if provider not in SUPPORTED or operation not in SUPPORTED[provider]:
                _fail('unsupported_operation', 'This provider does not support this operation', status='unavailable', retryability='no')
            data, coverage, continuation = getattr(self, '_' + operation)(provider, records, **args)
            result.update(status='ok', data=data)
            result['coverage'].update(coverage)
            result['continuation'].update(continuation)
            if coverage.get('partial'):
                result['status'] = 'partial'
        except ProviderError as exc:
            result['status'] = exc.status
            result['errors'].append({'code': exc.code, 'detail': exc.detail, 'retryability': exc.retryability})
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            result['errors'].append({'code': 'invalid_response', 'detail': 'Provider response did not match its documented schema', 'retryability': 'unknown'})
        result['provenance']['attempt_count'] = len(records)
        return result

    def _request(self, provider, records, path, params=None, *, xml=False):
        params, headers = dict(params or {}), {'Accept': 'application/xml' if xml else 'application/json',
            'User-Agent': 'RSI-Lab-literature/1.0'}
        if provider == 'pubmed':
            params['tool'] = 'rsi_lab_literature'
            for env_key, param in (('NCBI_API_KEY', 'api_key'), ('NCBI_EMAIL', 'email')):
                if self.env.get(env_key):
                    params[param] = self.env[env_key]
        elif provider == 'openalex' and self.env.get('OPENALEX_API_KEY'):
            params['api_key'] = self.env['OPENALEX_API_KEY']
        elif provider == 'semantic_scholar' and self.env.get('SEMANTIC_SCHOLAR_API_KEY'):
            headers['x-api-key'] = self.env['SEMANTIC_SCHOLAR_API_KEY']
        elif provider == 'unpaywall':
            email = self.env.get('UNPAYWALL_EMAIL')
            if not isinstance(email, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                _fail('configuration_required', 'Configure UNPAYWALL_EMAIL on the host', status='unavailable', retryability='after_configuration')
            params['email'] = email
        url = BASES[provider] + path + ('?' + urllib.parse.urlencode(params) if params else '')
        if self._rate_limit:
            interval = {'pubmed': .11 if self.env.get('NCBI_API_KEY') else .35,
                        'semantic_scholar': 1.05}.get(provider, .1)
            with _RATE_LOCK:
                wait = interval - (time.monotonic() - _LAST_REQUEST.get(provider, 0))
                if wait > 0:
                    time.sleep(wait)
                _LAST_REQUEST[provider] = time.monotonic()
        receipt = {'url': _safe_url(url), 'method': 'GET', 'attempted_at': datetime.now(timezone.utc).isoformat(),
                   'status_code': None, 'bytes_received': None, 'response_sha256': None}
        records.append(receipt)
        try:
            response = self.transport('GET', url, headers, self.timeout, self.max_response_bytes)
        except ProviderError:
            raise
        except (TimeoutError, OSError, urllib.error.URLError):
            _fail('transport_error', 'Provider network request failed', retryability='yes')
        if not isinstance(response, dict) or type(response.get('status')) is not int or not isinstance(response.get('body'), bytes):
            _fail('invalid_response', 'Transport returned an invalid response envelope')
        status, raw = response['status'], response['body']
        receipt.update(status_code=status, bytes_received=len(raw), response_sha256=hashlib.sha256(raw).hexdigest())
        if len(raw) > self.max_response_bytes:
            _fail('response_too_large', 'Provider response exceeded the byte limit', retryability='no')
        if status != 200:
            code = {401: 'authentication_required', 403: 'access_denied', 404: 'not_found', 429: 'rate_limited'}.get(status, 'http_error')
            state = 'unavailable' if status in {401, 403, 404, 410} else 'failed'
            _fail(code, f'Provider returned HTTP {status}', status=state,
                  retryability='yes' if status == 429 or status >= 500 else 'after_configuration' if status in {401, 403} else 'no')
        if xml:
            return raw
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            _fail('invalid_response', 'Provider returned malformed JSON')
        if not isinstance(data, dict) or data.get('error') or data.get('errorList'):
            _fail('provider_error', 'Provider returned an error or an unexpected JSON envelope')
        return data

    def _filters(self, filters, provider=None):
        filters = {} if filters is None else filters
        allowed = {'year_from', 'year_to', 'open_access', 'sort'}
        if provider == 'semantic_scholar':
            allowed.add('match')
        if not isinstance(filters, dict) or set(filters) - allowed:
            _fail('invalid_input', 'Supported filters: ' + ', '.join(sorted(allowed)), retryability='no')
        if 'match' in filters and filters['match'] != 'title':
            _fail('invalid_input', 'Semantic Scholar match must be title when specified', retryability='no')
        for name in ('year_from', 'year_to'):
            if name in filters:
                _integer(filters[name], name, 1000, 9999)
        if filters.get('year_from', 1000) > filters.get('year_to', 9999):
            _fail('invalid_input', 'year_from exceeds year_to', retryability='no')
        if 'open_access' in filters and type(filters['open_access']) is not bool:
            _fail('invalid_input', 'open_access must be a boolean', retryability='no')
        if filters.get('sort', 'relevance') not in {'relevance', 'date', 'citations'}:
            _fail('invalid_input', 'Unsupported sort', retryability='no')
        return dict(filters)

    def _ref(self, provider, reference):
        if isinstance(reference, dict):
            if reference.get('provider') == provider and isinstance(reference.get('provider_id'), str):
                return _text(reference['provider_id'], 'paper_ref', 1024)
            for key in ({'pubmed': ('pmid', 'pmcid', 'doi'), 'europe_pmc': ('pmid', 'pmcid', 'doi'),
                         'openalex': ('doi', 'pmid', 'pmcid'), 'semantic_scholar': ('doi', 'pmid', 'pmcid'),
                         'unpaywall': ('doi',)}[provider]):
                if reference.get(key):
                    value = str(reference[key])
                    return (key.upper() + ':' + value) if key in {'pmid', 'pmcid'} else value
            _fail('invalid_input', 'paper_ref has no usable identifier for this provider', retryability='no')
        reference = _text(reference, 'paper_ref', 1024)
        prefix = provider + ':'
        return reference[len(prefix):] if reference.startswith(prefix) else reference

    def _search(self, provider, records, query, filters, cursor, limit):
        query, filters = _text(query, 'query'), self._filters(filters, provider)
        _integer(limit, 'limit', 1, MAX_LIMIT)
        if provider == 'semantic_scholar' and filters.get('match') == 'title':
            # Explicit opt-in to the documented single best-title-match API.
            # Never silently switch to this after a relevance-search failure.
            if limit != 1 or cursor is not None or set(filters) != {'match'}:
                _fail('unsupported_title_match_options',
                      'Title matching requires limit=1, no cursor, and filters={"match":"title"}; additional filters are not supported by this adapter',
                      retryability='no')
            raw = self._request(provider, records, 'paper/search/match', {'query': query, 'fields': _S2_FIELDS})
            rows = raw.get('data')
            if not isinstance(rows, list) or len(rows) != 1:
                _fail('invalid_response', 'Semantic Scholar title matching must return one best-match record')
            paper = _s2_paper(rows[0])
            score = rows[0].get('matchScore')
            if score is not None and (type(score) not in {int, float} or not math.isfinite(score)):
                _fail('invalid_response', 'Semantic Scholar returned an invalid title-match score')
            paper['title_match_score'] = score
            return {'papers': [paper]}, {'returned': 1, 'reported_total': None,
                'query_mode': 'single_best_title_match', 'provider_result_cap': 1,
                'corpus_complete': False, 'exact_title_match_guaranteed': False,
                'partial': False}, {'next_cursor': None, 'complete_for_query': True}
        context = [provider, 'search', query, filters, limit]
        sort = filters.get('sort', 'relevance')
        incomplete = False
        if provider == 'europe_pmc':
            state = _uncursor(cursor, context, '*')
            _text(state, 'provider cursor', 4096)
            term = '(' + query + ')'
            if 'year_from' in filters or 'year_to' in filters:
                term += f" AND FIRST_PDATE:[{filters.get('year_from', 1000)}-01-01 TO {filters.get('year_to', 9999)}-12-31]"
            if 'open_access' in filters:
                term += ' AND OPEN_ACCESS:' + ('Y' if filters['open_access'] else 'N')
            params = {'query': term, 'format': 'json', 'resultType': 'core', 'pageSize': limit, 'cursorMark': state}
            if sort != 'relevance':
                params['sort'] = 'P_PDATE_D desc' if sort == 'date' else 'CITED desc'
            raw = self._request(provider, records, 'search', params)
            rows = raw.get('resultList', {}).get('result')
            if not isinstance(rows, list) or _count(raw.get('hitCount')) is None:
                _fail('invalid_response', 'Europe PMC search result list/count is missing')
            papers = [_epmc_paper(x) for x in rows]
            next_state = raw.get('nextCursorMark') if rows else None
            if next_state == state:
                next_state = None
            total = _count(raw['hitCount'])
            incomplete = cursor is None and next_state is None and total > len(papers)
        elif provider == 'pubmed':
            if sort == 'citations':
                _fail('unsupported_filter', 'PubMed does not support citation-count sorting', status='unavailable', retryability='no')
            state = _uncursor(cursor, context, 0)
            _integer(state, 'offset', 0, 9999)
            term = '(' + query + ')'
            if 'year_from' in filters or 'year_to' in filters:
                term += f" AND {filters.get('year_from', 1000)}:{filters.get('year_to', 9999)}[dp]"
            if 'open_access' in filters:
                _fail('unsupported_filter', 'PubMed free-fulltext is not an open-access license filter', status='unavailable', retryability='no')
            raw = self._request(provider, records, 'esearch.fcgi', {'db': 'pubmed', 'term': term, 'retmode': 'json',
                'retstart': state, 'retmax': min(limit, 10000 - state), 'sort': 'pub date' if sort == 'date' else 'relevance'})
            search = raw.get('esearchresult')
            if not isinstance(search, dict) or search.get('errorlist') or not isinstance(search.get('idlist'), list) or _count(search.get('count')) is None:
                _fail('invalid_response', 'PubMed search result list/count is missing or rejected')
            ids = search['idlist']
            if any(not _pmid(x) for x in ids):
                _fail('invalid_response', 'PubMed search returned an invalid PMID')
            papers = self._pubmed_fetch(records, ids) if ids else []
            total = _count(search['count'])
            next_state = state + len(ids) if ids and state + len(ids) < min(total, 10000) else None
            incomplete = state + len(ids) < total and next_state is None
        elif provider == 'openalex':
            state = _uncursor(cursor, context, '*')
            _text(state, 'provider cursor', 4096)
            if cursor and not self.env.get('OPENALEX_API_KEY'):
                _fail('configuration_required', 'OpenAlex cursor continuation requires OPENALEX_API_KEY', status='unavailable', retryability='after_configuration')
            params = {'search': query, 'per_page': limit, 'cursor': state}
            clauses = []
            if 'year_from' in filters or 'year_to' in filters:
                clauses.append(f"publication_year:{filters.get('year_from', 1000)}-{filters.get('year_to', 9999)}")
            if 'open_access' in filters:
                clauses.append('is_oa:' + str(filters['open_access']).lower())
            if clauses:
                params['filter'] = ','.join(clauses)
            if sort != 'relevance':
                params['sort'] = 'publication_date:desc' if sort == 'date' else 'cited_by_count:desc'
            raw = self._request(provider, records, 'works', params)
            papers, total, next_state = self._oa_list(raw)
            incomplete = 'next_cursor' not in raw['meta'] or (cursor is None and next_state is None and total > len(papers))
        else:
            if sort != 'relevance' or filters.get('open_access') is False:
                _fail('unsupported_filter', 'Semantic Scholar relevance search supports relevance ordering and open_access=true only', status='unavailable', retryability='no')
            state = _uncursor(cursor, context, 0)
            _integer(state, 'offset', 0, 999)
            params = {'query': query, 'limit': min(limit, 1000 - state), 'offset': state, 'fields': _S2_FIELDS}
            if 'year_from' in filters or 'year_to' in filters:
                params['year'] = f"{filters.get('year_from', '')}-{filters.get('year_to', '')}"
            if filters.get('open_access'):
                params['openAccessPdf'] = ''
            raw = self._request(provider, records, 'paper/search', params)
            if not isinstance(raw.get('data'), list) or _count(raw.get('total')) is None:
                _fail('invalid_response', 'Semantic Scholar search result list/count is missing')
            papers, total = [_s2_paper(x) for x in raw['data']], _count(raw['total'])
            next_state = raw.get('next')
            if next_state is not None:
                _integer(next_state, 'provider offset', state + 1, 1000000)
                if next_state >= 1000:
                    next_state = None
            incomplete = next_state is None and total > state + len(papers)
        if len(papers) > limit:
            _fail('invalid_response', 'Provider returned more papers than the requested limit')
        capped = provider in {'pubmed', 'semantic_scholar'} and total > (10000 if provider == 'pubmed' else 1000)
        return {'papers': papers}, {'returned': len(papers), 'reported_total': total, 'total_is_estimate': provider == 'semantic_scholar',
            'provider_result_cap': 10000 if provider == 'pubmed' else 1000 if provider == 'semantic_scholar' else None,
            'partial': bool(next_state is not None or capped or incomplete), 'continuation_incomplete': incomplete}, {'next_cursor': _cursor(context, next_state) if next_state is not None else None,
            'complete_for_query': next_state is None and not capped and not incomplete}

    def _oa_list(self, raw):
        if not isinstance(raw.get('results'), list) or not isinstance(raw.get('meta'), dict) or _count(raw['meta'].get('count')) is None:
            _fail('invalid_response', 'OpenAlex result list/count is missing')
        next_state = raw['meta'].get('next_cursor') if raw['results'] else None
        if next_state is not None:
            _text(next_state, 'provider cursor', 4096)
        return [_openalex_paper(x) for x in raw['results']], _count(raw['meta']['count']), next_state

    def _pubmed_fetch(self, records, ids):
        raw = self._request('pubmed', records, 'efetch.fcgi', {'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml'}, xml=True)
        rows = _pubmed_papers(raw)
        by_id = {row['pmid']: row for row in rows}
        if len(by_id) != len(rows) or set(by_id) != set(ids):
            _fail('incomplete_response', 'PubMed metadata does not match all requested PMIDs')
        return [by_id[x] for x in ids]

    def _pubmed_id(self, records, ref):
        if _pmid(ref):
            return _pmid(ref)
        if _pmcid(ref):
            raw = self._request('pubmed', records, 'elink.fcgi', {'dbfrom': 'pmc', 'db': 'pubmed',
                'id': _pmcid(ref)[3:], 'linkname': 'pmc_pubmed', 'retmode': 'json'})
            ids = self._link_ids(raw, 'pmc_pubmed', _pmcid(ref)[3:])
        elif _doi(ref):
            raw = self._request('pubmed', records, 'esearch.fcgi', {'db': 'pubmed', 'term': '"' + _doi(ref) + '"[doi]', 'retmode': 'json', 'retmax': 2})
            search = raw.get('esearchresult') or {}
            ids = search.get('idlist')
            if not isinstance(ids, list) or _count(search.get('count')) is None or search.get('errorlist'):
                _fail('invalid_response', 'PubMed identifier resolution returned an invalid response')
            if _count(search['count']) > 1:
                _fail('ambiguous_identity', 'DOI resolves to multiple PubMed records', status='unavailable', retryability='no')
        else:
            _fail('invalid_input', 'PubMed accepts PMID, PMCID, or DOI', retryability='no')
        if not ids:
            _fail('not_found', 'No matching PubMed identity was found', status='unavailable', retryability='no')
        if len(ids) != 1 or not _pmid(ids[0]):
            _fail('ambiguous_identity', 'Identifier does not resolve to one PubMed record', status='unavailable', retryability='no')
        return ids[0]

    def _get_paper(self, provider, records, paper_ref):
        ref = self._ref(provider, paper_ref)
        if provider == 'pubmed':
            paper = self._pubmed_fetch(records, [self._pubmed_id(records, ref)])[0]
            if _doi(ref) and (paper['doi'] or '').lower() != _doi(ref).lower():
                _fail('identity_mismatch', 'Returned PubMed DOI does not match the requested DOI')
            if _pmcid(ref) and paper['pmcid'] != _pmcid(ref):
                _fail('identity_mismatch', 'Returned PubMed PMCID does not match the request')
        elif provider == 'europe_pmc':
            if re.fullmatch(r'[A-Z]+:[A-Za-z0-9._-]+', ref) and not ref.startswith(('PMID:', 'PMCID:')):
                source, pid = ref.split(':', 1)
                term = f'EXT_ID:{pid} AND SRC:{source}'
            elif _pmid(ref):
                term = f'EXT_ID:{_pmid(ref)} AND SRC:MED'
            elif _pmcid(ref):
                term = 'PMCID:' + _pmcid(ref)
            elif _doi(ref):
                term = 'DOI:"' + _doi(ref) + '"'
            else:
                _fail('invalid_input', 'Europe PMC accepts SOURCE:ID, PMID, PMCID, or DOI', retryability='no')
            raw = self._request(provider, records, 'search', {'query': term, 'format': 'json', 'resultType': 'core', 'pageSize': 2})
            rows = raw.get('resultList', {}).get('result')
            total = _count(raw.get('hitCount'))
            if not isinstance(rows, list) or total is None:
                _fail('invalid_response', 'Europe PMC lookup result is missing')
            if total == 0:
                _fail('not_found', 'No matching Europe PMC record was found', status='unavailable', retryability='no')
            if total != 1 or len(rows) != 1:
                _fail('ambiguous_identity', 'Identifier resolves to multiple Europe PMC records', status='unavailable', retryability='no')
            paper = _epmc_paper(rows[0])
            expected = _doi(ref)
            if expected and (paper['doi'] or '').lower() != expected.lower():
                _fail('identity_mismatch', 'Europe PMC DOI does not match the request')
            if _pmid(ref) and paper['pmid'] != _pmid(ref) or _pmcid(ref) and paper['pmcid'] != _pmcid(ref):
                _fail('identity_mismatch', 'Europe PMC record does not match the request')
            if re.fullmatch(r'[A-Z]+:[A-Za-z0-9._-]+', ref) and not ref.startswith(('PMID:', 'PMCID:')) and paper['provider_id'] != ref:
                _fail('identity_mismatch', 'Europe PMC source identity does not match the request')
        elif provider == 'openalex':
            oa_id = ref.rstrip('/').rsplit('/', 1)[-1]
            if re.fullmatch(r'W\d+', oa_id):
                identifier = oa_id
            elif _doi(ref):
                identifier = 'https://doi.org/' + _doi(ref)
            elif _pmid(ref):
                identifier = 'pmid:' + _pmid(ref)
            elif _pmcid(ref):
                identifier = 'pmcid:' + _pmcid(ref)
            else:
                _fail('invalid_input', 'OpenAlex accepts a work ID, DOI, PMID, or PMCID', retryability='no')
            raw = self._request(provider, records, 'works/' + urllib.parse.quote(identifier, safe=''))
            paper = _openalex_paper(raw)
            # Reference IDs are needed by the citation operation, but never
            # serialized as provider-controlled arbitrary executable requests.
            references = raw.get('referenced_works')
            paper['references_available'] = isinstance(references, list)
            paper['referenced_works'] = references if isinstance(references, list) else []
            if any(not isinstance(x, str) or not re.fullmatch(r'https://openalex.org/W\d+', x)
                   for x in paper['referenced_works']):
                _fail('invalid_response', 'OpenAlex returned invalid reference identities')
            if _doi(ref) and (paper['doi'] or '').lower() != _doi(ref).lower():
                _fail('identity_mismatch', 'OpenAlex DOI does not match the request')
            if re.fullmatch(r'W\d+', oa_id) and paper['provider_id'] != oa_id:
                _fail('identity_mismatch', 'OpenAlex work does not match the request')
            if _pmid(ref) and paper['pmid'] != _pmid(ref) or _pmcid(ref) and paper['pmcid'] != _pmcid(ref):
                _fail('identity_mismatch', 'OpenAlex record does not match the requested external identity')
        elif provider == 'semantic_scholar':
            identifier = 'DOI:' + _doi(ref) if _doi(ref) else ref
            if _pmid(ref):
                identifier = 'PMID:' + _pmid(ref)
            elif _pmcid(ref):
                identifier = 'PMCID:' + _pmcid(ref)
            if not (re.fullmatch(r'[0-9a-fA-F]{40}', identifier) or re.match(r'^(DOI:|PMID:|PMCID:|ARXIV:|CorpusId:)', identifier)):
                _fail('invalid_input', 'Semantic Scholar requires a paper ID or a typed DOI/PMID/PMCID/ARXIV/CorpusId', retryability='no')
            paper = _s2_paper(self._request(provider, records, 'paper/' + urllib.parse.quote(identifier, safe=''), {'fields': _S2_FIELDS}))
            if _doi(ref) and (paper['doi'] or '').lower() != _doi(ref).lower():
                _fail('identity_mismatch', 'Semantic Scholar DOI does not match the request')
            if re.fullmatch(r'[0-9a-fA-F]{40}', ref) and paper['provider_id'].lower() != ref.lower():
                _fail('identity_mismatch', 'Semantic Scholar paper does not match the request')
            if _pmid(ref) and paper['pmid'] != _pmid(ref) or _pmcid(ref) and paper['pmcid'] != _pmcid(ref):
                _fail('identity_mismatch', 'Semantic Scholar record does not match the requested external identity')
        else:
            doi = _doi(ref)
            if not doi:
                _fail('invalid_input', 'Unpaywall requires a DOI', retryability='no')
            paper = _unpaywall_paper(self._request(provider, records, urllib.parse.quote(doi, safe='')))
            if paper['doi'].lower() != doi.lower():
                _fail('identity_mismatch', 'Unpaywall DOI does not match the request')
        return {'paper': paper}, {'returned': 1, 'content_scope': paper['content_scope']}, {'complete_for_query': True}

    def _link_ids(self, raw, linkname, source_id):
        sets = raw.get('linksets')
        if not isinstance(sets, list) or len(sets) != 1 or sets[0].get('error'):
            _fail('invalid_response', 'PubMed link response is missing its source link set')
        if sets[0].get('ids') != [source_id]:
            _fail('identity_mismatch', 'PubMed link source does not match the requested identity')
        ids = []
        for row in sets[0].get('linksetdbs', []):
            if row.get('linkname') == linkname:
                if not isinstance(row.get('links'), list) or any(not _pmid(x) for x in row['links']):
                    _fail('invalid_response', 'PubMed citation links are malformed')
                ids.extend(str(x) for x in row['links'])
        return list(dict.fromkeys(ids))

    def _get_citations(self, provider, records, paper_ref, direction, cursor, limit):
        if direction not in {'references', 'cited_by'}:
            _fail('invalid_input', 'direction must be references or cited_by', retryability='no')
        _integer(limit, 'limit', 1, MAX_LIMIT)
        ref = self._ref(provider, paper_ref)
        context = [provider, 'get_citations', ref, direction, limit]
        extra = {}
        if provider == 'europe_pmc':
            state = _uncursor(cursor, context, 1)
            _integer(state, 'page', 1, 10000)
            if re.fullmatch(r'(MED|PMC|PPR|AGR|PAT|CBA):[A-Za-z0-9._-]+', ref):
                source, pid = ref.split(':', 1)
            elif _pmid(ref):
                source, pid = 'MED', _pmid(ref)
            elif _pmcid(ref):
                source, pid = 'PMC', _pmcid(ref)
            else:
                paper = self._get_paper(provider, records, ref)[0]['paper']
                source, pid = paper['provider_id'].split(':', 1)
            endpoint = 'references' if direction == 'references' else 'citations'
            raw = self._request(provider, records, source + '/' + urllib.parse.quote(pid, safe='') + '/' + endpoint,
                                {'format': 'json', 'page': state, 'pageSize': limit})
            kind = 'reference' if direction == 'references' else 'citation'
            rows = raw.get(kind + 'List', {}).get(kind)
            total = _count(raw.get('hitCount'))
            if not isinstance(rows, list) or total is None:
                _fail('invalid_response', 'Europe PMC citation result list/count is missing')
            papers = [_epmc_paper(row) for row in rows]
            next_state = state + 1 if rows and state * limit < total else None
        elif provider == 'pubmed':
            state = _uncursor(cursor, context, 0)
            _integer(state, 'offset', 0, 1000000)
            pid = self._pubmed_id(records, ref)
            linkname = 'pubmed_pubmed_refs' if direction == 'references' else 'pubmed_pubmed_citedin'
            raw = self._request(provider, records, 'elink.fcgi', {'dbfrom': 'pubmed', 'db': 'pubmed',
                                'id': pid, 'linkname': linkname, 'retmode': 'json'})
            ids = self._link_ids(raw, linkname, pid)
            chosen, total = ids[state:state + limit], len(ids)
            papers = self._pubmed_fetch(records, chosen) if chosen else []
            next_state = state + len(chosen) if state + len(chosen) < total else None
            extra['pagination_note'] = 'ELink returns the indexed neighborhood; subsequent calls refetch it before local slicing'
        elif provider == 'openalex':
            paper = self._get_paper(provider, records, ref)[0]['paper']
            if direction == 'references':
                if not paper['references_available']:
                    _fail('invalid_response', 'OpenAlex response omitted referenced_works; an empty neighborhood cannot be inferred')
                state = _uncursor(cursor, context, 0)
                _integer(state, 'offset', 0, 1000000)
                ids = paper['referenced_works']
                selected, total = ids[state:state + limit], len(ids)
                if selected:
                    raw = self._request(provider, records, 'works', {'filter': 'openalex:' + '|'.join(x.rsplit('/', 1)[-1] for x in selected), 'per_page': len(selected)})
                    papers, _, _ = self._oa_list(raw)
                    expected, returned = set(selected), {'https://openalex.org/' + row['provider_id'] for row in papers}
                    if expected != returned:
                        _fail('incomplete_response', 'OpenAlex did not resolve all requested reference IDs')
                else:
                    papers = []
                next_state = state + len(selected) if state + len(selected) < total else None
            else:
                state = _uncursor(cursor, context, '*')
                _text(state, 'provider cursor', 4096)
                if cursor and not self.env.get('OPENALEX_API_KEY'):
                    _fail('configuration_required', 'OpenAlex cursor continuation requires OPENALEX_API_KEY', status='unavailable', retryability='after_configuration')
                raw = self._request(provider, records, 'works', {'filter': 'cites:' + paper['provider_id'], 'per_page': limit, 'cursor': state})
                papers, total, next_state = self._oa_list(raw)
                extra['continuation_incomplete'] = 'next_cursor' not in raw['meta'] or (cursor is None and next_state is None and total > len(papers))
        else:
            state = _uncursor(cursor, context, 0)
            _integer(state, 'offset', 0, 1000000)
            identifier = 'DOI:' + _doi(ref) if _doi(ref) else ('PMID:' + _pmid(ref) if _pmid(ref) else ('PMCID:' + _pmcid(ref) if _pmcid(ref) else ref))
            if not (re.fullmatch(r'[0-9a-fA-F]{40}', identifier) or re.match(r'^(DOI:|PMID:|PMCID:|ARXIV:|CorpusId:)', identifier)):
                _fail('invalid_input', 'Semantic Scholar requires a paper ID or typed identifier', retryability='no')
            suffix = 'references' if direction == 'references' else 'citations'
            raw = self._request(provider, records, 'paper/' + urllib.parse.quote(identifier, safe='') + '/' + suffix,
                                {'offset': state, 'limit': limit, 'fields': _S2_FIELDS + ',contexts,intents,isInfluential'})
            if not isinstance(raw.get('data'), list):
                _fail('invalid_response', 'Semantic Scholar citation list is missing')
            papers = []
            unresolved = 0
            for edge in raw['data']:
                item = edge.get('citedPaper' if direction == 'references' else 'citingPaper')
                if not isinstance(item, dict) or not item.get('paperId'):
                    unresolved += 1
                    continue
                paper = _s2_paper(item)
                paper['citation_contexts'] = [x[:MAX_TEXT] for x in edge.get('contexts') or [] if isinstance(x, str)][:100]
                paper['citation_intents'] = [x for x in edge.get('intents') or [] if isinstance(x, str)]
                papers.append(paper)
            extra['unresolved_edges'] = unresolved
            total, next_state = _count(raw.get('total')), raw.get('next')
            if next_state is not None:
                _integer(next_state, 'provider offset', state + 1, 1000000)
        if len(papers) > limit:
            _fail('invalid_response', 'Provider returned more citations than requested')
        incomplete = bool(extra.get('unresolved_edges') or extra.get('continuation_incomplete'))
        return {'papers': papers, 'direction': direction}, {'returned': len(papers), 'reported_total': total,
            'citation_coverage': 'provider_index_only', 'partial': next_state is not None or incomplete, **extra}, {
            'next_cursor': _cursor(context, next_state) if next_state is not None else None,
            'complete_for_query': next_state is None and not incomplete}

    def _fulltext_candidates(self, provider, records, paper_ref):
        data, _, _ = self._get_paper(provider, records, paper_ref)
        paper = data['paper']
        candidates = paper['fulltext_candidates']
        if not candidates:
            _fail('fulltext_location_unavailable', 'Provider supplied no usable fulltext location; availability elsewhere is unknown',
                  status='unavailable', retryability='other_source')
        return {'paper': paper, 'candidates': candidates}, {'returned': len(candidates),
            'content_scope': 'fulltext_locations_only', 'fulltext_retrieved': False}, {'complete_for_query': True}
