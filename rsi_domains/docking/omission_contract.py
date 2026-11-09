"""Frozen contract and independent point fixtures, not a geometry implementation.

The live engineer receives the normative contract and actual Meta request, but
not these acceptance cases. Expected fixture geometry is written explicitly;
no generated adapter is imported to manufacture the acceptance oracle.
"""
from __future__ import annotations

import copy
import json

from rsi_lab.store import digest

CAPABILITY_ID = 'docking.receptor_omission_geometry'
VERSION = '1.0'
SOURCE_NAMES = ('original_pdb', 'missing_records', 'prepared_pdb', 'prepared_pdbqt', 'reference_sdf', 'protocol')
SOURCE_IDS = {'original_pdb': 'receptor_source/original.pdb',
    'missing_records': 'receptor_source/missing_residues_and_atoms.txt',
    'prepared_pdb': 'receptor/receptor_effective.pdb', 'prepared_pdbqt': 'receptor/receptor.pdbqt',
    'reference_sdf': 'receptor/reference.sdf', 'protocol': 'protocol.json'}
LIMITATIONS = [
    'Observed heavy-atom geometry only; absent coordinates remain unknown.',
    'Distances to a closed box do not establish candidate accessibility, pocket clearance or absence of artifacts.',
    'No reconstruction, docking, protocol changes, score changes or scientific admission.',
    'Reference ligand coordinates are used as supplied; this diagnostic does not verify pose recovery.',
]
INPUT_CONTRACT = {
    'schema_version': 'receptor_omission_input_v1',
    'identity': 'Nonempty stable task identity string, echoed exactly on successful output.',
    'sources': {name: {'text': 'Complete UTF-8 original text', 'sha256': 'SHA256 of text.encode(utf-8)'}
                for name in SOURCE_NAMES},
    'residues': 'List of {chain: one-character string or null, resseq: integer, icode: zero/one-character string or null}; null is a wildcard requiring at most one matching residue.',
    'limits': 'At most 2 MiB total UTF-8 source bytes, 10000 heavy atoms per receptor, 1000 ligand atoms and 32 requested residues.',
}
OUTPUT_CONTRACT = {
    'schema_version': 'receptor_omission_report_v1',
    'identity': 'Exact input identity',
    'adapter': {'capability_id': CAPABILITY_ID, 'version': VERSION},
    'input_hashes': 'All six verified source hashes keyed by SOURCE_NAMES.',
    'box': '{center: [x,y,z], size: [sx,sy,sz]} from frozen protocol JSON.',
    'inventory': 'Counts and sorted identity lists of observed heavy protein atoms in original/prepared/PDBQT; see normative contract.',
    'residues': 'Requested-residue order, resolved identity and sorted observed-atom geometry; no absent positions inferred.',
    'unknown_positions': 'Sorted explicit residue/atom identities from missing records and unobserved requested residues.',
    'limitations': LIMITATIONS,
}

