# PABLO local/challenge host policy

`harness.py` restores the earlier PABLO Lab search-role policy inside the current
durable runtime. It is a molecular-domain host policy; it is not a kernel
scheduler, a Meta raw-first/challenge review, or another model execution loop.

## Original behavior and deliberate extensions

The source checkout is
`/Users/pan/Documents/ChatGPT/RSI-PABLO/pablo-meta-experiment`:

| Source | SHA-256 | Behavior restored |
|---|---|---|
| `variants/search3/a/entrypoint.py`, lines 60–75 | `2bfba65a2d0cbed835a60938705239a2a149223bf2641449d3b130be027d55c0` | Serial three local batches followed by one challenge batch |
| `variants/continuity11/qualification/science_policy.py`, lines 95–156 and 220–240 | `e43de615f92e0b86e38914bb549f371303b407c2fa26538e422e464bc206374c` | Challenge also on odd turns after four non-improving batches; inspect fresh feedback before another proposal; role-specific structural hypotheses and actual accepted score credit |

The local role refines the leading observed series with interpretable edits,
negative comparisons and alternative explanations. The challenge role selects a
different strong observed structural anchor and tests a competing direction.
Both receive explicit role instructions, not just a label.

The following are explicit **new** host safeguards, not claims that the original
implementation enforced them:

- A frozen `stale_score_threshold` (default 40) uses the ordered initial and new
  observed scores. At this plateau, the same alternating local/challenge regime
  is enabled even if four batches have not yet elapsed. A strict best-score
  improvement resets both score and batch stagnation counters.
- Local anchors must belong to the current best's Murcko series and be at or
  above the observed 75th percentile. Challenge anchors must belong to a
  different observed Murcko series; strong anchors are preferred. If none pass
  the percentile, the strongest different series is an explicit fallback. If
  no different observed series exists, the context reports a blocked reason;
  it never silently calls another local proposal a challenge. Acyclic molecules
  use non-stereochemical connectivity as a fallback family key rather than
  collapsing every acyclic molecule into an empty Murcko scaffold.
- Actual selected candidates, not only anchors, must implement the role. By
  default every challenge candidate leaves the incumbent's series; every local
  candidate has Morgan radius-2/2048-bit Tanimoto similarity of at least 0.35 to
  its observed anchor. These bounds are frozen as
  `challenge_min_outside_incumbent_fraction` and `local_min_anchor_similarity`.
  They are new operational constraints, not original policy constants. Historical
  scoring replay checks them too, so a different anchor cannot launder an
  unchanged local candidate pool into challenge credit.
- The scorer has an explicit frozen per-batch bound (default 10, never above
  20), and the next plan caps it at remaining valid scoring slots. The original
  nominal 3/5/8 candidate progression is guidance within that bound. The old
  policy's call-pressure expansion to 48 candidates is not copied because it
  conflicts with this host's bounded scorer and independently accounted Meta
  calls.

## Frozen state, execution and credit

New runs freeze `pablo_harness = harness.DEFAULT_CONFIG`. Old frozen runs without
the key retain their old contract; no ledger migration or retrospective role
attribution occurs.

`context(store, run_id, progress)` replays only actual settled scoring receipts.
It returns the next role, turn, score/batch stagnation, allocation regime,
state/trajectory/policy hashes, eligible anchors, role-specific instructions,
per-role accepted-score credits and strategy transitions. Lookups, methods,
model calls and unknown scoring effects do not advance the scoring turn.

`validate_proposal(...)` binds the role and observed anchor to that exact state.
Candidate selection stores this binding in its packet and result. The scoring
adapter must then call `validate_binding(...)` at admission and preparation,
freeze `harness_binding` in the actual scoring packet, and bind scoring to the
complete selected candidate set. A completed score batch with an absent or
stale role binding cannot be replayed as a valid enabled-harness batch.

The generic kernel provides serialization, identity claims and the per-batch
interpretation barrier. The PABLO host provides role selection, molecular
anchors and score-plateau meaning. Their separation is intentional: the kernel
does not know SMILES, Murcko scaffolds, numeric score direction or local/challenge
allocation.

`publication_credit(...)` records only accepted finite rows from the exact
successful score receipt. A proposal containing ten candidates but accepting
three earns three scores, not ten. This is allocation and provenance accounting,
not evidence that the role caused improvement.

## Offline regression verification

`experiments/pablo/tests/test_harness.py` uses synthetic scores and no model,
network or evaluator. It covers original cadence, each independent stagnation
trigger, strict-improvement reset, inherited plateau state, pending/unknown
effects, explicit anchor fallback/blocking, stale role bindings, unique actual
credit and remaining score capacity.

`experiments/pablo/tests/test_harness_runtime.py` runs the complete real Runtime
path with fixture model replies, an explicitly synthetic literature fact, real
candidate-selection arithmetic and a scorer stub returning 0.6. It checks
selection → scoring → mandatory interpretation → next challenge decision,
actual source delivery/citation, exact selected-set enforcement, stale plans,
different challenge structures and per-role accepted-score credit. It never
invokes the hidden evaluator, a model provider or a literature network request.
