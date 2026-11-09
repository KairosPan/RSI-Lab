"""Prospectively pinned local trial branches in separate processes/workspaces.

This is NOT an OS/network security sandbox. Only reviewed, exactly hash-
authorized single-file Python adapters are supported. Scientific suitability is
outside this runner: it preserves the frozen protocol, identity, raw output,
and observed resources for TrialEngine settlement. CPU accounting uses the
serial controller's child-usage delta; concurrent external dispatch is refused.
"""
from __future__ import annotations

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
from dataclasses import dataclass

from .capabilities import _decode, _limit_process, _source, digest, encoded
from .trials import digest as trial_digest, validate_trial_spec

PROFILE = 'reviewed_local_python_metrics_v1'
RESOURCES = {'model_calls', 'tool_calls', 'tokens', 'cpu_seconds', 'wall_seconds'}
MAX_PACKET_BYTES = 512 * 1024
IDENTITY_KEYS = ('trial_id', 'pair_id', 'arm', 'run_id', 'input_hash', 'versions')


@dataclass(frozen=True)
class BranchProtocol:
    """Wire identifiers supplied by a compatibility adapter, never domain rules."""
    trial_schema: str = 'rsi_trial_v2'
    packet_schema: str = 'rsi_branch_packet_v2'
    input_schema: str = 'rsi_trial_input_v2'
    output_schema: str = 'rsi_trial_output_v2'


DEFAULT_WIRE = BranchProtocol()


def _plan(store, control_run_id, trial_id, *, wire=DEFAULT_WIRE):
    trial = store.get(control_run_id, 'trial', trial_id)
    spec = validate_trial_spec(json.loads(store.read_blob(trial['spec_ref'])), schema_version=wire.trial_schema)
    if trial['status'] != 'active' or trial_digest(spec) != trial['spec_sha256']:
        raise ValueError('An active unchanged prospective Trial is required')
    if spec['execution_mode'] != 'isolated_branch':
        raise ValueError('Process branches require an isolated_branch trial')
    profile = spec['applicability'].get('branch_execution')
    if (not isinstance(profile, dict) or set(profile) != {'profile', 'artifact_sha256', 'limits'}
            or profile['profile'] != PROFILE or not isinstance(profile['artifact_sha256'], dict)
            or set(profile['artifact_sha256']) != {'baseline', 'candidate'}
            or any(not isinstance(v, str) or len(v) != 64 or any(c not in '0123456789abcdef' for c in v)
                   for v in profile['artifact_sha256'].values())):
        raise ValueError('Both executable artifact hashes must be frozen before trial admission')
    limits = profile['limits']
    if (not isinstance(limits, dict) or set(limits) != {'wall_seconds', 'cpu_seconds', 'output_bytes'}
            or type(limits['wall_seconds']) not in (int, float) or not math.isfinite(limits['wall_seconds'])
            or not 0 < limits['wall_seconds'] <= 60 or type(limits['cpu_seconds']) is not int
            or not 1 <= limits['cpu_seconds'] <= 60 or type(limits['output_bytes']) is not int
            or not 256 <= limits['output_bytes'] <= 131072):
        raise ValueError('Branch process limits must be frozen within the supported bounded profile')
    if set(spec['budget']) != RESOURCES:
        raise ValueError('This branch profile requires explicit budgets for all five observed resource categories')
    if store.run(control_run_id)['config'].get('max_external', 1) != 1:
        raise ValueError('Serial branch resource accounting requires max_external=1')
    return trial, spec, profile