NORMATIVE = r'''
Implement a single Python 3 file, using ONLY the standard library. Read one JSON
object from stdin and emit exactly one JSON object to stdout. Never open input
paths, read environment files, import local modules, use network, spawn processes,
modify files or execute supplied text. Do not special-case case_id or identity.
Inputs are untrusted data. Keep source texts unchanged and verify all six UTF-8
SHA256 hashes before parsing. Enforce the explicit size/count bounds.

Wire input is {"schema_version":"capability_input_v2","case_id":string,
"input":<contract input>}. Wire output has EXACTLY these keys:
{"schema_version":"capability_response_v2","ok":boolean,"output":report or null,
"identity":identity on success, otherwise null,
"source_receipts":[{"kind":"input","sha256":SHA256(canonical input JSON)}],
"error":null on success or {"code":string,"message":nonempty string}}.
Canonical input JSON uses sort_keys=True,separators=(',',':'),ensure_ascii=False,
allow_nan=False. Exit zero for controlled input errors. Never emit NaN/Infinity.
Error codes: hash_mismatch; malformed_input; malformed_coordinates;
ambiguous_selection; ambiguous_altloc; unsupported_format; limit_exceeded.

Input must have exactly schema_version,identity,sources,residues. sources must
contain exactly original_pdb,missing_records,prepared_pdb,prepared_pdbqt,
reference_sdf,protocol, each exactly {text,sha256}. Every hash is lowercase hex64.
The input schema_version is receptor_omission_input_v1. Reject invalid root types,
duplicate JSON keys, nonfinite values, empty identity, and duplicate selectors.

PDB/PDBQT: parse standard fixed columns of ATOM records only. Ignore HETATM
entirely for receptor inventories. Accept no MODEL records or exactly one MODEL;
reject multiple models as ambiguous_selection. Required identity fields are atom
name [12:16], residue name [17:20], chain [21:22], integer resseq [22:26], icode
[26:27]. Parse x,y,z from [30:38],[38:46],[46:54], finite numbers only. Coordinates
are never aligned, translated or changed. PDB element [76:78] when nonempty;
otherwise infer from first alphabetic character of atom name for this protein-only
scope. PDBQT element: final AutoDock atom type H/HD/HS is hydrogen; other types
are heavy. Discard H/D/T atoms from inventories and geometry. A nonblank altloc
[16:17] on ANY heavy ATOM in any receptor input fails ambiguous_altloc; there is
no implicit highest-occupancy choice. Duplicate heavy atom identity in one file,
or conflicting residue names at the same chain/resseq/icode, fails
ambiguous_selection. A residue identity is chain + ':' + decimal resseq + ':' +
icode. Atom identity is residue identity + ':' + resname + ':' + atom_name.
Examples: A:36::ARG:CA and B:2:A:GLY:CA. Blank chain is represented as ''.
Malformed ATOM coordinate/identity records fail explicitly, never get skipped.

SDF: exactly one V2000 molecule. Parse counts at fourth line [0:3] and [3:6],
atom coordinate columns [0:10],[10:20],[20:30], element [31:34]. Reject V3000,
bad counts, malformed/nonfinite coordinates, missing M END, multiple molecule
records or no heavy ligand atoms. Ignore H,D,T; use all remaining atom coordinates
as a supplied reference point set. Bond connectivity is not used for this metric.

Protocol: parse JSON object, use center and size, each exactly three finite
numbers with every size>0. Other protocol fields are retained as source evidence
but not changed or interpreted. No frame alignment. The closed axis-aligned box
is [center_i-size_i/2,center_i+size_i/2]. Each observed atom reports Euclidean
distance to that closed box (zero inside or boundary), minimum Euclidean distance
to any reference heavy atom, and classification: outside if any coordinate lies
outside bounds; boundary if within bounds and any equals a bound; inside
otherwise. Round reported distances to 6 decimals, keeping original coordinates
as parsed floats. Interior and boundary atoms both have distance_to_box=0.0.

For each selector in input order, match original observed residues by exact
resseq and specified chain/icode. Null chain/icode are wildcards. More than one
matching residue fails ambiguous_selection. If no observed match, identify it
using exact chain and icode; if either is null, fail ambiguous_selection because
the missing identity cannot be resolved. A known missing residue is reported
unknown, never assigned coordinates. Selected original atoms are sorted by full
atom identity. Per-residue min values are from ONLY these observed original atoms;
the presence/absence of the residue in prepared files does not remove the
original observations from measurement.

Report EXACT keys: schema_version,identity,adapter,input_hashes,box,inventory,
residues,unknown_positions,limitations. schema_version receptor_omission_report_v1.
adapter is {capability_id:'docking.receptor_omission_geometry',version:'1.0'}.
inventory EXACT keys:
{original_heavy_atoms:int,prepared_heavy_atoms:int,pdbqt_heavy_atoms:int,
removed_from_prepared:[sorted original-minus-prepared atom identities],
added_in_prepared:[sorted prepared-minus-original atom identities],
missing_from_pdbqt:[sorted prepared-minus-pdbqt atom identities],
added_in_pdbqt:[sorted pdbqt-minus-prepared atom identities]}.
Each residues entry EXACT keys:
{residue_id:string,observed_atom_count:int,min_distance_to_box:number or null,
min_distance_to_reference:number or null,atoms:[...]}, each atoms entry EXACT keys:
{atom_id:string,xyz:[x,y,z],classification:'inside'|'boundary'|'outside',
distance_to_box:number,distance_to_reference:number,in_prepared:bool,in_pdbqt:bool}.

Parse missing_records REMARK 465 and REMARK 470 PDB text, ignoring prose/header
lines, but recognize data rows of these forms (whitespace, optional model number,
optional insertion code supported):
REMARK 465     LEU A    37
REMARK 470     ARG A  36    CG   CD   NE
For each REMARK 465 data row emit {kind:'residue',identity:'A:37:',position:null}.
For each named REMARK 470 atom emit {kind:'atom',identity:'A:36::ARG:CG',position:null}.
Missing-record rows must identify the same chain/resseq/icode/resname scheme as
observed inventory, with insertions kept distinct. Check that no documented
missing residue/atom appears in original observed inventory; contradictory
records fail malformed_input. Every data line in missing_records must be an
exact line present in original_pdb (ignoring trailing spaces), otherwise fail
malformed_input. It is a source-bound extract, not independently invented data.
If an exact requested residue is unobserved and absent from REMARK465, add the
same kind:'residue' unknown entry. Deduplicate all unknown entries and sort by
(kind,identity). No unknown entry includes inferred coordinates, distance or
clearance. An empty observed residue has atoms=[], both min distances null.

Return limitations EXACTLY as supplied in the output contract. A valid geometry
report is never a declaration of safe docking, candidate accessibility, clearance,
resolved scientific prerequisites, or successful receptor repair.
'''


