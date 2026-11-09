"""Software-only archival fixtures. No models, docking, or real scores are used."""
from pathlib import Path
from tempfile import TemporaryDirectory
import shutil
import unittest

from lab_harness import controller as c
from lab_harness.docking import BATCH_SCHEMA, ENGINE, ENGINE_VERSION, POSE_SELECTION
from lab_harness.final_selection import select_final_compounds


# The small example records intentionally resemble a worker export, including
# independent native PDBQT, reconstructed SDF and PoseBusters CSV files. They exist only in each test's temp dir.
CASES = (
    ("tie", "C", (-8.0, -6.0)),
    ("above", "CC", (-6.6, -6.4)),
    ("invalid", "CCC", (-10.0, -10.0)),
    ("below", "CCCC", (-9.0, -9.0)),
)
RECEPTOR_FILES = ("receptor.pdbqt", "receptor_effective.pdb", "reference.sdf", "receptor_manifest.json")


def file_hashes(directory):
    return {str(p.relative_to(directory)): c.sha(p) for p in directory.rglob("*")
            if p.is_file() and p.name != "artifact_hashes.json"}


def alkane_sdf(smiles, scores):
    count = len(smiles)
    lines = [smiles, "  test fixture", "", f"{count:3d}{count-1:3d}  0  0  0  0  0  0  0  0999 V2000"]
    for i in range(count):
        lines.append(f"{i * 1.4:10.4f}    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0")
    for i in range(1, count):
        lines.append(f"{i:3d}{i+1:3d}  1  0  0  0  0")
    lines.append("M  END")
    for key, value in scores.items():
        lines.extend([f">  <{key}>  (1)", str(value), ""])
    return "\n".join([*lines, "$$$$", ""])


def pdbqt_models(smiles, energy):
    atoms = "\n".join(f"ATOM  {i+1:5d} C    LIG A   1      {i*1.4:6.3f}   0.000   0.000  1.00  0.00     0.000 C" for i in range(len(smiles)))
    ligand = f"REMARK SMILES {smiles}\nROOT\n{atoms}\nENDROOT\nTORSDOF 0\n"
    poses = "".join(f"MODEL {rank}\nREMARK VINA RESULT: {value:.3f} 0.000 0.000\n{ligand}ENDMDL\n"
                    for rank, value in [(1, energy), (2, energy + 0.75)])
    return ligand, poses


def write_job(folder, candidate, protocol, seed, energy):
    folder.mkdir(parents=True)
    cid, smiles = candidate["candidate_id"], candidate["smiles"]
    spec = {"candidate": candidate, "kind": "candidate", "seed": seed,
            "protocol": protocol, "data": "/worker/data", "vina": "/worker/vina"}
    c.dump(folder / "spec.json", spec)
    ligand, poses = pdbqt_models(smiles, energy)
    (folder / "ligand.pdbqt").write_text(ligand)
    (folder / "poses.pdbqt").write_text(poses)
    (folder / "ligand.sdf").write_text(alkane_sdf(smiles, {}))
    (folder / "top.sdf").write_text(alkane_sdf(smiles, {"vina_affinity": energy, "vina_model": 1, "canonical_isomeric_smiles": smiles}))
    valid = not (cid == "invalid" and seed == 43)
    checks = {"mol_pred_loaded": True, "sanitization": valid}
    (folder / "posebusters.csv").write_text(f"mol_pred_loaded,sanitization\nTrue,{valid}\n")
    (folder / "vina.stdout.txt").write_text("Unit-test output only.\n")
    (folder / "vina.stderr.txt").write_text("")
    command = [spec["vina"], "--receptor", "/worker/data/receptor/receptor.pdbqt", "--ligand", str(folder / "ligand.pdbqt"),
               "--out", str(folder / "poses.pdbqt"), "--seed", str(seed), "--exhaustiveness", "4", "--num_modes", "3",
               "--cpu", "2", "--scoring", "vina"]
    for key in ("center", "size"):
        for axis, value in zip("xyz", protocol[key]):
            command.extend([f"--{key}_{axis}", str(value)])
    result = {"kind": "candidate", "candidate_id": cid, "seed": seed, "status": "completed", "returncode": 0,
              "pose_count": 2, "pose_valid": valid, "pose_checks": checks, "identity_preserved": True,
              "preparation": {"canonical_smiles": smiles, "mmff_converged": True}, "scores": {"vina_affinity": energy},
              "engine": ENGINE, "vina_version": protocol["vina_version"], "vina_sha256": protocol["vina_sha256"],
              "selected_model": 1, "selected_canonical_smiles": smiles, "cpu_seconds": 1.0, "wall_seconds": 1.0,
              "command": command, "artifacts": {p.name: c.sha(p) for p in folder.iterdir() if p.is_file()}}
    c.dump(folder / "result.json", result)
    return result


