"""Network-free contracts for real provider adapters and resource boundaries."""
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from rsi_literature.providers import ProviderClient, operation_max_requests


def response(data, status=200):
    return {'status': status, 'headers': {}, 'body': data if isinstance(data, bytes) else json.dumps(data).encode()}


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, timeout, max_response_bytes):
        self.calls.append({'method': method, 'url': url, 'headers': headers,
                           'timeout': timeout, 'max_response_bytes': max_response_bytes})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


EPMC = {'id': '123', 'source': 'MED', 'title': 'A paper', 'abstractText': 'An abstract.',
        'doi': '10.1234/example', 'pmcid': 'PMC456', 'pubYear': '2024', 'isOpenAccess': 'Y',
        'authorList': {'author': [{'fullName': 'One Author'}]}, 'citedByCount': 9}
OA = {'id': 'https://openalex.org/W123', 'title': 'A paper', 'doi': 'https://doi.org/10.1234/example',
      'publication_year': 2024, 'abstract_inverted_index': {'An': [0], 'abstract.': [1]},
      'referenced_works': ['https://openalex.org/W456'], 'authorships': [],
      'has_content': {'pdf': True, 'grobid_xml': True},
      'content_urls': {'pdf': 'https://content.openalex.org/works/W123.pdf',
                       'grobid_xml': 'https://content.openalex.org/works/W123.grobid-xml'}}
S2 = {'paperId': 'a' * 40, 'title': 'A paper', 'abstract': 'An abstract.', 'year': 2024,
      'externalIds': {'DOI': '10.1234/example', 'PubMed': '123'},
      'openAccessPdf': {'url': 'https://example.org/paper.pdf'}, 'authors': [{'name': 'One Author'}]}
PUBMED_XML = b'''<?xml version="1.0"?><!DOCTYPE PubmedArticleSet SYSTEM "https://example.org/pubmed.dtd">
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
<ArticleTitle>A <i>paper</i></ArticleTitle><Abstract><AbstractText Label="METHODS">An abstract.</AbstractText></Abstract>
<Journal><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>
<AuthorList><Author><ForeName>One</ForeName><LastName>Author</LastName></Author></AuthorList>
</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/example</ArticleId>
<ArticleId IdType="pmc">PMC456</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'''


