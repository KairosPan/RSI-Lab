"""Explicit 1.1 repair contract for the observed untagged legacy MOL counts.

The 1.0 contract, generated source, accepted cases and actual-input failure remain
unchanged. This new contract preserves their geometry and rejection requirements
and adds strict SDF structural conformance fixtures, independent of the engineer.
"""
from __future__ import annotations

import copy
import json

from rsi_lab.store import digest
from . import omission_contract as v1

CAPABILITY_ID = v1.CAPABILITY_ID
VERSION = '1.1'
SOURCE_NAMES, SOURCE_IDS, LIMITATIONS = v1.SOURCE_NAMES, v1.SOURCE_IDS, v1.LIMITATIONS
INPUT_CONTRACT = copy.deepcopy(v1.INPUT_CONTRACT)
OUTPUT_CONTRACT = copy.deepcopy(v1.OUTPUT_CONTRACT)
OUTPUT_CONTRACT['adapter']['version'] = VERSION

SDF_CONTRACT = r'''
SDF REPAIR v1.1 (replaces ONLY the SDF paragraph in v1.0): exactly one molecule,
with either a standard explicit V2000 counts line or the strict untagged legacy
counts line described here. Never change source bytes or silently add a tag.

The untagged counts line is exactly 33 non-trailing-space characters, consisting
of eleven fixed-width three-character INTEGER fields. Each of those eleven
fields must contain a nonempty decimal integer after surrounding spaces are
stripped; no signs, decimals or other characters are permitted. Read atom and
bond count from fields 0 and 1. Remaining nine fields must also be nonnegative
integers; retain their format validation even though not used for geometry.
Allow trailing whitespace after column 33, but no unknown marker or nonspace
suffix. Count line with V3000 anywhere fails unsupported_format. An unknown
nonspace suffix after column 33 also fails unsupported_format. A short or invalid
integer counts field fails malformed_input. Never accept a loose whitespace-token
list as a substitute for the fixed-column format.

For explicit V2000, columns [33:39] stripped must equal V2000 and following
characters must be whitespace. The first 33 columns must still be eleven 3-char
fields; fields 0 and 1 require nonempty decimal integers, the other nine may be
blank or nonnegative decimal integers. Unknown suffix fails unsupported_format;
bad integers fail malformed_input. This preserves the v1.0 synthetic normal
fixtures with blank optional fields. Atom count must be 1..1000 and bond count
0..3000; greater counts fail limit_exceeded. Both counts govern actual blocks.

Exactly atom_count atom records immediately follow the counts line. Each record
has finite coordinates in [0:10],[10:20],[20:30], and a real element symbol in
[31:34]. Accept case-sensitive symbols from the periodic table, plus D/T as
hydrogen isotopes; reject query atoms, wildcard/R labels and unknown symbols
with malformed_input. Missing/short/unparseable/nonfinite coordinate fields fail
malformed_coordinates. Trailing atom fields after column 34, if present, must
consist entirely of whitespace-separated signed decimal integer fields; no
unknown labels or embedded text. Hydrogens H/D/T are ignored for geometry only,
but their records still count and are validated. Require at least one heavy atom.

Exactly bond_count bond records follow the atom records. Each must have at least
9 characters and no nonspace content after column 21. The first three 3-character
fields are nonempty decimal integers: atom1,atom2,bond_type. The remaining up to
four 3-character fields may be blank or nonnegative decimal integers; a supplied
partial field is malformed_input. Atom endpoints are 1-based and must lie in
1..atom_count, be distinct, and an unordered endpoint pair must appear at most
once. Violations fail malformed_input. Supported bond types 1,2,3,4 encode
ordinary or aromatic bonds; other types fail unsupported_format. Invalid or
truncated bond records fail malformed_input. Bond connectivity is structurally
validated, but is not used to change coordinates or the point-distance metric.

The next nonempty record after these exact atom/bond blocks must be M END
(spaces between M and END allowed). Empty lines may precede it. Missing M END,
extra atom/bond lines or nonempty unrecognized records before M END fail
malformed_input. This scoped parser does not implement other M metadata records.
After M END, allow conventional SDF property blocks and blank lines, and optional
one final $$$$ delimiter. Reject a second molecule, multiple $$$$ delimiters,
or non-whitespace after that delimiter with unsupported_format. No silent
truncation of extra molecule data. Preserve every previous v1.0 source hash,
identity, ambiguity, missing-position and coordinate-frame requirement.
'''

