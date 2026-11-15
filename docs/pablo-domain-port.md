# Minimal PABLO domain port

This host-side port supports a **fresh** molecular optimization run with the current
RSI-Lab runtime. It does not resume another run or import its ledger, generated
candidates, scientific memory, advanced research host, or method framework.
The oracle implementation is private host code. Do not register this document,
the oracle source, its underlying benchmark identity, or implementation details as
agent-readable task sources. The agents receive observed structure–score pairs
and the public capability contracts.

## Source provenance

The four scoring/lookup files under `rsi_domains/pablo/` used for the original
validation were copied from RSI-Lab commit
`ed71c7bcdc7c61d9261849a0c6d71b74db03d4d6`. The copied scorer and oracle were also
byte-identical to the locally frozen PR2 sources. That validation did not enable
the separate research host or Scientific Memory. The PR also integrates the
subsequently merged `main` implementation; its research modules are retained.
The adapter and lookup manifests now additionally expose a closed JSON input
schema; their scoring, accounting, authorization, and replay algorithms are unchanged.

| Original file | SHA-256 at source commit |
| --- | --- |
| `__init__.py` | `f0fb293e7db3d3b3295af797d76cd66c4bf0a77c2372c7a611773d6a7ced9008` |
| `oracle.py` | `f5327b4e5aaa4cd48525617fbb09b729cecd332044f776cf401fe7fee766074e` |
| `adapter.py` | `3d7dbf178f009e535c911197403b7b4845f280027497dd32c636604004064a79` |
| `lookup.py` | `425ee8371330a2c5324833fed683c4d325370147dfb6395f46e6ab3af1089a8e` |

## Fresh initialization and host integration

Use only the first 100 rows of the original
`pablo-meta-experiment/upstream/data/guacamol_init_data.csv`, adjacent to this
checkout. Its complete-file SHA-256 is
`ef8b0dd3cda1c3b3f520e21a311d94f0ef324e9ab1bc3ab2385e3b4ee4e5a6c1`.
Read the `smile` and `adip` columns, canonicalize molecular identities, and freeze
`{"candidates": [...], "scores": [...]}` in the new run's object store. Initial
observations do not count toward the 200 new unique evaluations.

Register `rsi_domains.pablo.adapter.make_adapter()` and
`rsi_domains.pablo.lookup.make_adapter()` alongside the selected literature
adapters in the current `Runtime`. Freeze these run configuration values before
execution:

- `pablo_run_root`: the canonical absolute directory of the new run.
- `pablo_initial_ref`: the object-store reference for the initial observations.
- `pablo_max_new_evaluations`: `200` for the requested new experiment.
- `pablo_oracle_fingerprint`: `oracle.fingerprint()` in the scientific environment.
- `max_external`: `1`, supplied to `Controller.initialize` for serial execution.

The public capabilities are `pablo.evaluate` and `pablo.lookup`, both version
`1.0`. Their inputs contain exactly `run_root`, `run_id`, and `candidates` (SMILES
strings, at most 4,096 characters each). Evaluate accepts 1–20 candidates;
lookup accepts 1–100. Scoring requires an applied Lab `request_diagnostic`
proposal and its exact authenticated diagnostic work. Meta and Literature cannot
directly authorize scoring. Lookup reports exact identities at its frozen query
snapshot; `not_seen_at_snapshot` is not a guarantee about later execution.

Each scorer effect reserves one tool call, 120 wall seconds, and 120 CPU seconds.
Lookup reserves one tool call and 30 seconds of each time measure. Include these
dimensions in the run budget. Valid, canonically unique accepted molecules consume
evaluation slots; invalid and duplicate candidates do not. Pending or unknown
effects hold their slots, and interrupted execution must be reconciled without
automatically rescoring. Successful receipts publish measured raw facts and return
to Lab through the current runtime's ordinary adapter continuation.

## Scientific environment and offline verification

The existing `pablo-meta-experiment/.venv/bin/python` provides Python 3.11.14,
RDKit 2024.9.6, GuacaMol 0.5.5, NumPy 1.26.4, and SciPy 1.12.0. The evaluator
fingerprint binds its source and these four package versions. Preserve the venv
interpreter path when launching it; resolving its executable symlink can select
the base interpreter without the installed packages. Literature SDK workers may
use their separately configured environments.

From the RSI-Lab checkout, run:

```sh
../pablo-meta-experiment/.venv/bin/python -m unittest discover -s tests/pablo -v
```

The integration fixture temporarily holds out three **existing original** initial
molecules and verifies their recorded scores through the current Lab → diagnostic
→ scorer → raw fact → Lab route. Its temporary ledger is separate from research
runs. Other checks cover replay without rescoring, refusal of an unauthenticated
host scoring call, duplicate/invalid filtering, read-only lookup, interrupted
execution, and the closed input contracts. Tests make no network or model calls
and do not generate new candidate molecules.
