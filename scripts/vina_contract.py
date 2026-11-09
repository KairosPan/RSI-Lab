"""The standalone AutoDock Vina profile shared by the controller and CPU worker."""
import math
import re

ENGINE = "AutoDock Vina"
ENGINE_VERSION = "1.2.7"
SEEDS = (17, 43)
BATCH_SCHEMA = "rsi_lab_vina_batch_v1"
POSE_SELECTION = "Vina MODEL 1, lowest native affinity; no replacement after pose checks"
WORKER_FILES = ("vina_contract.py", "vina_worker.py", "run_vina_batch.py")
PROFILE = {
    "engine": ENGINE, "scoring": "vina", "seeds": list(SEEDS), "pose_selection": POSE_SELECTION,
    "cpu": 2, "exhaustiveness": 4, "num_modes": 3, "dock_timeout_seconds": 180,
    "worker_timeout_seconds": 300, "retries": 0,
}


def validate_vina_protocol(protocol, *, live):
    """Replay needs the same scoring semantics; live additionally pins the executable."""
    for key in ("engine", "scoring", "seeds", "pose_selection"):
        if protocol.get(key) != PROFILE[key]:
            raise ValueError(f"Expected a standalone AutoDock Vina protocol with {key}={PROFILE[key]!r}")
    if any(key in protocol for key in ("gnina_sha256", "gnina_version", "gpu_device", "cnn", "cnn_scoring")):
        raise ValueError("GNINA protocols are incompatible with the standalone Vina evaluator")
    if not live:
        return
    if any(protocol.get(key) != expected for key, expected in PROFILE.items()):
        raise ValueError("Live protocol differs from the frozen AutoDock Vina worker settings")
    version = protocol.get("vina_version", {})
    if version.get("returncode") != 0 or version.get("stdout", "").strip() != f"{ENGINE} v{ENGINE_VERSION}":
        raise ValueError(f"Expected output from {ENGINE} {ENGINE_VERSION} --version")
    for key in ("vina_sha256", "runner_sha256", "adapter_sha256", "contract_sha256"):
        if not isinstance(protocol.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", protocol[key]):
            raise ValueError(f"Invalid live protocol hash: {key}")


def parse_vina_poses(text):
    """Return ordered affinities from native multi-model PDBQT, with rank and mode checks."""
    affinities = []
    in_model = False
    energy = None
    atom_seen = False
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "MODEL":
            if in_model or fields[1:] != [str(len(affinities) + 1)]:
                raise ValueError("Invalid Vina PDBQT MODEL order")
            in_model, atom_seen, energy = True, False, None
        elif fields[:3] == ["REMARK", "VINA", "RESULT:"]:
            if not in_model or energy is not None or len(fields) != 6:
                raise ValueError("Invalid Vina PDBQT affinity record")
            values = [float(n) for n in fields[3:]]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Nonfinite Vina PDBQT result")
            energy = values[0]
        elif fields[0] in ("ATOM", "HETATM"):
            if not in_model:
                raise ValueError("Vina PDBQT atom outside MODEL")
            atom_seen = True
        elif fields[0] == "ENDMDL":
            if not in_model or energy is None or not atom_seen or len(fields) != 1:
                raise ValueError("Incomplete Vina PDBQT pose")
            if affinities and energy < affinities[-1]:
                raise ValueError("Vina poses are not sorted by affinity")
            affinities.append(energy)
            in_model = False
    if in_model or not affinities:
        raise ValueError("Missing or incomplete Vina PDBQT poses")
    return affinities
