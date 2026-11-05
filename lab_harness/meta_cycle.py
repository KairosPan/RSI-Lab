"""Run one live Meta proposal and its complete prospective validation campaign.

There is no global wall-clock cutoff and no replay/proposal injection interface.
Existing Controller, transport and worker limits remain authoritative. Interrupted
or failed cycles retain their ledger and are never silently retried.
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path

from . import controller as c
from . import meta, transport


def _run_campaign(run, *, vina, worker_python):
    from .autonomous import run_campaign
    return run_campaign(run, vina=vina, worker_python=worker_python)


def _actual_meta_usage(workspace):
    """Reconcile the live Meta receipt with its archived Codex event stream."""
    root, identity, _ = meta.workspace(workspace)
    receipt = c.read(root / "call_receipt.json")
    folder = root / "call"
    usage = receipt.get("usage")
    if (identity.get("execution_mode") != "live" or identity.get("model_attempts") != 1
            or receipt.get("backend") != "codex" or receipt.get("error") is not None
            or type(receipt.get("returncode")) is not int or receipt["returncode"] != 0
            or receipt.get("usage_complete") is not True or not isinstance(usage, dict)
            or any(type(usage.get(key)) is not int or usage[key] < 0 for key in ("input_tokens", "output_tokens"))):
        raise ValueError("Meta invocation lacks complete actual usage; validation admission requires audit")
    if receipt != c.read(folder / "transport_result.json"):
        raise ValueError("Meta call receipt differs from its transport archive")
    invocation = c.read(folder / "invocation.json")
    if (invocation.get("backend") != "codex" or invocation.get("dispatched") is not True
            or type(invocation.get("returncode")) is not int or invocation["returncode"] != 0
            or any(invocation.get(key) != identity[key] for key in ("model", "effort"))):
        raise ValueError("Meta lacks a successful live dispatch receipt")
    proposal = c.read(root / "proposal.json")
    if (receipt.get("response") != proposal or c.read(folder / "response.json") != proposal
            or c.read(folder / "usage.json") != {"usage": usage, "usage_complete": True}):
        raise ValueError("Meta proposal/usage differs from its transport archive")
    output, event_usage, complete, errors = transport._events((folder / "events.jsonl").read_text())
    if errors or not complete or event_usage != usage or transport._json(output) != proposal:
        raise ValueError("Meta proposal/usage differs from its raw Codex events")
    return usage


def _paths(development_run, workspace, planned_pairs, gate_out):
    development, root, output = (Path(path).resolve() for path in (development_run, workspace, gate_out))
    if root.exists() or output.exists():
        raise ValueError("Meta cycle requires a new workspace and gate output; no implicit retry or overwrite")
    pairs = []
    for pair in planned_pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 4:
            raise ValueError("Each validation pair requires data, protocol, baseline run, and candidate run")
        pairs.append(tuple(Path(path).resolve() for path in pair))
    if len(pairs) < meta.DEFAULT_RULES["min_pairs"]:
        raise ValueError("A live Meta cycle requires at least two prospectively registered pairs")
    runs = [path for pair in pairs for path in pair[2:]]
    if len(set(runs)) != len(runs) or any(path.exists() for path in runs):
        raise ValueError("Every planned validation run must have a distinct, nonexistent path")
    if root.is_relative_to(development) or output.is_relative_to(development):
        raise ValueError("Cycle outputs cannot modify the completed development archive")
    protected = (development, root / "parent", root / "candidate", root / "call")
    if any(path.is_relative_to(folder) or folder.is_relative_to(path) for path in runs for folder in protected):
        raise ValueError("Validation run paths overlap protected development or Meta artifacts")
    if any(a != b and a.is_relative_to(b) for a in runs for b in runs):
        raise ValueError("Validation run paths cannot contain one another")
    if any(output.is_relative_to(path) for path in runs) or any(output.is_relative_to(folder) for folder in protected[1:]):
        raise ValueError("Gate output cannot modify validation archives or frozen Meta evidence")
    if output in {root / name for name in ("cycle.json", "selected_policy.json", "meta.json", "input.json",
                                         "rules.json", "proposal.json", "proposal.schema.json", "prompt.txt",
                                         "candidate.json", "call_receipt.json", "validation_plan.json", ".cycle.lock", ".lock")}:
        raise ValueError("Gate output collides with a Meta cycle artifact")
    return development, root, pairs, output


def execute_cycle(*, development_run, workspace, planned_pairs, gate_out, vina, worker_python, checkpoint=None):
    """Create and execute one new live Meta cycle; return its persisted ledger.

    ``planned_pairs`` contains (data, protocol, baseline_run, candidate_run)
    tuples. The model makes one real proposal. Every registered arm executes
    independently; no failed pair can be omitted or replaced. Policy selection
    is a pointer for a future campaign and never mutates an existing campaign.
    """
    development, root, pairs, output = _paths(development_run, workspace, planned_pairs, gate_out)
    development, dev, dev_report = meta.completed_run(development, "development")
    if dev["execution_mode"] != "live" or dev_report.get("synthetic") is not False:
        raise ValueError("Automatic Meta cycles require a completed live development campaign")
    if not dev_report.get("llm_usage_accounting_complete"):
        raise ValueError("Development model usage is incomplete; audit before starting Meta")
    meta.prepare(argparse.Namespace(development_run=development, out=root, min_pairs=meta.DEFAULT_RULES["min_pairs"]))
    ledger = {"schema_version": "lab_meta_cycle_v1", "status": "prepared", "created_at": c.now(),
              "development_run": str(development), "development_archive_sha256": c.sha(development / "artifact_hashes.json"),
              "workspace": str(root), "gate_out": str(output), "model": dev["model"], "effort": dev["effort"],
              "execution_mode": "live", "synthetic": False, "global_wall_time_limit_seconds": None,
              "requested_pairs": [[str(path) for path in pair] for pair in pairs],
              "vina": str(Path(vina).resolve()), "worker_python": os.path.abspath(os.fspath(worker_python)),
              "meta_usage": None, "validation_runs": [], "events": [], "decision": None,
              "eligible_for_next_development_run": False, "selected_policy": None}

    def record(status, **fields):
        ledger.update(status=status, **fields)
        ledger["events"].append({"at": c.now(), "status": status})
        c.dump(root / "cycle.json", ledger)

    def select(path, harness_hash, reason, gate=None):
        pointer = {"schema_version": "lab_selected_policy_v1", "created_at": c.now(), "harness": str(path),
                   "harness_hash": harness_hash, "reason": reason, "cycle": str(root),
                   "use": "new_development_campaign_only", "gate": str(output) if gate else None,
                   "gate_sha256": c.sha(output) if gate else None,
                   "candidate_promoted": bool(gate and gate["eligible_for_next_development_run"])}
        with (root / "selected_policy.json").open("x") as stream:
            stream.write(c.encoded(pointer).decode())
        return pointer

    record("prepared")
    with (root / ".cycle.lock").open("x") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            record("proposing")
            proposal = meta.propose(argparse.Namespace(workspace=root, response=None))
            ledger["meta_usage"] = _actual_meta_usage(root)
            ledger["meta_call_receipt_sha256"] = c.sha(root / "call_receipt.json")
            _, identity, _ = meta.workspace(root)
            if proposal["status"] == "no_change":
                pointer = select(root / "parent", identity["parent_harness_hash"], "meta_no_change")
                record("no_change", decision="no_change", selected_policy=pointer, finished_at=c.now())
                return ledger
            if proposal["status"] != "candidate_frozen":
                raise ValueError(f"Meta did not produce a frozen candidate: {proposal['status']}")
            record("candidate_frozen")
            meta.plan(argparse.Namespace(workspace=root, pair=pairs))
            _, identity, _ = meta.workspace(root)
            registration = c.read(root / "validation_plan.json")
            ledger["validation_plan_sha256"] = identity["validation_plan_sha256"]
            record("validation_planned")
            if checkpoint is not None:
                checkpoint("before_validation", workspace=root, registration=registration)
            for number, registered in enumerate(registration["pairs"], 1):
                for arm, policy, harness_hash in (("baseline", root / "parent", identity["parent_harness_hash"]),
                                                 ("candidate", root / "candidate", identity["candidate_harness_hash"])):
                    meta.workspace(root)  # Detect plan or frozen evidence drift before each execution.
                    run = Path(registered[arm])
                    attempt = {"pair": number, "arm": arm, "run": str(run), "harness_hash": harness_hash,
                               "status": "initializing", "started_at": c.now()}
                    ledger["validation_runs"].append(attempt)
                    record("validating")
                    state = c.init(argparse.Namespace(data=registered["data"], protocol=registered["protocol"],
                                   harness=policy, out=run, phase="validation", execution_mode="live",
                                   model=dev["model"], effort=dev["effort"]))
                    expected = {"dataset_hash": registered["dataset_hash"], "protocol_hash": registered["protocol_hash"],
                                "harness_hash": harness_hash, "runtime_hash": dev["runtime_hash"]}
                    if any(state.get(key) != value for key, value in expected.items()):
                        raise ValueError("Validation initialization differs from its frozen plan/runtime")
                    if checkpoint is not None:
                        checkpoint("before_docking", run=run)
                    attempt["status"] = "running"
                    record("validating")
                    result = _run_campaign(run, vina=vina, worker_python=worker_python)
                    attempt.update(status=result["status"], runner_result=result, finished_at=c.now())
                    record("validating")
                    if result["status"] != "completed":
                        raise ValueError(f"Validation pair {number} {arm} did not complete: {result['status']}")
                    _, _, evaluated = meta.completed_run(run, "validation")
                    if not evaluated["llm_usage_accounting_complete"] or evaluated.get("synthetic") is not False:
                        raise ValueError("Validation arm lacks complete actual model usage")
                    attempt.update(archive_sha256=c.sha(run / "artifact_hashes.json"),
                                   observed_input_plus_output_tokens=evaluated["observed_input_plus_output_tokens"])
                    record("validating")
            record("gating")
            if (c.sha(root / "call_receipt.json") != ledger["meta_call_receipt_sha256"]
                    or _actual_meta_usage(root) != ledger["meta_usage"]):
                raise ValueError("Meta inference receipt changed during validation")
            # Do not pass a selected subset. Gate reads every prospectively registered pair.
            assessment = meta.gate(argparse.Namespace(workspace=root, pair=None, out=output))
            if assessment["decision"] not in {"accepted", "rejected"} or assessment.get("synthetic") is not False:
                raise ValueError("Live Meta gate returned a non-live decision")
            accepted = assessment["decision"] == "accepted"
            if assessment.get("eligible_for_next_development_run") is not accepted:
                raise ValueError("Meta decision and admission eligibility disagree")
            if checkpoint is not None:
                checkpoint("before_promotion", workspace=root, assessment=assessment, gate_path=output)
            pointer = select(root / ("candidate" if accepted else "parent"),
                             identity["candidate_harness_hash"] if accepted else identity["parent_harness_hash"],
                             "gate_accepted" if accepted else "gate_rejected_parent_retained", assessment)
            record("completed", decision=assessment["decision"], eligible_for_next_development_run=accepted,
                   gate_sha256=c.sha(output), selected_policy=pointer, finished_at=c.now())
            return ledger
        except BaseException as exc:
            previous = ledger["status"]
            record("needs_audit", failure_stage=previous, error=f"{type(exc).__name__}: {exc}", finished_at=c.now())
            if not isinstance(exc, Exception):
                raise
            return ledger
