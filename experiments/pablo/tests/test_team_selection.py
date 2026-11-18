"""A Lab-led selection carries the Lab's batch decision; no scorer, model or network call."""
import copy
from dataclasses import replace
from pathlib import Path
import unittest

from rsi_lab import recommendation_contracts
from rsi_lab.store import digest, encode
from pablo_task import adapter, harness, methods, research
from pablo_task.literature import adapter as literature


RUN = 'team-selection'
ROOT = Path('/pablo-team-selection-fixture/run').resolve()
LEADER = harness._canonical('CCCCCCCCCCCCC')
PYRIDINE = harness._canonical('c1ccncc1')
YES = {'challenge_dispatched': True, 'challenge_adopted': True, 'reason': 'The leading series has stalled.'}
NO = {'challenge_dispatched': False, 'challenge_adopted': False, 'reason': 'The last edit still improved.'}
LOCAL = {'run_root': str(ROOT), 'run_id': RUN, 'proposal_role': 'local', 'anchor_parent': LEADER,
         'candidates': ['C' * 14, 'C' * 15, 'C' * 16], 'select_count': 2, 'method': 'knn_ucb_diversity',
         'parameters': {'neighbors': 2, 'exploration_weight': 0.2, 'diversity_weight': 0.1},
         'literature_evidence_ids': ['paper']}
INPUTS = {**LOCAL, 'proposal_role': 'challenge', 'anchor_parent': PYRIDINE,
          'candidates': ['CCc1ccncc1', 'OCc1ccncc1', 'Cc1ccncc1']}
SELECTION = 'selection-effect'


class FakeStore:
    """The reads selection and scoring admission make in a fresh run with no scoring batch yet."""
    def __init__(self, config=harness.LED_DEFAULT_CONFIG):
        self.root = ROOT
        paper = {'provider': literature.PROVIDER, 'topic': 'sample_efficient_search',
                 'query': literature.TOPICS['sample_efficient_search'], 'http_status': 200,
                 'response_complete': True, 'response_sha256': 'a' * 64,
                 'records': [{'provider_source': 'SYNTHETIC', 'publication_id': 'method-fixture',
                              'abstract_text': 'Synthetic method evidence for offline software testing.'}]}
        self.blobs = {'initial': encode({'candidates': [LEADER, PYRIDINE, 'CC'], 'scores': [.9, .88, .3]}),
                      'paper': encode(paper)}
        self.config = {'pablo_run_root': str(ROOT), 'pablo_initial_ref': 'initial', 'max_external': 1,
                       'pablo_max_new_evaluations': 20}
        if config is not None:
            self.config['pablo_harness'] = copy.deepcopy(config)
        self.records = {'fact': [{'id': 'paper', 'kind': 'source_statement', 'content_ref': 'paper',
                                  'provenance': {'adapter_id': literature.ADAPTER_ID}}],
                        'effect': [], 'work': [], 'registration': []}

    def run(self, run_id):
        assert run_id == RUN
        return {'config': self.config}

    def verify_history(self, run_id):
        assert run_id == RUN

    def list(self, run_id, entity_kind, **where):
        assert run_id == RUN
        return [row for row in self.records[entity_kind] if all(row.get(k) == v for k, v in where.items())]

    def get(self, run_id, entity_kind, record_id):
        [row] = self.list(run_id, entity_kind, id=record_id)
        return row

    def read_blob(self, ref):
        return self.blobs[ref]


def settle(store, inputs):
    """Record a succeeded, published selection Effect for these inputs, as the runtime would."""
    packet = methods._packet(store, RUN, inputs)
    raw = encode(packet)
    effect = {'id': SELECTION, 'status': 'succeeded', 'accounting_unknown': False, 'packet_hash': digest(raw),
              'result_ref': 'selection-receipt',
              'metadata': {'adapter_id': methods.ADAPTER_ID, 'adapter_version': methods.VERSION}}
    receipt = {'status': 'succeeded',
               'result': {**methods.select(packet), 'effect_id': SELECTION, 'packet_sha256': digest(raw)}}
    store.blobs['selection-receipt'] = encode(receipt)
    store.blobs['selection-publication'] = encode(methods.publication(effect, receipt))
    store.records['effect'].append(effect)
    store.records['registration'].append({'kind': 'adapter_publication', 'effect_id': SELECTION,
                                          'status': 'complete', 'adapter_id': methods.ADAPTER_ID,
                                          'publication_ref': 'selection-publication'})
    return receipt['result']


def scoring(plan):
    return {'run_root': str(ROOT), 'run_id': RUN, 'candidates': plan['selected_candidates'],
            'selection_effect_id': SELECTION, 'selection_plan_id': plan['plan_id'],
            'proposal_role': plan['proposal_role'], 'anchor_parent': plan['anchor_parent']}


