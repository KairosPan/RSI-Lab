#!/usr/bin/env python3
"""Build disjoint structure-only CDK2 cohorts without reading docking scores.

Run with the existing chemistry environment. This performs no embedding,
docking, model calls, network requests, or package installation.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path
import re
import shutil

from rdkit import Chem, rdBase
from rdkit.Chem import Descriptors, Lipinski
from rdkit.Chem.Scaffolds import MurckoScaffold

COHORTS = ('development_01', 'validation_01', 'validation_02', 'development_02')
SALT = 'rsi_lab_agentic_structure_only_v1'
ROOT = Path(__file__).resolve().parents[1]
PRIOR = Path('/Users/pan/Desktop/cis7000')


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path, value):
    with Path(path).open('x') as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def order_hash(value):
    return hashlib.sha256((SALT + ':' + value).encode()).hexdigest()


def structure_features(mol):
    scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=mol)
    return {'scaffold_smiles': scaffold, 'scaffold_id': 'scaf_' + hashlib.sha256(scaffold.encode()).hexdigest()[:16],
            'molecular_weight': Descriptors.MolWt(mol), 'logP': Descriptors.MolLogP(mol),
            'TPSA': Descriptors.TPSA(mol), 'HBD': Lipinski.NumHDonors(mol),
            'HBA': Lipinski.NumHAcceptors(mol), 'rotatable_bonds': Lipinski.NumRotatableBonds(mol),
            'heavy_atoms': mol.GetNumHeavyAtoms(), 'formal_charge': Chem.GetFormalCharge(mol),
            'ring_count': Lipinski.RingCount(mol), 'fragment_count': 1, 'unassigned_stereocenters': []}


def exclusions(args):
    if args.exclusions:
        return read(args.exclusions)
    ids, parents, structures, sources = set(), set(), set(), []

    def take(row):
        cid = row.get('candidate_id') or row.get('molecule_chembl_id')
        if cid:
            ids.add(cid)
        if row.get('parent_molecule_chembl_id'):
            parents.add(row['parent_molecule_chembl_id'])
        smiles = row.get('canonical_smiles') or row.get('smiles')
        if isinstance(smiles, str):
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                raise ValueError(f'Unparseable prior identity {cid}')
            structures.add(Chem.MolToSmiles(mol, isomericSmiles=True))

    def catalog(path):
        value = read(path)
        rows = value if isinstance(value, list) else value['candidates']
        sources.append({'path': str(path.resolve()), 'sha256': sha(path), 'kind': 'prior_catalog', 'records': len(rows)})
        for row in rows:
            take(row)

    for folder in sorted(args.prior_data.iterdir()):
        for name in ('candidates.json', 'eligible_assay_candidates.json'):
            path = folder / name
            if path.is_file():
                catalog(path)
    for path in args.exclude_catalog:
        if path.is_file():
            catalog(path)
    for folder in args.prior_run_root:
        for path in sorted(folder.glob('**/spec.json')):
            value = read(path)
            if isinstance(value.get('candidate'), dict):
                take(value['candidate'])
                sources.append({'path': str(path.resolve()), 'sha256': sha(path), 'kind': 'prior_job_identity'})
    return {'schema_version': 'agentic_identity_exclusions_v1', 'candidate_ids': sorted(ids),
            'parent_ids': sorted(parents), 'canonical_isomeric_smiles': sorted(structures),
            'sources': sources, 'score_files_read': False,
            'scope': 'Conservatively excludes entire prior catalogs and all historical job-spec identities, including unrun entries'}


def collect(raw, excluded):
    ids, parents, old = (set(excluded[k]) for k in ('candidate_ids', 'parent_ids', 'canonical_isomeric_smiles'))
    pool, rejected, counts = {}, [], collections.Counter()
    for row in sorted(raw, key=lambda r: (r.get('molecule_chembl_id') or '', r['activity_id'])):
        reason, stereo_issues = None, []
        description = row.get('assay_description') or ''
        if row.get('data_validity_comment') not in (None, '', 'Manually validated'):
            reason = 'flagged_data_validity'
        elif re.search(r'alloster|GSK3', description, re.I):
            reason = 'explicit_incompatible_site_or_target_description'
        elif row.get('target_chembl_id') != 'CHEMBL301':
            reason = 'wrong_target'
        else:
            smiles = row.get('canonical_smiles')
            mol = Chem.MolFromSmiles(smiles) if isinstance(smiles, str) else None
            if mol is None:
                reason = 'invalid_structure'
            elif len(Chem.GetMolFrags(mol)) != 1:
                reason = 'multiple_fragments'
            elif any(a.GetNumRadicalElectrons() for a in mol.GetAtoms()):
                reason = 'radical'
            elif (stereo_issues := [{'type': str(info.type), 'centered_on': info.centeredOn,
                                    'specified': str(info.specified)}
                                   for info in Chem.FindPotentialStereo(mol)
                                   if info.specified == Chem.StereoSpecified.Unspecified]):
                reason = 'unspecified_potential_stereochemistry'
            elif not 200 <= Descriptors.MolWt(mol) <= 600:
                reason = 'mw_outside_200_600'
            elif Lipinski.NumRotatableBonds(mol) > 8:
                reason = 'rotatable_bonds_over8'
        if not reason:
            canonical = Chem.MolToSmiles(mol, isomericSmiles=True)
            cid, parent = row['molecule_chembl_id'], row.get('parent_molecule_chembl_id')
            if canonical in old or cid in ids or parent in parents or parent in ids:
                reason = 'prior_identity'
            elif canonical in pool:
                # Preserve all clean source IDs without exposing assay values.
                current = pool[canonical]
                current['activity_ids'] = sorted(set(current['activity_ids']) | {row['activity_id']})
                current['assay_ids'] = sorted(set(current['assay_ids']) | {row['assay_chembl_id']})
                if row.get('document_chembl_id'):
                    current['document_ids'] = sorted(set(current['document_ids']) | {row['document_chembl_id']})
                reason = 'canonical_duplicate'
        if reason:
            counts[reason] += 1
            rejected.append({'activity_id': row['activity_id'], 'candidate_id': row.get('molecule_chembl_id'),
                             'reason': reason, **({'stereo_issues': stereo_issues} if stereo_issues else {})})
            continue
        pool[canonical] = {'candidate_id': cid, 'parent_molecule_chembl_id': parent,
            'smiles': canonical, 'canonical_smiles': canonical, 'assay_id': row['assay_chembl_id'],
            'assay_ids': [row['assay_chembl_id']], 'activity_ids': [row['activity_id']],
            'document_ids': [row['document_chembl_id']] if row.get('document_chembl_id') else [],
            'features': structure_features(mol)}
    return list(pool.values()), dict(counts), rejected


def build(args):
    if args.out.exists():
        raise ValueError('Output must not exist; never revise a frozen cohort in place')
    excluded = exclusions(args)
    raw_payload = read(args.source)
    pool, counts, rejected = collect(raw_payload['activities'], excluded)
    original_pool_count = len(pool)
    chemical_audit = None
    if getattr(args, 'chemical_preparation', False):
        try:
            from scripts.prepare_chemical_states import prepare_pool
        except ModuleNotFoundError:
            from prepare_chemical_states import prepare_pool
        pool, chemical_audit = prepare_pool(pool, excluded)
        for row in pool:
            row['features'] = structure_features(Chem.MolFromSmiles(row['prepared_smiles']))
    by_scaffold = collections.defaultdict(list)
    for row in pool:
        by_scaffold[row['features']['scaffold_smiles']].append(row)
    if len(by_scaffold) < 80:
        raise ValueError('Need at least80 clean scaffolds for this fixed four-cohort design')
    for rows in by_scaffold.values():
        rows.sort(key=lambda r: (order_hash(r['canonical_smiles']), r['candidate_id']))
    scaffold_order = sorted(by_scaffold, key=lambda s: (order_hash(s), s))
    selected = [by_scaffold[s][0] for s in scaffold_order[:80]]
    for key in ('candidate_id', 'canonical_smiles', 'parent_molecule_chembl_id'):
        if len({r[key] for r in selected}) != 80:
            raise ValueError(f'Selected identity conflict in {key}')
    assignments = {r['canonical_smiles']: COHORTS[i % 4] for i, r in enumerate(selected)}
    args.out.mkdir(parents=True)
    if args.exclusions:
        shutil.copyfile(args.exclusions, args.out / 'identity_exclusions.json')
    else:
        dump(args.out / 'identity_exclusions.json', excluded)
    if chemical_audit:
        dump(args.out / 'chemical_preparation_audit.json', chemical_audit)
    provenance = {'schema_version': 'structure_only_source_provenance_v1',
                  'original_source_path': str(args.source.resolve()), 'original_source_sha256': sha(args.source),
                  'source_format': 'Frozen ChEMBL37 activity response used only for identity and assay provenance',
                  'activity_values_exposed': False, 'activity_values_used_for_selection': False,
                  'source_scope': 'First1000 lexical-assay activity records, not the complete target universe',
                  'rdkit_version': rdBase.rdkitVersion}
    if args.source_metadata.is_file():
        meta = read(args.source_metadata)
        if meta['sha256'] != provenance['original_source_sha256']:
            raise ValueError('Source retrieval receipt does not match source bytes')
        provenance.update(source_url=meta['url'], retrieved_at_utc=meta['retrieved_at_utc'])
    dump(args.out / 'source_registry.json', {'schema_version': 'agentic_unscored_structures_v1',
         'source': provenance, 'count': len(pool), 'assigned_count': 80, 'unassigned_count': len(pool) - 80,
         'candidates': [{**r, 'assigned_cohort': assignments.get(r['canonical_smiles'])}
                        for r in sorted(pool, key=lambda r: r['candidate_id'])]})
    cohort_summary = {}
    for index, name in enumerate(COHORTS):
        folder = args.out / name
        folder.mkdir()
        candidates = selected[index::4]
        assay_ids = sorted({a for row in candidates for a in row['assay_ids']})
        assays = []
        for aid in assay_ids:
            observations = [row for row in raw_payload['activities'] if row['assay_chembl_id'] == aid]
            descriptions = sorted({r.get('assay_description') for r in observations if r.get('assay_description')})
            assays.append({'assay_id': aid, 'descriptions': descriptions,
                           'assay_pH_mentions': sorted({p for desc in descriptions for p in re.findall(r'\bpH\s*([0-9.]+)', desc, re.I)}),
                           'assay_species_verified': False, 'confidence_score': None,
                           'metadata_status': 'Description from frozen activities; full assay metadata not retrieved'})
        dump(folder / 'candidates.json', {'candidates': candidates})
        manifest = {'schema_version': 'lab_structure_only_catalog_v1', 'is_mock': False,
             'synthetic': False, 'public_input_mode': 'structure_only', 'target_chembl_id': 'CHEMBL301',
             'construct_id': '1DI8_chain_A', 'cohort': name, 'candidates': candidates,
             'source_provenance': provenance, 'scope': 'Isolated20-candidate live agentic development/validation pilot; no formal100 claim'}
        if chemical_audit:
            manifest['chemical_preparation'] = {'method': chemical_audit['method']}
            selected_ids = {row['candidate_id'] for row in candidates}
            dump(folder / 'chemical_states.json', {'schema_version': 'lab_chemical_states_v1',
                 'method': chemical_audit['method'], 'records': [r for r in chemical_audit['records']
                                                              if r['candidate_id'] in selected_ids]})
        dump(folder / 'manifest.json', manifest)
        dump(folder / 'assay_details.json', {'schema_version': 'mixed_assay_provenance_v1',
             'target_id': 'CHEMBL301', 'description': 'Mixed historical source assays, provenance only; no numerical activity labels supplied',
             'assays': assays, 'assay_organism': None, 'confidence_score': None,
             'limitations': ['No common verified assay pH; preserve per-assay mentions only',
                            'Human target annotation does not verify measured protein species',
                            'No cross-assay potency comparisons or biological efficacy claim']})
        dump(folder / 'source_manifest.json', provenance)
        shutil.copytree(args.receptor, folder / 'receptor')
        cohort_summary[name] = {'count': 20, 'candidate_ids': [r['candidate_id'] for r in candidates],
                               'unique_scaffolds': len({r['features']['scaffold_smiles'] for r in candidates}),
                               'data_hashes': {str(p.relative_to(folder)): sha(p) for p in sorted(folder.rglob('*')) if p.is_file()}}
    dump(args.out / 'split_manifest.json', {'schema_version': 'agentic_structure_only_split_v1',
         'source': provenance, 'builder_sha256': sha(Path(__file__)),
         'exclusions_sha256': sha(args.out / 'identity_exclusions.json'),
         'source_registry_sha256': sha(args.out / 'source_registry.json'), 'eligible_count': len(pool),
         'source_structure_count': original_pool_count,
         'chemical_preparation_audit_sha256': sha(args.out / 'chemical_preparation_audit.json') if chemical_audit else None,
         'eligible_scaffolds': len(by_scaffold), 'filter_counts': counts, 'rejected_rows': rejected,
         'selection': {'salt': SALT, 'hash': 'SHA256 UTF8(salt + colon + value)',
                       'scaffold_order': '(hash(scaffold),scaffold)',
                       'within_scaffold_order': '(hash(canonical_smiles),candidate_id)',
                       'cohort_assignment': 'First80 scaffold representatives interleaved by index modulo4',
                       'cohort_order': list(COHORTS), 'activity_values_used': False, 'docking_score_files_read': False},
         'identity_checks': {'unique_selected_structures': 80, 'unique_selected_parent_ids': 80,
                             'unique_selected_scaffolds': 80, 'prior_identity_overlap': 0},
         'cohorts': cohort_summary})
    print(json.dumps({'out': str(args.out.resolve()), 'cohorts': {k: v['count'] for k, v in cohort_summary.items()},
                      'remaining_unassigned': len(pool) - 80, 'eligible_total': len(pool)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--source', type=Path, default=PRIOR / 'data/lab_cdk2_v2/raw/17a4661f6f6cce070d3bc5c00fefe03722f00f11a04bf73ea6d421ab0e36c8e1.json')
    parser.add_argument('--source-metadata', type=Path, default=PRIOR / 'data/lab_cdk2_v2/raw/17a4661f6f6cce070d3bc5c00fefe03722f00f11a04bf73ea6d421ab0e36c8e1.meta.json')
    parser.add_argument('--receptor', type=Path, default=ROOT / 'runs/cdk2_screen_120_30min_20261105_1745/data/receptor')
    parser.add_argument('--prior-data', type=Path, default=PRIOR / 'data')
    parser.add_argument('--prior-run-root', action='append', type=Path)
    parser.add_argument('--exclude-catalog', action='append', type=Path)
    parser.add_argument('--exclusions', type=Path, help='Reuse saved identity exclusions for exact reconstruction')
    parser.add_argument('--chemical-preparation', action='store_true',
                        help='Apply fixed MolScrub single-state admission before the deterministic scaffold split')
    args = parser.parse_args()
    args.prior_run_root = args.prior_run_root or [ROOT / 'runs', PRIOR / 'runs']
    args.exclude_catalog = args.exclude_catalog or [ROOT / 'runs' / name / 'data/candidates.json' for name in (
        'local_cdk2_120_20261105_1733', 'cdk2_screen_120_30min_20261105_1745',
        'cdk2_reference_control_20261105_1745', 'cdk2_reference_control_20261105_1756')]
    build(args)
