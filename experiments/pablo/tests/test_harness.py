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


LED = harness.LED_DEFAULT_CONFIG
YES = {'challenge_dispatched': True, 'challenge_adopted': True, 'reason': 'The leading series has stalled.'}
NO = {'challenge_dispatched': False, 'challenge_adopted': False, 'reason': 'The last edit still improved.'}
LEADER = harness._canonical('CCCCCCCCCCCCC')
PYRIDINE = harness._canonical('c1ccncc1')
BACKSTOP = 'The backstop requires a challenge batch proposed by the Challenge sub-agent'
LED_STATE_KEYS = {'version', 'required_role', 'unchallenged_stale_batches', 'challenge_backstop_batches', 'turn',
                  'stale_batches', 'stale_scores', 'signals', 'completed_evaluations', 'remaining_evaluations',
                  'max_candidates', 'requested_candidates', 'trajectory_sha256', 'policy_sha256',
                  'completed_effect_ids', 'role_credits', 'state_sha256'}
LED_BINDING_KEYS = {'version', 'run_id', 'run_root', 'proposal_role', 'anchor_parent', 'turn', 'state_sha256',
                    'policy_sha256', 'trajectory_sha256', 'required_role', 'max_candidates', 'decision'}
NO_TOTALS = {'lab_chosen_challenge_batches': 0, 'backstop_challenge_batches': 0, 'dispatched_not_adopted_batches': 0}


def led_binding(state, role, anchor, decision):
    return {'version': harness.LED_VERSION, 'run_id': 'test', 'run_root': '/tmp/pablo-harness-fixture',
            'proposal_role': role, 'anchor_parent': anchor, 'turn': state['turn'],
            'state_sha256': state['state_sha256'], 'policy_sha256': state['policy_sha256'],
            'trajectory_sha256': state['trajectory_sha256'], 'required_role': state['required_role'],
            'max_candidates': state['max_candidates'], 'decision': copy.deepcopy(decision)}


def append_led_batch(batches, role, candidates, scores, decision=NO, *, start=None, config=LED):
    """Bind the next batch to the role the Lab chose; return the state it was admitted under."""
    start = start or initial()
    current = harness.project(start, batches, 100, config)
    rows = [{'candidate': harness._canonical(c), 'score': s} for c, s in zip(candidates, scores)]
    anchor = current['eligible_anchors'][role][0]['candidate']
    batches.append({'effect_id': 'effect-' + str(len(batches)), 'evaluations': rows,
                    'harness_binding': led_binding(current['next_plan'], role, anchor, decision)})
    return current['next_plan']


def stale_local_batches(batches, count, config=LED):
    """Local batches of one long alkane each, all scored below the leader's 0.9."""
    for _ in range(count):
        append_led_batch(batches, 'local', ['C' * (14 + len(batches))], [.2], config=config)