def engineering_prompt(request):
    """Frozen implementation instructions, excluding acceptance answers."""
    if request.get('capability_id') != CAPABILITY_ID or request.get('affects_scientific_results') is not False:
        raise ValueError('This contract implements only the requested result-neutral omission diagnostic')
    return ('Implement the actual Meta-requested missing capability below. Return your single-file source_code '
        'and concise limitations in the supplied JSON response schema. No tool use or code execution.\n'
        'META_REQUEST_JSON:\n' + json.dumps(request, ensure_ascii=False, sort_keys=True) + '\n'
        'INPUT_CONTRACT_JSON:\n' + json.dumps(INPUT_CONTRACT, sort_keys=True) + '\n'
        'OUTPUT_CONTRACT_JSON:\n' + json.dumps(OUTPUT_CONTRACT, sort_keys=True) + '\n'
        'NORMATIVE_CONTRACT:\n' + NORMATIVE)


def _source(text):
    return {'text': text, 'sha256': digest(text.encode())}


def _atom(serial, resseq, xyz, *, chain='A', icode='', residue='ARG', name='CA', altloc='', element='C', pdbqt=False):
    line = (f'ATOM  {serial:5d} {name:^4s}{altloc:1s}{residue:3s} {chain:1s}{resseq:4d}{icode:1s}   '
            f'{xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}{1.0:6.2f}{0.0:6.2f}')
    return line + (f'    {0.0:6.3f} {element}\n' if pdbqt else f'          {element:>2s}  \n')


def _sdf(points):
    return ('point fixture\ncontract oracle\n\n' + f'{len(points):3d}{0:3d}  0  0  0  0            999 V2000\n'
        + ''.join(f'{x:10.4f}{y:10.4f}{z:10.4f} {element:<3s} 0  0  0  0  0  0  0  0  0  0  0  0\n'
                  for x, y, z, element in points) + 'M  END\n$$$$\n')


def _fixture():
    remarks = 'REMARK 465     LEU A    37\nREMARK 470     ARG A  36    CG\n'
    original = (remarks + _atom(1, 36, (0, 0, 0)) + _atom(2, 43, (1, 0, 0), residue='LEU')
                + _atom(3, 150, (4, 0, 0)) + 'END\n')
    prepared = _atom(2, 43, (1, 0, 0), residue='LEU') + _atom(3, 150, (4, 0, 0)) + 'END\n'
    pdbqt = _atom(2, 43, (1, 0, 0), residue='LEU', pdbqt=True) + _atom(3, 150, (4, 0, 0), pdbqt=True)
    return {'schema_version': 'receptor_omission_input_v1', 'identity': 'independent-fixture-1',
        'sources': {name: _source(text) for name, text in {
            'original_pdb': original, 'missing_records': remarks, 'prepared_pdb': prepared,
            'prepared_pdbqt': pdbqt, 'reference_sdf': _sdf([(0, 0, 0, 'C')]),
            'protocol': json.dumps({'center': [0, 0, 0], 'size': [2, 2, 2]})}.items()},
        'residues': [{'chain': 'A', 'resseq': number, 'icode': ''} for number in (36, 43, 150, 37)]}


def _report_head(value, inventory):
    return {'schema_version': 'receptor_omission_report_v1', 'identity': value['identity'],
        'adapter': {'capability_id': CAPABILITY_ID, 'version': VERSION},
        'input_hashes': {name: value['sources'][name]['sha256'] for name in SOURCE_NAMES},
        'box': {'center': [0.0, 0.0, 0.0], 'size': [2.0, 2.0, 2.0]},
        'inventory': inventory, 'residues': [], 'unknown_positions': [], 'limitations': LIMITATIONS}


def _point_entry(residue_id, atom_id, xyz, classification, box_distance, reference_distance, prepared=True):
    return {'residue_id': residue_id, 'observed_atom_count': 1,
        'min_distance_to_box': box_distance, 'min_distance_to_reference': reference_distance,
        'atoms': [{'atom_id': atom_id, 'xyz': list(xyz), 'classification': classification,
                   'distance_to_box': box_distance, 'distance_to_reference': reference_distance,
                   'in_prepared': prepared, 'in_pdbqt': prepared}]}


