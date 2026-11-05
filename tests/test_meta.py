"""Offline Meta gates exercised with complete synthetic campaign archives."""
import argparse
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lab_harness import controller as c, demo, meta


class MetaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lab_meta_unit_")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.cleanup)
        self.no_models = mock.patch.object(c.transport, "invoke", side_effect=AssertionError("No live calls in tests"))
        self.no_models.start()
        self.addCleanup(self.no_models.stop)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(lambda: self.quiet.__exit__(None, None, None))
        self.dev = demo.run(self.root / "development")
        self.workspace = self.root / "meta"
        meta.prepare(argparse.Namespace(development_run=self.dev, out=self.workspace, min_pairs=2))

    def cleanup(self):
        for path in self.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        self.tmp.cleanup()

    def proposal(self):
        packet = c.read(self.workspace / "input.json")
        response = {"parent_harness_hash": packet["parent_harness_hash"],
                    "input_packet_sha256": c.sha(self.workspace / "input.json"),
                    "decision": "propose_change", "diagnosis": "Test lossless compact view",
                    "evidence_refs": ["evaluation.json"], "hypothesis": "Less presentation overhead",
                    "replacement_lab_planner": None, "input_view_mode": "compact_json_v1",
                    "regression_risks": ["Only a synthetic software fixture"]}
        response_path = self.root / "proposal.json"
        c.dump(response_path, response)
        return meta.propose(argparse.Namespace(workspace=self.workspace, response=response_path))

    def make_plan(self):
        self.proposal()
        self.registrations = []
        for index, start in enumerate((100, 200)):
            data = demo.make_data(self.root / f"data_{index}", start_index=start)
            self.registrations.append([str(data), str(data / "protocol.json"), str(self.root / f"baseline_{index}"), str(self.root / f"candidate_{index}")])
        return meta.plan(argparse.Namespace(workspace=self.workspace, pair=self.registrations))

    def run_pairs(self, wrong_phase=False, wrong_harness=False):
        for index, start in enumerate((100, 200)):
            _, _, baseline, candidate = self.registrations[index]
            demo.run(baseline, phase="development" if wrong_phase and index == 0 else "validation", start_index=start)
            demo.run(candidate, harness=None if wrong_harness and index == 0 else self.workspace / "candidate", phase="validation", start_index=start)

    def gate(self, pairs=None):
        return meta.gate(argparse.Namespace(workspace=self.workspace, pair=pairs, out=self.root / "gate.json"))

    def rehash(self, run):
        c.dump(run / "artifact_hashes.json", meta._tree_hashes(run))

    def test_complete_replay_gate_never_releases(self):
        result = self.make_plan()
        self.assertEqual(result["pairs"], 2)
        self.run_pairs()
        result = self.gate()
        self.assertEqual(result["decision"], "demo_only")
        self.assertFalse(result["eligible_for_next_development_run"])
        self.assertTrue(result["synthetic"])
        self.assertIsNone(result["meta_usage"])
        self.assertEqual(len(result["pairs"]), 2)
        self.assertEqual(result["mean_docking_energy_gain_kcal_mol"], 0.0)
        self.assertEqual(result["pairs"][0]["baseline_mean_qualified_docking_energy_kcal_mol"], -7.0)
        self.assertFalse((self.workspace / "draft").exists())
        self.assertEqual(c.read(self.workspace / "meta.json")["model_attempts"], 0)

    def test_completed_archive_tamper_and_wrong_phase_rejected(self):
        with self.assertRaisesRegex(ValueError, "validation"):
            meta.completed_run(self.dev, "validation")
        report = c.read(self.dev / "evaluation.json")
        report["observed_input_plus_output_tokens"] = 100
        c.dump(self.dev / "evaluation.json", report)
        with self.assertRaisesRegex(ValueError, "archive changed"):
            meta.completed_run(self.dev)
        state = c.read(self.dev / "state.json")
        state["usage_tokens"] = 100
        c.dump(self.dev / "state.json", state)
        self.rehash(self.dev)
        with self.assertRaisesRegex(ValueError, "token totals"):
            meta.completed_run(self.dev)

    def test_rebuilt_result_audit_rejects_self_consistent_state_report_edit(self):
        state = c.read(self.dev / "state.json")
        cid = next(iter(state["results"]))
        state["results"][cid]["qualified_median_vina_affinity"] = -100.0
        state["evidence"][state["results"][cid]["evidence_id"]] = state["results"][cid]
        report = c.read(self.dev / "evaluation.json")
        report["continuous_results"] = [state["results"][key] for key in report["shortlist"]]
        c.dump(self.dev / "state.json", state)
        c.dump(self.dev / "evaluation.json", report)
        self.rehash(self.dev)
        with self.assertRaisesRegex(ValueError, "raw batch receipts"):
            meta.completed_run(self.dev)

    def test_changed_candidate_rejected(self):
        self.make_plan()
        prompt = self.workspace / "candidate/lab_planner.md"
        prompt.chmod(0o644)
        prompt.write_text("changed after freezing")
        with self.assertRaisesRegex(ValueError, "candidate changed"):
            self.gate()

    def test_pair_wrong_harness_and_phase_rejected(self):
        self.make_plan()
        self.run_pairs(wrong_harness=True)
        with self.assertRaisesRegex(ValueError, "wrong baseline or candidate"):
            self.gate()

    def test_pair_phase_rejected(self):
        self.make_plan()
        self.run_pairs(wrong_phase=True)
        with self.assertRaisesRegex(ValueError, "validation campaign"):
            self.gate()

    def test_plan_requires_future_paths_and_forbids_replanning(self):
        self.make_plan()
        with self.assertRaisesRegex(ValueError, "replanning"):
            meta.plan(argparse.Namespace(workspace=self.workspace, pair=self.registrations))
        # A second workspace can propose but cannot register an already run path.
        other = self.root / "other_meta"
        meta.prepare(argparse.Namespace(development_run=self.dev, out=other, min_pairs=2))
        self.workspace = other
        self.proposal()
        Path(self.registrations[0][2]).mkdir()
        with self.assertRaisesRegex(ValueError, "nonexistent"):
            meta.plan(argparse.Namespace(workspace=self.workspace, pair=self.registrations))

    def test_gate_requires_plan_and_all_registered_pairs(self):
        self.proposal()
        with self.assertRaisesRegex(ValueError, "prospective validation plan"):
            self.gate()
        # Fresh workspace to register the complete prospective plan.
        self.workspace = self.root / "planned_meta"
        meta.prepare(argparse.Namespace(development_run=self.dev, out=self.workspace, min_pairs=2))
        self.make_plan()
        with self.assertRaisesRegex(ValueError, "all and only"):
            self.gate([self.registrations[0][2:]])
        altered = c.read(self.workspace / "validation_plan.json")
        altered["pairs"] = altered["pairs"][:1]
        c.dump(self.workspace / "validation_plan.json", altered)
        with self.assertRaisesRegex(ValueError, "plan changed"):
            self.gate()

    def test_scientific_protocol_drift_rejected_before_execution(self):
        self.proposal()
        registrations = []
        for index, start in enumerate((100, 200)):
            data = demo.make_data(self.root / f"drift_{index}", start_index=start)
            protocol = c.read(data / "protocol.json")
            protocol["exhaustiveness"] = 999
            c.dump(data / "protocol.json", protocol)
            registrations.append([data, data / "protocol.json", self.root / f"b{index}", self.root / f"q{index}"])
        with self.assertRaisesRegex(ValueError, "scientific protocol"):
            meta.plan(argparse.Namespace(workspace=self.workspace, pair=registrations))
        a = {"protocol_id": "a", "input_sha256": {"a": "hash"}, "phase": "development", "frozen_at": "then", "seeds": [17, 43], "exhaustiveness": 8}
        b = {**a, "protocol_id": "b", "input_sha256": {"b": "other"}, "phase": "validation", "frozen_at": "now"}
        self.assertEqual(meta.scientific_protocol(a), meta.scientific_protocol(b))
        b["exhaustiveness"] = 16
        self.assertNotEqual(meta.scientific_protocol(a), meta.scientific_protocol(b))

    def test_live_chemistry_freshness_does_not_accept_renamed_ids(self):
        with self.assertRaisesRegex(ValueError, "SMILES"):
            meta.check_new_chemistry([{"candidate_id": "new_id", "smiles": "CCO"}], {"CCO"}, "live")
        self.assertEqual(meta.check_new_chemistry([{"smiles": "CCC"}], {"CCO"}, "live"), {"CCC"})
        self.assertEqual(meta.check_new_chemistry([{"smiles": "C"}], {"C"}, "replay"), {"C"})
        with self.assertRaises(ValueError):
            meta.check_new_chemistry([{"smiles": "CC[NH3+]", "canonical_parent_identity": "CCN"}], {"CCN"}, "live")

    def test_failed_meta_transport_retains_standard_usage_receipt(self):
        state = c.read(self.workspace / "meta.json")
        state["execution_mode"] = "live"
        c.dump(self.workspace / "meta.json", state)
        receipt = {"response": None, "usage": {"input_tokens": 3}, "usage_complete": False,
                   "error": "mock timeout", "wall_seconds": 1.0, "returncode": -9, "backend": "codex"}
        with mock.patch.object(c.transport, "invoke", return_value=receipt):
            with self.assertRaisesRegex(ValueError, "mock timeout"):
                meta.propose(argparse.Namespace(workspace=self.workspace, response=None))
        self.assertEqual(c.read(self.workspace / "call_receipt.json"), receipt)
        self.assertEqual(c.read(self.workspace / "meta.json")["status"], "failed")

    def test_evaluation_denominator_and_identity_are_checked(self):
        report = c.read(self.dev / "evaluation.json")
        original = dict(report)
        report["shortlist_size"] = 1
        c.dump(self.dev / "evaluation.json", report)
        self.rehash(self.dev)
        with self.assertRaisesRegex(ValueError, "denominator"):
            meta.completed_run(self.dev)
        original["model"] = "other-model"
        c.dump(self.dev / "evaluation.json", original)
        self.rehash(self.dev)
        with self.assertRaisesRegex(ValueError, "identity"):
            meta.completed_run(self.dev)

    def test_receptor_hashes_cannot_change_with_candidate_pool(self):
        a = {"seeds": [17, 43], "input_sha256": {"candidates.json": "a", "receptor/receptor.pdbqt": "fixed"}}
        b = {"seeds": [17, 43], "input_sha256": {"candidates.json": "b", "receptor/receptor.pdbqt": "fixed"}}
        self.assertEqual(meta.scientific_protocol(a), meta.scientific_protocol(b))
        b["input_sha256"]["receptor/receptor.pdbqt"] = "changed"
        self.assertNotEqual(meta.scientific_protocol(a), meta.scientific_protocol(b))


