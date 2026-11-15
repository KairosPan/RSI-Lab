"""Bounded host-deployed Starling, with no model-controlled commands.

Native interface checked against:
https://github.com/starling-labs/starling/blob/main/src/starling/cli/conf/config.py

The optional json_file protocol targets an explicitly deployed host wrapper.
Both protocols require the host to bind a working corpus backend.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time

from . import remote
from .documents import DocumentError
from .parsers import _terminate, _ParserError, _number
from .sdk_tools import _sources


PROVIDERS = {'starling'}


class _Unavailable(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 20000:
        raise ValueError(name + ' must be nonempty bounded text')
    return value


def _command(settings):
    command = settings.get('command')
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
        raise _Unavailable('Configure an installed host command as an argv list')
    repository = settings.get('repository')
    repository = Path(repository).expanduser().resolve() if repository else None
    if repository and not repository.is_dir():
        raise _Unavailable('Configured project repository is not installed')
    result = []
    for argument in command:
        if repository and not argument.startswith('-') and '{' not in argument and (repository / argument).is_file():
            argument = str((repository / argument).absolute())
        result.append(argument)
    executable = shutil.which(result[0]) if not Path(result[0]).is_absolute() else result[0]
    if not executable or not Path(executable).is_file():
        raise _Unavailable('Configured project executable is unavailable')
    # A venv interpreter is often a symlink. Its invocation path selects the
    # environment; resolving it would silently run the base interpreter.
    result[0] = str(Path(executable).absolute())
    return result, repository


def _read_json(path, maximum, *, lines=False):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('Project output is absent, unsafe, or exceeds its size allowance')
    raw = path.read_bytes()
    return [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()] if lines else json.loads(raw)


def _execute(argv, job, settings, provenance, env=None, output=None):
    """Bound stdout/stderr independently so logs cannot impersonate JSON output."""
    timeout = _number(settings, 'timeout', 120, 1, 1800)
    log_limit = _number(settings, 'max_log_bytes', 65536, 1024, 1048576)
    output_limit = settings.get('max_output_bytes', 65536)
    try:
        process = subprocess.Popen(argv, cwd=job, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, start_new_session=True)
    except (FileNotFoundError, PermissionError) as exc:
        raise _ParserError('needs_configuration', 'Configured executable could not be started', 'unavailable') from exc
    provenance['attempt_count'] += 1
    logs = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ParserError('project_timeout', 'Project exceeded the host time allowance')
                if output and (output.is_symlink() or output.exists() and output.stat().st_size > output_limit):
                    raise _ParserError('output_limit', 'Project output exceeded its allowance or became unsafe')
                for key, _ in selector.select(min(remaining, 0.1)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        logs[key.data].extend(data)
                        if sum(map(len, logs.values())) > log_limit:
                            raise _ParserError('output_limit', 'Project logs exceeded the host byte allowance')
        code = process.wait()
        if code:
            raise _ParserError('project_exit', 'Project exited with status ' + str(code))
        return bytes(logs['stdout'])
    except BaseException:
        _terminate(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def _native(settings, inputs, job, sources, argv, output):
    model = settings['model']
    extra = settings.get('args', [])
    if not isinstance(extra, list) or any(not isinstance(arg, str) for arg in extra):
        raise ValueError('Host project args must be an argv list')
    if any(re.search(r'(?:api[_-]?key|access[_-]?token|password|secret)', arg, re.I) for arg in extra):
        raise ValueError('Bind project credentials through host environment names')
    argv += extra
    if not settings.get('backend'):
        raise _Unavailable('Starling requires a deployed corpus backend; upstream public backends are stubs')
    mode = 'extract' if 'prompt' in inputs else 'run'
    task = _text(inputs.get('prompt', inputs.get('query')), 'task')
    argv += ['mode=' + mode, 'task=' + json.dumps(task), 'run_name=' + job.name,
        'outputs.output_root=' + json.dumps(str(job / 'native-output')),
        'outputs.retrieval_spec=' + json.dumps(str(job / 'retrieval-spec.json')),
        'outputs.agent_run=' + json.dumps(str(output)),
        'outputs.extraction=' + json.dumps(str(job / 'extractions.jsonl'))]
    for role in ('main', 'sub', 'validator', 'investigator', 'extractor'):
        argv.append('models.' + role + '=' + json.dumps(model))
    argv += ['extract.schema_deriver_model=' + json.dumps(model),
             'extract.parallelism=1', 'extract.window_parallelism=1']
    if mode == 'extract':
        pmids = []
        for source in sources:
            metadata = source['source_metadata']
            identifiers = metadata.get('identifiers') or {}
            pmid = metadata.get('pmid') or identifiers.get('pmid') or identifiers.get('PMID')
            if not pmid:
                pmid = re.sub(r'^(?:PMID:|pubmed:)', '', str(metadata.get('paper_id', '')), flags=re.I)
            if not re.fullmatch(r'[1-9]\d{0,11}', str(pmid)):
                raise ValueError('Native Starling extraction needs PMID metadata on each registered document')
            pmids.append(str(pmid))
        if not pmids:
            raise ValueError('Native Starling extraction requires registered PMID sources')
        pmids_file = job / 'pmids.json'
        pmids_file.write_bytes(_json(pmids))
        # Upstream run_extract still validates a RetrievalSpec even when the
        # explicit PMID list bypasses retrieval. Its filters list may be empty.
        spec_file = job / 'pmid-override-spec.json'
        spec_file.write_bytes(_json({'filters': []}))
        argv += ['spec_file=' + json.dumps(str(spec_file)),
                 'extract.pmids_file=' + json.dumps(str(pmids_file)), 'extract.limit=' + str(len(pmids))]
        output = job / 'extractions.jsonl'
    return argv, output


def run_project(provider, settings, inputs, document_store, effect_id):
    provenance = {'provider': provider, 'effect_id': effect_id, 'attempt_count': 0,
        'integration_kind': 'deployment_bridge', 'cost_visibility': {'internal_model_calls': None,
            'internal_http_calls': None, 'accounting': 'opaque_external_job'}}
    result = {'status': 'failed', 'data': {}, 'provenance': provenance,
        'coverage': {'source_text_delivered': False, 'original_fulltext_verified': False,
            'limitations': ['Research-project outputs and support_text are interpretations, not verified original evidence.',
                           'Nested model, retrieval and embedding consumption is not fully metered.']},
        'continuation': None, 'errors': []}
    job = None
    try:
        if provider not in PROVIDERS:
            raise _Unavailable('No registered project bridge for this provider')
        if not isinstance(settings, dict) or not isinstance(inputs, dict):
            raise ValueError('Malformed host configuration')
        if set(inputs) - {'provider', 'document_ids', 'query', 'prompt', 'parameters', 'action', 'cursors'}:
            raise ValueError('Project inputs cannot specify commands, paths, models or environment')
        if inputs.get('provider', provider) != provider or inputs.get('action', 'start') != 'start':
            raise ValueError('Project entrypoint only starts the explicitly selected provider; it cannot poll or restart jobs')
        if 'parameters' in inputs and not isinstance(inputs['parameters'], dict):
            raise ValueError('Project parameters must be a JSON object')
        def check_parameters(value):
            if isinstance(value, dict):
                if any(key.lower() in {'command', 'cmd', 'env', 'environment', 'model', 'path',
                        'input_file', 'output_file', 'file_path', 'repository'} for key in value):
                    raise ValueError('Project parameters cannot override host execution or filesystem bindings')
                for child in value.values():
                    check_parameters(child)
            elif isinstance(value, list):
                for child in value:
                    check_parameters(child)
        check_parameters(inputs.get('parameters', {}))
        _text(effect_id, 'effect_id')
        if not isinstance(settings.get('model'), str) or not settings['model'].strip():
            raise _Unavailable('Configure the deployed project model explicitly')
        protocol = settings.get('protocol', 'native')
        if protocol not in {'native', 'json_file'}:
            raise _Unavailable('Starling requires protocol=native or an explicit json_file deployment')
        if not settings.get('backend'):
            raise _Unavailable('Starling requires a deployed corpus backend')
        maximum = settings.get('max_output_bytes', 65536)
        if type(maximum) is not int or not 1024 <= maximum <= 1048576:
            raise ValueError('Project output allowance must be 1 KiB to 1 MiB')
        argv, repository = _command(settings)
        sources, pages, reads, cursors = [], [], [], {}
        if inputs.get('document_ids'):
            sources, pages, reads, cursors = _sources(inputs, settings, document_store)
        if protocol == 'json_file' and not reads:
            raise ValueError('This project protocol requires registered source documents')
        provenance.update(protocol=protocol, sources=sources)
        result['continuation'] = {'document_cursors': cursors} if cursors else None
        binding = hashlib.sha256(_json({'provider': provider, 'settings': settings, 'inputs': inputs,
            'effect_id': effect_id, 'sources': sources})).hexdigest()
        parent = document_store.root / '.project_jobs'
        if parent.is_symlink():
            raise ValueError('Project job root cannot be a symbolic link')
        parent.mkdir(exist_ok=True)
        job = parent / ('job_' + hashlib.sha256((provider + ':' + effect_id).encode()).hexdigest()[:32])
        if job.is_symlink():
            raise ValueError('Project job cannot be a symbolic link')
        job.mkdir(exist_ok=True)
        marker = job / 'dispatch.json'
        if marker.exists():
            previous = _read_json(marker, 4096)
            if previous['binding_sha256'] != binding:
                raise ValueError('Project effect identity was reused with different inputs')
            cached = job / 'result.json'
            if cached.exists():
                return _read_json(cached, maximum + 524288)
            result['execution_unknown'] = True
            raise ValueError('Existing project dispatch has no certified result; do not re-execute')
        env = dict(os.environ)
        if repository:
            env['PYTHONPATH'] = str(repository) + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        env['RSI_PROJECT_MODEL'] = settings['model']
        output = job / 'output.json'
        if protocol == 'json_file':
            input_file = job / 'input.json'
            input_file.write_bytes(_json({'schema_version': 'rsi_project_input_v1', 'provider': provider,
                'effect_id': effect_id, 'model': settings['model'], 'query': inputs.get('query'),
                'prompt': inputs.get('prompt'), 'parameters': inputs.get('parameters', {}),
                'documents': [dict(source, text=read['text']) for source, read in zip(sources, reads)]}))
            template = ' '.join(argv)
            if '{input_file}' not in template or '{output_file}' not in template or '{model}' not in template:
                raise _Unavailable('json_file command must explicitly bind {input_file}, {output_file} and {model}')
            argv = [arg.format(input_file=str(input_file), output_file=str(output), model=settings['model']) for arg in argv]
        else:
            argv, output = _native(settings, inputs, job, sources, argv, output)
        with marker.open('xb') as stream:
            stream.write(_json({'binding_sha256': binding, 'effect_id': effect_id}))
            stream.flush()
            os.fsync(stream.fileno())
        _execute(argv, job, settings, provenance, env=env, output=output)
        raw_result = _read_json(output, maximum, lines=output.suffix == '.jsonl')
        if protocol == 'native' and output.suffix == '.jsonl' and not raw_result:
            raise _ParserError('incomplete_project_output',
                'Starling produced no extraction records for the requested PMIDs; reconcile before retrying')
        secrets = [value for key, value in env.items() if re.search(r'(?:API_KEY|ACCESS_TOKEN|PASSWORD|SECRET)$', key)]
        if settings.get('credential_env'):
            secrets.append(env.get(settings['credential_env'], ''))
        raw_result = remote.redact(raw_result, secrets)
        if len(_json(raw_result)) > maximum:
            raise ValueError('Project result exceeds its output allowance')
        consumes_text = protocol == 'json_file'
        result['status'] = 'partial' if cursors else 'ok'
        result['data'] = {'interpretation': raw_result, 'sources': sources,
            'authority': 'project_interpretation_not_original_evidence',
            'passages': pages if consumes_text else []}
        result['coverage'].update(source_text_delivered=bool(pages) and consumes_text,
            registered_sources_used_as='normalized_text_inputs' if consumes_text else 'seed_metadata_only',
            backend_source_reading_verified=False)
        (job / 'result.json').write_bytes(_json(result))
    except _Unavailable as exc:
        result['status'] = 'unavailable'
        result['errors'].append({'code': 'needs_configuration', 'detail': str(exc), 'retryability': 'after_configuration'})
    except _ParserError as exc:
        result['status'] = exc.status
        if provenance['attempt_count'] and exc.code != 'needs_configuration':
            result['execution_unknown'] = True
        result['errors'].append({'code': exc.code, 'detail': str(exc), 'retryability': 'unknown'})
    except (ValueError, DocumentError, OSError, KeyError, TypeError) as exc:
        if provenance['attempt_count']:
            result['execution_unknown'] = True
        result['errors'].append({'code': 'project_error', 'detail':
            'Project output was not certified; reconcile before retrying' if provenance['attempt_count'] else str(exc),
            'retryability': 'unknown' if result.get('execution_unknown') else 'no'})
    return result
