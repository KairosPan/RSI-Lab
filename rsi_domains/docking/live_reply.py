"""Bind an actual user message and deliver its original fact to dependent issues.

The host must supply the user's actual text and stable message ID explicitly.
This module never chooses an option, infers consent from silence, synthesizes an
answer, changes a question, or reopens a closed issue. The controller remains the
sole writer of the original ``source: user_reply`` fact.
"""
from __future__ import annotations

import json
import time

from rsi_lab.controller import identity
from rsi_lab.store import digest

ACTIVE_WAITS = {'waiting', 'disposition_due', 'disposed'}


def bind_and_resume(runtime, *, question_id, question_version, text, message_id):
    """Deliver one explicit message to all open issues waiting on this question.

    Question/version, exact text and message identity are immutable. A crash at
    any boundary can resume with the same arguments. Replays never resubmit a
    reply or advance time to produce a new wait cycle. No model/tool is invoked.
    """
    if (not isinstance(question_id, str) or not question_id.strip()
            or type(question_version) is not int or question_version < 1
            or not isinstance(message_id, str) or not message_id.strip()
            or not isinstance(text, str) or not text.strip()):
        raise ValueError('Actual nonempty reply text, message ID and original question/version are required')
    store, run, controller = runtime.store, runtime.run_id, runtime.controller
    store.verify_history(run)
    registration_id = identity('explicit_user_message', message_id)
    key = 'live-reply:' + registration_id
    payload = {'question_id': question_id, 'question_version': question_version,
               'text': text, 'message_id': message_id}

    def register(tx):
        question = tx.get('question', question_id)
        owner = tx.get('issue', question['issue_id'])
        dep = tx.get('dependency', question['dependency_id'])
        if (question['status'] != 'pending' or question['version'] != question_version
                or owner['status'] != 'open' or dep['producer'] != 'user'
                or owner['evidence_revision'] != question['issue_evidence_revision']
                or dep['revision'] != question['dependency_revision']):
            raise ValueError('Actual reply is not bound to the original open question and evidence/dependency version')
        waits = [wait for wait in tx.find('wait', dependency_id=dep['id'])
                 if wait['status'] in ACTIVE_WAITS and wait['producer'] == 'user'
                 and wait['observed_version'] == question['dependency_revision']
                 and tx.get('issue', wait['issue_id'])['status'] == 'open']
        return tx.insert('registration', registration_id, {'kind': 'explicit_user_reply_delivery',
            'status': 'registered', 'message_id': message_id, 'question_id': question_id,
            'question_version': question_version, 'dependency_id': dep['id'],
            'message_ref': tx.blob(payload), 'text_sha256': digest(text.encode()),
            'target_issue_ids': sorted({owner['id'], *[wait['issue_id'] for wait in waits]}),
            'target_wait_ids': sorted(wait['id'] for wait in waits),
            'tick_at': time.time(), 'result_ref': None})

    store.mutate(run, key + ':register', 'explicit_user_reply_registered', register, payload=payload)
    registration = store.get(run, 'registration', registration_id)
    if registration['status'] == 'complete':
        return json.loads(store.read_blob(registration['result_ref']))
    # This controller method alone creates the authoritative original fact.
    # Its idempotency key retains the exact original question version on retry.
    controller.reply(run, question_id, question_version, text, key + ':bind')
    fact_id = identity('reply', question_id)

    def attach(tx):
        question = tx.get('question', question_id)
        fact = tx.get('fact', fact_id)
        original = json.loads(store.read_blob(fact['content_ref']))
        expected = {'source': 'user_reply', 'question_id': question_id,
                    'question_version': question_version, 'text': text}
        if (original != expected or fact['kind'] != 'source_statement'
                or fact['provenance'] != {'question_id': question_id, 'question_version': question_version}
                or question['status'] != 'answered' or question['reply_ref'] != fact['content_ref']):
            raise ValueError('Original controller reply fact differs from the explicit user message')
        attached, closed = [], []
        for iid in registration['target_issue_ids']:
            issue = tx.get('issue', iid)
            if issue['status'] != 'open':
                closed.append(iid)
                continue
            if fact_id not in issue['evidence_ids']:
                tx.update('issue', iid, {'evidence_ids': issue['evidence_ids'] + [fact_id],
                                        'evidence_revision': issue['evidence_revision'] + 1})
            attached.append(iid)
        tx.update('registration', registration_id, {'status': 'attached', 'reply_fact_id': fact_id,
                    'reply_ref': fact['content_ref'], 'attached_issue_ids': attached, 'skipped_closed_issue_ids': closed})
        return {'reply_fact_id': fact_id, 'reply_ref': fact['content_ref'],
                'attached_issue_ids': attached, 'skipped_closed_issue_ids': closed}

    delivery = store.mutate(run, key + ':attach', 'explicit_user_reply_shared_with_dependents', attach,
                           payload={'message_registration_id': registration_id, 'reply_fact_id': fact_id})
    # One tick in the normal path. A crash just after tick is harmless: its
    # durable wait operations are idempotent and the retry uses this SAME time.
    controller.tick(run, now=registration['tick_at'])

    def complete(tx):
        waits = [{name: tx.get('wait', wid)[name] for name in ('id', 'issue_id', 'status')}
                 for wid in registration['target_wait_ids']]
        result = {'schema_version': 'explicit_user_reply_resume_v1', 'message_id': message_id,
            'question_id': question_id, 'question_version': question_version,
            **delivery, 'waits': waits, 'original_reply_preserved': True,
            'model_invoked': False, 'user_text_inferred': False}
        tx.update('registration', registration_id, {'status': 'complete', 'result_ref': tx.blob(result)})
        return result

    return store.mutate(run, key + ':complete', 'explicit_user_reply_resume_completed', complete,
                        payload={'message_registration_id': registration_id})
