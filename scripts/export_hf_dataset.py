#!/usr/bin/env python3
"""Export completed local studies to a private, checksummed HF data bundle.

Reads original files without modifying them. It performs no model calls,
chemistry, docking, network requests, or upload. Audit archives retain private
machine paths and exact source bytes; portable tables use archive member paths.
"""
from __future__ import annotations

import argparse
import collections
import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import statistics
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SUBSETS = {'agentic_pilot': 'agentic_cdk2_live_20261105_r3',
           'legacy_screen_120': 'cdk2_screen_120_30min_20261105_1745',
           'reference_control': 'cdk2_reference_control_20261105_1756'}
JOB_FILES = ('ligand.sdf', 'ligand.pdbqt', 'poses.pdbqt', 'top.sdf', 'posebusters.csv', 'result.json')
SECRETS = {
    'credential_like': re.compile(rb'\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{24,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b'),
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'authorization_bearer': re.compile(rb'(?i)authorization[\s"\x27:=]+bearer\s+[A-Za-z0-9._~-]{16,}'),
    'credential_json': re.compile(rb'(?i)"(?:api_key|access_token|refresh_token|password|secret_key)"\s*:\s*"[^"]{8,}"'),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def checked_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Only ordinary source files may be exported: {path.name}')
    data = path.read_bytes()
    for name, pattern in SECRETS.items():
        if pattern.search(data):
            # Never echo a matched credential or file contents.
            raise ValueError(f'Credential scan blocked {path.name}: {name}')
    return data


def safe_member(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or '\\' in name:
        raise ValueError('Unsafe archive member path')
    return path.as_posix()


def tree(root, prefix):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f'Missing ordinary source directory: {root.name}')
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Symlink not allowed in publication sources: {path.name}')
        if path.is_file():
            rows.append((safe_member(f'{prefix}/{path.relative_to(root).as_posix()}'), path))
    return rows


def archive(destination, members):
    """Create deterministic archive with a complete per-member byte hash index."""
    names, index = set(), []
    with Path(destination).open('xb') as stream, gzip.GzipFile(fileobj=stream, filename='', mode='wb', mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w') as tar:
            for name, path in sorted(members):
                name = safe_member(name)
                if name in names or name == 'EXPORT_MEMBER_HASHES.json':
                    raise ValueError('Duplicate or reserved archive member')
                names.add(name)
                data = checked_bytes(path)
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                tar.addfile(info, io.BytesIO(data))
                index.append({'path': name, 'sha256': digest(data), 'bytes': len(data)})
            data = json_bytes({'schema_version': 'lab_export_member_hashes_v1',
                               'source_bytes_preserved': True, 'members': index})
            info = tarfile.TarInfo('EXPORT_MEMBER_HASHES.json')
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    return {'member_count': len(index), 'uncompressed_source_bytes': sum(r['bytes'] for r in index)}


def jsonl(path, rows):
    with Path(path).open('x') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True) + '\n')


def aggregate(jobs):
    groups = collections.defaultdict(list)
    for job in jobs:
        groups[(job['subset'], job['campaign'], job['candidate_id'])].append(job)
    rows = []
    for (subset, campaign, cid), seeds in sorted(groups.items()):
        seeds.sort(key=lambda row: row['seed'])
        if [row['seed'] for row in seeds] != [17, 43]:
            raise ValueError(f'Incomplete or duplicated seed pair: {subset}/{campaign}/{cid}')
        values = [row['vina_affinity_kcal_mol'] for row in seeds]
        complete_scores = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)
        qualified = complete_scores and all(row['status'] == 'completed' and row['pose_valid'] is True
                                          and row['identity_preserved'] is True and row['selected_model'] == 1 for row in seeds)
        rows.append({'subset': subset, 'campaign': campaign, 'cohort': seeds[0]['cohort'],
                     'study_phase': seeds[0]['study_phase'], 'candidate_id': cid,
                     'canonical_parent_identity': seeds[0]['canonical_parent_identity'],
                     'prepared_smiles': seeds[0]['prepared_smiles'], 'prepared_state_id': seeds[0]['prepared_state_id'],
                     'seed_17_energy': values[0], 'seed_43_energy': values[1],
                     'two_seed_valid': qualified,
                     'raw_median_vina_affinity': statistics.median(values) if complete_scores else None,
                     'qualified_median_vina_affinity': statistics.median(values) if qualified else None,
                     'seed_range_vina_affinity': max(values) - min(values) if complete_scores else None,
                     'selected_for_shortlist': seeds[0]['selected_for_shortlist'],
                     'official_threshold': None, 'formal_qualification_assessed': False})
    return rows


