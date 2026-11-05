"""Drive a real Lab campaign through its existing model and scientific boundaries.

This supervisor selects no candidates and supplies no model responses. Completed
worker receipts may be imported after a restart; an uncertain dispatch is never
resent automatically. Its mutable execution records live outside the campaign.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from . import controller as c


class AuditRequired(RuntimeError):
    """External execution cannot safely proceed without reconciling receipts."""


def _execute_pending(run, state, execution, vina, worker_python):
    rid = state["pending_request"]
    reservation = state["requests"][rid]
    request = reservation["request"]
    request_path = run / "pending" / rid / "request.json"
    if c.read(request_path) != request or c.canonical_hash(request) != reservation["canonical_hash"]:
        raise AuditRequired("Pending request differs from its immutable reservation")
    phase = request["phase"]
    if phase not in {"online", "terminal"} or not c.ID.fullmatch(rid):
        raise AuditRequired("Invalid pending request identity")
    campaign = execution / "worker" / phase
    batch = campaign / "batches" / rid
    # The controller performs full validation during ingest, including every raw
    # artifact. The existence check here authorizes import, never acceptance.
    if (batch / "artifact_hashes.json").is_file():
        return batch
    receipt_path = execution / "dispatches" / rid / "receipt.json"
    if batch.exists() or receipt_path.exists():
        raise AuditRequired("Prior worker dispatch has no completed batch; no implicit retry")
    if (campaign / "state.json").exists():
        worker_state = c.read(campaign / "state.json")
        if worker_state.get("active_request") or rid in worker_state.get("requests", {}):
            raise AuditRequired("Worker has an unresolved or previously dispatched request")
    folder = receipt_path.parent
    folder.mkdir(parents=True, exist_ok=False)
    command = [worker_python, str(run / "frozen/run_vina_batch.py"), "run",
               "--data", state["source_data_path"],
               "--protocol", str(run / "frozen/protocol.json"),
               "--vina", vina, "--campaign", str(campaign), "--request", str(request_path)]
    if phase == "terminal":
        command.extend(["--locked-shortlist", str(run / "locked_shortlist.json")])
    receipt = {"schema_version": "lab_autonomous_dispatch_v1", "status": "prepared",
               "created_at": c.now(), "request_id": rid,
               "request_sha256": c.sha(request_path), "command": command,
               "returncode": None, "pid": None,
               "note": "Worker processes have their own sessions; interrupted dispatch requires receipt reconciliation."}
    c.dump(receipt_path, receipt)
    process = None
    try:
        with (folder / "stdout.log").open("x") as out, (folder / "stderr.log").open("x") as err:
            process = subprocess.Popen(command, stdout=out, stderr=err,
                                       start_new_session=True,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            receipt.update(status="dispatched", dispatched_at=c.now(), pid=process.pid)
            c.dump(receipt_path, receipt)
            receipt["returncode"] = process.wait()
        receipt.update(status="exited", finished_at=c.now())
        c.dump(receipt_path, receipt)
    except BaseException as exc:
        receipt.update(status="dispatch_unknown" if process is not None else "launch_failed",
                       error=f"{type(exc).__name__}: {exc}", interrupted_at=c.now())
        if process is not None:
            receipt["returncode"] = process.poll()
        c.dump(receipt_path, receipt)
        raise AuditRequired("Worker dispatch interrupted; inspect its recorded process and batch before resuming") from exc
    if receipt["returncode"] != 0:
        raise AuditRequired(f"Worker exited {receipt['returncode']}; inspect {receipt_path}")
    if not (batch / "artifact_hashes.json").is_file():
        raise AuditRequired("Worker exited without a complete batch receipt")
    return batch


def _summary(run, state, execution, *, status=None, error=None):
    result = {"status": status or state["status"], "run": str(run),
              "execution_root": str(execution), "controller_status": state["status"],
              "pending_request": state.get("pending_request"),
              "model_calls": len(state.get("llm_calls", [])),
              "observed_input_plus_output_tokens": state.get("usage_tokens", 0),
              "finished_at": c.now()}
    if result["status"] == "completed":
        result["evaluation"] = str(run / "evaluation.json")
    if error:
        result["error"] = error
    c.dump(execution / "supervisor_result.json", result)
    return result


def run_campaign(run, *, vina, worker_python=sys.executable):
    """Run an initialized live campaign; return completed or an explicit halt.

    Callers must initialize/freeze a fresh run before entry. This function adds
    no model or scientific budget and never changes a model's valid decisions.
    """
    run = Path(run).resolve()
    state = c.read(run / "state.json")
    if state["execution_mode"] != "live":
        raise ValueError("Autonomous supervision requires a live campaign, not replay")
    c.assert_frozen(run, state)
    vina = str(Path(vina).resolve())
    # Resolving a venv's Python symlink would silently discard that environment.
    worker_python = os.path.abspath(os.fspath(worker_python))
    if not os.path.isfile(vina) or not os.access(vina, os.X_OK):
        raise ValueError("Vina executable is missing or not executable")
    if not os.path.isfile(worker_python) or not os.access(worker_python, os.X_OK):
        raise ValueError("Worker Python is missing or not executable")
    execution = run.parent / (run.name + ".execution")
    execution.mkdir(exist_ok=True)
    with (execution / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = {"schema_version": "lab_autonomous_execution_v1", "run": str(run),
                    "frozen_campaign_sha256": c.sha(run / "frozen/campaign.json"),
                    "vina": vina, "worker_python": worker_python}
        config = execution / "identity.json"
        if config.exists():
            if c.read(config) != identity:
                raise ValueError("Supervisor execution identity changed")
        else:
            c.dump(config, identity)
        try:
            # Domain limits belong to the controller. This only detects an
            # implementation that repeatedly returns without making progress.
            while True:
                state = c.read(run / "state.json")
                c.assert_frozen(run, state)
                if state["status"] in {"completed", "needs_audit", "halted_without_shortlist"}:
                    return _summary(run, state, execution)
                if any(call.get("status") == "running" for call in state["llm_calls"]):
                    raise AuditRequired("An earlier model call is unresolved; no automatic retry")
                before = c.canonical_hash(state)
                if state.get("pending_request"):
                    batch = _execute_pending(run, state, execution, vina, worker_python)
                    c.ingest(SimpleNamespace(run=str(run), batch=str(batch)))
                elif state.get("locked_shortlist_sha256"):
                    c.finalize(SimpleNamespace(run=str(run)))
                else:
                    c.turn(SimpleNamespace(run=str(run), response=None))
                after = c.read(run / "state.json")
                if c.canonical_hash(after) == before:
                    raise AuditRequired("Controller made no progress; automatic loop stopped")
        except (AuditRequired, ValueError, OSError, KeyError, TypeError) as exc:
            return _summary(run, c.read(run / "state.json"), execution,
                            status="needs_audit", error=f"{type(exc).__name__}: {exc}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--vina", required=True)
    parser.add_argument("--worker-python", default=sys.executable)
    args = parser.parse_args()
    result = run_campaign(args.run, vina=args.vina, worker_python=args.worker_python)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
