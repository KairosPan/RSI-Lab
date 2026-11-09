"""Read-only docking prerequisite inspection; never prepares or docks molecules.

Checks establish narrow, stated facts about actual local files. Source claims,
missing scientific validation and applicable verified facts are separate. No
archived executable, arbitrary archived absolute path, or model conclusion is
executed or treated as scientific admission.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

SCHEMA = 'docking_prerequisite_inspection_v2'
MAX_FILE_BYTES = 32 * 1024 * 1024
SOURCE_KEYS = {'receptor_preparation_dir', 'reference_control', 'reference_control_root',
               'chemical_source', 'molscrub_root'}


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _vector(value, positive=False):
    return isinstance(value, list) and len(value) == 3 and all(_finite(x) and (not positive or x > 0) for x in value)


def _pdb(text, pdbqt=False):
    atoms = []
    for line in text.splitlines():
        if line[:6].strip() not in {'ATOM', 'HETATM'}:
            continue
        point = [float(line[start:start+8]) for start in (30, 38, 46)]
        if not all(math.isfinite(x) for x in point):
            raise ValueError('Nonfinite receptor coordinate')
        atom_type = line.split()[-1] if pdbqt else line[76:78].strip()
        atoms.append({'record': line[:6].strip(), 'atom': line[12:16].strip(),
            'residue': line[17:20].strip(), 'chain': line[21:22], 'resid': int(line[22:26]),
            'altloc': line[16:17].strip(), 'xyz': point, 'element': atom_type,
            'charge': float(line.split()[-2]) if pdbqt else None})
    if not atoms:
        raise ValueError('No actual receptor atoms')
    return atoms


def _sdf(text):
    records = [r for r in text.split('$$$$') if r.strip()]
    if len(records) != 1:
        raise ValueError('Expected one reference SDF record')
    lines = records[0].splitlines()
    count = int(lines[3][:3])
    if not 0 < count < 10000 or len(lines) < 4 + count:
        raise ValueError('Invalid reference atom count')
    atoms = [{'element': line[31:34].strip(), 'xyz': [float(line[i:i+10]) for i in (0, 10, 20)]}
             for line in lines[4:4+count]]
    if not all(all(math.isfinite(x) for x in a['xyz']) for a in atoms):
        raise ValueError('Nonfinite reference coordinates')
    return [a for a in atoms if a['element'] not in {'H', 'D'}]


class _Inspection:
    def __init__(self):
        self.receipts, self.raw, self.statements, self.checks = {}, [], [], []

    def read(self, name, path, expected=None, as_json=False):
        path = Path(path)
        receipt = {'source_id': name, 'path': str(path.resolve()), 'sha256': None,
                   'expected_sha256': expected, 'status': 'unverified', 'bytes': None}
        self.receipts[name] = receipt
        try:
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError('Missing file or bounded reader capacity exceeded')
            raw = path.read_bytes()
            receipt.update(sha256=_hash(raw), bytes=len(raw))
            if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)
                                         or receipt['sha256'] != expected):
                raise ValueError('Actual file differs from its frozen source hash')
            value = json.loads(raw) if as_json else raw.decode('utf-8')
            receipt['status'] = 'hash_verified' if expected is not None else 'read_unanchored'
            return value
        except (OSError, ValueError, UnicodeError) as exc:
            receipt['error'] = f'{type(exc).__name__}: {exc}'
            return None

    def fact(self, name, content, sources, *, statement=False):
        provenance = {'adapter': SCHEMA, 'adapter_sha256': _hash(Path(__file__).read_bytes()),
                      'sources': [self.receipts[s] for s in sources if s in self.receipts]}
        row = {'id': 'domain_' + name + '_' + _hash(_json([content, provenance]))[:16],
               'kind': 'source_statement' if statement else 'raw_fact', 'content': content, 'provenance': provenance}
        (self.statements if statement else self.raw).append(row)
        return row['id']

    def check(self, name, status, facts, scope, criterion, reason):
        self.checks.append({'check_id': name, 'status': status, 'fact_ids': facts,
                            'applicability': scope, 'resolution_criterion': criterion, 'reason': reason})


def inspect_preconditions(data_dir, protocol_path, *, sources=None, scope='exploratory'):
    """Inspect explicit local inputs and optionally authorized supplemental roots.

    `sources` recognizes SOURCE_KEYS only. It does not follow paths recorded in
    old manifests. Return values can be persisted with Controller.add_fact and
    passed to Runtime.observe; this function itself has no writes or tool calls.
    """
    if scope not in {'exploratory', 'formal'} or not isinstance(sources or {}, dict) or set(sources or {}) - SOURCE_KEYS:
        raise ValueError('Unsupported inspection scope or supplementary source key')
    sources = {k: Path(v).resolve() for k, v in (sources or {}).items()}
    data = Path(data_dir).resolve()
    audit = _Inspection()
    protocol = audit.read('protocol', protocol_path, as_json=True)
    if not isinstance(protocol, dict):
        protocol = {}
    hashes = protocol.get('input_sha256', {})
    if not isinstance(hashes, dict):
        hashes = {}
    loaded = {}
    required = {'manifest.json', 'candidates.json', 'chemical_states.json', 'receptor/receptor_manifest.json',
                'receptor/receptor.pdbqt', 'receptor/receptor_effective.pdb', 'receptor/reference.sdf'}
    for name in sorted(set(hashes) | required):
        path = data / name
        if Path(name).is_absolute() or not path.resolve().is_relative_to(data):
            audit.receipts[name] = {'source_id': name, 'status': 'unverified', 'error': 'Input path escapes explicit data root'}
            continue
        loaded[name] = audit.read(name, path, hashes.get(name), as_json=name.endswith('.json'))
    bad = [name for name in required if name not in hashes or audit.receipts[name]['status'] != 'hash_verified']
    bad += [name for name in hashes if audit.receipts[name]['status'] != 'hash_verified']
    integrity = audit.fact('input_integrity', {'required_files': sorted(required), 'invalid_or_unbound': sorted(set(bad))},
                           ['protocol', *sorted(audit.receipts.keys() - {'protocol'})])
    audit.check('input_integrity', 'invalid' if bad else 'verified', [integrity], 'Exact supplied protocol/data bytes',
                'All required and declared inputs exist and match the protocol SHA256 map',
                'Hash verification authenticates bytes relative to the supplied protocol, not scientific correctness')
    def document(name):
        value = loaded.get(name)
        if not isinstance(value, dict):
            if name not in bad:
                bad.append(name)
            return {}
        return value
    manifest, receptor, chemical = (document(name) for name in
        ('manifest.json', 'receptor/receptor_manifest.json', 'chemical_states.json'))
    candidates = document('candidates.json').get('candidates', [])
    if not isinstance(candidates, list) or any(not isinstance(row, dict) for row in candidates):
        candidates = []
        bad.append('candidates.json')
    source_dir = sources.get('receptor_preparation_dir')
    artifacts = receptor.get('artifacts', {})
    if not isinstance(artifacts, dict):
        artifacts = {}

    def supplemental(name):
        if source_dir is None or not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            return None
        receipt = artifacts.get(name, {})
        expected = receipt.get('sha256') if isinstance(receipt, dict) else None
        if expected is None:
            return None
        return audit.read('receptor_source/' + name, source_dir / name, expected)

    pdb_id = receptor.get('pdb_id')
    original_text = supplemental(str(pdb_id) + '.pdb')
    original, effective, typed, reference = [], [], [], []
    parse_errors = {}
    for name, text, parser in [('original', original_text, _pdb),
            ('effective', loaded.get('receptor/receptor_effective.pdb'), _pdb),
            ('typed', loaded.get('receptor/receptor.pdbqt'), lambda x: _pdb(x, True)),
            ('reference', loaded.get('receptor/reference.sdf'), _sdf)]:
        try:
            value = parser(text) if text is not None else []
            if name == 'original': original = value
            elif name == 'effective': effective = value
            elif name == 'typed': typed = value
            else: reference = value
        except (ValueError, IndexError, TypeError) as exc:
            parse_errors[name] = str(exc)
    geometry = audit.fact('receptor_geometry', {'atom_counts': {'original': len(original), 'effective': len(effective),
        'pdbqt': len(typed), 'reference_heavy': len(reference)}, 'parse_errors': parse_errors,
        'pdbqt_polar_hydrogens': sum(a['element'] == 'HD' for a in typed),
        'effective_hydrogens': sum(a['element'] in {'H', 'D'} for a in effective)},
        ['receptor/receptor_effective.pdb', 'receptor/receptor.pdbqt', 'receptor/reference.sdf', 'receptor_source/' + str(pdb_id) + '.pdb'])

    chain = receptor.get('protein_auth_chain')
    dbrefs = [line.split() for line in (original_text or '').splitlines() if line.startswith('DBREF ')]
    identity_ok = (bool(original) and any(len(r) >= 8 and r[1] == pdb_id and r[2] == chain and r[5] == 'UNP'
                    and r[6] == receptor.get('uniprot') for r in dbrefs)
                   and manifest.get('target_chembl_id') == receptor.get('chembl_target_id')
                   and isinstance(manifest.get('target_chembl_id'), str)
                   and manifest.get('construct_id') == f'{pdb_id}_chain_{chain}'
                   and bool(effective) and {a['chain'] for a in effective} == {chain})
    fid = audit.fact('target_mapping', {'catalog_target': manifest.get('target_chembl_id'),
        'construct': manifest.get('construct_id'), 'prepared_target': receptor.get('chembl_target_id'),
        'pdb_id': pdb_id, 'chain': chain, 'uniprot': receptor.get('uniprot'), 'raw_DBREF_records': dbrefs,
        'effective_chains': sorted({a['chain'] for a in effective})},
        ['manifest.json', 'receptor/receptor_manifest.json', 'receptor_source/' + str(pdb_id) + '.pdb'])
    audit.check('target_construct', 'verified' if identity_ok else 'unverified', [fid, geometry],
        'Deposited PDB chain/UniProt and catalog-to-prepared-target mapping; not physiological construct suitability',
        'Hash-bound PDB DBREF, actual chain atoms and manifest construct must agree', 'Labels alone do not establish the chain mapping')

    absent = sorted({int(m[1]) for m in re.findall(r'^REMARK 465\s+[A-Z]{3}\s+(\S)\s+(-?\d+)\s*$', original_text or '', re.M) if m[0] == chain})
    missing_atoms = {f'{m[0]}:{int(m[1])}': m[2].split() for m in re.findall(
        r'^REMARK 470\s+[A-Z]{3}\s+(\S)\s*(-?\d+)\s+([A-Z0-9 ]+)$', original_text or '', re.M) if m[0] == chain}
    orig_res = {(a['chain'], a['resid']) for a in original if a['record'] == 'ATOM'}
    eff_res = {(a['chain'], a['resid']) for a in effective if a['record'] == 'ATOM'}
    deleted = sorted(f'{c}:{n}' for c, n in orig_res - eff_res)
    fid = audit.fact('completeness', {'PDB_missing_residues': absent, 'PDB_missing_atoms': missing_atoms,
        'observed_deleted_residues': deleted, 'manifest_missing_residues': receptor.get('missing_residues'),
        'manifest_incomplete_residues': receptor.get('incomplete_residues')},
        ['receptor_source/' + str(pdb_id) + '.pdb', 'receptor/receptor_effective.pdb', 'receptor/receptor_manifest.json'])
    declared_consistent = original and absent == receptor.get('missing_residues') and missing_atoms == receptor.get('incomplete_residues')
    audit.check('receptor_completeness', 'limited' if declared_consistent else 'unverified', [fid, geometry],
        'Missing residues/atoms and deletions in the supplied receptor',
        'Verify sequence/atom completeness, any repairs, and the effect of unresolved changes on the intended site',
        'Recorded deletions and unreconstructed regions require a scoped structural assessment; empty labels cannot establish completeness')

    prep_receipts = []
    for attempt in receptor.get('preparation_attempts', []) if isinstance(receptor.get('preparation_attempts', []), list) else []:
        if not isinstance(attempt, dict) or type(attempt.get('returncode')) is not int or attempt['returncode'] != 0:
            continue
        command = attempt.get('command', [])
        if not isinstance(command, list) or not command or not str(command[0]).endswith('mk_prepare_receptor.py'):
            continue
        out_name, err_name = Path(str(attempt.get('stdout', ''))).name, Path(str(attempt.get('stderr', ''))).name
        out, err = supplemental(out_name), supplemental(err_name)
        if out is not None and err is not None:
            prep_receipts.append({'command': command, 'returncode': 0, 'stdout_sha256': _hash(out.encode()),
                                  'stderr_sha256': _hash(err.encode()), 'omitted_residues': attempt.get('omitted_incomplete_residues')})
    fid = audit.fact('receptor_preparation_receipts', {'verified_log_receipts': prep_receipts,
        'has_finite_pdbqt_charges': bool(typed) and all(_finite(a['charge']) for a in typed)},
        ['receptor/receptor_manifest.json', 'receptor/receptor.pdbqt',
         *[name for name in audit.receipts if name.startswith('receptor_source/preparation_')]])
    limits = audit.fact('declared_receptor_limits', {'limits': receptor.get('limits'), 'filter_rules': receptor.get('filter_rules')},
                        ['receptor/receptor_manifest.json'], statement=True)
    audit.check('receptor_chemical_state', 'limited' if prep_receipts and typed else 'unverified', [fid, geometry, limits],
        'Observed prepared PDBQT atom types/charges and documented Meeko execution only',
        'Residue-level state/hydrogen treatment must be justified for the applicable environment; verify output topology and method receipts',
        'Hydrogens, Gasteiger charges, or a successful command do not establish receptor pH states or hydrogen-bond topology')

    original_hetero = Counter(a['residue'] for a in original if a['record'] == 'HETATM')
    effective_hetero = Counter(a['residue'] for a in effective if a['record'] == 'HETATM')
    fid = audit.fact('water_cofactor_inventory', {'source_hetero_atom_counts': dict(original_hetero),
        'effective_hetero_atom_counts': dict(effective_hetero)},
        ['receptor_source/' + str(pdb_id) + '.pdb', 'receptor/receptor_effective.pdb'])
    audit.check('water_cofactors', 'limited' if original and effective else 'unverified', [fid, limits],
        'Retained/deleted heteroatom inventory for this receptor',
        'Audit each relevant water, ion, cofactor and special coordination case; justify retention/removal for the task',
        'Zero retained waters/cofactors is an observed preparation choice, not proof that they are unnecessary')

    method_ok, method_error = False, None
    try:
        from scripts.run_vina_batch import load_chemical_states
        method, registry = load_chemical_states(data, candidates=candidates, manifest=manifest)
        method_ok = method is not None and protocol.get('chemical_preparation') == {'method': method}
        if not method_ok:
            raise ValueError('Missing or mismatched frozen chemical method')
    except (ValueError, KeyError, TypeError, OSError) as exc:
        method, registry, method_error = chemical.get('method'), {}, str(exc)
    source_checks = []
    if isinstance(method, dict):
        method_sources = method.get('source_hashes', {})
        for name, expected in (method_sources if isinstance(method_sources, dict) else {}).items():
            path = sources.get('chemical_source') if name == 'prepare_chemical_states.py' else None
            if name.startswith('molscrub/') and 'molscrub_root' in sources:
                relative = Path(name).relative_to('molscrub')
                candidate_path = sources['molscrub_root'] / relative
                if candidate_path.resolve().is_relative_to(sources['molscrub_root']):
                    path = candidate_path
            if path is not None:
                audit.read('chemical_source/' + name, path, expected)
                source_checks.append({'name': name, 'status': audit.receipts['chemical_source/' + name]['status']})
    fid = audit.fact('ligand_states', {'registry_check_passed': method_ok, 'error': method_error,
        'candidate_count': len(candidates), 'registry_count': len(registry),
        'changed_state_count': sum(r.get('source_smiles') != r.get('prepared_smiles') for r in registry.values()),
        'method': {k: method.get(k) for k in ('engine', 'version', 'pH', 'admission', 'population_estimates_available')}
                    if isinstance(method, dict) else None, 'source_checks': source_checks},
        ['candidates.json', 'manifest.json', 'chemical_states.json', 'protocol',
         *[name for name in audit.receipts if name.startswith('chemical_source/')]])
    audit.check('ligand_state', 'limited' if method_ok else 'unverified', [fid],
        'Exactly-one retained state under the registered MolScrub rule convention',
        'Current-object preparation records and identities must match; resolve applicable pH and state coverage independently',
        'A retained state is not proof of dominant microstate, exhaustive tautomers or shared experimental pH')

    stereo_errors, stereo_backend = [], None
    if method_ok:
        try:
            from rdkit import Chem, rdBase
            from scripts.prepare_chemical_states import canonical, state_issues
            stereo_backend = {'rdkit': rdBase.rdkitVersion, 'method': 'current-source canonical and state_issues'}
            for cid, row in registry.items():
                parent, state = Chem.MolFromSmiles(row['source_smiles']), Chem.MolFromSmiles(row['prepared_smiles'])
                if parent is None or state is None:
                    stereo_errors.append({'compound_id': cid, 'issues': ['invalid_molecular_graph']}); continue
                issues = state_issues(Chem.RemoveHs(parent), Chem.RemoveHs(state))
                if canonical(parent) != row['canonical_parent_identity'] or canonical(state) != row['prepared_smiles']:
                    issues.append('noncanonical_registered_identity')
                if Chem.GetFormalCharge(state) != row['states'][0]['formal_charge']:
                    issues.append('formal_charge_mismatch')
                if issues:
                    stereo_errors.append({'compound_id': cid, 'issues': issues})
        except ImportError:
            stereo_errors = [{'issues': ['local_rdkit_inspection_unavailable']}]
    fid = audit.fact('stereochemistry', {'backend': stereo_backend, 'inspected_count': len(registry) if stereo_backend else 0,
        'issues': stereo_errors}, ['candidates.json', 'chemical_states.json'])
    audit.check('stereochemistry', 'verified' if method_ok and stereo_backend and not stereo_errors else 'unverified', [fid],
        'Source-to-prepared connectivity, specified stereo, unspecified potential stereo and formal charge',
        'Recompute graph/stereo checks on actual source and prepared structures, not only stored passed fields',
        'This graph check does not determine which experimental stereoisomer should have been supplied')

    center, size = protocol.get('center'), protocol.get('size')
    centroid = [sum(a['xyz'][i] for a in reference) / len(reference) for i in range(3)] if reference else None
    binding = receptor.get('reference_ligand', {})
    deposited = [a for a in original if isinstance(binding, dict) and a['record'] == 'HETATM'
                 and a['residue'] == binding.get('ccd') and a['chain'] == binding.get('auth_chain')
                 and a['resid'] == binding.get('auth_seq_id') and a['element'] not in {'H', 'D'}]
    unmatched = list(deposited)
    for atom in reference:
        matches = [i for i, other in enumerate(unmatched) if atom['element'] == other['element']
                   and math.dist(atom['xyz'], other['xyz']) <= 0.01]
        if matches:
            unmatched.pop(matches[0])
    frame_matches = bool(reference) and len(reference) == len(deposited) and not unmatched
    site_ok = (bool(reference) and _vector(center) and _vector(size, True) and center == receptor.get('center')
               and size == receptor.get('size') and math.dist(center, centroid) < 0.001
               and all(abs(a['xyz'][i] - center[i]) <= size[i]/2 for a in reference for i in range(3)))
    fid = audit.fact('binding_site', {'center': center, 'size': size, 'reference_centroid': centroid,
        'reference_in_box_and_centered': site_ok, 'deposited_reference_frame_matches': frame_matches},
        ['protocol', 'receptor/receptor_manifest.json', 'receptor/reference.sdf', 'receptor_source/' + str(pdb_id) + '.pdb'])
    audit.check('binding_site', 'verified' if site_ok and frame_matches else 'unverified', [fid],
        'Declared deposited reference instance, unchanged coordinates and its registered box',
        'Recompute reference coordinates/centroid and check agreement with the actual receptor frame and frozen box',
        'A centered reference proves this box definition, not completeness of alternative binding sites')

    fid = audit.fact('search_parameters', {k: protocol.get(k) for k in ('engine', 'scoring', 'seeds', 'exhaustiveness',
        'num_modes', 'cpu', 'pose_selection', 'retries')}, ['protocol'])
    audit.check('search_sufficiency', 'unverified', [fid], 'The intended target/library and search protocol',
        'A prospectively specified search-sensitivity/convergence comparison with raw runs is required',
        'Fixed exhaustiveness and two seeds do not establish search sufficiency; no accepted comparison adapter is registered here')

    controls, control_errors = [], []
    if 'reference_control' in sources:
        receipt = audit.read('reference_control', sources['reference_control'], as_json=True)
        rows = receipt.get('results', []) if isinstance(receipt, dict) else []
        control_root = sources.get('reference_control_root')
        for row in rows if isinstance(rows, list) else []:
            try:
                if not isinstance(row, dict) or row.get('schema_version') != 'reference_native_frame_rmsd_v1' or control_root is None:
                    raise ValueError('Missing supported control receipt or explicit raw control root')
                seed = row['seed']; job_name = Path(row['probe_path']).parent.name
                if type(seed) is not int or seed not in (17, 43) or not re.fullmatch(r'[A-Za-z0-9_.-]+_s' + str(seed), job_name):
                    raise ValueError('Invalid reference job identity')
                probe = audit.read('control_probe_' + str(seed), control_root / 'jobs' / job_name / 'top.sdf', row['probe_sha256'])
                ref = audit.read('control_reference_' + str(seed), control_root / 'data/receptor/reference.sdf', row['reference_sha256'])
                if probe is None or ref is None or row['reference_sha256'] != hashes.get('receptor/reference.sdf'):
                    raise ValueError('Reference control is not bound to the supplied reference bytes')
                if (row.get('heavy_identity_matches') is not True or row.get('alignment_applied') is not False
                        or not _finite(row.get('rmsd_angstrom')) or row['rmsd_angstrom'] < 0
                        or row.get('formal_qualification') is not False):
                    raise ValueError('Unsupported RMSD control semantics')
                controls.append({'seed': seed, 'reported_native_frame_rmsd_angstrom': row['rmsd_angstrom'],
                    'probe_sha256': row['probe_sha256'], 'reference_sha256': row['reference_sha256'],
                    'scope': 'Hash-bound historical pose-control receipt; RMSD not recomputed by this adapter'})
            except (ValueError, KeyError, TypeError) as exc:
                control_errors.append(str(exc))
    fid = audit.fact('calibration_receipt_binding', {'bound_pose_control_receipts': controls, 'errors': control_errors},
        [name for name in audit.receipts if name.startswith('control_') or name == 'reference_control'])
    audit.check('target_calibration', 'limited' if len(controls) == 2 and {r['seed'] for r in controls} == {17, 43} else 'unverified',
        [fid], 'Pose control versus broader target/library screening validation',
        'Raw, applicable-protocol calibration with preregistered success criteria; independent discrimination/enrichment if claimed',
        'Historical reference pose recovery alone does not establish ranking quality, thresholds or general target applicability')

    # A corrupt required input prevents otherwise successful derived checks from granting admission.
    if bad or parse_errors:
        for check in audit.checks:
            if check['check_id'] == 'input_integrity':
                check['status'] = 'invalid'
            if check['status'] in {'verified', 'limited'}:
                check['status'] = 'unverified'
                check['reason'] += '; required input integrity/geometry was not established'
    gaps = [{'gap_id': 'gap_' + c['check_id'], 'check_id': c['check_id'], 'status': 'unverified',
             'observed_check_status': c['status'], 'scope': c['applicability'], 'evidence_ids': c['fact_ids'],
             'reason': c['reason'], 'resolution_criterion': c['resolution_criterion']} for c in audit.checks if c['status'] != 'verified']
    return {'schema_version': SCHEMA, 'scope': scope, 'raw_facts': audit.raw, 'source_statements': audit.statements,
            'checks': audit.checks, 'gaps': gaps, 'source_receipts': list(audit.receipts.values()),
            'formal_ready': False, 'writes_performed': False,
            'limitations': ['Inspection does not prepare molecules, run docking or install capabilities',
                           'Hash agreement establishes provenance consistency, not scientific truth',
                           'No open-ended source search or model inference is substituted for missing validation']}
