"""Local/challenge policy replay with synthetic labels; no scorer/model calls."""
import copy
import json
from pathlib import Path
import unittest

from rsi_lab.store import digest
from pablo_task import harness


def initial():
    return {'candidates': [harness._canonical(s) for s in ['CCCCCCCCCCCCC', 'c1ccncc1', 'CC']],
            'scores': [.9, .88, .3]}


def binding(state, anchor):
    return {'version': harness.VERSION, 'run_id': 'test', 'run_root': '/tmp/pablo-harness-fixture',
            'proposal_role': state['proposal_role'], 'anchor_parent': anchor, 'turn': state['turn'],
            'state_sha256': state['state_sha256'], 'policy_sha256': state['policy_sha256'],
            'trajectory_sha256': state['trajectory_sha256'], 'regime': state['regime'],
            'reason': state['reason'], 'max_candidates': state['max_candidates']}


def append_batch(batches, candidates, scores, *, start=None, target=100, config=None):
    start = start or initial()
    current = harness.project(start, batches, target, config)
    state = current['next_plan']
    rows = [{'candidate': harness._canonical(c), 'score': s} for c, s in zip(candidates, scores)]
    batches.append({'effect_id': 'effect-' + str(len(batches)), 'evaluations': rows,
                    'harness_binding': binding(state, current['eligible_anchors'][0]['candidate'])})
    return state['proposal_role']


class FakeStore:
    def __init__(self, start, batches, target=100, config=None):
        self.root = Path('/tmp/pablo-harness-fixture')
        self.blobs = {'initial': json.dumps(start).encode()}
        self.config = {'pablo_harness': copy.deepcopy(config or harness.DEFAULT_CONFIG),
                       'pablo_initial_ref': 'initial', 'pablo_max_new_evaluations': target}
        self.effects = []
        candidates, scores = list(start['candidates']), list(start['scores'])
        for i, batch in enumerate(batches):
            packet = {'harness_binding': batch['harness_binding']}
            receipt = {'result': {'evaluations': batch['evaluations']}}
            self.blobs['packet-' + str(i)] = json.dumps(packet).encode()
            self.blobs['receipt-' + str(i)] = json.dumps(receipt).encode()
            self.effects.append({'id': batch['effect_id'], 'created_seq': i,
                'status': 'succeeded', 'accounting_unknown': False,
                'metadata': {'adapter_id': 'pablo.evaluate'},
                'packet_ref': 'packet-' + str(i), 'result_ref': 'receipt-' + str(i)})
            candidates.extend(r['candidate'] for r in batch['evaluations'])
            scores.extend(r['score'] for r in batch['evaluations'])
        self.progress = {'candidates': candidates, 'scores': scores,
                         'trajectory_sha256': digest({'candidates': candidates, 'scores': scores}),
                         'new_evaluations': len(candidates) - len(start['candidates'])}

    def run(self, run_id):
        return {'config': self.config}

    def list(self, run_id, kind):
        assert kind == 'effect'
        return self.effects

    def read_blob(self, ref):
        return self.blobs[ref]


