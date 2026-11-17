"""What a run has switched on, reported and checked before the first model call.

A host that leaves out a config key or a Runtime argument still gets a run
whose kernel tests pass; only the request sent to the model shows the feature
is missing. These functions read the frozen config, the services the Runtime
built, the response schema the roles receive and the request a ready Work
would send. Nothing here writes the ledger, calls a model or executes a tool.
"""
from __future__ import annotations

import json

from . import input_view
from .improvements import enabled
from .roles import ACTIONS, lab_settings_enabled, response_schema, schema_summary

AGENT_WORK = {'lab', 'meta_discovery', 'meta_disposition', 'disposition'}
# The block each expectation puts into a request, by its path in the request data.
REQUEST_BLOCKS = {'scientific_memory': 'task.scientific_memory', 'meta_lab': 'task.constraints.research_loop',
                  'orchestration': 'task.constraints.orchestration',
                  'research_policy': 'task.constraints.research_policy'}
SWITCHES = ('scientific_memory', 'meta_lab', 'orchestration', 'identity_provider', 'research_policy')
IDENTITY_LISTS = ('capabilities', 'measure')
MISSING = {'scientific_memory': 'the run config does not set research_features.scientific_memory to true',
           'meta_lab': 'the run config does not set research_features.meta_lab to true',
           'orchestration': 'the run config has no "orchestration" block ({"no_result_steps": N, "max_changes": M})',
           'research_policy': 'the run config has no "research_policy" with at least one field'}
PRESENT = {'scientific_memory': 'the run config sets research_features.scientific_memory to true',
           'meta_lab': 'the run config sets research_features.meta_lab to true',
           'orchestration': 'the run config has an "orchestration" block',
           'research_policy': 'the run config has a "research_policy"'}
ADD_TO_CONFIG = 'The run config is frozen at init; start a new run whose config has it.'
DROP_FROM_CONFIG = 'The run config is frozen at init; start a new run whose config leaves it out.'
LOAD_ADAPTER = 'Pass it in Runtime(..., adapters=[...]); on the command line use --adapter-factory.'


def feature_report(runtime):
    """The research features of ``runtime``'s run as one JSON object."""
    config = runtime.config
    catalog = runtime.capability_catalog()
    loaded = runtime.adapters.by_id
    # The same record the executor recomputes the disposition schema from.
    lab_settings = bool(runtime.orchestration) and lab_settings_enabled(runtime.orchestration.metadata('disposition', None))
    # Every request the Runtime builds carries agent_state, hence working_memory.
    modes = {mode: schema_summary(response_schema(mode, working_memory=True,
                 research_features=config.get('research_features'), research_policy=runtime.research_policy,
                 lab_settings=lab_settings)) for mode in ACTIONS}
    return {'run_id': runtime.run_id,
            'scientific_memory': {'declared': enabled(config, 'scientific_memory'),
                                  'service': runtime.scientific_memory is not None},
            'batch_interpretation_capabilities': sorted(runtime.batch_interpretations.capabilities),
            'meta_lab': {'declared': enabled(config, 'meta_lab'), 'service': runtime.improvements is not None},
            'orchestration': {'declared': 'orchestration' in config, 'service': runtime.orchestration is not None},
            # Scientific Memory calls it; a value it cannot call supplies no identities.
            'identity_provider': callable(runtime.scientific_identity_provider),
            'research_policy': bool(runtime.research_policy),
            'adapters': [{'adapter_id': adapter.adapter_id, 'version': adapter.version,
                          'measure': adapter.measure is not None, 'requires_acceptance': adapter.requires_acceptance,
                          'in_catalog': adapter.adapter_id in catalog}
                         for adapter in sorted(loaded.values(), key=lambda adapter: adapter.adapter_id)],
            'capability_catalog': [{'capability_id': key, 'version': catalog[key]['version'],
                                    'admission': catalog[key].get('admission'), 'adapter_loaded': key in loaded}
                                   for key in sorted(catalog)],
            'modes': modes}


def _carries(task, path):
    """Whether the request data holds the block at ``path``, such as task.constraints.research_loop."""
    value = {'task': task}
    for name in path.split('.'):
        if not isinstance(value, dict) or name not in value:
            return False
        value = value[name]
    return True


