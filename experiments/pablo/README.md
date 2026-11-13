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

New preparations use `pablo_research_pr4_feedback_v3`: the original Lab
local/challenge policy, a score-plateau adjustment, numerical literature-method
selection, and mandatory interpretation before the next scoring decision.
[FEEDBACK_V3.md](FEEDBACK_V3.md) separates kernel changes from host/domain changes,
describes the exact execution path and records the offline verification scope.
This code revision has **not** restarted the experiment.

`pablo_task/oracle.py` is byte-identical to the earlier PABLO implementation;
[`SOURCE_ORIGIN.json`](pablo_task/SOURCE_ORIGIN.json) records its origin and hashes.
Neither its hidden objective nor its formula belongs in model prompts or
literature queries. Literature searches use bounded public method topics.

## Run a continuation

Run from the repository root with the existing PABLO Python environment
(`guacamol`, `rdkit`, `numpy`, and `scipy`) and an authenticated Codex executable.
The CLI accepts a completed legacy PR #2 parent, or a settled PR #4 research
parent at a completed or explicitly paused boundary. Supply that parent's exact
frozen source tree. Legacy exports use the archived
`run_sources/pr2-pablo/scripts/continue_pablo_pr2.py`; research exports validate
the parent's ledger, boundary artifact, receipts, and scientific-memory bundle.
This is a continuation entrypoint, not an original-input restart. The model and
reasoning effort are inherited from the parent config;
`--effort high` overrides reasoning effort for a fresh run. `--max-input-bytes
524288` allows the complete research request to fit after recommendations add
required evidence. These settings cannot override an existing frozen run on resume.
`--prior-attempt-root` retains verified costs from a stopped attempt with the same
inherited observations, zero new scores, and no unsettled effects.

The 2026-11-12 integration smoke was separate from the formal 400-score campaign.
For a new small integration check, replace these paths with absolute locations
and use a new directory and ID:

```sh
python experiments/pablo/run.py \
  --root /absolute/path/to/runs/pablo_research_pr4_smoke_20261112 \
  --run-id pablo-research-pr4-smoke \
  --parent-root /absolute/path/to/runs/pablo_1000_sol_xhigh_pr2_20261112 \
  --parent-run-id pablo-1000-sol-xhigh-pr2 \
  --parent-source-root /absolute/path/to/the/frozen/pr2/checkout \
  --new-evaluations 10 --max-model-calls 60 --literature-max-calls 3 \
  --effort high --integration-smoke --prepare-only
```

The new run directory must not exist. `--prepare-only` imports state and writes
the preflight reports without a model call or historical rescoring. Start or
resume that prepared run using its original directory and ID:

```sh
python experiments/pablo/run.py \
  --root /absolute/path/to/runs/pablo_research_pr4_smoke_20261112 \
  --run-id pablo-research-pr4-smoke --resume
```

For a new formal 400-score campaign, use a separate root and run ID, select the
authorized historical parent, set `--new-evaluations 400`, and omit
`--integration-smoke`. Subsequent research segments retain the original campaign
baseline and target. `--new-evaluations` is the additional score quota for the
new segment: the completed campaign used 87 + 113 + 200, ending at 400.
The initial 1,300 observations are outside that quota. Scores
are valid novel finite observations, not model turns or tool calls.

The final 200-score segment completed with `gpt-6.1-sol / high`, a frozen
607-model-call allowance, and a separate 200-score cap. Its exact preparation
and resume commands, parent identities, and audit references are in the local
artifact `reports/pr4-research-verification/checkpoint400-run-plan.md`.
The local artifact `reports/pr4-research-verification/analysis400-conclusions.md`
contains the results and limitations; campaign artifacts are not shipped with
the source tree.
Those runs retain their frozen v2 execution sources; v3 requires a new segment.

## Carry and explicitly extend resources

Within an existing research campaign, preparation subtracts the parent's actual
spend from its resource limits before applying an explicitly requested extension.
Outstanding reservations must be settled. A completed legacy parent used to
start an independent campaign retains the original full policy baseline; the
provenance distinguishes `new_legacy_campaign` from `carry_research_campaign`.
Historical costs remain recorded in both cases.

| Option | Default | Accepted range |
| --- | ---: | ---: |
| `--additional-model-calls` | 0 | 0–1,000 |
| `--additional-tokens` | 0 | 0–1,000,000,000 |
| `--additional-tool-calls` | 0 | 0–10,000 |
| `--final-review-max-calls` | 24 | 1–128 |

`--max-model-calls` selects this segment's model limit (default 600, range
1–1,000); it cannot exceed the available baseline plus the explicit model
extension. Choose a lower value when the parent has less remaining. Final-review
calls consume this same model budget. `--literature-max-calls` defaults to 8;
literature execution also consumes the total tool-call budget.

For the current final segment, the authorized additions are 200 model calls,
50,000,000 tokens, and 500 tool calls. Adding them to the parent's remaining
407 calls, 51,620,107 tokens, and 900 tool calls gives 607, 101,620,107, and 1,400,
respectively. Final review is bounded to 64 calls. This preserves past spending;
it does not reset the original allowance. A soft-resource overrun consumes part
of an extension instead of being forgiven.

The identical `resource_budget_provenance` object is frozen in `run-config.json`,
`lineage.json`, and `manifest.json`. It records the allocation mode, parent limit
and spend, remaining balance or overrun, explicit additions, actual segment
limits, and unused available allowance. `task-plan.json` uses those actual
resource limits. New allowances and final-review settings require a new segment;
`--resume` rejects explicit overrides, including an explicit zero addition.

## Required checks and evidence

Both initial preparation and resume call `Runtime.preflight` for scientific
memory, Meta→Lab feedback, orchestration, the identity provider, research policy,
all configured capabilities (four in v3, three in older configurations), and
the evaluation/lookup measurement callbacks. The
host also validates the literature flag, bound run root, and call budget.
Every ready Lab/Meta request receives a second preflight that checks the actual
packet's research blocks and capabilities. It also checks the source-pinned
normalization, publication-coverage, decision-dependency, and semantic-progress
hooks on every adapter. V3 additionally requires the Harness, scoring
interpretation barrier, and source-pinned `required_evidence` hooks for numerical
selection and scoring. The actual request bytes must carry the complete
dependency manifest. When recommendations are available, the schema must require
`execution_contract_json`; when unavailable, it must omit `recommend_change`.
Native-session snapshots and the combined observable history/request size are
checked without starting a model turn. Final-review and batch-interpretation
packets intentionally offer no tools while retaining their research requirements.

V3 scoring requires a completed `pablo.select_candidates` plan, its exact selected
candidate set, role and anchor. Selection uses already observed scores and
applicable cited literature with abstracts; it never scores the candidate pool.
See [METHODS.md](pablo_task/METHODS.md) for the two implemented algorithms and
[HARNESS.md](pablo_task/HARNESS.md) for original policy provenance and new bounds.

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
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=experiments/pablo:. \
  python -m unittest discover -s experiments/pablo/tests -t .
```

Host tests make no model, network, or oracle calls; selected domain tests use
the real local scorer.
The new whole-loop tests use fixture model responses, explicit synthetic
literature and synthetic score labels. They verify execution and provenance,
not an improvement in optimization performance. The full kernel suite is run
separately with `python3 -m unittest discover -s tests`.