def lab_call(store, receipt):
    store.blobs['lab-receipt'] = encode(receipt)
    return {'id': 'lab-call', 'kind': 'lab', 'status': 'succeeded', 'result_ref': 'lab-receipt'}


class TeamSelectionTests(unittest.TestCase):
    def test_decision_is_not_part_of_a_recommendation(self):
        store = FakeStore()
        with_decision = {**INPUTS, 'decision': YES}
        self.assertEqual(methods.normalize_recommendation(store, RUN, with_decision),
                         methods.normalize_recommendation(store, RUN, INPUTS))
        self.assertNotIn('decision', methods.normalize_recommendation(store, RUN, with_decision))
        # The kernel compares an adopted recommendation with the request through the adapter's normalizer.
        selection = methods.make_adapter()
        self.assertEqual(recommendation_contracts.normalize(selection, store, RUN, with_decision),
                         recommendation_contracts.normalize(selection, store, RUN, INPUTS))
        self.assertEqual(with_decision['decision'], YES)

    def test_selection_inputs_take_an_optional_well_formed_decision(self):
        store = FakeStore()
        for bad in ({**INPUTS, 'decision': {**YES, 'reason': ''}}, {**INPUTS, 'decision': None},
                    {**LOCAL, 'decision': YES}, {**INPUTS, 'decision': {**YES, 'extra': 1}},
                    {**INPUTS, 'extra': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                methods.normalize_recommendation(store, RUN, bad)

    def test_led_request_needs_a_decision_and_other_runs_refuse_one(self):
        led, fixed, plain = FakeStore(), FakeStore(harness.DEFAULT_CONFIG), FakeStore(None)
        with self.assertRaisesRegex(ValueError, "requires the Lab's decision"):
            methods.admit_work(led, RUN, INPUTS)
        methods.admit_work(led, RUN, {**INPUTS, 'decision': YES})
        for store, message in ((fixed, 'takes no Lab decision'), (plain, 'decision')):
            with self.subTest(harness=store.config.get('pablo_harness', {}).get('version')):
                methods.admit_work(store, RUN, LOCAL)
                with self.assertRaisesRegex(ValueError, message):
                    methods.admit_work(store, RUN, {**LOCAL, 'decision': NO})

    def test_plan_binding_carries_the_decision(self):
        store = FakeStore()
        padded = {**YES, 'reason': '  ' + YES['reason'] + ' '}
        packet = methods._packet(store, RUN, {**INPUTS, 'decision': padded})
        self.assertEqual(packet['harness_binding']['decision'], YES)
        self.assertNotIn('decision', packet['inputs'])
        self.assertEqual(packet['inputs'], methods.normalize_recommendation(store, RUN, INPUTS))
        self.assertEqual(methods.select(packet)['harness_binding']['decision'], YES)
        local = methods._packet(store, RUN, {**LOCAL, 'decision': NO})
        self.assertEqual(local['harness_binding']['decision'], NO)
        fixed = FakeStore(harness.DEFAULT_CONFIG)
        self.assertNotIn('decision', methods._packet(fixed, RUN, LOCAL)['harness_binding'])

    def test_scoring_binding_takes_the_decision_stored_with_the_plan(self):
        store = FakeStore()
        plan = settle(store, {**INPUTS, 'decision': YES})
        selected, binding = adapter.selection_binding(store, RUN, scoring(plan))
        self.assertEqual(binding, plan['harness_binding'])
        self.assertEqual(binding['decision'], YES)
        self.assertEqual(selected['decision'], YES)
        for malformed in ({**YES, 'reason': ''}, {**YES, 'challenge_dispatched': 'yes'}, 'yes',
                          {key: value for key, value in YES.items() if key != 'reason'}):
            with self.subTest(decision=malformed), self.assertRaises(ValueError):
                harness.validate_binding(store, RUN, {**binding, 'decision': malformed}, plan['selected_candidates'])
        fixed = FakeStore(harness.DEFAULT_CONFIG)
        plan = settle(fixed, LOCAL)
        selected, binding = adapter.selection_binding(fixed, RUN, scoring(plan))
        self.assertEqual(binding, plan['harness_binding'])
        self.assertNotIn('decision', binding)
        self.assertNotIn('decision', selected)

    def test_dispatch_check_reads_the_requesting_call(self):
        store = FakeStore()
        challenged = lab_call(store, {'response': {}, 'child_threads': [{'thread_id': 't1', 'role': 'challenge'}]})
        with self.assertRaisesRegex(ValueError, 'call record'):
            methods.admit_request(store, RUN, {**INPUTS, 'decision': NO}, challenged)
        self.assertIsNone(methods.admit_request(store, RUN, {**INPUTS, 'decision': YES}, challenged))
        alone = lab_call(store, {'response': {}})
        with self.assertRaisesRegex(ValueError, 'call record'):
            methods.admit_request(store, RUN, {**INPUTS, 'decision': YES}, alone)
        methods.admit_request(store, RUN, {**INPUTS, 'decision': NO}, alone)
        for other in (FakeStore(harness.DEFAULT_CONFIG), FakeStore(None)):
            effect = lab_call(other, {'response': {}, 'child_threads': [{'thread_id': 't1', 'role': 'challenge'}]})
            self.assertIsNone(methods.admit_request(other, RUN, LOCAL, effect))

    def test_only_the_lab_requests_a_led_selection(self):
        store = FakeStore()
        record = {'response': {}, 'child_threads': [{'thread_id': 't1', 'role': 'challenge'}]}
        lab = lab_call(store, record)
        meta = {**lab, 'id': 'meta-call', 'kind': 'meta'}
        with self.assertRaisesRegex(ValueError, 'In a team run only the Lab requests a selection'):
            methods.admit_request(store, RUN, {**INPUTS, 'decision': YES}, meta)
        self.assertIsNone(methods.admit_request(store, RUN, {**INPUTS, 'decision': YES}, lab))
        for other in (FakeStore(harness.DEFAULT_CONFIG), FakeStore(None)):
            self.assertIsNone(methods.admit_request(other, RUN, LOCAL, {**lab_call(other, record), 'kind': 'meta'}))

    def test_only_a_led_registration_carries_the_request_hook(self):
        plain = research.registered_adapters(executable_methods=True)
        led = research.registered_adapters(executable_methods=True, led=True)
        self.assertEqual([a.adapter_id for a in led], [a.adapter_id for a in plain])
        *others, selection = plain
        *led_others, led_selection = led
        self.assertEqual(selection.adapter_id, methods.ADAPTER_ID)
        self.assertIsNone(selection.admit_request)
        self.assertEqual(selection.descriptor(), methods.make_adapter().descriptor())
        self.assertIs(led_selection.admit_request, methods.admit_request)
        self.assertEqual(replace(led_selection, manifest=selection.manifest, admit_request=None), selection)
        self.assertIn('admit_request', led_selection.descriptor()['callbacks'])
        self.assertEqual([a.descriptor() for a in led_others], [a.descriptor() for a in others])
        self.assertEqual([a.descriptor() for a in research.registered_adapters()],
                         [a.descriptor() for a in others])

    def test_a_led_registration_needs_the_executable_selection(self):
        with self.assertRaisesRegex(ValueError, 'executable_methods'):
            research.registered_adapters(led=True)
        with self.assertRaisesRegex(ValueError, 'executable_methods'):
            research.registered_adapters(executable_methods=False, led=True)

    def test_only_a_led_contract_shows_the_decision(self):
        fixed = sorted(['run_root', 'run_id', 'proposal_role', 'anchor_parent', 'candidates', 'select_count',
                        'method', 'parameters', 'literature_evidence_ids'])
        selection = research.registered_adapters(executable_methods=True)[-1]
        led_selection = research.registered_adapters(executable_methods=True, led=True)[-1]
        self.assertEqual(selection.manifest, methods.make_adapter().manifest)
        self.assertEqual(selection.manifest['input_contract']['fields'], fixed)
        self.assertFalse({'decision', 'decision_note'} & set(selection.manifest['input_contract']))
        contract = led_selection.manifest['input_contract']
        self.assertEqual(contract['fields'], sorted(fixed + ['decision']))
        self.assertEqual(set(contract['decision']), set(harness.DECISION_FIELDS))
        self.assertIn('true only with proposal_role challenge', contract['decision']['challenge_adopted'])
        self.assertIn('1 to 300 characters', contract['decision']['reason'])
        self.assertIn("the Lab's own statement", contract['decision_note'])
        self.assertIn('not part of a Meta recommendation', contract['decision_note'])
        self.assertEqual({k: v for k, v in contract.items() if k not in {'fields', 'decision', 'decision_note'}},
                         {k: v for k, v in selection.manifest['input_contract'].items() if k != 'fields'})
        self.assertEqual({k: v for k, v in led_selection.manifest.items() if k != 'input_contract'},
                         {k: v for k, v in selection.manifest.items() if k != 'input_contract'})

    def test_team_guidance_tells_meta_who_decides(self):
        added = (" In a team run the Lab decides each batch's local or challenge role; a recommendation for "
                 "pablo.select_candidates never contains decision.")
        plain, team = research.research_policy(), research.research_policy(team=True)
        self.assertEqual(team['recommendation_guidance'], plain['recommendation_guidance'] + added)
        self.assertEqual({k: v for k, v in team.items() if k != 'recommendation_guidance'},
                         {k: v for k, v in plain.items() if k != 'recommendation_guidance'})


if __name__ == '__main__':
    unittest.main()
