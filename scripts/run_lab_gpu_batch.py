#!/usr/bin/env python3
"""Execute one Controller-authored Lab request against the frozen P0 GPU profile.

This adapter never selects candidates. The authorization field identifies the
Controller transport contract; it is not a cryptographic security boundary.
All accepted candidates consume the eight-candidate campaign allowance, including
failures and interrupted jobs. An interrupted batch requires explicit audit; it
cannot be resubmitted or silently retried. Terminal evaluation uses a separate
campaign directory, a locked shortlist, and a five-candidate allowance.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import run_pilot_gpu as pilot

SCHEMA = "rsi_lab_gpu_batch_v1"
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
REQUIRED = {"schema_version", "phase", "campaign_id", "request_id", "candidate_ids",
            "input_state_version", "protocol_sha256", "candidates_sha256",
            "manifest_sha256", "controller_authorized"}
PROFILE = {"seeds": [17, 43], "cpu": 2, "gpu_device": 0, "exhaustiveness": 4,
           "num_modes": 3, "scoring": "vina", "cnn_scoring": "rescore", "cnn": "dense_1_3",
           "dock_timeout_seconds": 180, "worker_timeout_seconds": 300, "retries": 0}


def read_json(path):
    return json.loads(Path(path).read_text())


def validate_request(request, known_ids):
    fields = REQUIRED | ({"locked_shortlist_sha256"} if request.get("phase") == "terminal" else set())
    if set(request) != fields or request["schema_version"] != SCHEMA:
        raise ValueError(f"Request must have exactly the {SCHEMA} fields: {sorted(fields)}")
    if request["phase"] not in {"online", "terminal"}:
        raise ValueError("phase must be online or terminal")
    if request["controller_authorized"] is not True:
        raise ValueError("Controller authorization is required")
    for name in ["campaign_id", "request_id"]:
        if not isinstance(request[name], str) or not ID_PATTERN.fullmatch(request[name]):
            raise ValueError(f"Unsafe {name}")
    version = request["input_state_version"]
    if type(version) is not int or version < 0:
        raise ValueError("input_state_version must be a nonnegative integer")
    ids = request["candidate_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 4 or any(not isinstance(x, str) or not ID_PATTERN.fullmatch(x) for x in ids):
        raise ValueError("A batch must name 1–4 candidate IDs")
    if len(set(ids)) != len(ids) or not set(ids).issubset(known_ids):
        raise ValueError("Duplicate or unknown candidates")
    for name in [key for key in fields if key.endswith("_sha256")]:
        if not isinstance(request[name], str) or not re.fullmatch(r"[a-f0-9]{64}", request[name]):
            raise ValueError(f"Invalid {name}")


def validate_frozen_inputs(request, data, protocol_path, protocol, gnina):
    for name, path in [("protocol_sha256", protocol_path), ("candidates_sha256", data / "candidates.json"),
                       ("manifest_sha256", data / "manifest.json")]:
        if pilot.sha(path) != request[name]:
            raise ValueError(f"Frozen {name} mismatch")
    if any(protocol.get(key) != value for key, value in PROFILE.items()):
        raise ValueError("Protocol differs from the fixed P0 GNINA profile")
    if protocol.get("pose_selection") != "First pose after explicit CNNscore descending sort; no replacement after checks":
        raise ValueError("Frozen pose-selection policy mismatch")
    if protocol.get("tau") is not None or protocol.get("m_min") is not None:
        raise ValueError("This development adapter has no calibrated official pass criterion")
    for name in ["center", "size"]:
        if len(protocol.get(name, [])) != 3 or not all(math.isfinite(x) for x in protocol[name]):
            raise ValueError(f"Invalid receptor {name}")
    receptor = read_json(data / "receptor/receptor_manifest.json")
    fixed_box = {"center": [-8.773272727272728, 50.057272727272725, 12.795045454545454], "size": [22.0] * 3}
    for name, expected_values in fixed_box.items():
        if protocol[name] != receptor[name] or any(abs(a - b) > 1e-9 for a, b in zip(protocol[name], expected_values)):
            raise ValueError("Binding-site coordinates differ from frozen 1DI8 pilot")
    expected = protocol["input_sha256"]
    essential = {"candidates.json", "manifest.json", "receptor/receptor_manifest.json",
                 "receptor/receptor.pdbqt", "receptor/receptor_effective.pdb", "receptor/reference.sdf"}
    if not essential.issubset(expected):
        raise ValueError("Protocol lacks required input hashes")
    for name, digest in expected.items():
        path = (data / name).resolve()
        if not path.is_relative_to(data) or not path.is_file() or pilot.sha(path) != digest:
            raise ValueError(f"Frozen input mismatch: {name}")
    if pilot.sha(pilot.__file__) != protocol["runner_sha256"]:
        raise ValueError("P0 worker code hash changed")
    if pilot.sha(gnina) != protocol["gnina_sha256"]:
        raise ValueError("GNINA binary hash changed")
    for package, version in protocol["versions"].items():
        if importlib.metadata.version(package) != version:
            raise ValueError(f"Frozen package version changed: {package}")


def campaign_identity(request):
    return {key: request[key] for key in ["campaign_id", "phase", "protocol_sha256", "candidates_sha256", "manifest_sha256"]}


def check_admission(request, state):
    if state["identity"] != campaign_identity(request):
        raise ValueError("Campaign identity or frozen inputs changed")
    if state["state_version"] != request["input_state_version"]:
        raise ValueError("Stale input_state_version")
    if state.get("active_request") is not None:
        raise ValueError("Prior batch is incomplete; audit it before any new execution")
    if request["request_id"] in state["requests"]:
        raise ValueError("Duplicate request_id")
    if set(request["candidate_ids"]) & set(state["reserved_candidate_ids"]):
        raise ValueError("Candidate was already reserved in this campaign; no retries")
    cap = 8 if request["phase"] == "online" else 5
    if len(state["reserved_candidate_ids"]) + len(request["candidate_ids"]) > cap:
        raise ValueError(f"Phase {request['phase']} candidate limit {cap} would be exceeded")


def append_event(campaign, event):
    with (campaign / "events.jsonl").open("a") as f:
        f.write(json.dumps({"at": pilot.now(), **event}, ensure_ascii=False, allow_nan=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def execute_job(job, spec):
    job.mkdir()
    pilot.dump(job / "spec.json", spec)
    result = {"candidate_id": spec["candidate"]["candidate_id"], "kind": "candidate", "seed": spec["seed"],
              "status": "worker_error", "scores": None, "pose_valid": None, "cpu_seconds": None,
              "started_at": pilot.now(), "gpu_process_observed": False}
    started = time.monotonic()
    timed_out = False
    with (job / "worker.stdout.txt").open("w") as stdout, (job / "worker.stderr.txt").open("w") as stderr:
        process = subprocess.Popen([sys.executable, str(Path(pilot.__file__).resolve()), "--worker", str(job / "spec.json")],
                                   stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            process.wait(timeout=300)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            result["status"] = "worker_timeout"
            result["error"] = "300-second process-group limit reached; all descendants killed"
    if (job / "result.json").exists():
        result = read_json(job / "result.json")
    # A partially written worker result cannot override the parent's timeout or
    # abnormal-exit observation. Preserve that result's status for audit only.
    result["parent_observed_returncode"] = process.returncode
    if timed_out or process.returncode != 0:
        result["worker_reported_status"] = result.get("status")
        result["status"] = "worker_timeout" if timed_out else "worker_error"
        result["error"] = "300-second worker process-group limit reached" if timed_out else f"Worker exited {process.returncode}"
        result["cpu_seconds"] = None
    result["parent_observed_wall_seconds"] = time.monotonic() - started
    result["finished_at"] = pilot.now()
    pilot.dump(job / "result.json", result)
    return result


def run(args):
    data, campaign = Path(args.data).resolve(), Path(args.campaign).resolve()
    protocol_path, gnina = Path(args.protocol).resolve(), Path(args.gnina).resolve()
    request, protocol = read_json(args.request), read_json(protocol_path)
    candidates = read_json(data / "candidates.json")["candidates"]
    by_id = {c["candidate_id"]: c for c in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("Candidate manifest contains duplicate IDs")
    validate_request(request, set(by_id))
    validate_frozen_inputs(request, data, protocol_path, protocol, gnina)
    if request["phase"] == "terminal":
        if not args.locked_shortlist or pilot.sha(args.locked_shortlist) != request["locked_shortlist_sha256"]:
            raise ValueError("Terminal requests require the matching locked shortlist")
        shortlist = read_json(args.locked_shortlist)
        locked_ids = shortlist.get("candidate_ids", [])
        if shortlist.get("campaign_id") != request["campaign_id"] or not shortlist.get("locked_at"):
            raise ValueError("Invalid shortlist lock identity")
        if not 1 <= len(locked_ids) <= 5 or len(set(locked_ids)) != len(locked_ids):
            raise ValueError("Shortlist must contain 1–5 unique candidate IDs")
        if not set(request["candidate_ids"]).issubset(locked_ids) or not set(locked_ids).issubset(by_id):
            raise ValueError("Terminal request is outside the locked shortlist")
    elif args.locked_shortlist:
        raise ValueError("Locked-shortlist input is only used in terminal phase")
    gpu = pilot.probe(["nvidia-smi"])
    if gpu["returncode"] != 0:
        raise RuntimeError("GPU unavailable; no CPU fallback")
    campaign.mkdir(parents=True, exist_ok=True)
    with (campaign / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = campaign / "state.json"
        state = read_json(state_path) if state_path.exists() else {
            "identity": campaign_identity(request), "phase": request["phase"],
            "state_version": 0, "reserved_candidate_ids": [], "requests": {}, "active_request": None,
            "max_candidates": 8 if request["phase"] == "online" else 5,
            "max_jobs": 16 if request["phase"] == "online" else 10,
            "locked_shortlist_sha256": request.get("locked_shortlist_sha256"), "adapter_sha256": pilot.sha(__file__)}
        if state["adapter_sha256"] != pilot.sha(__file__):
            raise ValueError("Batch adapter changed during campaign")
        if state["locked_shortlist_sha256"] != request.get("locked_shortlist_sha256"):
            raise ValueError("Locked shortlist changed during terminal evaluation")
        check_admission(request, state)
        batch = campaign / "batches" / request["request_id"]
        batch.mkdir(parents=True, exist_ok=False)
        (batch / "jobs").mkdir()
        pilot.dump(batch / "request.json", request)
        if args.locked_shortlist:
            (batch / "locked_shortlist.json").write_bytes(Path(args.locked_shortlist).read_bytes())
        # Copy the exact bytes, preserving the declared protocol hash.
        (batch / "protocol.json").write_bytes(protocol_path.read_bytes())
        pilot.dump(batch / "gpu_before.json", gpu)
        state["state_version"] += 1
        state["reserved_candidate_ids"].extend(request["candidate_ids"])
        state["active_request"] = request["request_id"]
        state["requests"][request["request_id"]] = {"status": "reserved", "candidate_ids": request["candidate_ids"],
                                                   "request_sha256": pilot.sha(batch / "request.json")}
        pilot.dump(state_path, state)
        append_event(campaign, {"event": "reserve", "request_id": request["request_id"],
                               "candidate_ids": request["candidate_ids"], "state_version": state["state_version"]})
        started, results = time.monotonic(), []
        for candidate_id in request["candidate_ids"]:
            for seed in pilot.SEEDS:
                spec = {"candidate": by_id[candidate_id], "kind": "candidate", "seed": seed,
                        "data": str(data), "gnina": str(gnina), "protocol": protocol}
                result = execute_job(batch / "jobs" / f"{candidate_id}_s{seed}", spec)
                results.append(result)
                print(json.dumps({"request_id": request["request_id"], "progress": len(results),
                                  "total": len(request["candidate_ids"]) * 2, "candidate_id": candidate_id,
                                  "seed": seed, "status": result["status"]}), flush=True)
        records = [pilot.aggregate(by_id[cid], results) for cid in request["candidate_ids"]]
        summary = {"schema_version": SCHEMA, **campaign_identity(request),
                   "request_id": request["request_id"], "state_version": state["state_version"],
                   "request_sha256": pilot.sha(batch / "request.json"), "adapter_sha256": pilot.sha(__file__),
                   "status": "completed_with_failures" if any(r["status"] != "completed" for r in results) else "completed",
                   "wall_seconds": time.monotonic() - started, "worker_cpu_seconds": sum(r.get("cpu_seconds") or 0 for r in results),
                   "cpu_accounting_complete": all(r.get("cpu_seconds") is not None for r in results),
                   "reserved_candidates_total": len(state["reserved_candidate_ids"]),
                   "remaining_candidate_slots": state["max_candidates"] - len(state["reserved_candidate_ids"]),
                   "candidates": records, "jobs": results, "official_pass_threshold": None,
                   "locked_shortlist_sha256": request.get("locked_shortlist_sha256"),
                   "pool_relation_to_p0": protocol.get("pool_relation_to_p0", "Refer to the Controller dataset manifest; no holdout claim is established by this adapter."),
                   "limitations": ["Development campaign; not a final holdout or H0/H* comparison.",
                                   "CPU docking search plus GPU CNN rescore; sampled GPU use is not billed GPU-hours.",
                                   "No calibrated biological or official computational pass/fail criterion."]}
        pilot.dump(batch / "batch_summary.json", summary)
        pilot.dump(batch / "gpu_after.json", pilot.probe(["nvidia-smi"]))
        pilot.dump(batch / "artifact_hashes.json", {str(p.relative_to(batch)): pilot.sha(p)
                                                   for p in sorted(batch.rglob("*")) if p.is_file()})
        state["requests"][request["request_id"]].update(status=summary["status"],
            summary_sha256=pilot.sha(batch / "batch_summary.json"), artifacts_sha256=pilot.sha(batch / "artifact_hashes.json"))
        state["active_request"] = None
        pilot.dump(state_path, state)
        append_event(campaign, {"event": "complete", "request_id": request["request_id"],
                               "summary_sha256": pilot.sha(batch / "batch_summary.json"), "state_version": state["state_version"]})
        print(json.dumps({"batch_summary": str(batch / "batch_summary.json"), "state_version": state["state_version"],
                          "remaining_candidate_slots": summary["remaining_candidate_slots"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ["data", "protocol", "campaign", "request", "gnina"]:
        parser.add_argument(f"--{option}", required=True)
    parser.add_argument("--locked-shortlist", help="Terminal only: immutable JSON with campaign_id, candidate_ids, locked_at")
    run(parser.parse_args())
