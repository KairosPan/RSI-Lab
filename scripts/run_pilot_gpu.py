#!/usr/bin/env python3
"""Small, auditable GNINA/Colab P0 pilot. No agent comparison or efficacy labels.

Run with --data data/pilot_cdk2 --out runs/new_run --gnina /content/gnina.
Dependencies: rdkit, posebusters, numpy, pandas, scipy. Linux GPU required.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import resource
import signal
import statistics
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

SEEDS = [17, 43]
SCORE_FIELDS = ["minimizedAffinity", "CNNscore", "CNNaffinity"]


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def dump(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def probe(command, timeout=20):
    p = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    return {"command": command, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}


def cpu_seconds():
    return sum(r.ru_utime + r.ru_stime for r in
               [resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)])


def prepare(smiles, path):
    from rdkit import Chem
    from rdkit.Chem import AllChem, Descriptors
    m = Chem.MolFromSmiles(smiles)
    if m is None or len(Chem.GetMolFrags(m)) != 1:
        raise ValueError("Invalid or multicomponent input; no silent salt stripping")
    canonical = Chem.MolToSmiles(m, isomericSmiles=True)
    m = Chem.AddHs(m)
    m.RemoveAllConformers()
    params = AllChem.ETKDGv3()
    params.randomSeed, params.numThreads = 20261019, 1
    if AllChem.EmbedMolecule(m, params) != 0:
        raise ValueError("ETKDG embedding failed; no retry")
    if not AllChem.MMFFHasAllMoleculeParams(m):
        raise ValueError("MMFF94s parameters missing; no alternate force field")
    convergence = AllChem.MMFFOptimizeMolecule(m, mmffVariant="MMFF94s", maxIters=500)
    if convergence != 0:
        raise ValueError(f"MMFF94s did not converge in 500 iterations: {convergence}")
    with Chem.SDWriter(str(path)) as w:
        w.write(m)
    return {"canonical_smiles": canonical, "mw": Descriptors.MolWt(m),
            "heavy_atoms": m.GetNumHeavyAtoms(), "mmff_converged": True}


def gpu_monitor(stop, target_pid, path):
    with path.open("w") as f:
        while not stop.is_set():
            try:
                r = probe(["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory",
                           "--format=csv,noheader,nounits"], timeout=5)
                f.write(json.dumps({"time": now(), "gnina_pid": target_pid, **r}) + "\n")
                f.flush()
            except Exception as e:
                f.write(json.dumps({"time": now(), "error": str(e)}) + "\n")
            stop.wait(0.5)


def chosen_checks(config):
    return [module.get("rename_outputs", {}).get(name, name + module.get("rename_suffix", "")).lower().replace(" ", "_")
            for module in config["modules"] for name in module.get("chosen_binary_test_output", [])]


def worker(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    out = Path(spec_path).parent
    started, cpu_start = time.monotonic(), cpu_seconds()
    r = {"candidate_id": spec["candidate"]["candidate_id"], "kind": spec["kind"],
         "seed": spec["seed"], "started_at": now(), "status": "preparation_failed",
         "scores": None, "pose_valid": None, "gpu_process_observed": False}
    try:
        from rdkit import Chem
        from posebusters import PoseBusters
        import pandas as pd
        data, protocol = Path(spec["data"]), spec["protocol"]
        r["preparation"] = prepare(spec["candidate"]["smiles"], out / "input.sdf")
        command = [spec["gnina"], "--receptor", str(data / "receptor/receptor.pdbqt"),
                   "--ligand", str(out / "input.sdf"), "--out", str(out / "poses.sdf"),
                   "--seed", str(spec["seed"]), "--exhaustiveness", "4", "--num_modes", "3",
                   "--cpu", "2", "--device", "0", "--scoring", "vina", "--cnn_scoring", "rescore",
                   "--pose_sort_order", "CNNscore", "--cnn", "dense_1_3"]
        for name in ["center", "size"]:
            for axis, value in zip("xyz", protocol[name]):
                command.extend([f"--{name}_{axis}", str(value)])
        r["command"], r["status"] = command, "docking_failed"
        dock_start = time.monotonic()
        with (out / "gnina.stdout.txt").open("w") as stdout, (out / "gnina.stderr.txt").open("w") as stderr:
            p = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            stop = threading.Event()
            monitor = threading.Thread(target=gpu_monitor, args=(stop, p.pid, out / "gpu.jsonl"), daemon=True)
            monitor.start()
            try:
                r["returncode"] = p.wait(timeout=180)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
                r["status"] = "docking_timeout"
                raise TimeoutError("GNINA exceeded 180s; killed, no retry")
            finally:
                stop.set()
                monitor.join(timeout=6)
                r["docking_wall_seconds"] = time.monotonic() - dock_start
        for line in (out / "gpu.jsonl").read_text().splitlines():
            sample = json.loads(line)
            if any(s.split(",")[0].strip() == str(p.pid) for s in sample.get("stdout", "").splitlines()):
                r["gpu_process_observed"] = True
        if r["returncode"] != 0:
            raise RuntimeError(f"GNINA exited {r['returncode']}")
        r["status"] = "pose_parse_failed"
        poses = list(Chem.SDMolSupplier(str(out / "poses.sdf"), removeHs=False))
        if not poses or any(m is None for m in poses):
            raise ValueError("Missing or unparsable poses; cannot replace top pose")
        # GNINA is explicitly told to sort by CNNscore. Do not pick a lower pose after checks.
        top = poses[0]
        scores = {key: float(top.GetProp(key)) for key in SCORE_FIELDS}
        if not all(math.isfinite(v) for v in scores.values()):
            raise ValueError("Nonfinite model score")
        if any(float(m.GetProp("CNNscore")) > scores["CNNscore"] + 1e-6 for m in poses[1:]):
            raise ValueError("Output is not sorted by the frozen CNNscore rule")
        r["scores"], r["pose_count"] = scores, len(poses)
        r["identity_preserved"] = Chem.MolToSmiles(Chem.RemoveHs(top), isomericSmiles=True) == r["preparation"]["canonical_smiles"]
        with Chem.SDWriter(str(out / "top.sdf")) as w:
            w.write(top)
        r["status"] = "pose_check_failed"
        config = protocol["posebusters_configs"]["redock" if spec["kind"] == "reference" else "dock"]
        pb = PoseBusters(config=config, max_workers=0)
        table = pb.bust(out / "top.sdf", mol_cond=data / "receptor/receptor_effective.pdb",
                        mol_true=data / "receptor/reference.sdf" if spec["kind"] == "reference" else None,
                        full_report=True)
        table.to_csv(out / "posebusters.csv")
        row = table.iloc[0]
        checks = {key: None if pd.isna(row[key]) else bool(row[key]) for key in chosen_checks(config)}
        structural = [v for k, v in checks.items() if k != "rmsd_≤_2å"]
        structural.append(r["identity_preserved"])
        r["pose_valid"] = False if False in structural else (None if None in structural else True)
        r["pose_checks"] = checks
        if spec["kind"] == "reference":
            r["rmsd_angstrom"] = float(row["rmsd"]) if math.isfinite(float(row["rmsd"])) else None
        r["status"] = "completed"
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
        (out / "exception.txt").write_text(traceback.format_exc())
    finally:
        r.update(finished_at=now(), wall_seconds=time.monotonic() - started, cpu_seconds=cpu_seconds() - cpu_start)
        r["artifacts"] = {p.name: sha(p) for p in out.iterdir() if p.is_file() and p.name != "result.json"}
        dump(out / "result.json", r)


def aggregate(candidate, results):
    rows = [r for r in results if r["candidate_id"] == candidate["candidate_id"]]
    complete = len(rows) == len(SEEDS) and sorted(r["seed"] for r in rows) == SEEDS
    scored = complete and all(r.get("scores") is not None for r in rows)
    valid = scored and all(r["status"] == "completed" and r.get("pose_valid") is True for r in rows)
    record = {"candidate_id": candidate["candidate_id"], "ic50_nM": candidate.get("ic50_nM"),
              "pIC50": candidate.get("pIC50"), "completed_repeats": sum(r["status"] == "completed" for r in rows),
              "valid_repeats": sum(r.get("pose_valid") is True for r in rows),
              "statuses": ";".join(r["status"] for r in rows), "two_valid_repeats": bool(valid)}
    for key in SCORE_FIELDS:
        record[f"raw_median_{key}"] = statistics.median(r["scores"][key] for r in rows) if scored else None
        record[f"qualified_median_{key}"] = record[f"raw_median_{key}"] if valid else None
        record[f"seed_range_{key}"] = max(r["scores"][key] for r in rows) - min(r["scores"][key] for r in rows) if scored else None
    return record


def summarize(out, candidates, results, wall_seconds):
    records = [aggregate(c, results) for c in candidates]
    with (out / "results.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    terminal = [r for r in results if r["status"] not in {"not_started_budget", "worker_timeout", "worker_error"}]
    summary = {"phase": "P0_pilot", "is_mock": False, "official_pass_threshold": None,
               "official_pass_rate": None, "lab_agent_ran": False, "meta_improvement_ran": False,
               "candidate_count": len(candidates), "planned_jobs": (len(candidates) + 1) * len(SEEDS),
               "terminal_jobs": len(terminal), "completed_jobs": sum(r["status"] == "completed" for r in results),
               "status": "completed" if len(terminal) == len(results) else "incomplete",
               "wall_seconds": wall_seconds, "measured_worker_cpu_seconds": sum(r.get("cpu_seconds") or 0 for r in results),
               "cpu_accounting_complete": all(r.get("cpu_seconds") is not None for r in results),
               "gpu_process_observed_jobs": sum(r.get("gpu_process_observed", False) for r in results),
               "candidate_two_valid_repeats": sum(r["two_valid_repeats"] for r in records),
               "reference_results": [r for r in results if r["kind"] == "reference"], "candidates": records,
               "gpu_usage_note": "GPU process samples verify use; wall time is not active GPU compute time or billed cost.",
               "limitations": ["Small single-assay P0; not a Lab H0/H* or Meta comparison.",
                               "CHEMBL1061363 is CDK2/Cyclin A; assay organism is null and assignment confidence is 8 (homologous single protein), so measured species/construct are unconfirmed.",
                               "Uncalibrated continuous computational scores; no biological activity confirmation.",
                               "Rigid monomer CDK2 1DI8; absent loops and omitted incomplete residues; see receptor_manifest.json.",
                               "Single fixed input tautomer/protonation, two docking seeds, low exhaustiveness; exploratory only."]}
    dump(out / "summary.json", summary)
    lines = ["# RSI-Lab P0：Colab GPU 首轮实测", "", f"状态：{summary['status']}；20 个真实候选 + DTQ 共晶配体，固定种子 17/43。",
             f"完成工具执行与检查 {summary['completed_jobs']}/{summary['planned_jobs']} 项；候选两次姿态检查均有效 {summary['candidate_two_valid_repeats']}/{len(candidates)}。",
             f"总运行时间 {wall_seconds/60:.2f} 分钟；GNINA GPU 进程被监测到的任务 {summary['gpu_process_observed_jobs']}。", "",
             "这是 P0 工具/协议可行性实验：GNINA 的构象搜索在 CPU 上执行，CNN 重评分使用 GPU。没有运行 Lab/Meta、没有比较 H0/H*；尚未校准验收阈值，不报告官方通过率。CNNscore 是模型姿态分数，minimizedAffinity 是经验能量评分，不能解释成实测 IC50。", "",
             "数据来自 ChEMBL_37 的 CHEMBL1061363（CDK2/Cyclin A），assay organism 未注明，confidence=8（同源单蛋白靶点指派）；不能宣称测定物种或构建体已确认匹配 1DI8。", "",
             "## 共晶配体重对接（不做对齐的 heavy-atom RMSD）", ""]
    for r in summary["reference_results"]:
        lines.append(f"- seed {r['seed']}: {r['status']}; RMSD={r.get('rmsd_angstrom')}; pose_valid={r.get('pose_valid')}")
    lines += ["", "## 候选结果", "", "|候选|IC50 nM|完成/有效重复|CNNscore中位数|能量中位数|", "|---|---:|---:|---:|---:|"]
    for r in records:
        fmt = lambda v: "未形成" if v is None else f"{v:.4g}"
        lines.append(f"|{r['candidate_id']}|{fmt(r['ic50_nM'])}|{r['completed_repeats']}/{r['valid_repeats']}|{fmt(r['qualified_median_CNNscore'])}|{fmt(r['qualified_median_minimizedAffinity'])}|")
    lines += ["", "表中中位数只在两个固定种子均完成且姿态有效时形成；原始分数、全部失败和未完成状态保留在 results.csv 和 jobs/。未开始/截断不等同于科学验证失败。", "", "## 限制", ""]
    lines += [f"- {x}" for x in summary["limitations"]]
    (out / "report_zh.md").write_text("\n".join(lines) + "\n")
    return summary


def main(args):
    from rdkit import Chem
    from posebusters import PoseBusters
    data, out, gnina = Path(args.data).resolve(), Path(args.out).resolve(), Path(args.gnina).resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    gpu = probe(["nvidia-smi"])
    dump(out / "gpu_before.json", gpu)
    if gpu["returncode"] != 0:
        raise RuntimeError("NVIDIA GPU is required; no silent CPU fallback")
    version = probe([str(gnina), "--version"])
    dump(out / "gnina_version.json", version)
    if version["returncode"] != 0:
        raise RuntimeError("GNINA preflight failed")
    candidates = json.loads((data / "candidates.json").read_text())["candidates"]
    if len(candidates) != 20 or len({c["candidate_id"] for c in candidates}) != 20:
        raise ValueError("Expected exactly 20 unique frozen candidates")
    receptor = json.loads((data / "receptor/receptor_manifest.json").read_text())
    configs = {kind: PoseBusters(config=kind, max_workers=0).config for kind in ["dock", "redock"]}
    for config in configs.values():
        for module in config["modules"]:
            if module["function"] == "energy_ratio":
                module.setdefault("parameters", {})["num_threads"] = 1
    protocol = {"phase": "pilot", "frozen_at": now(), "seeds": SEEDS, "cpu": 2, "gpu_device": 0,
                "exhaustiveness": 4, "num_modes": 3, "scoring": "vina", "cnn_scoring": "rescore", "cnn": "dense_1_3",
                "pose_selection": "First pose after explicit CNNscore descending sort; no replacement after checks",
                "preparation": "ETKDGv3 seed20261019 single conformer; MMFF94s max500 convergence required; no tautomer/protonation search",
                "center": receptor["center"], "size": receptor["size"], "tau": None, "m_min": None,
                "dock_timeout_seconds": 180, "worker_timeout_seconds": 300, "wall_limit_seconds": 1800,
                "retries": 0, "posebusters_configs": configs, "gnina_sha256": sha(gnina),
                "gnina_version": version, "runner_sha256": sha(__file__), "python": sys.version,
                "platform": platform.platform(), "versions": {n: importlib.metadata.version(n) for n in ["rdkit", "posebusters", "numpy", "pandas", "scipy"]},
                "input_sha256": {str(p.relative_to(data)): sha(p) for p in sorted(data.rglob("*")) if p.is_file()},
                "score_semantics": {"minimizedAffinity": "empirical energy kcal/mol; lower is better", "CNNscore": "pose score; higher is better; not calibrated activity probability", "CNNaffinity": "model affinity prediction; higher is better; not measured pIC50"}}
    dump(out / "protocol.json", protocol)
    reference = next(m for m in Chem.SDMolSupplier(str(data / "receptor/reference.sdf")) if m is not None)
    control = {"candidate_id": "REFERENCE_DTQ", "smiles": Chem.MolToSmiles(Chem.RemoveHs(reference))}
    plan = [("reference", control)] + [("candidate", c) for c in candidates]
    results = []
    for kind, candidate in plan:
        for seed in SEEDS:
            job = out / "jobs" / f"{candidate['candidate_id']}_s{seed}"
            job.mkdir(parents=True)
            spec = {"candidate": candidate, "kind": kind, "seed": seed, "data": str(data), "gnina": str(gnina), "protocol": protocol}
            dump(job / "spec.json", spec)
            remaining = 1800 - (time.monotonic() - started)
            r = {"candidate_id": candidate["candidate_id"], "kind": kind, "seed": seed, "status": "not_started_budget", "scores": None, "pose_valid": None, "cpu_seconds": None}
            if remaining > 1:
                job_started = time.monotonic()
                with (job / "worker.stdout.txt").open("w") as stdout, (job / "worker.stderr.txt").open("w") as stderr:
                    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(job / "spec.json")], stdout=stdout, stderr=stderr, start_new_session=True)
                    try:
                        p.wait(timeout=min(300, remaining))
                        r["status"] = "worker_error"
                    except subprocess.TimeoutExpired:
                        os.killpg(p.pid, signal.SIGKILL)
                        p.wait()
                        r["status"] = "worker_timeout"
                if (job / "result.json").exists():
                    try:
                        r = json.loads((job / "result.json").read_text())
                    except (json.JSONDecodeError, OSError) as e:
                        (job / "result.json").rename(job / "result.corrupt.json")
                        r.update(status="worker_error", error=f"Unreadable worker result: {e}")
                r["parent_observed_wall_seconds"] = time.monotonic() - job_started
            dump(job / "result.json", r)
            results.append(r)
            print(json.dumps({"progress": len(results), "total": 42, "candidate_id": r["candidate_id"], "seed": seed, "status": r["status"], "scores": r.get("scores"), "pose_valid": r.get("pose_valid")}), flush=True)
    try:
        gpu_after = probe(["nvidia-smi"])
    except Exception as e:
        gpu_after = {"error": f"{type(e).__name__}: {e}"}
    dump(out / "gpu_after.json", gpu_after)
    summary = summarize(out, candidates, results, time.monotonic() - started)
    dump(out / "artifact_hashes.json", {str(p.relative_to(out)): sha(p) for p in sorted(out.rglob("*")) if p.is_file()})
    print(json.dumps({k: v for k, v in summary.items() if k not in {"candidates", "reference_results"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data")
    parser.add_argument("--out")
    parser.add_argument("--gnina")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    opts = parser.parse_args()
    if opts.worker:
        worker(opts.worker)
    elif all([opts.data, opts.out, opts.gnina]):
        main(opts)
    else:
        parser.error("--data, --out, and --gnina are required")
