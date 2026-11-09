"""Read-only docking diagnostics behind the generic capability boundary."""
import json
from pathlib import Path
import time

from rsi_lab.adapters import Adapter
from rsi_lab.controller import identity
from rsi_lab.store import digest
from . import domain

ADAPTER_ID = 'docking.prerequisites'
VERSION = '2.0'
EFFECT_KIND = 'docking_prerequisite_inspection'


def prepare(inputs):
    if not isinstance(inputs,dict) or set(inputs) != {'data_dir','protocol_path','sources','scope'}:
        raise ValueError('Docking inspection requires explicit data, protocol, sources and scope')
    return {'data_dir':str(Path(inputs['data_dir']).resolve()),
        'protocol_path':str(Path(inputs['protocol_path']).resolve()),
        'sources':{k:str(Path(v).resolve()) for k,v in inputs['sources'].items()},
        'scope':inputs['scope'], 'adapter_sha256':digest(Path(domain.__file__).read_bytes())}


def reservations(packet):
    return {'model_calls':0,'tool_calls':1,'tokens':0,'cpu_seconds':60,'wall_seconds':60}


def execute(effect, raw):
    packet = json.loads(raw)
    if effect['status'] != 'dispatching' or digest(raw) != effect['packet_ref']:
        raise ValueError('Inspection requires its persisted exact dispatching packet')
    if digest(Path(domain.__file__).read_bytes()) != packet['adapter_sha256']:
        return {'status':'failed_definite','actual_costs':dict.fromkeys(effect['reservations'],0),
                'error':'Pinned docking auditor changed before execution'}
    start,cpu = time.monotonic(),time.process_time()
    report = domain.inspect_preconditions(packet['data_dir'],packet['protocol_path'],
                                          sources=packet['sources'],scope=packet['scope'])
    return {'status':'succeeded','result':report,'actual_costs':{'model_calls':0,'tool_calls':1,'tokens':0,
        'cpu_seconds':time.process_time()-cpu,'wall_seconds':time.monotonic()-start}}


def publication(effect, receipt):
    report = receipt['result']
    return {'facts':report['raw_facts']+report['source_statements'],
        'conclusions':[{'id':identity('domain_conclusion',effect['id']),'kind':'validation','content':report['checks'],
            'provenance':{'effect_id':effect['id'],'scope':'Limited built-in prerequisite inspection'}}],
        'data':report}


def make_adapter():
    return Adapter(adapter_id=ADAPTER_ID,version=VERSION,effect_kind=EFFECT_KIND,
        manifest={'scope':'Read-only source-bound docking prerequisite inspection; not molecular preparation or scientific admission',
            'input_contract':['data_dir','protocol_path','explicit supplemental sources','scope'],
            'output_contract':['raw_facts','source_statements','scoped checks','unresolved gaps'],
            'limitations':['Does not infer unobserved assay conditions','Does not execute docking',
                           'A checklist is supplementary and never defines the full scope of Meta discovery']},
        prepare=prepare,reservations=reservations,execute=execute,publication=publication,
        source_paths=(domain.__file__,),legacy_effect_kinds=(EFFECT_KIND,))
