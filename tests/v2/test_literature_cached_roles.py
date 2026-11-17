"""Cached retrieval targets stay distinct from current scientific citations."""
import copy
import json
import unittest

from rsi_lab import images, roles
from rsi_lab.packets import PacketBuilder, make_evidence
from tests.v2.test_image_evidence import content as image_content


VERSIONS = {'issue_version': 0, 'evidence_revision': 0, 'scheduling_revision': 0}


class LiteratureCachedRoleTests(unittest.TestCase):
    def task(self):
        return {'goal': 'Compare two original papers', 'issue_id': None,
                'expected_versions': VERSIONS, 'constraints': {},
                'literature_task': {'id': 'research', 'task_revision': 3, 'capability_versions': {}},
                'reading_state': {'index_is_not_delivery': True}}

    def source(self, eid='original', delivery_kind='source_excerpt', **overrides):
        return make_evidence(eid, 'Reported methods and limitations. ' * 8,
            **{'kind': 'source_statement', 'source': json.dumps({'delivery_kind': delivery_kind}), **overrides})

    def packet(self, rows=(), *, strategy='complete', required=(), final=False):
        task = self.task()
        task['constraints']['final_review_only'] = final
        builder = PacketBuilder({row['evidence_id']: row for row in rows}, page_bytes=8)
        agent = roles.MetaAgent(builder)
        return builder.build(**agent.arguments('literature', task,
            evidence_ids=[row['evidence_id'] for row in rows], required_evidence_ids=list(required)),
            strategy=strategy)

    def command(self, action='read_cached_evidence', payload=None, refs=()):
        if payload is None:
            payload = {'evidence_ids': ['cached-passage'], 'document_ids': [], 'query': '', 'limit': 2}
        return {'command': {'mode': 'literature', 'action': action, 'issue_id': None,
            'expected_versions': VERSIONS, 'evidence_ids': list(refs), 'payload': payload,
            'task_id': 'research', 'task_revision': 3}}

    def validate(self, command, packet=None):
        return roles.validate_command(command, packet or self.packet(), current_versions=VERSIONS)

    def research(self, refs, action='complete_research'):
        return self.command(action, {'answer': 'Source-bound comparison.',
            'coverage_status': 'partial', 'stop_reason': 'Requested passages checked',
            'limitations': ['Limited to the selected passages'],
            'claims': [{'statement': 'The authors report a matched comparison.',
                        'evidence_ids': list(refs), 'limitations': ['As reported']}]}, refs)

    def test_cache_selection_allows_unused_fields_but_not_empty_selection(self):
        for payload in (
                {'evidence_ids': ['cached-passage'], 'document_ids': [], 'query': '', 'limit': 1},
                {'evidence_ids': [], 'document_ids': ['paper-a', 'paper-b'], 'query': '', 'limit': 16},
                {'evidence_ids': [], 'document_ids': [], 'query': 'uncertainty limitations', 'limit': 3}):
            with self.subTest(payload=payload):
                self.assertEqual(self.validate(self.command(payload=payload))['payload'], payload)
        for query in ('', '   '):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, 'requires an evidence ID'):
                self.validate(self.command(payload={'evidence_ids': [], 'document_ids': [], 'query': query, 'limit': 1}))

    def test_cache_request_is_closed_and_bounded(self):
        base = self.command()['command']['payload']
        for patch in ({'limit': 0}, {'limit': 17}, {'limit': True}, {'limit': '2'},
                      {'evidence_ids': ['p' + str(n) for n in range(17)]},
                      {'document_ids': ['doc-' + str(n) for n in range(17)]},
                      {'evidence_ids': ['same', 'same']}, {'evidence_ids': [' ']},
                      {'document_ids': ['same', 'same']}, {'document_ids': ['']},
                      {'unknown': 'field'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.validate(self.command(payload={**base, **patch}))

    def test_cache_targets_do_not_grant_citation_authority(self):
        # Runtime separately checks task ownership of retrieval targets. A
        # payload target need not be currently delivered, but citing it does.
        self.validate(self.command())
        with self.assertRaisesRegex(ValueError, 'not actually present'):
            self.validate(self.command(refs=['cached-passage']))
        packet = self.packet([self.source('cached-passage')], strategy='narrowed')
        self.assertEqual(packet['delivery_pages'], [])
        with self.assertRaisesRegex(ValueError, 'not actually present'):
            self.validate(self.research(['cached-passage']), packet)

    def test_cache_action_is_child_only_and_not_a_final_review_action(self):
        for mode in ('lab', 'discovery', 'disposition'):
            actions = [v['properties']['action']['enum'][0]
                       for v in roles.response_schema(mode)['properties']['command']['anyOf']]
            self.assertNotIn('read_cached_evidence', actions)
        with self.assertRaises(ValueError):
            self.validate(self.command(), self.packet(final=True))

    def test_complete_originals_from_both_papers_can_support_synthesis(self):
        rows = [self.source('paper-a-passage'), self.source('paper-b-passage', 'original')]
        packet = self.packet(rows)
        for action in ('publish_research_update', 'complete_research'):
            with self.subTest(action=action):
                self.assertEqual(self.validate(self.research([r['evidence_id'] for r in rows], action), packet)['action'], action)

    def test_summaries_observations_and_unknown_provenance_cannot_support_claims(self):
        rows = [self.source('observation', 'tool_observation'),
                self.source('summary', 'summary', kind='summary', summary_of=['unread-original']),
                self.source('unmarked', source='fixture-paper'),
                self.source('malformed', source='[]'),
                self.source('hypothesis', kind='hypothesis')]
        for row in rows:
            with self.subTest(eid=row['evidence_id']), self.assertRaisesRegex(ValueError, 'complete original evidence'):
                self.validate(self.research([row['evidence_id']]), self.packet([row]))

    def test_partial_original_delivery_cannot_support_claims(self):
        packet = self.packet([self.source()], strategy='paged')
        self.assertFalse(packet['delivery_pages'][0]['complete_source'])
        with self.assertRaisesRegex(ValueError, 'complete original evidence'):
            self.validate(self.research(['original']), packet)

    def test_source_image_requires_complete_page_and_actual_attachment(self):
        row = self.source('page-image', 'source_image', image=images.image_reference(image_content()))
        packet = self.packet([row])
        self.validate(self.research(['page-image']), packet)
        pointer_only = copy.deepcopy(packet)
        pointer_only['envelope'].pop('image_attachments')
        with self.assertRaisesRegex(ValueError, 'complete original evidence'):
            self.validate(self.research(['page-image']), pointer_only)
        false_image = self.source('false-image', 'source_image')
        with self.assertRaisesRegex(ValueError, 'complete original evidence'):
            self.validate(self.research(['false-image']), self.packet([false_image]))

    def test_access_limits_can_be_reported_without_scientific_claims(self):
        response = self.command('complete_research', {'answer': 'Source access was unavailable.',
            'coverage_status': 'unresolved', 'stop_reason': 'Access failure',
            'limitations': ['No original passage was delivered'], 'claims': []}, ['observation'])
        packet = self.packet([self.source('observation', 'tool_observation')])
        self.validate(response, packet)

    def test_instructions_distinguish_reading_coverage_from_current_sources(self):
        instructions = roles.MetaAgent(PacketBuilder({})).arguments('literature', self.task())['instruction']
        for text in ('task.reading_state', 'read_cached_evidence', 'both papers',
                     'Do not reacquire or reread an entire document', 'not command citations'):
            self.assertIn(text, instructions)


if __name__ == '__main__':
    unittest.main()