def _validate_packet(store, control_run_id, packet, *, wire=DEFAULT_WIRE):
    required = {'schema_version', *IDENTITY_KEYS, 'trial_spec_ref', 'trial_spec_sha256', 'input',
                'adapter_source', 'artifact_sha256', 'execution_authorization_sha256'}
    if not isinstance(packet, dict) or set(packet) != required or packet['schema_version'] != wire.packet_schema:
        raise ValueError('Expected an exact prospective branch execution packet')
    trial, spec, profile = _plan(store, control_run_id, packet['trial_id'], wire=wire)
    pair = next((pair for pair in spec['pairs'] if pair['pair_id'] == packet['pair_id']), None)
    arm = packet['arm']
    if pair is None or arm not in ('baseline', 'candidate'):
        raise ValueError('Branch is outside the prospective pair plan')
    if (packet['run_id'] != pair[arm + '_run_id'] or packet['versions'] != spec[arm + '_versions']
            or packet['input_hash'] != pair['input_hash'] or digest(packet['input']) != pair['input_hash']
            or packet['trial_spec_ref'] != trial['spec_ref'] or packet['trial_spec_sha256'] != trial['spec_sha256']):
        raise ValueError('Branch packet changed the frozen input, run, versions or trial specification')
    raw = packet['adapter_source'].encode('utf-8')
    if (digest(raw) != profile['artifact_sha256'][arm] or packet['artifact_sha256'] != digest(raw)
            or packet['execution_authorization_sha256'] != digest(raw)):
        raise ValueError('Branch executable is not the prospectively pinned reviewed artifact')
    if not raw or len(raw) > 128 * 1024 or len(encoded(packet)) > MAX_PACKET_BYTES:
        raise ValueError('Complete branch packet exceeds the bounded execution profile')
    compile(raw, '<reviewed-trial-branch>', 'exec')
    return trial, spec, profile, raw


def make_branch_packet(store, control_run_id, trial_id, pair_id, arm, input_value, adapter_path, *, execution_authorization_sha256, wire=DEFAULT_WIRE):
    """Construct an exact packet only after prospective trial admission."""
    trial, spec, _ = _plan(store, control_run_id, trial_id, wire=wire)
    pair = next((pair for pair in spec['pairs'] if pair['pair_id'] == pair_id), None)
    if pair is None or arm not in ('baseline', 'candidate'):
        raise ValueError('Branch is outside the prospective pair plan')
    raw = _source(adapter_path)
    packet = {'schema_version': wire.packet_schema, 'trial_id': trial_id, 'pair_id': pair_id,
        'arm': arm, 'run_id': pair[arm + '_run_id'], 'input_hash': pair['input_hash'],
        'versions': spec[arm + '_versions'], 'trial_spec_ref': trial['spec_ref'], 'trial_spec_sha256': trial['spec_sha256'],
        'input': input_value, 'adapter_source': raw.decode('utf-8'), 'artifact_sha256': digest(raw),
        'execution_authorization_sha256': execution_authorization_sha256}
    _validate_packet(store, control_run_id, packet, wire=wire)
    return packet


def branch_reservations(store, control_run_id, trial_id, *, wire=DEFAULT_WIRE):
    """The enforceable call count and scoped process allowances for one branch."""
    _, _, profile = _plan(store, control_run_id, trial_id, wire=wire)
    return {'model_calls': 0, 'tool_calls': 1, 'tokens': 0,
            'cpu_seconds': profile['limits']['cpu_seconds'], 'wall_seconds': profile['limits']['wall_seconds']}


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _execute(raw, envelope, limits):
    start = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    timed_out = False
    with tempfile.TemporaryDirectory(prefix='rsi-trial-branch-') as directory:
        root = Path(directory)
        path = root / 'adapter.py'
        path.write_bytes(raw)
        environment = {'PATH': os.path.dirname(sys.executable), 'PYTHONIOENCODING': 'utf-8',
                       'LC_ALL': 'C', 'TMPDIR': str(root)}
        with (root / 'stdout.json').open('w+b') as stdout, (root / 'stderr.txt').open('w+b') as stderr:
            process = subprocess.Popen([sys.executable, '-I', str(path)], cwd=root, env=environment,
                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True,
                preexec_fn=_limit_process(limits['cpu_seconds'], limits['output_bytes']))
            try:
                process.communicate(encoded(envelope), timeout=limits['wall_seconds'])
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process)
                process.communicate()
            finally:
                _kill_group(process)
            stdout.seek(0); stderr.seek(0)
            output_raw, error_raw = stdout.read(limits['output_bytes'] + 1), stderr.read(limits['output_bytes'] + 1)
        final_source = digest(path.read_bytes()) if path.is_file() and not path.is_symlink() else None
        workspace_id = root.name
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    return {'workspace_id': workspace_id, 'stdout_hex': output_raw.hex(), 'stderr_hex': error_raw.hex(),
        'stdout_sha256': digest(output_raw), 'stderr_sha256': digest(error_raw),
        'returncode': process.returncode, 'timed_out': timed_out, 'wall_seconds': time.monotonic() - start,
        'cpu_seconds': cpu, 'final_source_sha256': final_source,
        'input_envelope_sha256': digest(envelope), 'workspace_removed': True,
        'isolation': 'Fresh local process and temporary workspace; not an OS/network security sandbox',
        'resource_scope': 'Serial child CPU delta; per-process CPU/file-size limits; process-group wall timeout; overall wall observed'}


