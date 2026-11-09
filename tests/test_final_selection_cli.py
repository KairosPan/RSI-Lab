"""Exercise the CLI's docking manifest, immutable diagnostics, and legacy input error."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from lab_harness.__main__ import main
from test_docking_archive import write_archive


class FinalSelectionCliTests(unittest.TestCase):
    def test_cli_reads_relative_archive_and_saves_docking_shortfall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_archive(root / "live_archive")
            source, output = root / "manifest.json", root / "selection.json"
            source.write_text(json.dumps({"schema_version": "lab_docking_selection_input_v1",
                                          "threshold": -7., "runs": ["live_archive"]}))
            args = ["select-final", "--input", str(source), "--out", str(output)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 2)
            report = json.loads(output.read_text())
            self.assertEqual(report["count"], 2)
            self.assertEqual(report["direction"], "lower")
            self.assertEqual(report["evaluator"]["score_units"], "kcal/mol")
            self.assertTrue(report["evidence_verified"])
            self.assertEqual(report["input_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(len(report["validator_sha256"]), 64)
            preserved = output.read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 1)
            self.assertEqual(output.read_bytes(), preserved)

    def test_full_archive_selection_crosses_the_100_compound_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = []
            for index in range(101):
                energy = -9. if index < 99 else (-8. if index == 99 else -7.)
                cases.append((f"id_{index}", "C" * (index + 1), (energy, energy)))
            paths = []
            for offset in range(0, len(cases), 4):
                path = root / f"worker_{offset}"
                write_archive(path, cases=cases[offset:offset + 4])
                paths.append(path.name)
            for threshold, count in ((-9., 99), (-8., 100), (-7., 101)):
                source, output = root / f"input_{count}.json", root / f"result_{count}.json"
                source.write_text(json.dumps({"schema_version": "lab_docking_selection_input_v1",
                                              "threshold": threshold, "runs": paths}))
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(["select-final", "--input", str(source), "--out", str(output)])
                self.assertEqual(code, 0 if count >= 100 else 2)
                result = json.loads(output.read_text())
                self.assertEqual(result["count"], count)
                self.assertEqual(len(result["compounds"]), count)
                self.assertEqual(result["feasible"], count >= 100)

    def test_supplied_score_table_cannot_publish_a_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "scores.json", root / "result.json"
            source.write_text(json.dumps({"schema_version": "lab_final_selection_input_v1", "threshold": -7.,
                                          "score_name": "minimizedAffinity", "score_direction": "lower",
                                          "compounds": [{"compound_id": str(i), "canonical_smiles": "C" * (i + 1), "score": -9.} for i in range(100)]}))
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main(["select-final", "--input", str(source), "--out", str(output)]), 1)
            self.assertIn("AutoDock Vina archives", errors.getvalue())
            self.assertFalse(output.exists())
