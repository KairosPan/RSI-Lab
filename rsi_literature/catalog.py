"""Explicit integrations and their configuration requirements; no hidden installation."""
from __future__ import annotations

import copy
import importlib.util
import os
from pathlib import Path
import shutil


CORE_PROVIDERS = ('europe_pmc', 'pubmed', 'openalex', 'asta', 'unpaywall',
                  'paper2skill', 'grobid', 'langextract', 'poppler')
OPTIONAL_PROVIDERS = ('semantic_scholar', 'scite', 'starling',
                      'paperqa', 'elicit', 'edison')
DEFAULT_ENABLED_PROVIDERS = ('europe_pmc', 'pubmed', 'openalex', 'asta', 'paper2skill', 'poppler')


def _entry(name, transport, operations, source, **details):
    return {'name': name, 'transport': transport, 'operations': operations,
            'documentation': source, **details}


PROVIDERS = {
    'europe_pmc': _entry('Europe PMC', 'native_http', ['search_papers', 'get_paper', 'acquire_fulltext', 'get_citations'],
        'https://europepmc.org/RestfulWebService'),
    'pubmed': _entry('PubMed / PMC', 'native_http', ['search_papers', 'get_paper', 'acquire_fulltext', 'get_citations'],
        'https://www.ncbi.nlm.nih.gov/books/NBK25501/', optional_env=['NCBI_API_KEY', 'NCBI_EMAIL']),
    'openalex': _entry('OpenAlex', 'native_http', ['search_papers', 'get_paper', 'acquire_fulltext', 'get_citations'],
        'https://docs.openalex.org/', optional_env=['OPENALEX_API_KEY'],
        limitation='Anonymous first-page/lookup access; continuation and content access can require OPENALEX_API_KEY.'),
    'semantic_scholar': _entry('Semantic Scholar', 'native_http', ['search_papers', 'get_paper', 'acquire_fulltext', 'get_citations'],
        'https://api.semanticscholar.org/api-docs/', optional_env=['SEMANTIC_SCHOLAR_API_KEY']),
    'unpaywall': _entry('Unpaywall', 'native_http', ['get_paper', 'acquire_fulltext'],
        'https://unpaywall.org/products/api', required_env=['UNPAYWALL_EMAIL']),
    'asta': _entry('Asta Scientific Corpus', 'mcp_http', ['provider_tools', 'call_provider_tool'],
        'https://allenai.org/asta/resources/mcp', endpoint='https://asta-tools.allen.ai/mcp/v1',
        optional_env=['ASTA_TOOL_KEY'], tools=['get_paper', 'get_papers', 'search_papers', 'search_papers_by_title',
        'search_papers_by_relevance', 'get_citations', 'get_references', 'snippet_search', 'search_snippets']),
    'scite': _entry('Scite', 'mcp_http', ['provider_tools', 'call_provider_tool'],
        'https://scite.ai/mcp', endpoint='https://api.scite.ai/mcp', required_env=['SCITE_ACCESS_TOKEN'],
        tools=['search_literature', 'read_fulltext', 'citation_graph', 'verify_papers']),
    'grobid': _entry('GROBID', 'grobid_http', ['build_reading_package'],
        'https://grobid.readthedocs.io/en/latest/Grobid-service/', requires=['endpoint']),
    'poppler': _entry('Poppler', 'poppler_cli', ['build_reading_package', 'render_document_page'],
        'https://poppler.freedesktop.org/',
        executables={'pdftotext_path': 'pdftotext', 'pdfinfo_path': 'pdfinfo', 'pdftoppm_path': 'pdftoppm'},
        limitation='Local PDF text extraction and bounded page rendering; no OCR or model inference.'),
    'paper2skill': _entry('Paper2Agent / Paper2Skill', 'paper2skill_cli', ['build_reading_package'],
        'https://github.com/jmiao24/Paper2Agent', requires=['repository', 'python']),
    'starling': _entry('Starling', 'project_command', ['extract_evidence', 'run_review'],
        'https://github.com/starling-labs/starling', requires=['command', 'backend', 'model'],
        limitation='Public upstream search/normalization backends are stubs; an installed corpus backend is required.'),
    'langextract': _entry('LangExtract', 'python', ['extract_evidence'],
        'https://github.com/google/langextract', module='langextract', package='langextract', requires=['model']),
    'elicit': _entry('Elicit', 'elicit_http', ['search_papers', 'run_review'],
        'https://docs.elicit.com/', required_env=['ELICIT_API_KEY']),
    'edison': _entry('Edison Literature / PaperQA3', 'python', ['run_review'],
        'https://github.com/EdisonScientific/edison-client-docs', module='edison_client', package='edison-client',
        required_env=['EDISON_API_KEY']),
    'paperqa': _entry('PaperQA / PaperQA2', 'python', ['run_review'],
        'https://github.com/Future-House/paper-qa', module='paperqa', package='paper-qa', requires=['model', 'embedding']),
}


def provider_catalog(config=None, env=None):
    """Readiness is local configuration inspection, never a claim of a live check."""
    config, env = config or {}, os.environ if env is None else env
    connectors = config.get('connectors', {})
    enabled = set(config.get('enabled_providers', DEFAULT_ENABLED_PROVIDERS))
    rows = []
    for provider, spec in PROVIDERS.items():
        row = {'provider': provider, **copy.deepcopy(spec), 'live_verified': False,
               'tier': 'core' if provider in CORE_PROVIDERS else 'optional'}
        settings = connectors.get(provider, {})
        missing = []
        for key in spec.get('required_env', []):
            env_name = key if spec['transport'] == 'native_http' else settings.get('credential_env', key)
            if not env.get(env_name):
                missing.append('environment:' + env_name)
        for key in spec.get('requires', []):
            if not settings.get(key):
                missing.append('configuration:' + key)
        for key, default in spec.get('executables', {}).items():
            executable = settings.get(key, default)
            if not isinstance(executable, str) or not executable or not shutil.which(executable):
                missing.append('executable:' + key)
        module = spec.get('module')
        python = settings.get('python')
        if python and (not isinstance(python, str) or not (Path(python).is_file() or shutil.which(python))):
            missing.append('python_interpreter')
        if module and not settings.get('python') and importlib.util.find_spec(module) is None:
            missing.append('package:' + spec['package'])
        command = settings.get('command')
        if command:
            executable = command[0] if isinstance(command, list) and command else None
            if not executable or not (Path(executable).is_file() or shutil.which(executable)):
                missing.append('executable')
        if settings.get('repository') and not Path(settings['repository']).is_dir():
            missing.append('repository')
        row['enabled'] = provider in enabled
        row['status'] = 'disabled' if not row['enabled'] else 'needs_configuration' if missing else 'configured'
        row['missing'] = missing
        row['configured_transport'] = settings.get('transport', spec['transport'])
        rows.append(row)
    return rows
