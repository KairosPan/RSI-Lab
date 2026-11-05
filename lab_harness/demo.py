"""Offline synthetic fixtures for exercising contracts; never scientific evidence.

Every score below is deliberately fabricated and explicitly marked synthetic.
No model, AutoDock Vina, chemistry package, or network is used by these helpers.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

from . import controller as c
from .docking import BATCH_SCHEMA, ENGINE, POSE_SELECTION, SEEDS


def make_data(out, start_index=0):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    candidates = [{"candidate_id": f"C{i:04d}", "smiles": "C", "ic50_nM": 100.0,
                   "pIC50": 7.0, "assay_id": f"SYNTHETIC_ASSAY_{start_index}",
                   "activity_ids": [i], "document_ids": ["SYNTHETIC_NOT_A_PUBLIC_SOURCE"],
                   "features": {"molecular_weight": 16.0, "logP": 0.1}}
                  for i in range(start_index, start_index + 20)]
    c.dump(out / "candidates.json", {"synthetic": True, "candidates": candidates})
    c.dump(out / "manifest.json", {"synthetic": True, "purpose": "Software smoke test only; all measurements are fabricated"})
    protocol = {"protocol_id": "synthetic_cdk2_contract_v1", "engine": ENGINE, "scoring": "vina", "pose_selection": POSE_SELECTION,
                "seeds": list(SEEDS), "tau": None, "m_min": None,
                "input_sha256": {name: c.sha(out / name) for name in ["candidates.json", "manifest.json"]}}
    c.dump(out / "protocol.json", protocol)
    return out


def make_batch(run, out):
    """Create a synthetic result for the current request, restricted to replay."""
    run, out = Path(run).resolve(), Path(out).resolve()
    state = c.read(run / "state.json")
    c.assert_frozen(run, state)
    if state["execution_mode"] != "replay":
        raise ValueError("Synthetic batch generation requires replay mode")
    rid = state["pending_request"]
    if not rid:
        raise ValueError("No pending request to simulate")
    request = state["requests"][rid]["request"]
    out.mkdir(parents=True, exist_ok=False)
    c.dump(out / "request.json", request)
    shutil.copyfile(run / "frozen/protocol.json", out / "protocol.json")
    if request["phase"] == "terminal":
        shutil.copyfile(run / "locked_shortlist.json", out / "locked_shortlist.json")
    protocol = c.read(run / "frozen/protocol.json")
    by_id = {row["candidate_id"]: row for row in state["candidates"]}
    jobs = []
    for cid in request["candidate_ids"]:
        for seed in protocol["seeds"]:
            folder = out / "jobs" / f"{cid}_s{seed}"
            folder.mkdir(parents=True)
            c.dump(folder / "spec.json", {"protocol": protocol, "seed": seed, "kind": "candidate", "candidate": by_id[cid], "synthetic": True})
            row = {"candidate_id": cid, "seed": seed, "status": "completed", "returncode": 0,
                   "scores": {"vina_affinity": -7.0},
                   "pose_valid": True, "pose_checks": {"synthetic_check": True}, "identity_preserved": True,
                   "selected_model": 1, "cpu_seconds": 0.0, "wall_seconds": 0.0, "synthetic": True}
            c.dump(folder / "result.json", row)
            (folder / "posebusters.csv").write_text("synthetic_check\nTrue\n")
            jobs.append(row)
    summary = {"schema_version": BATCH_SCHEMA, "synthetic": True,
               **{key: request[key] for key in ["campaign_id", "phase", "request_id", "protocol_sha256", "candidates_sha256", "manifest_sha256"]},
               "state_version": request["input_state_version"] + 1, "request_sha256": c.sha(out / "request.json"),
               "adapter_sha256": state["frozen_hashes"]["run_vina_batch.py"], "jobs": jobs,
               "candidates": [c.aggregate_jobs(cid, jobs) for cid in request["candidate_ids"]],
               "worker_cpu_seconds": 0.0, "cpu_accounting_complete": True, "wall_seconds": 0.0}
    c.dump(out / "batch_summary.json", summary)
    c.dump(out / "artifact_hashes.json", {str(p.relative_to(out)): c.sha(p) for p in out.rglob("*") if p.is_file() and p.name != "artifact_hashes.json"})
    return out


def decision(state, action):
    ids = [row["candidate_id"] for row in state["candidates"][:5]]
    shortlist = [{"candidate_id": cid, "evidence_ids": [f"ev_ic50_{cid}"],
                  "uncertainty": "Fabricated fixture; no molecular conclusion is supported"} for cid in ids]
    memory = []
    if state["recent_tool_evidence_ids"]:
        refs = state["recent_tool_evidence_ids"]
        memory.append({"kind": "observation", "subject_ids": [state["evidence"][eid]["candidate_id"] for eid in refs],
                       "statement": "The synthetic fixture exercised ingestion; these scores are not experiment results.",
                       "evidence_ids": refs, "supersedes": None,
                       "scope": {"target_id": "CHEMBL301", "construct_id": state["construct_id"],
                                 "assay_ids": [], "protocol_id": state["protocol_id"], "tool_versions": [],
                                 "applicable_conditions": ["synthetic replay only"]},
                       "supporting_evidence_ids": refs, "challenging_evidence_ids": [], "status": "proposed",
                       "limitations": ["Fabricated software fixture"], "depends_on_memory_ids": []})
    return {"input_state_version": state["state_version"], "next_action": action,
            "reason_brief": "Exercise the controller contract using a synthetic fixture", "evidence_ids": [f"ev_ic50_{ids[0]}"],
            "tool_requests": [{"request_id": "online_001", "tool_id": c.TOOL, "candidate_ids": ids[:2], "parameter_profile": c.PROFILE}] if action == "tool_batch" else [],
            "read_evidence_ids": [], "memory_delta": memory, "shortlist": shortlist,
            "open_questions": ["No real experiment has been run"], "stop_reason": "Synthetic smoke test complete" if action == "submit" else None}


def run(out, harness=None, phase="development", start_index=0):
    """Run a complete synthetic campaign at out and return its absolute path."""
    campaign = Path(out).resolve()
    if campaign.exists():
        raise FileExistsError(f"Demo output already exists: {campaign}")
    fixtures = campaign.parent / ("_fixtures_" + campaign.name)
    fixtures.mkdir(parents=True, exist_ok=False)
    harness = Path(harness).resolve() if harness is not None else c.ROOT / "harness/h0"
    data = make_data(fixtures / "data", start_index=start_index)
    c.init(argparse.Namespace(data=data, protocol=data / "protocol.json", out=campaign, harness=harness,
                             phase=phase, execution_mode="replay", model="synthetic-no-model", effort="none"))
    response = fixtures / "decision.json"
    c.dump(response, decision(c.read(campaign / "state.json"), "tool_batch"))
    c.turn(argparse.Namespace(run=campaign, response=response))
    c.ingest(argparse.Namespace(run=campaign, batch=make_batch(campaign, fixtures / "online_batch")))
    c.dump(response, decision(c.read(campaign / "state.json"), "submit"))
    c.turn(argparse.Namespace(run=campaign, response=response))
    c.finalize(argparse.Namespace(run=campaign))
    c.ingest(argparse.Namespace(run=campaign, batch=make_batch(campaign, fixtures / "terminal_batch")))
    c.finalize(argparse.Namespace(run=campaign))
    return campaign
