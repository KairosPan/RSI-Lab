"""Synthetic contract tests only: no model call, docking, or experiment output."""
import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lab_harness import controller as c
from lab_harness import input_view as view
from lab_harness import demo
from scripts.vina_contract import BATCH_SCHEMA, ENGINE, PROFILE


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lab_controller_unit_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        candidates = [{"candidate_id": f"C{i:02d}", "smiles": "C", "ic50_nM": 100.0,
                       "pIC50": 7.0, "assay_id": "A1", "activity_ids": [i], "document_ids": ["DOC"],
                       "structure": {"molecular_weight": 16.0, "scaffold_id": "S0", "logP": 0.1,
                                     "max_morgan_tanimoto_to_p0": 0.2}} for i in range(20)]
        c.dump(self.data / "candidates.json", {"candidates": candidates})
        c.dump(self.data / "manifest.json", {"test_fixture": True})
        self.protocol = {**PROFILE, "protocol_id": "test", "tau": None, "m_min": None,
                         "vina_version": {"stdout": "AutoDock Vina v1.2.7", "returncode": 0},
                         "vina_sha256": "a" * 64,
                         "runner_sha256": c.sha(c.ROOT / "scripts/vina_worker.py"),
                         "adapter_sha256": c.sha(c.ROOT / "scripts/run_vina_batch.py"),
                         "contract_sha256": c.sha(c.ROOT / "scripts/vina_contract.py"),
                         "input_sha256": {name: c.sha(self.data / name) for name in ["candidates.json", "manifest.json"]}}
        c.dump(self.root / "protocol.json", self.protocol)
        self.harness = self.root / "harness"
        self.harness.mkdir()
        c.dump(self.harness / "manifest.json", {"schema_version": "lab_harness_v1", "name": "test", "version": "1"})
        c.dump(self.harness / "input_view.json", {"mode": "expanded_json_v1"})
        (self.harness / "lab_planner.md").write_text("Use evidence and return the schema. This is a synthetic unit test.")
        self.run = self.root / "test_campaign"
        with contextlib.redirect_stdout(io.StringIO()):
            c.init(argparse.Namespace(data=self.data, protocol=self.root / "protocol.json", out=self.run,
                                      model="never_called_test", effort="ultra", harness=self.harness,
                                      phase="development", execution_mode="live"))
        self.state = c.read(self.run / "state.json")

    def response(self, action="tool_batch", ids=None):
        return {"input_state_version": self.state["state_version"], "next_action": action,
                "reason_brief": "Synthetic contract test", "evidence_ids": ["ev_ic50_C00"],
                "tool_requests": [{"request_id": "online_001", "tool_id": c.TOOL,
                                   "candidate_ids": ids or ["C00", "C01"], "parameter_profile": c.PROFILE}] if action == "tool_batch" else [],
                "read_evidence_ids": ["ev_ic50_C00"] if action == "read_evidence" else [],
                "memory_delta": [], "shortlist": [{"candidate_id": f"C{i:02d}", "evidence_ids": [f"ev_ic50_C{i:02d}"], "uncertainty": "Unknown pose"} for i in range(5)],
                "open_questions": [], "stop_reason": "test stop" if action == "submit" else None}

    def memory(self, eid="ev_ic50_C00"):
        return {"kind": "observation", "subject_ids": ["C00"], "statement": "Synthetic sourced observation",
                "evidence_ids": [eid], "supersedes": None,
                "scope": {"target_id": "CHEMBL301", "construct_id": None, "assay_ids": ["A1"],
                          "protocol_id": "test", "tool_versions": [], "applicable_conditions": []},
                "supporting_evidence_ids": [eid], "challenging_evidence_ids": [], "status": "proposed",
                "limitations": ["Synthetic unit test"], "depends_on_memory_ids": []}

    def batch(self, state):
        rid = state["pending_request"]
        request = state["requests"][rid]["request"]
        batch = self.root / (rid + "_fixture")
        batch.mkdir()
        c.dump(batch / "request.json", request)
        (batch / "protocol.json").write_bytes((self.run / "frozen/protocol.json").read_bytes())
        engine_receipt = {key: self.protocol[key] for key in ("engine", "vina_sha256", "vina_version")}
        c.dump(batch / "engine.json", engine_receipt)
        jobs = []
        for cid in request["candidate_ids"]:
            for seed in [17, 43]:
                folder = batch / "jobs" / f"{cid}_s{seed}"
                folder.mkdir(parents=True)
                c.dump(folder / "spec.json", {"protocol": self.protocol, "seed": seed, "kind": "candidate",
                    "candidate": next(row for row in state["candidates"] if row["candidate_id"] == cid)})
                row = {"candidate_id": cid, "seed": seed, "status": "completed", "returncode": 0,
                       "scores": {"vina_affinity": -7.0 - seed / 100},
                       "pose_valid": True, "pose_checks": {"test_check": True}, "identity_preserved": True,
                       "selected_model": 1, "pose_count": 1, "cpu_seconds": 1.0, **engine_receipt}
                c.dump(folder / "result.json", row)
                (folder / "posebusters.csv").write_text("test_check\nTrue\n")
                (folder / "poses.pdbqt").write_text(f"MODEL 1\nREMARK VINA RESULT: {row['scores']['vina_affinity']} 0 0\nATOM      1  C\nENDMDL\n")
                jobs.append(row)
        summary = {"schema_version": BATCH_SCHEMA, **{key: request[key] for key in ["campaign_id", "phase", "request_id", "protocol_sha256", "candidates_sha256", "manifest_sha256"]},
                   "state_version": request["input_state_version"] + 1, "request_sha256": c.sha(batch / "request.json"),
                   "adapter_sha256": state["frozen_hashes"]["run_vina_batch.py"],
                   "runner_sha256": self.protocol["runner_sha256"], "contract_sha256": self.protocol["contract_sha256"], "jobs": jobs,
                   "candidates": [c.aggregate_jobs(cid, jobs) for cid in request["candidate_ids"]],
                   "worker_cpu_seconds": float(len(jobs)), "cpu_accounting_complete": True, "wall_seconds": 3.0}
        c.dump(batch / "batch_summary.json", summary)
        self.hash_batch(batch)
        return batch

    def hash_batch(self, batch):
        c.dump(batch / "artifact_hashes.json", {str(p.relative_to(batch)): c.sha(p) for p in batch.rglob("*") if p.is_file() and p.name != "artifact_hashes.json"})

    def test_public_features_and_no_prior_scores(self):
        self.assertEqual(self.state["candidates"][0]["features"]["logP"], 0.1)
        self.assertNotIn("max_morgan_tanimoto_to_p0", self.state["candidates"][0]["features"])
        self.assertEqual(self.state["llm_calls"], [])
        c.assert_frozen(self.run, self.state)

    def test_bad_response_is_transactional(self):
        original = copy.deepcopy(self.state)
        response = self.response()
        response["memory_delta"] = [self.memory()]
        response["tool_requests"][0]["candidate_ids"] = ["C00"] * 2
        with self.assertRaises(ValueError):
            c.apply_response(self.run, self.state, response)
        self.assertEqual(self.state, original)
        self.assertFalse((self.run / "pending").exists())

    def test_stale_and_unknown_references_rejected(self):
        response = self.response()
        response["input_state_version"] = 99
        with self.assertRaisesRegex(ValueError, "Stale"):
            c.validate_response(response, self.state)
        response = self.response()
        response["shortlist"][0]["evidence_ids"] = ["ev_ic50_C01"]
        with self.assertRaisesRegex(ValueError, "own record"):
            c.validate_response(response, self.state)

    def test_budget_batch_and_final_call(self):
        response = self.response(ids=[f"C{i:02d}" for i in range(5)])
        with self.assertRaises(ValueError):
            c.validate_response(response, self.state)
        self.state["online_reserved_ids"] = [f"C{i:02d}" for i in range(2, 9)]
        with self.assertRaisesRegex(ValueError, "budget"):
            c.validate_response(self.response(), self.state)
        with self.assertRaisesRegex(ValueError, "Only submit"):
            c.validate_response(self.response("read_evidence"), self.state, only_submit=True)

    def test_submit_and_auto_lock_require_real_observation(self):
        with self.assertRaisesRegex(ValueError, "observed actual"):
            c.validate_response(self.response("submit"), self.state)
        self.state["current_shortlist"] = self.response()["shortlist"]
        c.lock_shortlist(self.run, self.state, "budget")
        self.assertEqual(self.state["status"], "halted_without_shortlist")
        self.assertIsNone(self.state["locked_shortlist_sha256"])

    def test_reservation_and_worker_counter_separate_from_lab(self):
        self.state["state_version"] = 7
        response = self.response()
        state = c.apply_response(self.run, self.state, response)
        self.assertEqual(state["state_version"], 8)
        self.assertEqual(state["online_reserved_ids"], ["C00", "C01"])
        self.assertEqual(state["requests"]["online_001"]["request"]["input_state_version"], 0)
        response["input_state_version"] = 8
        with self.assertRaisesRegex(ValueError, "already reserved"):
            c.validate_response(response, state)

    def test_memory_supersedes_retains_history(self):
        response = self.response("read_evidence")
        response["memory_delta"] = [self.memory()]
        self.state = c.apply_response(self.run, self.state, response)
        response = self.response("read_evidence")
        replacement = self.memory()
        replacement["supersedes"] = "mem_0001"
        response["memory_delta"] = [replacement]
        state = c.apply_response(self.run, self.state, response)
        self.assertEqual(len(state["memory"]), 2)
        self.assertEqual(state["memory"][1]["review_state"], "pending")
        self.assertEqual(state["memory_review_events"][0]["review_state"], "superseded")

    def test_tool_feedback_requires_sourced_memory(self):
        self.state["recent_tool_evidence_ids"] = ["ev_ic50_C00"]
        with self.assertRaisesRegex(ValueError, "feedback"):
            c.validate_response(self.response("read_evidence"), self.state)
        response = self.response("read_evidence")
        response["memory_delta"] = [self.memory()]
        c.validate_response(response, self.state)

    def test_ingestion_idempotent_and_scores_recomputed(self):
        state = c.apply_response(self.run, self.state, self.response())
        c.dump(self.run / "state.json", state)
        batch = self.batch(state)
        with contextlib.redirect_stdout(io.StringIO()):
            c.ingest(argparse.Namespace(run=self.run, batch=batch))
        once = c.read(self.run / "state.json")
        with contextlib.redirect_stdout(io.StringIO()):
            c.ingest(argparse.Namespace(run=self.run, batch=batch))
        self.assertEqual(once, c.read(self.run / "state.json"))
        self.assertEqual(once["worker_versions"]["online"], 1)
        self.assertTrue(once["results"]["C00"]["two_valid_repeats"])

    def test_tampered_input_and_raw_pb_rejected(self):
        state = c.apply_response(self.run, self.state, self.response())
        batch = self.batch(state)
        specpath = batch / "jobs/C00_s17/spec.json"
        spec = c.read(specpath)
        spec["candidate"]["candidate_id"] = "C19"
        c.dump(specpath, spec)
        self.hash_batch(batch)
        with self.assertRaisesRegex(ValueError, "Job inputs"):
            c.validate_batch(batch, self.run, state)
        spec["candidate"]["candidate_id"] = "C00"
        c.dump(specpath, spec)
        (batch / "jobs/C00_s17/posebusters.csv").write_text("test_check\nFalse\n")
        self.hash_batch(batch)
        with self.assertRaisesRegex(ValueError, "raw CSV"):
            c.validate_batch(batch, self.run, state)




    def test_finalize_requires_lock_and_reserves_only_missing(self):
        with self.assertRaisesRegex(ValueError, "locked"):
            c.finalize(argparse.Namespace(run=self.run))
        self.state["current_shortlist"] = self.response()["shortlist"]
        self.state["results"]["C00"] = {"phase": "online"}
        c.lock_shortlist(self.run, self.state, "test")
        c.dump(self.run / "state.json", self.state)
        with contextlib.redirect_stdout(io.StringIO()):
            c.finalize(argparse.Namespace(run=self.run))
        state = c.read(self.run / "state.json")
        self.assertEqual(state["terminal_reserved_ids"], ["C01", "C02", "C03", "C04"])
        request = state["requests"][state["pending_request"]]["request"]
        self.assertEqual(request["locked_shortlist_sha256"], state["locked_shortlist_sha256"])
        self.assertEqual(request["input_state_version"], 0)

    def test_raw_cpu_time_rejects_negative_boolean_and_nonfinite_values(self):
        state = c.apply_response(self.run, self.state, self.response())
        batch = self.batch(state)
        summary = c.read(batch / "batch_summary.json")
        original = copy.deepcopy(summary)
        for value in (-1.0, True, float("inf")):
            with self.subTest(value=value):
                summary = copy.deepcopy(original)
                for row in summary["jobs"]:
                    row["cpu_seconds"] = value
                    path = batch / "jobs" / f"{row['candidate_id']}_s{row['seed']}" / "result.json"
                    path.write_text(json.dumps(row))
                summary["worker_cpu_seconds"] = float(value) * len(summary["jobs"])
                (batch / "batch_summary.json").write_text(json.dumps(summary))
                self.hash_batch(batch)
                with self.assertRaisesRegex(ValueError, "CPU time"):
                    c.validate_batch(batch, self.run, state)

    def test_raw_pose_checks_must_cover_all_frozen_protocol_checks(self):
        self.protocol["posebusters_configs"] = {"dock": {"modules": [{"chosen_binary_test_output": ["test_check", "required_check"]}]}}
        c.dump(self.run / "frozen/protocol.json", self.protocol)
        self.state["frozen_hashes"]["protocol.json"] = c.sha(self.run / "frozen/protocol.json")
        state = c.apply_response(self.run, self.state, self.response())
        batch = self.batch(state)
        with self.assertRaisesRegex(ValueError, "protocol coverage"):
            c.validate_batch(batch, self.run, state)

    def test_completed_campaign_commands_preserve_archive(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run = demo.run(self.root / "completed", self.harness)
        before = {str(p.relative_to(run)): c.sha(p) for p in run.rglob("*") if p.is_file()}
        with contextlib.redirect_stdout(io.StringIO()):
            c.turn(argparse.Namespace(run=run))
            c.finalize(argparse.Namespace(run=run))
            c.ingest(argparse.Namespace(run=run, batch=run / "batches/online_001"))
        after = {str(p.relative_to(run)): c.sha(p) for p in run.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def replay_run(self, name="replay", phase="development", harness=None):
        path = self.root / name
        with contextlib.redirect_stdout(io.StringIO()):
            c.init(argparse.Namespace(data=self.data, protocol=self.root / "protocol.json", out=path,
                                      model="never_called_test", effort="ultra", harness=harness or self.harness,
                                      phase=phase, execution_mode="replay"))
        return path

    def test_harness_identity_independent_of_phase_and_data(self):
        other = self.replay_run(phase="validation")
        state = c.read(other / "state.json")
        self.assertEqual(self.state["harness_hash"], state["harness_hash"])
        self.assertEqual(self.state["dataset_hash"], state["dataset_hash"])
        self.assertEqual(self.state["runtime_hash"], state["runtime_hash"])
        altered = self.root / "harness_changed"
        import shutil
        shutil.copytree(self.harness, altered)
        (altered / "lab_planner.md").write_text("A different planning strategy, same protected evaluator.")
        third = self.replay_run("changed", harness=altered)
        self.assertNotEqual(state["harness_hash"], c.read(third / "state.json")["harness_hash"])

    def test_frozen_campaign_and_implementation_are_checked(self):
        changed = copy.deepcopy(self.state)
        changed["execution_mode"] = "replay"
        with self.assertRaisesRegex(ValueError, "Campaign identity"):
            c.assert_frozen(self.run, changed)
        changed = copy.deepcopy(self.state)
        changed["protected_hashes"]["lab_harness/controller.py"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Protected implementation"):
            c.assert_frozen(self.run, changed)
        (self.run / "frozen/harness/lab_planner.md").chmod(0o600)
        (self.run / "frozen/harness/lab_planner.md").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "Frozen input"):
            c.assert_frozen(self.run, self.state)

    def test_live_rejects_replay_injection_before_reserving_call(self):
        response = self.root / "response.json"
        c.dump(response, self.response())
        with self.assertRaisesRegex(ValueError, "only in replay"):
            c.turn(argparse.Namespace(run=self.run, response=response))
        self.assertEqual(c.read(self.run / "state.json")["llm_calls"], [])

    def test_replay_never_calls_transport_and_charges_invalid_decision(self):
        run = self.replay_run()
        response = self.root / "response.json"
        invalid = self.response()
        invalid["input_state_version"] = 999
        c.dump(response, invalid)
        with mock.patch.object(c.transport, "invoke", side_effect=AssertionError("No model allowed")), contextlib.redirect_stdout(io.StringIO()):
            c.turn(argparse.Namespace(run=run, response=response))
            c.dump(response, self.response())
            c.turn(argparse.Namespace(run=run, response=response))
        state = c.read(run / "state.json")
        self.assertEqual([v["status"] for v in state["llm_calls"]], ["rejected", "accepted"])
        self.assertEqual(state["usage_tokens"], 0)
        self.assertEqual(state["status"], "awaiting_docking")
        self.assertTrue(all(not v["model_called"] for v in state["llm_calls"]))

    def test_live_transport_result_accounting_and_missing_usage_halts(self):
        result = {"response": self.response(), "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20},
                  "usage_complete": True, "error": None, "wall_seconds": 0.2, "returncode": 0, "backend": "codex"}
        with mock.patch.object(c.transport, "invoke", return_value=result), contextlib.redirect_stdout(io.StringIO()):
            c.turn(argparse.Namespace(run=self.run))
        state = c.read(self.run / "state.json")
        self.assertEqual(state["usage_tokens"], 120)
        self.assertEqual(state["llm_calls"][0]["status"], "accepted")
        state["pending_request"] = None
        state["status"] = "ready"
        c.dump(self.run / "state.json", state)
        result.update(usage=None, usage_complete=False, error="timeout")
        with mock.patch.object(c.transport, "invoke", return_value=result), contextlib.redirect_stdout(io.StringIO()):
            c.turn(argparse.Namespace(run=self.run))
        state = c.read(self.run / "state.json")
        self.assertEqual(state["status"], "needs_audit")
        self.assertFalse(state["llm_usage_complete"])
        self.assertEqual(len(state["llm_calls"]), 2)

    def test_synthetic_batch_rejected_by_live_campaign(self):
        state = c.apply_response(self.run, self.state, self.response())
        batch = self.batch(state)
        summary = c.read(batch / "batch_summary.json")
        summary["synthetic"] = True
        c.dump(batch / "batch_summary.json", summary)
        self.hash_batch(batch)
        with self.assertRaisesRegex(ValueError, "Synthetic batch"):
            c.validate_batch(batch, self.run, state)

    def test_cross_campaign_batch_is_not_a_cache(self):
        state0 = c.apply_response(self.run, self.state, self.response())
        run1 = self.replay_run("another")
        state1 = c.apply_response(run1, c.read(run1 / "state.json"), self.response())
        batch = self.batch(state0)
        with self.assertRaisesRegex(ValueError, "reservation"):
            c.validate_batch(batch, run1, state1)

    def test_terminal_reservation_rejects_candidates_outside_lock(self):
        self.state["current_shortlist"] = self.response()["shortlist"]
        self.state["results"]["C00"] = {"phase": "online"}
        self.state["online_reserved_ids"] = ["C00"]
        c.lock_shortlist(self.run, self.state, "synthetic test")
        for ids in (["C00"], ["C19"], ["C01"] * 2):
            with self.assertRaisesRegex(ValueError, "Terminal reservation"):
                c.reserve(self.run, self.state, ids, "bad_request", "terminal")

    def test_pilot_affinity_requires_two_valid_seeds(self):
        from lab_harness.docking import SCORE_FIELD, evaluator_descriptor
        jobs = [{"candidate_id": "C00", "seed": seed, "status": "completed", "pose_valid": True,
                 "scores": {"vina_affinity": energy}}
                for seed, energy in ((43, -8.0), (17, -6.0))]
        record = c.aggregate_jobs("C00", jobs)
        self.assertEqual(record[SCORE_FIELD], -7.0)
        self.assertEqual(c.qualified_docking_energies([record]), [-7.0])
        jobs[0]["pose_valid"] = False
        unqualified = c.aggregate_jobs("C00", jobs)
        self.assertIsNone(unqualified[SCORE_FIELD])
        self.assertEqual(unqualified["raw_median_vina_affinity"], -7.0)
        self.assertEqual(c.qualified_docking_energies([unqualified]), [])
        payload = c.task_input(self.state, self.protocol)
        self.assertEqual(c.read(self.run / "frozen/evaluator.json")["docking_evaluator"], evaluator_descriptor())
        self.assertEqual(payload["protocol_ref"]["docking_evaluator"], evaluator_descriptor())
        self.assertEqual(payload["tool_catalog"][0]["docking_evaluator"], evaluator_descriptor())

    def test_end_to_end_synthetic_demo_is_clearly_marked(self):
        with mock.patch.object(c.transport, "invoke", side_effect=AssertionError("No model")), contextlib.redirect_stdout(io.StringIO()):
            result = demo.run(self.root / "demo", self.harness)
        report = c.read(result / "evaluation.json")
        self.assertTrue(report["synthetic"])
        self.assertFalse(report["lab_agent_ran"])
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["shortlist_size"], 5)
        self.assertEqual(report["output_scope"], "pilot_shortlist")
        self.assertEqual(report["formal_min_final_compounds"], 100)
        self.assertFalse(report["formal_final_list_requirement_satisfied"])
        self.assertEqual(report["observed_input_plus_output_tokens"], 0)
        self.assertEqual(report["online_unique_candidates"], 2)
        self.assertEqual(report["terminal_unique_candidates"], 3)
        self.assertEqual(report["mean_qualified_docking_energy_kcal_mol"], -7.0)
        self.assertEqual(report["qualified_docking_energy_count"], 5)
        self.assertEqual(report["docking_evaluator"], report["continuous_results"][0]["docking_evaluator"])

class PresentationTests(unittest.TestCase):
    def test_all_modes_preserve_order_types_unknowns_and_reserved_keys(self):
        repeated = {"evidence": ["raw source", "中文", None], "measurement": 42.0}
        original = {"z": [None, True, False, 1, 1.0, -0.0, "未知"],
                    "a": [{"a": 1, "b": 2}, {"b": 2, "a": 1}],
                    "$ref": 0, "$literal": [["x", 3]],
                    "repeated": [repeated, repeated], "empty": [{}, [], ""],
                    "unknown_computation": {"scores": None, "pose_valid": None}}
        before = view.compact(original)
        for mode in sorted(view.MODES):
            with self.subTest(mode=mode):
                text = view.render(original, mode)
                self.assertEqual(view.compact(view.decode(text, mode)), before)
                self.assertTrue(view.audit_render(original, text, mode)["ordered_roundtrip_equal"])
                self.assertEqual(view.compact(original), before)
        encoded = json.loads(view.render(original, "lossless_deduplicated_json_v1"))
        self.assertTrue(encoded["dictionary"])
        self.assertIn("$literal", encoded["root"])

    def test_object_order_is_never_collapsed_by_deduplication(self):
        a = {"first": "x" * 40, "second": "y" * 40}
        b = {"second": "y" * 40, "first": "x" * 40}
        original = [a, b, a, b]
        restored = view.decode(view.render(original, "lossless_deduplicated_json_v1"),
                               "lossless_deduplicated_json_v1")
        self.assertEqual([list(item) for item in restored], [list(item) for item in original])
        self.assertEqual(view.compact(restored), view.compact(original))
        restored[0]["first"] = "independent"
        self.assertEqual(restored[2]["first"], "x" * 40)

    def test_invalid_json_values_and_unknown_configs_fail_closed(self):
        cyclic = []
        cyclic.append(cyclic)
        for original in [float("nan"), float("inf"), (1, 2), {1: "bad"}, cyclic]:
            with self.subTest(value_type=type(original).__name__):
                with self.assertRaises(ValueError):
                    view.render(original, "compact_json_v1")
        with self.assertRaises(ValueError):
            view.validate_config({"mode": "summary"})
        with self.assertRaises(ValueError):
            view.validate_config({"wrong": "compact_json_v1"})
        with self.assertRaises(ValueError):
            view.validate_config({"mode": "compact_json_v1", "drop_fields": []})

    def test_malformed_references_cycles_and_duplicate_keys_rejected(self):
        mode = "lossless_deduplicated_json_v1"
        original = {"very_long_value": [None, "x" * 50]}
        payload = json.loads(view.render([original, original], mode))
        dangling = copy.deepcopy(payload)
        dangling["root"] = {"$ref": 99999}
        cyclic = copy.deepcopy(payload)
        cyclic["root"] = {"$ref": 0}
        cyclic["dictionary"][0] = {"$ref": 0}
        boolean_index = copy.deepcopy(payload)
        boolean_index["root"] = {"$ref": True}
        duplicate_literal = copy.deepcopy(payload)
        duplicate_literal["root"] = {"$literal": [["a", 1], ["a", 2]]}
        for bad in [dangling, cyclic, boolean_index, duplicate_literal]:
            with self.assertRaises(ValueError):
                view.decode(json.dumps(bad), mode)
        with self.assertRaises(ValueError):
            view.decode('{"a":1,"a":2}', "compact_json_v1")
        with self.assertRaises(ValueError):
            view.audit_render({"a": 1, "b": 2}, '{"b":2,"a":1}', "compact_json_v1")



if __name__ == "__main__":
    unittest.main()
