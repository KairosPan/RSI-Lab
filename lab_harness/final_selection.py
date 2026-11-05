"""Apply the formal-list threshold to audited AutoDock Vina docking observations."""
from __future__ import annotations

import math
from pathlib import Path
import statistics

from .docking import SCORE_DIRECTION, SCORE_FIELD, evaluator_descriptor

MIN_FINAL_COMPOUNDS = 100


def _filter_docking_rows(observations, threshold):
    by_identity = {}
    by_id = {}
    for row in observations:
        cid = row["compound_id"]
        identity = row.get("canonical_parent_identity", row["canonical_smiles"])
        if by_id.setdefault(cid, identity) != identity:
            raise ValueError(f"Compound ID {cid!r} maps to multiple molecular identities")
        previous = by_identity.get(identity)
        if previous is None:
            by_identity[identity] = row
        else:
            if (previous["canonical_smiles"] != row["canonical_smiles"]
                    or previous.get("prepared_state_id") != row.get("prepared_state_id")):
                raise ValueError(f"Conflicting prepared states for parent identity {identity!r}")
            if previous["seed_results"] != row["seed_results"] or previous["score"] != row["score"]:
                raise ValueError(f"Conflicting docking results for {identity!r}")
            if (cid, row["batch"]) < (previous["compound_id"], previous["batch"]):
                by_identity[identity] = row
    selected = [{"compound_id": r["compound_id"], "canonical_smiles": r["canonical_smiles"],
                 "canonical_parent_identity": r.get("canonical_parent_identity", r["canonical_smiles"]),
                 "prepared_state_id": r.get("prepared_state_id"),
                 "score": r["score"], "docking_batch": r["batch"]}
                for r in by_identity.values() if r["score"] is not None and r["score"] <= threshold]
    selected.sort(key=lambda r: (r["score"], r["compound_id"]))
    count = len(selected)
    feasible = count >= MIN_FINAL_COMPOUNDS
    return {"threshold": threshold, "score_name": SCORE_FIELD, "direction": SCORE_DIRECTION,
            "evaluator": evaluator_descriptor(), "min_final_compounds": MIN_FINAL_COMPOUNDS, "count": count,
            "missing_count": sum(r["score"] is None for r in by_identity.values()),
            "duplicate_count": len(observations) - len(by_identity), "feasible": feasible,
            "status": "accepted" if feasible else "insufficient_compounds",
            "shortfall": max(0, MIN_FINAL_COMPOUNDS - count),
            "mean_score": statistics.mean(row["score"] for row in selected) if selected else None,
            "compounds": selected}


def select_final_compounds(payload: dict, *, base_dir=None) -> dict:
    """Read every admitted batch of each archive. Manifest paths resolve relative to its file."""
    # Controller imports the cardinality bound above; defer its archive reader until runtime.
    from .docking_archive import read_docking_run

    if not isinstance(payload, dict) or payload.get("schema_version") != "lab_docking_selection_input_v1":
        raise ValueError("Expected lab_docking_selection_input_v1 with threshold (kcal/mol) and runs (completed live AutoDock Vina archives). Supplied score tables cannot be evaluated.")
    if set(payload) != {"schema_version", "threshold", "runs"}:
        raise ValueError("Docking manifest requires exactly schema_version, threshold and runs")
    threshold = payload["threshold"]
    try:
        finite_threshold = type(threshold) in (int, float) and math.isfinite(threshold)
    except OverflowError:
        finite_threshold = False
    if not finite_threshold:
        raise ValueError("Docking threshold must be a finite number in kcal/mol")
    paths = payload["runs"]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        raise ValueError("runs must be a nonempty array of docking campaign directory paths")
    root = Path.cwd() if base_dir is None else Path(base_dir)
    runs = [(root / path).resolve() for path in paths]
    if len(set(runs)) != len(runs):
        raise ValueError("Each docking archive may appear only once")

    observations, sources = [], []
    for run in runs:
        rows, source = read_docking_run(run)
        if sources and source["profile_sha256"] != sources[0]["profile_sha256"]:
            raise ValueError("Docking archives use incompatible scientific or worker profiles")
        observations.extend(rows)
        sources.append(source)
    return {"schema_version": "lab_vina_selection_result_v1", "source": "autodock_vina_archives",
            "evidence_verified": True, "archives": sources, **_filter_docking_rows(observations, threshold)}
