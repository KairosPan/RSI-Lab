import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from scripts.literature_autonomy_cases import DEFAULT_SEED, freeze_cases, private_oracles, public_cases


class AutonomyCaseTests(unittest.TestCase):
    def test_twenty_cases_are_ten_counterfactual_pairs_with_only_source_delivery_changed(self):
        public = {case["case_id"]: case for case in public_cases()}
        private = private_oracles()
        self.assertEqual(len(public), 20)
        self.assertEqual(public.keys(), private.keys())
        self.assertEqual(sum(row["external_research_needed"] for row in private.values()), 10)
        pairs = {}
        for case_id, oracle in private.items():
            self.assertRegex(case_id, r"^c_[0-9a-f]{16}$")
            pairs.setdefault(oracle["pair_id"], []).append(case_id)
        self.assertEqual(len(pairs), 10)
        for pair in pairs.values():
            self.assertEqual(len(pair), 2)
            missing = next(public[key] for key in pair if private[key]["external_research_needed"])
            provided = next(public[key] for key in pair if not private[key]["external_research_needed"])
            self.assertEqual(missing["goal"], provided["goal"])
            self.assertEqual(missing["evidence"][0], provided["evidence"][0])
            left, right = missing["evidence"][1], provided["evidence"][1]
            self.assertEqual(left["content"]["metadata"], right["content"]["metadata"])
            self.assertEqual(left["id"], right["id"])
            self.assertEqual(left["content"]["passages"], [])
            self.assertEqual(len(right["content"]["passages"]), 1)

    def test_public_records_do_not_contain_oracle_fields_or_tool_instructions(self):
        private_fields = {"scenario_id", "pair_id", "external_research_needed", "acceptable_routes",
                          "required_reasoning", "forbidden_claims", "evaluation_notes"}

        def inspect(value):
            if isinstance(value, dict):
                self.assertFalse(private_fields.intersection(value))
                for nested in value.values():
                    inspect(nested)
            elif isinstance(value, list):
                for nested in value:
                    inspect(nested)

        for case in public_cases():
            self.assertEqual(set(case), {"case_id", "goal", "evidence"})
            self.assertIsNone(re.search(r"\b(search|literature|request_literature|positive|negative|delegate)\b", case["goal"], re.I))
            inspect(case)
            planning = case["evidence"][0]
            self.assertEqual(planning["provenance"]["origin"], "constructed_planning_example")
            self.assertIn("not an executed experiment", planning["provenance"]["scope"])

    def test_excerpt_identity_scope_and_per_source_quote_limit(self):
        totals = {}
        private = private_oracles()
        for case in public_cases():
            source = case["evidence"][1]
            url = source["provenance"]["origin"]
            self.assertTrue(url.startswith("https://"))
            self.assertFalse(source["provenance"]["full_document_delivered"])
            self.assertEqual(source["content"]["metadata"]["url"], url)
            for passage in source["content"]["passages"]:
                digest = hashlib.sha256(passage["text"].encode()).hexdigest()
                self.assertEqual(passage["sha256"], digest)
                self.assertEqual(private[case["case_id"]]["excerpt_sha256"], digest)
                self.assertTrue(passage["locator"])
                totals[url] = totals.get(url, 0) + len(passage["text"].split())
        self.assertEqual(len(totals), 9)
        self.assertTrue(all(count <= 25 for count in totals.values()), totals)

    def test_order_is_seeded_ids_are_stable_and_callers_cannot_mutate_future_inputs(self):
        cases = public_cases(DEFAULT_SEED)
        self.assertEqual(cases, public_cases(DEFAULT_SEED))
        alternate = public_cases(DEFAULT_SEED + 1)
        self.assertEqual({row["case_id"] for row in cases}, {row["case_id"] for row in alternate})
        self.assertNotEqual([row["case_id"] for row in cases], [row["case_id"] for row in alternate])
        cases[0]["evidence"][1]["content"]["metadata"]["url"] = "corrupt"
        cases[0]["evidence"][0]["content"].clear()
        self.assertNotEqual(cases, public_cases(DEFAULT_SEED))
        for invalid in (True, None, "20261115"):
            with self.assertRaises(ValueError):
                public_cases(invalid)

    def test_novelty_case_has_unambiguous_local_arithmetic_not_fake_assay_results(self):
        private = private_oracles()
        selected = [case for case in public_cases() if private[case["case_id"]]["scenario_id"] == "novelty_denominator"]
        self.assertEqual(len(selected), 2)
        for case in selected:
            values = case["evidence"][0]["content"]
            generated = values["generated_identifiers"]
            overlap = values["training_set_members_in_generated_set"]
            self.assertEqual(len(generated), len(set(generated)))
            self.assertTrue(set(overlap) <= set(generated))
            self.assertAlmostEqual(len(set(generated) - set(overlap)) / len(generated), 2 / 3)

    def test_freeze_separates_inputs_and_rubric_hashes_actual_bytes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "frozen"
            # Generation and freeze are offline; a network attempt fails the test.
            with patch("socket.socket", side_effect=AssertionError("No network permitted")):
                manifest = freeze_cases(destination)
            self.assertFalse(manifest["counts_toward_molecule_objective_evaluations"])
            self.assertEqual(manifest["case_count"], 20)
            self.assertEqual(manifest["pair_count"], 10)
            self.assertEqual(manifest["minimum_real_positive_child_paths"], 2)
            self.assertEqual(json.loads((destination / "manifest.json").read_text()), manifest)
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), digest)
            before = (destination / "public-cases.json").read_bytes()
            with self.assertRaises(FileExistsError):
                freeze_cases(destination, DEFAULT_SEED + 1)
            self.assertEqual((destination / "public-cases.json").read_bytes(), before)
            model_input = json.loads(before)
            self.assertEqual(model_input["cases"], public_cases())
            self.assertNotIn("oracles", model_input)
            self.assertEqual((destination / "private").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
