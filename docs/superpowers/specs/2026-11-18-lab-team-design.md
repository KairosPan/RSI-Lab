# Lab team: a lead with Local, Challenge and helper sub-agents

Date: 2026-11-18. Status: awaiting the owner's approval.
Builds on `2026-11-17-native-codex-capabilities-design.md` (the kernel change in
pull request 9). This branch is cut from that branch, not from `main`.

Lines marked **(my call)** are choices the owner did not make; they are open to
rejection in review.

## Intent

Today one Lab agent does everything, and in the PABLO host the code tells it
which part to play: every fourth scoring batch is a "challenge" batch, and after
stagnation every second one.

The owner wants the Lab to be a lead that runs a small team and decides for
itself. In their words: "Literature 作为子 agent，角色用 Local 和 Challenge，但
'一个负责人带几个子 agent、按需派帮手'", and "能不能 Lab 自行决定是否要
Challenge". The reasons they gave earlier: the agents should perform better, and
a system whose schedule is fixed in code does not show the autonomy of an
agentic system.

This document covers the first of two projects: the team itself, in the kernel
and in the PABLO host. The second project, Literature as a sub-agent with a
tool service, gets its own design afterwards.

## Goals

1. A run config can declare named sub-agent roles for a kernel role: a name, a
   one-line description and instructions. The kernel hands them to Codex as
   agent roles, so the lead starts one by name and Codex gives the sub-agent its
   instructions unchanged.
2. Each call's record says, for every sub-agent thread the call started, which
   role it ran as, how many tokens it used and whether it started with the
   lead's conversation history.
3. The PABLO host can switch native capabilities on for the Lab and defines two
   roles, `local` and `challenge`.
4. In the PABLO host the Lab decides, batch by batch, whether to dispatch
   Challenge and whether the batch is a local or a challenge batch. The fixed
   "every fourth batch" schedule and the alternation after stagnation are gone.
5. A backstop: only when the last 4 scoring batches of this run neither beat the
   best score nor were challenge batches must the next batch be a challenge
   batch. It is a setting and can be switched off, so "fully autonomous" and
   "with backstop" can be run as two arms.
6. For every batch the Lab records whether it dispatched Challenge, whether it
   adopted its proposal and one sentence on why. The host checks the first of
   these against what the call's record shows.

## Non-goals

- Literature as a sub-agent, and any tool service for Codex. The Lab keeps
  delegating with `request_literature` exactly as today.
- Sub-agents calling Controller tools. They propose; only the Lab's answer
  requests `pablo.select_candidates` and `pablo.evaluate`.
- A memory for sub-agents that the kernel manages. A Codex sub-agent thread can
  be addressed again in later turns of the same session and keeps its own
  history until that session is rebuilt; the design records this (section 3)
  and adds nothing on top.
- Changing the scoring function, the selection methods, the per-role candidate
  checks, the interpretation barrier or Meta.
- Roles with their own model or effort. The config shape leaves room; nothing
  is built.
- Sub-agents of sub-agents. `agents.max_depth=1` stays.
- Starting an experiment. A real run needs the owner's separate go-ahead.
- The local monitoring scripts under `reports/`, which are not in git.

## Anti-goals

These would make the change a failure even if it worked:

- A backstop that brings the fixed alternation back. The existing stagnation
  thresholds (4 batches or 40 scores without a new best) are already exceeded by
  inheritance: the campaign has gone 1,001 scores without one, and under the
  current rule the last run alternated strictly from its first batch. The
  backstop therefore counts only this run's own batches.
- Accepting the Lab's statement that it dispatched Challenge without checking
  the record.
- Role instructions that pass through the Lab's own words on the way to the
  sub-agent.
- Any change in the bytes sent, or in behaviour, for a run that declares no
  roles or uses an older PABLO architecture.

## Constraints

- Codex CLI 0.163.0. Custom agent roles are known from the CLI's config schema
  and its embedded text only; no real call has used one yet. The first build
  step is a live probe (see "First step").
- The kernel stays standard-library only.
- The repository is public. Sample data in tests and documents is invented.
- Nothing may open, list or scan `~/.codex/sessions` or
  `~/.codex/archived_sessions`. A rollout is read only through the exact path a
  call record names, or located by exact thread id as `codex_ledger` already does.