def build_tables(roots):
    compounds, jobs, campaigns = [], [], []
    for subset, root in roots.items():
        if subset == 'agentic_pilot':
            data_dirs = sorted(p for p in (root / 'data').iterdir() if (p / 'candidates.json').is_file())
            campaign_dirs = sorted(p for p in (root / 'campaigns').iterdir() if (p / 'evaluation.json').is_file())
        else:
            data_dirs, campaign_dirs = [root / 'data'], [root]
        catalog = {}
        for folder in data_dirs:
            candidates = read(folder / 'candidates.json')['candidates']
            for row in candidates:
                cid = row['candidate_id']
                if cid in catalog:
                    raise ValueError(f'Duplicate candidate across cohorts: {cid}')
                entry = {'subset': subset, 'cohort': folder.name if subset == 'agentic_pilot' else None,
                         'candidate_id': cid, 'source_smiles': row.get('source_smiles', row['canonical_smiles']),
                         'canonical_parent_identity': row.get('canonical_parent_identity', row['canonical_smiles']),
                         'prepared_smiles': row.get('prepared_smiles', row['canonical_smiles']),
                         'prepared_state_id': row.get('prepared_state_id'),
                         'preparation_profile': 'molscrub_0.3.2_pH_7.5_8.0_single_retained_state' if subset == 'agentic_pilot' else 'legacy_as_supplied_state',
                         'target_chembl_id': 'CHEMBL301', 'construct_id': '1DI8_chain_A',
                         'source_candidate': row, 'source_candidates_sha256': digest(checked_bytes(folder / 'candidates.json'))}
                catalog[cid] = entry
                compounds.append(entry)
        for folder in campaign_dirs:
            is_agent = subset == 'agentic_pilot'
            report_path = folder / ('evaluation.json' if is_agent else 'report_final/report.json')
            report = read(report_path)
            campaign = folder.name if is_agent else subset
            phase = report.get('phase', 'reference_control' if subset == 'reference_control' else 'exploratory_screen')
            shortlist = set(report.get('shortlist', []))
            campaigns.append({'subset': subset, 'campaign': campaign, 'source_report_sha256': digest(checked_bytes(report_path)),
                              'report': report})
            result_paths = sorted(folder.glob('batches/*/jobs/*/result.json')) if is_agent else sorted((folder / 'jobs').glob('*/result.json'))
            for path in result_paths:
                result = read(path)
                cid = result['candidate_id']
                entry = catalog[cid]
                member_dir = f'{subset}/' + path.parent.relative_to(root).as_posix()
                jobs.append({'subset': subset, 'campaign': campaign, 'cohort': entry['cohort'], 'study_phase': phase,
                             'candidate_id': cid, 'canonical_parent_identity': entry['canonical_parent_identity'],
                             'prepared_smiles': entry['prepared_smiles'], 'prepared_state_id': entry['prepared_state_id'],
                             'seed': result['seed'], 'status': result['status'], 'vina_affinity_kcal_mol': result.get('scores', {}).get('vina_affinity'),
                             'pose_valid': result.get('pose_valid'), 'identity_preserved': result.get('identity_preserved'),
                             'selected_model': result.get('selected_model'), 'pose_count': result.get('pose_count'),
                             'selected_for_shortlist': cid in shortlist if is_agent else None,
                             'engine': result.get('engine'), 'vina_sha256': result.get('vina_sha256'),
                             'pose_checks': result.get('pose_checks'), 'error': result.get('error'),
                             'source_result_sha256': digest(checked_bytes(path)),
                             'asset_archive': f'{subset}_inputs_poses.tar.gz',
                             'assets': {name: f'{member_dir}/{name}' for name in JOB_FILES if (path.parent / name).is_file()}})
    seen = {(job['subset'], job['candidate_id']) for job in jobs}
    for row in compounds:
        row['has_observed_docking_results'] = (row['subset'], row['candidate_id']) in seen
    return compounds, jobs, campaigns


