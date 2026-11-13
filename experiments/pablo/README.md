# PABLO research experiment on PR #4

This directory is an experiment host for the generic RSI-Lab kernel. PR #4
provides feature and request preflight APIs; it does not register PABLO tools or
start an experiment by itself.

- `run.py` owns preparation, mandatory preflight, the model loop, and final review.
- `host_support.py` owns the task plan, progress reports, and session boundaries.
- `continuation.py` transfers verified lineage and role-owned private memory.
- [`pablo_task/`](pablo_task/README.md) supplies evaluation, complete identity
  lookup, literature retrieval, scientific identity projection, and measurements.
- `rsi_lab/` remains the generic kernel. Domain code and host choices live here.

`pablo_task/oracle.py` is byte-identical to the earlier PABLO implementation;
[`SOURCE_ORIGIN.json`](pablo_task/SOURCE_ORIGIN.json) records its origin and hashes.
Neither its hidden objective nor its formula belongs in model prompts or
literature queries. Literature searches use bounded public method topics.

## Run a continuation

Run from the repository root with the existing PABLO Python environment
(`guacamol`, `rdkit`, `numpy`, and `scipy`) and an authenticated Codex executable.
The current CLI requires a completed historical parent and its frozen source
tree, including `run_sources/pr2-pablo/scripts/continue_pablo_pr2.py`. It is a
continuation entrypoint, not an original-input restart or a general parent-run
importer. The model and reasoning effort are inherited from the parent config;
`--effort high` overrides reasoning effort for a fresh run. `--max-input-bytes
524288` allows the complete research request to fit after recommendations add
required evidence. These settings cannot override an existing frozen run on resume.
`--prior-attempt-root` retains verified costs from a stopped attempt with the same
inherited observations, zero new scores, and no unsettled effects.

The live smoke prepared on 2026-11-12 inherits 1,300 observations from the completed
PR #2 run and requests 10 additional scores, with `gpt-6.1-sol` and `xhigh`.
Equivalent preparation, with paths replaced by their absolute local locations:

```sh
python experiments/pablo/run.py \
  --root /absolute/path/to/runs/pablo_research_pr4_smoke_20261112 \
  --run-id pablo-research-pr4-smoke \
  --parent-root /absolute/path/to/runs/pablo_1000_sol_xhigh_pr2_20261112 \
  --parent-run-id pablo-1000-sol-xhigh-pr2 \
  --parent-source-root /absolute/path/to/the/frozen/pr2/checkout \
  --new-evaluations 10 --max-model-calls 60 --literature-max-calls 3 \
  --integration-smoke --prepare-only
```

The new run directory must not exist. `--prepare-only` imports state and writes
the preflight reports without a model call or historical rescoring. Start or
resume that prepared run using its original directory and ID:

```sh
python experiments/pablo/run.py \
  --root /absolute/path/to/runs/pablo_research_pr4_smoke_20261112 \
  --run-id pablo-research-pr4-smoke --resume
```

For a formal 400-score continuation, use a separate new root and run ID, select
the authorized historical parent, set `--new-evaluations 400`, and omit
`--integration-smoke`. Set the model-call and literature budgets explicitly;
`--max-model-calls 600 --literature-max-calls 8` are the current defaults. The 400
counts additional valid novel finite scores, not model turns, tool calls, or
inherited observations. Keep the smoke separate from the formal run's lineage
unless its observations are intentionally part of the selected starting state.

## Required checks and evidence

Both initial preparation and resume call `Runtime.preflight` for scientific
memory, Meta→Lab feedback, orchestration, the identity provider, research policy,
all three capabilities, and the evaluation/lookup measurement callbacks. The
host also validates the literature flag, bound run root, and call budget.
Every ready Lab/Meta request receives a second preflight that checks the actual
packet's research blocks and capabilities. Final-review packets intentionally
offer no tools while retaining their research requirements.

Preparation freezes execution-source hashes in `manifest.json`; resume checks
them before recovery or a model call. Tests and documentation are excluded.
Do not edit frozen Python execution sources while a prepared run is in use.
The parent export uses its own verified archived implementation in a separate
process. Historical ledgers remain read-only, imported private memories retain
their original role and unverified status, and imported scientific evidence
retains its source references. `lineage.json` and `cumulative-costs.json` keep
prior segments and attempts distinct from new work and costs.

Passing preflight proves wiring. A live smoke still needs receipt-backed evidence
of external literature results, scientific-memory updates and later use, and a
Meta recommendation followed by Lab's explicit response, its bound execution,
measurement, and Meta review. A decline or deferral is valid behavior but does
not exercise the adopted-execution path. The smoke contract requests these
checks; the host does not claim they happened merely because scoring finished.
Neither an executed recommendation nor an improved score establishes causal
benefit without a controlled comparison.

Queued scoring intents are now exposed to both roles through bounded operational
context. Lookup includes queued Work even before an execution Effect exists.
The routing transaction checks the complete queued identity set before creating
either ordinary or recommendation-bound scoring Work; overlaps are refused with
the existing Work IDs. This leaves the original task and its credit unchanged.
The scorer also rechecks novelty at execution. Operational Work IDs are not
scientific evidence IDs.

`--max-recommendations N` sets the lifetime creation allowance for a fresh run
(default 16, including subsequently declined recommendations).
`--max-pending-recommendations N` independently limits simultaneous unfinished
recommendations (default 1). Proposed, deferred, adopted, and executed-but-not-yet-
reviewed recommendations occupy pending slots. Review, decline, or final deferral
frees a pending slot without refunding the lifetime allowance. Either limit can
be zero to disable creation while preserving result reviews and other actions.
Meta receives both limits, their current counts, and blocking recommendation IDs/statuses.
Unavailable recommendations are removed from the actual response schema, and
creation is checked again transactionally. Rejected issue-less Meta requests
receive one bounded feedback opportunity. Existing frozen run limits cannot be
overridden on resume.

Use `preflight.json`, `progress.json`, and `calls/` for live inspection without
opening the active ledger for writing. At completion, inspect `result.json`,
`research-report.json`, `scientific-memory.json`, and authenticated receipts.
`attention.json` records blocked or failed execution. Completion requires the
scoring quota and explicit disposition of pending feedback, current scientific
interpretations, open issues, unfinished work, and unsettled effects; final
review has a bounded call allowance.

## Local verification

```sh
PYTHONPATH=experiments/pablo python -m unittest \
  experiments.pablo.tests.test_pablo_adapter \
  experiments.pablo.tests.test_pablo_lookup \
  experiments.pablo.tests.test_literature_adapter \
  experiments.pablo.tests.test_research_domain_boundary \
  experiments.pablo.tests.test_host_preflight
```

These 53 tests passed on PR #4 during integration. Host tests make no model,
network, or oracle calls; selected domain tests use the real local scorer.
They supplement the separate live smoke rather than certify its outcome.