class LedHarnessTests(unittest.TestCase):
    def plan(self, batches, config=LED):
        return harness.project(initial(), batches, 100, config)['next_plan']

    def test_inherited_stagnation_does_not_arm_the_backstop(self):            # Review Focus 5
        start = {'candidates': ['C' * n for n in range(13, 73)],
                 'scores': [.9] + [.5 - n * .001 for n in range(59)]}
        plan = harness.project(start, [], 20, LED)['next_plan']
        self.assertEqual(plan['stale_scores'], 59)
        self.assertEqual((plan['required_role'], plan['unchallenged_stale_batches']), (None, 0))
        self.assertTrue(plan['signals']['score_plateau'])
        self.assertFalse(plan['signals']['stale_batches'])

    def test_backstop_fires_exactly_at_the_limit(self):                       # Review Focus 5
        batches = []
        stale_local_batches(batches, 3)
        plan = self.plan(batches)
        self.assertEqual((plan['required_role'], plan['unchallenged_stale_batches']), (None, 3))
        stale_local_batches(batches, 1)
        plan = self.plan(batches)
        self.assertEqual((plan['required_role'], plan['unchallenged_stale_batches']), ('challenge', 4))
        self.assertEqual(plan['challenge_backstop_batches'], 4)

    def test_a_new_best_or_a_scored_challenge_resets_the_counter(self):
        batches = []
        stale_local_batches(batches, 3)
        improving, challenged = copy.deepcopy(batches), copy.deepcopy(batches)
        append_led_batch(improving, 'local', ['C' * 40], [.95])
        append_led_batch(challenged, 'challenge', ['CCc1ccncc1'], [.2], YES)
        for sequence in (improving, challenged):
            with self.subTest(last=sequence[-1]['harness_binding']['proposal_role']):
                plan = self.plan(sequence)
                self.assertEqual((plan['required_role'], plan['unchallenged_stale_batches']), (None, 0))
        self.assertEqual(self.plan(improving)['stale_batches'], 0)
        self.assertEqual(self.plan(challenged)['stale_batches'], 4)

    def test_a_challenge_batch_with_no_scored_candidate_does_not_reset(self): # Review Focus 5
        batches = []
        stale_local_batches(batches, 3)
        append_led_batch(batches, 'challenge', [], [], YES)
        plan = self.plan(batches)
        self.assertEqual((plan['unchallenged_stale_batches'], plan['required_role']), (4, 'challenge'))
        self.assertEqual(plan['role_credits']['challenge']['batches'], 1)

    def test_backstop_off_never_requires(self):
        config = {**LED, 'challenge_backstop_batches': None}
        batches = []
        stale_local_batches(batches, 10, config)
        plan = self.plan(batches, config)
        self.assertEqual((plan['required_role'], plan['unchallenged_stale_batches']), (None, 10))
        self.assertIsNone(plan['challenge_backstop_batches'])

    def test_required_challenge_refuses_local_and_undispatched(self):
        batches = []
        stale_local_batches(batches, 4)
        store = FakeStore(initial(), batches, config=LED)
        with self.assertRaisesRegex(ValueError, BACKSTOP):
            harness.validate_proposal(store, 'test', 'local', LEADER, store.progress, decision=NO)
        with self.assertRaisesRegex(ValueError, BACKSTOP):
            harness.validate_proposal(store, 'test', 'challenge', PYRIDINE, store.progress, decision=NO)
        bound = harness.validate_proposal(store, 'test', 'challenge', PYRIDINE, store.progress, decision=YES)
        self.assertEqual(set(bound), LED_BINDING_KEYS)
        self.assertEqual((bound['required_role'], bound['decision'], bound['turn']), ('challenge', YES, 4))
        self.assertEqual(harness.validate_binding(store, 'test', bound, ['CCc1ccncc1'], store.progress), bound)

    def test_a_required_challenge_must_run_the_adopted_challenge_proposal(self):
        ignored = {'challenge_dispatched': True, 'challenge_adopted': False,
                   'reason': 'The Lab started Challenge but ran its own challenge batch.'}
        batches = []
        stale_local_batches(batches, 4)
        store = FakeStore(initial(), batches, config=LED)
        with self.assertRaisesRegex(ValueError, BACKSTOP):
            harness.validate_proposal(store, 'test', 'challenge', PYRIDINE, store.progress, decision=ignored)
        bound = harness.validate_proposal(store, 'test', 'challenge', PYRIDINE, store.progress, decision=YES)
        with self.assertRaisesRegex(ValueError, BACKSTOP):
            harness.validate_binding(store, 'test', {**bound, 'decision': ignored}, ['CCc1ccncc1'], store.progress)
        self.assertEqual(append_led_batch(batches, 'challenge', ['CCc1ccncc1'], [.2], ignored)['required_role'],
                         'challenge')
        with self.assertRaisesRegex(ValueError, BACKSTOP):
            harness.project(initial(), batches, 100, LED)
        # With nothing required, a dispatched Challenge whose proposal is not run still allows a local batch.
        store = FakeStore(initial(), [], config=LED)
        local = harness.validate_proposal(store, 'test', 'local', LEADER, store.progress, decision=ignored)
        self.assertEqual((local['required_role'], local['decision']), (None, ignored))
        free = []
        append_led_batch(free, 'local', ['C' * 14], [.2], ignored)
        self.assertEqual(harness.project(initial(), free, 100, LED)['decisions'][0]['decision'], ignored)

    def test_lab_may_run_its_own_challenge_when_none_is_required(self):
        own = {'challenge_dispatched': False, 'challenge_adopted': False, 'reason': 'Trying the amide series.'}
        store = FakeStore(initial(), [], config=LED)
        bound = harness.validate_proposal(store, 'test', 'challenge', PYRIDINE, store.progress, decision=own)
        self.assertEqual((bound['proposal_role'], bound['required_role'], bound['decision']),
                         ('challenge', None, own))
        harness.validate_binding(store, 'test', bound, ['CCc1ccncc1'], store.progress)
        local = harness.validate_proposal(store, 'test', 'local', LEADER, store.progress, decision=NO)
        self.assertEqual(local['proposal_role'], 'local')

    def test_decision_shape(self):
        for bad in (None, {}, {**YES, 'extra': 1}, {**YES, 'reason': ''}, {**YES, 'reason': 'a\nb'},
                    {**YES, 'reason': 'r' * 301}, {**YES, 'challenge_dispatched': 1},
                    {**YES, 'challenge_dispatched': False}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                harness.validate_decision(bad, 'challenge')
        with self.assertRaises(ValueError):
            harness.validate_decision(YES, 'local')            # adopted needs a challenge batch
        for bad in ({**YES, 'reason': ' \t '}, {**YES, 'reason': 'a\rb'}, {**YES, 'reason': 7},
                    {**YES, 'challenge_adopted': 'yes'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                harness.validate_decision(bad, 'challenge')
        with self.assertRaises(ValueError):
            harness.validate_decision(NO, 'scaffold_hop')
        normal = harness.validate_decision(YES, 'challenge')
        self.assertEqual(normal, YES)
        self.assertIsNot(normal, YES)
        self.assertEqual(harness.validate_decision({**NO, 'reason': '  ' + NO['reason'] + '\n'}, 'local'), NO)
        self.assertEqual(harness.validate_decision({**YES, 'reason': 'r' * 300}, 'challenge')['reason'], 'r' * 300)
        offered = {**YES, 'challenge_adopted': False}
        self.assertEqual(harness.validate_decision(offered, 'local'), offered)

    def test_dispatch_claim_must_match_the_call_record(self):                 # Review Focus 3 and 4
        harness.check_dispatch(YES, [{'role': 'challenge', 'started_in_call': False}])
        harness.check_dispatch(NO, [{'role': 'local'}, {'role': None}])
        harness.check_dispatch(NO, None)
        for decision, children in ((YES, []), (YES, None), (YES, [{'role': None}]), (YES, [{'role': 'local'}]),
                                   (NO, [{'role': 'challenge'}])):
            with self.subTest(children=children), self.assertRaises(ValueError) as raised:
                harness.check_dispatch(decision, children)
            self.assertIn('call record', str(raised.exception))
        with self.assertRaisesRegex(ValueError, 'could not be read'):
            harness.check_dispatch(YES, [{'role': None}])
        harness.check_dispatch(YES, ['not a thread', {'role': 'local'}, {'role': 'challenge'}])

    def test_replay_takes_each_role_from_its_binding_and_counts_totals(self):
        batches = []
        stale_local_batches(batches, 2)
        self.assertIsNone(append_led_batch(batches, 'challenge', ['CCc1ccncc1'], [.2], YES)['required_role'])
        stale_local_batches(batches, 4)
        required = append_led_batch(batches, 'challenge', ['OCc1ccncc1'], [.2], YES)
        self.assertEqual(required['required_role'], 'challenge')
        offered = {'challenge_dispatched': True, 'challenge_adopted': False,
                   'reason': 'The Challenge proposal repeated an observed failure.'}
        append_led_batch(batches, 'local', ['C' * 60], [.2], offered)
        result = harness.project(initial(), batches, 100, LED)
        self.assertEqual(result['totals'], {'lab_chosen_challenge_batches': 1, 'backstop_challenge_batches': 1,
                                            'dispatched_not_adopted_batches': 1})
        self.assertEqual(harness.project(initial(), batches[:3], 100, LED)['totals'],
                         {**NO_TOTALS, 'lab_chosen_challenge_batches': 1})
        self.assertEqual(result['next_plan']['role_credits']['challenge']['batches'], 2)
        self.assertEqual(result['next_plan']['role_credits']['local']['batches'], 7)
        decisions = result['decisions']
        self.assertEqual([d['turn'] for d in decisions], list(range(1, 9)))
        self.assertEqual([d['proposal_role'] for d in decisions],
                         ['local', 'challenge'] + ['local'] * 4 + ['challenge', 'local'])
        self.assertEqual([d['required'] for d in decisions], [False] * 6 + [True, False])
        self.assertEqual(decisions[-1], {'turn': 8, 'proposal_role': 'local', 'required': False,
                                         'decision': offered, 'improved': False, 'accepted_scores': 1})

    def test_replay_refuses_a_batch_that_ignored_a_required_challenge(self):
        batches = []
        stale_local_batches(batches, 4)
        append_led_batch(batches, 'local', ['C' * 60], [.2])
        with self.assertRaisesRegex(ValueError, BACKSTOP):
            harness.project(initial(), batches, 100, LED)
        unbound = copy.deepcopy(batches[:1])
        del unbound[0]['harness_binding']['proposal_role']
        with self.assertRaisesRegex(ValueError, 'role'):
            harness.project(initial(), unbound, 100, LED, verify_bindings=False)

    def test_fixed_schedule_runs_refuse_a_decision(self):
        store = FakeStore(initial(), [])
        with self.assertRaisesRegex(ValueError, 'decision'):
            harness.validate_proposal(store, 'test', 'local', LEADER, store.progress, decision=YES)
        self.assertNotIn('decision', harness.validate_proposal(store, 'test', 'local', LEADER, store.progress))
        led_store = FakeStore(initial(), [], config=LED)
        with self.assertRaisesRegex(ValueError, 'decision'):
            harness.validate_proposal(led_store, 'test', 'local', LEADER, led_store.progress)

    def test_led_configuration_has_its_own_complete_key_set(self):
        self.assertEqual(harness.configuration({'pablo_harness': LED}), LED)
        self.assertEqual(harness.DEFAULT_CONFIG['version'], harness.VERSION)
        off = {**LED, 'challenge_backstop_batches': None}
        self.assertEqual(harness.configuration({'pablo_harness': off}), off)
        for value in (0, 101, True, 4.0, '4'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Invalid'):
                harness.configuration({'pablo_harness': {**LED, 'challenge_backstop_batches': value}})
        for mixed in ({**LED, 'challenge_every_batches': 4}, {**harness.DEFAULT_CONFIG, 'version': harness.LED_VERSION},
                      {**LED, 'version': harness.VERSION}):
            with self.subTest(keys=sorted(mixed)), self.assertRaisesRegex(ValueError, 'complete frozen'):
                harness.configuration({'pablo_harness': mixed})
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            harness.configuration({'pablo_harness': {**LED, 'version': 'pablo_lab_led_v0'}})

    def test_led_projection_offers_both_roles(self):
        result = harness.project(initial(), [], 100, LED)
        self.assertEqual(result['schema_version'], harness.LED_VERSION)
        self.assertEqual(set(result['next_plan']), LED_STATE_KEYS)
        self.assertEqual(result['instructions'], harness.ROLE_INSTRUCTIONS)
        self.assertEqual({role: [r['candidate'] for r in rows] for role, rows in result['eligible_anchors'].items()},
                         {'local': [LEADER], 'challenge': [PYRIDINE]})
        self.assertEqual(result['eligible_anchor_count'], {'local': 1, 'challenge': 1})
        self.assertEqual((result['decisions'], result['totals'], result['blocked_reason']), ([], NO_TOTALS, None))
        self.assertEqual(result['next_plan']['requested_candidates'], 3)

    def test_missing_challenge_anchor_blocks_only_a_required_challenge(self):
        start = {'candidates': [harness._canonical(v) for v in ['Cc1ccccc1', 'CCc1ccccc1']], 'scores': [.9, .8]}
        store = FakeStore(start, [], config=LED)
        self.assertIsNone(harness.context(store, 'test', store.progress)['blocked_reason'])
        with self.assertRaisesRegex(ValueError, 'eligible observed'):
            harness.validate_proposal(store, 'test', 'challenge', 'CCc1ccccc1', store.progress, decision=YES)
        harness.validate_proposal(store, 'test', 'local', start['candidates'][0], store.progress, decision=NO)
        batches = []
        for candidate in ['Cc1ccccc1C', 'Cc1cccc(C)c1', 'Cc1ccc(C)cc1', 'Cc1ccccc1F']:
            append_led_batch(batches, 'local', [candidate], [.5], start=start)
        result = harness.project(start, batches, 100, LED)
        self.assertEqual(result['next_plan']['required_role'], 'challenge')
        self.assertEqual(result['blocked_reason'], 'no_observed_distinct_challenge_anchor')
        self.assertEqual(result['eligible_anchors']['challenge'], [])

    def test_scoring_rechecks_the_decision_its_binding_carries(self):
        batches = []
        stale_local_batches(batches, 4)
        store = FakeStore(initial(), batches, config=LED)
        bound = harness.validate_proposal(store, 'test', 'challenge', PYRIDINE, store.progress, decision=YES)
        undispatched = {**bound, 'decision': NO}
        padded = {**bound, 'decision': {**YES, 'reason': ' ' + YES['reason']}}
        missing = {key: value for key, value in bound.items() if key != 'decision'}
        for altered, message in ((undispatched, BACKSTOP), (padded, 'stale or altered'), (missing, 'decision')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                harness.validate_binding(store, 'test', altered, ['CCc1ccncc1'], store.progress)

    def test_publication_credit_serves_led_bindings(self):
        store = FakeStore(initial(), [], config=LED)
        bind = harness.validate_proposal(store, 'test', 'local', LEADER, store.progress, decision=NO)
        effect = {'id': 'e1', 'status': 'succeeded', 'accounting_unknown': False, 'packet_hash': 'packet',
                  'metadata': {'adapter_id': 'pablo.evaluate'}}
        publication = {'data': {'effect_id': 'e1', 'packet_sha256': 'packet',
                                'evaluations': [{'candidate': 'CCCCCCCCCCCCCCC', 'score': .5}]}}
        credit = harness.publication_credit(effect, publication, bind)
        self.assertEqual((credit['version'], credit['proposal_role'], credit['accepted_scores']),
                         (harness.LED_VERSION, 'local', 1))


if __name__ == '__main__':
    unittest.main()