def scientific_members(subset, root):
    members = tree(root / 'data', f'{subset}/data')
    if subset == 'agentic_pilot':
        members += tree(root / 'protocols', f'{subset}/protocols')
        for name in ('study_plan.json', 'gate_01.json', 'reference_state_check.json', 'result_summary.json', 'task_completion.json'):
            members.append((f'{subset}/workflow/{name}', root / name))
        source = Path(read(root / 'data/development_01/source_manifest.json')['original_source_path'])
        members += [(f'{subset}/upstream/chembl_37_activity_1000.json', source),
                    (f'{subset}/upstream/chembl_37_activity_1000.meta.json', source.with_suffix('.meta.json'))]
        for folder in sorted((root / 'campaigns').iterdir()):
            if not (folder / 'evaluation.json').is_file():
                continue
            for name in ('evaluation.json', 'locked_shortlist.json', 'locked_shortlist_detail.json'):
                members.append((f'{subset}/campaigns/{folder.name}/{name}', folder / name))
            for batch in sorted((folder / 'batches').iterdir()):
                if batch.is_dir():
                    for name in ('request.json', 'protocol.json', 'batch_summary.json', 'engine.json', 'artifact_hashes.json'):
                        members.append((f'{subset}/' + (batch / name).relative_to(root).as_posix(), batch / name))
        result_paths = root.glob('campaigns/*/batches/*/jobs/*/result.json')
    else:
        members.append((f'{subset}/protocol.json', root / 'protocol.json'))
        members += tree(root / 'report_final', f'{subset}/report_final')
        for name in ('reference_rmsd.json', 'plan.json', 'selection_audit.json', 'readiness.json'):
            if (root / name).is_file():
                members.append((f'{subset}/{name}', root / name))
        result_paths = root.glob('jobs/*/result.json')
    for result in sorted(result_paths):
        for name in JOB_FILES:
            path = result.parent / name
            if path.is_file():
                members.append((f'{subset}/' + path.relative_to(root).as_posix(), path))
    return members


def previous_members(agentic):
    audit = read(agentic / 'previous_model_overhead.json')
    members = [(f'reconciliation/{name}', agentic / name) for name in ('previous_attempts.json', 'previous_model_overhead.json')]
    for row in audit['calls']:
        study, directory = Path(row['study']), Path(row['call_directory'])
        if not directory.is_relative_to(study):
            raise ValueError('Prior call directory escaped its registered study')
        members += tree(directory, f'{study.name}/{directory.relative_to(study).as_posix()}')
    for row in audit['study_contexts']:
        study = Path(row['study'])
        for name in row['outcomes']:
            members.append((f'{study.name}/{safe_member(name)}', study / name))
    return members


def finalize_manifest(out, manifest, code_ref=None):
    manifest['code_ref'] = code_ref
    manifest['files'] = [{'path': p.name, 'bytes': p.stat().st_size, 'sha256': digest(checked_bytes(p))}
                         for p in sorted(out.iterdir()) if p.is_file() and p.name not in ('dataset_manifest.json', 'SHA256SUMS')]
    (out / 'dataset_manifest.json').write_bytes(json_bytes(manifest))
    (out / 'SHA256SUMS').write_text(''.join(f'{digest(checked_bytes(p))}  {p.name}\n' for p in sorted(out.iterdir())
                                         if p.is_file() and p.name != 'SHA256SUMS'))


