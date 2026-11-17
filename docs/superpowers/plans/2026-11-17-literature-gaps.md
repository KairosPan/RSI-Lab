# Literature Role Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A delegated Literature task never hangs silently, and the opt-in features written for Lab and Meta treat Literature calls the same way.

**Architecture:** Five small changes in the existing kernel modules, no new module. Each change extends a rule that already exists for Lab and Meta, or for tools, to the `literature` kind.

**Tech Stack:** Python standard library, `unittest`. Run tests with `/opt/homebrew/bin/python3.14 -m unittest ...` from the repository root.

**Spec:** `docs/superpowers/specs/2026-11-17-literature-gaps-design.md`

## Global Constraints

- A refused child action still ends the task as unresolved; no repair turn is added.
- An isolated call keeps its accounting duty: `settlement_status` stays `blocked_external`.
- A run without `orchestration`, without failure policies and without literature tools must behave exactly as before; the pinned baseline hashes in `tests/v2/test_orchestration.py` and `tests/v2/test_preflight.py` must not change.
- Every recovery action is idempotent under a stable operation key and happens before the Effect's completion record.
- Original source fact: provenance `delivery_kind` in `source_excerpt`, `source_image`, `original` (`rsi_lab/literature_reading.ORIGINALS`).

## Review Focus

- A child call fails definitely while its session also needs recovery: the task still returns to the Lab once, not twice.
- `recover()` runs twice after a child failure: one research result, one Lab Work.
- A retry is scheduled and then also fails with the allowance used up: the task ends then, not before.
- A delegation whose result cites only a tool-failure receipt: `no_result`, not `new_result`.
- A task that is active only because its child Work is capacity-blocked: listed once, and `recover()` still reopens it.

---

All new tests go in `tests/v2/test_literature_recovery_and_orchestration.py`, class `LiteratureRecoveryAndOrchestrationTests`, using its existing helpers (`open`, `delegate`, `child`, `request_tool`, `execute_tool`, `reply`, `model`, `task`, `results`). Add one helper there:

```python
def failing_model(error='Provider refused the request'):
    def call(effect, packet):
        return {'status': 'failed_definite', 'actual_costs': {'model_calls': 1}, 'response': None,
                'terminal_received': True, 'error': error}
    return call
```

### Task 1: Failure policies cover Literature calls

**Files:**
- Modify: `rsi_lab/runtime.py` (`_apply_usage_policy`, `_schedule_retry`, the policy loop in `recover`)
- Test: `tests/v2/test_literature_recovery_and_orchestration.py`

- [ ] **Step 1: Write the failing tests**
  - `test_usage_policy_charges_a_child_call_with_unmeasured_usage`: `open(unknown_usage_policy='charge_reservation')`, budgets and reservations that include `tokens`; the child model returns no `tokens` cost. Assert the child proposal is `applied` and the Effect has `accounting_unknown` false.
  - `test_a_failed_child_call_is_retried_within_the_allowance`: `open(model_failure_retries=1)`; run the child with `failing_model()`. Assert a ready `literature` Work exists whose inputs carry `retry_of` and `retry_attempt == 1`, the task is still `active`, and `results()` is empty.
- [ ] **Step 2: Run them; both fail**
- [ ] **Step 3: Add `'literature'` to the Effect kind sets in `_apply_usage_policy`, `_schedule_retry` and the `recover` policy loop, and to the Work kind set in `_schedule_retry`**
- [ ] **Step 4: Run the module and `tests/v2/test_failure_policy.py`; all pass**
- [ ] **Step 5: Commit** — `Apply the failure policies to Literature calls`

### Task 2: No orphaned child task

**Files:**
- Modify: `rsi_lab/runtime.py` (`run_meta`, `recover`, `apply_meta`), `rsi_lab/driver_report.py`
- Test: same module, plus `tests/v2/test_driver_report.py` only if it pins the attention keys

