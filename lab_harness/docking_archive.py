"""Read native standalone AutoDock Vina output from completed Lab campaigns."""
from pathlib import Path
import re

from . import controller as c
from .docking import BATCH_SCHEMA, ENGINE, ENGINE_VERSION, SCORE_FIELD, parse_vina_poses, validate_vina_protocol

_REQUIRED_RECEPTOR = {
    "receptor/receptor.pdbqt", "receptor/receptor_effective.pdb",
    "receptor/reference.sdf", "receptor/receptor_manifest.json",
}
_POOL_METADATA = {"input_sha256", "protocol_id", "phase", "frozen_at", "parent_protocol_sha256",
                  "pool_relation_to_p0", "lab_scope", "transport_scope"}
_FROZEN_CODE = {"adapter_sha256": "run_vina_batch.py", "runner_sha256": "vina_worker.py",
                "contract_sha256": "vina_contract.py"}


def _profile(protocol, manifest, state, code_hashes):
    validate_vina_protocol(protocol, live=True)
    hashes = protocol["input_sha256"]
    if manifest.get("target_chembl_id") != "CHEMBL301" or state.get("construct_id") != "1DI8_chain_A":
        raise ValueError("Expected the frozen CDK2 target and 1DI8 chain A construct")
    packages = protocol.get("versions", {})
    if not all(isinstance(packages.get(name), str) and packages[name].strip() for name in ("meeko", "rdkit", "posebusters")):
        raise ValueError("Docking profile lacks required chemistry package versions")
    modules = protocol.get("posebusters_configs", {}).get("dock", {}).get("modules", [])
    if not any(module.get("chosen_binary_test_output") for module in modules):
        raise ValueError("Docking profile lacks frozen PoseBusters checks")
    if not _REQUIRED_RECEPTOR.issubset(hashes):
        raise ValueError("Docking profile lacks required frozen receptor inputs")
    receptor = {k: v for k, v in hashes.items() if k.startswith("receptor/")}
    if any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in receptor.values()):
        raise ValueError("Invalid frozen receptor hash")
    if any(code_hashes[key] != protocol[key] for key in _FROZEN_CODE):
        raise ValueError("Frozen Vina code differs from the protocol")
    settings = {k: v for k, v in protocol.items() if k not in _POOL_METADATA}
    # The executable may be installed at different absolute paths. The frozen
    # binary hash and verified --version output establish the engine identity.
    settings["vina_version"] = {"returncode": 0, "stdout": f"{ENGINE} v{ENGINE_VERSION}"}
    return c.canonical_hash({"protocol": settings, "target": manifest["target_chembl_id"],
                             "construct": state["construct_id"], "receptor": receptor, "code": code_hashes})


def _check_engine(record, protocol):
    version = record.get("vina_version", {})
    if (record.get("engine") != ENGINE or record.get("vina_sha256") != protocol["vina_sha256"]
            or version.get("returncode") != 0 or version.get("stdout", "").strip() != protocol["vina_version"]["stdout"].strip()):
        raise ValueError("Archived engine receipt differs from the frozen standalone Vina executable")


def _selected_pose_properties(path):
    records = [record for record in path.read_text().split("$$$$") if record.strip()]
    if len(records) != 1 or "M  END" not in records[0]:
        raise ValueError("Expected exactly one reconstructed Vina pose in top.sdf")
    properties = {}
    for key, value in re.findall(r"^>\s*<([^>\r\n]+)>[^\r\n]*\r?\n([^\r\n]+)", records[0], re.M):
        if key in properties:
            raise ValueError(f"Duplicate selected pose property {key}")
        properties[key] = value.strip()
    required = {"vina_model", "vina_affinity", "canonical_isomeric_smiles"}
    if not required.issubset(properties):
        raise ValueError("Reconstructed Vina pose lacks rank, energy, or molecular identity")
    return properties


