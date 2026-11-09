"""Host-bound execution of a reviewed and accepted omission diagnostic.

The model chooses whether to request this capability. Only a reviewed host
binding selects executable bytes and the original-input bundle it may inspect.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time
import types

from rsi_lab.adapters import Adapter
from rsi_lab.controller import identity
from rsi_lab.store import digest, encode
from . import omission_contract


def prepare(inputs, binding):
    if set(inputs) != {'output_path'}:
        raise ValueError('Use exactly output_path; original inputs are fixed by the accepted host binding')
    target = Path(inputs['output_path']).resolve()
    root = Path(binding['output_root']).resolve()
    if not target.is_relative_to(root) or target == root or target.exists():
        raise ValueError('Diagnostic output must be a fresh file beneath its registered output root')
    artifact, source = Path(binding['artifact_path']), Path(binding['input_path'])
    if digest(artifact.read_bytes()) != binding['artifact_sha256'] or digest(source.read_bytes()) != binding['input_sha256']:
        raise ValueError('Reviewed artifact or bound original input changed')
    return {'schema_version': 'omission_diagnostic_packet_v1', **binding, 'output_path': str(target),
            'python_executable': sys.executable, 'python_version': sys.version}


def reservations(packet):
    return {'tool_calls': 1, 'model_calls': 0, 'tokens': 0, 'cpu_seconds': 30, 'wall_seconds': 60}


def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
    resource.setrlimit(resource.RLIMIT_FSIZE, (262144, 262144))


def execute(effect, raw, binding):
    if effect['status'] != 'dispatching' or digest(raw) != effect['packet_ref']:
        raise ValueError('Only the exact admitted diagnostic packet may execute')
    packet = json.loads(raw)
    start, parent = time.monotonic(), time.process_time()
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    response = None
    error = None
    output = b''
    stderr = b''
    dispatched = False
    try:
        if any(packet[k] != v for k, v in binding.items()):
            raise ValueError('Diagnostic packet differs from the reviewed host binding')
        source = Path(packet['artifact_path']).read_bytes()
        payload_raw = Path(packet['input_path']).read_bytes()
        if digest(source) != packet['artifact_sha256'] or digest(payload_raw) != packet['input_sha256']:
            raise ValueError('Artifact or source bundle changed after reservation')
        if sys.executable != packet['python_executable'] or sys.version != packet['python_version']:
            raise ValueError('Interpreter differs from the admitted packet')
        payload = json.loads(payload_raw)
        with tempfile.TemporaryDirectory(prefix='rsi-omission-') as folder:
            folder = Path(folder)
            artifact = folder/'adapter.py'
            artifact.write_bytes(source)
            with (folder/'stdout').open('w+b') as out, (folder/'stderr').open('w+b') as err:
                proc = subprocess.Popen([sys.executable, '-I', str(artifact)], stdin=subprocess.PIPE,
                    stdout=out, stderr=err, cwd=folder, start_new_session=True, preexec_fn=limits,
                    env={'PATH': str(Path(sys.executable).parent), 'PYTHONIOENCODING': 'utf-8', 'LC_ALL': 'C'})
                dispatched = True
                try:
                    proc.communicate(encode({'schema_version':'capability_input_v2','case_id':'actual-1DI8','input':payload}), timeout=60)
                finally:
                    try: os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    proc.wait()
                out.seek(0); err.seek(0)
                output, stderr = out.read(262145), err.read(262145)
            if proc.returncode != 0 or digest(artifact.read_bytes()) != packet['artifact_sha256']:
                raise ValueError('Diagnostic failed or modified its executable')
        response = json.loads(output, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
        if (set(response) != {'schema_version','ok','output','identity','source_receipts','error'}
                or response['schema_version'] != 'capability_response_v2' or response['ok'] is not True
                or response['error'] is not None or not isinstance(response['output'], dict)
                or response['identity'] != payload['identity']
                or response['source_receipts'] != [{'kind':'input','sha256':digest(payload)}]):
            raise ValueError('Actual diagnostic response failed identity/provenance/output verification')
        result = response['output']
        if (set(result) != set(omission_contract.OUTPUT_CONTRACT)
                or result['schema_version'] != 'receptor_omission_report_v1'
                or result['identity'] != payload['identity']
                or result['adapter'] != {'capability_id': binding['capability_id'], 'version': binding['version']}
                or result['input_hashes'] != {key: value['sha256'] for key, value in payload['sources'].items()}
                or result['limitations'] != omission_contract.LIMITATIONS
                or not isinstance(result['residues'], list)
                or len(result['residues']) != len(payload['residues'])
                or not isinstance(result['unknown_positions'], list)
                or any(row.get('position', 'absent') is not None for row in result['unknown_positions'])):
            raise ValueError('Measured report differs from the accepted scope and original source identities')
        encode(result)  # Reject nonfinite values anywhere in the complete output.
        path = Path(packet['output_path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream: stream.write(output)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    costs = {'tool_calls': int(dispatched), 'model_calls': 0, 'tokens': 0,
             'cpu_seconds': time.process_time()-parent+after.ru_utime+after.ru_stime-child.ru_utime-child.ru_stime,
             'wall_seconds': time.monotonic()-start}
    return {'status':'succeeded' if error is None else 'failed_definite','actual_costs':costs,
            'result':response,'stdout':output.decode(errors='replace'),'stderr':stderr.decode(errors='replace'),
            'output_sha256':digest(output),'input_sha256':packet['input_sha256'],
            'artifact_sha256':packet['artifact_sha256'],'error':error}


def publication(effect, receipt):
    if receipt['error'] is not None:
        raise ValueError('Failed diagnostic cannot publish a successful measurement')
    projection = {'source_kind': 'exact field projection; not the complete original report',
                  'full_result_ref': effect['result_ref'],
                  'residues': [{key: row[key] for key in ('residue_id', 'observed_atom_count',
                               'min_distance_to_box', 'min_distance_to_reference')}
                               for row in receipt['result']['output']['residues']],
                  'unknown_positions': receipt['result']['output']['unknown_positions'],
                  'limitations': receipt['result']['output']['limitations']}
    return {'facts':[{'id':identity('omission_geometry',effect['id']),'kind':'raw_fact',
             'content':receipt['result']['output'],'provenance':{'effect_id':effect['id'],
             'source':'accepted executable coordinate measurement','artifact_sha256':receipt['artifact_sha256'],
             'input_sha256':receipt['input_sha256']}},
             {'id': identity('omission_projection', effect['id']), 'kind': 'raw_fact', 'content': projection,
              'provenance': {'effect_id': effect['id'], 'full_original_result_ref': effect['result_ref'],
                             'transform': 'Exact field selection, no inference or omitted-source completion claim'}}],
            'conclusions':[], 'data':receipt['result']['output']}


def _bound(fn, binding):
    result = types.FunctionType(fn.__code__, fn.__globals__, fn.__name__, (json.loads(encode(binding)),))
    result.__module__, result.__qualname__ = fn.__module__, fn.__qualname__
    return result


def make_adapter(binding):
    return Adapter(adapter_id=binding['capability_id'], version=binding['version'],
        effect_kind='docking_omission_geometry',
        manifest={'artifact_sha256':binding['artifact_sha256'],
            'scope':'Read-only observed geometry for the exact bound 1DI8 originals; no candidate accessibility or scientific admission',
            'input_contract':{'output_path':'fresh JSON file below '+binding['output_root']},
            'bound_input_sha256':binding['input_sha256'],
            'output_contract':'Accepted capability_response_v2; measured distances, inventories, unknown positions and explicit limits'},
        prepare=_bound(prepare,binding), reservations=reservations, execute=_bound(execute,binding),
        publication=publication, source_paths=(binding['artifact_path'],binding['input_path'],omission_contract.__file__), requires_acceptance=True)