def cases():
    """Independent fixtures with hand-calculated expected distances."""
    first = _fixture()
    expected = _report_head(first, {'original_heavy_atoms': 3, 'prepared_heavy_atoms': 2, 'pdbqt_heavy_atoms': 2,
        'removed_from_prepared': ['A:36::ARG:CA'], 'added_in_prepared': [], 'missing_from_pdbqt': [], 'added_in_pdbqt': []})
    expected['residues'] = [
        _point_entry('A:36:', 'A:36::ARG:CA', (0., 0., 0.), 'inside', 0., 0., False),
        _point_entry('A:43:', 'A:43::LEU:CA', (1., 0., 0.), 'boundary', 0., 1.),
        _point_entry('A:150:', 'A:150::ARG:CA', (4., 0., 0.), 'outside', 3., 4.),
        {'residue_id': 'A:37:', 'observed_atom_count': 0, 'min_distance_to_box': None,
         'min_distance_to_reference': None, 'atoms': []}]
    expected['unknown_positions'] = [{'kind': 'atom', 'identity': 'A:36::ARG:CG', 'position': None},
                                     {'kind': 'residue', 'identity': 'A:37:', 'position': None}]
    result = [{'case_id': 'inside_boundary_outside_and_unknowns', 'kind': 'normal', 'input': first,
               'expected_output': expected, 'expected_identity': first['identity'], 'expected_error_code': None}]
    second = _fixture()
    second['identity'] = 'independent-chain-insertion-and-hydrogen'
    line = _atom(1, 2, (4, 5, 0), chain='B', icode='A', residue='GLY')
    second['sources'].update(original_pdb=_source(line), prepared_pdb=_source(line), missing_records=_source(''),
        prepared_pdbqt=_source(_atom(1, 2, (4, 5, 0), chain='B', icode='A', residue='GLY', pdbqt=True)),
        reference_sdf=_source(_sdf([(0, 0, 0, 'C'), (4, 5, 0, 'H')])))
    second['residues'] = [{'chain': 'B', 'resseq': 2, 'icode': 'A'}, {'chain': 'B', 'resseq': 99, 'icode': ''}]
    expected2 = _report_head(second, {'original_heavy_atoms': 1, 'prepared_heavy_atoms': 1, 'pdbqt_heavy_atoms': 1,
        'removed_from_prepared': [], 'added_in_prepared': [], 'missing_from_pdbqt': [], 'added_in_pdbqt': []})
    expected2['residues'] = [_point_entry('B:2:A', 'B:2:A:GLY:CA', (4., 5., 0.), 'outside', 5., 6.403124),
        {'residue_id': 'B:99:', 'observed_atom_count': 0, 'min_distance_to_box': None,
         'min_distance_to_reference': None, 'atoms': []}]
    expected2['unknown_positions'] = [{'kind': 'residue', 'identity': 'B:99:', 'position': None}]
    result.append({'case_id': 'chain_insertion_diagonal_and_unobserved', 'kind': 'normal', 'input': second,
                   'expected_output': expected2, 'expected_identity': second['identity'], 'expected_error_code': None})
    def failure(name, value, code):
        result.append({'case_id': name, 'kind': 'failure', 'input': value,
                       'expected_output': None, 'expected_identity': None, 'expected_error_code': code})
    bad = copy.deepcopy(first); bad['sources']['original_pdb']['sha256'] = '0' * 64
    failure('hash_mismatch', bad, 'hash_mismatch')
    bad = copy.deepcopy(first)
    text = bad['sources']['original_pdb']['text'].replace('   0.000', '     nan', 1)
    bad['sources']['original_pdb'] = _source(text)
    failure('nonfinite_coordinate', bad, 'malformed_coordinates')
    bad = copy.deepcopy(first)
    bad['sources']['original_pdb'] = _source(first['sources']['original_pdb']['text'] + _atom(4, 36, (1, 2, 3), chain='B'))
    bad['residues'][0]['chain'] = None
    failure('ambiguous_chain', bad, 'ambiguous_selection')
    bad = copy.deepcopy(first)
    bad['sources']['original_pdb'] = _source(first['sources']['original_pdb']['text'] + _atom(4, 36, (1, 2, 3), icode='A'))
    bad['residues'][0]['icode'] = None
    failure('ambiguous_insertion', bad, 'ambiguous_selection')
    bad = copy.deepcopy(first)
    bad['sources']['original_pdb'] = _source(first['sources']['original_pdb']['text'].replace(_atom(1, 36, (0, 0, 0)), _atom(1, 36, (0, 0, 0), altloc='A')))
    failure('unresolved_altloc', bad, 'ambiguous_altloc')
    bad = copy.deepcopy(first)
    bad['sources']['original_pdb'] = _source(first['sources']['original_pdb']['text'] + _atom(4, 36, (1, 2, 3)))
    failure('duplicate_atom_identity', bad, 'ambiguous_selection')
    bad = copy.deepcopy(first); bad['identity'] = ''
    failure('missing_task_identity', bad, 'malformed_input')
    bad = copy.deepcopy(first)
    bad['sources']['missing_records'] = _source('REMARK 465     LEU A   999\n')
    failure('unbound_missing_records', bad, 'malformed_input')
    return result


