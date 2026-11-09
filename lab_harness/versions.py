"""Small, hash-addressed Lab policy bundles; the evaluator is never editable here."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

FILES = frozenset({"manifest.json", "lab_planner.md", "input_view.json"})
MODES = frozenset({"expanded_json_v1", "compact_json_v1", "lossless_deduplicated_json_v1"})
MAX_FILE_BYTES = 64 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid JSON number: {value}")))


def _read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Harness must be a real directory, not a symlink")
    if {entry.name for entry in path.iterdir()} != FILES:
        raise ValueError("Harness must contain exactly manifest.json, lab_planner.md, and input_view.json")
    contents = {}
    for name in sorted(FILES):
        file = path / name
        if file.is_symlink() or not file.is_file():
            raise ValueError(f"Harness member must be a regular file: {name}")
        # O_NOFOLLOW also rejects a link substituted after the directory check.
        fd = os.open(file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError(f"Harness member must be a regular file: {name}")
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"Harness member exceeds 64 KiB: {name}")
        contents[name] = raw
    manifest, view = _json(contents["manifest.json"]), _json(contents["input_view.json"])
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "name", "version"}:
        raise ValueError("Manifest requires exactly schema_version, name, and version")
    if manifest["schema_version"] != "lab_harness_v1":
        raise ValueError("Unsupported harness schema")
    for key in ("name", "version"):
        if not isinstance(manifest[key], str) or not manifest[key].strip() or len(manifest[key]) > 128:
            raise ValueError(f"Manifest {key} must be a nonempty string of at most 128 characters")
    if not isinstance(view, dict) or set(view) != {"mode"} or not isinstance(view["mode"], str) or view["mode"] not in MODES:
        raise ValueError("Input view must specify exactly one supported lossless mode")
    if not contents["lab_planner.md"].decode("utf-8").strip():
        raise ValueError("Planner prompt cannot be empty")
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"files": hashes, "harness_hash": digest, "manifest": manifest, "input_view": view}, contents


def load_harness(path):
    """Validate a complete bundle and return hashes of the exact file bytes."""
    return _read(path)[0]


def freeze_harness(source, destination):
    """Copy a validated snapshot once, with read-only files and directory.

    Hash verification, not filesystem permission bits, is the integrity boundary:
    a file owner can deliberately make a snapshot writable again.
    """
    info, contents = _read(source)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for name, raw in contents.items():
            with (destination / name).open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            (destination / name).chmod(0o444)
        if load_harness(destination) != info:
            raise ValueError("Frozen harness integrity check failed")
        destination.chmod(0o555)
        return info
    except BaseException:
        destination.chmod(0o755)
        shutil.rmtree(destination)
        raise
