# Lab harness v1 validation

## Current live study: 2026-11-05 r3

`agentic_cdk2_live_20261105_r3` completed a real Owner/Inspector → development Lab → Meta → two paired validation cohorts → selected-policy development Lab cycle. It used Codex model calls and standalone AutoDock Vina 1.2.7; its decisions were not scripted replay. Download locations and archive handling are described in [publication and reproduction](publication-and-reproduction.md).

| Measure | Archived result |
| --- | --- |
| Completed Lab campaigns | 6: two development and four validation arms |
| Model calls in the r3 study | 22, including intake and Meta |
| Vina seed jobs | 92, representing 46 campaign-specific compound evaluations |
| Distinct docked compounds across campaigns | 35 |
| Validation shortlist coverage | 5/5 compounds qualified in each of the four arms |
| Parent validation Lab input+output tokens | 390,983 |
| Candidate validation Lab input+output tokens | 268,635 |
| Paired validation token reduction | 31.2924% |
| Mean paired docking-energy gain, parent minus candidate | +0.07045 kcal/mol |
| Gate outcome | Cost path passed; quality path did not pass; candidate adopted for development_02 |

The candidate used the lossless deduplicated input view. The two-pair result supports this development adoption only. Energy is a conditional mean over compounds whose two selected poses qualify; both arms' coverage must be read alongside it. The 31.3% reduction excludes intake, Meta proposal cost and earlier attempts, which remain separately recorded. It is not total-study dollar savings or a statistically established general improvement. Three parent/H0 campaigns exceeded the 150,000-token **soft** limit; observed use remains in the archive rather than being truncated to the nominal allowance.

The study used four disjoint 20-compound cohorts and selected five compounds per campaign. It did not freeze a formal energy threshold or assess the ≥100-compound formal-list requirement. Computational docking scores and valid poses do not establish biological activity.

Evidence is in the r3 archive's `study_state.json`, `result_summary.json`, `gate_01.json`, `campaigns/*/evaluation.json`, raw batches and model receipts. `previous_model_overhead.json` and `previous_attempts.json` retain earlier-attempt context. Archive manifests bind original bytes; do not rewrite original machine paths in place.

## Software validation before the live study

The archived `intake_artifacts/test_results.txt` records **183 tests passed in 8.942 seconds** in the scientific Python environment before r3. This count describes that checkpoint, not an automatically updated count for later publication/export tooling.

```sh
python3 -m unittest discover -s tests -v
python3 -m lab_harness demo --out runs/new_replay_demo --with-meta
```

Use a new demo directory. The replay is synthetic, reports `demo_only`, and does not call a real model or Vina. Tests cover budgets, immutable inputs, stale decisions, model accounting, raw-result reconciliation, worker failures, source references, preparation admission, parent identity, prospective pairing, gate arithmetic, checkpoint guards and live/replay separation. Chemistry-dependent tests require the documented scientific environment; check skipped tests when running with a minimal Python installation.

## Historical checkpoints

These records predate the current live study and describe the implementation at that time. Earlier archives retain their original source hashes and evaluator; they must not be relabelled as current Vina evidence.

| Checkpoint | Recorded validation | Current interpretation |
| --- | --- | --- |
| Initial Lab/Meta replay | 62 tests; synthetic development plus two validation pairs | Software-flow check only |
| Supplied-score minimum-count guard | 74 tests; 99 rejected and 101 retained at synthetic cutoffs | Historical score-table interface; current CLI requires live docking archives |
| Historical GNINA energy evaluator | 75 tests; archive/CLI count boundaries | GNINA archives are rejected by the current Vina formal CLI |
| Standalone Vina worker and replay | 81 tests; one real two-seed compound check | Numerical engine check, followed later by the r3 study |

The historical Vina worker check for CHEMBL194721 produced affinities −9.925 and −9.908 kcal/mol, both with valid identity-preserving poses, and a qualified median of −9.9165 kcal/mol. It was one compound, not a comparative agent result. The earlier GNINA re-analysis found 14 qualifying structures among 16 docked compounds at an uncalibrated example cutoff; it is not part of the current Vina study or a formal ≥100 list.

The active [`select-final` contract](lab-harness-v1.md) audits live standalone Vina archives, uses both fixed seeds and the frozen preparation registry, and counts unique parents. It rejects supplied-score tables, historical GNINA evidence and synthetic replay. Dataset release manifests specify which historical artifacts are included; historical checkpoint paths mentioned here are not a promise that every local development archive is distributed.