class ProviderTests(unittest.TestCase):
    def test_europe_pmc_search_pagination_and_original_scope(self):
        transport = Transport(response({'hitCount': 2, 'nextCursorMark': 'NEXT', 'resultList': {'result': [EPMC]}}),
                              response({'hitCount': 2, 'resultList': {'result': [EPMC]}}))
        client = ProviderClient(transport, env={})
        first = client.search('europe_pmc', 'a question', {'year_from': 2020}, limit=1)
        self.assertEqual(first['status'], 'partial')
        paper = first['data']['papers'][0]
        self.assertEqual(paper['paper_id'], 'europe_pmc:MED:123')
        self.assertEqual(paper['content_scope'], 'abstract')
        self.assertFalse(paper['fulltext_candidates'][0]['retrieved'])
        second = client.search('europe_pmc', 'a question', {'year_from': 2020}, limit=1,
                               cursor=first['continuation']['next_cursor'])
        self.assertEqual(second['status'], 'ok')
        query = parse_qs(urlsplit(transport.calls[1]['url']).query)
        self.assertEqual(query['cursorMark'], ['NEXT'])
        self.assertIn('FIRST_PDATE:[2020-01-01 TO 9999-12-31]', query['query'][0])

    def test_cursor_is_bound_to_query_and_limit(self):
        transport = Transport(response({'hitCount': 2, 'nextCursorMark': 'NEXT', 'resultList': {'result': [EPMC]}}))
        client = ProviderClient(transport, env={})
        cursor = client.search('europe_pmc', 'one', limit=1)['continuation']['next_cursor']
        result = client.search('europe_pmc', 'two', cursor=cursor, limit=1)
        self.assertEqual(result['errors'][0]['code'], 'invalid_cursor')
        self.assertEqual(result['provenance']['attempt_count'], 0)
        result = client.search('europe_pmc', 'one', cursor=cursor, limit=2)
        self.assertEqual(result['status'], 'failed')

    def test_empty_success_requires_explicit_provider_result(self):
        transport = Transport(response({'hitCount': 0, 'resultList': {'result': []}}), response({}))
        client = ProviderClient(transport, env={})
        result = client.search('europe_pmc', 'absent')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['coverage']['reported_total'], 0)
        self.assertFalse(result['coverage']['absence_is_negative_evidence'])
        self.assertEqual(client.search('europe_pmc', 'bad')['status'], 'failed')

    def test_pubmed_search_fetches_abstract_and_accounts_both_requests(self):
        transport = Transport(response({'esearchresult': {'count': '1', 'idlist': ['123']}}), response(PUBMED_XML))
        result = ProviderClient(transport, env={}).search('pubmed_pmc', 'a question')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['provenance']['attempt_count'], 2)
        paper = result['data']['papers'][0]
        self.assertEqual(paper['title'], 'A paper')
        self.assertEqual(paper['abstract'], 'METHODS: An abstract.')
        self.assertEqual(paper['fulltext_candidates'][0]['availability'], 'pmcid_link_unverified')

    def test_pubmed_fetch_mismatched_identity_is_not_success(self):
        transport = Transport(response({'esearchresult': {'count': '1', 'idlist': ['999']}}), response(PUBMED_XML))
        result = ProviderClient(transport, env={}).search('pubmed', 'test')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['errors'][0]['code'], 'incomplete_response')

    def test_pubmed_doi_lookup_verifies_identity(self):
        transport = Transport(response({'esearchresult': {'count': '1', 'idlist': ['123']}}), response(PUBMED_XML))
        result = ProviderClient(transport, env={}).get_paper('pubmed', '10.1234/other')
        self.assertEqual(result['errors'][0]['code'], 'identity_mismatch')

    def test_pubmed_xml_entities_are_rejected(self):
        malicious = b'<!DOCTYPE PubmedArticleSet [<!ENTITY x "danger">]><PubmedArticleSet>&x;</PubmedArticleSet>'
        result = ProviderClient(Transport(response(malicious)), env={}).get_paper('pubmed', '123')
        self.assertEqual(result['status'], 'failed')

    def test_pubmed_citation_direction_and_request_bound(self):
        transport = Transport(response({'linksets': [{'ids': ['999'], 'linksetdbs': [{'linkname': 'pubmed_pubmed_citedin', 'links': ['123']}]}]}), response(PUBMED_XML))
        result = ProviderClient(transport, env={}).get_citations('pubmed', '999', 'cited_by')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['papers'][0]['pmid'], '123')
        self.assertIn('linkname=pubmed_pubmed_citedin', transport.calls[0]['url'])
        self.assertLessEqual(len(transport.calls), operation_max_requests('pubmed', 'get_citations'))

    def test_pubmed_missing_linksets_fails(self):
        result = ProviderClient(Transport(response({})), env={}).get_citations('pubmed', '123')
        self.assertEqual(result['status'], 'failed')

    def test_pubmed_missing_source_is_not_an_empty_neighborhood(self):
        result = ProviderClient(Transport(response({'linksets': [{}]})), env={}).get_citations('pubmed', '123')
        self.assertEqual(result['status'], 'failed')

    def test_missing_continuation_cannot_claim_complete_results(self):
        cases = [('europe_pmc', {'hitCount': 2, 'resultList': {'result': [EPMC]}}),
                 ('openalex', {'meta': {'count': 2}, 'results': [OA]}),
                 ('semantic_scholar', {'total': 2, 'data': [S2]})]
        for provider, raw in cases:
            with self.subTest(provider=provider):
                result = ProviderClient(Transport(response(raw)), env={}).search(provider, 'test', limit=1)
                self.assertEqual(result['status'], 'partial')
                self.assertFalse(result['continuation']['complete_for_query'])

    def test_openalex_abstract_and_cached_content_candidates(self):
        transport = Transport(response(OA))
        result = ProviderClient(transport, env={}).fulltext_candidates('openalex', 'W123')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['paper']['abstract'], 'An abstract.')
        candidates = result['data']['candidates']
        self.assertEqual([x['format'] for x in candidates], ['pdf', 'tei'])
        self.assertEqual(candidates[0]['required_configuration'], 'OPENALEX_API_KEY')
        self.assertFalse(result['coverage']['fulltext_retrieved'])

    def test_openalex_cursor_needs_key_and_never_exposes_key(self):
        transport = Transport(response({'meta': {'count': 2, 'next_cursor': 'NEXT'}, 'results': [OA]}))
        client = ProviderClient(transport, env={})
        first = client.search('openalex', 'test', limit=1)
        second = client.search('openalex', 'test', limit=1, cursor=first['continuation']['next_cursor'])
        self.assertEqual(second['status'], 'unavailable')
        self.assertEqual(len(transport.calls), 1)
        transport = Transport(response(OA))
        result = ProviderClient(transport, env={'OPENALEX_API_KEY': 'VERY_SECRET'}).get_paper('openalex', 'W123')
        self.assertIn('VERY_SECRET', transport.calls[0]['url'])
        self.assertNotIn('VERY_SECRET', json.dumps(result))

    def test_openalex_citations_references_get_exact_list(self):
        referenced = {**OA, 'id': 'https://openalex.org/W456'}
        transport = Transport(response(OA), response({'meta': {'count': 1}, 'results': [referenced]}))
        result = ProviderClient(transport, env={}).get_citations('openalex', 'W123', 'references')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['papers'][0]['provider_id'], 'W456')
        self.assertIn('openalex:W456', parse_qs(urlsplit(transport.calls[1]['url']).query)['filter'][0])
        self.assertEqual(len(transport.calls), operation_max_requests('openalex', 'get_citations'))

    def test_openalex_no_references_field_is_unknown_not_empty(self):
        # Regression added with the explicit-reference contract below.
        raw = {**OA}
        del raw['referenced_works']
        result = ProviderClient(Transport(response(raw)), env={}).get_citations('openalex', 'W123', 'references')
        self.assertEqual(result['status'], 'failed')

    def test_semantic_scholar_citation_contexts(self):
        transport = Transport(response({'data': [{'citedPaper': S2, 'contexts': ['We used this method.'], 'intents': ['methodology']}]}))
        result = ProviderClient(transport, env={'SEMANTIC_SCHOLAR_API_KEY': 'SECRET'}).get_citations('semantic_scholar', 'DOI:10.1234/seed')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['papers'][0]['citation_contexts'], ['We used this method.'])
        self.assertEqual(transport.calls[0]['headers']['x-api-key'], 'SECRET')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_semantic_scholar_unresolved_citation_is_partial(self):
        transport = Transport(response({'data': [{'citedPaper': None}]}))
        result = ProviderClient(transport, env={}).get_citations('semantic_scholar', 'a' * 40)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['coverage']['unresolved_edges'], 1)
        self.assertFalse(result['continuation']['complete_for_query'])

    def test_semantic_scholar_search_has_approximate_total(self):
        transport = Transport(response({'total': 1, 'data': [S2]}))
        result = ProviderClient(transport, env={}).search('semantic_scholar', 'test')
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['coverage']['total_is_estimate'])

    def test_semantic_scholar_explicit_title_match_returns_followup_identifiers(self):
        transport = Transport(response({'data': [{**S2, 'matchScore': 0.98}]}))
        result = ProviderClient(transport, env={}).search('semantic_scholar', 'A paper', {'match': 'title'}, limit=1)
        self.assertEqual(result['status'], 'ok')
        paper = result['data']['papers'][0]
        self.assertEqual(paper['provider_id'], S2['paperId'])
        self.assertEqual(paper['doi'], '10.1234/example')
        self.assertEqual(paper['pmid'], '123')
        self.assertEqual(paper['abstract'], 'An abstract.')
        self.assertEqual(paper['title_match_score'], 0.98)
        self.assertFalse(paper['fulltext_candidates'][0]['retrieved'])
        self.assertEqual(result['coverage']['query_mode'], 'single_best_title_match')
        self.assertFalse(result['coverage']['corpus_complete'])
        self.assertTrue(result['continuation']['complete_for_query'])
        request = urlsplit(transport.calls[0]['url'])
        self.assertTrue(request.path.endswith('/paper/search/match'))
        self.assertNotIn('limit', parse_qs(request.query))
        self.assertEqual(result['provenance']['attempt_count'], 1)

    def test_title_match_rejects_unhandled_filters_limits_and_cursors_before_network(self):
        transport = Transport()
        client = ProviderClient(transport, env={})
        cases = [({'match': 'title'}, 2, None), ({'match': 'title'}, 1, 'cursor'),
                 ({'match': 'title', 'year_from': 2020}, 1, None),
                 ({'match': 'title', 'open_access': True}, 1, None),
                 ({'match': 'title', 'sort': 'relevance'}, 1, None),
                 ({'match': 'title', 'unknown': True}, 1, None),
                 ({'match': 'automatic'}, 1, None)]
        for filters, limit, cursor in cases:
            with self.subTest(filters=filters, limit=limit, cursor=cursor):
                result = client.search('semantic_scholar', 'A paper', filters, cursor=cursor, limit=limit)
                self.assertEqual(result['status'], 'failed')
        self.assertEqual(client.search('europe_pmc', 'A paper', {'match': 'title'}, limit=1)['status'], 'failed')
        self.assertEqual(transport.calls, [])

    def test_title_match_404_and_malformed_results_are_not_empty_success(self):
        for raw, status in [(response({}, 404), 'unavailable'), (response({'data': []}), 'failed'),
                            (response({'data': [S2, S2]}), 'failed'), (response({}), 'failed')]:
            result = ProviderClient(Transport(raw), env={}).search('semantic_scholar', 'A paper', {'match': 'title'}, limit=1)
            self.assertEqual(result['status'], status)
            self.assertFalse(result['coverage']['absence_is_negative_evidence'])

    def test_relevance_search_never_falls_back_to_title_matching(self):
        transport = Transport(response({}, 429))
        result = ProviderClient(transport, env={}).search('semantic_scholar', 'A paper', limit=1)
        self.assertEqual(result['errors'][0]['code'], 'rate_limited')
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(urlsplit(transport.calls[0]['url']).path.endswith('/paper/search'))

    def test_unpaywall_configuration_and_retired_search(self):
        transport = Transport()
        client = ProviderClient(transport, env={})
        self.assertEqual(client.get_paper('unpaywall', '10.1234/example')['errors'][0]['code'], 'configuration_required')
        self.assertEqual(client.search('unpaywall', 'test')['errors'][0]['code'], 'unsupported_operation')
        self.assertEqual(transport.calls, [])

    def test_unpaywall_oa_locations_email_redaction(self):
        transport = Transport(response({'doi': '10.1234/example', 'title': 'A paper', 'is_oa': True,
            'best_oa_location': {'url_for_pdf': 'https://example.org/paper.pdf', 'license': 'cc-by', 'version': 'publishedVersion'}}))
        result = ProviderClient(transport, env={'UNPAYWALL_EMAIL': 'researcher@institution.edu'}).fulltext_candidates('unpaywall', 'https://doi.org/10.1234/example')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['candidates'][0]['license'], 'cc-by')
        self.assertNotIn('researcher', json.dumps(result))

    def test_fulltext_unavailable_is_distinct_from_empty_search(self):
        raw = {**S2, 'openAccessPdf': None}
        result = ProviderClient(Transport(response(raw)), env={}).fulltext_candidates('semantic_scholar', 'a' * 40)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['errors'][0]['code'], 'fulltext_location_unavailable')

    def test_credential_bearing_candidate_is_not_persisted(self):
        raw = {**S2, 'openAccessPdf': {'url': 'https://example.org/paper.pdf?api_key=SECRET'}}
        result = ProviderClient(Transport(response(raw)), env={}).get_paper('semantic_scholar', 'a' * 40)
        self.assertEqual(result['data']['paper']['fulltext_candidates'], [])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_timeout_http_rate_limit_and_size_are_not_empty_success(self):
        cases = [(TimeoutError('https://host/?api_key=SECRET'), 'transport_error'),
                 (response({}, 429), 'rate_limited'), (response(b'x' * 1025), 'response_too_large')]
        for raw, expected in cases:
            with self.subTest(expected=expected):
                result = ProviderClient(Transport(raw), env={}, max_response_bytes=1024).get_paper('openalex', 'W123')
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['errors'][0]['code'], expected)
                self.assertEqual(result['provenance']['attempt_count'], 1)
                self.assertNotIn('SECRET', json.dumps(result))

    def test_invalid_input_does_not_dispatch(self):
        transport = Transport()
        client = ProviderClient(transport, env={})
        for limit in (0, True, 101):
            self.assertEqual(client.search('europe_pmc', 'test', limit=limit)['status'], 'failed')
        self.assertEqual(client.search('pubmed', 'test', {'open_access': True})['status'], 'unavailable')
        self.assertEqual(client.search('europe_pmc', 'test', {'unknown': 1})['status'], 'failed')
        self.assertEqual(client.get_paper('openalex', 'http://127.0.0.1/secret')['status'], 'failed')
        self.assertEqual(client.get_citations('pubmed', '123', direction='related')['status'], 'failed')
        self.assertEqual(transport.calls, [])

    def test_max_requests_and_aliases(self):
        self.assertEqual(operation_max_requests('pubmed_pmc', 'search'), 2)
        self.assertEqual(operation_max_requests('pubmed', 'get_citations'), 3)
        self.assertEqual(operation_max_requests('unpaywall', 'search'), 0)
        self.assertEqual(operation_max_requests('europe_pmc', 'get_citations'), 2)
        self.assertEqual(operation_max_requests('semantic_scholar', 'get_paper'), 1)


if __name__ == '__main__':
    unittest.main()
