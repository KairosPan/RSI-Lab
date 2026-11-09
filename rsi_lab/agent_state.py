"""Code-owned agent continuity, separate from shared scientific facts.

A native session is replaceable. Its owner's versioned working memory is not.
Transaction hooks let the controller claim/settle a turn in its existing outbox
transactions. This module never invokes a model or replays an external effect.
"""
from __future__ import annotations

import json
from .store import digest, encode

MEMORY_FIELDS = frozenset({'goal_refs', 'plan_items', 'attempted_steps',
    'rationale_summaries', 'tentative_hypotheses', 'open_questions', 'next_step',
    'evidence_refs', 'work_refs', 'effect_refs', 'artifact_refs',
    'dependency_versions', 'review_flags', 'handoff_refs'})
FINAL_EFFECTS = frozenset({'succeeded', 'failed_definite', 'cancelled'})


def _id(prefix, *parts):
    return prefix + '_' + digest(list(parts))[:24]


def _copy(value):
    return json.loads(encode(value))


def _nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' must be a nonempty string')
    return value


class AgentState:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id
        self.config = store.run(run_id)['config']

    def _mutate(self, key, event, fn, payload):
        return self.store.mutate(self.run_id, key, event, fn, payload=payload)

    def ensure_session(self, role, agent_instance_id=None, branch_id='main', runtime_binding=None):
        """Resolve a trusted owner's current session; never heal unknown by retry."""
        if role not in {'lab', 'meta'}:
            raise ValueError('Only Lab and Meta own persistent primary sessions')
        agent_instance_id = _nonempty(agent_instance_id or role, 'Agent identity')
        branch_id = _nonempty(branch_id, 'Branch')
        owner_scope = {'task_id': self.config.get('task_id', self.run_id),
                       'run_id': self.run_id, 'branch_id': branch_id,
                       'agent_instance_id': agent_instance_id, 'role': role}
        oid = _id('agent', owner_scope)
        existing = self.store.list(self.run_id, 'agent_owner', id=oid)
        if existing:
            session = self.store.get(self.run_id, 'agent_session', existing[0]['session_id'])
            if runtime_binding is not None and session['runtime_binding'] != runtime_binding:
                raise ValueError('Runtime binding changed; explicitly rebuild or migrate the session')
            return session
        payload = {'owner': owner_scope, 'runtime_binding': runtime_binding}
        def apply(tx):
            sid = _id('session', oid, 1)
            tx.insert('agent_owner', oid, {'scope': owner_scope, 'session_id': sid,
                'session_generation': 1, 'memory_revision': 0, 'memory_head_ref': None})
            return tx.insert('agent_session', sid, self._session_document(oid, owner_scope, 1, runtime_binding))
        return self._mutate('agent-owner:' + oid, 'agent_session_created', apply, payload)

    @staticmethod
    def _session_document(oid, scope, generation, binding, supersedes=None):
        return {'owner_id': oid, 'owner': scope, 'role': scope['role'],
            'agent_instance_id': scope['agent_instance_id'], 'branch_id': scope['branch_id'],
            'session_generation': generation, 'runtime_binding': binding,
            'provider_thread_id': None, 'status': 'idle', 'committed_turn': 0,
            'active_turn_id': None, 'fence': 0, 'checkpoint_ref': None,
            'context_manifest': None, 'supersedes_session': supersedes,
            'recovery_reason': None}

    def working_memory(self, session_id, *, requester_session_id=None):
        session = self.store.get(self.run_id, 'agent_session', session_id)
        if requester_session_id is not None:
            requester = self.store.get(self.run_id, 'agent_session', requester_session_id)
            requester_owner = self.store.get(self.run_id, 'agent_owner', requester['owner_id'])
            if requester['owner_id'] != session['owner_id'] or requester_owner['session_id'] != requester_session_id:
                raise ValueError('Working memory requires its current owner; use an explicit handoff')
        owner = self.store.get(self.run_id, 'agent_owner', session['owner_id'])
        if owner['memory_head_ref'] is None:
            return {'owner_id': owner['id'], 'revision': 0, 'head_ref': None, 'content': {}}
        revision = self.store.get(self.run_id, 'working_memory', owner['memory_head_ref'])
        return {'owner_id': owner['id'], 'revision': revision['revision'],
                'head_ref': revision['id'], 'content': _copy(revision['content'])}

    def read_working_memory(self, session_id, *, actor_session_id):
        """Agent-facing read: explicit actor identity is supplied by the host."""
        return self.working_memory(session_id, requester_session_id=actor_session_id)

    def session_snapshot(self, session_id):
        return {'session': self.store.get(self.run_id, 'agent_session', session_id),
                'working_memory': self.working_memory(session_id)}

    def prepare_turn_tx(self, tx, session_id, work_id, *, expected_turn_seq=None,
                        expected_memory_revision=None, context_ref=None):
        session = tx.get('agent_session', session_id)
        owner = tx.get('agent_owner', session['owner_id'])
        tx.get('work', work_id)
        if owner['session_id'] != session_id or session['status'] != 'idle' or session['active_turn_id']:
            raise ValueError('Session is busy, superseded or requires recovery')
        if expected_turn_seq is not None and expected_turn_seq != session['committed_turn']:
            raise ValueError('Session prefix changed after context preparation')
        if expected_memory_revision is not None and expected_memory_revision != owner['memory_revision']:
            raise ValueError('Working memory changed after context preparation')
        tid = _id('turn', session_id, session['committed_turn'] + 1, work_id)
        turn = tx.insert('agent_turn', tid, {'session_id': session_id,
            'owner_id': owner['id'], 'work_id': work_id,
            'turn_seq': session['committed_turn'] + 1, 'expected_session_head': session['committed_turn'],
            'expected_memory_revision': owner['memory_revision'], 'context_ref': context_ref,
            'fence': None, 'status': 'prepared', 'effect_ids': [], 'request_results': {},
            'terminal_ref': None, 'memory_update_status': None})
        tx.update('agent_session', session_id, {'status': 'prepared', 'active_turn_id': tid})
        return turn

    def claim_turn_tx(self, tx, turn_id, effect_id, *, expected_fence=None):
        turn = tx.get('agent_turn', turn_id)
        session = tx.get('agent_session', turn['session_id'])
        owner = tx.get('agent_owner', turn['owner_id'])
        effect = tx.get('effect', effect_id)
        if session['active_turn_id'] != turn_id or owner['session_id'] != session['id']:
            raise ValueError('Session turn no longer owns its writer lease')
        if (effect['work_id'] != turn['work_id']
                and effect.get('metadata', {}).get('agent_turn_id') != turn_id):
            raise ValueError('Child Effect is not bound to this turn owner')
        if effect.get('metadata', {}).get('agent_session_id', session['id']) != session['id']:
            raise ValueError('Child Effect belongs to another session')
        if effect['status'] != 'prepared':
            raise ValueError('Only a prepared child Effect can claim a turn')
        if owner['memory_revision'] != turn['expected_memory_revision']:
            raise ValueError('Working memory revision is stale')
        if effect_id in turn['effect_ids']:
            raise ValueError('An already dispatched child request must not be resent')
        if turn['status'] == 'prepared':
            if session['status'] != 'prepared' or session['committed_turn'] != turn['expected_session_head']:
                raise ValueError('Session prefix changed before dispatch')
            fence = session['fence'] + 1
            if expected_fence is not None and expected_fence != session['fence']:
                raise ValueError('Stale session fencing token')
            tx.update('agent_session', session['id'], {'status': 'in_turn', 'fence': fence})
        elif turn['status'] in {'running', 'waiting'}:
            fence = turn['fence']
            if session['status'] != 'in_turn' or session['fence'] != fence:
                raise ValueError('Stale session fencing token')
            if expected_fence is not None and expected_fence != fence:
                raise ValueError('Stale child request fencing token')
            if any(result['status'] == 'unknown' for result in turn['request_results'].values()):
                raise ValueError('Unknown child Effect requires reconciliation')
            if any(eid not in turn['request_results'] for eid in turn['effect_ids']):
                raise ValueError('The previous session request has not settled')
        else:
            raise ValueError('Completed or unknown turn cannot dispatch another request')
        return tx.update('agent_turn', turn_id, {'status': 'running', 'fence': fence,
            'effect_ids': turn['effect_ids'] + [effect_id]})

    def settle_request_tx(self, tx, turn_id, effect_id, receipt):
        turn = tx.get('agent_turn', turn_id)
        session = tx.get('agent_session', turn['session_id'])
        if effect_id not in turn['effect_ids']:
            raise ValueError('Receipt belongs to a different turn')
        status = receipt.get('status')
        if status not in {'succeeded', 'failed_definite', 'unknown'}:
            raise ValueError('Unverifiable child execution status')
        previous = turn['request_results'].get(effect_id)
        result = {'status': status, 'receipt_hash': digest(receipt)}
        if previous == result:
            return turn
        if previous and previous['status'] != 'unknown':
            # Accounting may be completed later without rewriting execution.
            if previous['status'] != status:
                raise ValueError('A known child outcome cannot change')
            return turn
        results = {**turn['request_results'], effect_id: result}
        owns = (session['active_turn_id'] == turn_id and session['fence'] == turn['fence'])
        unknown = any(row['status'] == 'unknown' for row in results.values())
        updated = tx.update('agent_turn', turn_id, {'request_results': results,
            'status': 'recovery_required' if unknown or not owns else 'waiting'})
        if owns:
            tx.update('agent_session', session['id'], {'status': 'recovery_required' if unknown else 'in_turn',
                'recovery_reason': 'unknown_effect' if unknown else None})
        return updated

    def _memory_delta(self, delta, old):
        if not isinstance(delta, dict) or set(delta) - MEMORY_FIELDS:
            raise ValueError('Working memory update has unsupported fields; owner and facts are controller-owned')
        for name, value in delta.items():
            if name == 'next_step':
                if value is not None and not isinstance(value, (str, dict)):
                    raise ValueError('next_step must be text, an object, or null')
            elif name == 'dependency_versions':
                if not isinstance(value, (dict, list)):
                    raise ValueError('dependency_versions must be structured references')
            elif not isinstance(value, list):
                raise ValueError(name + ' must be a list')
        content = {**old, **_copy(delta)}
        if len(encode(content)) > self.config.get('max_working_memory_bytes', 65536):
            raise ValueError('Working memory exceeds its registered byte limit')
        return content

    def commit_turn_tx(self, tx, turn_id, *, terminal_ref, working_memory_delta=None,
                       expected_memory_revision=None, provider_thread_id=None, context_manifest=None):
        """Commit a trusted native terminal after its child requests have settled.

        The controller, not a model command, calls this hook with the durable
        terminal receipt. A malformed memory proposal cannot erase completion.
        """
        turn = tx.get('agent_turn', turn_id)
        if turn['status'] == 'committed':
            if turn['terminal_ref'] != terminal_ref:
                raise ValueError('Turn terminal changed after commit')
            return turn
        session = tx.get('agent_session', turn['session_id'])
        owner = tx.get('agent_owner', turn['owner_id'])
        if (session['active_turn_id'] != turn_id or session['fence'] != turn['fence']
                or owner['session_id'] != session['id']):
            raise ValueError('Stale turn cannot update the current agent session')
        if turn['status'] not in {'running', 'waiting'} or not turn['effect_ids']:
            raise ValueError('Unknown/unstarted turn has no certified terminal')
        effects = [tx.get('effect', eid) for eid in turn['effect_ids']]
        if (len(turn['request_results']) != len(effects)
                or any(e['status'] not in FINAL_EFFECTS for e in effects)
                or any(r['status'] == 'unknown' for r in turn['request_results'].values())):
            raise ValueError('Required child Effects have not settled')
        terminal = json.loads(self.store.read_blob(terminal_ref))
        if not isinstance(terminal, dict) or terminal.get('status') not in {'succeeded', 'failed_definite'}:
            raise ValueError('Turn requires a known terminal execution receipt')
        if (self.config.get('execution_mode') in {'live', 'role_recall'}
                and terminal.get('terminal_received') is not True):
            raise ValueError('Native turn requires an authenticated terminal event')
        if terminal.get('protocol_valid') is False:
            raise ValueError('Rejected native protocol cannot commit an agent turn')
        if (terminal.get('native_writer_quiesced') is False
                or terminal.get('recovery_requires_fencing') is True):
            raise ValueError('Unconfirmed native writer must be fenced before turn commit')
        if terminal_ref not in {e.get('receipt_ref') for e in effects} | {e.get('result_ref') for e in effects}:
            raise ValueError('Terminal must reference a settled child receipt')
        if expected_memory_revision is None:
            expected_memory_revision = turn['expected_memory_revision']
        if (expected_memory_revision != turn['expected_memory_revision']
                or expected_memory_revision != owner['memory_revision']):
            raise ValueError('Working memory CAS conflict')
        if session['committed_turn'] != turn['expected_session_head']:
            raise ValueError('Session head CAS conflict')
        if provider_thread_id is not None:
            _nonempty(provider_thread_id, 'Provider thread identity')
            if session['provider_thread_id'] not in {None, provider_thread_id}:
                raise ValueError('Provider returned another native thread')
        old = {} if owner['memory_head_ref'] is None else tx.get('working_memory', owner['memory_head_ref'])['content']
        memory_status, memory_error = 'unchanged', None
        if working_memory_delta is not None:
            try:
                content = self._memory_delta(working_memory_delta, old)
            except (ValueError, TypeError) as exc:
                content, memory_status, memory_error = None, 'rejected', str(exc)
            if content is not None and content != old:
                revision = owner['memory_revision'] + 1
                rid = _id('memory', owner['id'], revision, turn_id)
                source_effect = next(e for e in effects if terminal_ref in {e.get('receipt_ref'), e.get('result_ref')})
                tx.insert('working_memory', rid, {'owner_id': owner['id'], 'owner': owner['scope'],
                    'revision': revision, 'parent_ref': owner['memory_head_ref'], 'content': content,
                    'source_turn_id': turn_id, 'source_call_id': source_effect['id'],
                    'source_effect_id': source_effect['id'], 'ledger_snapshot_seq': tx.seq,
                    'context_manifest_hash': digest(context_manifest) if context_manifest is not None else None,
                    'schema_version': 'agent_working_memory_v1', 'verification': 'unverified_working_state'})
                tx.update('agent_owner', owner['id'], {'memory_revision': revision, 'memory_head_ref': rid})
                memory_status = 'updated'
        updated = tx.update('agent_turn', turn_id, {'status': 'committed', 'terminal_ref': terminal_ref,
            'memory_update_status': memory_status, 'memory_update_error': memory_error})
        tx.update('agent_session', session['id'], {'status': 'idle', 'active_turn_id': None,
            'committed_turn': turn['turn_seq'], 'provider_thread_id': provider_thread_id or session['provider_thread_id'],
            'context_manifest': _copy(context_manifest) if context_manifest is not None else session['context_manifest'],
            'recovery_reason': None})
        return updated

    def cancel_turn_tx(self, tx, turn_id, reason):
        turn = tx.get('agent_turn', turn_id)
        session = tx.get('agent_session', turn['session_id'])
        if turn['status'] != 'prepared' or turn['effect_ids']:
            raise ValueError('Only an undispatched turn can be cancelled')
        if session['active_turn_id'] != turn_id:
            raise ValueError('Prepared turn no longer owns the session')
        tx.update('agent_session', session['id'], {'status': 'idle', 'active_turn_id': None})
        return tx.update('agent_turn', turn_id, {'status': 'cancelled', 'cancellation_reason': str(reason)})

    def seal_checkpoint(self, session_id, key):
        def apply(tx):
            session = tx.get('agent_session', session_id)
            owner = tx.get('agent_owner', session['owner_id'])
            if session['status'] != 'idle' or session['active_turn_id'] or owner['session_id'] != session_id:
                raise ValueError('Checkpoint requires a quiescent current session')
            cid = _id('checkpoint', session_id, session['committed_turn'], owner['memory_revision'])
            existing = tx.find('agent_checkpoint', id=cid)
            if existing:
                return existing[0]
            checkpoint = tx.insert('agent_checkpoint', cid, {'owner_id': owner['id'],
                'session_id': session_id, 'session_snapshot': session,
                'memory_revision': owner['memory_revision'], 'memory_head_ref': owner['memory_head_ref'],
                'ledger_seq': tx.seq, 'context_manifest': session['context_manifest'],
                'runtime_binding': session['runtime_binding']})
            tx.update('agent_session', session_id, {'checkpoint_ref': cid})
            return checkpoint
        return self._mutate(key, 'agent_checkpoint_sealed', apply, {'session_id': session_id})

    def rebuild_session(self, session_id, reason, key, *, runtime_binding=None, checkpoint_id=None):
        """Replace a quiescent native session, preserving the current memory head.

        Unknown turns must reconcile first. This cannot use a fresh thread to
        evade an external effect or restore an older scientific/financial state.
        """
        _nonempty(reason, 'Reconstruction reason')
        def apply(tx):
            session = tx.get('agent_session', session_id)
            owner = tx.get('agent_owner', session['owner_id'])
            if owner['session_id'] != session_id or session['active_turn_id']:
                raise ValueError('Unresolved or stale session cannot be replaced')
            if session['status'] not in {'idle', 'recovery_required'}:
                raise ValueError('Session is not at a reconstructable boundary')
            if checkpoint_id:
                checkpoint = tx.get('agent_checkpoint', checkpoint_id)
                if checkpoint['owner_id'] != owner['id']:
                    raise ValueError('Checkpoint belongs to another agent')
            generation = owner['session_generation'] + 1
            sid = _id('session', owner['id'], generation)
            binding = session['runtime_binding'] if runtime_binding is None else runtime_binding
            document = self._session_document(owner['id'], owner['scope'], generation, binding, session_id)
            document.update(reconstruction_reason=reason, reconstruction_checkpoint=checkpoint_id,
                reconstruction_memory_ref=owner['memory_head_ref'], resume_mode='reconstruct_from_checkpoint')
            new = tx.insert('agent_session', sid, document)
            tx.update('agent_session', session_id, {'status': 'superseded'})
            tx.update('agent_owner', owner['id'], {'session_id': sid, 'session_generation': generation})
            return new
        return self._mutate(key, 'agent_session_reconstructed', apply,
            {'session_id': session_id, 'reason': reason, 'binding': runtime_binding, 'checkpoint_id': checkpoint_id})

    def propose_handoff(self, source_session_id, target_session_id, selected_fields, purpose, key,
                        *, expected_revision=None):
        _nonempty(purpose, 'Handoff purpose')
        if (not isinstance(selected_fields, list) or not selected_fields
                or any(field not in MEMORY_FIELDS for field in selected_fields)):
            raise ValueError('Handoff must select explicit working-memory fields')
        payload = dict(source_session_id=source_session_id, target_session_id=target_session_id,
                       selected_fields=selected_fields, purpose=purpose, expected_revision=expected_revision)
        def apply(tx):
            source, target = [tx.get('agent_session', sid) for sid in (source_session_id, target_session_id)]
            if source['owner_id'] == target['owner_id'] or source['branch_id'] != target['branch_id']:
                raise ValueError('Handoff needs different agents in the same authorized branch')
            owner = tx.get('agent_owner', source['owner_id'])
            recipient = tx.get('agent_owner', target['owner_id'])
            if owner['session_id'] != source_session_id or recipient['session_id'] != target_session_id:
                raise ValueError('Handoff requires current source and recipient sessions')
            if expected_revision is not None and expected_revision != owner['memory_revision']:
                raise ValueError('Handoff working-memory revision changed')
            content = {} if owner['memory_head_ref'] is None else tx.get('working_memory', owner['memory_head_ref'])['content']
            selected = {k: content[k] for k in selected_fields if k in content}
            hid = _id('handoff', self.run_id, key)
            return tx.insert('agent_handoff', hid, {'source_owner_id': source['owner_id'],
                'target_owner_id': target['owner_id'], 'source_memory_ref': owner['memory_head_ref'],
                'scope': {'run_id': self.run_id, 'branch_id': source['branch_id']}, 'purpose': purpose,
                'payload_ref': tx.blob(selected), 'status': 'proposed', 'target_work_id': None})
        return self._mutate(key, 'agent_handoff_proposed', apply, payload)

    def accept_handoff(self, handoff_id, target_session_id, key):
        def apply(tx):
            handoff = tx.get('agent_handoff', handoff_id)
            session = tx.get('agent_session', target_session_id)
            owner = tx.get('agent_owner', session['owner_id'])
            if handoff['target_owner_id'] != owner['id'] or owner['session_id'] != target_session_id:
                raise ValueError('Only the designated current agent can accept this handoff')
            if handoff['status'] == 'accepted':
                return handoff
            if handoff['status'] != 'proposed':
                raise ValueError('Handoff is not available')
            wid = _id('work', 'agent_handoff', handoff_id)
            tx.insert('work', wid, {'issue_id': None, 'kind': 'lab' if session['role'] == 'lab' else 'meta_discovery',
                'trigger': 'handoff:' + handoff_id, 'expected_versions': {}, 'status': 'ready',
                'priority': 10, 'inputs': {'handoff_id': handoff_id, 'agent_instance_id': session['agent_instance_id'],
                                         'branch_id': session['branch_id'], 'evidence_ids': []},
                'attempts': 0, 'effect_id': None})
            return tx.update('agent_handoff', handoff_id, {'status': 'accepted', 'target_work_id': wid})
        return self._mutate(key, 'agent_handoff_accepted', apply,
            {'handoff_id': handoff_id, 'target_session_id': target_session_id})

    def handoff_payload(self, handoff_id, *, actor_session_id):
        handoff = self.store.get(self.run_id, 'agent_handoff', handoff_id)
        session = self.store.get(self.run_id, 'agent_session', actor_session_id)
        owner = self.store.get(self.run_id, 'agent_owner', session['owner_id'])
        if (handoff['status'] != 'accepted' or handoff['target_owner_id'] != owner['id']
                or owner['session_id'] != actor_session_id):
            raise ValueError('Only the current recipient can read an accepted handoff')
        return {'handoff_id': handoff_id, 'source_memory_ref': handoff['source_memory_ref'],
                'purpose': handoff['purpose'], 'content': json.loads(self.store.read_blob(handoff['payload_ref']))}

    def discard_completed_turn(self, session_id, reason, key):
        """Explicitly quarantine a known finished turn before reconstructing.

        This discards proposals, not evidence or costs. Unknown execution or an
        unconfirmed native writer must be reconciled/fenced before this path.
        """
        _nonempty(reason, 'Discard reason')
        def apply(tx):
            session = tx.get('agent_session', session_id)
            owner = tx.get('agent_owner', session['owner_id'])
            if owner['session_id'] != session_id:
                raise ValueError('Only the current agent session can be quarantined')
            if session['active_turn_id'] is None:
                return session
            turn = tx.get('agent_turn', session['active_turn_id'])
            if not turn['effect_ids']:
                raise ValueError('Use prepared cancellation before any external dispatch')
            effects = [tx.get('effect', eid) for eid in turn['effect_ids']]
            if any(effect['status'] not in FINAL_EFFECTS for effect in effects):
                raise ValueError('Unknown child Effects cannot be discarded or bypassed')
            receipts = [json.loads(self.store.read_blob(effect['result_ref'] or effect['receipt_ref']))
                        for effect in effects]
            never_dispatched = all(receipt.get('never_dispatched') is True
                                   and effect['status'] == 'failed_definite'
                                   for effect, receipt in zip(effects, receipts))
            terminal = receipts[-1]
            if not never_dispatched and (terminal.get('terminal_received') is not True
                    or terminal.get('native_writer_quiesced') is False
                    or terminal.get('recovery_requires_fencing') is True):
                raise ValueError('A certified terminal and quiescent native writer are required')
            tx.update('agent_turn', turn['id'], {'status': 'rejected',
                'rejection_reason': reason, 'discarded_terminal_ref': effects[-1]['result_ref'],
                'memory_update_status': 'discarded'})
            return tx.update('agent_session', session_id, {'status': 'recovery_required',
                'active_turn_id': None, 'fence': session['fence'] + 1,
                'recovery_reason': reason})
        return self._mutate(key, 'agent_turn_discarded', apply,
                            {'session_id': session_id, 'reason': reason})