def request_report(runtime, work_id):
    """What the model request for one ready Lab or Meta Work carries.

    The request is built on the path prepare_meta uses and read back from the
    bytes that would be sent. Nothing is written: no Effect is prepared, no
    session or root question is created, and the Work stays ready.

    When ``status`` is not ready nothing would be sent: the hash, size,
    actions and extra fields are None, and the rest describes the request
    that could not be sent.

    This reports the content of the request, not whether the call would be
    admitted: the persistent-session context bound, budgets, external
    capacity, session state, issue versions and a finalizing run are checked
    only at dispatch.
    """
    rows = runtime.store.list(runtime.run_id, 'work', id=work_id)
    if not rows:
        raise ValueError('A ready Lab or Meta Work is required; there is no Work %s' % work_id)
    work = rows[0]
    if work['kind'] not in AGENT_WORK or work['status'] != 'ready':
        raise ValueError('A ready Lab or Meta Work is required; Work %s is a %s Work in status %s'
                         % (work_id, work['kind'], work['status']))
    agent, mode, task, options = runtime._context(work, read_only=True)
    status, reason, planned = 'ready', None, None
    memory = task.get('scientific_memory', {})
    if memory.get('status') == 'blocked':
        # prepare_meta blocks the Work here instead of planning a packet.
        status, reason = 'blocked', 'Scientific Memory context is blocked: ' + (
            memory.get('reason') or 'required objects are missing: '
            + json.dumps(memory.get('missing_required_objects'), ensure_ascii=False))
    else:
        planned = agent.prepare(mode, task, **options)
        if planned['status'] != 'ready':
            status, reason, planned = planned['status'], planned['reason'], None
    if planned is not None:
        envelope = planned['envelope']
        sent = input_view.decode(envelope['prompt'].rsplit('INPUT_DATA_JSON:\n', 1)[1],
                                 envelope['input_view_mode'])['task']
        summary = schema_summary(envelope['response_schema'])
    else:
        # No request exists, so no schema is claimed for it; the real call
        # stops here too, before any rule of the packet itself is applied.
        sent, summary = task, {'actions': None, 'extra_fields': None}
    return {'work_id': work_id, 'work_kind': work['kind'], 'mode': mode, 'status': status, 'reason': reason,
            'present': {path: _carries(sent, path) for path in REQUEST_BLOCKS.values()},
            'final_review_only': sent['constraints'].get('final_review_only') is True,
            'interpretation_only': sent['constraints'].get('interpretation_only') is True,
            'capabilities': sorted(sent['capability_catalog']), **summary,
            'input_bytes': planned['input_bytes'] if planned else None,
            'packet_sha256': planned['packet_sha256'] if planned else None}


