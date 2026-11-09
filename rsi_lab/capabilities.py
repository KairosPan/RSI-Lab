"""Executable adapter acceptance through a previously reserved controller Effect.

An engineer supplies a real single-file Python adapter. Acceptance runs fixed
normal/failure cases in fresh processes and directories and checks outputs,
identity, source receipts and measured resources. This is process/workspace
isolation, NOT an OS or network security sandbox: only an explicitly hash-
authorized, reviewed local adapter may be executed by this runner.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time

MAX_ADAPTER_BYTES = 128 * 1024
MAX_PACKET_BYTES = 512 * 1024
MAX_CASES = 32
PROFILE = 'reviewed_local_python_json_v1'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encoded(value)).hexdigest()


def _text(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 512


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(ch in '0123456789abcdef' for ch in value)


def validate_request(spec):
    required = {'schema_version', 'capability_id', 'purpose', 'scope', 'input_contract', 'output_contract',
                'scientific_impact', 'baseline_scientific_protocol', 'candidate_scientific_protocol',
                'new_study_id', 'execution_profile', 'recovery', 'limits', 'cases'}
    if not isinstance(spec, dict) or set(spec) != required or spec['schema_version'] != 'capability_request_v2':
        raise ValueError('Expected the exact capability_request_v2 specification')
    if not all(_text(spec[key]) for key in ('capability_id', 'purpose', 'baseline_scientific_protocol', 'candidate_scientific_protocol')):
        raise ValueError('Capability purpose and scientific identities must be explicit')
    if (not isinstance(spec['scope'], dict) or not spec['scope'] or not isinstance(spec['input_contract'], dict)
            or not spec['input_contract'] or not isinstance(spec['output_contract'], dict) or not spec['output_contract']):
        raise ValueError('Applicable scope and input/output contracts are required')
    if type(spec['scientific_impact']) is not bool or spec['execution_profile'] != PROFILE:
        raise ValueError('Only reviewed local adapters with explicit scientific impact are supported')
    changed = spec['baseline_scientific_protocol'] != spec['candidate_scientific_protocol']
    if spec['scientific_impact'] and (not changed or not _text(spec['new_study_id'])):
        raise ValueError('Scientifically consequential capabilities require a new protocol and study')
    if not spec['scientific_impact'] and (changed or spec['new_study_id'] is not None):
        raise ValueError('A method change cannot masquerade as a result-neutral capability')
    if spec['recovery'] != {'query_supported': False, 'idempotency_supported': False, 'unknown_result': 'blocked_external'}:
        raise ValueError('This adapter protocol has no validated query/idempotency interface; unknown effects block')
    limits = spec['limits']
    if not isinstance(limits, dict) or set(limits) != {'wall_seconds_per_case', 'cpu_seconds_per_case', 'output_bytes_per_case'}:
        raise ValueError('Acceptance must freeze enforceable per-case execution limits')
    if (type(limits['wall_seconds_per_case']) not in (int, float) or not math.isfinite(limits['wall_seconds_per_case'])
            or not 0 < limits['wall_seconds_per_case'] <= 60
            or type(limits['cpu_seconds_per_case']) is not int or not 1 <= limits['cpu_seconds_per_case'] <= 60
            or type(limits['output_bytes_per_case']) is not int or not 256 <= limits['output_bytes_per_case'] <= 131072):
        raise ValueError('Acceptance execution limits are outside the supported bounded profile')
    cases = spec['cases']
    if not isinstance(cases, list) or not 2 <= len(cases) <= MAX_CASES:
        raise ValueError('Acceptance requires bounded normal and failure cases')
    ids, kinds = set(), set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'case_id', 'kind', 'input', 'expected_output', 'expected_identity', 'expected_error_code'}:
            raise ValueError('Each case must pin input, output, identity and failure expectations before delivery')
        if not _text(case['case_id']) or case['case_id'] in ids or case['kind'] not in ('normal', 'failure'):
            raise ValueError('Invalid, duplicate or unsupported acceptance case')
        if not isinstance(case['input'], dict):
            raise ValueError('Capability test input must be a JSON object')
        if case['kind'] == 'normal':
            if not isinstance(case['expected_output'], dict) or not _text(case['expected_identity']) or case['expected_error_code'] is not None:
                raise ValueError('Normal cases require actual expected output and an identity oracle')
        elif case['expected_output'] is not None or case['expected_identity'] is not None or not _text(case['expected_error_code']):
            raise ValueError('Failure cases must expect a specific error, not a fabricated successful output')
        ids.add(case['case_id']); kinds.add(case['kind'])
    if kinds != {'normal', 'failure'}:
        raise ValueError('Both normal and failure inputs must be exercised')
    if len(encoded(spec)) > MAX_PACKET_BYTES // 2:
        raise ValueError('Capability request exceeds the local acceptance capacity')
    return json.loads(encoded(spec))


def _source(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.suffix != '.py':
        raise ValueError('A real single-file Python adapter is required; symbolic links are refused')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(MAX_ADAPTER_BYTES + 1)
    if not raw or len(raw) > MAX_ADAPTER_BYTES:
        raise ValueError('Adapter is empty or exceeds the registered source capacity')
    compile(raw, str(path), 'exec')
    return raw


def _limit_process(cpu_seconds, output_bytes):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))
    return apply


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate output key')
        result[key] = value
    return result


def _decode(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))


def _manifest(manifest, spec):
    required = {'schema_version', 'capability_id', 'capability_version', 'artifact_sha256', 'scope',
                'input_contract', 'output_contract', 'packages', 'engineering_provenance'}
    if (not isinstance(manifest, dict) or set(manifest) != required or manifest['schema_version'] != 'capability_delivery_v2'
            or not _text(manifest['capability_version']) or not _sha(manifest['artifact_sha256'])
            or not isinstance(manifest['packages'], dict) or any(not _text(k) or not _text(v) for k, v in manifest['packages'].items())
            or not isinstance(manifest['engineering_provenance'], dict) or not manifest['engineering_provenance']):
        raise ValueError('Delivery requires an executable version, exact artifact, contracts and engineering provenance')
    if any(manifest[key] != spec[key] for key in ('capability_id', 'scope', 'input_contract', 'output_contract')):
        raise ValueError('Capability delivery changed the frozen acceptance scope or contracts')


def _packet(packet):
    required = {'schema_version', 'request_id', 'request_spec', 'request_spec_sha256', 'manifest',
                'adapter_source', 'execution_authorization_sha256'}
    if (not isinstance(packet, dict) or set(packet) != required
            or packet['schema_version'] != 'capability_acceptance_packet_v2' or not _text(packet['request_id'])):
        raise ValueError('Unsupported capability execution packet')
    spec = validate_request(packet['request_spec'])
    _manifest(packet['manifest'], spec)
    raw = packet['adapter_source'].encode('utf-8')
    if not raw or len(raw) > MAX_ADAPTER_BYTES or len(encoded(packet)) > MAX_PACKET_BYTES:
        raise ValueError('Executable delivery exceeds bounded capacity')
    if digest(raw) != packet['manifest']['artifact_sha256'] or packet['execution_authorization_sha256'] != digest(raw):
        raise ValueError('Adapter bytes do not match the exact reviewed execution authorization')
    if packet['request_spec_sha256'] != digest(spec):
        raise ValueError('Capability acceptance request changed')
    compile(raw, '<reviewed-capability-adapter>', 'exec')
    return spec, raw


def _response_checks(case, limits, output_raw, error_raw, *, returncode, timed_out, cpu_seconds, source_unchanged):
    """Derive checks from recorded bytes, not an adapter's claim of passing."""
    parsed, error = None, None
    checks = {'normal_or_expected_failure': False, 'identity_preserved': False, 'source_receipt_verified': False,
              'resource_limits_passed': not timed_out and cpu_seconds <= limits['cpu_seconds_per_case'] + 0.05
                  and len(output_raw) <= limits['output_bytes_per_case'] and len(error_raw) <= limits['output_bytes_per_case'],
              'adapter_unchanged': source_unchanged}
    try:
        if returncode != 0 or timed_out:
            raise ValueError('Adapter did not terminate normally within the registered limit')
        parsed = _decode(output_raw)
        if not isinstance(parsed, dict) or set(parsed) != {'schema_version', 'ok', 'output', 'identity', 'source_receipts', 'error'}:
            raise ValueError('Adapter response differs from the exact acceptance output protocol')
        if parsed['schema_version'] != 'capability_response_v2' or type(parsed['ok']) is not bool:
            raise ValueError('Malformed adapter success state')
        checks['source_receipt_verified'] = parsed['source_receipts'] == [{'kind': 'input', 'sha256': digest(case['input'])}]
        checks['identity_preserved'] = parsed['identity'] == case['expected_identity']
        if case['kind'] == 'normal':
            checks['normal_or_expected_failure'] = parsed['ok'] is True and parsed['error'] is None and parsed['output'] == case['expected_output']
        else:
            checks['normal_or_expected_failure'] = (parsed['ok'] is False and parsed['output'] is None
                and isinstance(parsed['error'], dict) and set(parsed['error']) == {'code', 'message'}
                and parsed['error']['code'] == case['expected_error_code'] and _text(parsed['error']['message']))
    except (ValueError, TypeError, UnicodeError) as exc:
        error = str(exc)
    return checks, parsed, error


