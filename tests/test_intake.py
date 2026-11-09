"""Offline intake state/permission tests. No real model or scientific calls."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_harness import intake


def finding(requirement='Input provenance', status='known', scope='exploratory_required', refs=None,
            kind='current_prerequisite', stage=None, guard=''):
    return {'requirement': requirement, 'status': status, 'scope': scope,
            'requirement_type': kind, 'stage': stage or intake.DEFERRED_TYPES.get(kind, 'before_launch'), 'guard_id': guard,
            'evidence_ids': ['source'] if refs is None else refs,
            'rationale': 'This condition determines whether the requested scope is interpretable.',
            'resolution': 'Documented scope accepted.' if status == 'known' else 'Resolve with evidence before dependent work.'}


def owner(action, findings=(), artifacts=(), questions=()):
    return {'action': action, 'artifact_ids': list(artifacts), 'findings': list(findings),
            'questions': list(questions), 'scientific_limitations': ['Exploratory computational scope only.']}


def inspector(verdict='ready_for_exploratory', findings=(), questions=()):
    return {'verdict': verdict, 'findings': list(findings), 'questions': list(questions),
            'scientific_limitations': ['No formal-study qualification.']}


def receipt(response, complete=True):
    return {'response': copy.deepcopy(response), 'usage': {'input_tokens': 10, 'output_tokens': 5} if complete else None,
            'usage_complete': complete, 'error': None, 'wall_seconds': 0.01, 'returncode': 0, 'backend': 'codex'}


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source.json'
        self.source.write_text('{"scope":"exploration","private_marker":"delivered-only-after-read"}\n')
        self.out = self.base / 'workspace'
        self.artifacts = {'source': {'path': self.source, 'title': 'Input provenance'}}

    def run_mock(self, responses, max_calls=5, **policy):
        prompts = []

        def invoke(prompt, schema, folder, model, effort):
            state = json.loads((self.out / 'intake/state.json').read_text())
            self.assertEqual(state['calls'][-1]['status'], 'reserved')
            self.assertEqual(len(state['calls']), len(prompts) + 1)
            self.assertTrue((folder / 'input.json').is_file())
            prompts.append(prompt)
            response = responses[len(prompts) - 1]
            return copy.deepcopy(response) if 'usage_complete' in response else receipt(response)

        with patch('lab_harness.intake.transport.invoke', side_effect=invoke) as mocked:
            result = intake.run_intake(goal='Run an isolated live exploratory agentic cycle.',
                artifacts=self.artifacts, out=self.out, max_calls=max_calls, **policy)
        return result, prompts, mocked

    def test_reserved_owner_read_then_independent_inspector_and_exact_evidence(self):
        formal = finding('Future formal criterion', 'missing', 'formal_only', refs=[])
        result, prompts, mocked = self.run_mock([
            owner('inspect', artifacts=['source']), owner('conclude', [finding(), formal]), inspector()])
        self.assertEqual(mocked.call_count, 3)
        self.assertNotIn('delivered-only-after-read', prompts[0])
        self.assertIn('delivered-only-after-read', prompts[1])
        self.assertIn('Independent Inspector', prompts[2])
        self.assertEqual(result['status'], 'ready_for_exploratory')
        self.assertFalse(result['formal_readiness'])
        self.assertEqual((self.out / 'intake/evidence/source.txt').read_bytes(), self.source.read_bytes())
        self.assertEqual([x['role'] for x in result['calls']], ['owner', 'owner', 'inspector'])
        self.assertEqual(json.loads((self.out / 'intake/result.json').read_text()), result)

    def test_catalog_only_citation_rejected_without_inspector_or_implicit_read(self):
        result, _, mocked = self.run_mock([owner('conclude', [finding()])])
        self.assertEqual(result['status'], 'not_ready')
        self.assertIn('not actually delivered', result['reason'])
        self.assertEqual(mocked.call_count, 1)
        self.assertFalse(list((self.out / 'intake/evidence').iterdir()))
        self.assertTrue((self.out / 'intake/calls/001_owner/receipt.json').is_file())

    def test_unknown_usage_preserved_and_never_retried(self):
        result, _, mocked = self.run_mock([receipt(owner('inspect', artifacts=['source']), complete=False)])
        self.assertEqual(result['status'], 'needs_audit')
        self.assertEqual(mocked.call_count, 1)
        self.assertIsNone(result['calls'][0]['usage'])
        self.assertEqual(result['calls'][0]['status'], 'needs_audit')
        self.assertFalse(result['read_receipts'])

    def test_question_returns_to_user_without_inferred_answer_and_no_resume(self):
        question = 'The allowed scope is unclear. Permit exploratory use, or defer execution?'
        result, _, mocked = self.run_mock([owner('ask_user', questions=[question])])
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['questions'], [question])
        self.assertEqual(mocked.call_count, 1)
        with patch('lab_harness.intake.transport.invoke') as again:
            with self.assertRaises(FileExistsError):
                intake.run_intake(goal='Same goal', artifacts=self.artifacts, out=self.out)
            again.assert_not_called()

    def test_question_repeating_delivered_ids_is_recorded_noop_with_raw_receipt_preserved(self):
        question = 'Choose the intended study scope before execution.'
        unresolved = finding('Study scope choice', 'missing', refs=['source'])
        result, _, mocked = self.run_mock([owner('inspect', artifacts=['source']),
            owner('ask_user', [unresolved], ['source', 'source'], [question])])
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['questions'], [question])
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(result['read_receipts']), 1)
        self.assertEqual(result['owner']['artifact_ids'], [])
        self.assertIn(unresolved, result['findings'])
        self.assertFalse(result['formal_readiness'])
        normalization = result['normalizations'][0]
        self.assertEqual(normalization['call_number'], 2)
        self.assertEqual(normalization['artifact_ids'], ['source', 'source'])
        self.assertEqual(normalization['evidence_hashes'], result['evidence_hashes'])
        raw = json.loads((self.out / 'intake/calls/002_owner/receipt.json').read_text())
        self.assertEqual(raw['response']['artifact_ids'], ['source', 'source'])

    def test_question_mixing_delivered_and_unseen_ids_fails_without_read_or_citation(self):
        unseen = self.base / 'unseen.txt'
        unseen.write_text('This content has never been delivered.')
        self.artifacts['unseen'] = {'path': unseen, 'title': 'Additional evidence'}
        result, prompts, mocked = self.run_mock([owner('inspect', artifacts=['source']),
            owner('ask_user', [finding('Scope decision', 'missing', refs=['source'])],
                  ['source', 'unseen'], ['Which scope is intended?'])])
        self.assertEqual(result['status'], 'not_ready')
        self.assertIn('already be delivered', result['reason'])
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(len(result['read_receipts']), 1)
        self.assertEqual(set(result['evidence_hashes']), {'source'})
        self.assertFalse(result['normalizations'])
        self.assertFalse((self.out / 'intake/evidence/unseen.txt').exists())
        self.assertNotIn('This content has never been delivered.', prompts[-1])

    def test_unresolved_discovery_cannot_disappear_from_later_owner_turn(self):
        unresolved = finding('Unverified essential condition', 'missing', refs=[])
        result, _, _ = self.run_mock([owner('inspect', [unresolved], ['source']),
                                      owner('conclude', [finding()]), inspector()])
        self.assertEqual(result['status'], 'not_ready')
        self.assertIn(unresolved, result['owner']['findings'])
        self.assertIn('Unresolved', result['reason'])

    def test_inspector_can_discover_capability_gap_and_runtime_blocks_false_ready(self):
        gap = finding('Required unavailable operation', 'unsupported', refs=['source'])
        result, _, _ = self.run_mock([owner('inspect', artifacts=['source']),
                                      owner('conclude', [finding()]), inspector(findings=[gap])])
        self.assertEqual(result['status'], 'needs_capability')
        self.assertFalse(result['formal_readiness'])

    def test_oversized_source_is_not_truncated_or_granted_citation_permission(self):
        self.source.write_text('x' * (intake.ARTIFACT_LIMIT + 1))
        gap = finding('Evidence delivery', 'unsupported', refs=[])
        result, prompts, _ = self.run_mock([owner('inspect', artifacts=['source']),
            owner('conclude', [gap]), inspector('needs_capability')])
        self.assertEqual(result['read_receipts'][0]['status'], 'blocked_capacity')
        self.assertFalse(result['evidence_hashes'])
        self.assertNotIn('x' * 100, prompts[-1])
        self.assertEqual(result['status'], 'needs_capability')

    def test_inspection_budget_cannot_consume_inspector_reserved_slot(self):
        result, _, mocked = self.run_mock([owner('inspect', artifacts=['source'])], max_calls=2)
        self.assertEqual(result['status'], 'not_ready')
        self.assertIn('budget exhausted', result['reason'])
        self.assertEqual(mocked.call_count, 1)
        self.assertIsNone(result['inspector'])

    def test_unknown_artifact_and_inspector_input_without_questions_fail_closed(self):
        result, _, mocked = self.run_mock([owner('inspect', artifacts=['../../private'])])
        self.assertEqual(result['status'], 'not_ready')
        self.assertEqual(mocked.call_count, 1)
        self.assertFalse(result['read_receipts'])
        self.out = self.base / 'second'
        result, _, _ = self.run_mock([owner('inspect', artifacts=['source']),
            owner('conclude', [finding()]), inspector('needs_input')])
        self.assertEqual(result['status'], 'not_ready')
        self.assertIn('requires explicit questions', result['reason'])

    def guard(self, kind='campaign_registration'):
        return {'registered': {'requirement_type': kind, 'stage': intake.DEFERRED_TYPES[kind],
                              'checkpoint': 'controller.init/assert_frozen',
                              'implementation': {'source': hashlib.sha256(self.source.read_bytes()).hexdigest()}}}

    def guarded_result(self, **policy):
        future = finding('Freeze campaign before scientific dispatch', 'missing',
                         kind='campaign_registration', guard='registered')
        result, _, _ = self.run_mock([owner('inspect', artifacts=['source']),
                                     owner('conclude', [finding(), future]), inspector()],
                                    guards=self.guard(), **policy)
        return result

    def test_exact_inspected_guard_allows_launch_but_future_outcome_stays_missing(self):
        result = self.guarded_result()
        self.assertEqual(result['status'], 'ready_for_exploratory')
        self.assertFalse(result['launch_blockers'])
        self.assertEqual(len(result['deferred_obligations']), 1)
        obligation = result['deferred_obligations'][0]
        self.assertEqual(obligation['stage'], 'before_docking')
        self.assertEqual(obligation['finding']['status'], 'missing')
        self.assertFalse(obligation['outcome_passed'])
        self.assertEqual(obligation['status'], 'unresolved_guarded')
        raw = json.loads((self.out / 'intake/calls/002_owner/receipt.json').read_text())
        self.assertEqual(raw['response']['findings'][1], obligation['finding'])
        intake.assert_guard_implementations(result, artifacts=self.artifacts,
                                           checkpoints={'controller.init/assert_frozen'}, stage='before_docking')

    def test_invented_guard_wrong_type_or_wrong_hash_never_defers(self):
        for number, patch in enumerate(({'guard_id': 'imagined'}, {'requirement_type': 'validation_admission',
                                                               'stage': 'before_validation'}, {})):
            with self.subTest(case=number):
                self.out = self.base / str(number)
                future = finding('Future prerequisite', 'missing', kind='campaign_registration', guard='registered')
                future.update(patch)
                guards = self.guard()
                if not patch:
                    guards['registered']['implementation']['source'] = '0' * 64
                result, _, _ = self.run_mock([owner('inspect', artifacts=['source']),
                    owner('conclude', [future]), inspector()], guards=guards)
                self.assertEqual(result['status'], 'not_ready')
                self.assertFalse(result['deferred_obligations'])
                self.assertTrue(result['launch_blockers'])

    def test_registered_but_undelivered_guard_source_cannot_admit(self):
        extra = self.base / 'guard.py'
        extra.write_text('def real_check():\n    raise ValueError("test fixture only")\n')
        self.artifacts['implementation'] = {'path': extra, 'title': 'Guard implementation'}
        guards = self.guard()
        guards['registered']['implementation'] = {'implementation': hashlib.sha256(extra.read_bytes()).hexdigest()}
        future = finding('Future prerequisite', 'missing', kind='campaign_registration', guard='registered')
        result, _, _ = self.run_mock([owner('inspect', artifacts=['source']),
            owner('conclude', [future]), inspector()], guards=guards)
        self.assertEqual(result['status'], 'not_ready')
        self.assertNotIn('implementation', result['evidence_hashes'])

    def test_current_discovery_cannot_be_moved_to_a_later_guard(self):
        original = finding('Actual ligand preparation prerequisite', 'missing', refs=[])
        moved = finding(original['requirement'], 'missing', kind='campaign_registration', guard='registered')
        result, _, mocked = self.run_mock([owner('inspect', [original], ['source']),
            owner('conclude', [moved])], guards=self.guard())
        self.assertEqual(result['status'], 'not_ready')
        self.assertIn('cannot move', result['reason'])
        self.assertEqual(mocked.call_count, 2)

    def test_required_ph_preparation_omission_or_scope_downgrade_blocks_launch(self):
        required = {'ligand_state_preparation': {'description': 'User required pH/state preparation before launch',
                    'evidence': {'source': hashlib.sha256(self.source.read_bytes()).hexdigest()}}}
        for number, findings in enumerate(([finding()],
                [finding('pH preparation', 'missing', 'optional', kind='ligand_state_preparation')],
                [finding('pH preparation', 'known', 'formal_only', kind='ligand_state_preparation')])):
            with self.subTest(case=number):
                self.out = self.base / f'ph_{number}'
                result, _, _ = self.run_mock([owner('inspect', artifacts=['source']),
                    owner('conclude', findings), inspector()], required_before_launch=required)
                self.assertEqual(result['status'], 'not_ready')
                self.assertTrue(result['launch_blockers'])
        self.out = self.base / 'ph_resolved'
        result, _, _ = self.run_mock([owner('inspect', artifacts=['source']),
            owner('conclude', [finding('pH preparation', kind='ligand_state_preparation')]), inspector()],
            required_before_launch=required)
        self.assertEqual(result['status'], 'ready_for_exploratory')

    def test_required_ph_evidence_must_match_pins_and_cannot_be_deferred(self):
        required = {'ligand_state_preparation': {'description': 'User-required pH preparation',
                    'evidence': {'source': '0' * 64}}}
        result, _, _ = self.run_mock([owner('inspect', artifacts=['source']),
            owner('conclude', [finding('pH preparation', kind='ligand_state_preparation')]), inspector()],
            required_before_launch=required)
        self.assertEqual(result['status'], 'not_ready')
        self.out = self.base / 'ph_late'
        moved = finding('pH preparation', 'missing', kind='ligand_state_preparation', stage='before_docking', guard='registered')
        result, _, _ = self.run_mock([owner('inspect', artifacts=['source']), owner('conclude', [moved])],
                                     guards=self.guard(), required_before_launch=required)
        self.assertEqual(result['status'], 'not_ready')
        self.assertIn('different dependent stage', result['reason'])

    def test_guard_registry_cannot_invent_deferrable_requirement_type(self):
        guards = self.guard()
        guards['registered']['requirement_type'] = 'ligand_state_preparation'
        with patch('lab_harness.intake.transport.invoke') as invoke:
            with self.assertRaisesRegex(ValueError, 'cannot defer'):
                intake.run_intake(goal='Test', artifacts=self.artifacts, out=self.out, guards=guards)
            invoke.assert_not_called()

    def test_selected_policy_handoff_cannot_close_at_promotion_boundary(self):
        guards = self.guard('selected_policy_handoff')
        guards['registered']['checkpoint'] = 'selected_policy.verify'
        guards['registered']['stage'] = 'before_promotion'
        with patch('lab_harness.intake.transport.invoke') as invoke:
            with self.assertRaisesRegex(ValueError, 'change its boundary'):
                intake.run_intake(goal='Test', artifacts=self.artifacts, out=self.out, guards=guards)
            invoke.assert_not_called()

    def test_guard_sources_and_wiring_are_checked_again_at_runtime(self):
        result = self.guarded_result()
        with self.assertRaisesRegex(ValueError, 'wired'):
            intake.assert_guard_implementations(result, artifacts=self.artifacts, checkpoints=set())
        self.source.write_text('changed after intake')
        with self.assertRaisesRegex(ValueError, 'changed'):
            intake.assert_guard_implementations(result, artifacts=self.artifacts,
                                               checkpoints={'controller.init/assert_frozen'})

    def test_stage_closure_requires_all_exact_runtime_receipts_and_unchanged_evidence(self):
        result = self.guarded_result()
        evidence = self.base / 'frozen_campaign.json'
        evidence.write_text('{"software_test_only":true}\n')
        guard = result['guard_registry']['registered']
        receipt = {'guard_id': 'registered', 'stage': 'before_docking', 'checkpoint': guard['checkpoint'],
                   'status': 'passed', 'guard_registry_sha256': result['guard_registry_sha256'],
                   'implementation': guard['implementation'],
                   'evidence': {str(evidence.resolve()): hashlib.sha256(evidence.read_bytes()).hexdigest()}}
        intake.assert_stage_ready(result, stage='before_docking', guard_receipts=[receipt])
        for receipts in ([], [receipt, receipt], [{**receipt, 'status': 'pending'}],
                         [{**receipt, 'stage': 'before_promotion'}], [{**receipt, 'evidence': {}}],
                         [{**receipt, 'implementation': {'source': '0' * 64}}]):
            with self.subTest(receipts=receipts), self.assertRaises(ValueError):
                intake.assert_stage_ready(result, stage='before_docking', guard_receipts=receipts)
        evidence.write_text('{"changed":true}\n')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            intake.assert_stage_ready(result, stage='before_docking', guard_receipts=[receipt])
        with self.assertRaisesRegex(ValueError, 'not formal'):
            intake.assert_stage_ready(result, stage='formal', guard_receipts=[])


if __name__ == '__main__':
    unittest.main()
