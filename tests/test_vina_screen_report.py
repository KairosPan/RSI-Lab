"""Reporter checks using temporary software fixtures; no docking is executed."""
import contextlib
import csv
import io
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from scripts import report_vina_screen as report
from scripts.vina_contract import ENGINE, ENGINE_VERSION, PROFILE, SEEDS
from test_docking_archive import write_job


def dump(path, value):
    path.write_text(report.json.dumps(value, indent=2) + "\n")


class VinaScreenReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "screen"
        for name in ("data", "frozen", "jobs"):
            (self.root / name).mkdir(parents=True)
        self.candidates = [{"candidate_id": cid, "smiles": smiles, "canonical_smiles": smiles}
                           for cid, smiles in [("tie", "C"), ("above", "CC"), ("invalid", "CCC"), ("below", "CCCC")]]
        self.candidates.extend({"candidate_id": f"unrun_{i}", "smiles": "C", "canonical_smiles": "C"} for i in range(116))
        dump(self.root / "data/candidates.json", {"candidates": self.candidates})
        scripts = Path(report.__file__).parent
        for name in ("vina_worker.py", "run_vina_batch.py", "vina_contract.py", "run_vina_screen.py"):
            shutil.copyfile(scripts / name, self.root / "frozen" / name)
        self.protocol = {**PROFILE, "center": [0.0] * 3, "size": [22.0] * 3,
                         "vina_sha256": "c" * 64,
                         "vina_version": {"stdout": f"{ENGINE} v{ENGINE_VERSION}", "returncode": 0},
                         "input_sha256": {"candidates.json": report.sha(self.root / "data/candidates.json")},
                         "posebusters_configs": {"dock": {"modules": [
                             {"chosen_binary_test_output": ["mol_pred_loaded", "sanitization"]}]}}}
        for key, name in (("runner_sha256", "vina_worker.py"), ("adapter_sha256", "run_vina_batch.py"),
                          ("contract_sha256", "vina_contract.py")):
            self.protocol[key] = report.sha(self.root / "frozen" / name)
        dump(self.root / "protocol.json", self.protocol)
        shutil.copyfile(self.root / "protocol.json", self.root / "frozen/protocol.json")
        self.plan = {"candidate_count": len(self.candidates), "seeds": list(SEEDS), "threshold": None,
                     "max_seed_jobs": len(self.candidates) * 2,
                     "protocol_sha256": report.sha(self.root / "protocol.json"),
                     "driver_sha256": report.sha(self.root / "frozen/run_vina_screen.py")}
        dump(self.root / "plan.json", self.plan)

    def add_job(self, index, seed, energy=-7.0):
        candidate = self.candidates[index]
        folder = self.root / "jobs" / f"{candidate['candidate_id']}_s{seed}"
        result = write_job(folder, candidate, self.protocol, seed, energy)
        result["parent_observed_returncode"] = 0
        dump(folder / "result.json", result)
        return folder

    def add_summary(self, status="deadline_reached"):
        results, unrun = [], []
        for candidate in self.candidates:
            for seed in SEEDS:
                file = self.root / "jobs" / f"{candidate['candidate_id']}_s{seed}" / "result.json"
                if file.exists():
                    results.append(report.read(file))
                else:
                    unrun.append({"candidate_id": candidate["candidate_id"], "seed": seed})
        summary = {"schema_version": "bounded_vina_screen_execution_v1", "status": status,
                   "candidate_count": len(self.candidates), "settled_seed_jobs": len(results),
                   "unrun_seed_jobs": unrun, "worker_cpu_seconds": sum(row.get("cpu_seconds") or 0 for row in results),
                   "cpu_accounting_complete": all(row.get("cpu_seconds") is not None for row in results),
                   "wall_seconds": 12.0, "plan_sha256": report.sha(self.root / "plan.json"),
                   "protocol_sha256": report.sha(self.root / "protocol.json")}
        dump(self.root / "execution_summary.json", summary)
        return summary

    def test_all_inputs_preserved_and_native_ranking_has_no_formal_claim(self):
        for index, energies in enumerate(((-8.0, -6.0), (-6.6, -6.4), (-10.0, -10.0), (-9.0, -9.0))):
            for seed, energy in zip(SEEDS, energies):
                self.add_job(index, seed, energy)
        result, rows, ranking = report.build_report(self.root)
        self.assertEqual([row["candidate_id"] for row in rows], [row["candidate_id"] for row in self.candidates])
        self.assertEqual([(row["candidate_id"], row["median_vina_affinity_kcal_mol"]) for row in ranking],
                         [("below", -9.0), ("tie", -7.0), ("above", -6.5)])
        self.assertEqual(result["candidate_status_counts"], {"two_seed_valid": 3, "pose_invalid_or_unknown": 1, "unrun": 116})
        self.assertIsNone(result["threshold"])
        self.assertFalse(result["formal_selection_assessed"])
        self.assertEqual(result["formal_min_distinct_compounds"], 100)
        self.assertEqual(result["formal_qualification"], "not_assessed_no_frozen_threshold")
        self.assertEqual(result["minimum_additional_distinct_valid_candidates_to_reach_100"], 97)
        self.assertIn("未测量 LLM 或 Meta 改善", report.markdown(result))

    def test_tampered_score_rejected_against_native_model_one(self):
        folder = self.add_job(0, 17)
        result = report.read(folder / "result.json")
        result["scores"]["vina_affinity"] = -99.0
        dump(folder / "result.json", result)
        with self.assertRaisesRegex(ValueError, "native Vina MODEL 1"):
            report.build_report(self.root)

    def test_tampered_native_pose_rejected_even_with_updated_artifact_hash(self):
        folder = self.add_job(0, 17)
        poses = folder / "poses.pdbqt"
        poses.write_text(poses.read_text().replace("-7.000", "-99.000"))
        result = report.read(folder / "result.json")
        result["scores"]["vina_affinity"] = -99.0
        result["artifacts"]["poses.pdbqt"] = report.sha(poses)
        dump(folder / "result.json", result)
        with self.assertRaisesRegex(ValueError, "affinity or pose rank"):
            report.build_report(self.root)

    def test_cutoff_statuses_keep_unknown_cpu_and_unrun_distinct(self):
        for index, status in enumerate(("global_deadline_timeout", "parent_interrupted", "launch_failed")):
            folder = self.add_job(index, 17)
            result = report.read(folder / "result.json")
            result.update(status=status, scores=None, identity_preserved=None, pose_valid=None,
                          cpu_seconds=0.0 if status == "launch_failed" else None,
                          parent_observed_returncode=-15, error="Software test cutoff")
            dump(folder / "result.json", result)
        self.add_summary()
        result, rows, ranking = report.build_report(self.root)
        self.assertEqual(ranking, [])
        self.assertEqual(len(rows), 120)
        self.assertEqual(result["seed_job_status_counts"], {"global_deadline_timeout": 1, "unrun": 237,
                                                          "parent_interrupted": 1, "launch_failed": 1})
        self.assertIsNone(rows[0]["seed_17_cpu_seconds"])
        self.assertIsNone(rows[0]["seed_17_energy"])
        self.assertEqual(result["timing"]["cpu_seconds"], {"known_sum": 0.0, "observed_jobs": 1,
                                                          "complete_for_reported_jobs": False})
        self.assertFalse(result["timing"]["full_run_cpu_accounting_complete"])

    def test_cutoff_cannot_retain_successful_score(self):
        folder = self.add_job(0, 17)
        result = report.read(folder / "result.json")
        result["status"] = "global_deadline_timeout"
        dump(folder / "result.json", result)
        with self.assertRaisesRegex(ValueError, "retains successful output"):
            report.build_report(self.root)

    def test_completed_requires_engine_and_parent_exit_receipts(self):
        folder = self.add_job(0, 17)
        original = report.read(folder / "result.json")
        for patch in ({"parent_observed_returncode": -15}, {"parent_observed_returncode": None},
                      {"parent_observed_returncode": False}, {"engine": "GNINA"}, {"vina_sha256": "d" * 64}):
            with self.subTest(patch=patch):
                dump(folder / "result.json", {**original, **patch})
                with self.assertRaises(ValueError):
                    report.build_report(self.root)

    def test_frozen_source_and_protocol_changes_rejected(self):
        for relative in ("protocol.json", "frozen/protocol.json", "frozen/vina_worker.py",
                         "frozen/run_vina_batch.py", "frozen/vina_contract.py", "frozen/run_vina_screen.py"):
            with self.subTest(file=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(ValueError, "[Pp]rotocol|[Ff]rozen"):
                    report.build_report(self.root)
                path.write_bytes(original)

    def test_summary_must_match_hashes_exact_job_partition_and_cpu(self):
        self.add_job(0, 17)
        summary = self.add_summary()
        report.build_report(self.root)
        for patch in ({"plan_sha256": "0" * 64}, {"protocol_sha256": "0" * 64},
                      {"candidate_count": 121}, {"settled_seed_jobs": 2},
                      {"unrun_seed_jobs": summary["unrun_seed_jobs"][:-1]},
                      {"unrun_seed_jobs": summary["unrun_seed_jobs"] + [summary["unrun_seed_jobs"][0]]},
                      {"worker_cpu_seconds": 0.0}, {"cpu_accounting_complete": False}, {"status": "completed"}):
            with self.subTest(patch=patch):
                dump(self.root / "execution_summary.json", {**summary, **patch})
                with self.assertRaisesRegex(ValueError, "Execution summary"):
                    report.build_report(self.root)

    def test_pending_job_cannot_be_declared_unrun_in_final_summary(self):
        folder = self.add_job(0, 17)
        (folder / "result.json").unlink()
        result, rows, _ = report.build_report(self.root)
        self.assertEqual(rows[0]["status"], "pending_or_interrupted")
        self.assertEqual(result["reported_seed_jobs"], 0)
        self.add_summary()
        with self.assertRaisesRegex(ValueError, r"settled \+ unrun"):
            report.build_report(self.root)

    def test_outputs_keep_all_rows_and_refuse_overwrite(self):
        output = self.root / "report"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(report.main(["--run", str(self.root), "--out", str(output)]), 0)
            before = (output / "report.json").read_bytes()
            self.assertEqual(report.main(["--run", str(self.root), "--out", str(output)]), 1)
        self.assertEqual(before, (output / "report.json").read_bytes())
        with (output / "all_results.csv").open(newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 120)
        with (output / "ranking.csv").open(newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])
        self.assertFalse(report.read(output / "report.json")["formal_selection_assessed"])


if __name__ == "__main__":
    unittest.main()