def _case(raw, case, limits):
    payload = {'schema_version': 'capability_input_v2', 'case_id': case['case_id'], 'input': case['input']}
    input_raw = encoded(payload)
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    timed_out = False
    with tempfile.TemporaryDirectory(prefix='rsi-capability-') as directory:
        root = Path(directory)
        adapter = root / 'adapter.py'
        adapter.write_bytes(raw)
        environment = {'PATH': os.path.dirname(sys.executable), 'PYTHONIOENCODING': 'utf-8', 'LC_ALL': 'C',
                       'TMPDIR': str(root)}
        with (root / 'stdout.json').open('w+b') as stdout, (root / 'stderr.txt').open('w+b') as stderr:
            process = subprocess.Popen([sys.executable, '-I', str(adapter)], cwd=root, stdin=subprocess.PIPE,
                stdout=stdout, stderr=stderr, env=environment, start_new_session=True,
                preexec_fn=_limit_process(limits['cpu_seconds_per_case'], limits['output_bytes_per_case']))
            try:
                process.communicate(input_raw, timeout=limits['wall_seconds_per_case'])
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            finally:
                # A completed adapter must not leave descendant processes running.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            stdout.seek(0); stderr.seek(0)
            output_raw = stdout.read(limits['output_bytes_per_case'] + 1)
            error_raw = stderr.read(limits['output_bytes_per_case'] + 1)
        final_source_hash = digest(adapter.read_bytes()) if adapter.is_file() and not adapter.is_symlink() else None
    wall = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    checks, parsed, error = _response_checks(case, limits, output_raw, error_raw,
        returncode=process.returncode, timed_out=timed_out, cpu_seconds=cpu, source_unchanged=final_source_hash == digest(raw))
    return {'case_id': case['case_id'], 'kind': case['kind'], 'input_sha256': digest(input_raw),
            'input': payload, 'stdout': output_raw.decode('utf-8', errors='replace'),
            'stderr': error_raw.decode('utf-8', errors='replace'), 'output_sha256': digest(output_raw),
            'stderr_sha256': digest(error_raw), 'stdout_hex': output_raw.hex(), 'stderr_hex': error_raw.hex(),
            'final_source_sha256': final_source_hash, 'response': parsed, 'returncode': process.returncode,
            'timed_out': timed_out, 'wall_seconds': wall, 'cpu_seconds': cpu,
            'resource_accounting': 'Observed serial-runner child CPU delta; OS CPU/file-size limits and process-group wall timeout',
            'checks': checks, 'passed': all(checks.values()), 'error': error}