**Interfaces:**
- Produces: `Runtime._return_failed_literature(effect) -> dict | None` — for a `literature` Effect in `failed_definite` whose `_schedule_retry` gave no successor, calls `_end_literature(work, 'model-failure:' + effect['id'], reason)`; `None` otherwise.
- Produces: report key `attention['stalled_literature_tasks']`, a list of `{'task_id', 'last_work_id', 'last_work_status'}`.

- [ ] **Step 1: Write the failing tests**
  - `test_a_failed_child_call_without_a_retry_returns_the_task_unresolved`: run the child with `failing_model()`; assert the task is `completed`, one result with `coverage_status == 'unresolved'`, one ready Lab Work with `literature_result_task_id`; then `recover()` twice changes nothing (`verify_history` equal).
  - `test_a_retry_that_also_fails_ends_the_task_only_then`: `model_failure_retries=1`; fail twice; after the first the task is `active`, after the second `completed` with one result.
  - `test_a_child_action_blocked_while_routing_returns_the_task_unresolved`: the child sends `read_cached_evidence` with `{'evidence_ids': ['missing'], 'document_ids': [], 'query': '', 'limit': 1}`; assert the proposal is `blocked_action`, the task `completed`, one unresolved result; replaying `apply_meta` adds no result.
  - `test_an_active_task_nobody_works_on_reads_stalled`: delegate, then cancel the child's ready Work with `store.mutate` (status `cancelled`); `run_until_wait(..., max_steps=1)` returns `status == 'stalled'`, `reason == 'stalled_literature_tasks'`, and the report lists the task id.
- [ ] **Step 2: Run them; all fail**
- [ ] **Step 3: Implement**
  - `_return_failed_literature` as above; call it in `run_meta` right after `_schedule_retry`, and in both `recover` loops that handle settled model Effects, before `_record_completion`.
  - In `apply_meta`: after a routing block of a `literature` Effect call `_end_literature(work, pid, reason)`; in the terminal branch call it for every status in `STOPPED_PROPOSALS`, not only `rejected`.
  - In `driver_report.py`: `_stalled_literature_tasks(store, run_id, works, effects)` — active `literature_task` registrations with no Work in `ready`/`leased` whose inputs name the task and no Effect naming it for which `effect_holds_execution` is true. Add the key to `attention`.
  - In `run_until_wait`: append `'stalled_literature_tasks'` as the last name in the stalled-reason order.
- [ ] **Step 4: Run the module, `tests/v2/test_driver_report.py`, `tests/v2/test_driver_liveness.py`, `tests/v2/test_literature_runtime.py`; all pass**
- [ ] **Step 5: Commit** — `Return or report every Literature task that has no one working on it`

### Task 3: Isolated calls are not read as running

**Files:**
- Modify: `rsi_lab/driver_report.py` (`build_report`, `_stalled_issues`), `rsi_lab/orchestration.py` (`steps`)
- Test: `tests/v2/test_interrupted_model_quarantine.py` (reuse its fixture that isolates a call)

- [ ] **Step 1: Write the failing tests**
  - `test_an_isolated_disposition_call_no_longer_hides_its_stalled_issue`: after isolating the only disposition call of an open issue, `build_report(runtime)['attention']['stalled_issues']` names the issue, `work_status != 'unresolved_effects'`, and `settlement_status == 'blocked_external'`.
  - `test_an_isolated_lab_call_is_not_an_orchestration_step`: in a run with `orchestration`, after isolating a Lab call, `runtime.orchestration.steps()` has no entry for that Effect and `status()['unfinished_steps']` is empty.
- [ ] **Step 2: Run them; both fail**
- [ ] **Step 3: Implement** — in `driver_report.py` use `effect_holds_execution` (from `rsi_lab.interruption`) for `work_status` and inside `_stalled_issues`; in `Orchestration.steps` skip an Effect for which `effect_is_quarantined` is true, beside the `failed_definite` skip.
- [ ] **Step 4: Run both modules and `tests/v2/test_orchestration.py`; all pass**
- [ ] **Step 5: Commit** — `Stop reading an isolated call as still running`

