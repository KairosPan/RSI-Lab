#!/usr/bin/env python3
"""Audit and report an exploratory two-seed Vina screen without running docking."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys

# Support direct invocation from outside the checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab_harness.docking_archive import _check_pose  # noqa: E402
from scripts.vina_contract import SEEDS, parse_vina_poses, validate_vina_protocol  # noqa: E402

JOB_STATUSES = {"completed", "preparation_failed", "docking_failed", "docking_timeout",
                "pose_parse_failed", "pose_check_failed", "worker_error", "worker_timeout",
                "global_deadline_timeout", "parent_interrupted", "launch_failed"}
CUTOFF_STATUSES = {"global_deadline_timeout", "parent_interrupted", "launch_failed"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def check_pose_flags(result, folder, protocol):
    expected = {module.get("rename_outputs", {}).get(key, key + module.get("rename_suffix", "")).lower().replace(" ", "_")
                for module in protocol["posebusters_configs"]["dock"]["modules"]
                for key in module.get("chosen_binary_test_output", [])}
    checks = result.get("pose_checks")
    if not expected or not isinstance(checks, dict) or set(checks) != expected:
        raise ValueError(f"{folder.name}: PoseBusters checks differ from protocol")
    if any(value is not None and type(value) is not bool for value in checks.values()):
        raise ValueError(f"{folder.name}: invalid PoseBusters flag")
    with (folder / "posebusters.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 1:
        raise ValueError(f"{folder.name}: expected one PoseBusters row")
    for key, value in checks.items():
        if key not in rows[0] or {"True": True, "False": False, "": None}.get(rows[0][key], "invalid") != value:
            raise ValueError(f"{folder.name}: PoseBusters result differs from raw CSV")
    if type(result.get("identity_preserved")) is not bool:
        raise ValueError(f"{folder.name}: missing identity check")
    flags = [*checks.values(), result["identity_preserved"]]
    expected_valid = False if False in flags else None if None in flags else True
    if result.get("pose_valid") is not expected_valid:
        raise ValueError(f"{folder.name}: pose_valid differs from raw checks")


def inspect_job(root, candidate, seed, protocol):
    folder = root / "jobs" / f"{candidate['candidate_id']}_s{seed}"
    spec_path, result_path = folder / "spec.json", folder / "result.json"
    if not spec_path.exists():
        if result_path.exists():
            raise ValueError(f"{folder.name}: result has no specification")
        return {"status": "unrun", "result_present": False, "qualified": False}
    if not folder.resolve().is_relative_to(root):
        raise ValueError("Job directory escapes the screen root")
    spec = read(spec_path)
    if (spec.get("seed") != seed or type(spec.get("seed")) is not int
            or spec.get("protocol") != protocol or spec.get("candidate") != candidate
            or spec.get("kind") != "candidate"):
        raise ValueError(f"{folder.name}: job specification differs from frozen inputs")
    if not result_path.exists():
        return {"status": "pending_or_interrupted", "result_present": False, "qualified": False}
    result = read(result_path)
    if (result.get("candidate_id") != candidate["candidate_id"] or result.get("seed") != seed
            or type(result.get("seed")) is not int or result.get("status") not in JOB_STATUSES
            or result.get("synthetic") or spec.get("synthetic")):
        raise ValueError(f"{folder.name}: invalid job identity, status, or provenance")
    for key in ("cpu_seconds", "wall_seconds", "parent_observed_wall_seconds", "docking_wall_seconds"):
        value = result.get(key)
        if value is not None and (not finite(value) or value < 0):
            raise ValueError(f"{folder.name}: invalid {key}")
    scores = result.get("scores")
    if result["status"] in CUTOFF_STATUSES and (scores is not None or result.get("pose_valid") is True
                                                or result.get("identity_preserved") is True):
        raise ValueError(f"{folder.name}: interrupted or unlaunched job retains successful output")
    if scores is not None:
        if not isinstance(scores, dict) or set(scores) != {"vina_affinity"} or not finite(scores["vina_affinity"]):
            raise ValueError(f"{folder.name}: invalid Vina score")
        native = parse_vina_poses((folder / "poses.pdbqt").read_text())
        if scores["vina_affinity"] != native[0] or result.get("selected_model") != 1:
            raise ValueError(f"{folder.name}: score differs from native Vina MODEL 1")
    canonical = result.get("preparation", {}).get("canonical_smiles")
    if result["status"] == "completed":
        if result.get("returncode") != 0 or type(result.get("returncode")) is not int or scores is None:
            raise ValueError(f"{folder.name}: completed result lacks successful docking")
        if type(result.get("parent_observed_returncode")) is not int or result["parent_observed_returncode"] != 0:
            raise ValueError(f"{folder.name}: completed result lacks successful parent exit receipt")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError(f"{folder.name}: completed result lacks canonical identity")
        if candidate.get("canonical_smiles") is not None and candidate["canonical_smiles"] != canonical:
            raise ValueError(f"{folder.name}: prepared identity differs from candidate registry")
        _check_pose(result, folder, canonical, protocol)
        check_pose_flags(result, folder, protocol)
    return {**result, "result_present": True, "result_sha256": sha(result_path),
            "qualified": result["status"] == "completed" and result.get("identity_preserved") is True
                         and result.get("pose_valid") is True,
            "canonical_smiles": canonical, "energy": scores["vina_affinity"] if scores else None}


def candidate_row(candidate, jobs):
    canonical = {job["canonical_smiles"] for job in jobs if job.get("canonical_smiles")}
    if len(canonical) > 1:
        raise ValueError(f"{candidate['candidate_id']}: prepared identities differ between seeds")
    qualified = all(job["qualified"] for job in jobs)
    states = [job["status"] for job in jobs]
    if qualified:
        status = "two_seed_valid"
    elif all(state == "unrun" for state in states):
        status = "unrun"
    elif any(state == "pending_or_interrupted" for state in states):
        status = "pending_or_interrupted"
    elif "unrun" in states:
        status = "partially_evaluated"
    elif any(state != "completed" for state in states):
        status = "execution_failed"
    else:
        status = "pose_invalid_or_unknown"
    row = {"candidate_id": candidate["candidate_id"], "smiles": candidate["smiles"],
           "canonical_smiles": next(iter(canonical), candidate.get("canonical_smiles")),
           "status": status, "two_seed_valid": qualified,
           "median_vina_affinity_kcal_mol": statistics.median(job["energy"] for job in jobs) if qualified else None,
           "seed_energy_range_kcal_mol": abs(jobs[0]["energy"] - jobs[1]["energy"]) if qualified else None}
    for seed, job in zip(SEEDS, jobs):
        for field in ("status", "energy", "identity_preserved", "pose_valid", "cpu_seconds", "wall_seconds", "error"):
            row[f"seed_{seed}_{field}"] = job.get(field)
    return row


def sum_known(jobs, field):
    values = [job[field] for job in jobs if job.get(field) is not None]
    return {"known_sum": sum(values), "observed_jobs": len(values),
            "complete_for_reported_jobs": len(values) == len(jobs)}


def job_span(jobs):
    starts = [datetime.fromisoformat(job["started_at"]) for job in jobs if job.get("started_at")]
    finishes = [datetime.fromisoformat(job["finished_at"]) for job in jobs if job.get("finished_at")]
    if not starts or not finishes:
        return None
    seconds = (max(finishes) - min(starts)).total_seconds()
    if seconds < 0:
        raise ValueError("Job timestamps have negative observed span")
    return seconds


def check_frozen_sources(root, plan, protocol):
    if "protocol_sha256" in plan and plan["protocol_sha256"] != sha(root / "protocol.json"):
        raise ValueError("Protocol differs from the frozen screen plan")
    frozen = root / "frozen"
    sources = {}
    if frozen.exists():
        for key, name in (("runner_sha256", "vina_worker.py"), ("adapter_sha256", "run_vina_batch.py"),
                          ("contract_sha256", "vina_contract.py")):
            path = frozen / name
            if not path.is_file() or sha(path) != protocol[key]:
                raise ValueError(f"Frozen worker source mismatch: {name}")
            sources[name] = sha(path)
        if (frozen / "protocol.json").exists() and sha(frozen / "protocol.json") != sha(root / "protocol.json"):
            raise ValueError("Frozen protocol copy differs from root protocol")
    if "driver_sha256" in plan:
        path = frozen / "run_vina_screen.py"
        if not path.is_file() or sha(path) != plan["driver_sha256"]:
            raise ValueError("Frozen screen driver differs from the plan")
        sources[path.name] = sha(path)
    return sources


def check_execution_summary(root, summary, candidates, all_jobs):
    if summary is None:
        return
    expected = {(row["candidate_id"], seed) for row in candidates for seed in SEEDS}
    reported = [job for job in all_jobs if job["result_present"]]
    if (summary.get("schema_version") != "bounded_vina_screen_execution_v1"
            or summary.get("plan_sha256") != sha(root / "plan.json")
            or summary.get("protocol_sha256") != sha(root / "protocol.json")):
        raise ValueError("Execution summary differs from the screen plan or protocol")
    if (type(summary.get("candidate_count")) is not int or summary["candidate_count"] != len(candidates)
            or type(summary.get("settled_seed_jobs")) is not int or summary["settled_seed_jobs"] != len(reported)):
        raise ValueError("Execution summary settled/candidate counts differ from job records")
    unrun = summary.get("unrun_seed_jobs")
    if not isinstance(unrun, list) or any(not isinstance(job, dict) or set(job) != {"candidate_id", "seed"}
                                        or type(job["seed"]) is not int for job in unrun):
        raise ValueError("Execution summary has an invalid unrun list")
    unrun_ids = [(job["candidate_id"], job["seed"]) for job in unrun]
    reported_ids = {(job["candidate_id"], job["seed"]) for job in reported}
    if (len(set(unrun_ids)) != len(unrun_ids) or set(unrun_ids) != expected - reported_ids
            or any(job["status"] == "pending_or_interrupted" for job in all_jobs)):
        raise ValueError("Execution summary settled + unrun jobs do not cover the exact plan")
    if summary.get("status") not in {"completed", "interrupted", "deadline_reached"}:
        raise ValueError("Unknown execution summary status")
    if summary["status"] == "completed" and (unrun or any(job["status"] in {"global_deadline_timeout", "parent_interrupted"} for job in reported)):
        raise ValueError("Execution summary claims completion despite unrun or interrupted jobs")
    total = summary.get("worker_cpu_seconds")
    if (not finite(total) or total < 0 or not math.isclose(total, sum(job.get("cpu_seconds") or 0 for job in reported), rel_tol=1e-9, abs_tol=1e-6)
            or summary.get("cpu_accounting_complete") is not all(job.get("cpu_seconds") is not None for job in reported)):
        raise ValueError("Execution summary CPU accounting differs from job records")
    if not finite(summary.get("wall_seconds")) or summary["wall_seconds"] < 0:
        raise ValueError("Execution summary wall time is invalid")


def build_report(root):
    root = Path(root).resolve()
    plan, protocol = read(root / "plan.json"), read(root / "protocol.json")
    candidates = read(root / "data/candidates.json")["candidates"]
    validate_vina_protocol(protocol, live=True)
    frozen_sources = check_frozen_sources(root, plan, protocol)
    if plan.get("threshold") is not None or plan.get("seeds") != list(SEEDS) or plan.get("candidate_count") != len(candidates):
        raise ValueError("Expected a threshold-free plan with matching candidate count and seeds 17/43")
    if not candidates or any(not isinstance(row.get("candidate_id"), str) or not ID.fullmatch(row["candidate_id"])
                             or not isinstance(row.get("smiles"), str) or not row["smiles"].strip() for row in candidates):
        raise ValueError("Candidate registry must have safe IDs and nonempty structures")
    ids = [row["candidate_id"] for row in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("Candidate registry has duplicate IDs")
    if "max_seed_jobs" in plan and (type(plan["max_seed_jobs"]) is not int or plan["max_seed_jobs"] != len(ids) * len(SEEDS)):
        raise ValueError("Screen plan seed-job budget differs from candidate count")
    for name, digest in protocol["input_sha256"].items():
        path = (root / "data" / name).resolve()
        if not path.is_relative_to(root / "data") or not path.is_file() or sha(path) != digest:
            raise ValueError(f"Frozen protocol input mismatch: {name}")
    expected_folders = {f"{cid}_s{seed}" for cid in ids for seed in SEEDS}
    extra = [path.name for path in (root / "jobs").iterdir() if path.is_dir() and path.name not in expected_folders] if (root / "jobs").exists() else []
    if extra:
        raise ValueError(f"Unexpected job directories: {extra}")
    all_rows, all_jobs = [], []
    for candidate in candidates:
        jobs = [inspect_job(root, candidate, seed, protocol) for seed in SEEDS]
        all_jobs.extend(jobs)
        all_rows.append(candidate_row(candidate, jobs))
    ranking = sorted((row for row in all_rows if row["two_seed_valid"]),
                     key=lambda row: (row["median_vina_affinity_kcal_mol"], row["candidate_id"]))
    reported = [job for job in all_jobs if job["result_present"]]
    qualified_identities = {row["canonical_smiles"] for row in ranking}
    status = "incomplete" if len(reported) != len(all_jobs) else "completed" if len(ranking) == len(candidates) else "completed_with_failures_or_invalid_poses"
    summary_path = root / "execution_summary.json"
    summary = read(summary_path) if summary_path.exists() else None
    check_execution_summary(root, summary, candidates, all_jobs)
    report = {"schema_version": "exploratory_vina_screen_report_v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "run": str(root), "status": status, "threshold": None, "formal_selection_assessed": False,
              "formal_min_distinct_compounds": 100, "formal_qualification": "not_assessed_no_frozen_threshold",
              "engine": protocol["engine"], "engine_version": protocol["vina_version"]["stdout"],
              "seeds": list(SEEDS), "score": "median of two valid native Vina MODEL 1 affinities",
              "direction": "lower", "units": "kcal/mol", "candidate_count": len(candidates),
              "expected_seed_jobs": len(all_jobs), "reported_seed_jobs": len(reported),
              "seed_job_status_counts": dict(Counter(job["status"] for job in all_jobs)),
              "candidate_status_counts": dict(Counter(row["status"] for row in all_rows)),
              "two_seed_valid_candidate_count": len(ranking),
              "two_seed_valid_distinct_canonical_count": len(qualified_identities),
              "minimum_additional_distinct_valid_candidates_to_reach_100": max(0, 100 - len(qualified_identities)),
              "mean_qualified_energy_kcal_mol": statistics.mean(row["median_vina_affinity_kcal_mol"] for row in ranking) if ranking else None,
              "timing": {"cpu_seconds": sum_known(reported, "cpu_seconds"), "worker_wall_seconds": sum_known(reported, "wall_seconds"),
                         "parent_wall_seconds": sum_known(reported, "parent_observed_wall_seconds"),
                         "docking_wall_seconds": sum_known(reported, "docking_wall_seconds"),
                         "observed_job_span_seconds": job_span(reported),
                         "full_run_cpu_accounting_complete": len(reported) == len(all_jobs)
                             and all(job.get("cpu_seconds") is not None for job in reported)},
              "execution_summary": summary, "plan": plan,
              "provenance": {"plan_sha256": sha(root / "plan.json"), "protocol_sha256": sha(root / "protocol.json"),
                             "verified_frozen_source_sha256": frozen_sources,
                             "candidates_sha256": sha(root / "data/candidates.json"), "reporter_sha256": sha(__file__),
                             "execution_summary_sha256": sha(summary_path) if summary_path.exists() else None,
                             "job_result_sha256": {f"{job['candidate_id']}_s{job['seed']}": job["result_sha256"] for job in reported}},
              "limitations": [*plan.get("limits", []),
                              "Qualified energy is conditional on two valid poses. All input candidates and failures remain in all_results.csv.",
                              "No formal threshold was selected; input count and valid-pose count do not establish 100 qualifying compounds.",
                              "The two docking seeds are not independent policy experiments. This screen measures no LLM or Meta improvement."]}
    return report, all_rows, ranking


def write_csv(path, rows, fields):
    with path.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def markdown(report):
    states = "、".join(f"{name} {count}" for name, count in report["candidate_status_counts"].items())
    timing = report["timing"]
    failure_lines = [f"- {status}：{count} 个种子任务" for status, count in report["seed_job_status_counts"].items() if status != "completed"]
    failures = "\n".join(failure_lines) if failure_lines else "全部种子任务均报告 completed。姿态有效性另行计数。"
    span = "未记录" if timing["observed_job_span_seconds"] is None else f"{timing['observed_job_span_seconds']:.2f} 秒"
    return f"""# 本地 Vina 探索性筛选