def write_archive(root, *, receptor_digest="a" * 64, execution_mode="live", cases=CASES, gnina=False, chemical=False):
    frozen = root / "frozen"
    frozen.mkdir(parents=True)
    for script in ("vina_worker.py", "run_vina_batch.py", "vina_contract.py"):
        shutil.copyfile(c.ROOT / "scripts" / script, frozen / script)
    candidates = [{"candidate_id": cid, "smiles": smiles, "ic50_nM": 100.0, "pIC50": 7.0,
                   "assay_id": "TEST_ASSAY", "activity_ids": [], "document_ids": []} for cid, smiles, _ in cases]
    if chemical:
        from tests.chemical_fixtures import annotate
        registry = annotate(candidates)
        c.dump(frozen / 'chemical_states.json', registry)
    manifest = {"is_mock": False, "target_chembl_id": "CHEMBL301", "candidates": [
        {"candidate_id": row["candidate_id"], "canonical_smiles": row["smiles"]} for row in candidates]}
    if chemical:
        manifest.update(candidates=candidates, chemical_preparation={'method': registry['method']})
    c.dump(frozen / "manifest.json", manifest)
    c.dump(frozen / "candidates.json", {"is_mock": False, "target_id": "CHEMBL301", "candidates": candidates})
    input_hashes = {name: c.sha(frozen / name) for name in ("manifest.json", "candidates.json")}
    if chemical:
        input_hashes['chemical_states.json'] = c.sha(frozen / 'chemical_states.json')
    input_hashes.update({f"receptor/{name}": receptor_digest for name in RECEPTOR_FILES})
    protocol = {"protocol_id": "unit-cdk2", "seeds": [17, 43], "engine": ENGINE, "scoring": "vina",
                "pose_selection": POSE_SELECTION, "preparation": "ETKDGv3 with MMFF94s", "tau": None, "m_min": None,
                "center": [-8.773272727272728, 50.057272727272725, 12.795045454545454], "size": [22.0] * 3,
                "cpu": 2, "exhaustiveness": 4, "num_modes": 3,
                "dock_timeout_seconds": 180, "worker_timeout_seconds": 300, "wall_limit_seconds": 1800, "retries": 0,
                "runner_sha256": c.sha(frozen / "vina_worker.py"), "adapter_sha256": c.sha(frozen / "run_vina_batch.py"),
                "contract_sha256": c.sha(frozen / "vina_contract.py"), "vina_sha256": "c" * 64,
                "vina_version": {"stdout": f"{ENGINE} v{ENGINE_VERSION}", "returncode": 0},
                "versions": {"rdkit": "2025.9.6", "meeko": "0.7.1", "posebusters": "0.6.5"}, "input_sha256": input_hashes,
                "posebusters_configs": {"dock": {"modules": [{"chosen_binary_test_output": ["mol_pred_loaded", "sanitization"]}]}}}
    if gnina:
        protocol.update(engine="GNINA", gnina_version={"returncode": 0, "stdout": "gnina v1.3.3"}, cnn_scoring="rescore")
    if chemical:
        protocol['chemical_preparation'] = {'method': registry['method']}
    c.dump(frozen / "protocol.json", protocol)
    c.dump(frozen / "campaign.json", {"campaign_id": root.name, "execution_mode": execution_mode,
                                      "protocol_hash": c.sha(frozen / "protocol.json")})
    rid = "online_001"
    batch = root / "batches" / rid
    batch.mkdir(parents=True)
    request = {"schema_version": BATCH_SCHEMA, "controller_authorized": True, "campaign_id": root.name,
               "candidate_ids": [row["candidate_id"] for row in candidates], "phase": "online", "request_id": rid,
               "input_state_version": 0, "protocol_sha256": c.sha(frozen / "protocol.json"),
               "candidates_sha256": input_hashes["candidates.json"], "manifest_sha256": input_hashes["manifest.json"]}
    c.dump(batch / "request.json", request)
    shutil.copyfile(frozen / "protocol.json", batch / "protocol.json")
    jobs = [write_job(batch / "jobs" / f"{candidate['candidate_id']}_s{seed}", candidate, protocol, seed, energy)
            for candidate, (_, _, energies) in zip(candidates, cases) for seed, energy in zip((17, 43), energies)]
    engine = {key: protocol[key] for key in ["engine", "vina_version", "vina_sha256"]}
    c.dump(batch / "engine.json", engine)
    summary = {**request, **engine, "status": "completed", "request_sha256": c.sha(batch / "request.json"),
               **{key: protocol[key] for key in ["runner_sha256", "adapter_sha256", "contract_sha256"]},
               "jobs": jobs, "state_version": 1,
               "candidates": [c.aggregate_jobs(row["candidate_id"], jobs) for row in candidates],
               "worker_cpu_seconds": float(len(jobs)), "cpu_accounting_complete": True, "wall_seconds": 8.0}
    c.dump(batch / "batch_summary.json", summary)
    c.dump(batch / "artifact_hashes.json", file_hashes(batch))
    state = {"campaign_id": root.name, "construct_id": "1DI8_chain_A", "execution_mode": execution_mode, "candidates": candidates,
             "status": "completed", "pending_request": None, "frozen_hashes": file_hashes(frozen), "protocol_id": protocol["protocol_id"],
             "requests": {rid: {"request": request, "canonical_hash": c.canonical_hash(request), "status": "ingested",
                                "ingested_artifacts_sha256": c.sha(batch / "artifact_hashes.json")}}}
    c.dump(root / "state.json", state)
    c.dump(root / "evaluation.json", {"campaign_id": root.name, "status": "completed", "execution_mode": execution_mode, "synthetic": False})
    c.dump(root / "artifact_hashes.json", file_hashes(root))
    return root


