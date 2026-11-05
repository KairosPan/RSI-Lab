"""Command-line entrypoint for the Lab and its bounded Meta workflow."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys

from . import controller as c


def parser():
    p = argparse.ArgumentParser(description="RSI-Lab v1: auditable Lab campaigns and bounded Meta revisions")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Freeze a new campaign; makes no model call")
    for name in ("data", "protocol", "out"):
        init.add_argument("--" + name, required=True)
    init.add_argument("--harness", default=str(c.ROOT / "harness/h0"))
    init.add_argument("--phase", choices=("development", "validation", "final"), default="development")
    init.add_argument("--execution-mode", choices=("live", "replay"), default="live")
    init.add_argument("--model", default="gpt-6-astra")
    init.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"), default="medium")
    for action in ("step", "ingest", "finalize", "status"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--run", required=True)
        if action == "step":
            cmd.add_argument("--response", help="Scripted JSON decision; allowed only in replay campaigns")
        if action == "ingest":
            cmd.add_argument("--batch", required=True)
    d = sub.add_parser("demo", help="Synthetic full campaign; no model, network, or GPU")
    d.add_argument("--out", required=True)
    d.add_argument("--harness", default=str(c.ROOT / "harness/h0"))
    d.add_argument("--with-meta", action="store_true", help="Also exercise proposal + two paired validations; never releases")
    selection = sub.add_parser("select-final", help="Evaluate archived standalone AutoDock Vina affinities; require at least 100 distinct compounds")
    selection.add_argument("--input", required=True, help="JSON manifest containing a docking energy threshold and completed live run directories")
    selection.add_argument("--out", required=True, help="New result/diagnostic JSON file; never overwritten")
    m = sub.add_parser("meta", help="Prepare evidence, propose a version, or evaluate a frozen paired gate")
    ms = m.add_subparsers(dest="meta_command", required=True)
    prepare = ms.add_parser("prepare")
    prepare.add_argument("--development-run", required=True)
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--min-pairs", type=int, default=2)
    propose = ms.add_parser("propose")
    propose.add_argument("--workspace", required=True)
    propose.add_argument("--response", help="Scripted proposal; allowed only in replay workspaces")
    plan = ms.add_parser("plan", help="Register every validation pool and pair before execution")
    plan.add_argument("--workspace", required=True)
    plan.add_argument("--pair", nargs=4, action="append", required=True, metavar=("DATA", "PROTOCOL", "BASELINE", "CANDIDATE"))
    gate = ms.add_parser("gate")
    gate.add_argument("--workspace", required=True)
    gate.add_argument("--pair", nargs=2, action="append", metavar=("BASELINE", "CANDIDATE"))
    gate.add_argument("--out", required=True)
    return p


def demo_command(args):
    from . import demo, meta
    if not args.with_meta:
        with contextlib.redirect_stdout(io.StringIO()):
            run = demo.run(args.out, harness=args.harness)
        return {"status": "completed", "synthetic": True, "model_called": False, "gpu_executed": False,
                "run": str(run), "evaluation": str(run / "evaluation.json")}
    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=False)
    with contextlib.redirect_stdout(io.StringIO()):
        development = demo.run(root / "development", harness=args.harness)
        meta.prepare(argparse.Namespace(development_run=development, out=root / "meta", min_pairs=2))
        workspace = root / "meta"
        packet = c.read(workspace / "input.json")
        # Deliberately scripted proposal: the demo checks mechanics, not whether
        # a model diagnosed or improved a real scientific run.
        proposal = {"parent_harness_hash": packet["parent_harness_hash"],
                    "input_packet_sha256": c.sha(workspace / "input.json"), "decision": "propose_change",
                    "diagnosis": "Synthetic exercise of lossless context compaction.",
                    "evidence_refs": ["evaluation.json"],
                    "hypothesis": "Compact JSON preserves input content; actual token benefit needs real calls.",
                    "replacement_lab_planner": None, "input_view_mode": "compact_json_v1",
                    "regression_risks": ["Reduced readability may hurt decisions; this replay cannot measure it."]}
        c.dump(root / "scripted_proposal.json", proposal)
        meta.propose(argparse.Namespace(workspace=workspace, response=root / "scripted_proposal.json"))
        planned = []
        for index in (1, 2):
            data = demo.make_data(root / f"validation_data_{index}", start_index=index * 100)
            planned.append((data, data / "protocol.json", root / f"baseline_{index}", root / f"candidate_{index}"))
        meta.plan(argparse.Namespace(workspace=workspace, pair=planned))
        pairs = []
        for index in (1, 2):
            baseline = demo.run(root / f"baseline_{index}", harness=workspace / "parent", phase="validation", start_index=index * 100)
            candidate = demo.run(root / f"candidate_{index}", harness=workspace / "candidate", phase="validation", start_index=index * 100)
            pairs.append((baseline, candidate))
        result = meta.gate(argparse.Namespace(workspace=workspace, pair=pairs, out=root / "gate.json"))
    return {"status": "completed", "synthetic": True, "model_called": False, "gpu_executed": False,
            "run": str(development), "meta_workspace": str(workspace), "gate": str(root / "gate.json"),
            "gate_decision": result["decision"], "eligible_for_next_development_run": False}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"init", "ingest", "finalize"}:
            getattr(c, args.command)(args)
            return 0
        if args.command == "step":
            c.turn(args)
            return 0
        if args.command == "select-final":
            from . import final_selection
            source = Path(args.input).resolve()
            raw = source.read_bytes()
            result = final_selection.select_final_compounds(json.loads(raw), base_dir=source.parent)
            import hashlib
            result["input_sha256"] = hashlib.sha256(raw).hexdigest()
            modules = ("controller.py", "docking.py", "docking_archive.py", "final_selection.py")
            result["validator_sources"] = {name: c.sha(c.ROOT / "lab_harness" / name) for name in modules}
            result["validator_sources"]["scripts/vina_contract.py"] = c.sha(c.ROOT / "scripts/vina_contract.py")
            result["validator_sha256"] = c.canonical_hash(result["validator_sources"])
            output = Path(args.out).resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x") as stream:
                stream.write(c.encoded(result).decode())
            print(json.dumps({key: value for key, value in result.items() if key != "compounds"},
                             indent=2, ensure_ascii=False, allow_nan=False))
            return 0 if result["feasible"] else 2
        if args.command == "status":
            run = Path(args.run).resolve()
            with c.locked(run):
                state = c.read(run / "state.json")
                c.assert_frozen(run, state)
                result = {key: state[key] for key in ("campaign_id", "phase", "execution_mode", "status", "state_version", "harness_hash", "pending_request", "usage_tokens", "llm_usage_complete")}
                result.update(decision_attempts=len(state["llm_calls"]), shortlist=state["current_shortlist"],
                              last_rejection=state["last_rejection"])
                if state["pending_request"]:
                    result["request"] = str(run / "pending" / state["pending_request"] / "request.json")
        elif args.command == "demo":
            result = demo_command(args)
        elif args.command == "meta":
            from . import meta
            result = getattr(meta, args.meta_command)(args)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