- PABLO tests that import the scorer need `guacamol`, which this machine does
  not have and the owner declined to install. `experiments/pablo/tests/test_harness.py`
  does not import it; `test_harness_runtime.py`, `test_methods.py` and the host
  preflight tests do.
- Existing runs are tied to their frozen source copies and cannot be resumed
  under new code. The team is a new architecture for newly prepared runs.
- What a sub-agent reports is a lead, not evidence. The rule in the kernel
  design is unchanged.

## Approaches

**A. Codex named roles (chosen by the owner).** The run config carries each
role's name, description and instructions; the kernel declares them to Codex
(`agents.<name>.description`, `agents.<name>.config_file`) and the lead starts a
sub-agent with `agent_type`. The instructions reach the sub-agent from Codex,
not from the lead, and the record can say which role a thread ran as. Cost: it
rests on a Codex feature not yet exercised by a real call.

**B. The lead relays the role.** The role cards go into the lead's request and
the lead copies the matching instructions into the task message of a generic
sub-agent, with an agreed marker line. Works with today's kernel. Cost: the
lead can rewrite or shorten the instructions of the side that argues against
it, and "this was Challenge" is the lead's claim, recognised by convention.

**C. Separate sessions run by the Controller** (as Literature is today). Each
role would keep its own memory and budget. Dropped: the Controller would be
dispatching, not the lead, and each batch would need more Lab round trips,
which is the opposite of what the owner chose.

The owner picked A. If the probe shows custom roles do not work in `codex exec`,
the build falls back to B: the role is recognised by a marker line in the
spawn message, and sections 3 to 9 are otherwise unchanged. The fallback is
reported to the owner before the build continues.

## What was checked

| Fact | Where |
|---|---|
| Codex has built-in roles `default`, `explorer`, `worker`; the spawn tool's text lists "Available roles"; an unknown `agent_type` is an error | text embedded in the 0.163.0 binary |
| A role is `agents.<name>` with `description`, `config_file`, `nickname_candidates`; a role file must define `developer_instructions` | config schema, and the binary's error messages |
| `codex debug prompt-input` accepts role settings but does not validate them, so it proves nothing about roles | run locally, no model call |
| A sub-agent's rollout starts with a `session_meta` holding `source.subagent.thread_spawn` with `parent_thread_id`, `depth` and `agent_path`; by default it also carries the parent's history | the kernel's live probe, items 6 and 7 |
| The kernel already reads each sub-agent's usage and lists it as `child_threads` (`thread_id`, `agent_path`) | `rsi_lab/codex_ledger.py` |
| `subagents` settings hold only `max_concurrent` today | `rsi_lab/native.py`, `_shape` |
| The batch role is fixed by `_next_state`; a proposal with another role is refused; the replay in `project` recomputes the due role from the batch index | `experiments/pablo/pablo_task/harness.py` |
| The PABLO host builds its own config, has no way to pass `native_capabilities`, and reads rollouts with the strict reader | `experiments/pablo/run.py`, `host_support.py` |
| Run 1300→1700 (Lab chose freely, no roles): 83% of candidates on scaffolds already seen, no new best | local artifact `analysis400-conclusions.md` |
| Run 1700→2100 (fixed schedule): 75 batches strictly alternating; batch best ≥ 0.72 in 10 of 37 challenge batches and 2 of 38 local ones; no new best | local artifact `feedback-v3-recovery-watch` |

The two runs differ in starting point and tools and were each run once. They
are the reason for a backstop that can be switched off, not proof of what works.

## Design

### 1. Roles in the run config

`subagents` gains an optional `roles` object:

```json
"native_capabilities": {
  "lab": {
    "subagents": {
      "max_concurrent": 3,
      "roles": {
        "local": {"description": "Proposes how to advance the leading series.",
                  "instructions": "..."},
        "challenge": {"description": "Proposes a batch that argues against the leading series.",
                      "instructions": "..."}
      }
    }
  }
}
```

- A role name matches `^[a-z][a-z0-9_-]{0,31}$`, is not `default`, `explorer`
  or `worker`, and is not a fixed key of Codex's `[agents]` table (such as
  `max_depth`), which the command also sets. **(my call)**
- At most 8 roles; `description` is one line of 1 to 300 characters;
  `instructions` is 1 to 8,000 characters. **(my call)**
- In the stored settings `subagents` holds a `roles` key only when at least
  one role is declared, so settings without roles are stored exactly as today.
- Roles are frozen with the run config like the other native settings, and
  compared on recovery like them.
