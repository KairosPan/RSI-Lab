"""Replaceable CSV aggregation capability; no scheduling logic lives here."""
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

from rsi_lab.adapters import Adapter
from rsi_lab.plans import validate_task_plan
from rsi_lab.store import digest, encode

ADAPTER_ID = 'tabular.aggregate_energy'
VERSION = '1.0'
PROTOCOL_VERSION = 'csv_explicit_units_v1'
PROTOCOL_PARAMETERS = {'unit_factors': {'Wh':.001,'kWh':1},
                       'duplicate_record_policy':'reject','missing_unit_policy':'reject'}
LIMITS = {'wall_seconds': 3, 'cpu_seconds': 1, 'output_bytes': 131072}


def source_bytes():
    return Path(__file__).with_name('adapter_program.py').read_bytes()


def prepare(inputs):
    if not isinstance(inputs, dict) or set(inputs) != {'dataset', 'capability_artifact_sha256', 'task_plan_ref', 'task_plan'}:
        raise ValueError('Aggregation requires its dataset, accepted capability artifact and frozen task plan reference')
    source = source_bytes()
    if inputs['capability_artifact_sha256'] != digest(source):
        raise ValueError('Aggregation artifact differs from the accepted capability')
    if not isinstance(inputs['task_plan_ref'], str) or len(inputs['task_plan_ref']) != 64:
        raise ValueError('Aggregation requires the exact immutable task plan identity')
    plan=validate_task_plan(inputs['task_plan'])
    if (digest(plan)!=inputs['task_plan_ref'] or plan['capability_versions'].get(ADAPTER_ID)!=VERSION
            or not any(row['input_id']=='dataset' and row['sha256']==digest(inputs['dataset']) for row in plan['inputs'])):
        raise ValueError('Aggregation input or capability differs from its frozen TaskPlan')
    if plan['protocol'] != {'version':PROTOCOL_VERSION,'parameters':PROTOCOL_PARAMETERS}:
        raise ValueError('This adapter does not support the requested task protocol semantics')
    return {'schema_version': 'tabular_aggregation_packet_v1', 'input': inputs['dataset'],
            'task_plan_ref': inputs['task_plan_ref'], 'artifact_sha256': digest(source),
            'adapter_source': source.decode(), 'limits': LIMITS}


def reservations(packet):
    return {'model_calls': 0, 'tokens': 0, 'tool_calls': 1,
            'cpu_seconds': packet['limits']['cpu_seconds'], 'wall_seconds': packet['limits']['wall_seconds']}


def _limit():
    resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMITS['output_bytes'], LIMITS['output_bytes']))


def execute(effect, packet_bytes):
    if effect['status'] != 'dispatching' or digest(packet_bytes) != effect['packet_ref']:
        raise ValueError('Tabular adapter requires its persisted, exact dispatching input')
    packet = json.loads(packet_bytes)
    if (packet.get('schema_version') != 'tabular_aggregation_packet_v1'
            or packet.get('artifact_sha256') != digest(source_bytes())
            or packet.get('adapter_source', '').encode() != source_bytes() or packet.get('limits') != LIMITS):
        raise ValueError('Tabular packet differs from the registered reviewed adapter')
    if effect.get('metadata',{}).get('task_plan_sha256')!=packet['task_plan_ref']:
        raise ValueError('Execution does not bind the actual registered task plan')
    started, before = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)
    envelope = {'schema_version': 'tabular_aggregate_input_v1', 'input': packet['input']}
    timed_out = False
    with tempfile.TemporaryDirectory(prefix='rsi-tabular-') as directory:
        root = Path(directory)
        program = root / 'adapter.py'; program.write_bytes(packet['adapter_source'].encode())
        with (root/'stdout.json').open('w+b') as stdout, (root/'stderr.txt').open('w+b') as stderr:
            process = subprocess.Popen([sys.executable, '-I', str(program)], cwd=root,
                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, start_new_session=True,
                env={'PATH':os.path.dirname(sys.executable),'PYTHONIOENCODING':'utf-8','LC_ALL':'C','TMPDIR':str(root)},
                preexec_fn=_limit)
            try:
                process.communicate(encode(envelope), timeout=LIMITS['wall_seconds'])
            except subprocess.TimeoutExpired:
                timed_out=True
                os.killpg(process.pid,signal.SIGKILL); process.communicate()
            finally:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            stdout.seek(0); stderr.seek(0)
            raw, errors = stdout.read(LIMITS['output_bytes']+1), stderr.read(LIMITS['output_bytes']+1)
        source_unchanged = program.is_file() and not program.is_symlink() and digest(program.read_bytes()) == packet['artifact_sha256']
        workspace = root.name
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    costs = {'model_calls':0,'tokens':0,'tool_calls':1,'wall_seconds':time.monotonic()-started,
        'cpu_seconds':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime}
    parsed, error = None, None
    try:
        parsed = json.loads(raw)
        if (timed_out or process.returncode != 0 or not source_unchanged or len(raw)>LIMITS['output_bytes']
                or set(parsed) != {'schema_version','ok','output','identity','source_receipts','error'}
                or parsed['schema_version'] != 'capability_response_v2'
                or parsed['source_receipts'] != [{'kind':'input','sha256':digest(packet['input'])}]
                or parsed['ok'] is not True or parsed['identity'] != packet['input']['dataset_id']):
            raise ValueError('Aggregation failed its executable/input identity contract')
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        error = str(exc)
    return {'status':'succeeded' if error is None else 'failed_definite','actual_costs':costs,
            'result':{'aggregation':parsed['output'] if error is None else None,
                      'input_sha256':digest(packet['input']),'task_plan_ref':packet['task_plan_ref']},
            'execution':{'workspace_id':workspace,'workspace_removed':True,'stdout_hex':raw.hex(),
                         'stderr_hex':errors.hex(),'returncode':process.returncode,'timed_out':timed_out,
                         'source_unchanged':source_unchanged,'error':error,
                         'isolation':'Fresh local process and temporary workspace; not an OS/network security sandbox'}}


def publication(effect, receipt):
    result = receipt['result']
    fact_id = 'tabular_result_' + digest([effect['id'], result['input_sha256']])[:24]
    return {'facts':[{'id':fact_id,'kind':'raw_fact','content':result,
                     'provenance':{'effect_id':effect['id'],'adapter_id':ADAPTER_ID,'adapter_version':VERSION,
                                   'artifact_sha256':digest(source_bytes()),'execution_ref':effect['result_ref']}}],
            'conclusions':[], 'data':result}


def make_adapter():
    return Adapter(adapter_id=ADAPTER_ID,version=VERSION,effect_kind='tabular_aggregation',
        manifest={'domain':'tabular_energy_measurements','artifact_sha256':digest(source_bytes()),
            'scope':'At most 1000 nonnegative scalar energy records with exact identities and explicit Wh/kWh metadata',
            'input_contract':{'dataset_id':'string','csv_text':'record_id,group,energy CSV','units':'exact record-to-Wh/kWh mapping'},
            'output_contract':{'groups':'energy_kwh totals and row counts','record_ids':'all unique input identities'},
            'limitations':['No inferred units','No duplicate identities','Serial child CPU accounting','No OS/network security sandbox']},
        prepare=prepare,reservations=reservations,execute=execute,publication=publication,
        source_paths=(str(Path(__file__).with_name('adapter_program.py')),),requires_acceptance=True)