def _validate_expectations(expect):
    """Refuse a misspelt or malformed expectation: skipping it would pass the very omission it names."""
    if type(expect) is not dict:
        raise ValueError('Preflight expectations must be an object such as {"scientific_memory": true}')
    unknown = [name for name in expect if name not in SWITCHES + IDENTITY_LISTS]
    if unknown:
        raise ValueError('Unknown preflight expectation %s; the supported ones are %s'
                         % (', '.join(repr(name) for name in unknown), ', '.join(SWITCHES + IDENTITY_LISTS)))
    for name in SWITCHES:
        if name in expect and type(expect[name]) is not bool:
            raise ValueError('Preflight expectation %s must be true or false' % name)
    for name in IDENTITY_LISTS:
        ids = expect.get(name, [])
        if (type(ids) not in (list, tuple) or any(type(item) is not str or not item for item in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Preflight expectation %s must be a list of distinct nonempty identities' % name)


def _found(config, name):
    """What the config holds under research_features where it does not enable ``name``, for the failure text."""
    features = config.get('research_features')
    if not isinstance(features, dict) or name not in features:
        return ''
    if name == 'orchestration':
        return ' (research_features.orchestration is set, but only the top-level block enables it)'
    return ' (found %s; only the JSON value true enables it)' % json.dumps(features[name], ensure_ascii=False)


def _run_problems(runtime, expect, report):
    """(expectation, subject, text) for each expectation the run's config and arguments do not meet."""
    problems = []
    def problem(name, text, subject=None):
        problems.append((name, subject, text))
    for name in ('scientific_memory', 'meta_lab', 'orchestration'):
        if name not in expect:
            continue
        declared, service = report[name]['declared'], report[name]['service']
        if expect[name] and not declared:
            problem(name, 'not enabled: %s%s. %s' % (MISSING[name], _found(runtime.config, name), ADD_TO_CONFIG))
        elif expect[name] and not service:
            problem(name, '%s, but this Runtime built no service for it.' % PRESENT[name])
        elif not expect[name] and declared:
            problem(name, 'expected off, but %s. %s' % (PRESENT[name], DROP_FROM_CONFIG))
        elif not expect[name] and service:
            problem(name, 'expected off, but this Runtime holds its service although the run config does not '
                          'declare it.')
    if 'identity_provider' in expect:
        provider = runtime.scientific_identity_provider
        if expect['identity_provider'] and provider is None:
            problem('identity_provider', 'Runtime(...) was built without scientific_identity_provider, so published '
                    'results get no domain identities. Pass scientific_identity_provider=<callable> to Runtime(...).')
        elif expect['identity_provider'] and not callable(provider):
            problem('identity_provider', 'the scientific_identity_provider passed to Runtime(...) is not callable (it '
                    'is a %s), so it supplies no domain identities. Pass a function.' % type(provider).__name__)
        elif expect['identity_provider'] and not report['scientific_memory']['service']:
            problem('identity_provider', 'scientific_identity_provider was passed to Runtime(...) but is unused, '
                    'because Scientific Memory is not enabled (run config key research_features.scientific_memory).')
        elif not expect['identity_provider'] and provider is not None:
            problem('identity_provider', 'expected off, but Runtime(...) was given scientific_identity_provider.')
    if 'research_policy' in expect:
        if expect['research_policy'] and not report['research_policy']:
            problem('research_policy', 'not set: %s. %s' % (MISSING['research_policy'], ADD_TO_CONFIG))
        elif not expect['research_policy'] and report['research_policy']:
            problem('research_policy', 'expected off, but %s. %s' % (PRESENT['research_policy'], DROP_FROM_CONFIG))
    catalog = {row['capability_id'] for row in report['capability_catalog']}
    adapters = {row['adapter_id']: row for row in report['adapters']}
    for capability in expect.get('capabilities', ()):
        if capability not in catalog and capability not in adapters:
            problem('capabilities', '%r is not in the capability catalog: no adapter with that identity is loaded. %s'
                    % (capability, LOAD_ADAPTER), capability)
        elif capability not in catalog:
            problem('capabilities', '%r is loaded but not in the capability catalog: its adapter has '
                    'requires_acceptance=True and this run holds no accepted capability record for it. Accept it with '
                    'Runtime.accept_capability(...), or register the adapter without requires_acceptance.'
                    % capability, capability)
        elif capability not in adapters:
            problem('capabilities', '%r is in the capability catalog from an accepted record, but its adapter is not '
                    'loaded, so a request for it would be admitted and never executed. %s'
                    % (capability, LOAD_ADAPTER), capability)
    for adapter_id in expect.get('measure', ()):
        if adapter_id not in adapters:
            problem('measure', 'adapter %r is not loaded. %s' % (adapter_id, LOAD_ADAPTER))
        elif not adapters[adapter_id]['measure']:
            problem('measure', 'adapter %r has no measure callback, so the feedback loop records no domain metrics '
                    'for its results. Build it with Adapter(..., measure=<function>).' % adapter_id)
        elif not adapters[adapter_id]['in_catalog']:
            problem('measure', 'the measure callback of adapter %r never runs: the adapter has '
                    'requires_acceptance=True and is not in the capability catalog, so it cannot be executed.'
                    % adapter_id)
        elif not report['meta_lab']['service']:
            # Only the feedback loop measures a publication.
            problem('measure', 'the measure callback of adapter %r is never called, because the Meta-to-Lab feedback '
                    'loop is not enabled (run config key research_features.meta_lab).' % adapter_id)
    return problems


def _request_problems(runtime, expect, request, reported):
    """Expectations the run meets but this request does not; ``reported`` ones are not repeated."""
    work = request['work_id']
    if request['status'] != 'ready':
        return [('request', None, 'the request for Work %s cannot be sent (%s): %s'
                 % (work, request['status'], request['reason']))]
    problems = []
    for name, path in REQUEST_BLOCKS.items():
        if name not in expect or (name, None) in reported or request['present'][path] == expect[name]:
            continue
        if expect[name]:
            problems.append((name, None, 'the run has it, but the request for Work %s carries no %s.' % (work, path)))
        else:
            problems.append((name, None, 'expected off, but the request for Work %s carries %s.' % (work, path)))
    orchestration = runtime.orchestration
    for capability in expect.get('capabilities', ()):
        if ('capabilities', capability) in reported or capability in request['capabilities']:
            continue
        if request['final_review_only']:
            why = ': a final-review request offers no capabilities'
        elif (orchestration and request['mode'] == 'lab'
                and capability in orchestration.settings(orchestration.current_change())['disabled_tools']):
            why = ': the Lab settings in force list it in disabled_tools'
        else:
            why = ''
        problems.append(('capabilities', capability, '%r is available to the run but absent from the request for '
                         'Work %s%s.' % (capability, work, why)))
    return problems


def preflight(runtime, expect, work_id=None):
    """Hold the run, and optionally one request, to the host's declared expectations.

    ``expect`` maps scientific_memory, meta_lab, orchestration,
    identity_provider and research_policy to the boolean the host expects, and
    capabilities and measure to lists of adapter identities: capabilities that
    must be available, adapters whose measure callback must be in use. A key
    left out is not checked.

    With ``work_id``, the request that ready Lab or Meta Work would send is
    built without being prepared and held to the same expectations.

    Returns the feature report, with the request report under ``request``
    when a Work was given. Raises ValueError with one line per unmet
    expectation, each naming the run config key or the Runtime argument that
    supplies it.
    """
    _validate_expectations(expect)
    report = feature_report(runtime)
    problems = _run_problems(runtime, expect, report)
    if work_id is not None:
        try:
            report['request'] = request_report(runtime, work_id)
        except (ValueError, KeyError) as exc:
            # A KeyError prints only the missing key, so name the error.
            reason = str(exc) if isinstance(exc, ValueError) else '%s: %s' % (type(exc).__name__, exc)
            problems.append(('request', None, 'no request can be built for Work %s: %s' % (work_id, reason)))
        else:
            problems += _request_problems(runtime, expect, report['request'],
                                          {(name, subject) for name, subject, _ in problems})
    if problems:
        raise ValueError('Preflight failed for run %r: %d expectation(s) not met.\n%s' % (
            runtime.run_id, len(problems), '\n'.join('- %s: %s' % (name, text) for name, _, text in problems)))
    return report