def verify_bundle(out):
    out = Path(out)
    expected = {}
    for line in (out / 'SHA256SUMS').read_text().splitlines():
        value, name = line.split('  ', 1)
        if PurePosixPath(safe_member(name)).name != name or name in expected:
            raise ValueError('Invalid checksum filename')
        expected[name] = value
    if set(expected) != {p.name for p in out.iterdir() if p.is_file() and p.name != 'SHA256SUMS'}:
        raise ValueError('Checksum coverage mismatch')
    for name, value in expected.items():
        if digest(checked_bytes(out / name)) != value:
            raise ValueError(f'Bundle checksum mismatch: {name}')
        if name.endswith('.tar.gz'):
            with tarfile.open(out / name, 'r:gz') as tar:
                members = tar.getmembers()
                if len({m.name for m in members}) != len(members) or any(not m.isfile() for m in members):
                    raise ValueError('Non-file or duplicate archive member')
                for member in members:
                    safe_member(member.name)
                index = json.load(tar.extractfile('EXPORT_MEMBER_HASHES.json'))['members']
                if {m.name for m in members} != {r['path'] for r in index} | {'EXPORT_MEMBER_HASHES.json'}:
                    raise ValueError('Archive index coverage mismatch')
                for row in index:
                    data = tar.extractfile(row['path']).read()
                    if digest(data) != row['sha256'] or len(data) != row['bytes']:
                        raise ValueError('Archive member checksum mismatch')
    for row in read(out / 'dataset_manifest.json')['files']:
        if expected.get(row['path']) != row['sha256'] or (out / row['path']).stat().st_size != row['bytes']:
            raise ValueError('Manifest checksum mismatch')
    return {'files': len(expected) + 1, 'bytes': sum(p.stat().st_size for p in out.iterdir() if p.is_file())}


def build(args):
    out = args.out.resolve()
    if out.exists():
        raise ValueError('Export destination must not exist; originals and prior exports are immutable')
    roots = {key: args.runs_root / name for key, name in SUBSETS.items()}
    if read(roots['agentic_pilot'] / 'study_state.json')['status'] != 'completed':
        raise ValueError('Only a completed study may be exported')
    out.mkdir(parents=True)
    compounds, jobs, campaigns = build_tables(roots)
    results = aggregate(jobs)
    jsonl(out / 'compounds.jsonl', compounds)
    jsonl(out / 'seed_jobs.jsonl', jobs)
    jsonl(out / 'campaigns.jsonl', campaigns)
    audit = read(roots['agentic_pilot'] / 'data/chemical_preparation_audit.json')
    jsonl(out / 'preparation_decisions.jsonl', audit['records'])
    with (out / 'compound_results.csv').open('x', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(results[0])); writer.writeheader(); writer.writerows(results)
    archives = {}
    for subset, root in roots.items():
        archives[f'{subset}_inputs_poses.tar.gz'] = archive(out / f'{subset}_inputs_poses.tar.gz', scientific_members(subset, root))
        audit_name = {'agentic_pilot': 'audit_agentic_r3', 'legacy_screen_120': 'audit_legacy_screen_120', 'reference_control': 'audit_reference_control'}[subset]
        archives[audit_name + '.tar.gz'] = archive(out / (audit_name + '.tar.gz'), tree(root, root.name))
    archives['audit_previous_model_attempts.tar.gz'] = archive(out / 'audit_previous_model_attempts.tar.gz', previous_members(roots['agentic_pilot']))
    write_docs(out, roots)
    manifest = {'schema_version': 'rsi_lab_hf_private_export_v1', 'dataset_repo': 'Kairoszhe/RSI-Lab-docking',
                'visibility': 'private', 'snapshot_date': '2026-11-05', 'exporter_sha256': digest(Path(__file__).read_bytes()),
                'code_ref': None, 'source_archives_modified': False, 'raw_audit_contains_machine_paths': True,
                'credential_scan': 'Pattern scan passed for every original exported file; not a guarantee against all possible secret formats',
                'subsets': SUBSETS, 'row_counts': {'compounds': len(compounds), 'seed_jobs': len(jobs),
                                                 'compound_results': len(results), 'campaigns': len(campaigns),
                                                 'preparation_decisions': len(audit['records'])},
                'archives': archives, 'license': 'other; see LICENSES_AND_SOURCES.md',
                'source_identity': {'agentic_study_plan_sha256': digest(checked_bytes(roots['agentic_pilot'] / 'study_plan.json')),
                                    'chembl_activity_snapshot_sha256': 'c95094c5e31e5ca558f7beaa300c7b5f444e6aefe235ad3ac57cff3fc1b6f67c'}}
    finalize_manifest(out, manifest, args.code_ref)
    print(json.dumps({'output': str(out), **verify_bundle(out), 'row_counts': manifest['row_counts']}))