def execute_acceptance(effect, packet_bytes):
    """Executor callback for Controller.dispatch_effect; never mutates a ledger."""
    packet = _decode(packet_bytes)
    if len(packet_bytes) > MAX_PACKET_BYTES or packet.get('schema_version') != 'capability_acceptance_packet_v2':
        raise ValueError('Unsupported or oversized capability execution packet')
    if effect.get('status') != 'dispatching' or effect.get('metadata', {}).get('capability_request_id') != packet.get('request_id'):
        raise ValueError('Capability acceptance requires a persisted, dispatched controller Effect')
    spec, raw = _packet(packet)
    manifest = packet['manifest']
    if effect.get('packet_ref') is not None and digest(packet_bytes) != effect['packet_ref']:
        raise ValueError('Dispatched packet differs from its reserved immutable bytes')
    actual_packages = {name: importlib.metadata.version(name) for name in manifest['packages']}
    if actual_packages != manifest['packages']:
        raise ValueError('Installed dependency versions differ from the reviewed delivery')
    cases = [_case(raw, case, spec['limits']) for case in spec['cases']]
    passed = all(case['passed'] for case in cases)
    receipt = {'schema_version': 'capability_acceptance_v2', 'request_id': packet['request_id'],
        'request_spec_sha256': digest(spec), 'artifact_sha256': digest(raw), 'manifest': manifest,
        'cases': cases, 'accepted': passed, 'acceptance_scope': spec['scope'],
        'isolation': 'Fresh local process and temporary working directory; not an OS/network security sandbox',
        'recovery': spec['recovery'], 'limitations': ['Acceptance establishes only the frozen cases and scope, not universal scientific validity',
            'Only previously reviewed hash-authorized adapters are allowed; arbitrary source cannot be safely sandboxed by this runner']}
    # Acceptance itself finished even when adapter tests failed. The registry
    # consumes accepted=False as a failed capability, never a passed tool.
    return {'status': 'succeeded', 'actual_costs': {'model_calls': 0, 'tool_calls': len(cases), 'tokens': 0,
            'cpu_seconds': sum(case['cpu_seconds'] for case in cases),
            'wall_seconds': sum(case['wall_seconds'] for case in cases)}, 'capability_acceptance': receipt}


