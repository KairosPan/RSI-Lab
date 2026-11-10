"""Durable Lab/Meta runtime. Models propose; only this closed router acts.

Adapters are injected by trusted application code. A model cannot supply Python,
register an executable, settle a cost, or promote a scientific protocol. Calls
are individually budgeted; this is a cooperative driver, not a background daemon.
"""
from __future__ import annotations

from datetime import datetime
import json
import time

from .controller import Controller, identity
from .store import digest, encode
from .packets import PacketBuilder, make_evidence, present_delivery
from .roles import MetaAgent, discovery_trigger, initial_review_receipt, validate_command
from .capabilities import CapabilityRegistry, execute_acceptance
from .trials import TrialEngine
from .agent_state import AgentState
from .session_context import read_native_snapshot, build_effective_context
from .transport import _json as transport_json
from .call_recovery import recover_codex_receipt, RecoveryBlocked
from .improvements import ImprovementLoop, enabled
from .research_policy import normalize_research_policy
from .driver_report import build_report


def recovery_error(stage, item_id, exc):
    """A reportable recovery failure for one recovery stage and item."""
    return {'stage': stage, 'id': item_id, 'error_type': type(exc).__name__, 'reason': str(exc)}


class Runtime:
    controller_factory = Controller
    trial_engine_factory = TrialEngine
    packet_builder_factory = PacketBuilder
    meta_agent_factory = MetaAgent

    def __init__(self, store, run_id, *, adapters=(), scientific_identity_provider=None):
        self.store, self.run_id = store, run_id
        self.controller = self.controller_factory(store)
        from .adapters import AdapterRegistry
        self.adapters = AdapterRegistry(adapters)
        self.config = store.run(run_id)['config']
        if self.config.get('unknown_usage_policy', 'hold') not in {'hold', 'charge_reservation'}:
            raise ValueError('unknown_usage_policy must be hold or charge_reservation')
        retries = self.config.get('model_failure_retries', 0)
        if type(retries) is not int or not 0 <= retries <= 5:
            raise ValueError('model_failure_retries must be an integer from 0 to 5')
        self.research_policy = normalize_research_policy(self.config.get('research_policy'))
        self.agents = AgentState(store, run_id)
        self.improvements = ImprovementLoop(self) if enabled(self.config,'meta_lab') else None
        self.scientific_memory = None
        if enabled(self.config,'scientific_memory'):
            from .scientific_memory import ScientificMemory
            self.scientific_memory = ScientificMemory(store, run_id, identity_provider=scientific_identity_provider)

    def _mutate(self, key, event, fn, payload):
        return self.store.mutate(self.run_id, key, event, fn, payload=payload)

    def versions(self, issue_id):
        if issue_id is None:
            return dict(issue_version=0, evidence_revision=0, scheduling_revision=0)
        issue = self.store.get(self.run_id, 'issue', issue_id)
        return dict(issue_version=issue['version'], evidence_revision=issue['evidence_revision'],
                    scheduling_revision=issue['scheduling_revision'])

    def capability_catalog(self):
        # A verified artifact is usable only under its explicit pinned version.
        result, ambiguous = {}, set()
        for row in self.store.list(self.run_id, 'capability', status='verified'):
            key = row['capability_id']
            if key in result or key in ambiguous:
                # No implicit latest-version activation.
                result.pop(key, None)
                ambiguous.add(key)
                continue
            result[key] = {'status': 'accepted', 'version': row['capability_version'],
                           'registration_id': row['id']}
        for adapter in self.adapters.by_id.values():
            if adapter.adapter_id in ambiguous:
                raise ValueError('Multiple artifact versions need explicit activation before adapter use')
            accepted = result.get(adapter.adapter_id)
            if accepted:
                record = self.store.get(self.run_id,'capability',accepted['registration_id'])
                if (accepted['version'] != adapter.version
                        or adapter.manifest.get('artifact_sha256') != record['artifact_ref']):
                    raise ValueError('Host adapter differs from the accepted artifact version or identity')
                result[adapter.adapter_id] = {**accepted,'admission':'verified_artifact_adapter',
                                              'descriptor':adapter.descriptor()}
            elif not adapter.requires_acceptance:
                result[adapter.adapter_id] = {'status': 'accepted', 'version': adapter.version,
                    'admission': 'explicit_host_registration', 'descriptor': adapter.descriptor()}
        return result

    def observe(self, event_id, trigger_kind, batch_index, evidence_ids=()):
        """Called at batch/input/tool/evidence boundaries, including open review."""
        payload = dict(event_id=event_id, trigger_kind=trigger_kind, batch_index=batch_index,
                       evidence_ids=list(evidence_ids))
        def apply(tx):
            for fid in evidence_ids:
                tx.get('fact', fid)
            counters = tx.find('registration', id='discovery_counters')
            state = counters[0] if counters else tx.insert('registration', 'discovery_counters',
                {'open_reviews_used': 0, 'last_open_review_batch': 0})
            proposal = discovery_trigger(event_id=event_id, trigger_kind=trigger_kind, batch_index=batch_index,
                last_open_review_batch=state['last_open_review_batch'], open_reviews_used=state['open_reviews_used'],
                max_open_reviews=self.config.get('max_open_reviews', 2),
                interval_batches=self.config.get('open_review_interval_batches', 5), anomaly_evidence_ids=evidence_ids)
            if proposal['admitted']:
                if proposal['open_review_charge']:
                    tx.update('registration', state['id'], {'last_open_review_batch': batch_index,
                        'open_reviews_used': state['open_reviews_used'] + 1})
                selected = list(evidence_ids)
                if not selected:
                    # Open review gets current raw material without requiring
                    # the caller to predict which premise is missing.
                    facts = sorted((f for f in tx.find('fact') if f['kind'] in {'raw_fact', 'source_statement'}
                                    and (f['provenance'] if isinstance(f['provenance'], dict) else {}).get('delivery_kind') not in {'summary', 'pointer'}),
                                   key=lambda f: f['created_seq'])
                    selected = [f['id'] for f in facts[-self.config.get('open_review_fact_limit', 16):]]
                work = self.controller._work(tx, None, 'meta_discovery', 'event:' + event_id,
                    inputs={'trigger': proposal, 'evidence_ids': selected})
                proposal['work_id'] = work['id']
            return proposal
        return self._mutate('observe:' + event_id, 'discovery_admission', apply, payload)

    def _context(self, work):
        issue_id = work['issue_id']
        issue = self.store.get(self.run_id, 'issue', issue_id) if issue_id else None
        mode = 'lab' if work['kind'] == 'lab' else 'discovery' if work['kind'] == 'meta_discovery' else 'disposition'
        evidence_ids = work['inputs'].get('evidence_ids', issue['evidence_ids'] if issue else [])
        catalog = {}
        for fact in self.store.list(self.run_id, 'fact'):
            provenance = fact['provenance'] if isinstance(fact['provenance'], dict) else {}
            catalog[fact['id']] = make_evidence(fact['id'], self.store.read_blob(fact['content_ref']).decode(),
                kind='summary' if provenance.get('delivery_kind') == 'summary' else fact['kind'],
                source=encode(fact['provenance']).decode(), version=fact['version'],
                summary_of=provenance.get('summary_of', []))
        task = {'goal': self.config.get('goal', 'Investigate task prerequisites and continue the frozen task plan'),
            'issue_id': issue_id, 'expected_versions': self.versions(issue_id),
            'run_id': self.run_id, 'capability_catalog': self.capability_catalog(),
            'authorized_sources': self.config.get('authorized_sources', []),
            'constraints': {'park_wake_syntax': 'version_changed or status:<status>',
                'one_expiry_action': work['inputs'].get('allowed_action'),
                'previous_routing_error': work['inputs'].get('routing_error'),
                'authorized_source_contracts': self.config.get('authorized_source_contracts', {}),
                'waiting': self.store.list(self.run_id, 'wait', issue_id=issue_id) if issue_id else []}}
        required = list(work['inputs'].get('required_evidence_ids', []))
        if self.config.get('research_features'):
            task['constraints']['research_features'] = self.config['research_features']
        if self.research_policy:
            task['constraints']['research_policy'] = self.research_policy
        if self.improvements:
            loop_context, loop_required = self.improvements.context(work,mode)
            task['constraints']['research_loop'] = loop_context
            required.extend(loop_required)
        if work['inputs'].get('final_review_only'):
            task['constraints']['final_review_only'] = True
            task['capability_catalog'] = {}
        if self.config.get('task_plan_id'):
            from .plans import load_task_plan
            try:
                task['constraints']['frozen_task_plan'] = load_task_plan(self.store, self.run_id, self.config['task_plan_id'])
            except KeyError:
                task['constraints'].update(task_plan_status='not_registered',task_plan_id=self.config['task_plan_id'])
        if issue:
            task['issue'] = issue
            task['requests'] = self.store.list(self.run_id, 'request', issue_id=issue_id)
            task['dependencies'] = self.store.list(self.run_id, 'dependency')
        else:
            task['trigger'] = work['inputs'].get('trigger', {})
        role = 'lab' if mode == 'lab' else 'meta'
        session = self.agents.ensure_session(role, agent_instance_id=work['inputs'].get('agent_instance_id'),
            branch_id=work['inputs'].get('branch_id', self.config.get('branch_id', 'main')),
            runtime_binding={'model': self.config.get('model', 'gpt-6-astra'),
                             'effort': self.config.get('effort', 'medium'), 'transport': 'codex_exec_text_v1'})
        memory = self.agents.read_working_memory(session['id'], actor_session_id=session['id'])
        if self.scientific_memory:
            # Only trusted host Work can specify a query. This owner's private
            # plan provides retrieval hints, never shared facts or authority.
            # Do not inspect the other role's memory or put hint text in the
            # shared context, evidence catalog, or publication records.
            supplied = work['inputs'].get('scientific_query', {})
            if isinstance(supplied,str):
                query = {'text':supplied}
            elif isinstance(supplied,dict):
                query = json.loads(encode(supplied))
            else:
                raise ValueError('Scientific retrieval query must be host-supplied text or an object')
            hints = [query.get('text','')]
            for field in ('plan_items','tentative_hypotheses','rationale_summaries','next_step'):
                value = memory['content'].get(field)
                if isinstance(value,str):
                    hints.append(value)
                elif isinstance(value,list):
                    hints.extend(item for item in value if isinstance(item,str))
            query['text'] = '\n'.join(hint for hint in hints if isinstance(hint,str) and hint.strip())[:8192]
            if mode == 'discovery':
                query['exclude_interpretations'] = True
            if work['inputs'].get('final_review_only'):
                query.update(final_review_only=True, include_historical=False)
            context = self.scientific_memory.prepare_context(role, list(dict.fromkeys(list(evidence_ids)+required)),
                query=query, budget=self.config.get('scientific_memory_context_bytes',16384))
            task['scientific_memory'] = context
            required.extend(context['required_evidence_ids'])
        task['agent_state'] = {'session_id': session['id'], 'agent_instance_id': session['agent_instance_id'],
            'role': role, 'memory_revision': memory['revision'], 'working_memory': memory['content'],
            'memory_authority': 'private_unverified_working_state',
            'committed_turn': session['committed_turn']}
        if work['inputs'].get('handoff_id'):
            task['agent_state']['handoff'] = self.agents.handoff_payload(
                work['inputs']['handoff_id'], actor_session_id=session['id'])
        agent = self.meta_agent_factory(self.packet_builder_factory(catalog, max_input_bytes=self.config.get('max_input_bytes', 131072)),
                          model=self.config.get('model', 'gpt-6-astra'), effort=self.config.get('effort', 'medium'))
        options = {'evidence_ids': evidence_ids,
                   'required_evidence_ids': list(dict.fromkeys(required))}
        return agent, mode, task, options

    def prepare_meta(self, work_id, reservations):
        if self.config.get('execution_mode') in {'live', 'role_recall'}:
            if (set(reservations) != {'model_calls', 'tokens', 'wall_seconds'} or reservations['model_calls'] != 1
                    or reservations['tokens'] <= 0 or reservations['wall_seconds'] <= 0):
                raise ValueError('Real Meta invocation needs one hard call reservation and explicit soft token/time estimates')
        work = self.store.get(self.run_id, 'work', work_id)
        if work['kind'] not in {'lab', 'meta_discovery', 'meta_disposition', 'disposition'} or work['status'] != 'ready':
            raise ValueError('An eligible Lab or Meta Work is required')
        agent, mode, task, options = self._context(work)
        if task.get('scientific_memory',{}).get('status') == 'blocked':
            return self._mutate('scientific-context-block:'+work_id,'packet_unrepresentable',
                lambda tx:tx.update('work',work_id,{'status':'blocked',
                    'outcome_ref':tx.blob(task['scientific_memory'])}),
                {'work_id':work_id,'scientific_memory':task['scientific_memory']})
        planned = agent.prepare(mode, task, **options)
        if planned['status'] != 'ready':
            return self._mutate('context-block:' + work_id, 'packet_unrepresentable',
                lambda tx: tx.update('work', work_id, {'status': 'blocked', 'outcome_ref': tx.blob(planned)}),
                {'work_id': work_id, 'plan': planned})
        packet = agent.for_dispatch(planned, mode, task, **options)
        session = self.store.get(self.run_id, 'agent_session', task['agent_state']['session_id'])
        snapshot = None
        if session.get('provider_thread_id'):
            try:
                previous = session.get('context_manifest') or {}
                if not previous.get('native_session_ref'):
                    raise ValueError('Session lacks a native snapshot; explicit reconstruction is required')
                snapshot = read_native_snapshot(previous['native_session_ref'],
                    expected_thread_id=session['provider_thread_id'],
                    expected_cwd=str(self.store.root / 'agents' / session['id'] / 'workspace'),
                    expected_prefix=previous.get('native_prefix'))
                if snapshot['prefix_sha256'] != previous.get('native_prefix', {}).get('prefix_sha256'):
                    raise ValueError('Native session advanced outside its committed turn; reconcile before resume')
            except (ValueError, OSError) as exc:
                reason = str(exc)
                def block_context(tx):
                    current = tx.get('agent_session', session['id'])
                    if current['version'] != session['version'] or current['active_turn_id']:
                        raise ValueError('Session changed while checking its context; inspect current owner')
                    tx.update('agent_session', session['id'], {'status': 'recovery_required', 'recovery_reason': reason})
                    return tx.update('work', work_id, {'status': 'blocked', 'outcome_ref': tx.blob({'reason': reason})})
                self._mutate('native-context-block:' + work_id, 'agent_context_blocked', block_context,
                    {'work_id': work_id, 'session_id': session['id'], 'reason': reason})
                raise ValueError(reason) from exc
        context = build_effective_context(packet['envelope_bytes'], session_id=session['id'],
            owner=session['owner'], snapshot=snapshot,
            working_memory_revision=task['agent_state']['memory_revision'],
            max_input_bytes=self.config.get('max_effective_context_bytes', self.config.get('max_input_bytes', 131072)))
        if context['status'] != 'ready':
            return self._mutate('session-context-block:' + work_id, 'packet_unrepresentable',
                lambda tx: tx.update('work', work_id, {'status': 'blocked', 'outcome_ref': tx.blob(context)}),
                {'work_id': work_id, 'context': context})
        plan_ref = self.store.put_blob({key: value for key, value in packet.items() if key != 'envelope_bytes'})
        kind = 'lab' if mode == 'lab' else 'meta'
        return self.controller.prepare_effect(self.run_id, work_id, kind, packet['envelope_bytes'], reservations,
            'prepare-agent:' + work_id, metadata={'mode': mode, 'plan_ref': plan_ref, 'issue_id': work['issue_id'],
                'agent_session_id': session['id'], 'agent_session': session,
                'expected_session_head': session['committed_turn'],
                'expected_memory_revision': task['agent_state']['memory_revision'],
                **({'research_features': self.config['research_features']} if self.config.get('research_features') else {}),
                **({'research_policy': self.research_policy} if self.research_policy else {}),
                **({'final_review_only':True} if task['constraints'].get('final_review_only') else {}),
                'context_ref': self.store.put_blob(context), 'effective_context_hash': context['effective_context_hash'],
                'max_effective_context_bytes': context['max_input_bytes']})

    def run_meta(self, work_id, executor, reservations):
        work = self.store.get(self.run_id, 'work', work_id)
        if work['status'] == 'leased' and work.get('effect_id'):
            effect = self.store.get(self.run_id, 'effect', work['effect_id'])
            if effect['kind'] not in {'meta', 'lab'} or effect['status'] != 'prepared':
                raise ValueError('An already dispatched or unknown invocation must be reconciled, never resent')
            if effect['reservations'] != reservations:
                raise ValueError('Prepared invocation must retain its original resource reservation')
        else:
            effect = self.prepare_meta(work_id, reservations)
        if effect.get('status') == 'blocked':
            return {'work': effect, 'proposal': None}
        effect = self.controller.dispatch_effect(self.run_id, effect['id'], executor)
        effect = self._apply_usage_policy(effect)
        if effect['status'] in {'succeeded', 'failed_definite'}:
            receipt = json.loads(self.store.read_blob(effect['result_ref']))
            if receipt.get('terminal_received') or self.config.get('execution_mode') not in {'live', 'role_recall'}:
                try:
                    self._commit_agent_turn(effect, receipt)
                except ValueError as exc:
                    self._block_agent_turn(effect, str(exc))
                    raise
            elif effect['status'] == 'failed_definite':
                self._block_agent_turn(effect, receipt.get('error') or 'Known failure has no native turn terminal')
        self._schedule_retry(effect)
        proposal = self.apply_meta(effect['id']) if effect['status'] == 'succeeded' and not effect.get('accounting_unknown') else None
        return {'effect': effect, 'proposal': proposal}

    def _apply_usage_policy(self, effect):
        """Charge a finished call's unmeasured resources at their held amount when the run opts in.

        Returns the Effect as stored afterwards. Dispatching and unknown calls
        keep their holds: only a known outcome can be settled this way.
        """
        if (self.config.get('unknown_usage_policy', 'hold') != 'charge_reservation'
                or effect['kind'] not in {'lab', 'meta'} or effect['status'] not in {'succeeded', 'failed_definite'}
                or not effect.get('accounting_unknown')):
            return effect
        missing = sorted(r for r in effect['reservations'] if r not in effect.get('actual_costs', {}))
        receipt = {'status': effect['status'],
                   'actual_costs': {r: effect['held_reservations'][r] for r in missing},
                   'estimated_resources': missing,
                   'reconciliation': {'effect_id': effect['id'],
                                      'provenance': 'run config unknown_usage_policy=charge_reservation'}}
        self.controller.reconcile(self.run_id, effect['id'], receipt, 'usage-policy:' + effect['id'])
        return self.store.get(self.run_id, 'effect', effect['id'])

    def _schedule_retry(self, effect):
        """Enqueue one successor for a definite provider failure while the run's retry allowance lasts.

        The failed Effect is never dispatched again. The successor's identity
        is fixed by the chain root and attempt, so repeating this is a no-op.
        """
        if effect['kind'] not in {'lab', 'meta'} or effect['status'] != 'failed_definite':
            return None
        work = self.store.get(self.run_id, 'work', effect['work_id'])
        attempt = work['inputs'].get('retry_attempt', 0) + 1
        if (work['kind'] not in {'lab', 'meta_discovery', 'meta_disposition'}
                or attempt > self.config.get('model_failure_retries', 0)):
            return None
        receipt = json.loads(self.store.read_blob(effect['result_ref']))
        if receipt.get('terminal_received') is not True or receipt.get('never_dispatched'):
            return None
        if work['issue_id'] and self.store.get(self.run_id, 'issue', work['issue_id'])['status'] != 'open':
            return None
        root_id = work['inputs'].get('retry_of', work['id'])
        key = 'retry:%d:%s' % (attempt, root_id)
        return self.controller.enqueue(self.run_id, work['kind'], key, key, issue_id=work['issue_id'],
            inputs={**work['inputs'], 'retry_of': root_id, 'retry_attempt': attempt}, priority=work['priority'])

    def enqueue_lab(self, trigger_id, evidence_ids=()):
        """Explicitly start a bounded Lab step; no implicit completed-goal claim."""
        for fid in evidence_ids:
            self.store.get(self.run_id, 'fact', fid)
        return self.controller.enqueue(self.run_id, 'lab', trigger_id, 'lab-start:' + trigger_id,
            inputs={'evidence_ids': list(evidence_ids), 'required_evidence_ids': list(evidence_ids)})

    def run_lab(self, work_id, executor, reservations):
        if self.store.get(self.run_id, 'work', work_id)['kind'] != 'lab':
            raise ValueError('Lab executor requires Lab-owned Work')
        return self.run_meta(work_id, executor, reservations)

    def _commit_agent_turn(self, effect, receipt):
        turn_id = effect.get('metadata', {}).get('agent_turn_id')
        if not turn_id:
            return None
        turn = self.store.get(self.run_id, 'agent_turn', turn_id)
        if turn['status'] in {'committed', 'rejected', 'cancelled'}:
            return turn
        if receipt.get('native_writer_quiesced') is False or receipt.get('recovery_requires_fencing'):
            raise ValueError('Native writer has not quiesced; reconcile or fence it before committing')
        # Validate the actual input binding before any private state can change.
        if (receipt.get('sent_packet_sha256') != effect['packet_hash']
                or receipt.get('sent_prompt_sha256') != digest(json.loads(self.store.read_blob(effect['packet_ref']))['prompt'].encode('utf-8'))):
            raise ValueError('Turn receipt does not certify its admitted input')
        if receipt.get('protocol_valid') is False:
            raise ValueError('Runtime protocol violation requires session reconciliation')
        if receipt.get('synthetic') and self.config.get('execution_mode') == 'live':
            raise ValueError('A scripted fixture cannot certify a live persistent session')
        delta = None
        response = receipt.get('response')
        if isinstance(response, dict) and 'working_memory_delta' in response:
            delta = response['working_memory_delta']
            if isinstance(delta, str):
                try:
                    delta = transport_json(delta)
                except (ValueError, TypeError):
                    delta = {'invalid_memory_json': delta}
        journal = receipt.get('journal')
        journal_state = None
        if journal:
            from .journal import read_journal
            journal_state = read_journal(journal['path'])
            if (journal_state['durable_seq'] != journal['durable_seq']
                    or journal_state['head_hash'] != journal['head_hash'] or journal_state.get('gaps')):
                raise ValueError('Execution journal differs from the certified terminal prefix')
        def commit(tx):
            out = self.agents.commit_turn_tx(tx, turn_id, terminal_ref=effect['result_ref'],
                working_memory_delta=delta,
                expected_memory_revision=effect['metadata']['expected_memory_revision'],
                provider_thread_id=receipt.get('provider_thread_id'),
                context_manifest=receipt.get('context_manifest'))
            if journal_state:
                jid = identity('journal_cursor', effect['id'])
                row = {'kind': 'journal_cursor', 'effect_id': effect['id'],
                    'path': journal['path'], 'head_hash': journal['head_hash'],
                    'durable_seq': journal['durable_seq'], 'applied_seq': journal['durable_seq']}
                tx.insert('registration', jid, row)
            return out
        return self._mutate('commit-agent-turn:' + turn_id, 'agent_turn_committed', commit,
            {'turn_id': turn_id, 'terminal_ref': effect['result_ref']})

    def _block_agent_turn(self, effect, reason):
        turn_id = effect.get('metadata', {}).get('agent_turn_id')
        if not turn_id:
            return
        def block(tx):
            turn = tx.get('agent_turn', turn_id)
            session = tx.get('agent_session', turn['session_id'])
            if turn['status'] in {'committed', 'rejected', 'cancelled'} or session['active_turn_id'] != turn_id:
                return session
            return tx.update('agent_session', session['id'], {'status': 'recovery_required',
                'recovery_reason': reason})
        self._mutate('block-agent-turn:' + turn_id + ':' + digest(reason), 'agent_session_recovery_required',
            block, {'turn_id': turn_id, 'effect_id': effect['id'], 'reason': reason})

    def apply_meta(self, effect_id):
        """Replay a delivered proposal safely after a crash; never call the model here."""
        self.store.verify_history(self.run_id)
        pid = identity('proposal', effect_id)
        existing = self.store.list(self.run_id, 'proposal', id=pid)
        if existing and existing[0]['status'] in {'applied', 'rejected', 'blocked_stale', 'blocked_action'}:
            return existing[0]
        effect = self.store.get(self.run_id, 'effect', effect_id)
        if effect['kind'] not in {'lab', 'meta'} or effect['status'] != 'succeeded' or effect.get('accounting_unknown'):
            raise ValueError('Only certified, cost-reconciled Meta responses can act')
        turn_id = effect.get('metadata', {}).get('agent_turn_id')
        if turn_id and self.store.get(self.run_id, 'agent_turn', turn_id)['status'] == 'rejected':
            return self._mutate('discard-proposal:' + pid, 'agent_proposal_discarded',
                lambda tx: tx.update('proposal', pid, {'status': 'rejected', 'error': 'Agent turn explicitly discarded'})
                if tx.find('proposal', id=pid) else tx.insert('proposal', pid, {
                    'effect_id': effect_id, 'issue_id': effect['issue_id'], 'status': 'rejected',
                    'command': None, 'error': 'Agent turn explicitly discarded', 'delivery_ref': None,
                    'result_ref': None, 'initial_review_ref': None}), {'effect_id': effect_id})
        packet = json.loads(self.store.read_blob(effect['metadata']['plan_ref']))
        packet['envelope_bytes'] = self.store.read_blob(effect['packet_ref'])
        if digest(packet['envelope_bytes']) != packet['packet_sha256']:
            raise ValueError('Final model packet identity differs from planned delivery')
        receipt = json.loads(self.store.read_blob(effect['result_ref']))
        if receipt.get('protocol_valid') is False:
            raise ValueError('Completed invocation violated its runtime/session protocol; reconcile before use')
        pages = present_delivery(packet, effect_id=effect_id,
            sent_packet_sha256=receipt.get('sent_packet_sha256'), sent_prompt_sha256=receipt.get('sent_prompt_sha256'),
            receipt_ref=effect['receipt_ref'])
        self._commit_agent_turn(effect, receipt)
        if self.scientific_memory:
            self.scientific_memory.apply_updates(effect,packet,receipt.get('response'))
        if not existing:
            response = receipt.get('response')
            request = None
            command = response.get('command', {}) if isinstance(response, dict) else {}
            try:
                if not isinstance(command, dict):
                    raise ValueError('Meta command must be a closed JSON object')
                if command.get('action') in {'narrow_request', 'replan_request'}:
                    request = self.store.get(self.run_id, 'request', command['payload']['request_id'])
                validated = validate_command(response, packet,
                    current_versions=self.versions(effect['issue_id']),
                    authorized_sources=self.config.get('authorized_sources', []),
                    capabilities=self.capability_catalog(), request=request)
                if validated['action'] == 'request_diagnostic':
                    self._admit_tool_inputs(validated['payload'])
                research_response = self.improvements.validate(response,packet) if self.improvements else {}
                work = self.store.get(self.run_id, 'work', effect['work_id'])
                allowed = work['inputs'].get('allowed_action')
                action_map = {'replan': 'replan_request', 'ask_user': 'ask_user', 'close_unresolved': 'close_issue',
                              'blocked_external': 'close_issue', 'narrow_request': 'narrow_request'}
                if allowed and validated['action'] != action_map[allowed]:
                    raise ValueError('Wait disposition attempted an action outside its bounded expiry plan')
                if allowed == 'close_unresolved' and validated['payload'].get('resolution') != 'closed_unresolved':
                    raise ValueError('Expiry cannot silently turn an unresolved issue into a verified conclusion')
                error = None
            except (ValueError, TypeError, KeyError) as exc:
                validated, error, research_response = None, str(exc), {}
            def admit(tx):
                result = tx.insert('proposal', pid, {'effect_id': effect_id, 'issue_id': effect['issue_id'],
                    'status': 'rejected' if error else 'admitted', 'command': validated, 'error': error,
                    **({'research_response':research_response} if self.improvements else {}),
                    'delivery_ref': tx.blob(pages), 'result_ref': None,
                    'initial_review_ref': tx.blob(initial_review_receipt(packet, response, effect_id=effect_id,
                        response_ref=effect['receipt_ref'])) if not error and effect['metadata']['mode'] == 'discovery' and not effect['metadata'].get('agent_session_id') else None})
                if error:
                    self._repair(tx, effect['issue_id'], pid, error, effect_id)
                return result
            proposal = self._mutate('admit:' + pid, 'meta_proposal_admitted', admit,
                                   {'effect_id': effect_id, 'receipt_ref': effect['receipt_ref']})
            if error:
                return proposal
        else:
            proposal = existing[0]
        # Re-entering our own partially committed route is safe. Intervening
        # independent issue changes invalidate the still-unapplied proposal.
        iid = proposal['issue_id']
        if iid:
            stale = False
            for event in self.store.db.execute('SELECT operation_key,changes_ref FROM events WHERE run_id=? AND seq>?',
                    (self.run_id, proposal['created_seq'])):
                changes = json.loads(self.store.read_blob(event['changes_ref']))
                if any(c['kind'] == 'issue' and c['document']['id'] == iid for c in changes):
                    if event['operation_key'] != 'route:' + pid and not event['operation_key'].startswith('route:' + pid + ':'):
                        stale = True
            if stale:
                return self._blocked_proposal(proposal, 'blocked_stale', 'Issue changed after proposal admission')
        # Every substep has a stable operation key. An interrupted route remains
        # admitted and can be resumed without either reissuing a call or guessing.
        try:
            result = self._route(proposal)
            if self.improvements:
                self.improvements.apply_responses(proposal,result)
        except (ValueError, KeyError, TypeError) as exc:
            # Data-shaped routing errors end the proposal; storage failures
            # (sqlite3.Error, unrelated OSError) propagate and leave it admitted.
            return self._blocked_proposal(proposal, 'blocked_action', str(exc))
        return self._mutate('applied:' + pid, 'meta_proposal_applied',
            lambda tx: tx.update('proposal', pid, {'status': 'applied', 'result_ref': tx.blob(result)}),
            {'proposal_id': pid})

    def _blocked_proposal(self, proposal, status, reason):
        def apply(tx):
            result = tx.update('proposal', proposal['id'], {'status': status, 'error': reason})
            self._repair(tx, proposal['issue_id'], proposal['id'], reason, proposal['effect_id'])
            return result
        return self._mutate('blocked:' + proposal['id'], 'proposal_blocked', apply,
                            {'proposal_id': proposal['id'], 'status': status, 'reason': reason})

    def _admit_tool_inputs(self, payload):
        # Dry-run the loaded adapter's side-effect-free prepare so that inputs
        # it can never accept are refused here instead of poisoning the queue.
        adapter = self.adapters.by_id.get(payload['capability_id'])
        if adapter is None:
            return
        try:
            adapter.prepare(json.loads(payload['inputs_json']))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('Tool inputs rejected by ' + payload['capability_id'] + ': ' + str(exc)) from exc

    def _repair(self, tx, iid, pid, reason, effect_id):
        # One repair opportunity per issue, independent of scientific evidence.
        if iid and tx.get('issue', iid)['status'] == 'open' and not tx.find('registration', id='repair:' + iid):
            tx.insert('registration', 'repair:' + iid, {'proposal_id': pid, 'reason': reason})
            self.controller._work(tx, iid, 'meta_disposition', 'repair:' + pid,
                                  inputs={'routing_error': reason}, priority=0)
        if iid is None:
            # A Lab proposal has no issue; it gets one repair turn, and a
            # repair turn (inputs carry repair_of) never gets another.
            source = tx.get('work', tx.get('effect', effect_id)['work_id'])
            if source['kind'] == 'lab' and 'repair_of' not in source['inputs']:
                self.controller._work(tx, None, 'lab', 'repair:' + pid,
                                      inputs={**source['inputs'], 'routing_error': reason, 'repair_of': pid})

    def _park(self, iid, dep, key, *, deadline=None, expiry='replan'):
        # Deadline is derived from the persisted proposal event time, not retry time.
        return self.controller.park(self.run_id, iid, dep['id'], dep['producer'], dep['revision'],
            {'kind': 'version_changed'}, deadline, expiry, key)

    def _route(self, proposal):
        command, key = proposal['command'], 'route:' + proposal['id']
        action, p, iid = command['action'], command['payload'], command['issue_id']
        base = identity('object', proposal['id'])
        event_time = self.store.db.execute('SELECT created_at FROM events WHERE run_id=? AND seq=?',
            (self.run_id, proposal['created_seq'])).fetchone()[0]
        deadline = event_time + self.config.get('wait_timeout_seconds', 3600)
        c, run = self.controller, self.run_id
        if action == 'recommend_change' and self.improvements:
            return self.improvements.recommend(proposal)
        if action == 'no_action':
            return {'outcome': 'no_action', 'reason': p['reason']}
        if action == 'propose_issue':
            return c.open_issue(run, base, [p['problem_kind'], p['scope'], p['title']], p['hypothesis'], p['scope'],
                command['evidence_ids'], p['competing_explanations'],
                [p['discriminating_check'], p['falsifying_observation']], key)
        if action in {'request_evidence', 'narrow_request', 'replan_request'}:
            q = p if action == 'request_evidence' else p['replacement']
            req = c.request(run, iid, base, q['query'], q['source_id'],
                {'object_ids': q['object_ids'], 'fields': q['fields'], 'max_pages': q['max_pages'], 'max_bytes': q['max_bytes']},
                key + ':request', supersedes=p.get('request_id'), source_version=q['source_version'], query_spec=q)
            dep = c.dependency(run, base, q['source_id'], {'status': 'pending', 'request_id': base}, key + ':dependency')
            producer = c.enqueue(run, 'evidence_retrieve', base, key + ':work',
                inputs={'issue_id': iid, 'request_id': base, 'query_spec': q})
            wait = self._park(iid, dep, key + ':park', deadline=deadline)
            return {'request_id': req['id'], 'work_id': producer['id'], 'wait_id': wait['id']}
        if action == 'ask_user':
            dep = c.dependency(run, base, 'user', {'status': 'pending'}, key + ':dependency')
            question = c.ask_user(run, iid, base, p['question'], base, key + ':question')
            self._mutate(key + ':question-options', 'question_options_saved',
                lambda tx: tx.update('question', base, {'options': p['options'], 'reason_user_needed': p['reason_user_needed']}), p)
            wait = self._park(iid, dep, key + ':park', deadline=deadline, expiry='close_unresolved')
            return {'question_id': question['id'], 'wait_id': wait['id']}
        if action == 'request_capability':
            prior = self.store.list(run, 'registration', kind='capability_build_request', issue_id=iid)
            if any(r['id'] != base and r['proposal']['capability_id'] == p['capability_id']
                   and r.get('evidence_revision') == command['expected_versions']['evidence_revision'] for r in prior):
                raise ValueError('An unchanged capability request cannot renew the same unresolved investigation')
            intent = self._mutate(key + ':intent', 'engineering_requested', lambda tx: tx.insert('registration', base,
                {'kind': 'capability_build_request', 'issue_id': iid, 'status': 'requested', 'proposal': p,
                 'dependency_id': base, 'evidence_revision': command['expected_versions']['evidence_revision']}), p)
            dep = c.dependency(run, base, 'engineering_executor', {'status': 'requested'}, key + ':dependency')
            work = c.enqueue(run, 'capability_build', base, key + ':work',
                inputs={'issue_id': iid, 'build_intent_id': intent['id']})
            wait = self._park(iid, dep, key + ':park', deadline=deadline, expiry='ask_user')
            return {'build_intent_id': intent['id'], 'work_id': work['id'], 'wait_id': wait['id']}
        if action == 'request_diagnostic':
            inputs = {**p, 'issue_id': iid, 'dependency_id': base,
                **({'lab_continuation': True, 'source_proposal_id': proposal['id']}
                   if command['mode'] == 'lab' else {})}
            if self.improvements and proposal.get('research_response', {}).get('recommendation_responses'):
                def create_diagnostic(tx):
                    previous = tx.find('dependency', id=base)
                    if previous:
                        if previous[0]['producer'] != p['capability_id']:
                            raise ValueError('Diagnostic dependency producer changed')
                    else:
                        value = {'status': 'pending'}
                        tx.insert('dependency', base, {'producer': p['capability_id'], 'value': value,
                            'content_hash': digest(value), 'revision': 1})
                    return c._work(tx, None, 'diagnostic', base, inputs=inputs)
                # A stale recommendation must roll back both the dependency
                # and the runnable Work, not merely reject the adoption label.
                self.improvements.apply_responses(proposal, None, create_result=create_diagnostic)
                # Older successful response transactions returned None. Their
                # stable Work identity still makes recovery backward compatible.
                work = self.store.get(run, 'work', identity('work', None, 'diagnostic', base))
                dep = self.store.get(run, 'dependency', base)
            else:
                dep = c.dependency(run, base, p['capability_id'], {'status': 'pending'}, key + ':dependency')
                work = c.enqueue(run, 'diagnostic', base, key + ':work', inputs=inputs)
            if iid:
                self._park(iid, dep, key + ':park', deadline=deadline)
            return work
        if action == 'propose_trial':
            # plan_ref is model-supplied: a reference to nothing, or to
            # something other than a JSON object, is a proposal error.
            try:
                spec = json.loads(self.store.read_blob(p['plan_ref']))
            except (FileNotFoundError, json.JSONDecodeError):
                spec = None
            if not isinstance(spec, dict):
                raise ValueError('Trial plan_ref does not name a stored trial specification')
            kinds = {'orchestration_policy': 'policy', 'tool_capability': 'capability', 'scientific_protocol': 'scientific_protocol'}
            version_keys = {'policy': 'policy', 'capability': 'capability', 'scientific_protocol': 'scientific_protocol'}
            kind = kinds[p['trial_kind']]
            if (spec['intervention_kind'] != kind or spec['baseline_versions'][version_keys[kind]] != p['baseline_version']
                    or spec['candidate_versions'][version_keys[kind]] != p['candidate_version']):
                raise ValueError('Trial proposal differs from its frozen executable plan')
            trial = self.trial_engine_factory(self.store, run).register(base, spec, key=key + ':trial')
            self._mutate(key + ':trial-issue', 'trial_issue_bound',
                lambda tx: tx.update('trial', base, {'issue_id': iid}), {'trial_id': base, 'issue_id': iid})
            dep = c.dependency(run, base, 'trial_engine', {'status': 'registered'}, key + ':dependency')
            work = c.enqueue(run, 'trial_prepare', base, key + ':work', inputs={'issue_id': iid, 'trial_id': base})
            self._park(iid, dep, key + ':park', deadline=deadline, expiry='ask_user')
            return {'trial_id': trial['id'], 'work_id': work['id']}
        if action == 'park':
            wake = p['wake_condition']
            if wake != 'version_changed' and not wake.startswith('status:'):
                raise ValueError('Wake condition must be executable: version_changed or status:<status>')
            condition = {'kind': 'version_changed'} if wake == 'version_changed' else {'kind': 'status', 'status': wake[7:]}
            return c.park(run, iid, p['dependency_id'], p['producer_id'], p['observed_version'], condition,
                datetime.fromisoformat(p['deadline_at']).timestamp(),
                {'narrow_request': 'narrow_request', 'replan_request': 'replan', 'close_unresolved': 'close_unresolved', 'escalate': 'ask_user'}[p['expiry_action']], key)
        if action == 'close_issue':
            return c.close_issue(run, iid, p['resolution'], p['reason'], key, evidence_ids=command['evidence_ids'])
        raise ValueError('Unregistered action')

    def ingest_delivery(self, request_id, content, provenance, *, kind='raw', page=0, source_version=1):
        if not isinstance(provenance, dict) or not provenance:
            raise ValueError('Delivery requires a structured source receipt')
        if kind == 'summary' and (not isinstance(provenance.get('summary_of'), list)
                or not provenance['summary_of'] or any(not isinstance(x, str) or not x for x in provenance['summary_of'])):
            raise ValueError('A summary must identify the original sources it summarizes')
        req = self.store.get(self.run_id, 'request', request_id)
        spec = req['query_spec']
        if page >= spec['max_pages']:
            raise ValueError('Delivery exceeds frozen request page bound')
        did = identity('delivery', request_id, page, source_version)
        make_evidence(did, encode(content).decode(), kind='summary' if kind == 'summary' else 'source_statement',
                      source=encode(provenance).decode(), summary_of=provenance.get('summary_of', []) if kind == 'summary' else [])
        old = self.store.list(self.run_id, 'delivery', request_id=request_id)
        consumed = sum(len(self.store.read_blob(d['content_ref'])) for d in old if d['id'] != did)
        if consumed + len(encode(content)) > spec['max_bytes']:
            raise ValueError('Delivery exceeds frozen request byte bound')
        delivery = self.controller.deliver(self.run_id, request_id, did, content, provenance, 'ingest:' + did,
                                          kind=kind, page=page, source_version=source_version)
        # Pointer/summary are preserved as such and never promoted to raw facts.
        def attach(tx):
            iid = req['issue_id']; issue = tx.get('issue', iid)
            fact = tx.insert('fact', did, {'kind': 'raw_fact' if kind == 'raw' else 'source_statement',
                'content_ref': tx.blob({'delivery_kind': kind, 'content': content}),
                'provenance': {**provenance, 'delivery_id': did, 'source_version': source_version, 'delivery_kind': kind}})
            tx.update('issue', iid, {'evidence_ids': issue['evidence_ids'] + [did],
                                   'evidence_revision': issue['evidence_revision'] + 1})
            for work in tx.find('work', kind='evidence_retrieve', status='ready'):
                if work['inputs'].get('request_id') == request_id:
                    tx.update('work', work['id'], {'status': 'succeeded', 'outcome_ref': tx.blob({'delivery_id': did})})
            return fact
        self._mutate('attach:' + did, 'delivered_evidence_attached', attach, {'delivery_id': did})
        self.controller.dependency(self.run_id, request_id, req['source'],
            {'status': 'delivered', 'delivery_id': did, 'kind': kind}, 'delivery-dependency:' + did)
        self.controller.tick(self.run_id)
        return delivery

    def accept_capability(self, work_id, spec, path, manifest, artifact_sha256):
        work = self.store.get(self.run_id, 'work', work_id)
        if work['kind'] != 'capability_build':
            raise ValueError('Capability implementation needs an admitted engineering Work')
        intent_id = work['inputs']['build_intent_id']
        intent = self.store.get(self.run_id, 'registration', intent_id)
        if (spec['capability_id'] != intent['proposal']['capability_id'] or
                spec['scientific_impact'] != intent['proposal']['affects_scientific_results']):
            raise ValueError('Executable delivery changes the requested capability or scientific impact')
        registry = CapabilityRegistry(self.store, self.run_id)
        request_id = 'acceptance:' + intent_id
        registry.request(request_id, spec, key='engineering-spec:' + intent_id)
        packet = registry.packet(request_id, path, manifest, execution_authorization_sha256=artifact_sha256)
        count, limits = len(spec['cases']), spec['limits']
        reservations = {'model_calls': 0, 'tool_calls': count, 'tokens': 0,
            'cpu_seconds': count * limits['cpu_seconds_per_case'], 'wall_seconds': count * limits['wall_seconds_per_case']}
        effect = self.controller.prepare_effect(self.run_id, work_id, 'capability_acceptance', packet, reservations,
            'prepare-capability:' + work_id, metadata={'capability_request_id': request_id, 'issue_id': intent['issue_id']})
        effect = self.store.get(self.run_id, 'effect', effect['id'])
        if effect['status'] == 'prepared':
            effect = self.controller.dispatch_effect(self.run_id, effect['id'], execute_acceptance)
        else:
            effect = self.store.get(self.run_id, 'effect', effect['id'])
        if effect['status'] != 'succeeded':
            return {'effect': effect, 'capability': None}
        capability = self.resume_capability(effect['id'])
        return {'effect': effect, 'capability': capability}

    def resume_capability(self, effect_id):
        effect = self.store.get(self.run_id, 'effect', effect_id)
        if effect['kind'] != 'capability_acceptance' or effect['status'] != 'succeeded' or effect.get('accounting_unknown'):
            raise ValueError('Capability recovery requires a completed reconciled acceptance Effect')
        work = self.store.get(self.run_id, 'work', effect['work_id'])
        intent_id = work['inputs']['build_intent_id']
        capability = CapabilityRegistry(self.store, self.run_id).accept(effect['metadata']['capability_request_id'],
            effect_id, key='accept-capability:' + work['id'])
        self._publish_capability(intent_id, effect_id, capability)
        return capability

    def _publish_capability(self, intent_id, effect_id, capability):
        intent = self.store.get(self.run_id, 'registration', intent_id)
        fid = identity('validation', effect_id)
        def apply(tx):
            issue = tx.get('issue', intent['issue_id'])
            tx.insert('fact', fid, {'kind': 'validation', 'content_ref': tx.blob(capability),
                'provenance': {'effect_id': effect_id, 'acceptance': 'actual reviewed adapter execution'}})
            tx.update('issue', issue['id'], {'evidence_revision': issue['evidence_revision'] + 1,
                'evidence_ids': issue['evidence_ids'] + [fid]})
            return tx.update('registration', intent_id, {'status': capability['status'], 'acceptance_effect_id': effect_id})
        self._mutate('publish-capability:' + effect_id, 'capability_validation_attached', apply,
                     {'effect_id': effect_id, 'capability_id': capability.get('id'), 'status': capability['status']})
        self.controller.dependency(self.run_id, intent_id, 'engineering_executor',
            {'status': capability['status'], 'acceptance_effect_id': effect_id}, 'capability-dependency:' + effect_id)
        self.controller.tick(self.run_id)

    def recover(self):
        effects = self.controller.recover(self.run_id)
        agent_recovery, errors = [], []
        # One failing item must not abort the whole recovery. Its stored state
        # is left untouched and the failure is reported for the operator.
        def failed(stage, item_id, exc):
            errors.append(recovery_error(stage, item_id, exc))
        # The outer receipt may be absent even though the CLI already completed.
        # Recover only this attempt's owned durable journal; never resend input.
        for effect in self.store.list(self.run_id, 'effect'):
            if effect['kind'] not in {'lab', 'meta'} or not effect.get('metadata', {}).get('agent_session_id'):
                continue
            try:
                prior = json.loads(self.store.read_blob(effect['result_ref'])) if effect.get('result_ref') else {}
                if (effect['status'] not in {'dispatching', 'unknown'}
                        and not (effect['status'] in {'succeeded', 'failed_definite'}
                            and prior.get('recovered_from_journal')
                            and (effect.get('accounting_unknown') or prior.get('recovery_requires_fencing')))):
                    continue
                try:
                    receipt = recover_codex_receipt(effect, self.store.read_blob(effect['packet_ref']), self.store.root / 'calls')
                    if receipt is not None:
                        effects.append(self.controller.reconcile(self.run_id, effect['id'], receipt,
                            'journal-reconcile:' + effect['id'] + ':' + digest(receipt)))
                except (RecoveryBlocked, ValueError, OSError) as exc:
                    self._block_agent_turn(effect, str(exc))
                    agent_recovery.append({'effect_id': effect['id'], 'status': 'blocked', 'reason': str(exc)})
            except Exception as exc:
                failed('agent_effect', effect['id'], exc)
        # Opt-in failure policies also cover calls settled before a restart.
        for effect in self.store.list(self.run_id, 'effect'):
            if effect['kind'] not in {'lab', 'meta'} or effect['status'] not in {'succeeded', 'failed_definite'}:
                continue
            try:
                self._schedule_retry(self._apply_usage_policy(effect))
            except Exception as exc:
                failed('agent_effect', effect['id'], exc)
        proposals = []
        for delivery in self.store.list(self.run_id, 'delivery', status='materialized'):
            # Packet presentation records are separate from source Deliveries.
            if not delivery.get('request_id'):
                continue
            try:
                self.ingest_delivery(delivery['request_id'], json.loads(self.store.read_blob(delivery['content_ref'])),
                    delivery['provenance'], kind=delivery['kind'], page=delivery['page'], source_version=delivery['source_version'])
            except Exception as exc:
                failed('delivery', delivery['id'], exc)
        for effect in self.store.list(self.run_id, 'effect', kind='capability_acceptance', status='succeeded'):
            if not effect.get('accounting_unknown'):
                try:
                    self.resume_capability(effect['id'])
                except Exception as exc:
                    failed('capability', effect['id'], exc)
        adapter_results = self.recover_adapters(errors=errors)
        if self.improvements:
            for effect in self.store.list(self.run_id,'effect',status='failed_definite'):
                try:
                    self.improvements.failed_execution(effect)
                except Exception as exc:
                    failed('improvement_failure', effect['id'], exc)
        for trial in self.store.list(self.run_id,'trial',status='settled'):
            if trial.get('issue_id'):
                try:
                    self.publish_trial_outcome(trial['id'])
                except Exception as exc:
                    failed('trial', trial['id'], exc)
        for effect in [e for e in self.store.list(self.run_id, 'effect')
                       if e['kind'] in {'meta', 'lab'} and e['status'] in {'succeeded', 'failed_definite'}]:
            try:
                receipt = json.loads(self.store.read_blob(effect['result_ref']))
                try:
                    if receipt.get('terminal_received') or self.config.get('execution_mode') not in {'live', 'role_recall'}:
                        self._commit_agent_turn(effect, receipt)
                    elif effect['status'] == 'failed_definite':
                        self._block_agent_turn(effect, receipt.get('error') or 'Known failure has no native turn terminal')
                    if effect['status'] == 'succeeded' and not effect.get('accounting_unknown'):
                        proposals.append(self.apply_meta(effect['id']))
                except ValueError as exc:
                    self._block_agent_turn(effect, str(exc))
                    agent_recovery.append({'effect_id': effect['id'], 'status': 'blocked', 'reason': str(exc)})
            except Exception as exc:
                failed('agent_effect', effect['id'], exc)
        self.controller.tick(self.run_id)
        return {'effects': effects, 'proposals': proposals, 'adapter_results': adapter_results,
                'agent_recovery': agent_recovery, 'errors': errors}

    def execute_adapter(self, adapter_id, inputs, *, trigger_id, work_id=None):
        adapter = self.adapters.get(adapter_id)
        self._require_admitted_adapter(adapter_id)
        descriptor = adapter.descriptor()
        from pathlib import Path
        for path,expected_hash in descriptor['implementation_sources'].items():
            if self.store.put_blob(Path(path).read_bytes()) != expected_hash:
                raise ValueError('Adapter source changed while freezing the executable artifact')
        if work_id is not None:
            work = self.store.get(self.run_id, 'work', work_id)
            if (work['kind'] != 'diagnostic' or work['inputs'].get('capability_id') != adapter_id
                    or work['inputs'].get('capability_version') != adapter.version
                    or json.loads(work['inputs']['inputs_json']) != inputs):
                raise ValueError('Diagnostic invocation differs from its admitted Work')
        try:
            packet = adapter.prepare(inputs)
            reservations = adapter.reservations(packet)
        except (ValueError, KeyError, TypeError) as exc:
            if work_id is None:
                raise
            return self._reject_diagnostic(work, str(exc))
        if work_id is None:
            work = self.controller.enqueue(self.run_id, 'diagnostic', 'adapter:' + adapter_id + ':' + trigger_id,
                'adapter-work:' + adapter_id + ':' + trigger_id, inputs={'adapter_id': adapter_id, 'input': inputs})
        metadata = {'adapter_id': adapter_id, 'adapter_version': adapter.version,
            'adapter_descriptor_sha256': digest(descriptor), 'adapter_descriptor_ref': self.store.put_blob(descriptor),
            'issue_id': work['inputs'].get('issue_id'), 'dependency_id': work['inputs'].get('dependency_id'),
            'trigger_id': trigger_id}
        if self.config.get('task_plan_id'):
            from .plans import load_task_plan
            plan = load_task_plan(self.store, self.run_id, self.config['task_plan_id'])
            metadata['task_plan_id'] = self.config['task_plan_id']
            metadata['task_plan_sha256'] = digest(plan)
        effect = self.controller.prepare_effect(self.run_id, work['id'], adapter.effect_kind, packet, reservations,
            'adapter-effect:' + adapter_id + ':' + trigger_id, metadata=metadata)
        current = self.store.get(self.run_id, 'effect', effect['id'])
        self.adapters.resolve(current)
        if current['status'] == 'prepared':
            current = self.controller.dispatch_effect(self.run_id, current['id'], adapter.execute)
        return self._settled_adapter_effect(current)

    def resume_adapter_effect(self, effect):
        """Dispatch a persisted prepared adapter Effect; prepare is never called again."""
        adapter = self.adapters.resolve(effect)
        # The capability may have lost its accepted standing since prepare.
        self._require_admitted_adapter(adapter.adapter_id)
        current = self.controller.dispatch_effect(self.run_id, effect['id'], adapter.execute)
        return self._settled_adapter_effect(current)

    def _require_admitted_adapter(self, adapter_id):
        # capability_catalog() itself raises when the loaded adapter differs
        # from the accepted artifact or several verified versions compete.
        if adapter_id not in self.capability_catalog():
            raise ValueError('The executable adapter requires verified artifact acceptance before use')

    def _settled_adapter_effect(self, current):
        if current['status'] != 'succeeded' or current.get('accounting_unknown'):
            if self.improvements:
                self.improvements.failed_execution(current)
            return {'effect': current, 'result': None}
        return {'effect': current, 'result': self.publish_adapter_effect(current)}

    def _reject_diagnostic(self, work, reason):
        """Fail an admitted diagnostic whose inputs the loaded adapter refuses.

        No Effect is created and nothing is reserved. The dependency records
        the failure so a parked issue wakes; a Lab request returns to the Lab.
        """
        work_id, inputs = work['id'], work['inputs']
        if inputs.get('dependency_id'):
            self.controller.dependency(self.run_id, inputs['dependency_id'], inputs['capability_id'],
                {'status': 'failed', 'reason': reason, 'work_id': work_id},
                'diagnostic-rejected-dependency:' + work_id)
        lab_inputs = None
        if inputs.get('lab_continuation'):
            lab_inputs = {'evidence_ids': [], 'required_evidence_ids': []}
            proposal = (self.store.list(self.run_id, 'proposal', id=inputs['source_proposal_id'])
                        if inputs.get('source_proposal_id') else [])
            if proposal:
                effect = self.store.list(self.run_id, 'effect', id=proposal[0]['effect_id'])
                source = self.store.list(self.run_id, 'work', id=effect[0]['work_id']) if effect else []
                if source:
                    lab_inputs = dict(source[0]['inputs'])
            lab_inputs.update(routing_error=reason, repair_of=work_id)
        def apply(tx):
            current = tx.get('work', work_id)
            if current['status'] != 'ready':
                raise ValueError('Rejected diagnostic Work is no longer ready')
            failed = tx.update('work', work_id, {'status': 'failed',
                'outcome_ref': tx.blob({'reason': reason, 'stage': 'adapter_prepare'})})
            if lab_inputs is not None:
                self.controller._work(tx, None, 'lab', 'tool-rejected:' + work_id, inputs=lab_inputs)
            return failed
        failed = self._mutate('diagnostic-rejected:' + work_id, 'diagnostic_rejected', apply,
                              {'work_id': work_id, 'reason': reason})
        self.controller.tick(self.run_id)
        return {'effect': None, 'result': None, 'work': failed, 'rejected': reason}

    def publish_adapter_effect(self, effect):
        self.store.verify_history(self.run_id)
        actual = self.store.get(self.run_id,'effect',effect['id'])
        if (actual != effect or actual['status']!='succeeded' or actual.get('accounting_unknown')):
            raise ValueError('Publication requires the actual succeeded, reconciled persisted Effect')
        packet = self.store.read_blob(actual['packet_ref'])
        if digest(packet) != actual['packet_hash']:
            raise ValueError('Adapter input differs from its reserved packet')
        publication_id = identity('publication', effect['id'])
        existing = self.store.list(self.run_id, 'registration', id=publication_id)
        if existing:
            publication = json.loads(self.store.read_blob(existing[0]['publication_ref']))
            adapter_id = existing[0]['adapter_id']
            if effect['metadata'].get('adapter_id',adapter_id) != adapter_id:
                raise ValueError('Published evidence belongs to a different adapter')
            if existing[0]['status']=='complete':
                if self.scientific_memory:
                    self.scientific_memory.observe_publication(effect,publication)
                if self.improvements:
                    self.improvements.publication(effect,publication)
                return publication['data']
        else:
            adapter = self.adapters.resolve(effect)
            adapter_id = adapter.adapter_id
            if effect['metadata'].get('adapter_id'):
                descriptor = json.loads(self.store.read_blob(effect['metadata']['adapter_descriptor_ref']))
                if (digest(descriptor)!=effect['metadata']['adapter_descriptor_sha256'] or descriptor!=adapter.descriptor()):
                    raise ValueError('Adapter descriptor receipt differs from its pinned implementation')
            receipt = json.loads(self.store.read_blob(effect['result_ref']))
            publication = adapter.publication(effect, receipt)
            if not isinstance(publication, dict) or set(publication) != {'facts', 'conclusions', 'data'}:
                raise ValueError('Adapter publication requires facts, conclusions and data')
            if not isinstance(publication['facts'], list) or not isinstance(publication['conclusions'], list):
                raise ValueError('Adapter evidence must be explicit records')
            rows = publication['facts'] + publication['conclusions']
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {'id','kind','content','provenance'}
                        or not isinstance(row['provenance'], dict) or not row['provenance']):
                    raise ValueError('Adapter evidence needs exact identity, kind, content and provenance')
            if any(r['kind'] not in {'raw_fact','source_statement'} for r in publication['facts']):
                raise ValueError('Inferences cannot be published as raw task evidence')
            if any(r['kind'] not in {'validation','hypothesis'} for r in publication['conclusions']):
                raise ValueError('Adapter conclusions must retain their interpretive identity')
            if len({r['id'] for r in rows}) != len(rows):
                raise ValueError('Duplicate evidence identities in adapter publication')
            def publish(tx):
                for row in rows:
                    old = tx.find('fact', id=row['id'])
                    doc = {'kind': row['kind'], 'content_ref': tx.blob(row['content']), 'provenance': row['provenance']}
                    if old and any(old[0][key] != value for key,value in doc.items()):
                        raise ValueError('Adapter tried to replace immutable evidence identity')
                    if not old:
                        tx.insert('fact', row['id'], doc)
                iid = effect.get('metadata',{}).get('issue_id')
                if iid:
                    issue = tx.get('issue',iid)
                    fresh = {r['id'] for r in rows} - set(issue['evidence_ids'])
                    if fresh:
                        tx.update('issue',iid,{'evidence_ids':sorted(set(issue['evidence_ids'])|fresh),
                                              'evidence_revision':issue['evidence_revision']+1})
                return tx.insert('registration', publication_id, {'kind':'adapter_publication','effect_id':effect['id'],
                    'adapter_id':adapter.adapter_id, 'status':'published', 'publication_ref':tx.blob(publication)})
            self._mutate('adapter-publication:' + effect['id'], 'adapter_evidence_published', publish,
                         {'effect_id':effect['id'],'publication_sha256':digest(publication)})
        dep_id = effect.get('metadata',{}).get('dependency_id')
        if self.scientific_memory:
            self.scientific_memory.observe_publication(effect,publication)
        if self.improvements:
            self.improvements.publication(effect,publication)
        if dep_id:
            self.controller.dependency(self.run_id, dep_id, adapter_id,
                {'status':'completed','effect_id':effect['id']}, 'adapter-dependency:' + effect['id'])
            self.controller.tick(self.run_id)
        source_work = self.store.get(self.run_id, 'work', effect['work_id'])
        if source_work['inputs'].get('lab_continuation'):
            ids = [r['id'] for r in publication['facts'] + publication['conclusions']]
            self.controller.enqueue(self.run_id, 'lab', 'result:' + effect['id'],
                'lab-continuation:' + effect['id'], inputs={'evidence_ids': ids,
                    'required_evidence_ids': [r['id'] for r in publication['facts']],
                    'source_effect_id': effect['id']})
        if publication['facts']:
            self.observe('adapter:' + effect['id'], 'new_evidence', 0, [r['id'] for r in publication['facts']])
        self._mutate('adapter-publication-complete:'+effect['id'],'adapter_notifications_completed',
            lambda tx:tx.update('registration',publication_id,{'status':'complete'}),{'effect_id':effect['id']})
        return publication['data']

    def pending_improvements(self):
        """Public host query for bounded final result-review drain; no mutations."""
        return self.improvements.pending() if self.improvements else []

    def recover_adapters(self, errors=None):
        """Publish settled adapter Effects; ``errors`` collects other failures per Effect.

        Without an ``errors`` list, an exception other than ValueError/KeyError
        propagates as before; with one, a recovery error row (stage ``adapter``)
        is appended and the remaining Effects are still recovered.
        """
        results = []
        for effect in self.store.list(self.run_id, 'effect', status='succeeded'):
            if not (effect.get('metadata',{}).get('adapter_id') or effect['kind'] in self.adapters.legacy):
                continue
            if effect.get('accounting_unknown'):
                continue
            try:
                self.publish_adapter_effect(effect)
                results.append({'effect_id':effect['id'],'status':'published'})
            except (ValueError,KeyError) as exc:
                results.append({'effect_id':effect['id'],'status':'blocked_adapter','reason':str(exc)})
            except Exception as exc:
                if errors is None:
                    raise
                errors.append(recovery_error('adapter', effect['id'], exc))
        return results

    def settle_trial(self, trial_id, *, key):
        trial = self.trial_engine_factory(self.store, self.run_id).settle(trial_id, key=key)
        if trial['status'] == 'settled':
            self.publish_trial_outcome(trial_id)
        return trial

    def publish_trial_outcome(self, trial_id):
        trial = self.store.get(self.run_id,'trial',trial_id)
        if trial['status'] != 'settled' or not trial.get('outcome_ref'):
            raise ValueError('Only a settled trial can release a waiting investigation')
        iid = trial.get('issue_id')
        if iid is None:
            return trial
        fid = identity('trial_validation',trial_id,trial['outcome_ref'])
        def attach(tx):
            current = tx.get('trial',trial_id)
            if current['outcome_ref'] != trial['outcome_ref'] or current['status'] != 'settled':
                raise ValueError('Trial outcome changed before publication')
            tx.insert('fact',fid,{'kind':'validation','content_ref':trial['outcome_ref'],
                'provenance':{'trial_id':trial_id,'spec_sha256':trial['spec_sha256'],'source':'code_settlement'}})
            issue = tx.get('issue',iid)
            return tx.update('issue',iid,{'evidence_ids':issue['evidence_ids']+[fid],
                                         'evidence_revision':issue['evidence_revision']+1})
        self._mutate('trial-published:'+fid,'trial_outcome_published',attach,
                     {'trial_id':trial_id,'outcome_ref':trial['outcome_ref']})
        dependencies = self.store.list(self.run_id,'dependency',id=trial_id)
        if dependencies:
            self.controller.dependency(self.run_id,trial_id,'trial_engine',
                {'status':'settled','outcome_ref':trial['outcome_ref']},'trial-dependency:'+fid)
            self.controller.tick(self.run_id)
        return trial

    def status(self):
        return {'run_id': self.run_id, 'history': self.store.verify_history(self.run_id),
                'ready': self.controller.ready(self.run_id), 'issues': self.store.list(self.run_id, 'issue'),
                'waits': self.store.list(self.run_id, 'wait'), 'questions': self.store.list(self.run_id, 'question', status='pending'),
                'prepared_effects': self.store.list(self.run_id, 'effect', status='prepared'),
                'blocked_work': self.store.list(self.run_id, 'work', status='blocked'),
                'blocked_proposals': [p for p in self.store.list(self.run_id, 'proposal') if p['status'].startswith('blocked') or p['status']=='rejected'],
                'adapter_publications_pending':self.pending_adapter_publications(),
                'effects_requiring_reconciliation': [e for e in self.store.list(self.run_id, 'effect')
                    if e['status'] in {'dispatching', 'unknown'} or e.get('accounting_unknown')],
                'budgets': self.store.list(self.run_id, 'budget'),
                'agent_sessions': self.store.list(self.run_id, 'agent_session'),
                'agent_turns': self.store.list(self.run_id, 'agent_turn')}

    def _agent_available(self, work):
        if work['kind'] not in {'lab', 'meta_discovery', 'meta_disposition', 'disposition'}:
            return True
        role = 'lab' if work['kind'] == 'lab' else 'meta'
        agent = work['inputs'].get('agent_instance_id', role)
        branch = work['inputs'].get('branch_id', self.config.get('branch_id', 'main'))
        owners = self.store.list(self.run_id, 'agent_owner')
        owner = next((o for o in owners if o['scope']['role'] == role
            and o['scope']['agent_instance_id'] == agent and o['scope']['branch_id'] == branch), None)
        return owner is None or self.store.get(self.run_id, 'agent_session', owner['session_id'])['status'] == 'idle'

    def finalize(self, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('A finalization reason is required')
        def apply(tx):
            old = tx.find('registration', id='run_lifecycle')
            if old:
                return old[0]
            return tx.insert('registration', 'run_lifecycle', {'status': 'finalizing', 'reason': reason})
        state = self._mutate('finalize-run', 'run_finalizing', apply, {'reason': reason})
        for effect in self.store.list(self.run_id, 'effect', status='prepared'):
            work = self.store.get(self.run_id, 'work', effect['work_id'])
            if work['kind'] not in {'effect_reconcile', 'trial_settle'}:
                self.controller.cancel_prepared(self.run_id, effect['id'], reason, 'finalize-cancel:' + effect['id'])
        checkpoints = [self.agents.seal_checkpoint(s['id'], 'final-checkpoint:' + s['id'])
                       for s in self.store.list(self.run_id, 'agent_session', status='idle')]
        return {'lifecycle': state, 'checkpoints': checkpoints, 'state': self.status()}

    def pending_adapter_publications(self):
        pending=[]
        for effect in self.store.list(self.run_id,'effect',status='succeeded'):
            if not (effect.get('metadata',{}).get('adapter_id') or effect['kind'] in self.adapters.legacy):
                continue
            prior=self.store.list(self.run_id,'registration',id=identity('publication',effect['id']))
            if prior and prior[0]['status']=='complete':
                continue
            if prior:
                pending.append({'effect_id':effect['id'],'adapter_id':prior[0]['adapter_id'],
                                'status':'notification_pending','reason':'Committed publication requires dependency notification'})
                continue
            try:
                self.adapters.resolve(effect)
                reason='Accounting reconciliation required' if effect.get('accounting_unknown') else 'Result publication not committed'
            except ValueError as exc:
                reason=str(exc)
            pending.append({'effect_id':effect['id'],'adapter_id':effect.get('metadata',{}).get('adapter_id'),
                            'status':'blocked_adapter','reason':reason})
        return pending

    def _cancel_refused(self, effect, refusal, cancelled, blocking, blocking_ids):
        # A refused prepared Effect provably never ran; release its lease and
        # reservation. If even cancellation is refused, report it instead.
        try:
            self.controller.cancel_prepared(self.run_id, effect['id'], refusal, 'driver-cancel:' + effect['id'])
        except ValueError as exc:
            blocking_ids.add(effect['id'])
            blocking.append({'effect_id': effect['id'], 'kind': effect['kind'], 'reason': str(exc)})
            return
        cancelled.append({'effect_id': effect['id'], 'reason': refusal})

    def _driver_wait_status(self, unresolved, blocking, skipped, recovery_errors):
        """The first matching reason why nothing more can run in this call."""
        if unresolved:
            return 'blocked_external', 'Existing unknown execution retains capacity and reconciliation responsibility'
        if blocking:
            return 'blocked_prepared', blocking[0]['reason']
        if skipped:
            return 'blocked_admission', skipped[0]['reason']
        status = self.status()
        if status['adapter_publications_pending']:
            return 'blocked_adapter', None
        if recovery_errors:
            return 'blocked_recovery', recovery_errors[0]['reason']
        if status['ready'] or any(w['status'] in {'waiting','disposition_due','disposed'} for w in status['waits']):
            return 'awaiting_producer_or_deadline', None
        return 'quiescent', None

    def run_until_wait(self, meta_executor, reservations, *, max_steps=20):
        """Drain eligible Lab/Meta and registered tools without occupying wait slots.

        Trusted source/engineering producers use the delivery and Effect
        APIs. This driver never invents an adapter for their pending Work. A
        caller can invoke it again after delivery, input/capacity change, or a
        deadline. Unknown invocations are reconciled before any new dispatch.
        """
        if type(max_steps) is not int or not 1 <= max_steps <= 1000:
            raise ValueError('The cooperative driver requires a bounded positive step allowance')
        recovery_errors = self.recover()['errors']
        actions, skipped, cancelled, blocking = [], [], [], []
        selected, blocking_ids = set(), set()
        outcome, reason, steps = 'step_limit_reached', None, 0
        while steps < max_steps:
            self.controller.tick(self.run_id)
            if self.store.list(self.run_id, 'registration', id='run_lifecycle', status='finalizing'):
                outcome, reason = 'finalizing', 'New agent turns are stopped; durable recovery remains available'
                break
            unresolved = [e for e in self.store.list(self.run_id, 'effect') if e['status'] in {'dispatching','unknown'}]
            # A prepared Effect holds a lease and was never dispatched: resume
            # it before new Work. One no adapter here can run is reported.
            prepared = self.store.list(self.run_id, 'effect', status='prepared')
            effect = None
            for candidate in prepared:
                if candidate['id'] in blocking_ids:
                    continue
                if candidate['kind'] not in {'lab', 'meta'}:
                    try:
                        self.adapters.resolve(candidate)
                    except ValueError as exc:
                        blocking_ids.add(candidate['id'])
                        blocking.append({'effect_id': candidate['id'], 'kind': candidate['kind'], 'reason': str(exc)})
                        continue
                effect = candidate
                break
            held = len(unresolved) + sum(e['id'] in blocking_ids for e in prepared)
            if effect is None and held >= self.config.get('max_external', 1):
                outcome, reason = self._driver_wait_status(unresolved, blocking, skipped, recovery_errors)
                break
            if effect is not None:
                work = self.store.get(self.run_id, 'work', effect['work_id'])
            else:
                work = next((w for w in self.controller.ready(self.run_id) if w['id'] not in selected and (
                             (w['kind'] in {'lab','meta_discovery','meta_disposition','disposition'} and self._agent_available(w))
                             or (w['kind']=='diagnostic' and w['inputs'].get('capability_id') in self.adapters.by_id))),None)
            if work is None:
                outcome, reason = self._driver_wait_status(unresolved, blocking, skipped, recovery_errors)
                break
            selected.add(work['id'])
            steps += 1
            try:
                if effect is not None and effect['kind'] not in {'lab', 'meta'}:
                    result = self.resume_adapter_effect(effect)
                elif work['kind']=='diagnostic':
                    result = self.execute_adapter(work['inputs']['capability_id'],
                        json.loads(work['inputs']['inputs_json']),trigger_id=work['id'],work_id=work['id'])
                else:
                    # A resumed prepared invocation keeps the reservation it
                    # was admitted with; the caller's applies to new Work only.
                    result = self.run_meta(work['id'], meta_executor,
                                           effect['reservations'] if effect is not None else reservations)
            except (ValueError, KeyError) as exc:
                # Admission checks happen before external work. Preserve the
                # concrete reason and go on; elapsed time is not a retry signal.
                current = self.store.get(self.run_id, 'work', work['id'])
                owned = effect['id'] if effect is not None else current.get('effect_id')
                owned = self.store.get(self.run_id, 'effect', owned) if owned else None
                if owned is not None and owned['status'] == 'prepared':
                    self._cancel_refused(owned, str(exc), cancelled, blocking, blocking_ids)
                else:
                    skipped.append({'work_id': work['id'], 'reason': str(exc)})
                continue
            actions.append({'work_id': work['id'], 'effect_id': (result.get('effect') or {}).get('id'),
                'proposal_status': (result.get('proposal') or {}).get('status'),
                'work_status': (result.get('work') or {}).get('status')})
        if outcome == 'step_limit_reached' and skipped:
            # The allowance ran out; keep the concrete reason of the first skip.
            reason = skipped[0]['reason']
        report = build_report(self, skipped=skipped, recovery_errors=recovery_errors, prepared_blocking=blocking)
        if outcome == 'quiescent':
            # Nothing runs, yet an obligation is left without a scheduled owner.
            attention = report['attention']
            reason = next((name for name in ('stalled_issues', 'stopped_lab', 'sessions_requiring_recovery',
                                             'capacity_blocked_work') if attention[name]), None)
            if reason:
                outcome = 'stalled'
        result = {'status': outcome, 'reason': reason, 'steps': actions, 'state': self.status(),
                  'recovery_errors': recovery_errors, 'skipped': skipped,
                  'cancelled_prepared': cancelled, 'prepared_blocking': blocking, 'report': report}
        # Include observation sequence in the key: equivalent polls can be
        # recorded without altering a wait's evidence or scheduling version.
        token = identity('driver', self.store.verify_history(self.run_id)['events'], result)
        self._mutate(token, 'driver_checkpoint', lambda tx: tx.insert('registration', token,
            {'kind':'driver_checkpoint','status':outcome,'result_ref':tx.blob(result)}), result)
        return result
