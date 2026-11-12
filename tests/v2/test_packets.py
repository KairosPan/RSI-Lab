import copy
import unittest

from rsi_lab.packets import (PacketBuilder, acknowledge_page, bind_user_reply, digest,
                                 make_evidence, make_question, page_evidence, present_delivery,
                                 validate_page)


def fact(eid='raw', text='Actual tool observation', **kwargs):
    return make_evidence(eid, text, kind='raw_fact', source='immutable worker receipt', **kwargs)


class PacketTests(unittest.TestCase):
    def arguments(self, **changes):
        return {'mode': 'disposition', 'instruction': 'Read delivered evidence only.',
                'schema': {'type': 'object'}, 'task': {'goal': 'Validate supplied records', 'identity': 'targetA'},
                'evidence_ids': ['raw'], 'required_evidence_ids': ['raw'], **changes}

    def test_plan_and_final_dispatch_are_identical_complete_inputs(self):
        builder = PacketBuilder({'raw': fact()})
        args = self.arguments()
        planned = builder.plan(**args)
        final = builder.for_dispatch(planned, **args)
        self.assertEqual(planned['envelope_bytes'], final['envelope_bytes'])
        self.assertEqual(planned['input_bytes'], len(planned['envelope_bytes']))
        self.assertEqual(planned['packet_sha256'], digest(planned['envelope_bytes']))
        self.assertIn('Return only one JSON object', planned['envelope']['prompt'])
        self.assertEqual(planned['envelope']['response_schema'], args['schema'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            builder.for_dispatch(planned, **{**args, 'task': {'identity': 'other target'}})

    def test_changed_capacity_before_dispatch_does_not_send(self):
        builder = PacketBuilder({'raw': fact()})
        args = self.arguments()
        planned = builder.plan(**args)
        builder.max_input_bytes = planned['input_bytes'] - 1
        with self.assertRaises(ValueError):
            builder.for_dispatch(planned, **args)

    def test_full_prompt_schema_and_escaping_count_toward_capacity(self):
        builder = PacketBuilder({'raw': fact(text='"\\' * 100)}, max_input_bytes=500)
        result = builder.plan(**self.arguments(schema={'description': 'x' * 700, 'type': 'object'}))
        self.assertEqual(result['status'], 'packet_unrepresentable')
        self.assertNotIn('envelope_bytes', result)
        self.assertEqual(len(result['attempts']), 4)
        self.assertTrue(all(x['input_bytes'] > 500 for x in result['attempts']))

    def test_optional_evidence_can_be_paged_but_is_explicitly_partial(self):
        builder = PacketBuilder({'raw': fact(), 'long': fact('long', 'x' * 20000)}, max_input_bytes=5000, page_bytes=128)
        result = builder.plan(**self.arguments(evidence_ids=['raw', 'long']))
        self.assertEqual((result['status'], result['strategy']), ('ready', 'paged'))
        self.assertEqual(result['partial_evidence_ids'], ['long'])
        long_page = next(p for p in result['delivery_pages'] if p['evidence_id'] == 'long')
        self.assertEqual(len(long_page['content']), 128)
        self.assertFalse(long_page['complete_source'])

    def test_required_dependency_is_not_silently_paged_or_removed(self):
        builder = PacketBuilder({'raw': fact(dependencies=['dependency']),
                                 'dependency': fact('dependency', 'x' * 20000)}, max_input_bytes=5000, page_bytes=64)
        result = builder.plan(**self.arguments())
        self.assertEqual(result['status'], 'packet_unrepresentable')
        self.assertEqual(result['required_evidence_ids'], ['raw', 'dependency'])
        self.assertFalse(result['dispatchable'])

    def test_lossless_deduplication_is_an_actual_bounded_remedy(self):
        task = {'repeated': [{'x': list(range(100))}] * 60}
        builder = PacketBuilder({'raw': fact()}, max_input_bytes=100000)
        args = self.arguments(task=task)
        compact = builder.build(**args)
        dedup = builder.build(**args, strategy='deduplicated')
        self.assertLess(dedup['input_bytes'], compact['input_bytes'])
        builder.max_input_bytes = dedup['input_bytes']
        planned = builder.plan(**args)
        self.assertEqual(planned['strategy'], 'deduplicated')
        self.assertTrue(planned['render_audit']['ordered_roundtrip_equal'])

    def test_final_narrowing_defers_optional_evidence(self):
        builder = PacketBuilder({'raw': fact(), 'long': fact('long', 'z' * 20000)}, max_input_bytes=100000, page_bytes=4000)
        args = self.arguments(evidence_ids=['raw', 'long'])
        narrow = builder.build(**args, strategy='narrowed')
        builder.max_input_bytes = narrow['input_bytes']
        result = builder.plan(**args)
        self.assertEqual(result['strategy'], 'narrowed')
        self.assertEqual(result['deferred_evidence_ids'], ['long'])
        self.assertEqual([p['evidence_id'] for p in result['delivery_pages']], ['raw'])

    def test_utf8_pages_reconstruct_original_and_reject_invalid_cursor(self):
        evidence = fact(text='数据🙂 and Δ')
        cursor, parts = 0, []
        while cursor < evidence['content_bytes']:
            page = page_evidence(evidence, cursor=cursor, max_content_bytes=5)
            validate_page(page, evidence)
            parts.append(page['content'])
            cursor = page['to_cursor']
        self.assertEqual(''.join(parts), evidence['content'])
        with self.assertRaisesRegex(ValueError, 'boundary'):
            page_evidence(evidence, cursor=1)

    def test_mutated_evidence_cannot_keep_its_original_hash(self):
        row = fact()
        row['content'] = 'made up outcome'
        with self.assertRaises(ValueError):
            PacketBuilder({'raw': row})
        with self.assertRaisesRegex(ValueError, 'Unknown required'):
            PacketBuilder({'raw': fact(dependencies=['absent'])}).plan(**self.arguments())


class DeliveryAndReplyTests(unittest.TestCase):
    def test_ack_requires_actual_presentation_version_and_page_hash(self):
        packet = PacketBuilder({'raw': fact()}).plan(mode='disposition', instruction='Review.',
            schema={'type': 'object'}, task={}, evidence_ids=['raw'], required_evidence_ids=['raw'])
        page = packet['delivery_pages'][0]
        with self.assertRaises(ValueError):
            acknowledge_page(page, page_id=page['page_id'], source_version=1, content_sha256=page['content_sha256'])
        delivery = present_delivery(packet, effect_id='effect1', sent_packet_sha256=packet['packet_sha256'],
            sent_prompt_sha256=packet['prompt_sha256'], receipt_ref='immutable-receipt')[0]
        ack = acknowledge_page(delivery, page_id=page['page_id'], source_version=1,
                               content_sha256=page['content_sha256'], raw_source_id='raw')
        self.assertEqual(ack['status'], 'acked')
        with self.assertRaises(ValueError):
            acknowledge_page(delivery, page_id=page['page_id'], source_version=2, content_sha256=page['content_sha256'])
        with self.assertRaises(ValueError):
            present_delivery(packet, effect_id='effect1', sent_packet_sha256='other',
                             sent_prompt_sha256=packet['prompt_sha256'], receipt_ref='receipt')

    def test_summary_ack_never_acknowledges_original(self):
        summary = make_evidence('summary', 'Shorter interpretation', kind='summary', source='model summary', summary_of=['original'])
        packet = PacketBuilder({'summary': summary}).plan(mode='disposition', instruction='Review.',
            schema={'type': 'object'}, task={}, evidence_ids=['summary'])
        page = present_delivery(packet, effect_id='effect1', sent_packet_sha256=packet['packet_sha256'],
            sent_prompt_sha256=packet['prompt_sha256'], receipt_ref='receipt')[0]
        for raw_source in ('original', 'summary'):
            with self.assertRaisesRegex(ValueError, 'summary'):
                acknowledge_page(page, page_id=page['page_id'], source_version=1,
                                 content_sha256=page['content_sha256'], raw_source_id=raw_source)
        self.assertEqual(acknowledge_page(page, page_id=page['page_id'], source_version=1,
                         content_sha256=page['content_sha256'])['status'], 'acked')

    def test_reply_binds_original_question_and_issue_version_without_guessing(self):
        question = make_question(question_id='q1', issue_id='i1', issue_version=4, question_version=2,
                                 prompt='Exploratory conditions or exact procedure reproduction?', options=['explore', 'reproduce'], trigger_id='e5')
        reply = {'question_id': 'q1', 'question_version': 2, 'message_id': 'user-message-1', 'answer': 'explore', 'source': 'user'}
        answered = bind_user_reply(question, reply, current_issue_version=4)
        self.assertEqual(answered['status'], 'answered')
        self.assertFalse(answered['scientific_truth_verified'])
        for bad in ({**reply, 'answer': ''}, {**reply, 'source': 'model'}, {**reply, 'question_version': 1},
                    {**reply, 'question_id': 'q2'}):
            with self.assertRaises(ValueError):
                bind_user_reply(question, bad, current_issue_version=4)
        with self.assertRaises(ValueError):
            bind_user_reply(question, reply, current_issue_version=5)
        with self.assertRaises(ValueError):
            bind_user_reply(answered, reply, current_issue_version=4)


if __name__ == '__main__':
    unittest.main()
