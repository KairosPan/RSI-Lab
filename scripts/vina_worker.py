#!/usr/bin/env python3
"""Execute one frozen CDK2 AutoDock Vina CPU job and preserve native pose evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import resource
import subprocess
import time
import traceback
from datetime import datetime, timezone

if __package__:
    from . import vina_contract as contract
    from .vina_contract import ENGINE, ENGINE_VERSION, SEEDS, parse_vina_poses
else:
    import vina_contract as contract
    from vina_contract import ENGINE, ENGINE_VERSION, SEEDS, parse_vina_poses


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def dump(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    temp.replace(path)


def engine_receipt(vina):
    vina = Path(vina).resolve()
    result = subprocess.run([str(vina), '--version'], text=True, capture_output=True, timeout=15)
    output = result.stdout.strip()
    if result.returncode != 0 or output != f'AutoDock Vina v{ENGINE_VERSION}':
        raise ValueError(f'Expected AutoDock Vina v{ENGINE_VERSION}; got {output!r}')
    return {'engine': ENGINE, 'vina_version': {'returncode': result.returncode, 'stdout': output},
            'vina_sha256': sha(vina)}


def prepare_ligand(smiles, job):
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from meeko import MoleculePreparation, PDBQTWriterLegacy
    mol = Chem.MolFromSmiles(smiles)
    if mol is None or len(Chem.GetMolFrags(mol)) != 1:
        raise ValueError('A single valid molecular structure is required')
    canonical = Chem.MolToSmiles(mol, isomericSmiles=True)
    mol = Chem.AddHs(mol)
    settings = AllChem.ETKDGv3()
    settings.randomSeed, settings.numThreads = 20261019, 1
    if AllChem.EmbedMolecule(mol, settings) != 0 or not AllChem.MMFFHasAllMoleculeParams(mol):
        raise ValueError('ETKDGv3 embedding or MMFF94s parameterization failed')
    if AllChem.MMFFOptimizeMolecule(mol, mmffVariant='MMFF94s', maxIters=500) != 0:
        raise ValueError('MMFF94s did not converge within 500 iterations')
    mol.SetProp('canonical_isomeric_smiles', canonical)
    with Chem.SDWriter(str(job / 'ligand.sdf')) as writer:
        writer.write(mol)
    setups = MoleculePreparation().prepare(mol)
    if len(setups) != 1:
        raise ValueError('Expected one Meeko ligand setup')
    text, success, error = PDBQTWriterLegacy.write_string(setups[0], add_index_map=True)
    if not success:
        raise ValueError(f'Meeko ligand preparation failed: {error}')
    (job / 'ligand.pdbqt').write_text(text)
    return {'canonical_smiles': canonical, 'input_smiles': smiles, 'embed_seed': 20261019}


def vina_argv(vina, protocol, data, job, seed):
    if type(seed) is not int or seed not in SEEDS:
        raise ValueError('Unregistered Vina seed')
    command = [str(vina), '--scoring', 'vina', '--receptor', str(data / 'receptor/receptor.pdbqt'),
               '--ligand', str(job / 'ligand.pdbqt'), '--out', str(job / 'poses.pdbqt'),
               '--cpu', str(protocol['cpu']), '--exhaustiveness', str(protocol['exhaustiveness']),
               '--num_modes', str(protocol['num_modes']), '--seed', str(seed)]
    for field in ('center', 'size'):
        for axis, value in zip('xyz', protocol[field], strict=True):
            command.extend([f'--{field}_{axis}', str(value)])
    return command


def export_first_pose(job, affinities):
    from meeko import PDBQTMolecule, RDKitMolCreate
    from rdkit import Chem
    docked = PDBQTMolecule.from_file(str(job / 'poses.pdbqt'), skip_typing=True)
    molecules = RDKitMolCreate.from_pdbqt_mol(docked)
    if len(molecules) != 1 or molecules[0] is None or molecules[0].GetNumConformers() != len(affinities):
        raise ValueError('Native pose count or ligand identity mapping could not be reconstructed')
    mol = molecules[0]
    canonical = Chem.MolToSmiles(Chem.RemoveHs(mol), isomericSmiles=True)
    mol.SetProp('vina_affinity', str(affinities[0]))
    mol.SetProp('vina_model', '1')
    mol.SetProp('canonical_isomeric_smiles', canonical)
    with Chem.SDWriter(str(job / 'top.sdf')) as writer:
        writer.write(mol, confId=mol.GetConformers()[0].GetId())
    exported = list(Chem.SDMolSupplier(str(job / 'top.sdf'), removeHs=False))
    if len(exported) != 1 or exported[0] is None:
        raise ValueError('Selected pose SDF is unreadable')
    return Chem.MolToSmiles(Chem.RemoveHs(exported[0]), isomericSmiles=True)


def pose_checks(job, data, config):
    from posebusters import PoseBusters
    import pandas as pd
    table = PoseBusters(config=config, max_workers=0).bust(job / 'top.sdf',
                mol_cond=data / 'receptor/receptor_effective.pdb', full_report=True)
    table.to_csv(job / 'posebusters.csv')
    if len(table) != 1:
        raise ValueError('Expected one selected-pose PoseBusters row')
    names = [module.get('rename_outputs', {}).get(key, key + module.get('rename_suffix', '')).lower().replace(' ', '_')
             for module in config['modules'] for key in module.get('chosen_binary_test_output', [])]
    if not names:
        raise ValueError('Frozen PoseBusters configuration has no selected tests')
    return {key: None if pd.isna(table.iloc[0][key]) else bool(table.iloc[0][key]) for key in names}


def cpu_seconds():
    return sum(x.ru_utime + x.ru_stime for x in (resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)))


def work(spec_path):
    spec_path = Path(spec_path).resolve()
    spec, job = read(spec_path), spec_path.parent
    protocol, data, vina = spec['protocol'], Path(spec['data']).resolve(), Path(spec['vina']).resolve()
    started, cpu_start = time.monotonic(), cpu_seconds()
    result = {'candidate_id': spec['candidate']['candidate_id'], 'kind': 'candidate', 'seed': spec['seed'],
              'started_at': now(), 'status': 'worker_error', 'scores': None, 'pose_valid': None, 'identity_preserved': None}
    try:
        contract.validate_vina_protocol(protocol, live=True)
        if sha(__file__) != protocol['runner_sha256'] or sha(contract.__file__) != protocol['contract_sha256']:
            raise ValueError('Worker sources differ from frozen protocol')
        engine = engine_receipt(vina)
        if any(protocol.get(key) != value for key, value in engine.items()):
            raise ValueError('Executable identity differs from frozen Vina protocol')
        result.update(engine)
        result['status'] = 'preparation_failed'
        result['preparation'] = prepare_ligand(spec['candidate']['smiles'], job)
        command = vina_argv(vina, protocol, data, job, spec['seed'])
        result.update(status='docking_failed', command=command)
        dock_start = time.monotonic()
        with (job / 'vina.stdout.txt').open('w') as out, (job / 'vina.stderr.txt').open('w') as err:
            try:
                proc = subprocess.run(command, stdout=out, stderr=err, timeout=protocol['dock_timeout_seconds'])
            except subprocess.TimeoutExpired:
                result['status'] = 'docking_timeout'
                raise
            finally:
                result['docking_wall_seconds'] = time.monotonic() - dock_start
        result['returncode'] = proc.returncode
        if proc.returncode:
            raise ValueError(f'Vina exited {proc.returncode}')
        result['status'] = 'pose_parse_failed'
        energies = parse_vina_poses((job / 'poses.pdbqt').read_text())
        if len(energies) > protocol['num_modes']:
            raise ValueError('Vina output exceeds the requested number of modes')
        selected = export_first_pose(job, energies)
        result.update(selected_model=1, pose_count=len(energies), scores={'vina_affinity': energies[0]},
                      selected_canonical_smiles=selected, identity_preserved=selected == result['preparation']['canonical_smiles'])
        result['status'] = 'pose_check_failed'
        checks = pose_checks(job, data, protocol['posebusters_configs']['dock'])
        flags = [*checks.values(), result['identity_preserved']]
        result.update(pose_checks=checks, pose_valid=False if False in flags else None if None in flags else True, status='completed')
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        (job / 'exception.txt').write_text(traceback.format_exc())
    result.update(finished_at=now(), cpu_seconds=cpu_seconds() - cpu_start, wall_seconds=time.monotonic() - started)
    dump(job / 'result.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', required=True, metavar='SPEC_JSON')
    work(parser.parse_args().worker)