- A role with no `roles`, or with `subagents` off, produces the same Codex
  command, prompt and envelope as before this change.

Any kernel role (`lab`, `meta`, `literature`) may declare roles. Only the
PABLO Lab uses them in this project.

### 2. Handing the roles to Codex

- Before a call is dispatched, one file per role is written to
  `<run root>/agents/<session id>/roles/<name>.toml`, holding `name`,
  `description` and `developer_instructions` and nothing else. The folder is
  inside the run root, which the terminal profile denies to commands. The files
  are written afresh for every call from the frozen settings. **(my call on the
  place)**
- The command gains, per role in name order, `-c agents.<name>.description=…`
  and `-c agents.<name>.config_file=…`.
- `agents.max_depth=1`, the concurrency limit and the default sub-agent model
  and effort stay as the kernel sets them today. A role sub-agent runs on the
  lead's model and effort.
- The paragraph the kernel adds to the prompt when sub-agents are on names the
  declared roles with their descriptions and says they are started with
  `agent_type`.
- Codex's built-in roles stay available. The prompt paragraph tells the lead to
  use the declared roles and, for helpers, `default`. A sub-agent started with
  `explorer` or `worker` is recorded like any other and not refused. **(my
  call)** If the probe shows those two change the model or effort, the probe
  report says so; token charging is unaffected either way.

### 3. What a call records

Each entry of `child_threads` gains:

- `role`: the role the thread ran as (`local`, `default`, …), or `null` when
  the record does not say.
- `forked_history`: whether the thread started with the lead's history.
- `started_in_call`: whether this call started the thread, or addressed one
  started in an earlier turn of the same session.

**Threads that continue.** The lead may address an earlier sub-agent again
instead of starting a new one; that sub-agent then remembers its earlier work.
This is allowed and recorded, not prevented: whether to continue a thread is
the lead's decision like the others. **(my call)** A thread's role must
therefore be known in every later call that uses it, not only in the call that
started it. A session rebuild ends all of its sub-agent threads.

The receipt's `native_capabilities` names the declared roles with the SHA-256
of each role's instructions.

Where `role` is read from is settled by the probe: the sub-agent's own
`session_meta` if it names the role, otherwise the lead's `spawn_agent` call
that created the thread. A thread whose role cannot be read is recorded with
`role: null`; it does not make the call invalid. **(my call)**

### 4. The team in the PABLO host

- `run.py` gains `--team` for a fresh preparation. Without it the host prepares
  `pablo_research_pr4_feedback_v3` exactly as today. With it the architecture is
  `pablo_research_pr4_team_v1` and the harness version is `pablo_lab_led_v1`.
  **(my call on the names)**
- `--team` requires `--native-capabilities FILE`, a JSON file holding the block
  the kernel reads. The host refuses it unless `lab.subagents` is on, and
  refuses a file that itself declares `local` or `challenge`: the host adds
  those two roles with its own text (section 6). Other roles in the file are
  kept.
- **Web search is allowed.** Asked on 2026-11-18 whether a team run may switch
  on web search for the Lab and its sub-agents, the owner chose to allow it.
  The hidden objective is then kept from search by the goal text alone ("Do not
  search for or infer hidden benchmark identity"), not by what the Lab can
  reach. A team run with web search on is not a blind run, and both its
  prepared config and its final report say so in plain words.
- **The terminal may not be handed the objective.** The host refuses a readable
  root that contains or lies inside the repository checkout, the frozen source
  copy or a parent run's root, or under which a `guacamol` package is found.
  **(my call)**
- **Searches can be read afterwards.** A call's record keeps the web search
  queries of its sub-agents as well as the lead's, the first entries of each as
  `native_activity` does for the lead today, so a reader can see what was
  searched for. **(my call)**
- `--challenge-backstop-batches N` sets the backstop (default 4); `off`
  disables it.
- Like every other frozen setting, none of these can be given with `--resume`.
- The host's two rollout readers (`startup_preflight` in `run.py`,
  `rebuild_at_boundary` in `host_support.py`) pass the Lab's native settings to
  `read_native_snapshot`.
- The per-request context limit for a team run defaults to 4,194,304 bytes,
  the value the kernel's example uses, because a native turn's history is much
  larger. **(my call)**