def spec(protocol_version='frozen_live_docking_protocol_unchanged'):
    return {'schema_version': 'capability_request_v2', 'capability_id': CAPABILITY_ID,
        'purpose': 'Measure original observed receptor-omission geometry without changing scientific results.',
        'scope': {'target': 'Explicit frozen receptor and caller-selected residues', 'coordinates': 'Original observed heavy atoms only',
                  'admission': 'Diagnostic measurement only; no scientific clearance inferred'},
        'input_contract': INPUT_CONTRACT, 'output_contract': OUTPUT_CONTRACT,
        'scientific_impact': False, 'baseline_scientific_protocol': protocol_version,
        'candidate_scientific_protocol': protocol_version, 'new_study_id': None,
        'execution_profile': 'reviewed_local_python_json_v1',
        'recovery': {'query_supported': False, 'idempotency_supported': False, 'unknown_result': 'blocked_external'},
        'limits': {'wall_seconds_per_case': 5, 'cpu_seconds_per_case': 2, 'output_bytes_per_case': 131072},
        'cases': cases()}


def manifest(artifact_sha256, *, protocol_version='frozen_live_docking_protocol_unchanged', provenance=None):
    contract = spec(protocol_version)
    return {'schema_version': 'capability_delivery_v2', 'capability_id': CAPABILITY_ID,
        'capability_version': VERSION, 'artifact_sha256': artifact_sha256,
        'scope': contract['scope'], 'input_contract': INPUT_CONTRACT, 'output_contract': OUTPUT_CONTRACT,
        'packages': {}, 'engineering_provenance': provenance or {'status': 'Draft; replace with actual reviewed engineering Effect receipt'}}


def real_input(sources, *, identity, residues, store=None, source_ids=None):
    """Build exact tool input from frozen source rows plus their immutable store.

    Alternatively accept a source-id -> {text,sha256} mapping already read from
    authorized immutable blobs. The host explicitly supplies the task identity,
    residue selection and any nonstandard source-name mapping. No path in a
    model request is opened here.
    """
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError('An explicit nonempty task identity is required')
    if not isinstance(residues, list) or not 1 <= len(residues) <= 32:
        raise ValueError('An explicit selection of 1–32 residues is required')
    for row in residues:
        if (not isinstance(row, dict) or set(row) != {'chain', 'resseq', 'icode'}
                or type(row['resseq']) is not int
                or row['chain'] is not None and (not isinstance(row['chain'], str) or len(row['chain']) != 1)
                or row['icode'] is not None and (not isinstance(row['icode'], str) or len(row['icode']) > 1)):
            raise ValueError('Invalid explicit residue selection')
    source_ids = SOURCE_IDS if source_ids is None else source_ids
    if (not isinstance(source_ids, dict) or set(source_ids) != set(SOURCE_NAMES)
            or any(not isinstance(value, str) or not value for value in source_ids.values())):
        raise ValueError('Explicit source mapping must identify all required source kinds')
    if isinstance(sources, list):
        if store is None:
            raise ValueError('Frozen source rows require their immutable Store')
        by_id = {row['source_id']: row for row in sources}
        source_map = {name: {'text': store.read_blob(by_id[sid]['source_ref']).decode('utf-8'),
                             'sha256': by_id[sid]['sha256']} for name, sid in source_ids.items()}
    else:
        source_map = {name: copy.deepcopy(sources[sid]) for name, sid in source_ids.items()}
    if any(digest(value['text'].encode()) != value['sha256'] for value in source_map.values()):
        raise ValueError('Real tool inputs differ from frozen source hashes')
    return {'schema_version': 'receptor_omission_input_v1', 'identity': identity, 'sources': source_map,
        'residues': copy.deepcopy(residues)}
