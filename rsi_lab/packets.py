"""Exact bounded role packets and version-bound evidence delivery.

Planning and dispatch use the same builder. Capacity is measured on the complete
application request envelope, including the response schema and transport prefix;
it is not a claim about invisible provider framing or a hard model-token limit.
This module does not write a ledger, infer scientific facts, or make model calls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from . import input_view, transport

KINDS = {'raw_fact', 'source_statement', 'hypothesis', 'validation', 'summary'}
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode('utf-8')).hexdigest()


def make_evidence(evidence_id, content, *, kind, source, version=1, dependencies=(), summary_of=()):
    if not isinstance(evidence_id, str) or not ID.fullmatch(evidence_id):
        raise ValueError('Evidence needs a stable safe identifier')
    if kind not in KINDS or not isinstance(content, str) or not isinstance(source, str) or not source.strip():
        raise ValueError('Evidence needs explicit kind, exact text, and source')
    if type(version) is not int or version < 1:
        raise ValueError('Evidence version must be a positive integer')
    for values in (dependencies, summary_of):
        if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or not ID.fullmatch(v) for v in values):
            raise ValueError('Evidence dependencies must be stable identifiers')
        if len(values) != len(set(values)):
            raise ValueError('Repeated evidence dependencies')
    if kind != 'summary' and summary_of:
        raise ValueError('Only explicitly labelled summaries may claim summary sources')
    if kind == 'summary' and not summary_of:
        raise ValueError('A summary must identify its original evidence without claiming delivery')
    return {'evidence_id': evidence_id, 'version': version, 'kind': kind, 'source': source,
            'content': content, 'content_sha256': digest(content), 'content_bytes': len(content.encode()),
            'dependencies': list(dependencies), 'summary_of': list(summary_of)}


def validate_evidence(row):
    expected = make_evidence(row['evidence_id'], row['content'], kind=row['kind'], source=row['source'],
                            version=row['version'], dependencies=row['dependencies'], summary_of=row['summary_of'])
    if row != expected:
        raise ValueError('Evidence content or immutable provenance differs from its identity')
    return row


def page_evidence(evidence, *, cursor=0, max_content_bytes=4096):
    """Page exact UTF-8 text. Cursors are byte offsets, never summary substitutes."""
    validate_evidence(evidence)
    raw = evidence['content'].encode()
    if type(cursor) is not int or not 0 <= cursor <= len(raw):
        raise ValueError('Invalid evidence page cursor')
    if type(max_content_bytes) is not int or max_content_bytes < 4:
        raise ValueError('Page capacity must fit a UTF-8 codepoint')
    try:
        raw[:cursor].decode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Cursor is not a UTF-8 boundary') from exc
    end = min(len(raw), cursor + max_content_bytes)
    while end > cursor:
        try:
            content = raw[cursor:end].decode('utf-8')
            break
        except UnicodeError:
            end -= 1
    else:
        content = ''
    page_hash = digest(content)
    page_id = f"{evidence['evidence_id']}:{evidence['version']}:{cursor}:{end}:{page_hash[:16]}"
    return {'page_id': page_id, 'evidence_id': evidence['evidence_id'], 'source_version': evidence['version'],
            'source_sha256': evidence['content_sha256'], 'source_kind': evidence['kind'],
            'source': evidence['source'], 'from_cursor': cursor, 'to_cursor': end,
            'total_source_bytes': len(raw), 'content': content, 'content_sha256': page_hash,
            'dependencies': list(evidence['dependencies']), 'summary_of': list(evidence['summary_of']),
            'complete_source': cursor == 0 and end == len(raw)}


def validate_page(page, evidence):
    expected = page_evidence(evidence, cursor=page['from_cursor'],
                             max_content_bytes=max(4, page['to_cursor'] - page['from_cursor']))
    if expected != page:
        raise ValueError('Delivered page differs from exact source bytes/version')


class PacketBuilder:
    def __init__(self, evidence_catalog, max_input_bytes=128 * 1024, *, page_bytes=4096, envelope_schema="rsi_model_request_v2"):
        if type(max_input_bytes) is not int or max_input_bytes < 1 or type(page_bytes) is not int or page_bytes < 4:
            raise ValueError('Packet and page capacities must be positive exact byte limits')
        self.catalog = copy.deepcopy(evidence_catalog)
        if not isinstance(self.catalog, dict):
            raise ValueError('Expected evidence catalog mapping')
        for key, row in self.catalog.items():
            validate_evidence(row)
            if key != row['evidence_id']:
                raise ValueError('Catalog identifier differs from evidence')
        self.max_input_bytes, self.page_bytes = max_input_bytes, page_bytes
        self.envelope_schema = envelope_schema

    def _closure(self, ids):
        found = []
        def visit(eid):
            if eid in found:
                return
            if eid not in self.catalog:
                raise ValueError(f'Unknown required evidence dependency: {eid}')
            found.append(eid)
            for dependency in self.catalog[eid]['dependencies']:
                visit(dependency)
        for eid in ids:
            visit(eid)
        return found

    def build(self, *, mode, instruction, schema, task, evidence_ids=(), required_evidence_ids=(),
              model='gpt-6-astra', effort='medium', strategy='complete', page_cursors=None,
              owner_interpretation=None, prior_review=None):
        """One exact builder shared by planning, effects and final dispatch checks."""
        if mode not in ('lab', 'discovery', 'disposition') or strategy not in ('complete', 'paged', 'deduplicated', 'narrowed'):
            raise ValueError('Unknown role mode or bounded context strategy')
        if not isinstance(instruction, str) or not instruction or not isinstance(schema, dict) or not isinstance(task, dict):
            raise ValueError('Packet requires instructions, response schema and versioned task')
        requested = self._closure(list(dict.fromkeys(evidence_ids)))
        required = self._closure(list(dict.fromkeys(required_evidence_ids)))
        requested = list(dict.fromkeys(required + requested))
        selected = required if strategy == 'narrowed' else requested
        cursors = {} if page_cursors is None else page_cursors
        if not isinstance(cursors, dict) or not set(cursors).issubset(selected):
            raise ValueError('Paging cursor references unrequested evidence')
        pages = []
        for eid in selected:
            row = self.catalog[eid]
            # Required identity/evidence closure is never silently reduced to a page.
            full = eid in required or strategy == 'complete' or strategy == 'narrowed'
            if full and cursors.get(eid, 0) != 0:
                raise ValueError('Required full evidence cannot use a partial cursor')
            pages.append(page_evidence(row, cursor=0 if full else cursors.get(eid, 0),
                                      max_content_bytes=max(4, row['content_bytes']) if full else self.page_bytes))
        metadata = [{key: self.catalog[eid][key] for key in
                     ('evidence_id', 'version', 'kind', 'source', 'content_sha256', 'content_bytes', 'dependencies', 'summary_of')}
                    for eid in requested]
        data = {'mode': mode, 'task': copy.deepcopy(task), 'evidence_catalog': metadata,
                'delivered_pages': pages, 'required_evidence_ids': required,
                'not_delivered_evidence_ids': [eid for eid in requested if eid not in selected],
                'partial_evidence_ids': [p['evidence_id'] for p in pages if not p['complete_source']]}
        if owner_interpretation is not None:
            data['owner_interpretation'] = copy.deepcopy(owner_interpretation)
        if prior_review is not None:
            data['prior_raw_review'] = copy.deepcopy(prior_review)
        presentation = 'lossless_deduplicated_json_v1' if strategy == 'deduplicated' else 'compact_json_v1'
        view = input_view.render(data, presentation)
        render_audit = input_view.audit_render(data, view, presentation)
        body = instruction + '\nINPUT_DATA_JSON:\n' + view
        prompt = transport.PREAMBLE + body
        envelope = {'schema_version': self.envelope_schema, 'mode': mode, 'model': model,
                    'effort': effort, 'prompt': prompt, 'response_schema': copy.deepcopy(schema),
                    'input_view_mode': presentation}
        raw = encoded(envelope)
        fitting = len(raw) <= self.max_input_bytes
        return {'status': 'ready' if fitting else 'blocked_capacity', 'strategy': strategy,
                'envelope': envelope, 'envelope_bytes': raw, 'packet_sha256': digest(raw),
                'prompt_sha256': digest(prompt), 'input_bytes': len(raw), 'max_input_bytes': self.max_input_bytes,
                'delivery_pages': copy.deepcopy(pages), 'required_evidence_ids': required,
                'deferred_evidence_ids': data['not_delivered_evidence_ids'],
                'partial_evidence_ids': data['partial_evidence_ids'], 'task': copy.deepcopy(task),
                'render_audit': render_audit, 'capacity_scope': 'Complete application request bytes; provider framing/token counts not inferred'}

    def plan(self, **arguments):
        """Bounded remedies; never loop, summarize originals, or hide missing data."""
        if 'strategy' in arguments:
            raise ValueError('Planner owns its bounded strategy order')
        attempts = []
        for strategy in ('complete', 'paged', 'deduplicated', 'narrowed'):
            packet = self.build(**arguments, strategy=strategy)
            attempts.append({'strategy': strategy, 'input_bytes': packet['input_bytes'], 'status': packet['status']})
            if packet['status'] == 'ready':
                packet['attempts'] = attempts
                return packet
        return {'status': 'packet_unrepresentable', 'attempts': attempts,
                'required_evidence_ids': packet['required_evidence_ids'], 'max_input_bytes': self.max_input_bytes,
                'next_action': 'narrow_request_or_replan', 'dispatchable': False,
                'reason': 'Required task identity/evidence dependency closure does not fit the complete request'}

    def for_dispatch(self, planned, **arguments):
        if planned.get('status') != 'ready':
            raise ValueError('Unrepresentable or unplanned input cannot be dispatched')
        actual = self.build(**arguments, strategy=planned['strategy'])
        if actual['status'] != 'ready' or actual['envelope_bytes'] != planned['envelope_bytes']:
            raise ValueError('Packet changed between context planning and dispatch')
        return actual


def present_delivery(packet, *, effect_id, sent_packet_sha256, sent_prompt_sha256, receipt_ref):
    """Controller calls only after a verified transport invocation actually sent this packet."""
    if (packet.get('status') != 'ready' or sent_packet_sha256 != packet['packet_sha256']
            or sent_prompt_sha256 != packet['prompt_sha256'] or not effect_id or not receipt_ref):
        raise ValueError('Delivery requires exact dispatched packet, prompt and receipt identities')
    return [{'status': 'presented', 'effect_id': effect_id, 'packet_sha256': sent_packet_sha256,
             'receipt_ref': receipt_ref, **copy.deepcopy(page)} for page in packet['delivery_pages']]


def acknowledge_page(delivery, *, page_id, source_version, content_sha256, raw_source_id=None):
    if (delivery.get('status') != 'presented' or page_id != delivery.get('page_id')
            or type(source_version) is not int or source_version != delivery.get('source_version')
            or content_sha256 != delivery.get('content_sha256')
            or digest(delivery.get('content', '')) != content_sha256):
        raise ValueError('ACK must identify a page actually presented with exact version and bytes')
    if raw_source_id is not None and (raw_source_id != delivery['evidence_id'] or delivery['source_kind'] == 'summary'):
        raise ValueError('A summary, pointer, or another record cannot acknowledge original evidence')
    return {**copy.deepcopy(delivery), 'status': 'acked', 'raw_source_ack': raw_source_id is not None}


def make_question(*, question_id, issue_id, issue_version, question_version, prompt, options, trigger_id):
    if any(not isinstance(v, str) or not v.strip() for v in (question_id, issue_id, prompt, trigger_id)):
        raise ValueError('A user question requires explicit identities and content')
    if any(type(v) is not int or v < 1 for v in (issue_version, question_version)):
        raise ValueError('Question and issue versions must be positive integers')
    if not isinstance(options, list) or any(not isinstance(v, str) or not v.strip() for v in options):
        raise ValueError('Question options must be text; an empty list permits free text')
    return {'question_id': question_id, 'question_version': question_version, 'issue_id': issue_id,
            'issue_version': issue_version, 'prompt': prompt, 'options': options,
            'trigger_id': trigger_id, 'status': 'waiting', 'content_sha256': digest(encoded({'prompt': prompt, 'options': options}))}


def bind_user_reply(question, reply, *, current_issue_version):
    if (question.get('status') != 'waiting' or current_issue_version != question['issue_version']
            or not isinstance(reply, dict) or set(reply) != {'question_id', 'question_version', 'message_id', 'answer', 'source'}
            or reply['source'] != 'user' or reply['question_id'] != question['question_id']
            or type(reply['question_version']) is not int or reply['question_version'] != question['question_version']
            or not isinstance(reply['message_id'], str) or not reply['message_id'].strip()
            or not isinstance(reply['answer'], str) or not reply['answer'].strip()):
        raise ValueError('User reply must bind the still-waiting original question and issue versions')
    return {**copy.deepcopy(question), 'status': 'answered', 'reply': copy.deepcopy(reply),
            'reply_sha256': digest(encoded(reply)), 'producer': 'user', 'scientific_truth_verified': False}