- `prospective_rebuild_history_bytes`, the rollout size at which the host
  replaces an idle session, is today copied from the parent run. A team run
  sets it at preparation, by default to the same 4,194,304 bytes, so a session
  is never rebuilt later than the point where its next request could be
  refused. `--rebuild-history-bytes` changes it. **(my call)** A rebuild drops
  the Lab's conversation and its sub-agent threads, so this value is part of
  what an arm of an experiment is.

### 5. Who decides the batch role

Harness version `pablo_lab_led_v1`. Its frozen configuration:

| Field | Value |
|---|---|
| `challenge_backstop_batches` | 1 to 100, or `null` for off; default 4 |
| `stale_batch_threshold`, `stale_score_threshold` | kept (4 and 40); they no longer decide roles |
| `max_candidates`, `local_min_anchor_similarity`, `challenge_min_outside_incumbent_fraction` | unchanged |
| `challenge_every_batches` | removed |

**The backstop counter.** `unchallenged_stale_batches` is the number of
consecutive settled scoring batches of this run that did not beat the best
score and were not a challenge batch with at least one scored candidate. (A
challenge batch in which nothing was scored counts like a local one.) It starts at 0 for a new run whatever the inherited
history says, and returns to 0 after a challenge batch or a batch that beats
the best. When it reaches `challenge_backstop_batches`, the next batch's
`required_role` is `challenge`; otherwise `required_role` is `null`.

**What the Lab receives.** The harness context names `required_role`, the
counter and the setting, the two stagnation signals as plain facts, eligible
anchors for both roles (up to 20 each) and the last 8 batch decisions. A
missing challenge anchor blocks the run only when a challenge batch is required.

**What a proposal carries.** `pablo.select_candidates` keeps `proposal_role`
and `anchor_parent` and gains a `decision` object:

- `challenge_dispatched` (true or false),
- `challenge_adopted` (true or false; true only with `proposal_role` `challenge`
  and `challenge_dispatched` true),
- `reason`, one sentence of 1 to 300 characters.

`decision` is the Lab's own statement. It is not part of what Meta can
recommend: a Meta recommendation for `pablo.select_candidates` still names the
role, anchor and pool, the comparison of a request against an adopted
recommendation leaves `decision` out, and adopting one is the Lab's decision
like any other. **(my call)** The decision is stored with the selection plan,
and the scoring request that consumes the plan takes it from there.

**What the host accepts.**

- Either role, unless `required_role` is `challenge`, in which case only
  `challenge`.
- A required challenge batch must have `challenge_dispatched` true: the
  proposal has to come from the Challenge sub-agent. **(my call)** When nothing
  is required, the Lab may also run a challenge batch of its own design.
- `challenge_dispatched` must match the record of the Lab call that made the
  request: true needs at least one sub-agent with role `challenge` among that
  call's `child_threads`, whether started in the call or addressed again, and
  false needs none. A mismatch refuses the request, and
  the Lab sees the refusal as it sees any refused tool request.
- The anchor rule and the candidate checks for the chosen role are as today:
  local candidates stay within the similarity bound of their anchor, challenge
  candidates all leave the leading series.

**Replay.** `project` takes each settled batch's role from its binding instead
of computing it from the batch index, checks that a required role was obeyed
and that the binding names the state it was admitted under, and credits the
chosen role. Its result adds, per batch, the decision, whether the backstop
required it, and totals: challenge batches chosen by the Lab, challenge batches
required by the backstop, and batches where Challenge was dispatched but not
adopted.

The batch size rule (3 candidates, more after stale batches) is unchanged.

### 6. What the Lab and the sub-agents are told

**The Lab** (goal text of a team run): it leads a team; it may start `local`,
`challenge` and `default` helpers; sub-agents only propose and what they return
is a lead; the Lab alone requests selection and scoring; for each batch it
decides whether to ask Challenge and which proposal to run, and states the
decision and its reason in the request; when the harness requires a challenge
batch it must dispatch Challenge and run its proposal.

**Local and Challenge** (role instructions, owned by the host): the two
existing role texts in `harness.py`, each followed by the same closing part:
you are a sub-agent of the Lab; you cannot score or select; return an anchor
from the eligible list you were given, a candidate pool as SMILES, the
hypothesis and what would falsify it; do not predict scores or guess the hidden
objective.

