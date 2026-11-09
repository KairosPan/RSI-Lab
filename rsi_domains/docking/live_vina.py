"""Explicit host-provided Vina execution; no diagnosis or prerequisite policy.

This adapter preserves the historical worker profile and validates its native
outputs. A successful Effect means the batch was executed and authenticated,
not that a ligand was valid or that this scientific protocol is applicable.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import resource
import sys
import time
from types import SimpleNamespace

from rsi_lab.adapters import Adapter
from rsi_lab.controller import identity
from rsi_lab.store import digest, encode
from scripts import run_vina_batch as batch_runner
from scripts import vina_contract, vina_worker
from lab_harness import controller as legacy_controller
from lab_harness import docking_archive

ADAPTER_ID = 'docking.vina_batch'
VERSION = '1.0'
MAX_CANDIDATES = 8


def _cpu():
    return sum(row.ru_utime + row.ru_stime for row in
               (resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)))


def prepare(inputs):
    if not isinstance(inputs, dict) or set(inputs) != {'data_dir', 'protocol_path', 'selected_ids', 'output_dir'}:
        raise ValueError('Vina requires explicit data_dir, protocol_path, selected_ids and fresh output_dir')
    data = Path(inputs['data_dir']).resolve(strict=True)
    protocol_path = Path(inputs['protocol_path']).resolve(strict=True)
    output = Path(inputs['output_dir']).resolve()
    if output.exists() or output.is_relative_to(data) or data.is_relative_to(output):
        raise ValueError('Vina output must be a fresh directory separate from frozen input data')
    protocol = vina_worker.read(protocol_path)
    vina_contract.validate_vina_protocol(protocol, live=True)
    ids = inputs['selected_ids']
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_CANDIDATES
            or any(not isinstance(cid, str) or not batch_runner.ID.fullmatch(cid) for cid in ids)
            or len(ids) != len(set(ids))):
        raise ValueError('Expected one to eight unique safe candidate identities')
    pool = vina_worker.read(data / 'candidates.json')['candidates']
    known = {row['candidate_id'] for row in pool}
    if len(known) != len(pool) or not set(ids).issubset(known):
        raise ValueError('Selected candidates are absent or duplicated in the frozen pool')
    # Prepare is read-only: executable version probes run inside the reserved Effect.
    binary_path = protocol.get('vina_binary_path')
    if not isinstance(binary_path, str) or not binary_path.strip():
        raise ValueError('The frozen protocol must explicitly provide vina_binary_path')
    vina = Path(binary_path).resolve(strict=True)
    if vina_worker.sha(vina) != protocol['vina_sha256']:
        raise ValueError('Vina executable hash differs from protocol')
    hashes = batch_runner.input_hashes(data)
    if hashes != protocol['input_sha256']:
        raise ValueError('Data differs from the frozen Vina protocol')
    if any(protocol[key] != value for key, value in batch_runner.source_hashes().items()):
        raise ValueError('Worker source differs from protocol')
    return {'schema_version': 'live_vina_packet_v1', 'data_dir': str(data),
            'protocol_path': str(protocol_path), 'protocol_sha256': vina_worker.sha(protocol_path),
            'input_sha256': hashes, 'selected_ids': ids, 'output_dir': str(output),
            'vina_binary': str(vina), 'vina_sha256': vina_worker.sha(vina),
            'python_executable': sys.executable, 'python_version': sys.version,
            'worker_sources': batch_runner.source_hashes(),
            'limits': {'wall_seconds': len(ids) * 2 * protocol['worker_timeout_seconds'] + 60,
                       'cpu_seconds': len(ids) * 2 * protocol['worker_timeout_seconds'] * protocol['cpu'] + 60}}


def reservations(packet):
    return {'model_calls': 0, 'tokens': 0, 'tool_calls': len(packet['selected_ids']) * 2,
            **packet['limits']}


def verify_batch(batch, frozen_root, request, candidates, protocol):
    """Authenticate raw archives using the existing, unchanged evaluator checks."""
    state = {'requests': {request['request_id']: {'canonical_hash': legacy_controller.canonical_hash(request)}},
             'execution_mode': 'live', 'frozen_hashes': {'run_vina_batch.py': protocol['adapter_sha256']},
             'candidates': candidates}
    _, summary, jobs, records = legacy_controller.validate_batch(batch, frozen_root, state)
    by_id = {row['candidate_id']: row for row in candidates}
    for job in jobs:
        docking_archive._check_pose(job, Path(batch) / 'jobs' / f"{job['candidate_id']}_s{job['seed']}",
                                   by_id[job['candidate_id']]['canonical_smiles'], protocol)
    return summary, jobs, records


def execute(effect, raw):
    if effect['status'] != 'dispatching' or digest(raw) != effect['packet_ref']:
        raise ValueError('Vina execution requires the exact persisted dispatching Effect')
    packet = json.loads(raw)
    started, cpu_before = time.monotonic(), _cpu()
    attempted = 0
    root = Path(packet['output_dir'])
    results, jobs, archive_receipts = [], [], []
    error = None
    try:
        if packet['schema_version'] != 'live_vina_packet_v1':
            raise ValueError('Unsupported Vina packet')
        data, protocol_path, vina = (Path(packet[key]) for key in ('data_dir', 'protocol_path', 'vina_binary'))
        if (vina_worker.sha(protocol_path) != packet['protocol_sha256']
                or batch_runner.input_hashes(data) != packet['input_sha256']
                or vina_worker.sha(vina) != packet['vina_sha256']
                or batch_runner.source_hashes() != packet['worker_sources']
                or sys.executable != packet['python_executable'] or sys.version != packet['python_version']):
            raise ValueError('Execution input, source, interpreter or binary changed after reservation')
        root.mkdir(parents=True, exist_ok=False)
        (root / 'frozen').mkdir()
        (root / 'frozen' / 'protocol.json').write_bytes(protocol_path.read_bytes())
        (root / 'effect_packet.json').write_bytes(raw)
        protocol = vina_worker.read(protocol_path)
        candidates = vina_worker.read(data / 'candidates.json')['candidates']
        ids = packet['selected_ids']
        for index, offset in enumerate(range(0, len(ids), 4)):
            selected = ids[offset:offset + 4]
            rid = f'batch_{index:03d}'
            request = {'schema_version': vina_contract.BATCH_SCHEMA, 'phase': 'online',
                'campaign_id': identity('campaign', effect['id'], index), 'request_id': rid,
                'candidate_ids': selected, 'input_state_version': 0, 'controller_authorized': True,
                'protocol_sha256': packet['protocol_sha256'],
                'candidates_sha256': packet['input_sha256']['candidates.json'],
                'manifest_sha256': packet['input_sha256']['manifest.json']}
            request_path = root / f'{rid}.request.json'
            request_path.write_bytes(encode(request))
            args = SimpleNamespace(data=str(data), protocol=str(protocol_path), vina=str(vina),
                campaign=str(root / f'campaign_{index:03d}'), request=str(request_path), locked_shortlist=None)
            # One outer dispatch, fixed sequential child jobs; no implicit retry.
            with (root / f'{rid}.stdout.txt').open('x') as stdout:
                with contextlib.redirect_stdout(stdout):
                    batch = batch_runner.run(args)
            summary, raw_jobs, records = verify_batch(batch, root, request, candidates, protocol)
            attempted += len(raw_jobs)
            jobs.extend(raw_jobs)
            results.extend(records)
            archive_receipts.append({'batch': str(batch), 'artifact_manifest_sha256': vina_worker.sha(batch / 'artifact_hashes.json'),
                                     'request_sha256': vina_worker.sha(batch / 'request.json'), 'status': summary['status']})
        if batch_runner.input_hashes(data) != packet['input_sha256']:
            raise ValueError('Frozen inputs changed while jobs were executing')
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        # Count every started job even if authentication or a later batch failed.
        if root.is_dir():
            attempted = len(list(root.glob('campaign_*/batches/*/jobs/*/spec.json')))
    costs = {'model_calls': 0, 'tokens': 0, 'tool_calls': attempted,
             'cpu_seconds': _cpu() - cpu_before, 'wall_seconds': time.monotonic() - started}
    result = {'candidate_ids': packet['selected_ids'], 'candidates': results, 'jobs': jobs,
        'protocol_sha256': packet['protocol_sha256'], 'input_sha256': packet['input_sha256'],
        'archive_receipts': archive_receipts, 'output_dir': str(root),
        'accounting': {'adapter_invocations': 1, 'attempted_worker_jobs': attempted,
                       'vina_docking_invocations_in_verified_jobs': sum('command' in row for row in jobs),
                       'cpu_scope': 'Serial parent plus waited descendant process rusage; includes preparation, docking and validation',
                       'tool_calls_unit': 'Attempted candidate-seed worker jobs; binary version probes are internal work'},
        'formal_qualification': False, 'error': error}
    return {'status': 'succeeded' if error is None else 'failed_definite', 'actual_costs': costs, 'result': result}


def publication(effect, receipt):
    result = receipt['result']
    if result.get('error') is not None:
        raise ValueError('An unauthenticated Vina archive cannot be published')
    provenance = {'effect_id': effect['id'], 'adapter_id': ADAPTER_ID, 'adapter_version': VERSION,
                  'receipt_ref': effect['result_ref'], 'protocol_sha256': result['protocol_sha256']}
    raw = {key: result[key] for key in ('candidate_ids', 'jobs', 'archive_receipts', 'output_dir', 'protocol_sha256', 'input_sha256', 'accounting')}
    conclusion = {'candidates': result['candidates'], 'raw_archive_validated': True,
                  'formal_qualification': False,
                  'scope': 'Native MODEL 1 score, exact candidate/seed coverage, raw PoseBusters and identity receipts; no prerequisite or target-utility conclusion'}
    return {'facts': [{'id': identity('vina_raw', effect['id']), 'kind': 'raw_fact', 'content': raw, 'provenance': provenance}],
            'conclusions': [{'id': identity('vina_validation', effect['id']), 'kind': 'validation', 'content': conclusion, 'provenance': provenance}],
            'data': result}


def make_adapter():
    return Adapter(adapter_id=ADAPTER_ID, version=VERSION, effect_kind='docking_vina_batch',
        manifest={'scope': 'Explicit CDK2 1DI8 Vina 1.2.7 profile; 1–8 candidates, seeds 17/43, sequential jobs',
            'input_contract': {'data_dir': 'frozen data directory', 'protocol_path': 'frozen protocol JSON',
                               'selected_ids': 'one to eight candidate IDs', 'output_dir': 'fresh output directory'},
            'output_contract': ['native archives', 'candidate-seed records', 'verified paired aggregates', 'actual compute cost'],
            'limitations': ['No scientific admission or prerequisite diagnosis', 'No automatic retries',
                           'Exploratory scores do not qualify a formal shortlist',
                           'Worker timeout is enforced; total CPU and wall reservations are estimates']},
        prepare=prepare, reservations=reservations, execute=execute, publication=publication,
        source_paths=(str(Path(__file__)), batch_runner.__file__, vina_worker.__file__, vina_contract.__file__,
                      legacy_controller.__file__, docking_archive.__file__))
