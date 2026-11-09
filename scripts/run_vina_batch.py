#!/usr/bin/env python3
"""Freeze a standalone AutoDock Vina protocol or execute one reserved CPU batch."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time

if __package__:
    from . import vina_contract as contract
    from . import vina_worker as worker
else:
    import vina_contract as contract
    import vina_worker as worker

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z')
REQUIRED_INPUTS = {'candidates.json', 'manifest.json', 'receptor/receptor_manifest.json',
                   'receptor/receptor.pdbqt', 'receptor/receptor_effective.pdb', 'receptor/reference.sdf'}
PUBLIC_INPUTS = ('assay_details.json', 'features.json', 'structure_validation.json', 'task_context.json', 'chemical_states.json')
PACKAGES = ('rdkit', 'meeko', 'posebusters', 'gemmi', 'numpy', 'pandas', 'scipy')
CENTER = [-8.773272727272728, 50.057272727272725, 12.795045454545454]
SIZE = [22.0] * 3


def source_hashes():
    return {'runner_sha256': worker.sha(worker.__file__), 'adapter_sha256': worker.sha(__file__),
            'contract_sha256': worker.sha(contract.__file__)}


def versions():
    return {name: importlib.metadata.version(name) for name in PACKAGES}


def load_chemical_states(data, *, candidates=None, manifest=None):
    """Validate the optional single-state registry without running chemistry.

    Parent identity is for counting/splits; prepared identity is for pose checks.
    The frozen preparation report is provenance, not a population estimate.
    """
    data = Path(data)
    manifest = worker.read(data / 'manifest.json') if manifest is None else manifest
    candidates = worker.read(data / 'candidates.json')['candidates'] if candidates is None else candidates
    prepared = manifest.get('chemical_preparation')
    path = data / 'chemical_states.json'
    if prepared is None:
        if path.exists() or any('prepared_state_id' in row or 'canonical_parent_identity' in row for row in candidates):
            raise ValueError('Chemical states require a declared preparation method')
        return None, {}
    if not isinstance(prepared, dict) or set(prepared) != {'method'} or not path.is_file():
        raise ValueError('Chemical preparation requires method and chemical_states.json')
    method = prepared['method']
    required = {'schema_version': 'molscrub_single_state_preparation_v1', 'engine': 'MolScrub',
                'version': '0.3.2', 'pka_model': 'rules', 'enumerate_tautomers': True,
                'tautomer_rounds': 2, 'enumerate_stereoisomers': False, 'skip_gen3d': True,
                'admission': 'exactly_one_predicted_state_with_preserved_identity_and_stereochemistry',
                'population_estimates_available': False}
    if not isinstance(method, dict) or any(method.get(k) != v or type(method.get(k)) is not type(v) for k, v in required.items()):
        raise ValueError('Unsupported single-state chemical preparation method')
    ph = method.get('pH', {})
    if (not isinstance(ph, dict) or ph.get('low') != 7.5 or ph.get('high') != 8.0
            or ph.get('interpretation') != 'exploratory_interval_not_common_assay_pH'):
        raise ValueError('Expected exploratory chemical preparation pH interval 7.5–8.0')
    sources = method.get('source_hashes')
    if (not isinstance(method.get('rdkit_version'), str) or not method['rdkit_version']
            or not isinstance(sources, dict) or not sources
            or any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v) for v in sources.values())):
        raise ValueError('Chemical preparation lacks versioned source provenance')
    registry = worker.read(path)
    if registry.get('schema_version') != 'lab_chemical_states_v1' or registry.get('method') != method:
        raise ValueError('Chemical registry method differs from manifest')
    rows = registry.get('records')
    manifest_rows = manifest.get('candidates')
    if not isinstance(rows, list) or not isinstance(manifest_rows, list):
        raise ValueError('Chemical registry and manifest require candidate records')
    by_id = {row['candidate_id']: row for row in rows}
    declared = {row['candidate_id']: row for row in manifest_rows}
    ids = {row['candidate_id'] for row in candidates}
    if len(ids) != len(candidates) or len(by_id) != len(rows) or len(declared) != len(manifest_rows) or set(by_id) != ids or set(declared) != ids:
        raise ValueError('Chemical registry must exactly cover the frozen candidate pool')
    parents, states_seen = set(), set()
    for candidate in candidates:
        cid = candidate['candidate_id']
        row = by_id[cid]
        states = row.get('states')
        if row.get('status') != 'accepted' or row.get('reason') is not None or not isinstance(states, list) or len(states) != 1:
            raise ValueError('Only exactly one accepted chemical state may enter docking')
        if (type(row.get('unique_state_count')) is not int or row['unique_state_count'] != 1
                or not isinstance(states[0], dict) or states[0].get('validation_issues') != []):
            raise ValueError('Accepted chemical state must have one unique state and no validation issues')
        for field in ('source_smiles', 'canonical_parent_identity', 'prepared_state_id', 'prepared_smiles'):
            value = row.get(field)
            if not isinstance(value, str) or not value or value != value.strip() or candidate.get(field) != value or declared[cid].get(field) != value:
                raise ValueError(f'Chemical provenance differs between registry and candidate: {field}')
        smiles = row['prepared_smiles']
        sid = 'state_' + hashlib.sha256(smiles.encode()).hexdigest()[:24]
        if (row['prepared_state_id'] != sid or states[0].get('state_id') != sid
                or states[0].get('canonical_smiles') != smiles or type(states[0].get('formal_charge')) is not int
                or candidate.get('smiles') != smiles or candidate.get('canonical_smiles') != smiles
                or declared[cid].get('canonical_smiles') != smiles):
            raise ValueError('Prepared molecular identity differs from the single registered state')
        if row['canonical_parent_identity'] in parents or smiles in states_seen:
            raise ValueError('Duplicate parent or prepared identity within the chemical registry')
        parents.add(row['canonical_parent_identity'])
        states_seen.add(smiles)
    return method, by_id


def input_hashes(data):
    load_chemical_states(data)
    names = REQUIRED_INPUTS | {name for name in PUBLIC_INPUTS if (data / name).is_file()}
    hashes = {}
    for name in sorted(names):
        path = (data / name).resolve()
        if not path.is_relative_to(data) or not path.is_file():
            raise ValueError(f'Missing or escaping input {name}')
        hashes[name] = worker.sha(path)
    return hashes


def check_box(data, protocol):
    receptor = worker.read(data / 'receptor/receptor_manifest.json')
    for name, values in [('center', CENTER), ('size', SIZE)]:
        if protocol.get(name) != receptor.get(name) or len(receptor[name]) != 3 or any(not math.isclose(v, expected, rel_tol=0, abs_tol=1e-9)
                for v, expected in zip(receptor[name], values, strict=True)):
            raise ValueError('Binding site differs from the frozen CDK2 1DI8 pilot box')


def freeze_protocol(args):
    data, vina = Path(args.data).resolve(), Path(args.vina).resolve()
    receptor = worker.read(data / 'receptor/receptor_manifest.json')
    checks = worker.read(args.pose_config)
    checks = checks.get('posebusters_configs', checks).get('dock')
    if not isinstance(checks, dict) or not checks.get('modules'):
        raise ValueError('Expected a dock PoseBusters config with modules')
    protocol = {**contract.PROFILE, **worker.engine_receipt(vina), **source_hashes(),
                'vina_binary_path': str(vina),
                'protocol_id': args.protocol_id, 'phase': 'standalone_vina_pilot', 'frozen_at': worker.now(),
                'center': receptor['center'], 'size': receptor['size'], 'tau': None, 'm_min': None,
                'preparation': 'RDKit ETKDGv3 seed 20261019; MMFF94s max500; Meeko single PDBQT setup',
                'versions': versions(), 'input_sha256': input_hashes(data), 'posebusters_configs': {'dock': checks},
                'score_semantics': {'vina_affinity': 'Vina MODEL 1 binding score in kcal/mol; lower is better'}}
    chemical_method, _ = load_chemical_states(data)
    if chemical_method is not None:
        protocol['chemical_preparation'] = {'method': chemical_method}
    contract.validate_vina_protocol(protocol, live=True)
    check_box(data, protocol)
    path = Path(args.out).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(protocol, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'protocol': str(path), 'protocol_sha256': worker.sha(path), **worker.engine_receipt(vina)}))
    return protocol


def validate_request(request, known):
    keys = {'schema_version', 'phase', 'campaign_id', 'request_id', 'candidate_ids', 'input_state_version',
            'controller_authorized', 'protocol_sha256', 'candidates_sha256', 'manifest_sha256'}
    if request.get('phase') == 'terminal':
        keys.add('locked_shortlist_sha256')
    if set(request) != keys or request['schema_version'] != contract.BATCH_SCHEMA or request['phase'] not in ('online', 'terminal'):
        raise ValueError('Unsupported Vina batch schema or phase')
    if request['controller_authorized'] is not True or type(request['input_state_version']) is not int or request['input_state_version'] < 0:
        raise ValueError('Expected a Controller-authorized request with nonnegative state version')
    for key in ('campaign_id', 'request_id'):
        if not isinstance(request[key], str) or not ID.fullmatch(request[key]):
            raise ValueError(f'Invalid {key}')
    ids = request['candidate_ids']
    if not isinstance(ids, list) or not 1 <= len(ids) <= 4 or any(not isinstance(cid, str) or not ID.fullmatch(cid) for cid in ids):
        raise ValueError('Expected 1–4 candidate IDs')
    if len(set(ids)) != len(ids) or not set(ids).issubset(known):
        raise ValueError('Unknown or duplicate candidates')
    for key in keys:
        if key.endswith('_sha256') and (not isinstance(request[key], str) or not re.fullmatch(r'[a-f0-9]{64}', request[key])):
            raise ValueError(f'Invalid request hash {key}')


def validate_inputs(data, protocol_path, protocol, request, vina):
    contract.validate_vina_protocol(protocol, live=True)
    check_box(data, protocol)
    chemical_method, _ = load_chemical_states(data)
    expected_chemical = None if chemical_method is None else {'method': chemical_method}
    if protocol.get('chemical_preparation') != expected_chemical:
        raise ValueError('Chemical preparation differs from frozen protocol')
    if {k: protocol[k] for k in source_hashes()} != source_hashes():
        raise ValueError('Vina worker, adapter or shared contract differs from frozen source')
    engine = worker.engine_receipt(vina)
    if any(protocol.get(key) != value for key, value in engine.items()):
        raise ValueError('Vina executable differs from frozen protocol')
    if protocol.get('versions') != versions():
        raise ValueError('Scientific package versions differ from freeze-protocol')
    if protocol.get('input_sha256') != input_hashes(data):
        raise ValueError('Input hashes differ from freeze-protocol')
    for key, path in [('protocol_sha256', protocol_path), ('candidates_sha256', data / 'candidates.json'), ('manifest_sha256', data / 'manifest.json')]:
        if request[key] != worker.sha(path):
            raise ValueError(f'Frozen request {key} mismatch')
    return engine


def aggregate(cid, jobs):
    rows = sorted((r for r in jobs if r['candidate_id'] == cid), key=lambda r: r['seed'])
    paired = [r['seed'] for r in rows] == list(contract.SEEDS)
    energies = [r['scores']['vina_affinity'] for r in rows if r.get('scores') is not None]
    raw = statistics.median(energies) if paired and len(energies) == 2 else None
    valid = raw is not None and all(r['status'] == 'completed' and r.get('pose_valid') is True for r in rows)
    return {'candidate_id': cid, 'completed_repeats': sum(r['status'] == 'completed' for r in rows),
            'valid_repeats': sum(r.get('pose_valid') is True for r in rows), 'two_valid_repeats': valid,
            'statuses': ';'.join(r['status'] for r in rows), 'raw_median_vina_affinity': raw,
            'qualified_median_vina_affinity': raw if valid else None,
            'seed_range_vina_affinity': max(energies) - min(energies) if raw is not None else None}


def execute_job(job, spec, engine):
    job.mkdir()
    worker.dump(job / 'spec.json', spec)
    started, timed_out = time.monotonic(), False
    with (job / 'worker.stdout.txt').open('w') as out, (job / 'worker.stderr.txt').open('w') as err:
        process = subprocess.Popen([sys.executable, str(Path(worker.__file__).resolve()), '--worker', str(job / 'spec.json')],
                                   stdout=out, stderr=err, start_new_session=True)
        try:
            process.wait(timeout=spec['protocol']['worker_timeout_seconds'])
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    result = {**engine, 'kind': 'candidate', 'candidate_id': spec['candidate']['candidate_id'], 'seed': spec['seed'],
              'status': 'worker_error', 'scores': None, 'pose_valid': None, 'identity_preserved': None, 'cpu_seconds': None}
    if (job / 'result.json').is_file():
        result.update(worker.read(job / 'result.json'))
    if timed_out or process.returncode != 0:
        result.update(worker_reported_status=result['status'], status='worker_timeout' if timed_out else 'worker_error',
                      error=f'Parent observed timeout={timed_out} exit={process.returncode}', cpu_seconds=None)
    result.update(engine_provenance='batch_preflight_and_frozen_protocol', parent_observed_returncode=process.returncode, parent_observed_wall_seconds=time.monotonic() - started,
                  artifacts={p.name: worker.sha(p) for p in job.iterdir() if p.is_file() and p.name != 'result.json'})
    worker.dump(job / 'result.json', result)
    return result


def run(args):
    data, campaign = Path(args.data).resolve(), Path(args.campaign).resolve()
    protocol_path, vina = Path(args.protocol).resolve(), Path(args.vina).resolve()
    request, protocol = worker.read(args.request), worker.read(protocol_path)
    pool = worker.read(data / 'candidates.json')['candidates']
    by_id = {row['candidate_id']: row for row in pool}
    if len(by_id) != len(pool):
        raise ValueError('Candidate pool has duplicate IDs')
    validate_request(request, set(by_id))
    engine = validate_inputs(data, protocol_path, protocol, request, vina)
    phase, rid = request['phase'], request['request_id']
    if phase == 'terminal':
        if not args.locked_shortlist or worker.sha(args.locked_shortlist) != request['locked_shortlist_sha256']:
            raise ValueError('Terminal request requires the exact locked shortlist')
        lock = worker.read(args.locked_shortlist)
        ids = lock['candidate_ids']
        if lock['campaign_id'] != request['campaign_id'] or not lock.get('locked_at') or len(ids) != 5 or len(set(ids)) != 5 or not set(ids).issubset(by_id) or not set(request['candidate_ids']).issubset(ids):
            raise ValueError('Terminal request is outside the locked shortlist')
    elif args.locked_shortlist:
        raise ValueError('Online phase does not use a locked shortlist')
    identity = {key: request[key] for key in ('campaign_id', 'phase', 'protocol_sha256', 'candidates_sha256', 'manifest_sha256')}
    cap = 8 if phase == 'online' else 5
    campaign.mkdir(parents=True, exist_ok=True)
    with (campaign / '.lock').open('a') as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        statepath = campaign / 'state.json'
        state = worker.read(statepath) if statepath.exists() else {
            'identity': identity, 'state_version': 0, 'reserved_candidate_ids': [], 'requests': {}, 'active_request': None,
            'locked_shortlist_sha256': request.get('locked_shortlist_sha256'), **source_hashes()}
        if state['identity'] != identity or state['state_version'] != request['input_state_version'] or state['locked_shortlist_sha256'] != request.get('locked_shortlist_sha256') or any(state[key] != value for key, value in source_hashes().items()):
            raise ValueError('Campaign identity, state version or frozen worker changed')
        if state['active_request'] or rid in state['requests'] or set(request['candidate_ids']) & set(state['reserved_candidate_ids']):
            raise ValueError('Prior unfinished request or repeated reservation; no implicit retry')
        if len(state['reserved_candidate_ids']) + len(request['candidate_ids']) > cap:
            raise ValueError('Phase candidate budget exceeded')
        batch = campaign / 'batches' / rid
        (batch / 'jobs').mkdir(parents=True, exist_ok=False)
        worker.dump(batch / 'request.json', request)
        (batch / 'protocol.json').write_bytes(protocol_path.read_bytes())
        if args.locked_shortlist:
            (batch / 'locked_shortlist.json').write_bytes(Path(args.locked_shortlist).read_bytes())
        worker.dump(batch / 'engine.json', engine)
        state['state_version'] += 1
        state['reserved_candidate_ids'].extend(request['candidate_ids'])
        state['active_request'] = rid
        state['requests'][rid] = {'status': 'reserved', 'request_sha256': worker.sha(batch / 'request.json')}
        worker.dump(statepath, state)
        started, results = time.monotonic(), []
        for cid in request['candidate_ids']:
            for seed in contract.SEEDS:
                spec = {'kind': 'candidate', 'candidate': by_id[cid], 'seed': seed, 'data': str(data), 'vina': str(vina), 'protocol': protocol}
                result = execute_job(batch / 'jobs' / f'{cid}_s{seed}', spec, engine)
                results.append(result)
                print(json.dumps({'candidate_id': cid, 'seed': seed, 'status': result['status']}), flush=True)
        summary = {**identity, **engine, **source_hashes(), 'schema_version': contract.BATCH_SCHEMA, 'request_id': rid,
                   'request_sha256': worker.sha(batch / 'request.json'), 'state_version': state['state_version'],
                   'status': 'completed' if all(r['status'] == 'completed' for r in results) else 'completed_with_failures',
                   'jobs': results, 'candidates': [aggregate(cid, results) for cid in request['candidate_ids']],
                   'wall_seconds': time.monotonic() - started, 'worker_cpu_seconds': sum(r.get('cpu_seconds') or 0 for r in results),
                   'cpu_accounting_complete': all(r.get('cpu_seconds') is not None for r in results),
                   'locked_shortlist_sha256': request.get('locked_shortlist_sha256'), 'reserved_candidates_total': len(state['reserved_candidate_ids']),
                   'remaining_candidate_slots': cap - len(state['reserved_candidate_ids'])}
        worker.dump(batch / 'batch_summary.json', summary)
        worker.dump(batch / 'artifact_hashes.json', {str(p.relative_to(batch)): worker.sha(p) for p in sorted(batch.rglob('*')) if p.is_file()})
        state['active_request'] = None
        state['requests'][rid].update(status=summary['status'], artifacts_sha256=worker.sha(batch / 'artifact_hashes.json'))
        worker.dump(statepath, state)
        print(json.dumps({'batch': str(batch), 'status': summary['status'], 'remaining_candidate_slots': summary['remaining_candidate_slots']}))
        return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    freeze = sub.add_parser('freeze-protocol', help='Pin inputs, standalone binary, scientific packages and CPU worker source')
    for name in ('data', 'vina', 'out'):
        freeze.add_argument('--' + name, required=True)
    freeze.add_argument('--pose-config', default=str(Path(__file__).with_name('vina_pose_checks.json')))
    freeze.add_argument('--protocol-id', default='cdk2_autodock_vina_v1')
    batch = sub.add_parser('run', help='Execute one Controller-authored Vina batch locally')
    for name in ('data', 'protocol', 'vina', 'campaign', 'request'):
        batch.add_argument('--' + name, required=True)
    batch.add_argument('--locked-shortlist')
    args = parser.parse_args()
    (freeze_protocol if args.action == 'freeze-protocol' else run)(args)


if __name__ == '__main__':
    main()