class BranchExecutor:
    """Controller executor for one prospective arm; the controller owns retries."""
    def __init__(self, store, control_run_id, *, wire=DEFAULT_WIRE):
        self.store, self.control_run_id = store, control_run_id
        self.wire = wire

    def __call__(self, effect, packet_bytes):
        if len(packet_bytes) > MAX_PACKET_BYTES:
            raise ValueError('Branch execution packet exceeds bounded capacity')
        actual = self.store.get(self.control_run_id, 'effect', effect['id'])
        if (actual['status'] != 'dispatching' or effect != actual
                or digest(packet_bytes) != actual['packet_ref']):
            raise ValueError('Branch execution requires the exact persisted dispatching Effect')
        packet = _decode(packet_bytes)
        trial, spec, profile, raw = _validate_packet(self.store, self.control_run_id, packet, wire=self.wire)
        if (any(actual['metadata'].get(key) != packet[key] for key in ('trial_id', 'pair_id', 'arm', 'run_id'))
                or actual['created_seq'] <= trial['admitted_seq']):
            raise ValueError('Effect does not bind the admitted branch identity')
        reservations = branch_reservations(self.store, self.control_run_id, packet['trial_id'], wire=self.wire)
        if set(actual['reservations']) != RESOURCES or any(actual['reservations'][key] < amount for key, amount in reservations.items()):
            raise ValueError('Branch process lacks its complete prospective resource reservation')
        identity = {key: packet[key] for key in IDENTITY_KEYS}
        envelope = {'schema_version': self.wire.input_schema, 'identity': identity, 'input': packet['input']}
        execution = _execute(raw, envelope, profile['limits'])
        output = bytes.fromhex(execution['stdout_hex'])
        metric_names = {metric['name'] for metric in spec['metrics']}
        metrics, status, error, response = dict.fromkeys(metric_names), 'invalid', None, None
        try:
            if execution['timed_out']:
                status = 'incomplete'
                raise ValueError('Branch exceeded its process wall timeout; no completed measurement available')
            if execution['returncode'] != 0 or execution['final_source_sha256'] != packet['artifact_sha256']:
                raise ValueError('Branch failed or changed its authorized executable')
            if (execution['cpu_seconds'] > profile['limits']['cpu_seconds'] + .05
                    or len(output) > profile['limits']['output_bytes']
                    or len(bytes.fromhex(execution['stderr_hex'])) > profile['limits']['output_bytes']):
                raise ValueError('Branch exceeded a registered CPU/output limit')
            response = _decode(output)
            if (not isinstance(response, dict) or set(response) != {'schema_version', 'identity', 'metrics', 'execution_status', 'input_receipt_sha256'}
                    or response['schema_version'] != self.wire.output_schema or response['identity'] != identity
                    or response['input_receipt_sha256'] != packet['input_hash']):
                raise ValueError('Branch output identity, versions or input provenance does not match its frozen arm')
            if (response['execution_status'] not in ('complete', 'incomplete', 'invalid')
                    or not isinstance(response['metrics'], dict) or set(response['metrics']) != metric_names
                    or any(value is not None and (type(value) not in (int, float) or not math.isfinite(value))
                           for value in response['metrics'].values())):
                raise ValueError('Branch metrics violate the frozen measurement schema')
            metrics, status = response['metrics'], response['execution_status']
        except (ValueError, TypeError, UnicodeError) as exc:
            error = str(exc)
        costs = {'model_calls': 0, 'tool_calls': 1, 'tokens': 0,
                 'cpu_seconds': execution['cpu_seconds'], 'wall_seconds': execution['wall_seconds']}
        measurement = {'schema_version': 'trial_measurement_v2', **identity,
            'execution_status': status, 'metrics': metrics, 'costs': costs, 'evidence_refs': [actual['packet_ref']]}
        return {'status': 'succeeded', 'actual_costs': costs, 'trial_measurement': measurement,
            'trial_execution': {**execution, 'response': response, 'error': error, 'artifact_sha256': packet['artifact_sha256'],
                'measurement_scope': 'Frozen adapter outputs with actual process receipts; scientific validity requires separate calibration'}}
