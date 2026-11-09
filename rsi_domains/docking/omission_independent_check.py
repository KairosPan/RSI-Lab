"""Independent RDKit/NumPy check of generated receptor-omission measurements.

Never imports or executes the generated program. This verifies the complete
reported measurement against original bytes; it does not establish that a
receptor is scientifically suitable or that missing coordinates are harmless.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time

from rsi_lab.controller import identity
from rsi_lab.store import digest, encode

SOURCES = ('original_pdb', 'missing_records', 'prepared_pdb', 'prepared_pdbqt', 'reference_sdf', 'protocol')
CAPABILITY = 'docking.receptor_omission_geometry'
TOLERANCE = 1e-6
LIMITATIONS = [
    'Observed heavy-atom geometry only; absent coordinates remain unknown.',
    'Distances to a closed box do not establish candidate accessibility, pocket clearance or absence of artifacts.',
    'No reconstruction, docking, protocol changes, score changes or scientific admission.',
    'Reference ligand coordinates are used as supplied; this diagnostic does not verify pose recovery.',
]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON identity: ' + key)
        result[key] = value
    return result


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON: ' + value)))


def _atoms(text, *, pdbqt=False):
    """Independent fixed-column ATOM reader; preserve chain and insertion IDs."""
    import numpy as np
    atoms, residues = {}, {}
    if sum(line.startswith('MODEL ') for line in text.splitlines()) > 1:
        raise ValueError('Multiple receptor models are outside the checked scope')
    for line in text.splitlines():
        if line[:6].strip() != 'ATOM':
            continue
        name, resname = line[12:16].strip(), line[17:20].strip()
        chain, insertion = line[21:22].strip(), line[26:27].strip()
        seq = int(line[22:26])
        xyz = np.array([float(line[30:38]), float(line[38:46]), float(line[46:54])], dtype=float)
        if not name or not resname or not np.isfinite(xyz).all():
            raise ValueError('Malformed original atom identity or coordinate')
        element = line.split()[-1] if pdbqt else line[76:78].strip()
        if not element:
            element = next((char for char in name if char.isalpha()), '')
        if element.upper() in ({'H', 'HD', 'HS', 'D', 'T'} if pdbqt else {'H', 'D', 'T'}):
            continue
        if line[16:17].strip():
            raise ValueError('Unresolved alternate receptor coordinates')
        rid = f'{chain}:{seq}:{insertion}'
        atom_id = f'{rid}:{resname}:{name}'
        if atom_id in atoms or (rid in residues and residues[rid]['resname'] != resname):
            raise ValueError('Duplicate or conflicting receptor atom identity')
        atoms[atom_id] = xyz
        residues.setdefault(rid, {'chain': chain, 'resseq': seq, 'icode': insertion,
                                  'resname': resname, 'atoms': []})['atoms'].append(atom_id)
    if len(atoms) > 10000:
        raise ValueError('Receptor exceeds independent checker capacity')
    return atoms, residues


def _reference(text):
    """RDKit interprets the full native SDF; raw counts/elements/coordinates agree."""
    import numpy as np
    from rdkit import Chem
    records = [row for row in text.split('$$$$') if row.strip()]
    if len(records) != 1:
        raise ValueError('Exactly one original reference SDF is required')
    block = records[0]
    lines = block.splitlines()
    if len(lines) < 4 or 'V3000' in lines[3]:
        raise ValueError('Unsupported or truncated reference SDF')
    count, bonds = int(lines[3][:3]), int(lines[3][3:6])
    if not 0 < count <= 1000 or bonds < 0 or len(lines) < 4 + count + bonds:
        raise ValueError('Invalid raw reference counts')
    mol = Chem.MolFromMolBlock(block, sanitize=False, removeHs=False, strictParsing=True)
    if mol is None or mol.GetNumAtoms() != count or mol.GetNumBonds() != bonds or mol.GetNumConformers() != 1:
        raise ValueError('RDKit reference atom/bond counts differ from native counts')
    points = np.asarray(mol.GetConformer().GetPositions(), dtype=float)
    raw_points = np.array([[float(line[a:a + 10]) for a in (0, 10, 20)] for line in lines[4:4 + count]], dtype=float)
    elements = [line[31:34].strip() for line in lines[4:4 + count]]
    symbols = [atom.GetSymbol() for atom in mol.GetAtoms()]
    normalized = ['H' if element.upper() in {'D', 'T'} else element for element in elements]
    if normalized != symbols or not np.isfinite(points).all() or not np.isfinite(raw_points).all():
        raise ValueError('RDKit reference elements or coordinates differ from raw atom block')
    if not np.allclose(points, raw_points, atol=TOLERANCE, rtol=0):
        raise ValueError('RDKit reference coordinates differ from raw atom block')
    heavy = np.array([atom.GetAtomicNum() > 1 for atom in mol.GetAtoms()], dtype=bool)
    if not heavy.any():
        raise ValueError('Reference contains no observed heavy atoms')
    return points[heavy], {'raw_atom_count': count, 'raw_bond_count': bonds,
        'rdkit_atom_count': mol.GetNumAtoms(), 'rdkit_bond_count': mol.GetNumBonds(),
        'heavy_atom_count': int(heavy.sum()), 'elements': elements, 'coordinates': points.tolist(),
        'format': 'V2000' if 'V2000' in lines[3] else 'legacy_fixed_counts_accepted_by_RDKit'}


def _missing(text, original, atoms, residues):
    original_lines = {line.rstrip() for line in original.splitlines()}
    unknown = set()
    pattern = re.compile(r'^REMARK\s+(465|470)\s+(?:[0-9]+\s+)?([A-Z0-9]{3})\s+(\S)\s*(-?[0-9]+)([A-Za-z]?)\s*(.*)$')
    for line in text.splitlines():
        match = pattern.fullmatch(line.rstrip())
        if match is None:
            continue
        record, resname, chain, seq, insertion, tail = match.groups()
        if line.rstrip() not in original_lines:
            raise ValueError('Missing-coordinate record is not an original PDB data line')
        rid = f'{chain}:{int(seq)}:{insertion}'
        if record == '465':
            if tail.strip() or rid in residues:
                raise ValueError('Contradictory or unsupported missing residue record')
            unknown.add(('residue', rid))
        else:
            names = tail.split()
            if not names:
                raise ValueError('Missing-atom record contains no atom identity')
            for name in names:
                atom_id = f'{rid}:{resname}:{name}'
                if atom_id in atoms:
                    raise ValueError('Documented missing atom has original coordinates')
                unknown.add(('atom', atom_id))
    return unknown


def _expected(value, adapter_version):
    import numpy as np
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'identity', 'sources', 'residues'}
            or value['schema_version'] != 'receptor_omission_input_v1'
            or not isinstance(value['identity'], str) or not value['identity'].strip()
            or set(value['sources']) != set(SOURCES)):
        raise ValueError('Unsupported original input bundle')
    sources = value['sources']
    if sum(len(row['text'].encode()) for row in sources.values()) > 2 * 1024 * 1024:
        raise ValueError('Original source bundle exceeds independent checker capacity')
    hashes = {}
    for name in SOURCES:
        row = sources[name]
        if set(row) != {'text', 'sha256'} or not isinstance(row['text'], str) or digest(row['text'].encode()) != row['sha256']:
            raise ValueError('Original source hash mismatch: ' + name)
        hashes[name] = row['sha256']
    original, residues = _atoms(sources['original_pdb']['text'])
    prepared, _ = _atoms(sources['prepared_pdb']['text'])
    pdbqt, _ = _atoms(sources['prepared_pdbqt']['text'], pdbqt=True)
    reference, ref_receipt = _reference(sources['reference_sdf']['text'])
    protocol = _decode(sources['protocol']['text'])
    center, size = np.asarray(protocol['center'], dtype=float), np.asarray(protocol['size'], dtype=float)
    if center.shape != (3,) or size.shape != (3,) or not np.isfinite(center).all() or not np.isfinite(size).all() or not (size > 0).all():
        raise ValueError('Invalid original closed-box bounds')
    low, high = center - size / 2, center + size / 2
    unknown = _missing(sources['missing_records']['text'], sources['original_pdb']['text'], original, residues)
    selected, seen = [], set()
    if not isinstance(value['residues'], list) or not 0 < len(value['residues']) <= 32:
        raise ValueError('Invalid independent residue selection capacity')
    for selector in value['residues']:
        if set(selector) != {'chain', 'resseq', 'icode'} or type(selector['resseq']) is not int:
            raise ValueError('Malformed requested residue identity')
        matches = [rid for rid, row in residues.items() if row['resseq'] == selector['resseq']
                   and all(selector[key] is None or selector[key] == row[key] for key in ('chain', 'icode'))]
        if len(matches) > 1 or (not matches and (selector['chain'] is None or selector['icode'] is None)):
            raise ValueError('Ambiguous original residue selection')
        rid = matches[0] if matches else f"{selector['chain']}:{selector['resseq']}:{selector['icode']}"
        if rid in seen:
            raise ValueError('Duplicate requested residue identity')
        seen.add(rid)
        atom_ids = sorted(residues[rid]['atoms']) if matches else []
        if not atom_ids:
            unknown.add(('residue', rid))
            selected.append({'residue_id': rid, 'observed_atom_count': 0,
                             'min_distance_to_box': None, 'min_distance_to_reference': None, 'atoms': []})
            continue
        points = np.stack([original[aid] for aid in atom_ids])
        # Independently vectorized all-atom distances; no adapter helper is used.
        box_distances = np.linalg.norm(np.maximum(np.maximum(low - points, points - high), 0), axis=1)
        ref_distances = np.linalg.norm(points[:, None, :] - reference[None, :, :], axis=2).min(axis=1)
        rows = []
        for index, aid in enumerate(atom_ids):
            xyz = points[index]
            classification = ('outside' if np.any((xyz < low) | (xyz > high))
                              else 'boundary' if np.any((xyz == low) | (xyz == high)) else 'inside')
            rows.append({'atom_id': aid, 'xyz': xyz.tolist(), 'classification': classification,
                'distance_to_box': float(box_distances[index]), 'distance_to_reference': float(ref_distances[index]),
                'in_prepared': aid in prepared, 'in_pdbqt': aid in pdbqt})
        selected.append({'residue_id': rid, 'observed_atom_count': len(atom_ids),
            'min_distance_to_box': float(box_distances.min()), 'min_distance_to_reference': float(ref_distances.min()), 'atoms': rows})
    expected = {'schema_version': 'receptor_omission_report_v1', 'identity': value['identity'],
        'adapter': {'capability_id': CAPABILITY, 'version': adapter_version}, 'input_hashes': hashes,
        'box': {'center': center.tolist(), 'size': size.tolist()},
        'inventory': {'original_heavy_atoms': len(original), 'prepared_heavy_atoms': len(prepared),
            'pdbqt_heavy_atoms': len(pdbqt), 'removed_from_prepared': sorted(set(original) - set(prepared)),
            'added_in_prepared': sorted(set(prepared) - set(original)),
            'missing_from_pdbqt': sorted(set(prepared) - set(pdbqt)), 'added_in_pdbqt': sorted(set(pdbqt) - set(prepared))},
        'residues': selected, 'unknown_positions': [{'kind': kind, 'identity': aid, 'position': None} for kind, aid in sorted(unknown)],
        'limitations': LIMITATIONS}
    return expected, ref_receipt


def _compare(expected, observed, path, differences):
    if isinstance(expected, dict):
        if not isinstance(observed, dict) or set(observed) != set(expected):
            differences.append({'path': path, 'reason': 'exact_object_fields_differ'})
            return
        for key, value in expected.items():
            _compare(value, observed[key], path + '.' + key, differences)
    elif isinstance(expected, list):
        if not isinstance(observed, list) or len(observed) != len(expected):
            differences.append({'path': path, 'reason': 'exact_list_coverage_or_order_differs', 'expected_count': len(expected),
                                'observed_count': len(observed) if isinstance(observed, list) else None})
            return
        for index, value in enumerate(expected):
            _compare(value, observed[index], f'{path}[{index}]', differences)
    elif type(expected) is int:
        if type(observed) is not int or observed != expected:
            differences.append({'path': path, 'reason': 'exact_integer_mismatch', 'expected': expected, 'observed': repr(observed)})
    elif type(expected) is float:
        if (type(observed) not in (float, int) or not math.isfinite(observed)
                or abs(float(expected) - float(observed)) > TOLERANCE):
            differences.append({'path': path, 'reason': 'numeric_mismatch', 'expected': expected, 'observed': repr(observed)})
    elif type(expected) != type(observed) or observed != expected:
        differences.append({'path': path, 'reason': 'exact_identity_or_value_mismatch', 'expected': expected, 'observed': repr(observed)})


def verify(input_bundle, response):
    """Return an independent validation receipt, including failures unchanged."""
    import numpy as np
    from rdkit import rdBase
    base = {'schema_version': 'omission_independent_validation_v1', 'passed': False,
        'status': 'unverified', 'differences': [], 'numeric_absolute_tolerance': TOLERANCE,
        'versions': {'rdkit': rdBase.rdkitVersion, 'numpy': np.__version__},
        'scope': 'Full requested observed-atom identity coverage, original-source hashes, inventories, unknown positions and closed-box/reference geometry only; no scientific admission.'}
    try:
        encode(input_bundle); encode(response)
        if (not isinstance(response, dict) or set(response) != {'schema_version', 'ok', 'output', 'identity', 'source_receipts', 'error'}
                or response['schema_version'] != 'capability_response_v2'):
            raise ValueError('Unsupported producer response shape')
        if response['ok'] is not True:
            return {**base, 'status': 'producer_failed', 'producer_error': response.get('error'),
                    'differences': [{'path': 'response.ok', 'reason': 'No successful generated geometry exists to validate'}]}
        if (response['error'] is not None or response['identity'] != input_bundle['identity']
                or response['source_receipts'] != [{'kind': 'input', 'sha256': digest(input_bundle)}]):
            raise ValueError('Producer task identity or original input receipt mismatch')
        report = response['output']
        version = report['adapter']['version']
        if not isinstance(version, str) or not version:
            raise ValueError('Missing measured adapter version')
        expected, reference = _expected(input_bundle, version)
        differences = []
        _compare(expected, report, 'report', differences)
        return {**base, 'passed': not differences, 'status': 'verified' if not differences else 'mismatch',
            'differences': differences, 'input_sha256': digest(input_bundle), 'response_sha256': digest(response),
            'source_hashes': expected['input_hashes'], 'reference_check': reference,
            'requested_residues': len(expected['residues']),
            'observed_selected_atoms': sum(row['observed_atom_count'] for row in expected['residues']),
            'inventory': expected['inventory'], 'unknown_position_count': len(expected['unknown_positions'])}
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
        return {**base, 'status': 'invalid_input_or_response', 'differences': [{'path': 'input_or_response', 'reason': str(exc)}]}


def metered_execute(runtime, binding, output_path):
    """Validate an existing generated response file through one durable Effect.

    output_path is the existing diagnostic response, not an output to fabricate.
    A neighboring .independent_check.json contains the measured checker receipt.
    """
    import numpy as np
    from rdkit import rdBase
    source = Path(binding['input_path']).resolve(strict=True)
    response_path = Path(output_path).resolve(strict=True)
    if not response_path.is_relative_to(Path(binding['output_root']).resolve()):
        raise ValueError('Measured response lies outside the bound diagnostic output root')
    source_raw, response_raw = source.read_bytes(), response_path.read_bytes()
    if digest(source_raw) != binding['input_sha256']:
        raise ValueError('Actual input bundle differs from the registered capability binding')
    code = Path(__file__).read_bytes()
    packet = {'schema_version': 'omission_independent_check_packet_v1', 'input_ref': runtime.store.put_blob(source_raw),
        'response_ref': runtime.store.put_blob(response_raw), 'checker_ref': runtime.store.put_blob(code),
        'binding': binding, 'response_path': str(response_path),
        'versions': {'rdkit': rdBase.rdkitVersion, 'numpy': np.__version__}}
    token = identity('omission_independent', digest(packet))
    work = runtime.controller.enqueue(runtime.run_id, 'diagnostic', token, token + ':work',
                                      inputs={'binding': binding, 'response_path': str(response_path)})
    effect = runtime.controller.prepare_effect(runtime.run_id, work['id'], 'omission_independent_validation', packet,
        {'tool_calls': 1, 'cpu_seconds': 10, 'wall_seconds': 30}, token + ':prepare',
        metadata={'issue_id': binding.get('issue_id'), 'checker_sha256': digest(code), 'response_sha256': digest(response_raw)})

    def execute(admitted, raw):
        if admitted['status'] != 'dispatching' or digest(raw) != admitted['packet_ref']:
            raise ValueError('Independent check needs the exact committed execution packet')
        started, cpu = time.monotonic(), time.process_time()
        exact = _decode(raw)
        if (digest(Path(__file__).read_bytes()) != exact['checker_ref'] or
                exact['versions'] != {'rdkit': rdBase.rdkitVersion, 'numpy': np.__version__}):
            return {'status': 'failed_definite', 'actual_costs': {'tool_calls': 0, 'cpu_seconds': 0, 'wall_seconds': 0},
                    'error': 'Independent checker source or library versions changed'}
        measured = _decode(runtime.store.read_blob(exact['response_ref']))
        value = _decode(runtime.store.read_blob(exact['input_ref']))
        result = verify(value, measured)
        if measured.get('ok') is True and measured.get('output', {}).get('adapter') != {
                'capability_id': binding['capability_id'], 'version': binding['version']}:
            result.update(passed=False, status='mismatch')
            result['differences'].append({'path': 'report.adapter', 'reason': 'Measured adapter identity differs from accepted binding'})
        return {'status': 'succeeded', 'result': result, 'actual_costs': {'tool_calls': 1,
            'cpu_seconds': time.process_time() - cpu, 'wall_seconds': time.monotonic() - started}}

    current = runtime.store.get(runtime.run_id, 'effect', effect['id'])
    if current['status'] == 'prepared':
        current = runtime.controller.dispatch_effect(runtime.run_id, current['id'], execute)
    if current['status'] != 'succeeded' or current.get('accounting_unknown'):
        return {'effect': current, 'validation': None}
    receipt = _decode(runtime.store.read_blob(current['result_ref']))
    validation = receipt['result']
    fact_id = identity('independent_validation', current['id'])
    runtime.controller.add_fact(runtime.run_id, fact_id, 'validation', validation,
        {'effect_id': current['id'], 'source': 'Independent RDKit/NumPy original-input check',
         'checker_sha256': digest(code), 'response_sha256': digest(response_raw)}, 'publish:' + token)
    if binding.get('issue_id'):
        def attach(tx):
            issue = tx.get('issue', binding['issue_id'])
            if fact_id in issue['evidence_ids']:
                return {'attached': True, 'fact_id': fact_id}
            tx.update('issue', issue['id'], {'evidence_ids': issue['evidence_ids'] + [fact_id],
                                           'evidence_revision': issue['evidence_revision'] + 1})
            if issue['status'] == 'open':
                runtime.controller._work(tx, issue['id'], 'meta_disposition', 'validation:' + fact_id,
                                         inputs={'evidence_ids': issue['evidence_ids'] + [fact_id]})
            return {'attached': True, 'fact_id': fact_id}
        runtime.store.mutate(runtime.run_id, 'attach:' + token, 'independent_validation_attached', attach,
                             payload={'fact_id': fact_id, 'issue_id': binding['issue_id']})
    saved = response_path.with_name(response_path.stem + '.independent_check.json')
    if saved.exists():
        if saved.read_bytes() != encode(validation):
            raise ValueError('Existing independent receipt differs; never overwrite it')
    else:
        with saved.open('xb') as stream:
            stream.write(encode(validation))
    return {'effect': current, 'validation': validation, 'fact_id': fact_id, 'output_path': str(saved)}