class CapabilityRegistry:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def request(self, request_id, spec, *, key):
        spec = validate_request(spec)
        def commit(tx):
            return tx.insert('request', request_id, {'kind': 'capability_build', 'status': 'requested',
                'spec_ref': tx.blob(spec), 'spec_sha256': digest(spec), 'capability_id': spec['capability_id'],
                'delivery_ref': None, 'acceptance_effect_id': None})
        return self.store.mutate(self.run_id, key, 'capability_build_requested', commit, payload={'request_id': request_id, 'spec': spec})

    def packet(self, request_id, adapter_path, manifest, *, execution_authorization_sha256):
        request = self.store.get(self.run_id, 'request', request_id)
        if request.get('kind') != 'capability_build' or request['status'] != 'requested':
            raise ValueError('Capability acceptance needs an outstanding frozen build request')
        spec = validate_request(json.loads(self.store.read_blob(request['spec_ref'])))
        if digest(spec) != request['spec_sha256']:
            raise ValueError('Frozen capability request changed')
        _manifest(manifest, spec)
        raw = _source(adapter_path)
        if digest(raw) != manifest['artifact_sha256'] or execution_authorization_sha256 != manifest['artifact_sha256']:
            raise ValueError('Executable adapter differs from the reviewed hash authorization')
        packet = {'schema_version': 'capability_acceptance_packet_v2', 'request_id': request_id,
            'request_spec': spec, 'request_spec_sha256': digest(spec), 'manifest': manifest,
            'adapter_source': raw.decode('utf-8'), 'execution_authorization_sha256': execution_authorization_sha256}
        if len(encoded(packet)) > MAX_PACKET_BYTES:
            raise ValueError('Final complete acceptance packet exceeds execution capacity')
        return packet

    def accept(self, request_id, effect_id, *, key):
        def commit(tx):
            request, effect = tx.get('request', request_id), tx.get('effect', effect_id)
            if request.get('kind') != 'capability_build' or request['status'] != 'requested' or effect.get('status') != 'succeeded':
                raise ValueError('Capability acceptance requires an outstanding request and succeeded Effect')
            if effect.get('metadata', {}).get('capability_request_id') != request_id or effect['created_seq'] <= request['created_seq']:
                raise ValueError('Capability testing must follow and bind its prospective build request')
            packet = _decode(self.store.read_blob(effect['packet_ref']))
            _packet(packet)
            result_ref = effect.get('result_ref') or effect.get('receipt_ref')
            result = _decode(self.store.read_blob(result_ref))
            acceptance = result.get('capability_acceptance')
            spec = validate_request(_decode(self.store.read_blob(request['spec_ref'])))
            if (not isinstance(acceptance, dict) or acceptance.get('schema_version') != 'capability_acceptance_v2'
                    or packet.get('request_id') != request_id or packet.get('request_spec') != spec
                    or acceptance.get('request_id') != request_id or acceptance.get('request_spec_sha256') != digest(spec)
                    or acceptance.get('manifest') != packet.get('manifest')
                    or acceptance.get('artifact_sha256') != digest(packet['adapter_source'].encode())
                    or acceptance.get('acceptance_scope') != spec['scope'] or acceptance.get('recovery') != spec['recovery']):
                raise ValueError('Acceptance receipt is not bound to the actual executable delivery and request')
            cases = acceptance.get('cases')
            if not isinstance(cases, list) or len(cases) != len(spec['cases']):
                raise ValueError('Every registered acceptance case is required')
            checks = {'normal_or_expected_failure', 'identity_preserved', 'source_receipt_verified', 'resource_limits_passed', 'adapter_unchanged'}
            for expected, case in zip(spec['cases'], cases):
                if (case.get('case_id') != expected['case_id'] or case.get('kind') != expected['kind']
                        or case.get('input') != {'schema_version': 'capability_input_v2', 'case_id': expected['case_id'], 'input': expected['input']}
                        or case.get('input_sha256') != digest(case['input']) or set(case.get('checks', {})) != checks
                        or any(type(value) is not bool for value in case['checks'].values())
                        or case.get('passed') is not all(case['checks'].values())
                        or type(case.get('returncode')) is not int or type(case.get('timed_out')) is not bool
                        or type(case.get('cpu_seconds')) not in (int, float) or not math.isfinite(case['cpu_seconds']) or case['cpu_seconds'] < 0
                        or type(case.get('wall_seconds')) not in (int, float) or not math.isfinite(case['wall_seconds']) or case['wall_seconds'] < 0):
                    raise ValueError('Acceptance case lacks actual matching input, output, source or resource receipts')
                try:
                    output_raw, error_raw = bytes.fromhex(case['stdout_hex']), bytes.fromhex(case['stderr_hex'])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError('Acceptance requires original stdout/stderr bytes') from exc
                if (digest(output_raw) != case.get('output_sha256') or digest(error_raw) != case.get('stderr_sha256')
                        or output_raw.decode('utf-8', errors='replace') != case.get('stdout')
                        or error_raw.decode('utf-8', errors='replace') != case.get('stderr')):
                    raise ValueError('Acceptance raw output hashes do not match')
                derived, response, error = _response_checks(expected, spec['limits'], output_raw, error_raw,
                    returncode=case['returncode'], timed_out=case['timed_out'], cpu_seconds=case['cpu_seconds'],
                    source_unchanged=case.get('final_source_sha256') == packet['manifest']['artifact_sha256'])
                if case['checks'] != derived or case.get('response') != response or case.get('error') != error:
                    raise ValueError('Claimed acceptance checks differ from independently checked raw receipts')
            passed = all(case['passed'] for case in cases)
            actual = {'model_calls': 0, 'tool_calls': len(cases), 'tokens': 0,
                      'cpu_seconds': sum(case['cpu_seconds'] for case in cases),
                      'wall_seconds': sum(case['wall_seconds'] for case in cases)}
            if result.get('status') != 'succeeded' or result.get('actual_costs') != actual:
                raise ValueError('Acceptance actual resource receipt does not reconcile with executed cases')
            if acceptance.get('accepted') is not passed:
                raise ValueError('Acceptance decision differs from actual test receipts')
            tx.update('request', request_id, {'status': 'accepted' if passed else 'failed_acceptance',
                'delivery_ref': tx.blob(packet['manifest']), 'acceptance_effect_id': effect_id, 'acceptance_ref': result_ref})
            if not passed:
                return {'status': 'failed_acceptance', 'request_id': request_id, 'effect_id': effect_id}
            capability_id = spec['capability_id'] + ':' + packet['manifest']['capability_version']
            return tx.insert('capability', capability_id, {'status': 'verified', 'capability_id': spec['capability_id'],
                'capability_version': packet['manifest']['capability_version'], 'artifact_ref': tx.blob(packet['adapter_source'].encode()),
                'artifact_sha256': acceptance['artifact_sha256'], 'manifest_ref': tx.blob(packet['manifest']),
                'acceptance_ref': result_ref, 'request_id': request_id, 'effect_id': effect_id,
                'scope': spec['scope'], 'input_contract': spec['input_contract'], 'output_contract': spec['output_contract'],
                'scientific_impact': spec['scientific_impact'], 'scientific_protocol': spec['candidate_scientific_protocol'],
                'new_study_id': spec['new_study_id'], 'recovery': spec['recovery'],
                'use': 'new_study_only' if spec['scientific_impact'] else 'scope_compatible_future_work_only'})
        return self.store.mutate(self.run_id, key, 'capability_acceptance_settled', commit,
                                 payload={'request_id': request_id, 'effect_id': effect_id})