状态为 `{report['status']}`。输入 {report['candidate_count']} 个候选，已取得 {report['reported_seed_jobs']}/{report['expected_seed_jobs']} 个种子任务结果。固定种子为 17、43，每次读取原始 PDBQT 的第一姿态。

双种子均完成、分子身份保持且姿态有效的候选有 {report['two_seed_valid_candidate_count']} 个，按 canonical SMILES 去重后为 {report['two_seed_valid_distinct_canonical_count']} 个。`ranking.csv` 按两种子能量中位数从低到高排列，单位 kcal/mol。`all_results.csv` 保留全部输入、逐种子分数、失败及未执行状态。

候选状态分布为 {states}。

已知 CPU 用量合计 {timing['cpu_seconds']['known_sum']:.2f} 秒，覆盖 {timing['cpu_seconds']['observed_jobs']}/{report['reported_seed_jobs']} 个已有结果。已知 worker 墙钟时长合计 {timing['worker_wall_seconds']['known_sum']:.2f} 秒。并发任务时长之和不等于整次筛选耗时，实际任务时间跨度为 {span}。

{failures}

本次 threshold 为空，正式达标数尚未评估。正式要求为至少 100 个不同且达到冻结阈值的化合物，{report['candidate_count']} 个输入或双有效姿态数均不能替代该要求。该运行未测量 LLM 或 Meta 改善，两个 docking seeds 不构成独立策略实验。

本次使用既有的单受体准备、固定搜索盒和输入 SMILES 结构状态。能量与姿态检查用于计算筛选，不能据此推断实验活性。具体运行边界与实际执行汇总见 `report.json`，原始对接与姿态证据保留在运行目录。
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Exploratory screen root with plan, protocol, data, and jobs")
    parser.add_argument("--out", required=True, help="New report directory; existing paths are refused")
    args = parser.parse_args(argv)
    try:
        output = Path(args.out).resolve()
        if output.exists():
            raise ValueError("Report directory already exists; choose a new output path")
        report, rows, ranking = build_report(args.run)
        report_text = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        markdown_text = markdown(report)
        output.mkdir(parents=True, exist_ok=False)
        write_csv(output / "all_results.csv", rows, list(rows[0]))
        write_csv(output / "ranking.csv", ranking, list(rows[0]))
        (output / "report.json").write_text(report_text)
        (output / "REPORT.md").write_text(markdown_text)
        print(json.dumps({"report": str(output), "status": report["status"], "two_seed_valid": len(ranking), "threshold": None}))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