def _check_command(command, spec, protocol, folder):
    if (not isinstance(command, list) or not command or command[0] != spec.get("vina")
            or len(command[1:]) % 2 or any(not isinstance(arg, str) for arg in command)):
        raise ValueError("Invalid standalone Vina command receipt")
    options = dict(zip(command[1::2], command[2::2]))
    if len(options) * 2 != len(command[1:]):
        raise ValueError("Duplicate Vina command-line option")
    settings = {key: str(protocol[key]) for key in ("scoring", "exhaustiveness", "num_modes", "cpu")}
    settings["seed"] = str(spec["seed"])
    for axis_name in ("center", "size"):
        for axis, value in zip("xyz", protocol[axis_name]):
            settings[f"{axis_name}_{axis}"] = str(value)
    if any(options.get("--" + key) != value for key, value in settings.items()):
        raise ValueError("Vina command differs from frozen scoring, seed, resource or search settings")
    if set(options) != {"--" + key for key in settings} | {"--receptor", "--ligand", "--out"}:
        raise ValueError("Vina command contains unsupported options")
    if (Path(options["--receptor"]) != Path(spec["data"]) / "receptor/receptor.pdbqt"
            or Path(options["--ligand"]).parts[-2:] != (folder.name, "ligand.pdbqt")
            or Path(options["--out"]).parts[-2:] != (folder.name, "poses.pdbqt")):
        raise ValueError("Vina command input/output paths differ from the job")


def _check_pose(job, folder, canonical, protocol):
    spec = c.read(folder / "spec.json")
    if job.get("synthetic") or spec.get("synthetic"):
        raise ValueError("Synthetic jobs cannot enter the docking evaluator")
    if job["status"] != "completed":
        return
    _check_engine(job, protocol)
    _check_command(job.get("command"), spec, protocol, folder)
    required_artifacts = ("poses.pdbqt", "ligand.pdbqt", "top.sdf", "posebusters.csv", "spec.json")
    artifacts = job.get("artifacts", {})
    if any(artifacts.get(name) != c.sha(folder / name) for name in required_artifacts):
        raise ValueError("Selected Vina pose is missing its worker artifact receipt")
    if job.get("preparation", {}).get("canonical_smiles") != canonical:
        raise ValueError("Docked input differs from frozen canonical SMILES")
    poses = parse_vina_poses((folder / "poses.pdbqt").read_text())
    selected = _selected_pose_properties(folder / "top.sdf")
    if (type(job.get("pose_count")) is not int or job["pose_count"] != len(poses) or len(poses) > protocol["num_modes"]
            or type(job.get("selected_model")) is not int or job["selected_model"] != 1
            or selected["vina_model"] != "1" or job.get("scores") != {"vina_affinity": poses[0]}
            or float(selected["vina_affinity"]) != poses[0]):
        raise ValueError("Docking affinity or pose rank differs from native Vina MODEL 1")
    identity = selected["canonical_isomeric_smiles"]
    if (not identity or job.get("selected_canonical_smiles") != identity
            or job.get("identity_preserved") is not (identity == canonical)):
        raise ValueError("Selected Vina pose identity differs from its recorded identity check")


