#!/usr/bin/env python3
"""Score-blind, bounded MolScrub preparation; never select among ambiguous states.

The fixed pH interval is an exploratory modelling assumption, not a shared assay
condition. A unique rule-selected state is not a measured dominant microstate.
This module performs no 3D embedding, docking, installation, or network requests.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import importlib.metadata
import io
import json
import multiprocessing
from pathlib import Path
import re
import sys

from rdkit import Chem, rdBase

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime/chemical-preparation'
MAX_STATES = 128
TIMEOUT_SECONDS = 30


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(mol):
    mol = Chem.RemoveHs(Chem.Mol(mol))
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(mol, isomericSmiles=True)


def engine():
    if RUNTIME.is_dir() and str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    import molscrub
    if importlib.metadata.version('molscrub') != '0.3.2':
        raise ValueError('This protocol requires MolScrub 0.3.2')
    return molscrub.Scrub(ph_low=7.5, ph_high=8.0, pka_model='rules',
                         skip_gen3d=True, do_stereoisomers=False, keep_all_frags=True)


def method():
    engine()
    import molscrub
    package = Path(molscrub.__file__).resolve().parent
    files = sorted(p for p in package.rglob('*') if p.is_file() and
                   (p.suffix == '.py' or 'data' in p.relative_to(package).parts))
    return {'schema_version': 'molscrub_single_state_preparation_v1',
            'engine': 'MolScrub', 'version': '0.3.2', 'rdkit_version': rdBase.rdkitVersion,
            'pH': {'low': 7.5, 'high': 8.0, 'interpretation': 'exploratory_interval_not_common_assay_pH'},
            'pka_model': 'rules', 'enumerate_tautomers': True, 'tautomer_rounds': 2,
            'tautomer_scope': 'bounded_rule_heuristic_not_exhaustive',
            'tautomer_retention': 'KEEPMAX_SMARTS_and_KEEPMIN_SMARTS_filter_before_returning_states',
            'unique_state_interpretation': 'one_retained_rule_selected_state_not_one_possible_microstate',
            'pka_scope': 'coarse_functional_group_rules_without_population_or_uncertainty_estimates',
            'upstream_sanitization': 'invalid_reaction_products_are_skipped_and_exhaustive_conversion_may_retain_input',
            'unexpected_exception_diagnostic': 'reject_parent',
            'enumerate_stereoisomers': False, 'skip_gen3d': True,
            'admission': 'exactly_one_predicted_state_with_preserved_identity_and_stereochemistry',
            'population_estimates_available': False, 'maximum_output_states': MAX_STATES,
            'per_parent_timeout_seconds': TIMEOUT_SECONDS,
            'timeout_or_output_overflow': 'reject_parent_never_truncate_or_choose_a_state',
            'source_hashes': {'prepare_chemical_states.py': sha(__file__),
                             **{'molscrub/' + str(p.relative_to(package)): sha(p) for p in files}},
            'dependencies': {name: importlib.metadata.version(name)
                             for name in ('numpy', 'pandas', 'joblib')}}


def _query(parent, stereo):
    """Match heavy connectivity while allowing protonation/tautomer bond changes."""
    query = Chem.RWMol(parent)
    for atom in parent.GetAtoms():
        isotope = str(atom.GetIsotope())
        replacement = Chem.AtomFromSmarts(f'[{isotope}#{atom.GetAtomicNum()}]')
        if stereo:
            replacement.SetChiralTag(atom.GetChiralTag())
        query.ReplaceAtom(atom.GetIdx(), replacement)
    for bond in parent.GetBonds():
        # Keep specified E/Z bonds intact; any disappearance/inversion must fail.
        if not stereo or bond.GetStereo() == Chem.BondStereo.STEREONONE:
            query.ReplaceBond(bond.GetIdx(), Chem.BondFromSmarts('~'))
    return query.GetMol()


def state_issues(parent, state):
    issues = []
    if (state.GetNumAtoms() != parent.GetNumAtoms() or
            state.GetNumBonds() != parent.GetNumBonds() or
            not state.HasSubstructMatch(_query(parent, False))):
        issues.append('heavy_atom_identity_changed')
    elif not state.HasSubstructMatch(_query(parent, True), useChirality=True):
        issues.append('source_stereochemistry_lost_or_changed')
    if any(atom.GetNumRadicalElectrons() for atom in state.GetAtoms()):
        issues.append('radical_state')
    if any(info.specified == Chem.StereoSpecified.Unspecified for info in Chem.FindPotentialStereo(state)):
        issues.append('unspecified_potential_stereochemistry')
    return issues


def prepare_record(row, scrub):
    """Pure one-parent admission logic; injectable enumerator supports unit tests."""
    source = row.get('source_smiles') or row['canonical_smiles']
    parent = Chem.MolFromSmiles(source)
    if parent is None or len(Chem.GetMolFrags(parent)) != 1:
        raise ValueError('Source parent must be a valid, single-component molecule')
    parent = Chem.RemoveHs(parent)
    identity = canonical(parent)
    record = {'candidate_id': row['candidate_id'], 'source_smiles': identity,
              'canonical_parent_identity': identity, 'status': 'rejected', 'reason': None,
              'prepared_state_id': None, 'prepared_smiles': None, 'states': []}
    outputs = list(scrub(Chem.Mol(parent)))
    record['emitted_state_count'] = len(outputs)
    if len(outputs) > MAX_STATES:
        record['reason'] = 'output_state_limit_exceeded'
        return record
    states = {}
    for output in outputs:
        state = Chem.RemoveHs(Chem.Mol(output))
        Chem.SanitizeMol(state)
        smiles = canonical(state)
        state = Chem.MolFromSmiles(smiles)
        states[smiles] = {'state_id': 'state_' + hashlib.sha256(smiles.encode()).hexdigest()[:24],
                          'canonical_smiles': smiles, 'formal_charge': Chem.GetFormalCharge(state),
                          'validation_issues': state_issues(parent, state)}
    record['states'] = [states[key] for key in sorted(states)]
    record['unique_state_count'] = len(states)
    if not states:
        record['reason'] = 'zero_predicted_states'
    elif len(states) != 1:
        record['reason'] = 'ambiguous_predicted_states'
    elif record['states'][0]['validation_issues']:
        record['reason'] = 'invalid_predicted_state'
    else:
        only = record['states'][0]
        record.update(status='accepted', prepared_state_id=only['state_id'],
                      prepared_smiles=only['canonical_smiles'])
    return record


def _child(row, scrub, connection):
    log = io.StringIO()
    rdBase.LogToPythonStderr()
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = prepare_record(row, scrub)
    except Exception as exc:
        result = {'candidate_id': row['candidate_id'], 'source_smiles': row['canonical_smiles'],
                  'canonical_parent_identity': row['canonical_smiles'], 'status': 'rejected',
                  'reason': 'enumeration_error', 'error': f'{type(exc).__name__}: {exc}',
                  'prepared_state_id': None, 'prepared_smiles': None, 'states': []}
    diagnostics = re.sub(r'^\[\d{2}:\d{2}:\d{2}\] ', '', log.getvalue(), flags=re.MULTILINE)
    result['diagnostics'] = diagnostics[:65536]
    result['diagnostics_truncated'] = len(diagnostics) > 65536
    if 'uncought exception' in diagnostics.lower():
        result.update(status='rejected', reason='enumeration_error', prepared_state_id=None,
                      prepared_smiles=None, error='MolScrub reported an unexpected internal exception')
    connection.send(result)
    connection.close()


def bounded_record(row, scrub, timeout=TIMEOUT_SECONDS):
    # MolScrub's rule enumeration has no public state/CPU cap. Isolate each parent
    # so a pathological structure cannot exhaust or hang the whole preparation.
    context = multiprocessing.get_context('fork')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(row, scrub, sender))
    process.start()
    sender.close()
    try:
        if receiver.poll(timeout):
            try:
                return receiver.recv()
            except EOFError:
                reason = 'enumeration_process_failed'
        else:
            reason = 'enumeration_timeout'
        return {'candidate_id': row['candidate_id'], 'source_smiles': row['canonical_smiles'],
                'canonical_parent_identity': row['canonical_smiles'], 'status': 'rejected',
                'reason': reason, 'prepared_state_id': None, 'prepared_smiles': None, 'states': []}
    finally:
        receiver.close()
        process.join(timeout=0.1)
        if process.is_alive():
            process.kill()
            process.join()


def prepare_pool(pool, exclusions):
    scrub = engine()
    preparation_method = method()
    previous = []
    previous_states = set(exclusions['canonical_isomeric_smiles'])
    for index, smiles in enumerate(sorted(previous_states)):
        record = bounded_record({'candidate_id': f'prior_{index:05d}', 'canonical_smiles': smiles}, scrub)
        previous.append(record)
        previous_states.update(s['canonical_smiles'] for s in record['states'])
    incomplete_reasons = {'enumeration_error', 'enumeration_timeout', 'enumeration_process_failed',
                          'output_state_limit_exceeded', 'zero_predicted_states'}
    incomplete = [r['candidate_id'] for r in previous if r['reason'] in incomplete_reasons]
    if incomplete:
        raise ValueError('Prior chemical-state enumeration incomplete; cohort selection blocked: ' + ', '.join(incomplete))
    records = [bounded_record(row, scrub) for row in sorted(pool, key=lambda row: row['candidate_id'])]
    groups = collections.defaultdict(list)
    for record in records:
        if record['status'] == 'accepted':
            groups[record['prepared_smiles']].append(record)
    for record in records:
        if record['status'] != 'accepted':
            continue
        if record['prepared_smiles'] in previous_states:
            record.update(status='rejected', reason='prior_prepared_state_overlap')
        elif len(groups[record['prepared_smiles']]) > 1:
            # Reject the entire collapsed group instead of arbitrarily assigning
            # duplicate parent sources across cohorts.
            record.update(status='rejected', reason='prepared_state_duplicate_parents')
    accepted = {r['candidate_id']: r for r in records if r['status'] == 'accepted'}
    rows = []
    for row in pool:
        record = accepted.get(row['candidate_id'])
        if record:
            rows.append({**row, **{k: record[k] for k in ('source_smiles', 'canonical_parent_identity',
                                                        'prepared_state_id', 'prepared_smiles')},
                         'smiles': record['prepared_smiles'], 'canonical_smiles': record['prepared_smiles']})
    audit = {'schema_version': 'lab_chemical_states_v1', 'method': preparation_method, 'records': records,
             'input_count': len(pool), 'accepted_count': len(rows),
             'accepted_changed_state_count': sum(r['source_smiles'] != r['prepared_smiles'] for r in rows),
             'rejection_counts': dict(collections.Counter(r['reason'] for r in records if r['status'] != 'accepted')),
             'prior_identity_audit': {'source_count': len(previous), 'all_predicted_state_count': len(previous_states),
                                      'records': previous,
                                      'incomplete_count': len(incomplete)},
             'docking_scores_used': False, 'activity_values_used': False,
             'limitations': ['The interval is an exploratory assumption; historical assays have no common verified pH',
                            'A unique predicted state is not proof of dominance or complete microstate coverage',
                            'Two-round tautomer enumeration is filtered by KEEPMAX/KEEPMIN SMARTS before returning states; not exhaustive thermodynamics',
                            'Upstream rejects invalid reaction products; acid/base sanitization failure may retain its input; returned-state validation cannot prove complete pKa-site coverage',
                            'Ligand preparation does not determine receptor protonation or missing structure']}
    return rows, audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-registry', required=True, type=Path)
    parser.add_argument('--exclusions', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    arguments = parser.parse_args()
    source = json.loads(arguments.source_registry.read_text())
    rows, audit = prepare_pool(source['candidates'], json.loads(arguments.exclusions.read_text()))
    audit['prepared_candidates'] = rows
    with arguments.out.open('x') as stream:
        json.dump(audit, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'accepted': len(rows), 'rejections': audit['rejection_counts']}))
