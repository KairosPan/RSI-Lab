"""Preparing a team run: roles, tool switches, limits and the not-blind statement; no scorer, model or network."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rsi_lab import native
from rsi_lab.controller import Controller
from rsi_lab.runtime import Runtime
from rsi_lab.store import Store
from pablo_task import harness, team


def tree(base):
    """checkout/experiments, outside/env/lib/site-packages/guacamol/__init__.py and an empty clean/."""
    checkout, outside, clean = base / 'checkout', base / 'outside', base / 'clean'
    (checkout / 'experiments').mkdir(parents=True)
    package = outside / 'env' / 'lib' / 'site-packages' / 'guacamol'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    clean.mkdir()
    return checkout, outside, clean


class TeamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='pablo-team-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def test_role_text_is_the_harness_text_plus_the_closing(self):
        for role in ('local', 'challenge'):
            self.assertTrue(team.ROLES[role]['instructions'].startswith(harness.ROLE_INSTRUCTIONS[role]))
            self.assertIn('You are a sub-agent of the Lab.', team.ROLES[role]['instructions'])
        self.assertEqual(team.ROLES['local']['description'],
                         'Proposes a batch that refines the leading structural series.')
        self.assertEqual(team.ROLES['challenge']['description'],
                         'Proposes a batch that argues against the leading structural series.')
        closing = (' You are a sub-agent of the Lab. You cannot score or select molecules. Return one anchor from '
                   'the eligible anchors you were given, a pool of candidate SMILES, the hypothesis, and the result '
                   'that would falsify it. Do not predict scores or guess the hidden objective.')
        for role in ('local', 'challenge'):
            self.assertEqual(team.ROLES[role]['instructions'], harness.ROLE_INSTRUCTIONS[role] + closing)

    def test_block_gains_the_two_roles(self):
        block = team.native_block({'lab': {'subagents': True, 'web_search': True}}, protected=[])
        self.assertEqual(sorted(block['lab']['subagents']['roles']), ['challenge', 'local'])
        self.assertEqual(block['lab']['subagents']['roles'], team.ROLES)
        # The kernel reads the result as it reads any run config.
        settings = native.capabilities({'native_capabilities': block})
        self.assertEqual(settings['lab']['subagents']['roles'], team.ROLES)
        self.assertTrue(settings['lab']['web_search'])

    def test_block_keeps_the_files_own_settings_and_roles(self):
        reviewer = {'description': 'Reviews a proposal.', 'instructions': 'Read the proposal and list its weak points.'}
        given = {'lab': {'subagents': {'max_concurrent': 2, 'roles': {'reviewer': reviewer}}},
                 'meta': {'web_search': True}}
        before = copy.deepcopy(given)
        block = team.native_block(given, protected=[])
        self.assertEqual(given, before)
        self.assertEqual(block['meta'], {'web_search': True})
        self.assertEqual(block['lab']['subagents']['max_concurrent'], 2)
        self.assertEqual(block['lab']['subagents']['roles'], {'reviewer': reviewer, **team.ROLES})

    def test_block_refusals(self):
        for bad in ({}, {'lab': {'subagents': False}}, {'lab': {'web_search': True}},
                    {'lab': {'subagents': {'roles': {'local': {'description': 'd', 'instructions': 'i'}}}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                team.native_block(bad, protected=[])

    def test_block_refuses_what_the_kernel_would_refuse(self):
        for bad in ([], {'lab': {'subagents': True, 'sandbox': 'off'}}, {'lab': {'subagents': 'yes'}},
                    {'lab': {'subagents': {'max_concurrent': 9}}},
                    {'lab': {'subagents': {'roles': {'default': {'description': 'd', 'instructions': 'i'}}}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                team.native_block(bad, protected=[])

    def test_roots_that_hand_over_the_objective(self):
        checkout, outside, clean = tree(self.base)
        self.assertIsNone(team.exposes_objective(clean, [checkout]))
        for root in (checkout, checkout / 'experiments', checkout.parent, outside, outside / 'env'):
            with self.subTest(root=root):
                self.assertIsInstance(team.exposes_objective(root, [checkout]), str)
        self.assertIn('guacamol', team.exposes_objective(outside, [checkout]))
        self.assertIn('guacamol', team.exposes_objective(outside / 'env' / 'lib' / 'site-packages' / 'guacamol', []))

    def test_scorer_source_or_a_run_store_under_a_root_is_refused_though_not_listed(self):
        _, _, clean = tree(self.base)
        (clean / 'tools').mkdir()
        (clean / 'tools' / 'oracle.py').write_text('')   # Not in a pablo_task folder.
        self.assertIsNone(team.exposes_objective(clean, []))
        scorer = self.base / 'source-copy' / 'experiments' / 'pablo' / 'pablo_task'
        scorer.mkdir(parents=True)
        (scorer / 'oracle.py').write_text('')
        reason = team.exposes_objective(self.base / 'source-copy', [])
        self.assertIn("holds the scorer's source " + str(scorer / 'oracle.py'), reason)
        earlier = self.base / 'old-runs' / 'earlier-run'
        Store(earlier).close()                          # What the kernel leaves in every run root.
        reason = team.exposes_objective(self.base / 'old-runs', [])
        self.assertIn("holds a run's store: " + str(earlier / team.RUN_STORE), reason)
        self.assertIn('in the run root ' + str(earlier), reason)

    def test_every_entry_named_like_the_package_is_refused(self):
        _, _, clean = tree(self.base)
        for index, (name, folder) in enumerate((('guacamol', True), ('guacamol-0.5.2.dist-info', True),
                                                ('Guacamol.egg-link', False), ('guacamol.zip', False))):
            with self.subTest(name=name):
                root = self.base / ('environment-' + str(index))
                place = root / 'lib' / 'site-packages'
                place.mkdir(parents=True)
                if folder:
                    (place / name).mkdir()
                else:
                    (place / name).write_text('')
                reason = team.exposes_objective(root, [])
                self.assertIn('holds ' + str(place / name) + ', named like the guacamol package', reason)
                if folder:
                    # The folder itself as a readable root is refused by its own name.
                    self.assertIn('is named like the guacamol package', team.exposes_objective(place / name, []))
        # A symlink is refused by its name, and not followed: its target here is clean.
        linked = self.base / 'linked'
        linked.mkdir()
        os.symlink(clean, linked / 'GUACAMOL')
        self.assertIn(str(linked / 'GUACAMOL'), team.exposes_objective(linked, []))
        self.assertIsNone(team.exposes_objective(clean, []))

    def test_a_root_that_cannot_be_checked_in_bounds_is_refused(self):
        _, _, clean = tree(self.base)
        deep = clean.joinpath(*['level'] * 6)
        deep.mkdir(parents=True)
        self.assertIsNone(team.exposes_objective(clean, []))
        (deep / 'level').mkdir()
        self.assertIn('too large to check', team.exposes_objective(clean, []))
        wide = self.base / 'wide'
        for name in ('a', 'b', 'c'):
            (wide / name).mkdir(parents=True)
        self.assertIsNone(team.exposes_objective(wide, []))
        with patch.object(team, 'MAX_FOLDERS', 3):
            self.assertIn('too large to check', team.exposes_objective(wide, []))

    def test_the_search_does_not_follow_symlinks(self):
        _, _, clean = tree(self.base)
        os.symlink(clean, clean / 'loop')
        self.assertIsNone(team.exposes_objective(clean, []))

    def test_block_refuses_an_exposing_readable_root(self):
        checkout, _, clean = tree(self.base)
        exposing = {'lab': {'subagents': True, 'terminal': {'readable_roots': [str(checkout)]}}}
        with self.assertRaisesRegex(ValueError, 'readable_roots'):
            team.native_block(exposing, protected=[checkout])
        accepted = team.native_block({'lab': {'subagents': True, 'terminal': {'readable_roots': [str(clean)]}}},
                                     protected=[checkout])
        self.assertEqual(accepted['lab']['terminal'], {'readable_roots': [str(clean)]})
        # Every role's roots are checked, not only the Lab's.
        with self.assertRaisesRegex(ValueError, 'meta'):
            team.native_block({'lab': {'subagents': True}, 'meta': {'terminal': {'readable_roots': [str(checkout)]}}},
                              protected=[checkout])

    def test_web_search_makes_the_run_not_blind(self):
        self.assertIn('not a blind run', team.not_blind({'lab': {'subagents': True, 'web_search': True}}))
        self.assertIsNone(team.not_blind({'lab': {'subagents': True}}))
        self.assertEqual(team.not_blind({'lab': {'subagents': True, 'web_search': True}}),
                         'Web search is on for the Lab: the hidden objective is protected by instruction only, '
                         'and this run is not a blind run.')
        self.assertEqual(team.NOT_BLIND, team.not_blind({'lab': {'web_search': True}}))

    def test_not_blind_names_every_role_whose_web_search_is_on(self):
        sentence = ('Web search is on for {}: the hidden objective is protected by instruction only, '
                    'and this run is not a blind run.')
        on = {'web_search': True}
        cases = [({'lab': {'subagents': True, **on}}, 'the Lab'), ({'lab': {'subagents': True}, 'meta': on}, 'Meta'),
                 ({'lab': on, 'meta': on}, 'the Lab and Meta'), ({'literature': on, 'lab': on}, 'the Lab and Literature'),
                 ({'lab': on, 'meta': on, 'literature': on}, 'the Lab, Meta and Literature')]
        for block, who in cases:
            with self.subTest(block=block):
                self.assertEqual(team.not_blind(block), sentence.format(who))
        for block in ({}, {'lab': {'subagents': True}, 'meta': {'web_search': False}, 'literature': {}}):
            with self.subTest(block=block):
                self.assertIsNone(team.not_blind(block))
        # The block is read as the kernel reads it, so what the kernel refuses is refused here too.
        for block in ({'meta': {'web_search': 'yes'}}, {'search': on}):
            with self.subTest(block=block), self.assertRaises(ValueError):
                team.not_blind(block)

    def test_goal_states_the_team_in_order(self):
        order = ['local', 'challenge', 'default', 'lead', 'pablo.select_candidates', 'pablo.evaluate',
                 'decision', 'challenge_dispatched', 'challenge_adopted', 'reason', 'required_role',
                 'fork_turns', 'none']
        at = 0
        for word in order:
            found = team.GOAL.find(word, at)
            self.assertGreaterEqual(found, 0, word)
            at = found + len(word)
        self.assertNotIn('\n', team.GOAL)
        self.assertNotIn('increases challenge frequency', team.GOAL)

    def test_preparation_holds_every_team_setting(self):
        block = {'lab': {'subagents': True, 'web_search': True}}
        settings = team.preparation(block, protected=[])
        self.assertEqual(settings, {
            'research_architecture': 'pablo_research_pr4_team_v1',
            'pablo_harness': harness.LED_DEFAULT_CONFIG,
            'native_capabilities': team.native_block(block, protected=[]),
            'max_effective_context_bytes': 4194304, 'prospective_rebuild_history_bytes': 4194304,
            'pablo_not_blind': team.not_blind(block), 'pablo_team_protected_roots': []})
        self.assertEqual(harness.configuration(settings)['version'], harness.LED_VERSION)
        listed = team.preparation(block, protected=[self.base / 'a' / '..' / 'b', self.base / 'b', 'relative'])
        self.assertEqual(listed['pablo_team_protected_roots'], [str(self.base / 'b'), str(Path('relative').resolve())])
        off = team.preparation({'lab': {'subagents': True}}, protected=[], backstop=None,
                               rebuild_history_bytes=262144)
        self.assertIsNone(off['pablo_harness']['challenge_backstop_batches'])
        self.assertEqual(off['prospective_rebuild_history_bytes'], 262144)
        self.assertIn('pablo_not_blind', off)
        self.assertIsNone(off['pablo_not_blind'])
        for changes in ({'backstop': 0}, {'backstop': 101}, {'backstop': True}, {'backstop': 'off'},
                        {'rebuild_history_bytes': 262143}, {'rebuild_history_bytes': 4194304.0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                team.preparation(block, protected=[], **changes)

    def test_session_settings_are_those_the_kernel_sends_with_the_role(self):
        block = team.native_block({'lab': {'subagents': True, 'web_search': True}}, protected=[])
        by_role = native.capabilities({'native_capabilities': block})
        self.assertEqual(team.session_native(by_role, 'lab'), by_role['lab'])
        self.assertIsNone(team.session_native(by_role, 'meta'))
        plain = native.capabilities({})
        for role in native.ROLES:
            self.assertIsNone(team.session_native(plain, role))

    def test_team_module_needs_neither_the_scorer_nor_the_host(self):
        imported = set()
        for node in ast.walk(ast.parse(Path(team.__file__).read_text())):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(((node.module or '') + '.' + alias.name).strip('.') for alias in node.names)
        # research and adapter import the scorer module themselves.
        for name in imported:
            with self.subTest(name=name):
                self.assertFalse({'oracle', 'run', 'research', 'adapter'} & set(name.split('.')))


@unittest.skipUnless(importlib.util.find_spec('rdkit'), 'Optional PABLO RDKit dependency required')
class LedRequestTests(unittest.TestCase):
    """End to end: the led selection adapter in a real Runtime checks the Lab's claim against the call record."""
    def setUp(self):
        from pablo_task import oracle, research
        from pablo_task.literature import adapter as literature
        temporary = tempfile.TemporaryDirectory(prefix='pablo-team-request-')
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name).resolve() / 'run')
        self.addCleanup(self.store.close)
        self.run = 'team-request'
        controller = Controller(self.store)
        initial = self.store.put_blob({'candidates': ['CCCO', 'Cc1ccncc1', 'Cc1ccccc1'], 'scores': [.2, .7, .8]})
        controller.initialize(self.run, max_external=1,
            budgets={key: {'limit': 10000, 'hard': key.endswith('calls')}
                     for key in ('model_calls', 'tokens', 'tool_calls', 'cpu_seconds', 'wall_seconds')},
            config={'execution_mode': 'replay', 'max_open_reviews': 0,
                    'pablo_run_root': str(self.store.root), 'pablo_initial_ref': initial,
                    'pablo_max_new_evaluations': 6, 'pablo_harness': copy.deepcopy(harness.LED_DEFAULT_CONFIG),
                    'literature_run_root': str(self.store.root), 'literature_max_calls': 2})
        self.runtime = Runtime(self.store, self.run,
                               adapters=research.registered_adapters(executable_methods=True, led=True))
        paper = {'provider': literature.PROVIDER, 'topic': 'sample_efficient_search',
                 'query': literature.TOPICS['sample_efficient_search'], 'http_status': 200,
                 'response_complete': True, 'response_sha256': 'c' * 64,
                 'records': [{'provider_source': 'SYNTHETIC', 'publication_id': 'team-request-fixture',
                              'abstract_text': 'Synthetic abstract motivating an offline selection request.'}]}
        controller.add_fact(self.run, 'paper', 'source_statement', paper,
                            {'adapter_id': literature.ADAPTER_ID, 'fixture': True}, 'paper')
        blocked = patch.object(oracle, 'score', side_effect=AssertionError('No scoring in this test'))
        blocked.start()
        self.addCleanup(blocked.stop)

    def request(self, child_threads):
        """One Lab call, scripted, that asks for a challenge selection and says Challenge was dispatched."""
        from experiments.pablo.tests.test_harness_runtime import FakeModel
        from pablo_task import methods
        inputs = {'run_root': str(self.store.root), 'run_id': self.run, 'proposal_role': 'challenge',
                  'anchor_parent': 'Cc1ccncc1', 'candidates': ['CCc1ccncc1', 'CCCc1ccncc1', 'COc1ccncc1'],
                  'select_count': 2, 'method': 'knn_ucb_diversity',
                  'parameters': {'neighbors': 2, 'exploration_weight': .2, 'diversity_weight': .1},
                  'literature_evidence_ids': ['paper'],
                  'decision': {'challenge_dispatched': True, 'challenge_adopted': True,
                               'reason': 'The fixture leading series has stalled.'}}
        model = FakeModel('request_diagnostic', {'capability_id': methods.ADAPTER_ID,
                          'capability_version': methods.VERSION, 'inputs_json': json.dumps(inputs)})

        def call(effect, raw):
            return {**model(effect, raw), 'child_threads': child_threads}
        work = self.runtime.enqueue_lab('team-select', ['paper'])
        outcome = self.runtime.run_lab(work['id'], call, {'model_calls': 1, 'tokens': 10, 'wall_seconds': 1})
        selections = [w for w in self.store.list(self.run, 'work', kind='diagnostic')
                      if w['inputs'].get('capability_id') == methods.ADAPTER_ID]
        return outcome, selections, inputs

    def test_a_claimed_dispatch_without_a_challenge_sub_agent_is_refused(self):
        outcome, selections, _ = self.request([{'thread_id': 'thread-local', 'role': 'local'}])
        self.assertEqual(outcome['proposal']['status'], 'blocked_action', outcome['proposal'])
        self.assertIn('has no sub-agent with role challenge', outcome['proposal']['error'])
        self.assertEqual(selections, [])
        self.assertEqual(json.loads(self.store.read_blob(outcome['effect']['result_ref']))['child_threads'],
                         [{'thread_id': 'thread-local', 'role': 'local'}])
        self.store.verify_history(self.run)

    def test_a_claimed_dispatch_with_a_challenge_sub_agent_is_admitted(self):
        outcome, selections, inputs = self.request([{'thread_id': 'thread-challenge', 'role': 'challenge'}])
        self.assertEqual(outcome['proposal']['status'], 'applied', outcome['proposal'])
        [selection] = selections
        self.assertEqual(selection['status'], 'ready')
        self.assertEqual(json.loads(selection['inputs']['inputs_json'])['decision'], inputs['decision'])
        self.store.verify_history(self.run)


if __name__ == '__main__':
    unittest.main()
