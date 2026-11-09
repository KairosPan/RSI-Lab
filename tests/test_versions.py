import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest

from lab_harness.versions import freeze_harness, load_harness


class HarnessVersionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "manifest.json").write_text(json.dumps({"schema_version": "lab_harness_v1", "name": "H0", "version": "1"}))
        (self.source / "input_view.json").write_text('{"mode":"expanded_json_v1"}')
        (self.source / "lab_planner.md").write_text("Plan from delivered evidence.\n")

    def cleanup(self):
        for path in self.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
        self.tmp.cleanup()

    def test_freeze_keeps_exact_bytes_hashes_and_is_independent(self):
        expected = load_harness(self.source)
        destination = self.root / "frozen"
        actual = freeze_harness(self.source, destination)
        self.assertEqual(actual, expected)
        expected_hash = hashlib.sha256(json.dumps(expected["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(actual["harness_hash"], expected_hash)
        self.assertFalse(destination.stat().st_mode & stat.S_IWUSR)
        for name in actual["files"]:
            self.assertEqual((destination / name).read_bytes(), (self.source / name).read_bytes())
            self.assertFalse((destination / name).stat().st_mode & stat.S_IWUSR)
        (self.source / "lab_planner.md").write_text("Changed source\n")
        self.assertEqual(load_harness(destination), expected)
        self.assertNotEqual(load_harness(self.source)["harness_hash"], expected["harness_hash"])

    def test_existing_destination_is_never_overwritten(self):
        destination = self.root / "existing"
        destination.mkdir()
        marker = destination / "keep"
        marker.write_text("original")
        with self.assertRaises(FileExistsError):
            freeze_harness(self.source, destination)
        self.assertEqual(marker.read_text(), "original")

    def test_extra_files_and_missing_files_are_rejected(self):
        extra = self.source / "evaluator.py"
        extra.write_text("pass")
        with self.assertRaises(ValueError):
            load_harness(self.source)
        extra.unlink()
        (self.source / "input_view.json").unlink()
        with self.assertRaises(ValueError):
            load_harness(self.source)

    def test_symlink_file_and_directory_are_rejected(self):
        prompt = self.source / "lab_planner.md"
        target = self.root / "outside"
        target.write_bytes(prompt.read_bytes())
        prompt.unlink()
        prompt.symlink_to(target)
        with self.assertRaises(ValueError):
            load_harness(self.source)
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            load_harness(alias)

    def test_prompt_is_nonempty_utf8_with_byte_limit(self):
        for invalid in (b" \n", b"\xff", b"x" * (65536 + 1)):
            (self.source / "lab_planner.md").write_bytes(invalid)
            with self.assertRaises((ValueError, UnicodeError)):
                load_harness(self.source)
        (self.source / "lab_planner.md").write_bytes(b"x" * 65536)
        load_harness(self.source)

    def test_manifest_has_only_allowed_fields(self):
        for value in (
            {"schema_version": "lab_harness_v1", "name": "x", "version": "1", "budget": 999},
            {"schema_version": "future", "name": "x", "version": "1"},
            {"schema_version": "lab_harness_v1", "name": "", "version": "1"},
            {"schema_version": "lab_harness_v1", "name": "x", "version": 1},
        ):
            (self.source / "manifest.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_harness(self.source)

    def test_only_three_lossless_modes_are_accepted(self):
        for mode in ("expanded_json_v1", "compact_json_v1", "lossless_deduplicated_json_v1"):
            (self.source / "input_view.json").write_text(json.dumps({"mode": mode}))
            self.assertEqual(load_harness(self.source)["input_view"]["mode"], mode)
        for value in ({"mode": "no_change"}, {"mode": "summary"}, {"mode": []}, {"mode": "compact_json_v1", "limit": 1}):
            (self.source / "input_view.json").write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_harness(self.source)

    def test_duplicate_json_keys_are_rejected(self):
        (self.source / "input_view.json").write_text('{"mode":"compact_json_v1","mode":"expanded_json_v1"}')
        with self.assertRaises(ValueError):
            load_harness(self.source)


if __name__ == "__main__":
    unittest.main()