def write_docs(out, roots):
    (out / 'README.md').write_text(README)
    (out / 'SCHEMA.md').write_text(SCHEMA)
    license_file = ROOT / '.runtime/chemical-preparation/molscrub-0.3.2.dist-info/licenses/LICENSE'
    (out / 'LICENSES_AND_SOURCES.md').write_text(LICENSES + '\n\n## MolScrub upstream license (verbatim)\n\n```text\n' + checked_bytes(license_file).decode() + '\n```\n')


README = '''---
pretty_name: RSI-Lab CDK2 exploratory docking and agentic study
language: [en, zh]
license: other
license_name: component-specific
license_link: https://huggingface.co/datasets/Kairoszhe/RSI-Lab-docking/blob/main/LICENSES_AND_SOURCES.md
tags: [chemistry, molecular-docking, cdk2, tabular, 3d, agentic-evaluation]
configs:
- config_name: compounds
  data_files: compounds.jsonl
- config_name: seed_jobs
  data_files: seed_jobs.jsonl
- config_name: compound_results
  data_files: compound_results.csv
- config_name: campaigns
  data_files: campaigns.jsonl
- config_name: preparation_decisions
  data_files: preparation_decisions.jsonl
---

# RSI-Lab CDK2 exploratory docking

**Private research archive.** This repository includes original prompts, model
responses, execution receipts, and local machine paths. Do not change visibility
to public without a separate content/privacy review. No authentication tokens are
intentionally included; a pattern scan passed at export. Original files were not
modified. See `dataset_manifest.json`, `SHA256SUMS`, and each archive's
`EXPORT_MEMBER_HASHES.json` for integrity verification.

## Three distinct subsets

| Subset | Candidate inputs | Observed seed jobs | Scope |
|---|---:|---:|---|
| agentic_pilot | 80 across four disjoint cohorts | 92 across six campaigns; 35 distinct docked compounds | Real Owner/Inspector, Lab, Meta, two paired validation cohorts, then development |
| legacy_screen_120 | 120 | 240 | Earlier as-supplied chemical states; 118 compounds have two valid poses |
| reference_control | 1 native 1DI8 DTQ ligand | 2 | Descriptive redocking control; not part of compound ranking |

These subsets have different preparation histories. Do not merge them as one
uniform scientific protocol. Validation parent/candidate arms share their
prospectively paired cohort: repeated compound/seed observations across arms
are not independent new compounds. Unobserved candidate scores are missing,
not zero. All 334 job observations and invalid-pose outcomes are retained.

All docking uses standalone AutoDock Vina 1.2.7, seeds 17/43, native MODEL 1,
with lower affinity in kcal/mol being better. The qualified compound score is
the median of both seeds only when both completed, preserved chemical identity,
and passed the configured PoseBusters checks. No pose is replaced after checks.
There is no calibrated official score threshold and no formal >=100 selection
claim. The agentic study ends with five compounds, not a formal screening list.

The agentic Meta gate accepted its candidate through the **cost path**, with
31.2924% fewer validation Lab tokens and no pose-valid coverage regression.
The quality path did not pass. This tiny development gate is not evidence of
general efficacy, biological potency, or statistical superiority.

## Chemical and source limitations

Agentic inputs use MolScrub 0.3.2 rules at the exploratory pH interval 7.5-8.0,
two tautomer-reaction rounds, and KEEPMAX/KEEPMIN filtering. Only one unique
retained state with preserved heavy-atom/isotope identity and stereochemistry
is admitted. Of 350 source candidates, 268 were admitted, 52 were ambiguous,
and 30 failed state validation. A unique retained result is not measured
dominance, a population estimate, or exhaustive microstate coverage. Upstream
invalid reaction products may be skipped; acid/base sanitization can retain
input. The interval is not a verified common assay pH or a sensitivity study.

The unchanged pilot receptor has missing loops, removed waters and residues,
and default residue protonation; ligand preparation does not make the receptor
pH-calibrated. Reference-state equivalence and frame-preserving redocking RMSD
are descriptive controls, not a validation of ligand ranking.

Structures and assay provenance come from the first 1000 lexical-assay records
in a frozen ChEMBL_37 CDK2 query (CHEMBL301), retrieved 2026-10-20, not an
unbiased target-wide population. Historical identities and all returned prepared
states were excluded; the split is deterministic and scaffold-disjoint.
The separately archived raw source includes measured activity fields solely
for source reconstruction; those values were not exposed as Lab inputs or used
to choose/rank the cohorts. Do not present docking scores as IC50 labels.

## Contents and reproduction

The five root tables are directly loadable with Hugging Face Datasets or pandas.
`*_inputs_poses.tar.gz` packages contain candidate inputs, scientific protocols,
native poses, identity/PoseBusters results and source references. The agentic
package also contains the exact raw ChEMBL snapshot and retrieval metadata under
`agentic_pilot/upstream/`. See `SCHEMA.md` for archive paths and table joins.

`audit_*.tar.gz` packages preserve the original run bytes, including duplicate
frozen inputs and full model receipts. The prior-attempt archive contains seven
original model attempts needed for overhead reconciliation; six have verified
usage and one has unknown usage. Unknown cost is not zero. These archives are
audit evidence, not directly relocatable resumable studies: create a new study
directory to rerun. Do not overwrite or silently reuse original receipts.

The GitHub code revision is recorded in `dataset_manifest.json.code_ref` when
publication is finalized. A null value means that cross-reference is pending;
source hashes remain available. Download a specific HF revision and verify
`SHA256SUMS` before using the data. License terms and citations are separated
by component in `LICENSES_AND_SOURCES.md`; no new project-code license is granted.
'''