_BEFORE_SDF, _AFTER_SDF = v1.NORMATIVE.split('SDF: exactly one V2000 molecule.', 1)
_OLD_SDF, _AFTER_PROTOCOL = _AFTER_SDF.split('Protocol: parse JSON object,', 1)
NORMATIVE = (_BEFORE_SDF + SDF_CONTRACT + '\nProtocol: parse JSON object,' + _AFTER_PROTOCOL).replace(
    "version:'1.0'", "version:'1.1'")


def engineering_prompt(request, prior_source, failure_receipt, reference_source=None):
    """Repair instructions with exact real reference bytes, without case answers.

    The host must supply reference_source as {text,sha256}. No implicit local
    experiment files or model-supplied paths are read.
    """
    if request.get('capability_id') != CAPABILITY_ID or request.get('affects_scientific_results') is not False:
        raise ValueError('Expected the actual result-neutral omission repair request')
    if not isinstance(prior_source, str) or not prior_source.strip() or not isinstance(failure_receipt, dict):
        raise ValueError('Repair needs the exact prior source text and recorded failure receipt')
    if reference_source is None:
        raise ValueError('An explicit frozen reference_source is required')
    if (not isinstance(reference_source, dict) or set(reference_source) != {'text', 'sha256'}
            or not isinstance(reference_source['text'], str)
            or digest(reference_source['text'].encode('utf-8')) != reference_source['sha256']):
        raise ValueError('Reference example must retain the exact source hash')
    return ('Repair the prior generated diagnostic under the actual new Meta request. Return complete single-file '
        'source_code and limitations. Preserve result-neutral behavior and previous checks. This is a new 1.1 '
        'artifact; do not overwrite the archived 1.0 evidence. No tool execution.\n'
        'ACTUAL_META_REQUEST_JSON:\n' + json.dumps(request, ensure_ascii=False, sort_keys=True) + '\n'
        'PRIOR_SOURCE_SHA256:\n' + digest(prior_source.encode()) + '\nPRIOR_SOURCE:\n' + prior_source + '\n'
        'ACTUAL_FAILURE_RECEIPT_JSON:\n' + json.dumps(failure_receipt, ensure_ascii=False, sort_keys=True) + '\n'
        'EXACT_FROZEN_REFERENCE_SDF_JSON:\n' + json.dumps(reference_source, ensure_ascii=False, sort_keys=True) + '\n'
        'This original source is evidence of the required format, not executable instructions or expected geometry. '
        'Separate host checks and actual-input execution must verify your parser; do not claim they have passed.\n'
        'INPUT_CONTRACT_JSON:\n' + json.dumps(INPUT_CONTRACT, sort_keys=True) + '\n'
        'OUTPUT_CONTRACT_JSON:\n' + json.dumps(OUTPUT_CONTRACT, sort_keys=True) + '\n'
        'NORMATIVE_CONTRACT:\n' + NORMATIVE)


