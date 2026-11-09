"""One auditable Meta proposal and a prospective paired development gate.

Meta changes a declarative harness only. No generated Python or shell is executed.
The gate measures computational coverage/score and observed tokens, not efficacy.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics

from . import controller as c
from . import transport, versions
from .docking import TOOL_VERSION, evaluator_descriptor

DEFAULT_RULES = {
    "min_pairs": 2,
    "min_mean_pose_valid_gain": 0.2,
    "min_mean_docking_energy_gain_kcal_mol": 0.1,
    "max_mean_docking_energy_loss_kcal_mol": 0.1,
    "min_token_reduction": 0.10,
}
S = {"type": "string"}
PROPOSAL_SCHEMA = c.obj({
    "parent_harness_hash": S,
    "input_packet_sha256": S,
    "decision": {"type": "string", "enum": ["propose_change", "no_change"]},
    "diagnosis": S,
    "evidence_refs": c.arr(S),
    "hypothesis": S,
    "replacement_lab_planner": {"type": ["string", "null"]},
    "input_view_mode": {"type": ["string", "null"], "enum": [None, "expanded_json_v1", "compact_json_v1", "lossless_deduplicated_json_v1"]},
    "regression_risks": c.arr(S),
})
META_CONTRACT = """You are the RSI-Lab Meta agent. Use only this completed development
packet. Diagnose one mechanism and propose one small, testable harness revision,
or no_change. Cite exact packet evidence_refs. You may replace lab_planner.md and
choose a supported lossless input representation; no other modification is allowed.
Keep the Lab response JSON contract, scientific uncertainty, source citations,
fixed objective, budgets, numerical tools, and candidate independence intact.
The protected docking evaluator prefers lower two-seed qualified median AutoDock Vina
affinity in kcal/mol. Use ranked MODEL 1 for each seed. Compare
qualified docking energy alongside valid-pose coverage.
Do not encode candidate IDs, assay answers, scores, or development-specific rankings
in the new policy. Do not change the evaluator, data, runtime, tool profile, or
common contract. Prompts are editable policy, never authority over those constraints.
A proposal is not a validated improvement. State its hypothesis and regression risks.
Do not use tools, access files, delegate, or browse. Return only the schema JSON.
"""


def _tree_hashes(folder):
    folder = Path(folder)
    return {str(p.relative_to(folder)): c.sha(p) for p in sorted(folder.rglob("*"))
            if p.is_file() and p.name not in {".lock", "artifact_hashes.json"}}


def completed_run(path, phase=None):
    """Require a frozen, fully archived campaign, not caller-supplied metrics."""
    run = Path(path).resolve()
    state = c.read(run / "state.json")
    c.assert_frozen(run, state)
    if state["status"] != "completed" or state.get("pending_request"):
        raise ValueError("A fully completed campaign is required")
    if phase and state.get("phase") != phase:
        raise ValueError(f"Expected a {phase} campaign")
    if c.read(run / "artifact_hashes.json") != _tree_hashes(run):
        raise ValueError("Completed campaign archive changed")
    report = c.read(run / "evaluation.json")
    identity_keys = ("campaign_id", "phase", "execution_mode", "model", "effort", "protocol_hash", "dataset_hash", "harness_hash", "runtime_hash", "presentation_mode")
    if report.get("status") != "completed" or any(report.get(key) != state[key] for key in identity_keys):
        raise ValueError("Evaluation identity differs from the completed state")
    if report.get("synthetic") is not (state["execution_mode"] == "replay"):
        raise ValueError("Evaluation synthetic status differs from campaign")
    shortlist = c.read(run / "locked_shortlist.json")["candidate_ids"]
    if report.get("shortlist_size") != c.LIMITS["shortlist_size"] or len(shortlist) != c.LIMITS["shortlist_size"] or len(set(shortlist)) != len(shortlist):
        raise ValueError("Evaluation shortlist denominator differs from fixed evaluator")
    rows = [state["results"][cid] for cid in shortlist]
    if report["shortlist"] != shortlist or report["continuous_results"] != rows:
        raise ValueError("Evaluation is inconsistent with locked results")
    if report["paired_pose_valid_candidates"] != sum(r["two_valid_repeats"] for r in rows):
        raise ValueError("Evaluation coverage differs from tool results")
    # Rebuild result projections from raw receipts rather than trusting mutually
    # consistent state/report edits. The model never supplies these scores.
    rebuilt = {}
    for rid, reservation in state["requests"].items():
        batch = run / "batches" / rid
        request, summary, jobs, records = c.validate_batch(batch, run, state)
        if reservation.get("status") != "ingested" or reservation.get("ingested_artifacts_sha256") != c.sha(batch / "artifact_hashes.json"):
            raise ValueError("Completed campaign has an uncommitted batch receipt")
        for record in records:
            cid = record["candidate_id"]
            if cid in rebuilt:
                raise ValueError("Candidate evaluated more than once in campaign")
            eid = f"ev_tool_{rid}_{cid}"
            candidate_jobs = [row for row in jobs if row["candidate_id"] == cid]
            evidence = {"evidence_id": eid, "kind": "synthetic_computational_results" if state["execution_mode"] == "replay" else "actual_computational_results",
                        "candidate_id": cid, "synthetic": state["execution_mode"] == "replay",
                        "phase": request["phase"], "request_id": rid, "protocol_id": state["protocol_id"],
                        "protocol_sha256": request["protocol_sha256"], "tool_version": TOOL_VERSION, **record,
                        "jobs": [{k: row.get(k) for k in ["seed", "status", "scores", "pose_valid", "pose_checks", "identity_preserved", "error", "cpu_seconds", "wall_seconds", "docking_wall_seconds", "selected_model"]} for row in candidate_jobs],
                        "artifact_ref": f"batches/{rid}", "official_affinity_pass": None, "docking_evaluator": evaluator_descriptor()}
            if state["evidence"].get(eid) != evidence:
                raise ValueError("Evidence projection differs from raw batch receipts")
            rebuilt[cid] = evidence
    if state["results"] != rebuilt:
        raise ValueError("Result projection differs from raw batch receipts")
    descriptor = evaluator_descriptor()
    if report.get("docking_evaluator") != descriptor or c.read(run / "frozen/evaluator.json").get("docking_evaluator") != descriptor:
        raise ValueError("Docking evaluator identity differs from the fixed AutoDock Vina evaluator")
    energies = c.qualified_docking_energies(rows)
    if report.get("qualified_docking_energy_count") != len(energies) or report.get("mean_qualified_docking_energy_kcal_mol") != (statistics.mean(energies) if energies else None):
        raise ValueError("Evaluation mean/count differs from qualified docking energies")
    calls = state["llm_calls"]
    expected_turns = {f"{number:03d}" for number in range(1, len(calls) + 1)}
    if {path.name for path in (run / "turns").iterdir()} != expected_turns:
        raise ValueError("Model call ledger differs from archived turns")
    tokens, complete = 0, True
    for number, call in enumerate(calls, 1):
        folder = run / "turns" / f"{number:03d}"
        if c.read(folder / "call.json") != call or call["number"] != number or call["status"] not in {"accepted", "rejected"}:
            raise ValueError("Model call ledger differs from archived call receipt")
        if state["execution_mode"] == "replay":
            invocation = c.read(folder / "invocation.json")
            if invocation.get("backend") != "replay" or invocation.get("synthetic") is not True or call["model_called"] or call["usage"] is not None:
                raise ValueError("Replay call cannot claim live inference or usage")
            call_complete = True
        else:
            receipt = c.read(folder / "transport_result.json")
            usage = receipt["usage"]
            if c.read(folder / "usage.json") != {"usage": usage, "usage_complete": receipt["usage_complete"]} or call["usage"] != usage:
                raise ValueError("Model usage differs from transport receipt")
            if receipt["backend"] != "codex" or call["backend"] != "codex" or not call["model_called"]:
                raise ValueError("Live call lacks Codex transport provenance")
            measured = isinstance(usage, dict) and all(type(usage.get(key)) is int and usage[key] >= 0 for key in ["input_tokens", "output_tokens"])
            if measured:
                tokens += usage["input_tokens"] + usage["output_tokens"]
            call_complete = bool(measured and receipt["usage_complete"] and not receipt["error"])
        if call.get("usage_accounting_complete") is not call_complete:
            raise ValueError("Call accounting completeness differs from transport")
        complete &= call_complete
    if report["model_calls"] != calls or state["usage_tokens"] != tokens or report["observed_input_plus_output_tokens"] != tokens:
        raise ValueError("Campaign token totals differ from turn receipts")
    if state["llm_usage_complete"] is not complete or report["llm_usage_accounting_complete"] is not complete:
        raise ValueError("Campaign accounting completeness differs from turn receipts")
    return run, state, report


def prepare(args):
    run, state, report = completed_run(args.development_run, "development")
    parent = versions.load_harness(run / "frozen" / "harness")
    rules = {**DEFAULT_RULES, "min_pairs": args.min_pairs}
    if type(args.min_pairs) is not int or args.min_pairs < 1:
        raise ValueError("min_pairs must be positive")
    evidence = {"evaluation.json": report,
                "memory": state["memory"],
                "memory_review_events": state["memory_review_events"]}
    for path in sorted((run / "turns").glob("*/*")):
        if path.name in {"input.json", "response.json", "usage.json", "rejection.json", "render_audit.json"}:
            evidence[str(path.relative_to(run))] = c.read(path)
    packet = {"schema_version": "lab_meta_packet_v1", "phase": "development",
              "parent_harness_hash": parent["harness_hash"],
              "development_run": str(run), "development_dataset_hash": state["dataset_hash"],
              "development_candidate_ids": [r["candidate_id"] for r in state["candidates"]],
              "execution_mode": state["execution_mode"], "model": state["model"], "effort": state["effort"],
              "parent_policy": (run / "frozen/harness/lab_planner.md").read_text(),
              "parent_input_view": parent["input_view"], "rules": rules,
              "evidence_refs": list(evidence), "evidence": evidence}
    if len(c.encoded(packet)) > 2 * 1024 * 1024:
        raise ValueError("Development packet exceeds 2 MiB; explicitly select a smaller completed campaign")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    versions.freeze_harness(run / "frozen/harness", out / "parent")
    c.dump(out / "input.json", packet)
    c.dump(out / "rules.json", rules)
    c.dump(out / "proposal.schema.json", PROPOSAL_SCHEMA)
    prompt = META_CONTRACT + "\nINPUT_PACKET_SHA256: " + c.sha(out / "input.json") + "\n" + c.encoded(packet).decode()
    (out / "prompt.txt").write_text(prompt)
    frozen = {name: c.sha(out / name) for name in ("input.json", "rules.json", "proposal.schema.json", "prompt.txt")}
    c.dump(out / "meta.json", {"schema_version": "lab_meta_workspace_v1", "status": "prepared",
           "created_at": c.now(), "parent_harness_hash": parent["harness_hash"], "frozen_hashes": frozen,
           "development_artifacts_sha256": c.sha(run / "artifact_hashes.json"),
           "execution_mode": state["execution_mode"], "max_model_attempts": 1,
           "model_attempts": 0, "model": state["model"], "effort": state["effort"]})
    return {"status": "prepared", "workspace": str(out), "parent_harness_hash": parent["harness_hash"], "model_called": False}


def workspace(path):
    root = Path(path).resolve()
    meta = c.read(root / "meta.json")
    for name, digest in meta["frozen_hashes"].items():
        if c.sha(root / name) != digest:
            raise ValueError(f"Prepared Meta input changed: {name}")
    if versions.load_harness(root / "parent")["harness_hash"] != meta["parent_harness_hash"]:
        raise ValueError("Parent harness changed")
    if meta.get("validation_plan_sha256") and c.sha(root / "validation_plan.json") != meta["validation_plan_sha256"]:
        raise ValueError("Frozen validation plan changed")
    return root, meta, c.read(root / "input.json")


def propose(args):
    root, _, _ = workspace(args.workspace)
    with c.locked(root):
        root, meta, packet = workspace(root)
        if meta["status"] != "prepared":
            raise ValueError("Meta workspace already attempted; no implicit retry")
        replay = getattr(args, "response", None)
        if (meta["execution_mode"] == "replay") != bool(replay):
            raise ValueError("Replay workspace requires --response; live workspace forbids replay proposals")
        meta.update(status="running", model_attempts=0 if replay else 1, started_at=c.now())
        c.dump(root / "meta.json", meta)  # Persist the attempt before any process.
        try:
            if replay:
                response = c.read(replay)
                c.dump(root / "proposal.json", response)
                receipt = {"backend": "replay", "usage": None, "usage_complete": False,
                           "error": None, "synthetic": True, "model_called": False}
            else:
                receipt = transport.invoke((root / "prompt.txt").read_text(), PROPOSAL_SCHEMA,
                                           root / "call", meta["model"], meta["effort"], timeout=300)
                c.dump(root / "call_receipt.json", receipt)
                if receipt["error"]:
                    raise ValueError(receipt["error"])
                response = receipt["response"]
                c.dump(root / "proposal.json", response)
            c.dump(root / "call_receipt.json", receipt)
            c.validate_schema(response, PROPOSAL_SCHEMA)
            if response["parent_harness_hash"] != meta["parent_harness_hash"] or response["input_packet_sha256"] != c.sha(root / "input.json"):
                raise ValueError("Meta proposal belongs to another input or parent")
            if not response["evidence_refs"] or not set(response["evidence_refs"]).issubset(packet["evidence_refs"]):
                raise ValueError("Meta must cite existing development evidence_refs")
            if not response["diagnosis"].strip() or not response["hypothesis"].strip():
                raise ValueError("Meta needs a diagnosis and testable hypothesis")
            if response["decision"] == "no_change":
                if response["replacement_lab_planner"] is not None or response["input_view_mode"] is not None:
                    raise ValueError("no_change must not contain edits")
                meta["status"] = "no_change"
            else:
                planner = response["replacement_lab_planner"]
                mode = response["input_view_mode"]
                old_planner = (root / "parent/lab_planner.md").read_text()
                old_mode = packet["parent_input_view"]["mode"]
                if (planner is None or planner == old_planner) and (mode is None or mode == old_mode):
                    raise ValueError("Proposal does not change the parent policy")
                if planner is not None:
                    # This enforces only literal identity leakage. Semantic
                    # overfitting still needs fresh validation and human review.
                    import re
                    if any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(cid) + r"(?![A-Za-z0-9_])", planner)
                           for cid in packet["development_candidate_ids"]):
                        raise ValueError("Policy contains a development candidate ID")
                candidate = root / "draft"
                shutil.copytree(root / "parent", candidate)
                candidate.chmod(0o755)
                for file in candidate.iterdir():
                    file.chmod(0o644)
                manifest = c.read(candidate / "manifest.json")
                manifest["version"] = "meta-" + c.canonical_hash(response)[:12]
                c.dump(candidate / "manifest.json", manifest)
                if planner is not None:
                    (candidate / "lab_planner.md").write_text(planner)
                if mode is not None:
                    c.dump(candidate / "input_view.json", {"mode": mode})
                frozen = versions.freeze_harness(candidate, root / "candidate")
                shutil.rmtree(candidate)
                c.dump(root / "candidate.json", {**frozen, "parent_harness_hash": meta["parent_harness_hash"],
                       "proposal_sha256": c.sha(root / "proposal.json"), "created_at": c.now(),
                       "status": "frozen_unvalidated", "execution_mode": meta["execution_mode"]})
                meta.update(status="candidate_frozen", candidate_harness_hash=frozen["harness_hash"])
        except Exception as exc:
            meta.update(status="failed", error=str(exc), finished_at=c.now())
            c.dump(root / "meta.json", meta)
            raise
        meta["finished_at"] = c.now()
        c.dump(root / "meta.json", meta)
        return {"status": meta["status"], "workspace": str(root),
                "candidate": str(root / "candidate") if meta["status"] == "candidate_frozen" else None,
                "model_called": not bool(replay)}


def scientific_protocol(protocol):
    """Only campaign/dataset identity may vary; numerical settings stay fixed."""
    ignored = {"protocol_id", "input_sha256", "phase", "frozen_at"}
    # Candidate pools change, but receptor/construct artifacts are part of the
    # scientific evaluator and must remain identical across all paired runs.
    receptor_names = {"receptor_manifest.json", "receptor.pdbqt", "receptor_effective.pdb", "reference.sdf"}
    receptor_hashes = {name: digest for name, digest in protocol.get("input_sha256", {}).items()
                       if "receptor" in Path(name).parts or Path(name).name in receptor_names}
    return {"settings": {key: value for key, value in protocol.items() if key not in ignored},
            "receptor_input_sha256": receptor_hashes}


def chemical_identities(candidates):
    return {value.strip() for row in candidates for value in
            (row["smiles"], row.get("canonical_parent_identity", row["smiles"]))}


def check_new_chemistry(candidates, seen_smiles, execution_mode):
    """Reject parent or prepared-state reuse; legacy inputs use exact SMILES."""
    structures = chemical_identities(candidates)
    if execution_mode == "live" and structures & seen_smiles:
        raise ValueError("Live validation pool reuses a development or prior-pair SMILES")
    return structures


def plan(args):
    """Register all validation datasets and exact future run paths before execution."""
    root, _, _ = workspace(args.workspace)
    with c.locked(root):
        root, meta, packet = workspace(root)
        if meta["status"] != "candidate_frozen" or meta.get("validation_plan_sha256"):
            raise ValueError("Plan requires an unplanned frozen candidate; replanning is forbidden")
        candidate = c.read(root / "candidate.json")
        if versions.load_harness(root / "candidate")["harness_hash"] != candidate["harness_hash"] or candidate["harness_hash"] != meta["candidate_harness_hash"]:
            raise ValueError("Frozen candidate changed")
        dev_run, dev, _ = completed_run(packet["development_run"], "development")
        if c.sha(dev_run / "artifact_hashes.json") != meta["development_artifacts_sha256"]:
            raise ValueError("Development archive changed since preparation")
        pairs = args.pair
        rules = c.read(root / "rules.json")
        if not pairs or len(pairs) < rules["min_pairs"]:
            raise ValueError("Not enough independent pairs for the frozen validation plan")
        reference_protocol = scientific_protocol(c.read(dev_run / "frozen/protocol.json"))
        seen_ids = set(packet["development_candidate_ids"])
        seen_smiles = chemical_identities(dev["candidates"])
        seen_runs, seen_datasets, planned = set(), {dev["dataset_hash"]}, []
        for data_path, protocol_path, baseline_path, candidate_path in pairs:
            data, protocol_file = Path(data_path).resolve(), Path(protocol_path).resolve()
            baseline, changed = Path(baseline_path).resolve(), Path(candidate_path).resolve()
            if baseline == changed or baseline in seen_runs or changed in seen_runs or baseline.exists() or changed.exists():
                raise ValueError("Every planned run path must be distinct and nonexistent before registration")
            seen_runs.update((baseline, changed))
            protocol = c.read(protocol_file)
            if scientific_protocol(protocol) != reference_protocol:
                raise ValueError("Validation scientific protocol differs from development")
            for name, digest in protocol["input_sha256"].items():
                source = (data / name).resolve()
                if not source.is_relative_to(data) or not source.is_file() or c.sha(source) != digest:
                    raise ValueError("Planned protocol input hash mismatch")
            candidates, evidence = c.freeze_public_data(data)
            if meta["execution_mode"] == "live" and c.read(data / "manifest.json").get("synthetic") is True:
                raise ValueError("Synthetic validation data cannot enter a live plan")
            snapshot = {"candidates": candidates, "evidence": evidence,
                        "snapshot_policy": "Only supplied public assay/SMILES/structure features; no prior candidate-level model scores"}
            dataset_hash = c.canonical_hash({"inputs": protocol["input_sha256"], "public_snapshot": hashlib.sha256(c.encoded(snapshot)).hexdigest()})
            ids = {row["candidate_id"] for row in candidates}
            if dataset_hash in seen_datasets or ids & seen_ids:
                raise ValueError("Planned validation pools must be fresh and mutually disjoint")
            seen_smiles.update(check_new_chemistry(candidates, seen_smiles, meta["execution_mode"]))
            seen_ids.update(ids)
            seen_datasets.add(dataset_hash)
            planned.append({"data": str(data), "protocol": str(protocol_file), "dataset_hash": dataset_hash,
                            "protocol_hash": c.sha(protocol_file), "baseline": str(baseline), "candidate": str(changed)})
        registration = {"schema_version": "lab_validation_plan_v1", "created_at": c.now(),
                        "parent_harness_hash": meta["parent_harness_hash"], "candidate_harness_hash": candidate["harness_hash"],
                        "execution_mode": meta["execution_mode"], "pairs": planned,
                        "policy": "Every registered pair is required; failures or omissions cannot be replaced or excluded."}
        with (root / "validation_plan.json").open("x") as stream:
            stream.write(c.encoded(registration).decode())
        meta["validation_plan_sha256"] = c.sha(root / "validation_plan.json")
        c.dump(root / "meta.json", meta)
        return {"status": "validation_planned", "workspace": str(root), "plan": str(root / "validation_plan.json"), "pairs": len(planned)}



def assess_pairs(pairs, rules, synthetic):
    """Pure arithmetic after prospective admission and archive verification."""
    count = len(pairs)
    if count < rules["min_pairs"]:
        raise ValueError("Insufficient pairs for decision")
    ge = lambda a, b: a >= b or math.isclose(a, b, rel_tol=0, abs_tol=1e-12)
    coverage = sum(p["candidate_pose_valid"] - p["baseline_pose_valid"] for p in pairs) / count
    # Each arm's mean uses every candidate with two qualified poses. Keep the
    # counts visible and reject a loss of coverage. An empty arm provides no
    # energy comparison for either release path; failed positions get no score.
    comparable = all(p["baseline_pose_valid"] > 0 and p["candidate_pose_valid"] > 0
                     and p["baseline_mean_qualified_docking_energy_kcal_mol"] is not None
                     and p["candidate_mean_qualified_docking_energy_kcal_mol"] is not None for p in pairs)
    gain = (statistics.mean(p["baseline_mean_qualified_docking_energy_kcal_mol"] - p["candidate_mean_qualified_docking_energy_kcal_mol"]
                            for p in pairs) if comparable else None)
    baseline_tokens = sum(p["baseline_tokens"] for p in pairs)
    candidate_tokens = sum(p["candidate_tokens"] for p in pairs)
    reduction = 1 - candidate_tokens / baseline_tokens if baseline_tokens else None
    no_coverage_regression = all(p["candidate_pose_valid"] >= p["baseline_pose_valid"] for p in pairs)
    admissible = no_coverage_regression and comparable and ge(gain, -rules["max_mean_docking_energy_loss_kcal_mol"])
    quality = admissible and (ge(coverage, rules["min_mean_pose_valid_gain"]) or
                              (coverage == 0 and ge(gain, rules["min_mean_docking_energy_gain_kcal_mol"])))
    cost = admissible and reduction is not None and ge(reduction, rules["min_token_reduction"])
    passed = quality or cost
    return {"decision": "demo_only" if synthetic else ("accepted" if passed else "rejected"),
            "eligible_for_next_development_run": passed and not synthetic,
            "quality_path_passed": quality, "cost_path_passed": cost,
            "mean_pose_valid_gain": coverage, "mean_docking_energy_gain_kcal_mol": gain,
            "all_pairs_have_qualified_docking_energy": comparable,
            "token_reduction_fraction": reduction}


def gate(args):
    root, meta, packet = workspace(args.workspace)
    if meta["status"] != "candidate_frozen":
        raise ValueError("A frozen candidate is required")
    candidate = c.read(root / "candidate.json")
    if versions.load_harness(root / "candidate")["harness_hash"] != candidate["harness_hash"] or candidate["harness_hash"] != meta["candidate_harness_hash"]:
        raise ValueError("Frozen candidate changed")
    if candidate["proposal_sha256"] != c.sha(root / "proposal.json"):
        raise ValueError("Frozen proposal changed")
    _, dev, _ = completed_run(packet["development_run"], "development")
    if c.sha(Path(packet["development_run"]) / "artifact_hashes.json") != meta["development_artifacts_sha256"]:
        raise ValueError("Development archive changed since preparation")
    rules = c.read(root / "rules.json")
    if not meta.get("validation_plan_sha256"):
        raise ValueError("A prospective validation plan is required before running pairs")
    registration = c.read(root / "validation_plan.json")
    expected_pairs = [(row["baseline"], row["candidate"]) for row in registration["pairs"]]
    supplied = getattr(args, "pair", None)
    if supplied:
        supplied = [(str(Path(b).resolve()), str(Path(q).resolve())) for b, q in supplied]
        if len(supplied) != len(expected_pairs) or set(supplied) != set(expected_pairs):
            raise ValueError("Gate requires all and only the registered validation pairs")
    if len(expected_pairs) < rules["min_pairs"]:
        raise ValueError("Not enough independent validation pairs for the frozen gate")
    seen_runs, seen_datasets = set(), {packet["development_dataset_hash"]}
    seen_candidates = set(packet["development_candidate_ids"])
    seen_smiles = chemical_identities(dev["candidates"])
    reference_protocol = scientific_protocol(c.read(Path(packet["development_run"]) / "frozen/protocol.json"))
    pairs = []
    synthetic = meta["execution_mode"] == "replay"
    for registered in registration["pairs"]:
        baseline_path, candidate_path = registered["baseline"], registered["candidate"]
        br, bs, b = completed_run(baseline_path, "validation")
        cr, cs, q = completed_run(candidate_path, "validation")
        if br == cr or br in seen_runs or cr in seen_runs:
            raise ValueError("Each validation run can be used only once")
        seen_runs.update((br, cr))
        if bs["harness_hash"] != meta["parent_harness_hash"] or cs["harness_hash"] != candidate["harness_hash"]:
            raise ValueError("Validation pair has the wrong baseline or candidate harness")
        for key in ("dataset_hash", "protocol_hash", "model", "effort", "execution_mode", "runtime_hash"):
            if bs[key] != cs[key]:
                raise ValueError(f"Validation pair differs in {key}")
        if bs["dataset_hash"] != registered["dataset_hash"] or bs["protocol_hash"] != registered["protocol_hash"]:
            raise ValueError("Validation pair differs from its registered dataset/protocol")
        if scientific_protocol(c.read(br / "frozen/protocol.json")) != reference_protocol:
            raise ValueError("Validation scientific protocol differs from development")
        for key in ("model", "effort", "execution_mode", "runtime_hash"):
            if bs[key] != dev[key]:
                raise ValueError(f"Validation differs from development in {key}")
        if bs["created_at"] <= registration["created_at"] or cs["created_at"] <= registration["created_at"]:
            raise ValueError("Validation must start after prospective plan registration")
        ids = {row["candidate_id"] for row in bs["candidates"]}
        if bs["dataset_hash"] in seen_datasets or ids & seen_candidates:
            raise ValueError("Validation pools must be fresh and mutually disjoint from development")
        seen_smiles.update(check_new_chemistry(bs["candidates"], seen_smiles, meta["execution_mode"]))
        seen_datasets.add(bs["dataset_hash"])
        seen_candidates.update(ids)
        synthetic |= bs["execution_mode"] != "live" or bool(b.get("synthetic")) or bool(q.get("synthetic"))
        if not synthetic and (not b["llm_usage_accounting_complete"] or not q["llm_usage_accounting_complete"]):
            raise ValueError("Live gate requires complete model usage accounting")
        pairs.append({"baseline": str(br), "candidate": str(cr), "dataset_hash": bs["dataset_hash"],
                      "baseline_archive_sha256": c.sha(br / "artifact_hashes.json"),
                      "candidate_archive_sha256": c.sha(cr / "artifact_hashes.json"),
                      "baseline_pose_valid": b["paired_pose_valid_candidates"], "candidate_pose_valid": q["paired_pose_valid_candidates"],
                      "baseline_mean_qualified_docking_energy_kcal_mol": b["mean_qualified_docking_energy_kcal_mol"],
                      "candidate_mean_qualified_docking_energy_kcal_mol": q["mean_qualified_docking_energy_kcal_mol"],
                      "baseline_tokens": b["observed_input_plus_output_tokens"],
                      "candidate_tokens": q["observed_input_plus_output_tokens"]})
    assessment = assess_pairs(pairs, rules, synthetic)
    result = {"schema_version": "lab_meta_gate_v1", "created_at": c.now(),
              **assessment,
              "synthetic": synthetic, "rules": rules, "pairs": pairs, "docking_evaluator": evaluator_descriptor(),
              "validation_plan_sha256": meta["validation_plan_sha256"],
              "parent_harness_hash": meta["parent_harness_hash"], "candidate_harness_hash": candidate["harness_hash"],
              "candidate": str(root / "candidate"),
              "meta_usage": c.read(root / "call_receipt.json").get("usage"),
              "limitations": ["Development engineering gate; thresholds are not biologically calibrated.",
                              "Each energy mean is conditional on valid poses for both seeds. Inspect valid counts per arm alongside energy gains. A pair with no valid energy in either arm cannot support a release.",
                              "Meta proposal cost is reported separately and not amortized into paired Lab tokens.",
                              "Accepted changes apply only to new campaigns; no current campaign is mutated.",
                              "Source references and literal ID checks do not establish semantic validity or absence of overfitting.",
                              "Live pool separation checks supplied canonical parent identities for prepared inputs and exact SMILES for legacy inputs; chemical equivalence still relies on the frozen dataset curation."]}
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        stream.write(c.encoded(result).decode())
    return result
