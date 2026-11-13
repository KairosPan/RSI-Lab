"""Trusted semantic progress, distinct from receipt identity and execution success.

Adapters describe query/state/result values without execution IDs. The kernel
deduplicates that description while retaining every original execution receipt.
"""
import json

from .controller import identity
from .store import digest, encode


class ResultSignals:
    def __init__(self, runtime):
        self.runtime, self.store, self.run_id = runtime, runtime.store, runtime.run_id

    def record(self, effect, publication, adapter):
        if adapter.progress is None:
            return None
        description = adapter.progress(json.loads(encode(effect)), json.loads(encode(publication)))
        return self.record_frozen(effect, publication, adapter.adapter_id, adapter.version, description)

    def record_frozen(self, effect, publication, adapter_id, adapter_version, description):
        """Replay a publication's pinned description without loading its plugin."""
        if description is None:
            return None
        if effect['status'] != 'succeeded' or effect.get('accounting_unknown'):
            raise ValueError('Semantic progress requires a settled successful execution')
        fields = {'query', 'state', 'result', 'new_scores', 'information_units'}
        if not isinstance(description, dict) or set(description) != fields:
            raise ValueError('Semantic progress must describe query, state, result and explicit counts')
        if any(type(description[k]) is not int or description[k] < 0
               for k in ('new_scores', 'information_units')):
            raise ValueError('Semantic progress counts must be nonnegative integers')
        if len(encode(description)) > 262144:
            raise ValueError('Semantic result description exceeds its bounded size')
        hashes = {name + '_sha256': digest(description[name]) for name in ('query', 'state', 'result')}
        key = digest({'adapter_id': adapter_id, 'adapter_version': adapter_version, **hashes})
        evidence_ids = [row['id'] for row in publication['facts'] + publication['conclusions']]
        if not evidence_ids and (description['new_scores'] or description['information_units']):
            raise ValueError('Semantic progress requires published source evidence')
        rid = identity('semantic_result', effect['id'])

        def apply(tx):
            for fid in evidence_ids:
                tx.get('fact', fid)
            previous = sorted(tx.find('registration', kind='semantic_result'),
                              key=lambda row: (row['created_seq'], row['id']))
            canonical = next((row for row in previous if row['semantic_key'] == key), None)
            new = canonical is None and bool(description['new_scores'] or description['information_units'])
            reason = 'repeated_result' if canonical else 'new_result' if new else 'no_semantic_progress'
            return tx.insert('registration', rid, {
                'kind': 'semantic_result', 'effect_id': effect['id'], 'adapter_id': adapter_id,
                'adapter_version': adapter_version, 'semantic_key': key, **hashes,
                'publication_sha256': digest(publication), 'result_ref': effect['result_ref'],
                'source_evidence_ids': evidence_ids,
                'canonical_evidence_ids': canonical['canonical_evidence_ids'] if canonical else evidence_ids,
                'canonical_effect_id': canonical['canonical_effect_id'] if canonical else effect['id'],
                'is_new_result': new, 'outcome': 'new_result' if new else 'no_result', 'reason_code': reason,
                'new_scores': description['new_scores'] if new else 0,
                'information_units': description['information_units'] if new else 0,
                'batch_index': sum(row['is_new_result'] for row in previous) + int(new),
                'causal_benefit': 'not_established'})

        return self.runtime._mutate('semantic-result:' + effect['id'], 'semantic_result_recorded', apply,
            {'effect_id': effect['id'], 'semantic_key': key, 'publication_sha256': digest(publication),
             'description': description})
