#!/usr/bin/env python3
"""Resumable Lab campaign controller with a frozen evaluator and versioned harness.

V1 is the explicit CDK2/1DI8, twenty-candidate, five-shortlist AutoDock Vina profile.
The model returns decisions only. Numerical jobs run through the external worker.
Replay is a synthetic software exercise and cannot establish scientific results.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import csv
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from . import input_view as lab_input_view
from . import versions, transport
from .final_selection import MIN_FINAL_COMPOUNDS
from .docking import (BATCH_SCHEMA, ENGINE, SCORE_FIELD, SEEDS, TOOL_VERSION, WORKER_FILES,
                      evaluator_descriptor, parse_vina_poses, validate_vina_protocol)
from scripts.run_vina_batch import load_chemical_states

ROOT = Path(__file__).resolve().parents[1]
TOOL = "autodock_vina"
PROFILE = "autodock_vina_cdk2_v1"
SCORES = ("vina_affinity",)
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
LIMITS = {"online_candidates": 8, "batch_candidates": 4, "llm_calls": 6,
          "llm_timeout_seconds": 300, "soft_total_tokens": 150000, "shortlist_size": 5}
TERMINAL_LIMIT = 5
PRESENTATION_FILE = "harness/input_view.json"


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def event(run, kind, **fields):
    with (run / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"at": now(), "event": kind, **fields}, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def locked(run):
    with (run / ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def arr(items):
    return {"type": "array", "items": items}


STRING = {"type": "string"}
NULLSTR = {"type": ["string", "null"]}
STRINGS = arr(STRING)
MEMORY_SCHEMA = obj({
    "kind": {"type": "string", "enum": ["observation", "hypothesis", "conflict", "strategy"]},
    "subject_ids": STRINGS, "statement": STRING, "evidence_ids": STRINGS,
    "supersedes": NULLSTR,
    "scope": obj({"target_id": NULLSTR, "construct_id": NULLSTR, "assay_ids": STRINGS,
                  "protocol_id": NULLSTR, "tool_versions": arr(obj({"tool_id": STRING, "version": STRING})),
                  "applicable_conditions": STRINGS}),
    "supporting_evidence_ids": STRINGS, "challenging_evidence_ids": STRINGS,
    "status": {"type": "string", "enum": ["proposed", "supported_in_scope", "disputed"]},
    "limitations": STRINGS, "depends_on_memory_ids": STRINGS,
})
RESPONSE_SCHEMA = obj({
    "input_state_version": {"type": "integer"},
    "next_action": {"type": "string", "enum": ["tool_batch", "read_evidence", "submit"]},
    "reason_brief": STRING, "evidence_ids": STRINGS,
    "tool_requests": arr(obj({"request_id": STRING, "tool_id": STRING,
                              "candidate_ids": STRINGS, "parameter_profile": STRING})),
    "read_evidence_ids": STRINGS, "memory_delta": arr(MEMORY_SCHEMA),
    "shortlist": arr(obj({"candidate_id": STRING, "evidence_ids": STRINGS, "uncertainty": STRING})),
    "open_questions": STRINGS, "stop_reason": NULLSTR,
})


def validate_schema(value, schema, path="response"):
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    is_type = {"object": type(value) is dict, "array": type(value) is list,
               "string": type(value) is str, "integer": type(value) is int,
               "null": value is None}
    if not any(is_type[kind] for kind in kinds):
        raise ValueError(f"{path}: expected {kinds}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: invalid enum")
    if type(value) is dict:
        if set(value) != set(schema["properties"]):
            raise ValueError(f"{path}: keys differ from strict schema")
        for key, child in value.items():
            validate_schema(child, schema["properties"][key], path + "." + key)
    if type(value) is list:
        for index, child in enumerate(value):
            validate_schema(child, schema["items"], f"{path}[{index}]")


def protected_sources():
    paths = sorted((ROOT / "lab_harness").glob("*.py"))
    paths += [ROOT / "lab_harness/common_contract.md"]
    paths += [ROOT / "scripts" / name for name in WORKER_FILES]
    return {str(path.relative_to(ROOT)): path for path in paths}


def assert_frozen(run, state):
    for name, digest in state["frozen_hashes"].items():
        if sha(run / "frozen" / name) != digest:
            raise ValueError(f"Frozen input changed: {name}")
    live = protected_sources()
    if set(live) != set(state["protected_hashes"]):
        raise ValueError("Protected implementation file set changed after init")
    for name, digest in state["protected_hashes"].items():
        if sha(live[name]) != digest:
            raise ValueError(f"Protected implementation changed after init: {name}")
    harness = versions.load_harness(run / "frozen/harness")
    if harness["harness_hash"] != state["harness_hash"]:
        raise ValueError("Harness identity changed after init")
    if lab_input_view.validate_config(harness["input_view"]) != state["presentation_mode"]:
        raise ValueError("Presentation state differs from frozen configuration")
    identity = read(run / "frozen/campaign.json")
    for key in ("campaign_id", "phase", "execution_mode", "model", "effort", "protocol_hash", "dataset_hash", "harness_hash", "runtime_hash"):
        if state[key] != identity[key]:
            raise ValueError(f"Campaign identity differs from frozen configuration: {key}")
    if state.get("locked_shortlist_sha256") and sha(run / "locked_shortlist.json") != state["locked_shortlist_sha256"]:
        raise ValueError("Locked shortlist changed")


def freeze_public_data(data):
    records = read(data / "candidates.json")["candidates"]
    manifest = read(data / "manifest.json")
    chemical_method, chemical_records = load_chemical_states(data, candidates=records, manifest=manifest)
    public_mode = manifest.get("public_input_mode", "historical_assay")
    if public_mode not in {"historical_assay", "structure_only"}:
        raise ValueError("Unsupported public input mode")
    if len(records) != 20 or len({r["candidate_id"] for r in records}) != 20:
        raise ValueError("Expected 20 unique candidates")
    features_by_id = {}
    for name in ["features.json", "structure_validation.json"]:
        if (data / name).exists():
            payload = read(data / name)
            items = payload if isinstance(payload, list) else payload.get("candidates", payload.get("molecules", payload.get("results", [])))
            if isinstance(items, list):
                for row in items:
                    if isinstance(row, dict) and row.get("candidate_id"):
                        features_by_id[row["candidate_id"]] = row
    allowed = {"scaffold", "scaffold_id", "scaffold_smiles", "murcko_scaffold", "mw", "molecular_weight",
               "logp", "tpsa", "hbd", "hba", "logP", "TPSA", "HBD", "HBA", "rotatable_bonds", "heavy_atoms",
               "formal_charge", "ring_count", "fragment_count", "unassigned_stereocenters"}
    evidence, public = {}, []
    if chemical_method is not None:
        evidence["ev_chemical_preparation"] = {
            "evidence_id": "ev_chemical_preparation", "kind": "chemical_preparation_method",
            "method": chemical_method,
            "limitations": ["Exploratory pH interval 7.5–8.0 is a preparation assumption, not a shared assay condition",
                            "One admitted rule-predicted state; no state population estimate or receptor pH preparation is claimed"]}
    for source in records:
        cid = source["candidate_id"]
        if not ID.fullmatch(cid):
            raise ValueError("Unsafe candidate ID")
        record = {key: source.get(key) for key in ["candidate_id", "smiles", "assay_id", "activity_ids", "document_ids"]}
        if not isinstance(record["smiles"], str) or not record["smiles"].strip():
            raise ValueError(f"Missing public structure: {cid}")
        if public_mode == "historical_assay":
            record.update({key: source.get(key) for key in ("ic50_nM", "pIC50")})
            if type(record["ic50_nM"]) not in (int, float) or not math.isfinite(record["ic50_nM"]) or record["ic50_nM"] <= 0:
                raise ValueError(f"Missing public structure/IC50: {cid}")
            if type(record["pIC50"]) not in (int, float) or not math.isfinite(record["pIC50"]) or not math.isclose(record["pIC50"], 9 - math.log10(record["ic50_nM"]), abs_tol=1e-9):
                raise ValueError(f"Inconsistent public pIC50: {cid}")
        elif any(source.get(key) is not None for key in ("ic50_nM", "pIC50")):
            raise ValueError("Structure-only candidate input must omit activity values")
        record["features"] = {k: v for k, v in {**features_by_id.get(cid, {}), **source.get("structure", {}), **source.get("features", {}), **source}.items() if k in allowed}
        if chemical_method is not None:
            provenance = chemical_records[cid]
            record.update({key: provenance[key] for key in ("source_smiles", "canonical_parent_identity", "prepared_state_id", "prepared_smiles")})
            eid = f"ev_chemical_state_{cid}"
            evidence[eid] = {"evidence_id": eid, "kind": "chemical_preparation", "candidate_id": cid,
                             "method_evidence_id": "ev_chemical_preparation", "record": provenance}
        public.append(record)
        if public_mode == "historical_assay":
            eid = f"ev_ic50_{cid}"
            evidence[eid] = {"evidence_id": eid, "kind": "public_measurement", "candidate_id": cid,
                             "ic50_nM": record["ic50_nM"], "pIC50": record["pIC50"], "assay_id": record["assay_id"],
                             "activity_ids": record["activity_ids"], "document_ids": record["document_ids"],
                             "limitations": ["Public historical assay input, not a new result or hidden evaluation label"]}
        else:
            eid = f"ev_source_{cid}"
            evidence[eid] = {"evidence_id": eid, "kind": "structure_source", "candidate_id": cid,
                             "assay_id": record["assay_id"], "activity_ids": record["activity_ids"],
                             "document_ids": record["document_ids"],
                             "limitations": ["Source provenance only; no activity values or prior docking scores supplied"]}
        if record["features"]:
            eid = f"ev_features_{cid}"
            evidence[eid] = {"evidence_id": eid, "kind": "structure_features", "candidate_id": cid, "features": record["features"]}
    assay = read(data / "assay_details.json") if (data / "assay_details.json").exists() else {}
    evidence["ev_assay_context"] = {"evidence_id": "ev_assay_context", "kind": "assay_context",
        "target_id": "CHEMBL301", "description": assay.get("description"), "assay_organism": assay.get("assay_organism"),
        "confidence_score": assay.get("confidence_score"), "confidence_description": assay.get("confidence_description"),
        "public_input_mode": public_mode,
        "limitations": ["Assay and rigid 1DI8 monomer construct may differ; new computation is not biological confirmation",
                        "Only explicitly supplied assay conditions and confidence apply; unknown conditions are not inferred from target identity"]}
    if (data / "task_context.json").exists():
        context = read(data / "task_context.json")
        if not isinstance(context, dict):
            raise ValueError("Task context must be a source-backed object")
        evidence["ev_task_context"] = {"evidence_id": "ev_task_context", "kind": "task_readiness_context", "context": context}
    return public, evidence


def init(args):
    data, protocol_path, run = Path(args.data).resolve(), Path(args.protocol).resolve(), Path(args.out).resolve()
    harness_path = Path(args.harness).resolve()
    harness = versions.load_harness(harness_path)
    presentation_mode = lab_input_view.validate_config(harness["input_view"])
    phase = getattr(args, "phase", "development")
    execution_mode = getattr(args, "execution_mode", "live")
    if phase not in {"development", "validation", "final"} or execution_mode not in {"live", "replay"}:
        raise ValueError("Unsupported campaign phase or execution mode")
    if not ID.fullmatch(run.name):
        raise ValueError("Run directory basename must be a safe campaign ID")
    protocol = read(protocol_path)
    validate_vina_protocol(protocol, live=execution_mode == "live")
    if execution_mode == "live":
        for key, name in (("runner_sha256", "vina_worker.py"), ("adapter_sha256", "run_vina_batch.py"),
                          ("contract_sha256", "vina_contract.py")):
            if sha(ROOT / "scripts" / name) != protocol[key]:
                raise ValueError(f"Protocol pins a different worker source: {name}")
    if protocol.get("tau") is not None or protocol.get("m_min") is not None or protocol.get("seeds") != list(SEEDS):
        raise ValueError("Expected uncalibrated two-seed [17,43] protocol")
    for name, digest in protocol["input_sha256"].items():
        path = (data / name).resolve()
        if not path.is_relative_to(data) or not path.is_file() or sha(path) != digest:
            raise ValueError(f"Protocol input hash mismatch: {name}")
    candidates, evidence = freeze_public_data(data)
    chemical_method, _ = load_chemical_states(data)
    expected_chemical = None if chemical_method is None else {"method": chemical_method}
    if protocol.get("chemical_preparation") != expected_chemical:
        raise ValueError("Chemical preparation differs from the frozen protocol")
    if chemical_method is not None and protocol["input_sha256"].get("chemical_states.json") != sha(data / "chemical_states.json"):
        raise ValueError("Protocol must bind the chemical state registry")
    if execution_mode == "live" and read(data / "manifest.json").get("synthetic") is True:
        raise ValueError("Synthetic fixture data requires replay execution mode")
    run.mkdir(parents=True, exist_ok=False)
    frozen = run / "frozen"
    frozen.mkdir()
    for source, name in [(protocol_path, "protocol.json"), (data / "candidates.json", "candidates.json"), (data / "manifest.json", "manifest.json")]:
        shutil.copyfile(source, frozen / name)
    if chemical_method is not None:
        shutil.copyfile(data / "chemical_states.json", frozen / "chemical_states.json")
    versions.freeze_harness(harness_path, frozen / "harness")
    shutil.copyfile(ROOT / "lab_harness/common_contract.md", frozen / "common_contract.md")
    protected = protected_sources()
    protected_hashes = {name: sha(path) for name, path in protected.items()}
    for name, path in protected.items():
        destination = frozen / "protected" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    for name in WORKER_FILES:
        shutil.copyfile(ROOT / "scripts" / name, frozen / name)
    dump(frozen / "response.schema.json", RESPONSE_SCHEMA)
    dump(frozen / "D0.json", {"candidates": candidates, "evidence": evidence,
         "snapshot_policy": "Only supplied public assay/SMILES/structure features; no prior candidate-level model scores"})
    dataset_hash = canonical_hash({"inputs": protocol["input_sha256"], "public_snapshot": sha(frozen / "D0.json")})
    identity = {"campaign_id": run.name, "phase": phase, "execution_mode": execution_mode,
                "model": args.model, "effort": args.effort, "protocol_hash": sha(protocol_path),
                "dataset_hash": dataset_hash, "harness_hash": harness["harness_hash"],
                "runtime_hash": canonical_hash(protected_hashes)}
    dump(frozen / "campaign.json", identity)
    dump(frozen / "evaluator.json", {"profile": "cdk2_autodock_vina_v1", "limits": LIMITS,
          "tool_id": TOOL, "parameter_profile": PROFILE, "objective": read_h0_objective(),
          "docking_evaluator": evaluator_descriptor(),
          "quality_definition": "Report valid two-seed coverage and qualified median AutoDock Vina model-1 affinity in kcal/mol (lower is better).",
          "minimum_online_batches": 1, "terminal_candidate_limit_separate_from_online": TERMINAL_LIMIT})
    hashes = {str(p.relative_to(frozen)): sha(p) for p in frozen.rglob("*") if p.is_file()}
    state = {**identity, "state_version": 0, "status": "ready", "created_at": now(),
             "presentation_mode": presentation_mode, "source_data_path": str(data),
             "controller_sha256": sha(__file__), "protected_hashes": protected_hashes, "frozen_hashes": hashes,
             "protocol_id": protocol.get("protocol_id", run.name), "construct_id": "1DI8_chain_A",
             "candidates": candidates, "evidence": evidence, "memory": [], "memory_review_events": [],
             "current_shortlist": [], "open_questions": [], "llm_calls": [], "usage_tokens": 0,
             "llm_usage_complete": True, "online_reserved_ids": [], "terminal_reserved_ids": [],
             "worker_versions": {"online": 0, "terminal": 0}, "requests": {}, "pending_request": None,
             "results": {}, "recent_tool_evidence_ids": [], "last_rejection": None,
             "locked_shortlist_sha256": None, "read_evidence_ids": []}
    dump(run / "state.json", state)
    event(run, "init", **identity, controller_sha256=state["controller_sha256"])
    print(json.dumps({"status": "initialized_no_model_called", "run": str(run), "candidate_count": 20,
                      **identity, "limits": LIMITS, "terminal_candidates_separate_limit": TERMINAL_LIMIT}))
    return state


def task_input(state, protocol):
    remaining_calls = LIMITS["llm_calls"] - len(state["llm_calls"])
    final_call = remaining_calls <= 1 or LIMITS["online_candidates"] == len(state["online_reserved_ids"])
    memory = copy.deepcopy(state["memory"])
    for record in memory:
        relevant = [r for r in state["memory_review_events"] if r["memory_id"] == record["memory_id"]]
        if relevant:
            record["review_state"] = relevant[-1]["review_state"]
    return {
        "state_version": state["state_version"], "phase": state["phase"], "harness_version": state["harness_hash"],
        "task_spec": {"target_id": "CHEMBL301", "construct_id": state["construct_id"], "candidate_pool": state["candidates"],
                      "shortlist_size": 5, "objective": read_h0_objective(), "docking_evaluator": evaluator_descriptor(),
                      "allowed_actions": ["submit"] if final_call else ["tool_batch", "read_evidence", "submit"],
                      "requirement": "At least one actual online batch result must be observed before submit; 8 candidates is a cap, not a quota."},
        "protocol_ref": {"protocol_id": state["protocol_id"], "sha256": state["frozen_hashes"]["protocol.json"],
                         "seeds": protocol["seeds"], "docking_evaluator": evaluator_descriptor(), "no_calibrated_tau": True, "score_semantics": protocol.get("score_semantics", {})},
        "tool_catalog": [{"tool_id": TOOL, "version": TOOL_VERSION, "parameter_profiles": [PROFILE],
                          "batch_size_max": 4, "seeds_per_candidate": len(SEEDS), "docking_evaluator": evaluator_descriptor(), "cost": "One candidate slot reserves both CPU Vina jobs, including failures; actual CPU and wall time measured afterwards"}],
        "budget_snapshot": {"remaining_online_candidate_slots": 8 - len(state["online_reserved_ids"]),
                            "reserved_or_used_candidate_ids": state["online_reserved_ids"], "remaining_llm_calls_including_this_one": remaining_calls,
                            "llm_tokens_observed": state["usage_tokens"], "llm_token_soft_limit": 150000,
                            "cpu_wall_seconds_are_measured": True},
        "memory_snapshot": {"memory_version": state["state_version"], "entries": memory, "open_questions": state["open_questions"],
                            "omitted_count": 0, "review_note": "All model memory remains pending semantic review; a valid source ID does not prove the statement."},
        "recent_tool_results": [state["evidence"][eid] for eid in state["recent_tool_evidence_ids"]],
        "evidence_index": list(state["evidence"].values()), "current_shortlist": state["current_shortlist"],
        "requested_evidence": [state["evidence"][eid] for eid in state["read_evidence_ids"]],
        "last_rejection": state["last_rejection"],
        "scientific_context": state["evidence"].get("ev_task_context"),
    }


def read_h0_objective():
    return ("Submit five candidates. Prioritize valid pose coverage for both frozen seeds and lower qualified median "
            "AutoDock Vina affinity (kcal/mol). Use the first ranked mode per seed before pose checks. "
            "Use available public assay provenance, structure diversity and uncertainty as supporting evidence; do not invent absent activity values. "
            "Unknown docking energies remain unknown. Docking does not establish biological activity.")


def validate_response(response, state, only_submit=False):
    validate_schema(response, RESPONSE_SCHEMA)
    if response["input_state_version"] != state["state_version"]:
        raise ValueError("Stale input_state_version")
    known = {r["candidate_id"] for r in state["candidates"]}
    evidence = state["evidence"]
    memory_ids = {r["memory_id"] for r in state["memory"]}
    def refs(ids):
        if len(ids) != len(set(ids)) or not set(ids).issubset(evidence):
            raise ValueError("Unknown/duplicate evidence reference")
    refs(response["evidence_ids"])
    refs(response["read_evidence_ids"])
    shortlist = response["shortlist"]
    if len(shortlist) != 5 or len({r["candidate_id"] for r in shortlist}) != 5:
        raise ValueError("Shortlist must contain exactly five unique candidates")
    for item in shortlist:
        if item["candidate_id"] not in known or not item["evidence_ids"]:
            raise ValueError("Unknown shortlist candidate or absent evidence")
        refs(item["evidence_ids"])
        if not any(evidence[eid].get("candidate_id") == item["candidate_id"] for eid in item["evidence_ids"]):
            raise ValueError("Shortlist evidence must include this candidate's own record")
    assay_ids = {r["assay_id"] for r in state["candidates"]}
    for item in response["memory_delta"]:
        if not item["statement"].strip() or len(item["subject_ids"]) != len(set(item["subject_ids"])) or not set(item["subject_ids"]).issubset(known):
            raise ValueError("Empty memory or unknown subject")
        refs(item["evidence_ids"])
        for name in ["supporting_evidence_ids", "challenging_evidence_ids"]:
            refs(item[name])
            if not set(item[name]).issubset(item["evidence_ids"]):
                raise ValueError("Memory supporting/challenging references not in evidence_ids")
        if item["kind"] == "observation" and not item["evidence_ids"]:
            raise ValueError("Observation needs evidence")
        if len(item["depends_on_memory_ids"]) != len(set(item["depends_on_memory_ids"])) or not set(item["depends_on_memory_ids"]).issubset(memory_ids) or (item["supersedes"] is not None and item["supersedes"] not in memory_ids):
            raise ValueError("Unknown memory dependency or supersedes")
        scope = item["scope"]
        if scope["target_id"] not in (None, "CHEMBL301") or scope["construct_id"] not in (None, state["construct_id"]) or scope["protocol_id"] not in (None, state["protocol_id"]):
            raise ValueError("Memory scope does not match this campaign")
        if not set(scope["assay_ids"]).issubset(assay_ids):
            raise ValueError("Unknown memory assay")
        if any(v != {"tool_id": TOOL, "version": TOOL_VERSION} for v in scope["tool_versions"]):
            raise ValueError("Unknown tool/version in memory")
    action = response["next_action"]
    if only_submit and action != "submit":
        raise ValueError("Only submit is permitted on the final allowed call / exhausted online slots")
    if action == "tool_batch":
        if len(response["tool_requests"]) != 1 or response["read_evidence_ids"] or response["stop_reason"] is not None:
            raise ValueError("tool_batch requires one request, no read IDs or stop reason")
        request = response["tool_requests"][0]
        if not ID.fullmatch(request["request_id"]) or request["tool_id"] != TOOL or request["parameter_profile"] != PROFILE:
            raise ValueError("Invalid tool/profile/request identity")
        ids = request["candidate_ids"]
        if not 1 <= len(ids) <= 4 or len(ids) != len(set(ids)) or not set(ids).issubset(known):
            raise ValueError("Batch requires 1–4 unique known candidates")
        if request["request_id"] in state["requests"] or set(ids) & set(state["online_reserved_ids"]):
            raise ValueError("Request/candidate already reserved; reuse existing result, no rerun")
        if len(ids) + len(state["online_reserved_ids"]) > 8:
            raise ValueError("Online candidate budget exceeded")
    elif action == "read_evidence":
        if not response["read_evidence_ids"] or response["tool_requests"] or response["stop_reason"] is not None:
            raise ValueError("read_evidence requires references only")
    else:
        if response["tool_requests"] or response["read_evidence_ids"] or not response["stop_reason"]:
            raise ValueError("submit requires stop reason and no requests")
        if not any(value["phase"] == "online" for value in state["results"].values()):
            raise ValueError("Prospective campaign requires one observed actual online batch before submit")
    if state["recent_tool_evidence_ids"] and not any(set(m["evidence_ids"]) & set(state["recent_tool_evidence_ids"]) for m in response["memory_delta"]):
        raise ValueError("New tool feedback must be recorded in at least one evidence-linked memory_delta")


def reserve(run, state, ids, request_id, phase):
    if phase not in {"online", "terminal"}:
        raise ValueError("Unknown reservation phase")
    if phase == "terminal":
        locked = read(run / "locked_shortlist.json")
        if (not state["locked_shortlist_sha256"] or sha(run / "locked_shortlist.json") != state["locked_shortlist_sha256"]
                or not 1 <= len(ids) <= LIMITS["batch_candidates"] or len(ids) != len(set(ids))
                or not set(ids).issubset(locked["candidate_ids"])
                or set(ids) & (set(state["online_reserved_ids"]) | set(state["terminal_reserved_ids"]))
                or len(state["terminal_reserved_ids"]) + len(ids) > TERMINAL_LIMIT):
            raise ValueError("Terminal reservation violates lock, no-rerun rule or separate five-candidate cap")
    request = {"schema_version": BATCH_SCHEMA, "phase": phase, "campaign_id": state["campaign_id"],
               "request_id": request_id, "candidate_ids": ids, "input_state_version": state["worker_versions"][phase],
               "protocol_sha256": state["frozen_hashes"]["protocol.json"],
               "candidates_sha256": state["frozen_hashes"]["candidates.json"],
               "manifest_sha256": state["frozen_hashes"]["manifest.json"], "controller_authorized": True}
    if phase == "terminal":
        request["locked_shortlist_sha256"] = state["locked_shortlist_sha256"]
    digest = canonical_hash(request)
    if request_id in state["requests"]:
        if state["requests"][request_id]["canonical_hash"] != digest:
            raise ValueError("Same request ID has different immutable contents")
        return
    state[phase + "_reserved_ids"].extend(ids)
    state["requests"][request_id] = {"request": request, "canonical_hash": digest, "lab_state_version": state["state_version"], "status": "reserved"}
    state["pending_request"] = request_id
    state["status"] = "awaiting_docking"
    dump(run / "pending" / request_id / "request.json", request)


def lock_shortlist(run, state, reason):
    if state.get("locked_shortlist_sha256"):
        raise ValueError("Shortlist is already locked")
    if len(state["current_shortlist"]) != 5 or not any(value["phase"] == "online" for value in state["results"].values()):
        state["status"] = "halted_without_shortlist"
        return
    lock = {"campaign_id": state["campaign_id"], "candidate_ids": [r["candidate_id"] for r in state["current_shortlist"]], "locked_at": now()}
    dump(run / "locked_shortlist.json", lock)
    dump(run / "locked_shortlist_detail.json", {**lock, "shortlist": state["current_shortlist"], "reason": reason, "state_version": state["state_version"]})
    state["locked_shortlist_sha256"] = sha(run / "locked_shortlist.json")
    state["status"] = "shortlist_locked"


def apply_response(run, state, response):
    updated = copy.deepcopy(state)
    validate_response(response, updated, only_submit=len(updated["llm_calls"]) >= 6 or len(updated["online_reserved_ids"]) >= 8)
    for proposal in response["memory_delta"]:
        mid = f"mem_{len(updated['memory']) + 1:04d}"
        record = {**copy.deepcopy(proposal), "memory_id": mid, "review_state": "pending",
                  "derived_by": "Lab", "at": now(), "state_version": updated["state_version"] + 1}
        updated["memory"].append(record)
        old = proposal["supersedes"]
        if old:
            updated["memory_review_events"].append({"memory_id": old, "review_state": "superseded", "by": mid})
            for dependent in updated["memory"]:
                if old in dependent["depends_on_memory_ids"]:
                    updated["memory_review_events"].append({"memory_id": dependent["memory_id"], "review_state": "needs_review", "by": mid})
    updated["current_shortlist"] = response["shortlist"]
    updated["open_questions"] = response["open_questions"]
    updated["read_evidence_ids"] = response["read_evidence_ids"]
    updated["recent_tool_evidence_ids"] = []
    updated["last_rejection"] = None
    updated["state_version"] += 1
    if response["next_action"] == "tool_batch":
        request = response["tool_requests"][0]
        reserve(run, updated, request["candidate_ids"], request["request_id"], "online")
    elif response["next_action"] == "submit":
        lock_shortlist(run, updated, response["stop_reason"])
    else:
        updated["status"] = "ready"
    return updated


def write_turn_input(run, folder, payload, state):
    """Save original input and exactly the JSON view appended to the prompt."""
    dump(folder / "input.json", payload)
    mode = lab_input_view.validate_config(read(run / "frozen" / PRESENTATION_FILE))
    view = lab_input_view.render(payload, mode)
    audit = lab_input_view.audit_render(payload, view, mode)
    (folder / "view.json").write_text(view)
    audit.update(input_json_sha256=sha(folder / "input.json"),
                 presentation_config_sha256=state["frozen_hashes"][PRESENTATION_FILE])
    dump(folder / "render_audit.json", audit)
    prompt = (run / "frozen/common_contract.md").read_text() + "\n" + (run / "frozen/harness/lab_planner.md").read_text()
    prompt += "\nYou are a stdin-only decision role. Do not call any tools, inspect files, use web/apps or delegate. Respond only with the schema JSON. Claims stay pending review. "
    prompt += "Use only supplied evidence. Tool requests execute later through the Controller. Record new feedback in sourced memory_delta. The final allowed call must submit.\nTASK_INPUT_JSON:\n" + view
    return prompt


def turn(args):
    run = Path(args.run).resolve()
    with locked(run):
        state = read(run / "state.json")
        assert_frozen(run, state)
        response_source = getattr(args, "response", None)
        replay = state["execution_mode"] == "replay"
        if response_source is not None and not replay:
            raise ValueError("Response injection is allowed only in replay mode")
        if state["pending_request"] or state["locked_shortlist_sha256"] or state["status"] in {"needs_audit", "halted_without_shortlist", "completed"}:
            print(json.dumps({"status": state["status"], "pending_request": state["pending_request"], "model_called": False}))
            return state
        if any(call["status"] == "running" for call in state["llm_calls"]):
            raise ValueError("Prior model call interrupted; usage/state audit required before another call")
        if len(state["llm_calls"]) >= LIMITS["llm_calls"] or state["usage_tokens"] >= LIMITS["soft_total_tokens"]:
            lock_shortlist(run, state, "Controller resource limit; last validated shortlist retained")
            dump(run / "state.json", state)
            print(json.dumps({"status": state["status"], "model_called": False}))
            return state
        if replay and response_source is None:
            raise ValueError("Replay step requires --response; no model will be called")
        if replay:
            # Read before reservation so a missing file does not consume a decision slot.
            response = read(response_source)
        payload = task_input(state, read(run / "frozen/protocol.json"))
        number = len(state["llm_calls"]) + 1
        folder = run / "turns" / f"{number:03d}"
        folder.mkdir(parents=True, exist_ok=False)
        prompt = write_turn_input(run, folder, payload, state)
        call = {"number": number, "status": "running", "started_at": now(), "cost_usd": None,
                "usage": None, "backend": "replay" if replay else "codex", "model_called": not replay,
                "accounting_note": "Synthetic replay: no model tokens incurred" if replay else "Observed token usage is not a verified dollar invoice; cached/reasoning counts are not added twice"}
        state["llm_calls"].append(call)
        dump(run / "state.json", state)
        event(run, "decision_call_reserved", number=number, execution_mode=state["execution_mode"])
        if replay:
            (folder / "prompt.txt").write_text(prompt)
            dump(folder / "response.json", response)
            result = {"response": response, "usage": None, "usage_complete": True, "error": None,
                      "wall_seconds": 0.0, "returncode": None, "backend": "replay"}
            dump(folder / "invocation.json", {"backend": "replay", "synthetic": True, "response_source": str(Path(response_source).resolve()), "model_called": False})
        else:
            try:
                result = transport.invoke(prompt, RESPONSE_SCHEMA, folder, state["model"], state["effort"], timeout=LIMITS["llm_timeout_seconds"])
            except Exception as exc:
                result = {"response": None, "usage": None, "usage_complete": False, "error": str(exc),
                          "wall_seconds": None, "returncode": None, "backend": "codex"}
        call.update(finished_at=now(), wall_seconds=result["wall_seconds"], returncode=result["returncode"], usage=result["usage"])
        usage = result["usage"]
        measured = isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in ["input_tokens", "output_tokens"])
        call["usage_accounting_complete"] = replay or (measured and result["usage_complete"] and not result["error"])
        if measured and not replay:
            state["usage_tokens"] += usage["input_tokens"] + usage["output_tokens"]
        if not call["usage_accounting_complete"]:
            state["llm_usage_complete"] = False
        call["status"] = "rejected"
        try:
            if result["error"]:
                raise ValueError(result["error"])
            if not replay and not call["usage_accounting_complete"]:
                raise ValueError("Completed call lacks complete actual token usage")
            state = apply_response(run, state, result["response"])
            state["llm_calls"][-1]["status"] = "accepted"
            event(run, "model_decision_accepted", number=number, action=result["response"]["next_action"], state_version=state["state_version"])
        except Exception as exc:
            state["last_rejection"] = {"call_number": number, "reason": str(exc), "budget_charged": True}
            dump(folder / "rejection.json", state["last_rejection"])
            event(run, "model_decision_rejected", **state["last_rejection"])
            if not replay and not call["usage_accounting_complete"]:
                state["status"] = "needs_audit"
        dump(folder / "call.json", state["llm_calls"][-1])
        dump(run / "state.json", state)
        print(json.dumps({"status": state["status"], "call": number, "call_status": state["llm_calls"][-1]["status"],
                          "model_called": not replay, "rejection": state["last_rejection"], "pending_request": state["pending_request"], "usage_tokens": state["usage_tokens"]}))
        return state


def aggregate_jobs(cid, jobs):
    rows = sorted([row for row in jobs if row["candidate_id"] == cid], key=lambda row: row["seed"])
    paired = [row["seed"] for row in rows] == list(SEEDS)
    scored = paired and all(row.get("scores") is not None for row in rows)
    valid = scored and all(row["status"] == "completed" and row.get("pose_valid") is True for row in rows)
    record = {"candidate_id": cid, "completed_repeats": sum(row["status"] == "completed" for row in rows),
              "valid_repeats": sum(row.get("pose_valid") is True for row in rows), "two_valid_repeats": bool(valid),
              "statuses": ";".join(row["status"] for row in rows)}
    for field in SCORES:
        raw = statistics.median(row["scores"][field] for row in rows) if scored else None
        record["raw_median_" + field] = raw
        record["qualified_median_" + field] = raw if valid else None
        record["seed_range_" + field] = max(row["scores"][field] for row in rows) - min(row["scores"][field] for row in rows) if scored else None
    return record


def validate_batch(batch, run, state):
    batch, run = Path(batch).resolve(), Path(run).resolve()
    request = read(batch / "request.json")
    rid = request["request_id"]
    if rid not in state["requests"] or canonical_hash(request) != state["requests"][rid]["canonical_hash"]:
        raise ValueError("Batch does not match a Controller reservation")
    hashes = read(batch / "artifact_hashes.json")
    actual_files = {str(path.relative_to(batch)) for path in batch.rglob("*") if path.is_file() and path.name != "artifact_hashes.json"}
    if set(hashes) != actual_files:
        raise ValueError("Artifact manifest does not cover exactly the batch files")
    for name, digest in hashes.items():
        path = (batch / name).resolve()
        if not path.is_relative_to(batch) or not path.is_file() or sha(path) != digest:
            raise ValueError(f"Batch artifact hash mismatch: {name}")
    if sha(batch / "protocol.json") != request["protocol_sha256"]:
        raise ValueError("Batch protocol mismatch")
    summary = read(batch / "batch_summary.json")
    if state["execution_mode"] == "live" and summary.get("synthetic") is True:
        raise ValueError("Synthetic batch cannot enter a live campaign")
    if request.get("schema_version") != BATCH_SCHEMA or summary.get("schema_version") != BATCH_SCHEMA or summary.get("adapter_sha256") != state["frozen_hashes"]["run_vina_batch.py"]:
        raise ValueError("Batch schema or frozen Vina adapter changed")
    for name in ["campaign_id", "phase", "request_id", "protocol_sha256", "candidates_sha256", "manifest_sha256"]:
        if summary.get(name) != request[name]:
            raise ValueError(f"Batch summary identity mismatch: {name}")
    if summary.get("request_sha256") != sha(batch / "request.json") or summary.get("state_version") != request["input_state_version"] + 1:
        raise ValueError("Batch request hash/state version mismatch")
    if request["phase"] == "terminal" and (request["locked_shortlist_sha256"] != state["locked_shortlist_sha256"] or sha(batch / "locked_shortlist.json") != state["locked_shortlist_sha256"]):
        raise ValueError("Terminal batch shortlist mismatch")
    jobs = [read(path) for path in sorted((batch / "jobs").glob("*/result.json"))]
    expected = {(cid, seed) for cid in request["candidate_ids"] for seed in SEEDS}
    observed = [(row["candidate_id"], row["seed"]) for row in jobs]
    if len(observed) != len(expected) or set(observed) != expected:
        raise ValueError("Batch lacks exact requested candidate/seed coverage")
    if sorted(summary["jobs"], key=lambda row: (row["candidate_id"], row["seed"])) != sorted(jobs, key=lambda row: (row["candidate_id"], row["seed"])):
        raise ValueError("Batch summary jobs do not match raw job records")
    protocol = read(run / "frozen/protocol.json")
    config = protocol.get("posebusters_configs", {}).get("dock")
    expected_checks = None if config is None else {
        module.get("rename_outputs", {}).get(name, name + module.get("rename_suffix", "")).lower().replace(" ", "_")
        for module in config["modules"] for name in module.get("chosen_binary_test_output", [])
    }
    by_id = {c["candidate_id"]: c for c in state["candidates"]}
    for row in jobs:
        folder = batch / "jobs" / f"{row['candidate_id']}_s{row['seed']}"
        spec = read(folder / "spec.json")
        if spec["protocol"] != protocol or spec["seed"] != row["seed"] or spec["candidate"]["candidate_id"] != row["candidate_id"] or spec["candidate"]["smiles"] != by_id[row["candidate_id"]]["smiles"] or spec["kind"] != "candidate":
            raise ValueError("Job inputs or protocol differ from frozen inputs")
        if row["status"] not in {"completed", "preparation_failed", "docking_failed", "docking_timeout", "pose_parse_failed", "pose_check_failed", "worker_error", "worker_timeout"}:
            raise ValueError("Unknown job status")
        cpu = row.get("cpu_seconds")
        if cpu is not None and (type(cpu) not in (int, float) or not math.isfinite(cpu) or cpu < 0):
            raise ValueError("Invalid actual job CPU time")
        if row.get("pose_valid") is not None and type(row["pose_valid"]) is not bool:
            raise ValueError("Invalid pose validity type")
        if row.get("scores") is not None and (set(row["scores"]) != set(SCORES) or not all(type(v) in [int, float] and math.isfinite(v) for v in row["scores"].values())):
            raise ValueError("Invalid/nonfinite numerical scores")
        if row["status"] == "completed" and (type(row.get("returncode")) is not int or row["returncode"] != 0 or row.get("scores") is None):
            raise ValueError("Completed job lacks a real successful score")
        if row["status"] == "completed":
            checks = row.get("pose_checks", {})
            if not checks or any(type(v) is not bool and v is not None for v in checks.values()):
                raise ValueError("Completed job lacks PoseBusters checks")
            if expected_checks is not None and set(checks) != expected_checks:
                raise ValueError("PoseBusters checks differ from frozen protocol coverage")
            if type(row.get("identity_preserved")) is not bool:
                raise ValueError("Completed job lacks a boolean molecular identity check")
            with (folder / "posebusters.csv").open() as stream:
                pb_rows = list(csv.DictReader(stream))
            if len(pb_rows) != 1:
                raise ValueError("Expected exactly one selected-pose PoseBusters row")
            for name, value in checks.items():
                if name not in pb_rows[0]:
                    raise ValueError("PoseBusters selected check missing from raw CSV")
                observed = {"True": True, "False": False, "": None}.get(pb_rows[0][name], "invalid")
                if observed != value:
                    raise ValueError("PoseBusters checks differ from raw CSV")
            structural = list(checks.values()) + [row.get("identity_preserved")]
            expected_valid = False if False in structural else (None if None in structural else True)
            if row.get("pose_valid") is not expected_valid:
                raise ValueError("Pose validity does not follow raw checks and identity")
    cpu_total = summary.get("worker_cpu_seconds")
    if type(cpu_total) not in (int, float) or not math.isfinite(cpu_total) or cpu_total < 0:
        raise ValueError("Invalid actual batch CPU time")
    if not math.isclose(cpu_total, sum(row.get("cpu_seconds") or 0 for row in jobs), rel_tol=1e-9, abs_tol=1e-6) or summary.get("cpu_accounting_complete") is not all(row.get("cpu_seconds") is not None for row in jobs):
        raise ValueError("Batch CPU accounting differs from raw jobs")
    if type(summary.get("wall_seconds")) not in [int, float] or not math.isfinite(summary["wall_seconds"]) or summary["wall_seconds"] < 0:
        raise ValueError("Invalid actual batch wall time")
    if state["execution_mode"] == "live":
        version_receipt = {key: protocol[key] for key in ("engine", "vina_version", "vina_sha256")}
        receipt = read(batch / "engine.json")
        if any(receipt.get(key) != value for key, value in version_receipt.items()):
            raise ValueError("Batch engine receipt differs from frozen Vina executable")
        for field in ("runner_sha256", "contract_sha256"):
            if summary.get(field) != protocol[field]:
                raise ValueError(f"Vina batch source changed: {field}")
        for row in jobs:
            if row.get("synthetic") or any(row.get(key) != value for key, value in version_receipt.items()):
                raise ValueError("Job receipt does not identify the frozen AutoDock Vina executable")
            if row["status"] == "completed":
                folder = batch / "jobs" / f"{row['candidate_id']}_s{row['seed']}"
                modes = parse_vina_poses((folder / "poses.pdbqt").read_text())
                if row.get("selected_model") != 1 or len(modes) != row.get("pose_count") or modes[0] != row["scores"]["vina_affinity"]:
                    raise ValueError("Job score differs from native AutoDock Vina MODEL 1")
    records = [aggregate_jobs(cid, jobs) for cid in request["candidate_ids"]]
    provided = {r["candidate_id"]: r for r in summary["candidates"]}
    if len(provided) != len(summary["candidates"]) or set(provided) != set(request["candidate_ids"]):
        raise ValueError("Summary candidate identities differ from request")
    for record in records:
        if any(provided.get(record["candidate_id"], {}).get(key) != value for key, value in record.items()):
            raise ValueError("Candidate aggregate differs from raw jobs")
    return request, summary, jobs, records


def ingest(args):
    run, batch = Path(args.run).resolve(), Path(args.batch).resolve()
    with locked(run):
        state = read(run / "state.json")
        assert_frozen(run, state)
        request, summary, jobs, records = validate_batch(batch, run, state)
        rid = request["request_id"]
        digest = sha(batch / "artifact_hashes.json")
        previous = state["requests"][rid].get("ingested_artifacts_sha256")
        if previous:
            if previous != digest:
                raise ValueError("Different contents for an already-ingested request")
            print(json.dumps({"status": "already_ingested", "request_id": rid}))
            return
        if state["pending_request"] != rid:
            raise ValueError("Batch is not the currently pending request")
        destination = run / "batches" / rid
        if destination.exists():
            raise ValueError("Batch archive already exists without committed state; audit before recovery")
        shutil.copytree(batch, destination)
        recent = []
        for record in records:
            cid = record["candidate_id"]
            evidence_id = f"ev_tool_{rid}_{cid}"
            candidate_jobs = [row for row in jobs if row["candidate_id"] == cid]
            evidence = {"evidence_id": evidence_id, "kind": "synthetic_computational_results" if state["execution_mode"] == "replay" else "actual_computational_results", "candidate_id": cid,
                        "synthetic": state["execution_mode"] == "replay",
                        "phase": request["phase"], "request_id": rid, "protocol_id": state["protocol_id"],
                        "protocol_sha256": request["protocol_sha256"], "tool_version": TOOL_VERSION, **record,
                        "jobs": [{k: row.get(k) for k in ["seed", "status", "scores", "pose_valid", "pose_checks", "identity_preserved", "error", "cpu_seconds", "wall_seconds", "docking_wall_seconds", "selected_model"]} for row in candidate_jobs],
                        "artifact_ref": f"batches/{rid}", "official_affinity_pass": None, "docking_evaluator": evaluator_descriptor()}
            state["evidence"][evidence_id] = evidence
            state["results"][cid] = evidence
            recent.append(evidence_id)
        state["requests"][rid].update(status="ingested", ingested_artifacts_sha256=digest,
            actual_worker_cpu_seconds=summary.get("worker_cpu_seconds"), actual_wall_seconds=summary.get("wall_seconds"),
            cpu_accounting_complete=summary.get("cpu_accounting_complete"))
        state["worker_versions"][request["phase"]] = summary["state_version"]
        state["pending_request"] = None
        state["state_version"] += 1
        state["recent_tool_evidence_ids"] = recent if request["phase"] == "online" else []
        state["status"] = "shortlist_locked" if state["locked_shortlist_sha256"] else "ready"
        if any(row["status"] in {"worker_error", "worker_timeout"} for row in jobs):
            state["status"] = "needs_audit"
        dump(run / "state.json", state)
        event(run, "batch_ingested", request_id=rid, phase=request["phase"], artifacts_sha256=digest, state_version=state["state_version"])
        print(json.dumps({"status": state["status"], "request_id": rid, "records": records}))


def qualified_docking_energies(rows):
    """Return one finite docking energy per candidate with two qualified poses."""
    values = []
    for row in rows:
        energy = row[SCORE_FIELD]
        if row["two_valid_repeats"]:
            if type(energy) not in (int, float) or not math.isfinite(energy):
                raise ValueError("Valid two-seed result lacks a finite qualified docking energy")
            values.append(energy)
        elif energy is not None:
            raise ValueError("Unqualified result cannot have a qualified docking energy")
    return values


def finalize(args):
    run = Path(args.run).resolve()
    with locked(run):
        state = read(run / "state.json")
        assert_frozen(run, state)
        if state["status"] == "completed":
            return read(run / "evaluation.json")
        if not state["locked_shortlist_sha256"]:
            raise ValueError("Shortlist must be locked before terminal evaluation")
        if state["pending_request"] or state["status"] == "needs_audit":
            print(json.dumps({"status": state["status"], "pending_request": state["pending_request"]}))
            return
        shortlist = read(run / "locked_shortlist.json")["candidate_ids"]
        missing = [cid for cid in shortlist if cid not in state["results"]]
        if missing:
            rid = f"terminal_{state['worker_versions']['terminal'] + 1:03d}"
            reserve(run, state, missing[:4], rid, "terminal")
            dump(run / "state.json", state)
            event(run, "terminal_evaluation_reserved", request_id=rid, candidate_ids=missing[:4])
            print(json.dumps({"status": "awaiting_terminal_docking", "request": str(run / "pending" / rid / "request.json"), "locked_shortlist": str(run / "locked_shortlist.json")}))
            return
        result = [state["results"][cid] for cid in shortlist]
        energies = qualified_docking_energies(result)
        report = {"campaign_id": state["campaign_id"], "status": "completed",
                  "output_scope": "pilot_shortlist", "formal_min_final_compounds": MIN_FINAL_COMPOUNDS,
                  "formal_final_list_requirement_satisfied": False, "lab_agent_ran": state["execution_mode"] == "live",
                  "phase": state["phase"], "execution_mode": state["execution_mode"], "synthetic": state["execution_mode"] == "replay",
                  "harness_hash": state["harness_hash"], "protocol_hash": state["protocol_hash"], "dataset_hash": state["dataset_hash"],
                  "runtime_hash": state["runtime_hash"], "model": state["model"], "effort": state["effort"], "presentation_mode": state["presentation_mode"],
                  "presentation_config_sha256": state["frozen_hashes"][PRESENTATION_FILE],
                  "presentation_change_applied": state["presentation_mode"] != "expanded_json_v1",
                  "meta_optimizer_executed_in_this_campaign": False, "improvement_demonstrated": None,
                  "official_affinity_pass_threshold": None, "official_affinity_pass_rate": None,
                  "locked_shortlist_sha256": state["locked_shortlist_sha256"],
                  "shortlist": shortlist, "paired_pose_valid_candidates": sum(r["two_valid_repeats"] for r in result),
                  "shortlist_size": 5, "continuous_results": result,
                  "docking_evaluator": evaluator_descriptor(), "qualified_docking_energy_count": len(energies),
                  "mean_qualified_docking_energy_kcal_mol": statistics.mean(energies) if energies else None,
                  "online_unique_candidates": len(state["online_reserved_ids"]), "terminal_unique_candidates": len(state["terminal_reserved_ids"]),
                  "online_candidate_limit": LIMITS["online_candidates"], "terminal_candidate_limit": TERMINAL_LIMIT,
                  "model_calls": state["llm_calls"], "observed_input_plus_output_tokens": state["usage_tokens"],
                  "llm_usage_accounting_complete": state["llm_usage_complete"], "llm_actual_cost_usd": None,
                  "numeric_batches": state["requests"], "memory_entries": len(state["memory"]),
                  "execution_policy": "No cross-campaign result caching. Terminal evaluation reuses observed online results and computes only missing locked-shortlist candidates. Replay is synthetic.",
                  "limitations": ["Synthetic replay exercises software only; no scientific result or model usage is claimed" if state["execution_mode"] == "replay" else "Single CDK2 campaign; improvement requires separate paired validation", "Uncalibrated continuous computational scores, no biological efficacy inference",
                                  "Tool disabling and event audit are used; no claim of OS filesystem read isolation", "LLM token limit is soft; call count and timeout are enforced; monetary costs are unavailable",
                                  "Scientific memory content remains pending semantic review; source validation does not prove scientific correctness"]}
        dump(run / "evaluation.json", report)
        state["status"] = "completed"
        dump(run / "state.json", state)
        event(run, "campaign_complete", evaluation_sha256=sha(run / "evaluation.json"))
        dump(run / "artifact_hashes.json", {str(p.relative_to(run)): sha(p) for p in sorted(run.rglob("*")) if p.is_file() and p.name not in {".lock", "artifact_hashes.json"}})
        print(json.dumps({"status": "completed", "paired_pose_valid_candidates": report["paired_pose_valid_candidates"], "shortlist_size": 5,
                          "mean_qualified_docking_energy_kcal_mol": report["mean_qualified_docking_energy_kcal_mol"],
                          "online_candidates": report["online_unique_candidates"], "terminal_candidates": report["terminal_unique_candidates"], "evaluation": str(run / "evaluation.json")}))
        return report