def select(*runs, threshold=-7.0):
    return select_final_compounds({"schema_version": "lab_docking_selection_input_v1", "threshold": threshold,
                                   "runs": [str(run) for run in runs]})


class DockingArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = write_archive(self.root / "dock_fixture")

    def test_inclusive_dual_seed_median_energy_excludes_invalid_pose(self):
        report = select(self.run)
        self.assertEqual(report["count"], 2)
        self.assertEqual(report["shortfall"], 98)
        self.assertEqual(report["score_name"], "qualified_median_vina_affinity")
        self.assertEqual(report["archives"][0]["engine"], ENGINE)
        self.assertEqual(report["missing_count"], 1)
        self.assertFalse(report["feasible"])
        self.assertEqual(report["status"], "insufficient_compounds")
        self.assertEqual([(r["compound_id"], r["score"]) for r in report["compounds"]], [("below", -9.0), ("tie", -7.0)])
        self.assertEqual(select(self.run, threshold=-8.5)["count"], 1)

    def test_preparation_archive_retains_parent_and_prepared_identity(self):
        run = write_archive(self.root / 'chemical_fixture', chemical=True)
        result = select(run)
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['compounds'][0]['canonical_parent_identity'], 'CCCC')
        self.assertTrue(result['compounds'][0]['prepared_state_id'].startswith('state_'))
        self.assertEqual(result['archives'][0]['chemical_preparation']['method']['engine'], 'MolScrub')

    def modify_job_artifact(self, filename, before, after):
        # Keep all artifact hashes consistent. Rejection must come from the
        # independent checks of energy, rank and chemical identity.
        rid = "online_001"
        batch = self.run / "batches" / rid
        job = batch / "jobs/below_s17"
        file = job / filename
        original = file.read_text()
        self.assertIn(before, original)
        file.write_text(original.replace(before, after))
        result = c.read(job / "result.json")
        result["artifacts"][filename] = c.sha(file)
        c.dump(job / "result.json", result)
        summary = c.read(batch / "batch_summary.json")
        index = next(i for i, row in enumerate(summary["jobs"]) if row["candidate_id"] == "below" and row["seed"] == 17)
        summary["jobs"][index] = result
        c.dump(batch / "batch_summary.json", summary)
        c.dump(batch / "artifact_hashes.json", file_hashes(batch))
        state = c.read(self.run / "state.json")
        state["requests"][rid]["ingested_artifacts_sha256"] = c.sha(batch / "artifact_hashes.json")
        c.dump(self.run / "state.json", state)
        c.dump(self.run / "artifact_hashes.json", file_hashes(self.run))

    def test_pose_energy_change_rejected_even_after_all_hashes_updated(self):
        self.modify_job_artifact("poses.pdbqt", "REMARK VINA RESULT: -9.000", "REMARK VINA RESULT: -99.000")
        with self.assertRaisesRegex(ValueError, "(?i)pose|score|affinity"):
            select(self.run)

    def test_reconstructed_pose_identity_must_match_worker_receipt(self):
        self.modify_job_artifact("top.sdf", "<canonical_isomeric_smiles>  (1)\nCCCC", "<canonical_isomeric_smiles>  (1)\nC")
        with self.assertRaisesRegex(ValueError, "(?i)identity"):
            select(self.run)

    def test_gnina_receipts_rejected_even_with_vina_named_scores(self):
        legacy = write_archive(self.root / "gnina_fixture", gnina=True)
        with self.assertRaisesRegex(ValueError, "(?i)standalone|AutoDock Vina|GNINA"):
            select(legacy)

    def test_live_selection_refuses_replay_archives(self):
        replay = write_archive(self.root / "replay_fixture", execution_mode="replay")
        with self.assertRaisesRegex(ValueError, "(?i)live|replay|synthetic"):
            select(replay)

    def test_incompatible_receptor_profiles_cannot_be_pooled(self):
        other = write_archive(self.root / "changed_receptor", receptor_digest="b" * 64)
        with self.assertRaisesRegex(ValueError, "(?i)profile|receptor|incompatible|protocol"):
            select(self.run, other)


if __name__ == "__main__":
    unittest.main()