def read_docking_run(run):
    """Verify campaign receipts, recompute paired energies, and retain source references."""
    archive = c.read(run / "artifact_hashes.json")

    def checked(name):
        path = run / name
        if path.resolve().is_relative_to(run) and path.is_file() and c.sha(path) == archive.get(name):
            return path
        raise ValueError(f"Completed archive hash mismatch: {name}")

    state = c.read(checked("state.json"))
    evaluation = c.read(checked("evaluation.json"))
    if state["status"] != "completed" or evaluation["status"] != "completed" or state.get("pending_request"):
        raise ValueError("Docking evaluator requires a completed campaign")
    if evaluation["campaign_id"] != state["campaign_id"] or not state.get("requests"):
        raise ValueError("Missing docking reservations or inconsistent campaign identity")
    if state.get("execution_mode") != "live" or evaluation.get("execution_mode") != "live" or evaluation.get("synthetic") is not False:
        raise ValueError("Only live standalone AutoDock Vina archives can enter formal selection")

    def frozen(name):
        path = checked("frozen/" + name)
        if c.sha(path) != state["frozen_hashes"].get(name):
            raise ValueError(f"Frozen campaign file changed: {name}")
        return path

    protocol = c.read(frozen("protocol.json"))
    validate_vina_protocol(protocol, live=True)
    manifest = c.read(frozen("manifest.json"))
    if manifest.get("synthetic") or manifest.get("is_mock") is not False:
        raise ValueError("Formal selection requires non-synthetic archived measurements")
    identity = c.read(frozen("campaign.json"))
    if (identity.get("execution_mode") != "live" or identity.get("campaign_id") != state["campaign_id"]
            or identity.get("protocol_hash") != state["frozen_hashes"]["protocol.json"]):
        raise ValueError("Frozen campaign identity does not match live Vina archive")
    table = c.read(frozen("candidates.json"))["candidates"]
    run_hashes = state["frozen_hashes"]
    for name in ("manifest.json", "candidates.json"):
        if protocol["input_sha256"].get(name) != run_hashes[name]:
            raise ValueError(f"Protocol does not bind {name}")
    if manifest.get("chemical_preparation") is not None:
        state_registry = frozen("chemical_states.json")
        if protocol["input_sha256"].get("chemical_states.json") != c.sha(state_registry):
            raise ValueError("Protocol does not bind the chemical state registry")
    chemical_method, chemical_records = c.load_chemical_states(run / "frozen", candidates=table, manifest=manifest)
    expected_chemical = None if chemical_method is None else {"method": chemical_method}
    if protocol.get("chemical_preparation") != expected_chemical:
        raise ValueError("Archived chemical preparation differs from scientific protocol")
    code_hashes = {key: c.sha(frozen(name)) for key, name in _FROZEN_CODE.items()}
    profile = _profile(protocol, manifest, state, code_hashes)
    canonical = {r["candidate_id"]: r["canonical_smiles"] for r in manifest["candidates"]}
    pool = {r["candidate_id"] for r in table}
    if (len(canonical) != len(manifest["candidates"]) or len(pool) != len(table) or pool != set(canonical)
            or not all(isinstance(cid, str) and c.ID.fullmatch(cid) for cid in pool)
            or not all(isinstance(v, str) and v and v == v.strip() for v in canonical.values())):
        raise ValueError("Inconsistent frozen compound registry")
    audit_state = {**state, "candidates": table}
    observations = []
    measured = set()
    for rid, reservation in state["requests"].items():
        if not c.ID.fullmatch(rid):
            raise ValueError("Invalid docking request ID")
        batch = run / "batches" / rid
        if (not batch.resolve().is_relative_to(run) or reservation.get("status") != "ingested"
                or c.sha(batch / "artifact_hashes.json") != reservation.get("ingested_artifacts_sha256")):
            raise ValueError("Docking batch has no matching ingested receipt")
        request, summary, jobs, records = c.validate_batch(batch, run, audit_state)
        if (request.get("schema_version") != BATCH_SCHEMA or summary.get("schema_version") != BATCH_SCHEMA
                or request != reservation["request"] or request["request_id"] != rid
                or request["campaign_id"] != state["campaign_id"] or request["controller_authorized"] is not True
                or request["protocol_sha256"] != run_hashes["protocol.json"]
                or request["manifest_sha256"] != run_hashes["manifest.json"]
                or request["candidates_sha256"] != run_hashes["candidates.json"]):
            raise ValueError("Vina batch differs from its frozen authorization")
        _check_engine(c.read(batch / "engine.json"), protocol)
        _check_engine(summary, protocol)
        if any(summary.get(key) != code_hashes[key] for key in _FROZEN_CODE):
            raise ValueError("Vina batch was produced by different worker code")
        for record in records:
            cid = record["candidate_id"]
            if cid in measured:
                raise ValueError("Duplicate candidate docking execution within one campaign")
            measured.add(cid)
            seed_results = []
            for job in sorted((j for j in jobs if j["candidate_id"] == cid), key=lambda j: j["seed"]):
                folder = batch / "jobs" / f"{cid}_s{job['seed']}"
                _check_pose(job, folder, canonical[cid], protocol)
                seed_results.append((job["seed"], job["status"], job.get("pose_valid"),
                                     (job.get("scores") or {}).get("vina_affinity")))
            observation = {"compound_id": cid, "canonical_smiles": canonical[cid],
                           "score": record[SCORE_FIELD], "seed_results": tuple(seed_results), "batch": str(batch)}
            if chemical_method is not None:
                observation.update({key: chemical_records[cid][key] for key in (
                    "source_smiles", "canonical_parent_identity", "prepared_state_id", "prepared_smiles")})
            observations.append(observation)
    provenance = {"run": str(run), "campaign_id": state["campaign_id"], "archive_sha256": c.sha(run / "artifact_hashes.json"),
                  "protocol_sha256": run_hashes["protocol.json"], "profile_sha256": profile,
                  "engine": ENGINE, "engine_version": ENGINE_VERSION, "vina_sha256": protocol["vina_sha256"],
                  "docked_count": len(measured), "undocked_count": len(pool - measured),
                  "chemical_preparation": expected_chemical}
    return observations, provenance
