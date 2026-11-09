"""Frozen repair contract checks; no generated diagnostic is imported or run."""
import copy
import unittest

from rsi_domains.docking import omission_contract as v1
from rsi_domains.docking import omission_contract_v11 as v11
from rsi_lab.capabilities import validate_request
from rsi_lab.store import digest, encode


class OmissionContractV11Tests(unittest.TestCase):
    def test_prior_fixtures_are_preserved_except_explicit_adapter_version(self):
        old = v1.cases()
        for row in old:
            if row['kind'] == 'normal':
                row['expected_output']['adapter']['version'] = '1.1'
        self.assertEqual(v11.cases()[:10], old)
        self.assertEqual(v1.cases()[0]['expected_output']['adapter']['version'], '1.0')

    def test_spec_is_bounded_and_has_strict_legacy_success_and_rejections(self):
        spec = validate_request(v11.spec())
        self.assertEqual(len(spec['cases']), 23)
        self.assertLess(len(encode(spec)), 256 * 1024)
        self.assertLessEqual(len(spec['cases']) * spec['limits']['wall_seconds_per_case'], 60)
        normal = spec['cases'][10]
        counts = normal['input']['sources']['reference_sdf']['text'].splitlines()[3]
        self.assertEqual(len(counts), 33)
        self.assertEqual([int(counts[i:i+3]) for i in range(0, 33, 3)], [2, 1] + [0] * 9)
        self.assertEqual(normal['expected_output']['residues'][2]['min_distance_to_reference'], 4.)
        self.assertIn('truncated_bond_block', {row['case_id'] for row in spec['cases']})

    def test_all_changed_sources_have_new_hashes_and_expected_success_binding(self):
        for row in v11.cases():
            for key, source in row['input']['sources'].items():
                if row['case_id'] != 'hash_mismatch':
                    self.assertEqual(digest(source['text'].encode()), source['sha256'])
            if row['kind'] == 'normal':
                self.assertEqual(row['expected_output']['input_hashes'], {
                    key: source['sha256'] for key, source in row['input']['sources'].items()})

    def test_prompt_contains_real_format_and_prior_failure_but_no_fixture_answers(self):
        request = {'capability_id': v11.CAPABILITY_ID, 'affects_scientific_results': False}
        source = {'text': 'explicit frozen original', 'sha256': digest(b'explicit frozen original')}
        prompt = v11.engineering_prompt(request, 'prior source bytes', {'error': 'unsupported_format'}, source)
        self.assertIn('prior source bytes', prompt)
        self.assertIn('explicit frozen original', prompt)
        self.assertIn('unsupported_format', prompt)
        self.assertNotIn('strict_untagged_legacy_counts_and_bond', prompt)
        self.assertNotIn('independent-fixture-1', prompt)
        self.assertIn("version:'1.1'", prompt)
        changed = copy.deepcopy(source); changed['text'] += ' altered'
        with self.assertRaises(ValueError):
            v11.engineering_prompt(request, 'prior source bytes', {}, changed)

    def test_prompt_requires_explicit_source_without_reading_local_history(self):
        request = {'capability_id': v11.CAPABILITY_ID, 'affects_scientific_results': False}
        with self.assertRaisesRegex(ValueError, 'explicit frozen reference_source'):
            v11.engineering_prompt(request, 'prior source bytes', {})

    def test_real_input_preserves_explicit_identity_residues_and_source_mapping(self):
        fixture = v1.cases()[0]['input']
        mapping = {name: 'fixture-source:' + name for name in v1.SOURCE_NAMES}
        sources = {mapping[name]: value for name, value in fixture['sources'].items()}
        residues = [{'chain': 'B', 'resseq': 7, 'icode': ''}]
        value = v11.real_input(sources, identity='new-task', residues=residues, source_ids=mapping)
        self.assertEqual(value['identity'], 'new-task')
        self.assertEqual(value['sources'], fixture['sources'])
        self.assertEqual(value['residues'], residues)
        residues[0]['resseq'] = 8
        self.assertEqual(value['residues'][0]['resseq'], 7)
        with self.assertRaisesRegex(ValueError, 'nonempty task identity'):
            v11.real_input(sources, identity='', residues=residues, source_ids=mapping)
        with self.assertRaisesRegex(ValueError, 'selection of 1–32'):
            v11.real_input(sources, identity='new-task', residues=[], source_ids=mapping)


if __name__ == '__main__':
    unittest.main()
