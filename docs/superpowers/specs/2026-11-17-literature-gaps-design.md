# Literature role gaps left by the merge of the Literature delegation

## Intent

A delegated Literature task must never hang silently, and the opt-in features
written for Lab and Meta calls must treat Literature calls the same way. The
four gaps below were found by the review of the merge and left open then.

## Goals

- The failure policies cover Literature model calls.
- A child task always goes back to its Lab or is reported; it is never left
  `active` with nobody working on it while the driver says `quiescent`.
- An operator-isolated call is no longer read as still running.
- Orchestration judges a delegation by what the research returned.

## Non-goals

- No change to what a research result is, or to how the child chooses tools.
- No repair turn for a refused child action. The existing rule stays: a refused
  child action ends the task as unresolved and returns it to the Lab.
- No change to the accounting of an isolated call: its unknown costs keep the
  run `blocked_external`.

## Approaches

For orchestration and delegation, two ways were considered.

1. **Judge by result (chosen).** A `request_literature` step is `unfinished`
   while the child task is active, `new_result` once the task has returned a
   result that cites at least one original source fact, and `no_result` when
   the task ends without one. This mirrors how a tool request is judged.
2. **Do not count delegation steps.** Simpler and never signals falsely, but
   research could then never keep a settings change in trial.

The owner picked the first.

## Design

1. **Failure policies.** `unknown_usage_policy=charge_reservation` and
   `model_failure_retries` apply to `literature` Effects and Work as they do to
   Lab and Meta. A retry is a new Literature Work with the same task binding.
2. **No orphaned task.**
   - A child model call that fails definitely with no retry scheduled returns
     the task to the Lab as unresolved.
   - A child proposal blocked while routing does the same, also on replay.
   - The driver report gains `attention.stalled_literature_tasks`: active tasks
     with no ready or leased Work and no Effect that still holds execution.
     It is the last reason considered for `stalled`.
3. **Isolated calls.** Stalled-issue detection and `work_status` ignore an
   isolated Effect. Orchestration leaves an isolated Lab call out of its steps,
   as it does a definite model failure.
4. **Delegation in orchestration.** As chosen above. An original source fact is
   one whose provenance `delivery_kind` is `source_excerpt`, `source_image` or
   `original`.
5. **Tool inputs refused at proposal time.** For a child, the admission dry run
   is skipped; the refusal then arrives at execution and goes back to the child
   as an execution observation, the same as any tool refused at execution.

## Left to the builder

Reason codes and operation keys, the wording of the documentation, and the
shape of the tests. Each change starts from a failing test.
