"""Formal selection keeps the cardinality rule after evaluating docking evidence."""
import unittest

from lab_harness.final_selection import _filter_docking_rows, select_final_compounds


def observations(count):
    return [{"compound_id": f"id_{i}", "canonical_smiles": "C" * (i + 1),
             "score": -8., "seed_results": ((17, "completed", True, -7.), (43, "completed", True, -9.)),
             "batch": "fixture/online"} for i in range(count)]


class FinalSelectionTests(unittest.TestCase):
    def test_parent_identity_is_counting_unit_and_different_states_cannot_be_cherry_picked(self):
        row = {**observations(1)[0], 'canonical_parent_identity': 'CN',
               'canonical_smiles': 'C[NH3+]', 'prepared_state_id': 'state_positive'}
        alias = {**row, 'compound_id': 'alias'}
        result = _filter_docking_rows([row, alias], -8.)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['duplicate_count'], 1)
        self.assertEqual(result['compounds'][0]['canonical_parent_identity'], 'CN')
        changed = {**alias, 'canonical_smiles': 'CN', 'prepared_state_id': 'state_neutral'}
        with self.assertRaisesRegex(ValueError, 'Conflicting prepared states'):
            _filter_docking_rows([row, changed], -8.)

    def test_minimum_is_100_and_all_qualifiers_are_retained(self):
        for count in (99, 100, 101):
            result = _filter_docking_rows(observations(count), -8.)
            self.assertEqual(result["count"], count)
            self.assertEqual(len(result["compounds"]), count)
            self.assertEqual(result["feasible"], count >= 100)
            self.assertEqual(result["shortfall"], max(100 - count, 0))
            self.assertEqual(result["mean_score"], -8.)

    def test_aliases_count_once_and_conflicting_seed_pairs_fail(self):
        rows = observations(99)
        alias = {**rows[0], "compound_id": "alias", "batch": "another/online"}
        result = _filter_docking_rows([*rows, alias], -8.)
        self.assertEqual(result["count"], 99)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertFalse(result["feasible"])
        conflict = {**alias, "seed_results": ((17, "completed", True, -5.), (43, "completed", True, -11.))}
        with self.assertRaisesRegex(ValueError, "Conflicting docking"):
            _filter_docking_rows([*rows, conflict], -8.)
        with self.assertRaisesRegex(ValueError, "multiple molecular"):
            _filter_docking_rows([*rows, {**rows[0], "canonical_smiles": "F"}], -8.)

    def test_manifest_rejects_arbitrary_scores_minimum_overrides_and_invalid_paths(self):
        manifest = {"schema_version": "lab_docking_selection_input_v1", "threshold": -7., "runs": ["example"]}
        for key, value in (("min_final_compounds", 1), ("score_direction", "higher"), ("threshold", True),
                           ("threshold", float("nan")), ("runs", []), ("runs", ["example", "./example"])):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                select_final_compounds({**manifest, key: value})
        with self.assertRaisesRegex(ValueError, "AutoDock Vina archives"):
            select_final_compounds({"schema_version": "lab_final_selection_input_v1", "score_name": "minimizedAffinity"})
