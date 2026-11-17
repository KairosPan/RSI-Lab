# Executable method selection

`pablo.select_candidates` version `1.0` is a PABLO host capability. It computes
candidate selection from the complete authorized observation history, a bounded
candidate pool, and cited literature publications. It does not call the hidden
scorer, retrieve new literature, execute model-written code, or consume score
slots. The generic RSI-Lab kernel knows only its ordinary Adapter contract.

The implemented methods are reviewed method primitives, **not reproductions of
arbitrary papers returned by literature search**. A cited abstract motivates a
method choice; it does not certify transfer, scientific validity, or performance.
The result records this distinction and the exact source hashes.

## Algorithms

Both methods use RDKit Morgan bit vectors with radius 2, 2,048 bits, and chirality
disabled. Canonical molecular identity still includes stereochemistry. These
are separate choices: distinct molecular identities can have identical method
representations. The RDKit version is frozen in the packet and verified before
execution.

- `fingerprint_diversity`: greedily maximize the minimum Tanimoto distance to
  all observed molecules and all candidates already selected in this batch.
- `knn_ucb_diversity`: estimate a candidate's score using the similarity-weighted
  mean of its `neighbors` nearest observed molecules. Weights are
  `max(similarity, 1e-6)`. The uncertainty proxy is the weighted standard
  deviation of their observed scores plus distance to the nearest observed
  molecule. Greedy acquisition is `mean + exploration_weight * uncertainty +
  diversity_weight * minimum_distance`. The distance term includes both all
  observed molecules and the current batch's selected candidates.

The uncertainty term is an **uncalibrated proxy**, not a confidence interval or
a fitted Gaussian-process posterior. No claim of implementing a particular
Bayesian-optimization paper is made. Candidate score labels come exclusively
from the already observed dataset; selected candidates have no new true score.
Ties resolve by canonical SMILES, independent of submitted pool order.

## Inputs

```json
{
  "run_root": "/absolute/frozen/run",
  "run_id": "current-run",
  "proposal_role": "local",
  "anchor_parent": "previously observed canonical SMILES",
  "candidates": ["candidate SMILES", "another candidate SMILES"],
  "select_count": 2,
  "method": "knn_ucb_diversity",
  "parameters": {
    "neighbors": 8,
    "exploration_weight": 0.2,
    "diversity_weight": 0.1
  },
  "literature_evidence_ids": ["actual-literature-fact-id"]
}
```

This is a field-shape illustration; placeholder SMILES are intentionally not
executable. All fields are required; unknown fields are rejected. Each request
uses one `local` or `challenge` role. If `pablo_harness` is enabled, its current
role, eligible anchor, state hash and per-batch allowance are frozen and checked.
Method selection and lookup do not advance Harness turns.

Bounds are 1–256 pool entries, 1–20 selected candidates, 1–32 neighbors, and
finite acquisition weights in [0, 2]. The Harness may impose a lower batch cap.
The complete training history must contain at most 10,000 observations; a larger
history is rejected rather than silently truncated. Invalid molecules are
rejected; canonical aliases in a pool collapse deterministically. Previously
observed and queued/prepared/dispatching/unknown scoring identities are excluded.
Candidates incompatible with the Harness role are also excluded and their
rejection reason is recorded: local candidates must meet the frozen anchor
similarity threshold; challenge candidates must leave the incumbent series.
An insufficient eligible pool is rejected before model-selected substitutions
can silently change the plan.

Literature grounding requires 1–4 genuine applicable `literature.search`
source-statement facts, including at least one available abstract per fact.
Trusted continuation imports retain the original source provenance. Empty or
metadata-only results do not ground execution. Source text is never interpreted
as executable instructions. The fixed method-to-topic catalog is in `methods.py`.
The adapter's `required_evidence` hook asks the generic kernel to require these
original facts in the actual model delivery and explicit command citations.

## Execution and scoring handoff

Preparation freezes the full observed data, canonical pool, parameters,
literature hashes, Harness state and implementation identity. Execution verifies
that this packet is the persisted dispatching Effect and that its sources and
research state are unchanged, then performs the numerical selection.

The published raw computation includes:

- `plan_id`, computed from the complete deterministic result;
- `selected_candidates`, `selection_rows`, and per-candidate predictions;
- the training, trajectory, pool, implementation and literature source hashes;
- the role, anchor, and current Harness binding;
- an explicit `new_scores: 0` and method limitations.

The PABLO scoring host must call `validate_evaluation_plan` when admitting a
scoring request. It checks the settled selection Effect and receipt, exact plan
ID, complete canonical selected set, role, anchor, trajectory and current Harness
state, including a completed publication. Candidate replacement, omission, duplicate insertion, stale plans and
invented plan IDs fail. Ordinary scoring identity claims then cover the gap
between admission and execution. Subsequent score receipts, not prediction rows,
measure whether this method helped.

The adapter supplies source-pinned exact-input recommendation normalization,
separate actual selection coverage verification, decision dependencies and
semantic progress. Repeating the same method/query/state does not gain a new
semantic identity merely from a different Effect ID. The kernel does not acquire
PABLO-specific candidate fields or algorithm assumptions.

## Offline validation

```sh
PYTHONPATH=experiments/pablo:. python -m unittest experiments.pablo.tests.test_methods -v
```

Tests use synthetic observed labels and block `oracle.score`. They exercise
actual numerical changes from different training labels, greedy diversity,
deterministic aliases, pending Work before Effect creation, exact score-plan
handoff, stale state, forged dispatch, role/anchor enforcement, imported source
provenance and tampered completion claims. No experiment is restarted.