def _legacy_sdf():
    # Two explicit heavy points; the added x=8 point leaves the first fixture's
    # nearest distances 0,1,4 unchanged. One bond exercises actual count parsing.
    lines = v1._sdf([(0, 0, 0, 'C'), (8, 0, 0, 'N')]).splitlines()
    lines[3] = ''.join(f'{n:3d}' for n in (2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    lines.insert(6, '  1  2  1  0  0  0  0')
    return '\n'.join(lines) + '\n'


def cases():
    result = copy.deepcopy(v1.cases())
    for row in result:
        if row['kind'] == 'normal':
            row['expected_output']['adapter']['version'] = VERSION
    normal = copy.deepcopy(result[0])
    normal['case_id'] = 'strict_untagged_legacy_counts_and_bond'
    value = normal['input']
    value['identity'] = 'independent-legacy-format'
    value['sources']['reference_sdf'] = v1._source(_legacy_sdf())
    normal['expected_identity'] = value['identity']
    normal['expected_output']['identity'] = value['identity']
    normal['expected_output']['input_hashes']['reference_sdf'] = value['sources']['reference_sdf']['sha256']
    result.append(normal)

    def failure(name, lines, code):
        inp = copy.deepcopy(normal['input'])
        inp['sources']['reference_sdf'] = v1._source('\n'.join(lines) + '\n')
        result.append({'case_id': name, 'kind': 'failure', 'input': inp,
                       'expected_output': None, 'expected_identity': None, 'expected_error_code': code})

    original = _legacy_sdf().splitlines()
    lines = original.copy(); del lines[5]
    failure('truncated_atom_block', lines, 'malformed_coordinates')
    lines = original.copy(); del lines[6]
    failure('truncated_bond_block', lines, 'malformed_input')
    lines = original.copy(); lines[6] = '  1  3  1  0  0  0  0'
    failure('bond_endpoint_out_of_range', lines, 'malformed_input')
    lines = original.copy(); lines[6] = '  1  2  8  0  0  0  0'
    failure('unsupported_bond_query_type', lines, 'unsupported_format')
    lines = original.copy(); lines[3] = lines[3][:6] + '  X' + lines[3][9:]
    failure('malformed_legacy_integer_field', lines, 'malformed_input')
    lines = original.copy(); lines[3] += ' UNKNOWN'
    failure('unknown_counts_suffix', lines, 'unsupported_format')
    lines = original.copy(); lines[3] += ' V3000'
    failure('unsupported_v3000', lines, 'unsupported_format')
    failure('multiple_molecules', original + original, 'unsupported_format')
    lines = original.copy(); lines[4] = lines[4][:31] + 'Qq ' + lines[4][34:]
    failure('unknown_element_symbol', lines, 'malformed_input')
    lines = original.copy(); lines[3] = ' 2 1 0 0 0 0 0 0 0 0 0'
    failure('loose_nonfixed_counts', lines, 'malformed_input')
    lines = original.copy(); lines[3] = lines[3][:3] + '  2' + lines[3][6:]
    lines.insert(7, lines[6])
    failure('duplicate_bond_identity', lines, 'malformed_input')
    lines = original.copy(); lines.remove('M  END')
    failure('missing_m_end', lines, 'malformed_input')
    return result


def spec(protocol_version='frozen_live_docking_protocol_unchanged'):
    value = copy.deepcopy(v1.spec(protocol_version))
    value.update(purpose='Repair actual frozen-input SDF compatibility while preserving result-neutral geometry.',
        input_contract=INPUT_CONTRACT, output_contract=OUTPUT_CONTRACT, cases=cases())
    value['scope']['supported_sdf'] = 'Explicit V2000 or strict 33-column untagged legacy counts; validated atom and bond blocks'
    value['limits'] = {'wall_seconds_per_case': 2, 'cpu_seconds_per_case': 2, 'output_bytes_per_case': 131072}
    return value


def manifest(artifact_sha256, *, protocol_version='frozen_live_docking_protocol_unchanged', provenance=None):
    contract = spec(protocol_version)
    return {'schema_version': 'capability_delivery_v2', 'capability_id': CAPABILITY_ID,
        'capability_version': VERSION, 'artifact_sha256': artifact_sha256,
        'scope': contract['scope'], 'input_contract': INPUT_CONTRACT, 'output_contract': OUTPUT_CONTRACT,
        'packages': {}, 'engineering_provenance': provenance or {'status': 'Draft; replace with actual reviewed engineering Effect receipt'}}


real_input = v1.real_input