SCHEMA = '''# Data schema and joins

All JSONL lines are independent objects. CSV empty cells represent missing/null
values, not zero. All numerical docking affinities use kcal/mol, lower is better.

- `compounds.jsonl`: one input compound per subset/cohort (201 rows). Keys are
  `subset`, `cohort`, `candidate_id`; original and prepared identity fields are
  separate. `source_candidate` retains structure features and ChEMBL provenance.
  `has_observed_docking_results=false` means no experiment was run for that input.
- `seed_jobs.jsonl`: one actual seed invocation per campaign (334 rows). Join on
  subset/candidate ID, and retain campaign/seed to distinguish validation arms.
  `pose_valid`, `identity_preserved`, `selected_model`, `status`, and `pose_checks`
  determine eligibility. `asset_archive` + `assets` locate exact archived files.
- `compound_results.csv`: one seed pair per campaign and candidate (167 rows).
  The raw median may exist when pose validation fails; the qualified median is
  then null. Both seeds 17/43 must be present exactly once. `official_threshold`
  is null and `formal_qualification_assessed` is false throughout this export.
- `campaigns.jsonl`: original reports for six agentic campaigns and the two
  legacy/control runs, with their original file hashes (8 rows).
- `preparation_decisions.jsonl`: all 350 prospective ligand-state decisions,
  including rejected alternatives, diagnostics and original-parent mapping.

Scientific archive member roots are `agentic_pilot/`, `legacy_screen_120/`, and
`reference_control/`. Each has `data/` with original candidates and receptor.
The agentic root additionally contains:

```
data/{development_01,validation_01,validation_02,development_02}/
data/{source_registry,identity_exclusions,chemical_preparation_audit,split_manifest}.json
upstream/chembl_37_activity_1000.json
upstream/chembl_37_activity_1000.meta.json
protocols/<cohort>.json
workflow/{study_plan,gate_01,reference_state_check,result_summary,task_completion}.json
campaigns/<campaign>/{evaluation,locked_shortlist,locked_shortlist_detail}.json
campaigns/<campaign>/batches/<request>/{request,protocol,batch_summary,engine,artifact_hashes}.json
campaigns/<campaign>/batches/<request>/jobs/<candidate>_s<seed>/
```

Legacy/control scientific jobs live at `<subset>/jobs/<candidate>_s<seed>/`.
Each exported job retains `ligand.sdf`, `ligand.pdbqt`, `poses.pdbqt`, `top.sdf`,
`posebusters.csv`, and `result.json` when present. The result's original command
may contain private absolute paths; asset-table paths are portable. Native PDBQT
contains ranked Vina modes; top.sdf is the MODEL 1 pose used for validation.

Audit archives use the original run-directory basename as their member root.
The previous-attempt archive contains only registered original call directories,
their study/intake state/result files, and reconciliation summaries. It does not
copy unrelated historical experiments. Each archive has a complete uncompressed
member-byte SHA256 index at `EXPORT_MEMBER_HASHES.json`; it is export metadata,
not an original run artifact. SHA256SUMS covers every root file except itself.
The dataset manifest covers all payload files except itself and SHA256SUMS,
avoiding circular hashes. Rewriting code_ref requires refreshing SHA256SUMS.
'''

