"""Offline orchestration tests. Mock receipts never enter a real campaign."""
import copy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from lab_harness import controller as c, meta_cycle as cycle


def write_meta_receipt(root, *, decision="propose_change", complete=True):
    proposal = {"decision": decision, "test_fixture": True}
    usage = {"input_tokens": 100, "output_tokens": 20}
    receipt = {"backend": "codex", "returncode": 0, "error": None, "response": proposal,
               "usage": usage, "usage_complete": complete}
    c.dump(root / "proposal.json", proposal)
    c.dump(root / "call_receipt.json", receipt)
    c.dump(root / "call/transport_result.json", receipt)
    c.dump(root / "call/response.json", proposal)
    c.dump(root / "call/usage.json", {"usage": usage, "usage_complete": complete})
    c.dump(root / "call/invocation.json", {"backend": "codex", "returncode": 0, "dispatched": True,
                                         "model": "test-model", "effort": "medium"})
    events = [{"type": "thread.started", "thread_id": "test-only"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": c.encoded(proposal).decode()}},
              {"type": "turn.completed", "usage": usage}]
    (root / "call/events.jsonl").write_text("\n".join(c.json.dumps(event) for event in events) + "\n")
    return receipt


class MetaCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.dev = self.root / "development"
        self.dev.mkdir()
        c.dump(self.dev / "artifact_hashes.json", {"test": "fixture"})
        self.workspace = self.root / "meta"
        self.gate = self.workspace / "gate.json"
        self.pairs = [(self.root / f"data_{i}", self.root / f"protocol_{i}.json",
                       self.root / f"baseline_{i}", self.root / f"candidate_{i}") for i in range(2)]
        self.dev_state = {"execution_mode": "live", "model": "test-model", "effort": "medium", "runtime_hash": "runtime"}
        self.evaluation = {"synthetic": False, "llm_usage_accounting_complete": True,
                           "observed_input_plus_output_tokens": 200}
        self.identity = {"execution_mode": "live", "model_attempts": 1, "model": "test-model", "effort": "medium",
                         "parent_harness_hash": "parent", "candidate_harness_hash": "candidate"}
        self.events, self.inits = [], []
        self.decision, self.complete_usage, self.gate_decision = "propose_change", True, "accepted"

        def completed(path, phase=None):
            return Path(path), dict(self.dev_state), dict(self.evaluation)

        def prepare(args):
            self.events.append("prepare")
            Path(args.out).mkdir()
            return {"status": "prepared"}

        def propose(args):
            self.assertIsNone(args.response)  # Production must not inject a scripted proposal.
            self.events.append("propose")
            write_meta_receipt(Path(args.workspace), decision=self.decision, complete=self.complete_usage)
            return {"status": "no_change" if self.decision == "no_change" else "candidate_frozen"}

        def workspace(path):
            return Path(path), dict(self.identity), {}

        def plan(args):
            self.events.append("plan")
            self.assertFalse(any(Path(path).exists() for pair in args.pair for path in pair[2:]))
            records = [{"data": str(data), "protocol": str(protocol), "baseline": str(baseline), "candidate": str(changed),
                        "dataset_hash": f"data-{i}", "protocol_hash": f"protocol-{i}"}
                       for i, (data, protocol, baseline, changed) in enumerate(args.pair)]
            c.dump(self.workspace / "validation_plan.json", {"pairs": records})
            self.identity["validation_plan_sha256"] = c.sha(self.workspace / "validation_plan.json")

        def init(args):
            self.assertIn("plan", self.events)
            self.events.append("init:" + Path(args.out).name)
            self.inits.append(args)
            Path(args.out).mkdir()
            c.dump(Path(args.out) / "artifact_hashes.json", {"test": "fixture"})
            index = int(Path(args.out).name.rsplit("_", 1)[1])
            return {"dataset_hash": f"data-{index}", "protocol_hash": f"protocol-{index}",
                    "harness_hash": Path(args.harness).name, "runtime_hash": "runtime"}

        def runner(run, **kwargs):
            self.events.append("run:" + Path(run).name)
            return {"status": "completed", "run": str(run)}

        def gate(args):
            self.events.append("gate")
            self.assertIsNone(args.pair)  # The real gate must load the entire registered plan.
            result = {"decision": self.gate_decision, "synthetic": False,
                      "eligible_for_next_development_run": self.gate_decision == "accepted"}
            c.dump(args.out, result)
            return result

        for target, attr, side_effect in ((cycle.meta, "completed_run", completed), (cycle.meta, "prepare", prepare),
                                          (cycle.meta, "propose", propose), (cycle.meta, "workspace", workspace),
                                          (cycle.meta, "plan", plan), (cycle.c, "init", init),
                                          (cycle, "_run_campaign", runner), (cycle.meta, "gate", gate)):
            patcher = mock.patch.object(target, attr, side_effect=side_effect)
            setattr(self, "mock_" + attr.lstrip("_"), patcher.start())
            self.addCleanup(patcher.stop)

    def execute(self, **overrides):
        options = {"development_run": self.dev, "workspace": self.workspace, "planned_pairs": self.pairs,
                   "gate_out": self.gate, "vina": "/test/vina", "worker_python": "/test/python"}
        return cycle.execute_cycle(**{**options, **overrides})

    def test_all_registered_arms_run_after_plan_then_selected_candidate_is_future_only(self):
        result = self.execute()
        self.assertEqual(self.events, ["prepare", "propose", "plan", "init:baseline_0", "run:baseline_0",
                                      "init:candidate_0", "run:candidate_0", "init:baseline_1", "run:baseline_1",
                                      "init:candidate_1", "run:candidate_1", "gate"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["eligible_for_next_development_run"])
        self.assertIsNone(result["global_wall_time_limit_seconds"])
        self.assertEqual(len(result["validation_runs"]), 4)
        pointer = c.read(self.workspace / "selected_policy.json")
        self.assertEqual(pointer["harness"], str(self.workspace / "candidate"))
        self.assertEqual(pointer["use"], "new_development_campaign_only")
        self.assertTrue(pointer["candidate_promoted"])
        self.assertEqual(pointer["gate_sha256"], c.sha(self.gate))
        self.assertEqual(c.read(self.workspace / "cycle.json"), result)
        for index, args in enumerate(self.inits):
            self.assertEqual((args.phase, args.execution_mode, args.model, args.effort),
                             ("validation", "live", "test-model", "medium"))
            self.assertEqual(args.harness, self.workspace / ("parent" if index % 2 == 0 else "candidate"))
        self.assertEqual(c.read(self.dev / "artifact_hashes.json"), {"test": "fixture"})

    def test_no_change_is_valid_without_validation_or_gate(self):
        self.decision = "no_change"
        result = self.execute()
        self.assertEqual(self.events, ["prepare", "propose"])
        self.assertEqual(result["status"], "no_change")
        self.assertFalse(result["eligible_for_next_development_run"])
        self.assertEqual(result["selected_policy"]["harness"], str(self.workspace / "parent"))
        self.assertFalse(result["selected_policy"]["candidate_promoted"])
        self.assertFalse(self.gate.exists())

    def test_stage_checkpoints_follow_actual_registration_initialization_and_gate(self):
        stages = []
        def checkpoint(stage, **context):
            stages.append(stage)
            if stage == 'before_validation':
                self.assertEqual(self.events[-1], 'plan')
                self.assertFalse(self.inits)
            elif stage == 'before_docking':
                self.assertTrue(self.events[-1].startswith('init:'))
            else:
                self.assertEqual(self.events[-1], 'gate')
                self.assertFalse((self.workspace / 'selected_policy.json').exists())
        self.assertEqual(self.execute(checkpoint=checkpoint)['status'], 'completed')
        self.assertEqual(stages, ['before_validation'] + ['before_docking'] * 4 + ['before_promotion'])

    def test_unresolved_validation_obligation_prevents_every_arm(self):
        def checkpoint(stage, **context):
            raise ValueError('Unresolved validation prerequisite')
        result = self.execute(checkpoint=checkpoint)
        self.assertEqual(result['status'], 'needs_audit')
        self.mock_init.assert_not_called()
        self.mock_gate.assert_not_called()

    def test_unresolved_promotion_obligation_prevents_policy_handoff(self):
        def checkpoint(stage, **context):
            if stage == 'before_promotion':
                raise ValueError('Unresolved promotion prerequisite')
        result = self.execute(checkpoint=checkpoint)
        self.assertEqual(result['status'], 'needs_audit')
        self.assertTrue(self.gate.exists())
        self.assertFalse((self.workspace / 'selected_policy.json').exists())

    def test_rejected_gate_retains_parent(self):
        self.gate_decision = "rejected"
        result = self.execute()
        self.assertEqual(result["decision"], "rejected")
        self.assertFalse(result["eligible_for_next_development_run"])
        self.assertEqual(result["selected_policy"]["harness_hash"], "parent")
        self.assertFalse(result["selected_policy"]["candidate_promoted"])

    def test_unknown_meta_usage_stops_before_validation_and_cannot_retry(self):
        self.complete_usage = False
        result = self.execute()
        self.assertEqual(result["status"], "needs_audit")
        self.assertIn("usage", result["error"])
        self.assertEqual(self.events, ["prepare", "propose"])
        self.assertFalse((self.workspace / "selected_policy.json").exists())
        with self.assertRaisesRegex(ValueError, "no implicit retry"):
            self.execute()
        self.assertEqual(self.mock_propose.call_count, 1)

    def test_incomplete_arm_cannot_be_skipped_or_promoted(self):
        self.mock_run_campaign.side_effect = lambda run, **kw: {"status": "needs_audit", "run": str(run)}
        result = self.execute()
        self.assertEqual(result["status"], "needs_audit")
        self.assertEqual(len(result["requested_pairs"]), 2)
        self.assertEqual(len(result["validation_runs"]), 1)
        self.assertEqual(result["validation_runs"][0]["status"], "needs_audit")
        self.mock_gate.assert_not_called()
        self.assertFalse((self.workspace / "selected_policy.json").exists())

    def test_plan_rejection_does_not_execute_any_arm(self):
        self.mock_plan.side_effect = ValueError("Validation pools overlap")
        result = self.execute()
        self.assertEqual(result["status"], "needs_audit")
        self.assertEqual(result["failure_stage"], "candidate_frozen")
        self.mock_init.assert_not_called()
        self.mock_gate.assert_not_called()

    def test_meta_receipt_drift_during_validation_cannot_promote(self):
        def runner(run, **kwargs):
            changed = c.read(self.workspace / "call_receipt.json")
            changed["usage"]["output_tokens"] += 1
            c.dump(self.workspace / "call_receipt.json", changed)
            return {"status": "completed", "run": str(run)}
        self.mock_run_campaign.side_effect = runner
        result = self.execute()
        self.assertEqual(result["status"], "needs_audit")
        self.assertIn("receipt changed", result["error"])
        self.mock_gate.assert_not_called()
        self.assertFalse((self.workspace / "selected_policy.json").exists())

    def test_runtime_or_dataset_drift_stops_before_live_runner(self):
        self.mock_init.return_value = {}
        self.mock_init.side_effect = lambda args: {"dataset_hash": "changed"}
        result = self.execute()
        self.assertEqual(result["status"], "needs_audit")
        self.assertIn("frozen plan/runtime", result["error"])
        self.mock_run_campaign.assert_not_called()
        self.mock_gate.assert_not_called()

    def test_replay_or_incomplete_development_cannot_start_meta(self):
        self.dev_state["execution_mode"] = "replay"
        with self.assertRaisesRegex(ValueError, "live development"):
            self.execute()
        self.dev_state["execution_mode"] = "live"
        self.evaluation["llm_usage_accounting_complete"] = False
        with self.assertRaisesRegex(ValueError, "usage is incomplete"):
            self.execute()
        self.mock_prepare.assert_not_called()

    def test_insufficient_or_overlapping_paths_rejected_before_meta(self):
        for overrides in ({"planned_pairs": self.pairs[:1]},
                          {"gate_out": self.dev / "new_gate.json"},
                          {"gate_out": self.workspace / "cycle.json"},
                          {"planned_pairs": [self.pairs[0], self.pairs[0]]}):
            with self.subTest(options=overrides), self.assertRaises(ValueError):
                self.execute(**overrides)
        self.mock_prepare.assert_not_called()

    def test_keyboard_interrupt_is_recorded_without_implicit_resume(self):
        self.mock_run_campaign.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        result = c.read(self.workspace / "cycle.json")
        self.assertEqual(result["status"], "needs_audit")
        self.assertIn("KeyboardInterrupt", result["error"])
        self.mock_gate.assert_not_called()


class ActualMetaUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.receipt = write_meta_receipt(self.root)
        identity = {"execution_mode": "live", "model_attempts": 1, "model": "test-model", "effort": "medium"}
        patcher = mock.patch.object(cycle.meta, "workspace", return_value=(self.root, identity, {}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_requires_reconciled_event_and_call_usage(self):
        self.assertEqual(cycle._actual_meta_usage(self.root), self.receipt["usage"])
        changed = copy.deepcopy(self.receipt)
        changed["usage"]["output_tokens"] += 1
        c.dump(self.root / "call_receipt.json", changed)
        with self.assertRaisesRegex(ValueError, "transport archive"):
            cycle._actual_meta_usage(self.root)

    def test_forged_receipts_without_matching_events_rejected(self):
        changed = copy.deepcopy(self.receipt)
        changed["usage"]["output_tokens"] += 1
        c.dump(self.root / "call_receipt.json", changed)
        c.dump(self.root / "call/transport_result.json", changed)
        c.dump(self.root / "call/usage.json", {"usage": changed["usage"], "usage_complete": True})
        with self.assertRaisesRegex(ValueError, "raw Codex events"):
            cycle._actual_meta_usage(self.root)

    def test_tool_event_cannot_masquerade_as_text_only_meta(self):
        events = self.root / "call/events.jsonl"
        with events.open("a") as stream:
            stream.write(c.json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}) + "\n")
        with self.assertRaisesRegex(ValueError, "raw Codex events"):
            cycle._actual_meta_usage(self.root)


if __name__ == "__main__":
    unittest.main()
