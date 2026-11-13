# PABLO task adapters for the PR #4 experiment

This package supplies task-specific capabilities to the generic `rsi_lab` runtime.
It is separate from the kernel: it has no model loop or scheduler, and it does not
modify `rsi_lab`. The experiment host must explicitly register it and enable its
research features.

The implementation comes from the local `pablo-200-rounds` checkout. See
`SOURCE_ORIGIN.json` for the source paths and SHA-256 values. The scoring module
`oracle.py` is byte-identical to that source; the scorer's identity and formula
remain controller-only implementation details and must not be included in agent
prompts or outbound literature queries.

## Wiring

With `experiments/pablo` on `PYTHONPATH`:

```python
from pablo_task.research import registered_adapters, project_publication, research_policy

# Freeze these task choices when creating the run.
config["research_features"] = {
    "scientific_memory": True,
    "meta_lab": True,
    "literature": True,
}
config["research_policy"] = research_policy()
# The host also supplies the PABLO identity/input bindings, budgets,
# literature_run_root, and literature_max_calls before initialization.
runtime = Runtime(
    store, run_id,
    adapters=registered_adapters(),
    scientific_identity_provider=project_publication,
)
runtime.preflight({
    "scientific_memory": True,
    "meta_lab": True,
    "identity_provider": True,
    "research_policy": True,
    "capabilities": ["pablo.evaluate", "pablo.lookup", "literature.search"],
    "measure": ["pablo.evaluate", "pablo.lookup"],
})
```

The host should additionally check actual ready model requests with
`runtime.preflight(expect, work_id=...)`. Passing preflight verifies wiring; it
is not evidence that a model used a tool or that a recommendation improved a
scientific outcome.

- `pablo.evaluate` validates canonical novelty and the frozen evaluation quota,
  scores only authorized Lab proposals, and saves durable receipts.
- `pablo.lookup` checks the complete inherited/current/pending canonical set
  without spending a new scoring slot.
- `literature.search` retrieves real Europe PMC metadata and abstracts through
  bounded public method topics. It never sends candidates, scores, private memory,
  or the hidden scoring objective. See [its contract](literature/README.md).
- `project_publication` records source-bound scientific identities and bounded
  batch outcomes. `project_measurement` supplies domain metrics for the feedback
  loop. A measured outcome does not establish causal benefit.

## Verification

From the repository root, using a Python environment with the PABLO dependencies:

```sh
PYTHONPATH=experiments/pablo python -m unittest \
  experiments.pablo.tests.test_pablo_adapter \
  experiments.pablo.tests.test_pablo_lookup \
  experiments.pablo.tests.test_literature_adapter \
  experiments.pablo.tests.test_research_domain_boundary
```

These tests use real local scoring where explicitly stated and fixtures for model
responses and HTTP retrieval. They do not prove live model use or live external
retrieval. The full experiment host must perform and record those checks separately.