### Task 4: Orchestration judges a delegation by its research

**Files:**
- Modify: `rsi_lab/orchestration.py` (`_outcome`)
- Test: `tests/v2/test_literature_recovery_and_orchestration.py`

**Interfaces:**
- Produces reason codes `research_result` (with outcome `new_result`) and `research_unresolved` (with `no_result`).

- [ ] **Step 1: Write the failing tests** — all with `open(orchestration={'no_result_steps': 2, 'max_changes': 2})`; read the Lab step with `runtime.orchestration.steps()[-1]`.
  - `test_a_pending_delegation_is_an_unfinished_step`: right after `delegate()`, outcome `unfinished`; `runtime.orchestration.signal()` is `None`.
  - `test_research_that_returns_an_original_is_a_new_result`: the child reads the document (the fixture `execute` must return one passage, as in `tests/v2/test_literature_delegation_boundary.py`) and completes citing it; outcome `new_result`, code `research_result`.
  - `test_research_that_ends_unresolved_is_no_result`: the child completes with `UNRESOLVED` and no citation; outcome `no_result`, code `research_unresolved`.
  - `test_a_result_citing_only_a_tool_failure_is_no_result`: the tool fails, the child completes citing the failure fact; outcome `no_result`.
- [ ] **Step 2: Run them; all fail**
- [ ] **Step 3: Implement in `_outcome`**, before the `action != 'request_diagnostic'` line: for `request_literature` read the task `identity('literature_task', pid)`; if any fact in `task['result_ids']` has `summary_of` naming a fact whose `delivery_kind` is in `ORIGINALS` → `('new_result', 'research_result', None)`; else if the task is `active` → `('unfinished', None, None)`; else `('no_result', 'research_unresolved', <stop_reason of the last result>)`. A missing task is `unfinished`.
- [ ] **Step 4: Run the module and `tests/v2/test_orchestration.py`; all pass, baseline hashes unchanged**
- [ ] **Step 5: Commit** — `Judge a Literature delegation by the research it returns`

### Task 5: Tool inputs refused at proposal time go back to the child

**Files:**
- Modify: `rsi_lab/runtime.py` (`apply_meta`)
- Test: `tests/v2/test_literature_recovery_and_orchestration.py`

- [ ] **Step 1: Write the failing test** — `test_child_tool_inputs_refused_by_the_adapter_return_to_the_child`: give the fixture `prepare` a second switch that refuses inputs containing `refuse`; the child requests the tool with `{'refuse': True}`. Assert the proposal is `applied`, executing the queued Work yields `rejected`, the task is `active`, and a child Work carries the `rejected_before_execution` fact.
- [ ] **Step 2: Run it; it fails with the proposal `rejected` and the task `completed`**
- [ ] **Step 3: In `apply_meta`, call `_admit_tool_inputs` only when `effect['kind'] != 'literature'`**
- [ ] **Step 4: Run the module and `tests/v2/test_adapter_work_admission.py`; all pass**
- [ ] **Step 5: Commit** — `Let a child read why its tool inputs were refused`

### Task 6: Documentation and full verification

**Files:**
- Modify: `docs/persistent-agents.md` (attention list, stalled-reason order, failure policies), `docs/rsi-lab-core.md` (section "Meta 调整 Lab 编排": delegation outcomes and isolated calls)

- [ ] **Step 1: Update both documents in their existing Chinese style; state what is now covered and nothing more**
- [ ] **Step 2: Run everything** — `tests` under Python 3.14 and 3.13, `tests/literature`, `tests/pablo`, `experiments/pablo/tests` with `PYTHONPATH=experiments/pablo:.` and `-t .`, and the frontend suite. Expected: all pass except the guacamol-gated PABLO tests, unchanged from `main`.
- [ ] **Step 3: Commit** — `Document the Literature role under the failure policies and orchestration`