**Challenge's starting point.** The Lab is told to start Challenge without its
own conversation history and to pass it the anchors, constraints and results it
needs. Whether it did is recorded in the call's record (`forked_history`),
not enforced. **(my call)** The reason for asking: the Lab's history is full of
its reasoning for the leading series. The reason for not enforcing: a Challenge
that starts empty depends on the Lab's briefing for its data, and nobody has
measured which matters more.

### 7. Cost

- A Lab turn is still one model call. The kernel already charges the tokens of
  every sub-agent to the turn that started it, so a team run needs a larger
  token budget; the operator sets it when preparing the run.
- The replay's per-batch record and `child_threads` together give tokens per
  role per batch.
- `tool_calls` is unaffected.

### 8. Recovery and older runs

- Recovery treats the roles as part of the native settings it already compares.
- Role files are rewritten before every call, so a damaged or stale file cannot
  reach Codex.
- A run prepared without `--team` and every older architecture keep their
  current behaviour.

### 9. Tests

All offline, with fixtures built from the probe's findings:

- Kernel: role validation and normal form; the command for zero, one and two
  roles, and unchanged output without roles; role files written and rewritten;
  `role` and `forked_history` read from fixture rollouts; receipts; recovery
  with changed roles.
- Harness (`test_harness.py`, runnable here): the counter over mixed
  sequences, including inherited stagnation not arming the backstop, the
  backstop firing exactly at the limit and resetting, backstop off, a refused
  local proposal when challenge is required, a challenge proposal without an
  anchor, decision shape, replay totals.
- Host: `--team` preparation, the refusals in section 4 (including each kind of
  readable root that exposes the objective), the plain statement that a run
  with web search on is not blind, the dispatch check against `child_threads`. Tests that need the scorer cannot run on this
  machine; they are written and reported as not run.

## First step: a live probe

Needs the owner's consent, because it uses their Codex quota: about ten short
calls at low effort, the scale of the kernel's probe. It runs in a scratch
folder with marker strings, not in a run. It establishes:

1. Whether `codex exec` with the kernel's flags accepts a role declared only
   through `-c`, lists it to the lead and accepts it as `agent_type`.
2. Whether the sub-agent receives the role file's `developer_instructions`.
3. Where the role is written: the sub-agent's `session_meta`, the lead's
   `spawn_agent` arguments, or both.
4. That a role sub-agent runs under the lead's permission profile and its usage
   rows carry the lead's turn id, as plain sub-agents do.
5. Whether a role file inside the denied run root is readable by Codex itself.
6. How a sub-agent is started without the lead's history, what the default is,
   and how the difference shows in the records.
7. Whether the built-in `explorer` and `worker` roles change the model or
   effort.
8. What the record looks like when the lead names an unknown `agent_type`.
9. What the records show when the lead addresses a role sub-agent again in a
   later turn, and whether its role can still be read then.

Items 1 and 2 decide between approach A and the fallback. The findings are
written into the plan before any other task starts.

## Added after the probe

The live probe ran on 2026-11-18 and confirmed approach A; its findings are in
the plan (`docs/superpowers/plans/2026-11-18-lab-team.md`). Three points go
beyond the sections above, each **(my call)**:

- The lead can choose a model and effort for each sub-agent it starts. Nothing
  restricts this; every `child_threads` entry records the `model` and `effort`
  the sub-agent ran on.
- A challenge batch in which no candidate was scored does not satisfy the
  backstop: the counter returns to 0 after a challenge batch only when at least
  one of its candidates was scored.
- In a run with roles, the lead's request shows it the full instructions of its
  sub-agents, because the frozen settings are part of the request's constraints.

## Left to the builder

- How a tool request is tied to the Lab call that made it, so the host can read
  that call's `child_threads` when it checks `challenge_dispatched`. Today the
  adapter hooks receive the store, the run id and the inputs, and only
  Literature tasks carry a parent effect id.
- How a thread's role stays known in later calls that address it again, if the
  probe shows the role is written only where the thread was started.
- Whether the full role text or only its digest is stored with each Effect.
- The exact wording of the goal text and of the closing part of the role
  instructions.
- Names of the new fields in the harness state and projection beyond those
  given here.
- Whether `--native-capabilities` reads the same file shape as the kernel's
  example or a smaller one.
- How a `guacamol` package under a readable root is looked for, and how deep.
- How many search queries per sub-agent a record keeps.

## After this project

- Literature as a sub-agent with a tool service (the second project).
- A real run from the 2,100 observations, in two arms with the backstop on and
  off, if the owner authorizes it.
