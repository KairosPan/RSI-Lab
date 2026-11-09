"""Fabricated provenance records for contract tests, never chemical predictions."""
import hashlib


METHOD = {'schema_version': 'molscrub_single_state_preparation_v1', 'engine': 'MolScrub',
          'version': '0.3.2', 'rdkit_version': 'fixture',
          'pH': {'low': 7.5, 'high': 8.0, 'interpretation': 'exploratory_interval_not_common_assay_pH'},
          'pka_model': 'rules', 'enumerate_tautomers': True, 'tautomer_rounds': 2,
          'enumerate_stereoisomers': False, 'skip_gen3d': True,
          'admission': 'exactly_one_predicted_state_with_preserved_identity_and_stereochemistry',
          'population_estimates_available': False, 'source_hashes': {'fixture_rules': 'a' * 64}}


def annotate(candidates):
    records = []
    for row in candidates:
        smiles = row['smiles']
        sid = 'state_' + hashlib.sha256(smiles.encode()).hexdigest()[:24]
        row.update(source_smiles=smiles, canonical_parent_identity=smiles,
                   prepared_state_id=sid, prepared_smiles=smiles, canonical_smiles=smiles)
        records.append({key: row[key] for key in ('candidate_id', 'source_smiles', 'canonical_parent_identity',
                                                 'prepared_state_id', 'prepared_smiles')})
        records[-1].update(status='accepted', reason=None, unique_state_count=1,
                           states=[{'state_id': sid, 'canonical_smiles': smiles, 'formal_charge': 0,
                                    'validation_issues': []}])
    return {'schema_version': 'lab_chemical_states_v1', 'method': METHOD, 'records': records}
