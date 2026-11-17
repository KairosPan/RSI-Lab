"""Opt-in, domain-neutral result interpretation before the next Lab decision.

A review is a separate durable Lab turn with no executable actions. The next
decision receives the exact interpreted investigation and decision revisions,
with their complete original evidence. This certifies recorded interpretation,
not its scientific validity. Existing runs without this setting are unchanged.
"""
from .controller import identity


KIND = 'batch_interpretation_barrier'


class InterpretationBlocked(ValueError):
    """The dispatch transaction found an unmet interpretation obligation."""


class BatchInterpretations:
    def __init__(self, runtime):
        self.runtime, self.store, self.run_id = runtime, runtime.store, runtime.run_id
        values = runtime.config.get('batch_interpretation_capabilities', [])
        if (not isinstance(values, list) or len(values) > 32
                or any(not isinstance(v, str) or not v for v in values)
                or len(set(values)) != len(values)):
            raise ValueError('batch_interpretation_capabilities requires distinct adapter identities')
        if values and runtime.scientific_memory is None:
            raise ValueError('Batch interpretation requires scientific memory')
        if set(values) - set(runtime.adapters.by_id):
            raise ValueError('Batch interpretation requires loaded adapters')
        self.capabilities = set(values)

    def rows(self):
        return self.store.list(self.run_id, 'registration', kind=KIND)

    def publication(self, effect, publication):
        if effect.get('metadata', {}).get('adapter_id') not in self.capabilities:
            return None
        bid = identity('batch_interpretation', effect['id'])
        existing = self.store.list(self.run_id, 'registration', id=bid)
        if existing:
            return existing[0]
        records = [r for r in self.runtime.scientific_memory.read_history(kind='investigation')
                   if r['source_effect_id'] == effect['id'] and r['origin_run_id'] == self.run_id]
        if len(records) != 1 or not publication['facts']:
            raise ValueError('A batch interpretation needs its original investigation and results')
        source = self.store.get(self.run_id, 'work', effect['work_id'])
        scope = self.runtime.controller.diagnostic_lab_scope(self.run_id, source)
        refs = [{'id': r['id'], 'revision': r['revision']} for r in records]
        evidence = [r['id'] for r in publication['facts']]
        def create(tx):
            barrier = tx.insert('registration', bid, {'kind': KIND, 'status': 'awaiting_interpretation',
                'effect_id': effect['id'], 'scope': scope, 'investigations': refs,
                'source_evidence_ids': evidence, 'decision_refs': []})
            work = self.runtime.controller._work(tx, None, 'lab', 'interpret:' + bid,
                inputs={**scope, 'batch_interpretation_id': bid,
                        'evidence_ids': evidence, 'required_evidence_ids': evidence}, priority=2)
            return tx.update('registration', bid, {'review_work_id': work['id']})
        return self.runtime._mutate('batch-interpretation:' + effect['id'],
            'batch_interpretation_required', create, {'effect_id': effect['id'], 'scope': scope})

    def dependencies(self, work):
        if work['kind'] != 'lab' or not self.capabilities:
            return []
        scope = self.runtime.controller.lab_scope(self.run_id, work['inputs'])
        return [{'kind': 'registration', 'where': {'kind': KIND,
            'scope.branch_id': scope['branch_id'], 'scope.agent_instance_id': scope['agent_instance_id']}}]

    def _interpretations(self, barrier, *, reviewer_id=None):
        memory = self.runtime.scientific_memory
        refs = []
        for ref in barrier['investigations']:
            record = memory.get(ref['id'], ref['revision'])
            state = memory._interpretation(record)
            if state['status'] != 'interpreted' or memory._freshness(record) != 'current':
                raise ValueError('Batch result must have a current source-bound interpretation before continuation')
            decisions = []
            for decision_ref in state['decision_refs']:
                decision = memory.get(decision_ref['id'], decision_ref['revision'])
                if (decision['content'].get('result_disposition', 'interpreted') == 'interpreted'
                        and (reviewer_id is None or decision['source_effect_id'] == reviewer_id)):
                    decisions.append(decision_ref)
            if not decisions:
                raise ValueError('Each batch result needs an interpretation written by this review turn')
            refs.extend(decisions)
        return list({(r['id'], r['revision']): r for r in refs}.values())

    def _check_completed(self, barrier):
        current = {(r['id'], r['revision']) for r in self._interpretations(barrier)}
        pinned = {(r['id'], r['revision']) for r in barrier['decision_refs']}
        # Additional independent interpretations do not withdraw this one.
        # Its exact revisions must nevertheless remain current and supported
        # by their unchanged original source closure.
        if not pinned or not pinned.issubset(current):
            raise ValueError('Recorded batch interpretation changed; a new review is required')

    def context(self, work):
        if work['kind'] != 'lab' or not self.capabilities:
            return None
        bid = work['inputs'].get('batch_interpretation_id')
        if bid:
            barrier = self.store.get(self.run_id, 'registration', bid)
            if barrier.get('kind') != KIND or barrier['scope'] != self.runtime.controller.lab_scope(self.run_id, work['inputs']):
                raise ValueError('Interpretation work does not own this batch')
            phase, refs = 'interpretation', barrier['investigations']
        else:
            scope = self.runtime.controller.lab_scope(self.run_id, work['inputs'])
            rows = [r for r in self.rows() if r['scope'] == scope]
            if not rows:
                return None
            if any(r['status'] != 'interpreted' for r in rows):
                raise ValueError('Lab awaits batch interpretation')
            barrier = max(rows, key=lambda r: r['created_seq'])
            self._check_completed(barrier)
            phase, refs = 'decision', barrier['investigations'] + barrier['decision_refs']
        return {'barrier_id': barrier['id'], 'phase': phase, 'source_effect_id': barrier['effect_id'],
            'investigations': barrier['investigations'], 'decision_refs': barrier['decision_refs'],
            'required_object_ids': list(dict.fromkeys(r['id'] for r in refs)),
            'required_evidence_ids': barrier['source_evidence_ids'],
            'interpretation_is_not_validated_science': True}

    def settle_review(self, effect, packet):
        context = packet['task'].get('constraints', {}).get('batch_interpretation')
        if not context or context['phase'] != 'interpretation':
            return None
        bid = context['barrier_id']
        # Each step has a stable key. Recovery after memory commit revalidates
        # that memory, then atomically releases and wakes the decision Work.
        def settle(tx):
            barrier = tx.get('registration', bid)
            work = tx.get('work', effect['work_id'])
            if (barrier.get('kind') != KIND or effect.get('kind') != 'lab'
                    or work['inputs'].get('batch_interpretation_id') != bid
                    or barrier['scope'] != self.runtime.controller.lab_scope(self.run_id, work['inputs'])
                    or context['investigations'] != barrier['investigations']):
                raise ValueError('Interpretation effect does not own this batch review')
            if barrier['status'] == 'interpreted':
                return barrier
            updates = tx.find('registration', kind='scientific_memory_update_result', effect_id=effect['id'])
            if len(updates) != 1 or updates[0]['status'] != 'applied':
                raise ValueError('Batch review requires successfully applied scientific memory updates')
            refs = self._interpretations(barrier, reviewer_id=effect['id'])
            updated = tx.update('registration', bid, {'status': 'interpreted',
                'decision_refs': refs, 'review_effect_id': effect['id']})
            self.runtime.controller.wake_lab(tx, 'interpreted:' + bid,
                inputs={**barrier['scope'], 'evidence_ids': barrier['source_evidence_ids'],
                        'required_evidence_ids': barrier['source_evidence_ids']})
            return updated
        return self.runtime._mutate('batch-interpreted:' + bid, 'batch_interpretation_completed',
                                    settle, {'barrier_id': bid})

    def admit_execution(self, adapter_id, work=None):
        if adapter_id not in self.capabilities:
            return
        scope = (self.runtime.controller.diagnostic_lab_scope(self.run_id, work) if work else
                 self.runtime.controller.lab_scope(self.run_id, {}))
        rows = [r for r in self.rows() if r['scope'] == scope]
        if any(r['status'] != 'interpreted' for r in rows):
            raise ValueError('Cannot execute the next batch before interpreting its predecessor')
        if rows:
            self._check_completed(max(rows, key=lambda r: r['created_seq']))