class HarnessTests(unittest.TestCase):
    def test_original_serial_three_local_then_challenge(self):
        batches, roles = [], []
        for i, candidate in enumerate(['CCCC', 'CCCCC', 'CCCCCC', 'CCCCCCC', 'CCCCCCCC', 'CCCCCCCCC',
                                        'CCCCCCCCCC', 'CCCCCCCCCCC']):
            roles.append(append_batch(batches, [candidate], [.91 + i * .001]))
        self.assertEqual(roles, ['local', 'local', 'local', 'challenge'] * 2)
        state = harness.project(initial(), batches, 100)['next_plan']
        self.assertEqual(state['role_credits']['local']['accepted_scores'], 6)
        self.assertEqual(state['role_credits']['challenge']['accepted_scores'], 2)
        self.assertEqual(state['role_credits']['local']['improving_batches'], 6)
        self.assertEqual(state['stale_batches'], 0)
        self.assertEqual(state['stale_scores'], 0)

    def test_original_stale_batch_adaptation_is_separate_from_new_score_threshold(self):
        config = {**harness.DEFAULT_CONFIG, 'stale_score_threshold': 1000}
        batches = []
        roles = [append_batch(batches, ['C' * (i + 4)], [.2], config=config) for i in range(6)]
        self.assertEqual(roles, ['local', 'local', 'local', 'challenge', 'local', 'challenge'])
        state = harness.project(initial(), batches[:-1], 100, config)['next_plan']
        self.assertIn('stale_batches_challenge', state['reason'])
        self.assertEqual(state['regime'], 'alternating_local_challenge')

    def test_inherited_plateau_changes_allocation_before_four_new_batches(self):
        config = {**harness.DEFAULT_CONFIG, 'stale_score_threshold': 3}
        start = initial()
        start['candidates'].extend(['CCCC', 'CCCCC'])
        start['scores'].extend([.2, .2])
        self.assertEqual(harness.project(start, [], 100, config)['next_plan']['stale_scores'], 4)
        batches = []
        append_batch(batches, ['CCCCCC'], [.2], start=start, config=config)
        state = harness.project(start, batches, 100, config)['next_plan']
        self.assertEqual(state['proposal_role'], 'challenge')
        self.assertIn('score_plateau_challenge', state['reason'])

    def test_improvement_resets_score_plateau_and_records_transition(self):
        config = {**harness.DEFAULT_CONFIG, 'stale_score_threshold': 2}
        batches = []
        append_batch(batches, ['CCCC'], [.95], config=config)
        append_batch(batches, ['CCCCC'], [.96], config=config)
        result = harness.project(initial(), batches, 100, config)
        self.assertEqual(result['next_plan']['regime'], 'scheduled_local_challenge')
        self.assertEqual(result['next_plan']['stale_scores'], 0)
        self.assertEqual([r['to'] for r in result['strategy_transitions']],
                         ['alternating_local_challenge', 'scheduled_local_challenge'])

    def test_only_actual_rows_earn_role_credit(self):
        batches = []
        append_batch(batches, ['CCCC', 'CCCCC'], [.4, .5])
        batches[0]['proposed_count'] = 200  # Deliberately ignored.
        result = harness.project(initial(), batches, 100)
        self.assertEqual(result['next_plan']['role_credits']['local']['accepted_scores'], 2)
        self.assertEqual(result['next_plan']['completed_evaluations'], 2)

    def test_lookups_and_unknown_effects_do_not_advance_turn_or_earn_scores(self):
        batches = []
        append_batch(batches, ['CCCC'], [.4])
        store = FakeStore(initial(), batches)
        store.effects.extend([
            {'id': 'lookup', 'created_seq': 5, 'status': 'succeeded', 'metadata': {'adapter_id': 'pablo.lookup'}},
            {'id': 'unknown', 'created_seq': 6, 'status': 'unknown', 'metadata': {'adapter_id': 'pablo.evaluate'}}])
        result = harness.context(store, 'test', store.progress)
        self.assertEqual(result['next_plan']['turn'], 1)
        self.assertEqual(result['next_plan']['completed_evaluations'], 1)
        self.assertEqual(result['pending_scoring_effect_ids'], ['unknown'])

    def test_stale_role_and_tampered_state_binding_are_rejected(self):
        batches = []
        append_batch(batches, ['CCCC'], [.4])
        for key, value in [('proposal_role', 'challenge'), ('turn', 99), ('state_sha256', 'wrong')]:
            modified = copy.deepcopy(batches)
            modified[0]['harness_binding'][key] = value
            with self.assertRaisesRegex(ValueError, 'exact admitted'):
                harness.project(initial(), modified, 100)

    def test_challenge_uses_different_observed_family_with_explicit_weak_anchor_fallback(self):
        config = {**harness.DEFAULT_CONFIG, 'stale_score_threshold': 1}
        start = {'candidates': [harness._canonical(s) for s in ['CCc1ccccc1', 'c1ccncc1', 'CC']],
                 'scores': [.9, .88, .3]}
        start['candidates'].append(harness._canonical('Cc1ccccc1'))
        start['scores'].append(.89)
        batches = []
        append_batch(batches, ['CCCc1ccccc1'], [.2], start=start, config=config)
        result = harness.project(start, batches, 100, config)
        self.assertEqual(result['next_plan']['proposal_role'], 'challenge')
        self.assertEqual(result['anchor_rule'], 'strongest_observed_different_series_below_percentile_fallback')
        self.assertEqual(result['eligible_anchors'][0]['candidate'], harness._canonical('c1ccncc1'))
        self.assertNotEqual(result['eligible_anchors'][0]['family'], result['incumbent_family'])

    def test_no_distinct_challenge_anchor_is_explicitly_blocked(self):
        start = {'candidates': [harness._canonical(v) for v in ['Cc1ccccc1', 'CCc1ccccc1']],
                 'scores': [.9, .8]}
        config = {**harness.DEFAULT_CONFIG, 'stale_score_threshold': 1}
        batches = []
        append_batch(batches, ['CCCc1ccccc1'], [.8], start=start, config=config)
        result = harness.project(start, batches, 100, config)
        self.assertEqual(result['blocked_reason'], 'no_observed_distinct_challenge_anchor')
        self.assertEqual(result['eligible_anchors'], [])

    def test_remaining_capacity_and_duplicate_rows_cannot_overcredit(self):
        batches = []
        append_batch(batches, ['CCCC', 'CCCCC'], [.4, .5], target=2)
        result = harness.project(initial(), batches, 2)
        self.assertEqual(result['next_plan']['max_candidates'], 0)
        self.assertEqual(result['blocked_reason'], 'score_budget_complete')
        duplicate = copy.deepcopy(batches)
        duplicate[0]['evaluations'][1]['candidate'] = duplicate[0]['evaluations'][0]['candidate']
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            harness.project(initial(), duplicate, 2)
        excess = copy.deepcopy(batches)
        excess[0]['evaluations'].append({'candidate': 'CCCCCC', 'score': .6})
        with self.assertRaisesRegex(ValueError, 'evaluation budget'):
            harness.project(initial(), excess, 2)

    def test_submission_binds_anchor_role_and_remaining_batch_capacity(self):
        store = FakeStore(initial(), [], target=2)
        result = harness.validate_proposal(store, 'test', 'local', initial()['candidates'][0], store.progress)
        self.assertEqual(result['max_candidates'], 2)
        harness.validate_binding(store, 'test', result, ['CCCC', 'CCCCC'], store.progress)
        with self.assertRaisesRegex(ValueError, 'currently due'):
            harness.validate_proposal(store, 'test', 'challenge', 'c1ccncc1', store.progress)
        with self.assertRaisesRegex(ValueError, 'eligible observed'):
            harness.validate_proposal(store, 'test', 'local', 'c1ccncc1', store.progress)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            harness.validate_binding(store, 'test', result, ['CCCC', 'CCCCC', 'CCCCCC'], store.progress)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            harness.validate_binding(store, 'test', result, ['CCCC', 'C(CC)C'], store.progress)

    def test_successful_receipt_credit_and_unknown_exclusion(self):
        store = FakeStore(initial(), [])
        bind = harness.validate_proposal(store, 'test', 'local', initial()['candidates'][0], store.progress)
        effect = {'id': 'e1', 'status': 'succeeded', 'accounting_unknown': False, 'packet_hash': 'packet',
                  'metadata': {'adapter_id': 'pablo.evaluate'}}
        publication = {'data': {'effect_id': 'e1', 'packet_sha256': 'packet',
                                'evaluations': [{'candidate': 'CCCC', 'score': .5}]}}
        credit = harness.publication_credit(effect, publication, bind)
        self.assertEqual(credit['accepted_scores'], 1)
        self.assertEqual(credit['proposal_role'], 'local')
        with self.assertRaisesRegex(ValueError, 'bound successful'):
            harness.publication_credit({**effect, 'accounting_unknown': True}, publication, bind)

    def test_configuration_and_legacy_opt_out(self):
        self.assertIsNone(harness.configuration({}))
        with self.assertRaisesRegex(ValueError, 'complete frozen'):
            harness.configuration({'pablo_harness': {'version': harness.VERSION}})
        for field in ['challenge_every_batches', 'stale_batch_threshold', 'stale_score_threshold', 'max_candidates']:
            with self.assertRaisesRegex(ValueError, 'Invalid'):
                harness.configuration({'pablo_harness': {**harness.DEFAULT_CONFIG, field: True}})

    def test_role_candidate_validation_prevents_decorative_challenge_label(self):
        config = harness.DEFAULT_CONFIG
        challenger = {'proposal_role': 'challenge', 'anchor_parent': harness._canonical('c1ccncc1')}
        with self.assertRaisesRegex(ValueError, 'do not leave'):
            harness.validate_candidates(['CCCc1ccccc1'], challenger, 'CCc1ccccc1', config)
        valid = harness.validate_candidates(['CCc1ccncc1'], challenger, 'CCc1ccccc1', config)
        self.assertEqual(valid['outside_incumbent_series'], 1)
        local = {'proposal_role': 'local', 'anchor_parent': harness._canonical('CCc1ccccc1')}
        harness.validate_candidates(['CCCc1ccccc1'], local, 'CCc1ccccc1', config)
        with self.assertRaisesRegex(ValueError, 'anchor-similarity'):
            harness.validate_candidates(['CC'], local, 'CCc1ccccc1', config)

    def test_successful_receipt_replay_cannot_relabel_local_structures_as_challenge(self):
        start = {'candidates': [harness._canonical(s) for s in ['CCc1ccccc1', 'c1ccncc1', 'CC']],
                 'scores': [.9, .88, .3]}
        config = {**harness.DEFAULT_CONFIG, 'stale_score_threshold': 1}
        batches = []
        append_batch(batches, ['CCCc1ccccc1'], [.2], start=start, config=config)
        append_batch(batches, ['CCc1ccncc1'], [.3], start=start, config=config)
        batches[-1]['evaluations'][0]['candidate'] = harness._canonical('CCCCc1ccccc1')
        with self.assertRaisesRegex(ValueError, 'do not leave'):
            harness.project(start, batches, 100, config)

    def test_context_does_not_import_role_credit_from_another_run_binding(self):
        batches = []
        append_batch(batches, ['CCCC'], [.5])
        batches[0]['harness_binding']['run_id'] = 'other-run'
        store = FakeStore(initial(), batches)
        with self.assertRaisesRegex(ValueError, 'different frozen run'):
            harness.context(store, 'test', store.progress)


if __name__ == '__main__':
    unittest.main()