class DecisionArithmeticTests(unittest.TestCase):
    def pair(self, **overrides):
        return {"baseline_pose_valid": 4, "candidate_pose_valid": 4,
                "baseline_mean_qualified_docking_energy_kcal_mol": -7.0,
                "candidate_mean_qualified_docking_energy_kcal_mol": -7.0,
                "baseline_tokens": 100, "candidate_tokens": 100, **overrides}

    def assess(self, *pairs):
        return meta.assess_pairs(list(pairs), meta.DEFAULT_RULES, False)

    def test_lower_energy_or_better_coverage_can_qualify(self):
        energy = self.pair(candidate_mean_qualified_docking_energy_kcal_mol=-7.1)
        result = self.assess(energy, energy)
        self.assertEqual(result["decision"], "accepted")
        self.assertAlmostEqual(result["mean_docking_energy_gain_kcal_mol"], 0.1)
        self.assertTrue(result["quality_path_passed"])
        coverage = self.assess(self.pair(candidate_pose_valid=5), self.pair())
        self.assertTrue(coverage["quality_path_passed"])
        higher = self.pair(candidate_mean_qualified_docking_energy_kcal_mol=-6.9)
        self.assertFalse(self.assess(higher, higher)["quality_path_passed"])

    def test_ten_percent_token_reduction_respects_energy_noninferiority(self):
        within_tolerance = self.pair(candidate_tokens=90, candidate_mean_qualified_docking_energy_kcal_mol=-6.9)
        self.assertTrue(self.assess(within_tolerance, within_tolerance)["cost_path_passed"])
        outside_tolerance = self.pair(candidate_tokens=1, candidate_pose_valid=5,
                                      candidate_mean_qualified_docking_energy_kcal_mol=-6.89)
        self.assertEqual(self.assess(outside_tolerance, outside_tolerance)["decision"], "rejected")

    def test_per_pair_coverage_cannot_regress_even_with_better_energy(self):
        mixed = [self.pair(candidate_pose_valid=2, candidate_mean_qualified_docking_energy_kcal_mol=-8.0),
                 self.pair(baseline_pose_valid=1, candidate_pose_valid=5)]
        self.assertEqual(self.assess(*mixed)["decision"], "rejected")
        self.assertEqual(self.assess(self.pair(), self.pair())["decision"], "rejected")

    def test_all_failed_or_one_noncomparable_pair_never_wins_on_cost(self):
        empty = self.pair(baseline_pose_valid=0, candidate_pose_valid=0,
                          baseline_mean_qualified_docking_energy_kcal_mol=None,
                          candidate_mean_qualified_docking_energy_kcal_mol=None, candidate_tokens=1)
        missing_baseline = self.pair(baseline_pose_valid=0, candidate_pose_valid=5,
                                     baseline_mean_qualified_docking_energy_kcal_mol=None, candidate_tokens=1)
        good = self.pair(candidate_pose_valid=5, candidate_mean_qualified_docking_energy_kcal_mol=-9.0, candidate_tokens=1)
        for pairs in ((empty, empty), (empty, good), (missing_baseline, good)):
            result = self.assess(*pairs)
            self.assertEqual(result["decision"], "rejected")
            self.assertFalse(result["cost_path_passed"])
            self.assertFalse(result["quality_path_passed"])
            self.assertFalse(result["all_pairs_have_qualified_docking_energy"])
            self.assertIsNone(result["mean_docking_energy_gain_kcal_mol"])

    def test_synthetic_success_is_still_demo_only(self):
        result = meta.assess_pairs([self.pair(candidate_pose_valid=5, candidate_tokens=1)] * 2, meta.DEFAULT_RULES, True)
        self.assertTrue(result["quality_path_passed"])
        self.assertEqual(result["decision"], "demo_only")
        self.assertFalse(result["eligible_for_next_development_run"])


if __name__ == "__main__":
    unittest.main()
