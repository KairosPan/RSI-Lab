#!/usr/bin/env python3
"""Bounded exploratory screening with frozen Vina workers; not a Lab campaign."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    temp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def run(root, vina):
    root, vina = Path(root).resolve(), Path(vina).resolve()
    plan, protocol = read(root / 'plan.json'), read(root / 'protocol.json')
    # Import only this run's frozen, auditable worker and contract.
    sys.path.insert(0, str(root / 'frozen'))
    import run_vina_batch as adapter
    import vina_worker as worker
    data = root / 'data'
    if plan['driver_sha256'] != sha(__file__):
        raise ValueError('Screen driver differs from frozen plan')
    if plan['protocol_sha256'] != sha(root / 'protocol.json'):
        raise ValueError('Protocol differs from frozen plan')
    adapter.contract.validate_vina_protocol(protocol, live=True)
    adapter.check_box(data, protocol)
    if protocol['input_sha256'] != adapter.input_hashes(data):
        raise ValueError('Input hashes differ from frozen protocol')
    if protocol['versions'] != adapter.versions():
        raise ValueError('Scientific package versions changed')
    if any(protocol[k] != v for k, v in adapter.source_hashes().items()):
        raise ValueError('Frozen worker source mismatch')
    engine = worker.engine_receipt(vina)
    if any(protocol[k] != v for k, v in engine.items()):
        raise ValueError('Vina identity mismatch')
    candidates = read(data / 'candidates.json')['candidates']
    ids = [row['candidate_id'] for row in candidates]
    if (len(ids) != plan['candidate_count'] or len(set(ids)) != len(ids)
            or any(not adapter.ID.fullmatch(cid) for cid in ids)):
        raise ValueError('Invalid candidate registry')
    if plan['seeds'] != protocol['seeds'] or not 1 <= plan['max_external_processes'] <= 4:
        raise ValueError('Invalid seed or concurrency plan')
    if plan['max_seed_jobs'] != len(ids) * len(plan['seeds']):
        raise ValueError('Inconsistent job budget')
    deadline_epoch = datetime.fromisoformat(plan['compute_deadline_utc']).timestamp()
    remaining = deadline_epoch - time.time()
    if remaining <= 0:
        raise ValueError('Compute deadline already expired')
    deadline = time.monotonic() + remaining
    started = time.monotonic()
    started_at = now()
    jobs = root / 'jobs'
    jobs.mkdir(exist_ok=True)
    if any(jobs.iterdir()) or (root / 'execution_summary.json').exists():
        raise ValueError('Run already started; no implicit resume or retry')
    pending = [(row, seed) for row in candidates for seed in plan['seeds']]
    active, results = {}, []
    interrupted = None

    def event(kind, **fields):
        with (root / 'events.jsonl').open('a') as out:
            out.write(json.dumps({'at': now(), 'event': kind, **fields}) + '\n')

    def finish(pid, reason=None):
        item = active.pop(pid)
        process, job, begin, streams = item
        if reason:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        for stream in streams:
            stream.close()
        spec = read(job / 'spec.json')
        try:
            result = read(job / 'result.json') if (job / 'result.json').exists() else {}
        except (ValueError, OSError) as exc:
            result = {'status': 'invalid_worker_receipt', 'error': str(exc)}
            reason = reason or 'worker_error'
        if reason or process.returncode != 0 or not result:
            result.update(worker_reported_status=result.get('status'), status=reason or 'worker_error',
                          scores=None, pose_valid=None, identity_preserved=None, cpu_seconds=None)
        result.update(candidate_id=spec['candidate']['candidate_id'], seed=spec['seed'],
                      parent_observed_returncode=process.returncode,
                      parent_observed_wall_seconds=time.monotonic() - begin,
                      **engine)
        result['artifacts'] = {p.name: sha(p) for p in job.iterdir() if p.is_file() and p.name != 'result.json'}
        dump(job / 'result.json', result)
        results.append(result)
        event('job_settled', candidate_id=result['candidate_id'], seed=result['seed'], status=result['status'])
        print(json.dumps({'settled': len(results), 'total': len(ids) * 2,
                          'candidate_id': result['candidate_id'], 'seed': result['seed'],
                          'status': result['status'], 'pose_valid': result.get('pose_valid')}), flush=True)

    def stop(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')

    old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
    event('started', candidate_count=len(ids), deadline=plan['compute_deadline_utc'])
    try:
        while pending or active:
            current = time.monotonic()
            for pid, (process, job, begin, streams) in list(active.items()):
                if process.poll() is not None:
                    finish(pid)
                elif current >= deadline:
                    finish(pid, 'global_deadline_timeout')
                elif current - begin >= protocol['worker_timeout_seconds']:
                    finish(pid, 'worker_timeout')
            if current >= deadline:
                break
            while pending and len(active) < plan['max_external_processes'] and time.monotonic() < deadline:
                candidate, seed = pending.pop(0)
                job = jobs / f"{candidate['candidate_id']}_s{seed}"
                job.mkdir()
                spec = {'kind': 'candidate', 'candidate': candidate, 'seed': seed,
                        'data': str(data), 'vina': str(vina), 'protocol': protocol}
                dump(job / 'spec.json', spec)
                event('job_reserved', candidate_id=candidate['candidate_id'], seed=seed)
                streams = [(job / name).open('w') for name in ('worker.stdout.txt', 'worker.stderr.txt')]
                env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'OMP_NUM_THREADS': '1',
                       'OPENBLAS_NUM_THREADS': '1', 'VECLIB_MAXIMUM_THREADS': '1'}
                try:
                    process = subprocess.Popen([sys.executable, str(root / 'frozen/vina_worker.py'),
                                                '--worker', str(job / 'spec.json')],
                                               stdout=streams[0], stderr=streams[1],
                                               start_new_session=True, env=env)
                except Exception as exc:
                    for stream in streams:
                        stream.close()
                    result = {'candidate_id': candidate['candidate_id'], 'seed': seed,
                              'status': 'launch_failed', 'error': str(exc), 'scores': None,
                              'pose_valid': None, 'identity_preserved': None, 'cpu_seconds': 0.0}
                    dump(job / 'result.json', result)
                    results.append(result)
                    event('job_launch_failed', candidate_id=candidate['candidate_id'], seed=seed, error=str(exc))
                    raise
                active[process.pid] = (process, job, time.monotonic(), streams)
                event('job_spawned', candidate_id=candidate['candidate_id'], seed=seed, pid=process.pid)
            time.sleep(0.1)
    except BaseException as exc:
        interrupted = f'{type(exc).__name__}: {exc}'
    finally:
        # Terminate every owned process group before attempting any receipt IO.
        for process, job, begin, streams in active.values():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for process, job, begin, streams in active.values():
            process.wait()
        for pid in list(active):
            try:
                finish(pid, 'parent_interrupted' if interrupted else 'global_deadline_timeout')
            except Exception as exc:
                interrupted = (interrupted or '') + f'; receipt failure: {exc}'
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        summary = {'schema_version': 'bounded_vina_screen_execution_v1', 'started_at': started_at,
                   'finished_at': now(), 'wall_seconds': time.monotonic() - started,
                   'status': 'interrupted' if interrupted else 'deadline_reached' if pending or any(
                       r['status'] == 'global_deadline_timeout' for r in results) else 'completed',
                   'error': interrupted, 'candidate_count': len(ids), 'settled_seed_jobs': len(results),
                   'unrun_seed_jobs': [{'candidate_id': row['candidate_id'], 'seed': seed} for row, seed in pending],
                   'worker_cpu_seconds': sum(r.get('cpu_seconds') or 0 for r in results),
                   'cpu_accounting_complete': all(r.get('cpu_seconds') is not None for r in results),
                   'plan_sha256': sha(root / 'plan.json'), 'protocol_sha256': sha(root / 'protocol.json')}
        dump(root / 'execution_summary.json', summary)
        event('finished', **summary)
    if interrupted:
        raise RuntimeError(interrupted)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--vina', required=True)
    args = parser.parse_args()
    with (Path(args.run) / '.screen.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(run(args.run, args.vina)))


if __name__ == '__main__':
    main()