LICENSES = '''# Component licenses and sources

This private data release does not grant a new license to user-authored code,
reports, prompts, or the project as a whole. Those materials retain their
existing rights. HF's `license: other` describes the mixed-component bundle.

## ChEMBL-derived structures, assay provenance and source records

ChEMBL data is from https://www.ebi.ac.uk/chembl/; version **ChEMBL_37**.
The frozen source query was retrieved 2026-10-20. ChEMBL-derived data portions
remain under **Creative Commons Attribution-ShareAlike 3.0 Unported**; preserve
attribution and the applicable share-alike terms when redistributing adaptations.
Official terms: https://chembl.gitbook.io/chembl-interface-documentation/about
License: https://creativecommons.org/licenses/by-sa/3.0/
Attribution guidance: https://chembl.github.io/chembl-licensing/

Cite Zdrazil et al., *The ChEMBL Database in 2023*, Nucleic Acids Research
52(D1), D1180-D1192 (2024), https://doi.org/10.1093/nar/gkad1004.
The activity-query bytes have SHA256
`c95094c5e31e5ca558f7beaa300c7b5f444e6aefe235ad3ac57cff3fc1b6f67c`.
Calculated input descriptors in this study were recomputed with RDKit; they
are not extracted commercial ChEMBL property-calculation training targets.

## PDB coordinates and native reference ligand

The PDB archive coordinate data are **CC0 1.0**. RCSB API external-resource
annotations retain their original providers' restrictions. We preserve source
identifiers and preparation modifications rather than asserting a new license
over the original PDB entry. Official policy: https://www.rcsb.org/pages/policies
Structure: https://doi.org/10.2210/pdb1DI8/pdb
Original study: Shewchuk et al., J Med Chem 43:133-138 (2000),
https://doi.org/10.1021/jm990401t.

## MolScrub source snapshots

The original audit includes fixed MolScrub 0.3.2 source/rule snapshots under
`preparation_sources/molscrub/`. These files remain **GNU GPL version 3**;
the upstream license text is reproduced below. Source repository and release:
https://github.com/forlilab/molscrub and
https://github.com/forlilab/molscrub/releases/tag/0.3.2 .
Source/rule hashes and RDKit/runtime versions are in the preparation method.
This license notice must remain associated with redistributed source copies.
The project wrapper and other user-authored code are not relicensed by this text.

## Generated results and interpretation

Vina predictions, pose-validation results, model outputs, and aggregate reports
are computational research observations, not measured binding affinity, IC50,
clinical evidence, or a validated >=100-compound selection. Tool names/versions
are attribution and provenance. Redistribution of this mixed archive must retain
the applicable component terms; this document supplies no additional blanket
code license or warranty. See README.md for scientific scope and limitations.
'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs-root', type=Path, default=ROOT / 'runs')
    parser.add_argument('--out', type=Path, default=ROOT / 'runs/publication_20261105/hf_dataset')
    parser.add_argument('--code-ref')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--set-code-ref', help='Update only derivative export metadata/checksums, never original sources')
    args = parser.parse_args()
    if args.set_code_ref:
        verify_bundle(args.out)
        finalize_manifest(args.out, read(args.out / 'dataset_manifest.json'), args.set_code_ref)
        print(json.dumps(verify_bundle(args.out)))
    elif args.verify:
        print(json.dumps(verify_bundle(args.out)))
    else:
        build(args)
