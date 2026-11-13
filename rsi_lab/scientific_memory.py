"""Source-bound scientific state, distinct from an agent's private scratchpad.

This intentionally small service uses the existing transactional registration
table. Revisions are append-only; only their head pointers change. Source facts,
execution receipts and budgets remain authoritative in their original tables.
No model call, semantic entailment check, or cross-role private-memory read is
performed here. Model interpretations always have a pending support check.
"""
from __future__ import annotations

import json
import re

from .store import digest, encode

SCHEMA = 'scientific_memory_v1'
OBJECT_KINDS = {'question', 'evidence', 'claim', 'investigation', 'method_experience', 'decision'}
RELATIONS = {'supports', 'challenges', 'context', 'derived_from'}
UPDATE_FIELDS = {'kind', 'id', 'expected_revision', 'question_ids', 'content',
                 'source_evidence_ids', 'object_refs'}
REVISION = 'scientific_memory_revision'
HEAD = 'scientific_memory_head'
EXPERIENCE_KINDS = {'claim', 'method_experience', 'decision'}
# Function words should not make a recent, unrelated receipt look relevant to
# a specific method or failure mode. Values, rather than JSON field names, are
# searched below; this remains a deterministic lexical aid, not entailment.
QUERY_STOP_WORDS = {'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from',
                    'in', 'is', 'it', 'of', 'on', 'or', 'that', 'the', 'these',
                    'this', 'to', 'was', 'were', 'with'}


def _terms(value):
    if isinstance(value, dict):
        return set().union(*(_terms(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_terms(item) for item in value)) if value else set()
    return (set(re.findall(r'[^\W_]+', value.lower())) - QUERY_STOP_WORDS
            if isinstance(value, str) else set())


def _copy(value):
    return json.loads(encode(value))


def _key(prefix, *parts):
    return 'sm_' + prefix + '_' + digest(list(parts))[:32]


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x or len(x) > 256 for x in value):
        raise ValueError(name + ' requires explicit bounded identities')
    if len(set(value)) != len(value):
        raise ValueError(name + ' contains duplicate identities')
    return value


class ScientificMemory:
    """Six-object, run-scoped service with optional trusted domain projection.

    ``identity_provider(source_store, source_run_id, effect, publication)`` returns identity rows or a dict
    ``{identities: [...], experience: {...}}``. Each identity row has identity,
    conditions, fact_id and locator. The callback is trusted domain code, never
    a model response. It may describe a batch's observed negative outcome, but
    cannot promote that observation to a validated general method.
    """

    def __init__(self, store, run_id, identity_provider=None):
        self.store, self.run_id = store, run_id
        self.identity_provider = identity_provider
        self.config = store.run(run_id)['config']

    def _rows(self, kind):
        return self.store.list(self.run_id, 'registration', kind=kind)

    def _head(self, object_id):
        rows = self.store.list(self.run_id, 'registration', id=_key('head', object_id))
        return rows[0] if rows else None

    def get(self, object_id, revision=None):
        head = self._head(object_id)
        if head is None:
            raise KeyError(object_id)
        revision = head['revision'] if revision is None else revision
        row = self.store.get(self.run_id, 'registration', _key('revision', object_id, revision))
        record = row['record']
        if row['record_sha256'] != digest(record):
            raise ValueError('Scientific memory revision hash mismatch')
        return _copy(record)

    def read_history(self, object_id=None, *, kind=None, question_id=None):
        rows = [r['record'] for r in self._rows(REVISION)]
        return sorted((_copy(r) for r in rows
                       if (object_id is None or r['id'] == object_id)
                       and (kind is None or r['kind'] == kind)
                       and (question_id is None or r['id'] == question_id or question_id in r['question_ids'])),
                      key=lambda r: (r['id'], r['revision']))

    @staticmethod
    def _revision(record, current):
        """``record`` in the form stored when it is appended after revision ``current``."""
        return _copy({**record, 'schema_version': SCHEMA, 'revision': current + 1,
                      'supersedes': None if current == 0 else {'id': record['id'], 'revision': current}})

    def _append(self, tx, record, expected_revision):
        hid = _key('head', record['id'])
        heads = tx.find('registration', id=hid)
        old = heads[0] if heads else None
        current = 0 if old is None else old['revision']
        if type(expected_revision) is not int or expected_revision != current:
            raise ValueError('Stale scientific memory revision')
        if old and old['object_kind'] != record['kind']:
            raise ValueError('Scientific object kind cannot change')
        record = self._revision(record, current)
        rid = _key('revision', record['id'], current + 1)
        tx.insert('registration', rid, {'kind': REVISION, 'schema_version': SCHEMA,
            'object_id': record['id'], 'object_kind': record['kind'],
            'record': record, 'record_sha256': digest(record)})
        fields = {'kind': HEAD, 'schema_version': SCHEMA, 'object_id': record['id'],
                  'object_kind': record['kind'], 'revision': current + 1, 'record_id': rid}
        if old:
            tx.update('registration', hid, fields, expected_version=old['version'])
        else:
            tx.insert('registration', hid, fields)
        return record

    def _record(self, kind, object_id, content, *, question_ids=(), sources=(), refs=(),
                effect_id=None, authority='model_interpretation'):
        return {'id': object_id, 'kind': kind, 'question_ids': list(question_ids),
                'content': _copy(content), 'source_refs': _copy(list(sources)),
                'object_refs': _copy(list(refs)), 'source_effect_id': effect_id,
                'origin_run_id': self.run_id, 'authority': authority,
                'support_check': {'status': 'pending', 'outcome': None},
                'validated_scope': None}

    def _question_record(self):
        qid = _key('question', self.run_id, 'frozen-task')
        return self._record('question', qid, {'question': self.config.get('goal', 'Investigate the frozen task'),
            'scope': {'task_id': self.config.get('task_id'), 'run_id': self.run_id}, 'status': 'open'},
            authority='task_contract')

    def ensure_question(self):
        record = self._question_record()
        if self._head(record['id']):
            return self.get(record['id'])
        return self.store.mutate(self.run_id, 'sm:root-question', 'scientific_question_registered',
            lambda tx: self._append(tx, record, 0), payload=record)

    def _unregistered_question(self):
        """The root question as ``ensure_question`` would append it; None once it is registered."""
        record = self._question_record()
        return None if self._head(record['id']) else self._revision(record, 0)

    def _source(self, fact_id):
        fact = self.store.get(self.run_id, 'fact', fact_id)
        if fact['kind'] not in {'raw_fact', 'source_statement'}:
            raise ValueError('Scientific sources require raw observations or attributed source statements')
        raw = self.store.read_blob(fact['content_ref'])
        provenance = fact.get('provenance', {})
        if provenance.get('scientific_memory_import') and provenance.get('original_source'):
            original = provenance['original_source']
            if original['content_sha256'] != digest(raw):
                raise ValueError('Imported scientific source content changed')
            return _copy(original)
        return {'run_id': self.run_id, 'fact_id': fact_id, 'version': fact['version'],
                'content_sha256': digest(raw), 'locator': 'full_record', 'source_kind': fact['kind']}

    def _local_source(self, ref):
        if ref['run_id'] == self.run_id:
            fid = ref['fact_id']
        else:
            alias = self.store.get(self.run_id, 'registration', _key('source_alias', ref))
            fid = alias['local_fact_id']
        actual = self._source(fid)
        if actual['content_sha256'] != ref['content_sha256'] or actual['version'] != ref['version']:
            raise ValueError('Scientific source content/version changed')
        return fid

    def _certified_effect(self, effect):
        eid = effect if isinstance(effect, str) else effect['id']
        actual = self.store.get(self.run_id, 'effect', eid)
        if actual['status'] != 'succeeded' or actual.get('accounting_unknown') or not actual.get('result_ref'):
            raise ValueError('Scientific memory requires a successful reconciled effect')
        return actual, json.loads(self.store.read_blob(actual['result_ref']))

    def _projection(self, publication, supplied=None, *, source_store=None, source_run_id=None, effect=None):
        projection = supplied if supplied is not None else (self.identity_provider(
            source_store or self.store, source_run_id or self.run_id, effect, publication) if self.identity_provider else {})
        if isinstance(projection, list):
            projection = {'identities': projection}
        if not isinstance(projection, dict) or set(projection) - {'identities', 'experience'}:
            raise ValueError('Invalid trusted domain memory projection')
        return _copy(projection)

    def _index(self, tx, rows, source_ids, effect_id, origin):
        for row in rows:
            if (not isinstance(row, dict) or set(row) - {'identity', 'conditions', 'fact_id', 'locator', 'status'}
                    or not isinstance(row.get('identity'), str) or not row['identity']
                    or not isinstance(row.get('conditions'), dict) or not row['conditions']
                    or row.get('fact_id') not in source_ids or not isinstance(row.get('locator'), str)):
                raise ValueError('Exact identities need source locators and explicit comparison conditions')
            status = row.get('status', 'completed')
            if status != 'completed':
                raise ValueError('Only completed observations enter the completed-work index')
            source = self._source(row['fact_id'])
            iid = _key('identity', row['identity'], row['conditions'], source, row['locator'])
            if not tx.find('registration', id=iid):
                tx.insert('registration', iid, {'kind': 'scientific_memory_identity', 'schema_version': SCHEMA,
                    'identity': row['identity'], 'conditions': row['conditions'], 'source_ref': source,
                    'locator': row['locator'], 'effect_id': effect_id, 'origin': origin,
                    'status': 'completed', 'independent_replication': False})

    def _observe(self, publication, *, effect_id, operation, origin, projection=None):
        question = self.ensure_question()
        effect = None if effect_id is None else self.store.get(self.run_id, 'effect', effect_id)
        projection = self._projection(publication, projection, effect=effect)
        facts = publication['facts']
        sources = [self._source(f['id']) for f in facts]
        for f, ref in zip(facts, sources):
            actual = self.store.get(self.run_id, 'fact', f['id'])
            if digest(f['content']) != ref['content_sha256'] or actual['provenance'] != f['provenance']:
                raise ValueError('Published memory evidence differs from immutable fact')
        content = {'status': 'completed', 'interpretation_status': 'awaiting_review',
                   'origin': origin, 'result_fact_ids': [f['id'] for f in facts]}
        payload = {'publication': publication, 'effect_id': effect_id, 'origin': origin, 'projection': projection}
        def apply(tx):
            records = []
            for f, source in zip(facts, sources):
                evidence = self._record('evidence', _key('evidence', self.run_id, f['id']),
                    {'source_kind': f['kind'], 'source_locator': 'full_record', 'origin': origin},
                    question_ids=[question['id']], sources=[source], effect_id=effect_id,
                    authority='recorded_source')
                records.append(self._append(tx, evidence, 0))
            if effect_id is not None or origin.get('effect_id'):
                record = self._record('investigation', _key('investigation', self.run_id, operation), content,
                    question_ids=[question['id']], sources=sources, effect_id=effect_id,
                    authority='execution_receipt')
                records.append(self._append(tx, record, 0))
            if projection.get('experience'):
                experience = projection['experience']
                if (not isinstance(experience, dict) or not isinstance(experience.get('scope'), dict)
                        or experience.get('outcome') not in {'negative', 'positive', 'mixed', 'inconclusive'}):
                    raise ValueError('Observed experience requires bounded scope and explicit outcome')
                record = self._record('method_experience', _key('experience', self.run_id, operation),
                    {**experience, 'generalization': 'Not established beyond these recorded inputs', 'origin': origin},
                    question_ids=[question['id']], sources=sources, effect_id=effect_id,
                    authority='domain_observation')
                records.append(self._append(tx, record, 0))
            self._index(tx, projection.get('identities', []), {f['id'] for f in facts}, effect_id, origin)
            # New observations can challenge a claim without an existing source
            # edge. Matching is deliberately conservative and is not entailment.
            for head in tx.find('registration', kind=HEAD):
                if head['object_kind'] not in {'claim', 'method_experience'}:
                    continue
                old = self.get(head['object_id'])
                if old['authority'] != 'model_interpretation' or question['id'] not in old['question_ids']:
                    continue
                tx.insert('registration', _key('review', operation, old['id'], old['revision']),
                    {'kind': 'scientific_memory_review_pending', 'schema_version': SCHEMA,
                     'object_id': old['id'], 'revision': old['revision'], 'source_refs': sources,
                     'reason': 'New observations for the same question; applicability not yet assessed'})
            return {'status': 'recorded', 'objects': [{'id': r['id'], 'revision': r['revision']} for r in records]}
        return self.store.mutate(self.run_id, operation, 'scientific_results_recorded', apply, payload=payload)

    def observe_publication(self, effect, publication):
        actual, receipt = self._certified_effect(effect)
        if actual['kind'] in {'lab', 'meta'}:
            raise ValueError('Model interpretation is not a scientific execution receipt')
        publications = self.store.list(self.run_id, 'registration', kind='adapter_publication', effect_id=actual['id'])
        if not publications or any(json.loads(self.store.read_blob(p['publication_ref'])) != publication for p in publications):
            raise ValueError('Scientific memory requires the registered adapter publication')
        return self._observe(publication, effect_id=actual['id'], operation='sm:publication:' + actual['id'],
            origin={'run_id': self.run_id, 'effect_id': actual['id'], 'receipt_ref': actual['result_ref'], 'historical': False})

    def observe_fact(self, fact_id, *, projection=None, origin=None):
        """Trusted host entry for task inputs or already imported historical facts."""
        fact = self.store.get(self.run_id, 'fact', fact_id)
        publication = {'facts': [{'id': fact_id, 'kind': fact['kind'],
            'content': json.loads(self.store.read_blob(fact['content_ref'])), 'provenance': fact['provenance']}],
            'conclusions': [], 'data': None}
        return self._observe(publication, effect_id=None, operation='sm:fact:' + fact_id,
            origin=origin or {'run_id': self.run_id, 'historical': True, 'type': 'task_input'}, projection=projection)

    def lookup_exact(self, identities, conditions):
        _strings(identities, 'identities')
        if not isinstance(conditions, dict) or not conditions:
            raise ValueError('Exact lookup requires explicit nonempty compatibility conditions')
        wanted = set(identities)
        result = []
        for row in self._rows('scientific_memory_identity'):
            if row['identity'] in wanted and row['conditions'] == conditions:
                try:
                    fid, state = self._local_source(row['source_ref']), 'current'
                    if any(change['source_ref'] == row['source_ref'] for change in self._rows('scientific_memory_source_change')):
                        state = 'needs_review'
                except (KeyError, ValueError, FileNotFoundError):
                    fid, state = None, 'unavailable'
                result.append({k: row[k] for k in ('identity', 'conditions', 'locator', 'origin', 'status')} |
                              {'source_evidence_id': fid, 'freshness': state})
        return result

    def mark_source_changed(self, fact_id, *, replacement_id=None, reason, key=None):
        """Trusted correction notification; facts and old judgments stay immutable."""
        source = self._source(fact_id)
        replacement = None if replacement_id is None else self._source(replacement_id)
        if not isinstance(reason, str) or not reason:
            raise ValueError('Source changes require an explicit reason')
        payload = {'source_ref': source, 'replacement_ref': replacement, 'reason': reason}
        return self.store.mutate(self.run_id, key or 'sm:source-change:' + digest(payload),
            'scientific_source_changed', lambda tx: tx.insert('registration', _key('change', payload),
                {'kind': 'scientific_memory_source_change', 'schema_version': SCHEMA, **payload}), payload=payload)

    def _freshness(self, record, seen=None, *, changes=None, pending_reviews=None):
        seen = set() if seen is None else seen
        token = (record['id'], record['revision'])
        if token in seen:
            return 'current'
        seen.add(token)
        changed = changes if changes is not None else {digest(r['source_ref']) for r in self._rows('scientific_memory_source_change')}
        pending_reviews = self._rows('scientific_memory_review_pending') if pending_reviews is None else pending_reviews
        state = 'current'
        for source in record['source_refs']:
            try:
                self._local_source(source)
            except (KeyError, ValueError, FileNotFoundError):
                return 'unavailable'
            if digest(source) in changed:
                state = 'needs_review'
        bound = {digest(ref) for ref in record['source_refs']}
        for pending in pending_reviews:
            if (pending['object_id'] == record['id'] and pending['revision'] <= record['revision']
                    and any(digest(ref) not in bound for ref in pending['source_refs'])):
                state = 'needs_review'
        for ref in record['object_refs']:
            try:
                dependency = self.get(ref['id'], ref['revision'])
                dep_state = self._freshness(dependency, seen, changes=changed, pending_reviews=pending_reviews)
                if dep_state != 'current' or self._head(ref['id'])['revision'] != ref['revision']:
                    state = 'needs_review'
            except KeyError:
                return 'unavailable'
        return state

    def _sources_for(self, record, seen=None):
        seen = set() if seen is None else seen
        token = (record['id'], record['revision'])
        if token in seen:
            return []
        seen.add(token)
        ids = [self._local_source(s) for s in record['source_refs']]
        for ref in record['object_refs']:
            ids += self._sources_for(self.get(ref['id'], ref['revision']), seen)
        return list(dict.fromkeys(ids))

    def _historical(self, record):
        return record['origin_run_id'] != self.run_id or bool(record['content'].get('origin', {}).get('historical'))

    def _interpretation(self, record, rows=None, *, changes=None, pending_reviews=None):
        rows = self._rows('scientific_memory_interpretation') if rows is None else rows
        matches = []
        for row in rows:
            if row['investigation_id'] != record['id'] or row['investigation_revision'] != record['revision']:
                continue
            # Interpretation rows are an immutable audit index, not permanent
            # completion authority. A later decision may withdraw/defer its
            # interpretation, or its evidence/dependencies may need review.
            ref = row['decision_ref']
            try:
                head = self._head(ref['id'])
                if head is None or head['revision'] != ref['revision']:
                    continue
                decision = self.get(ref['id'], ref['revision'])
                content = decision['content']
                if (decision['kind'] != 'decision'
                        or not isinstance(content.get('result_interpretation'), str)
                        or not content['result_interpretation'].strip()
                        or content.get('result_disposition', 'interpreted') != row['disposition']
                        or not any(r['id'] == record['id'] and r['revision'] == record['revision']
                                   for r in decision['object_refs'])
                        or self._freshness(decision, changes=changes, pending_reviews=pending_reviews) != 'current'):
                    continue
            except (KeyError, ValueError, OSError):
                # Missing or invalid supporting revisions cannot close review.
                continue
            matches.append(row)
        completed = [r for r in matches if r['disposition'] == 'interpreted']
        return {'status': 'interpreted' if completed else 'deferred' if matches else 'awaiting_interpretation',
                'decision_refs': [r['decision_ref'] for r in matches],
                'support_check': 'pending', 'historical': self._historical(record)}

    def review_report(self, *, include_historical=False, limit=100):
        """Report execution interpretation separately from scientific support checks.

        A source-bound interpretation is recorded, not certified correct. Imported
        outstanding historical work remains visible and is not silently described
        as having been reviewed by the new segment.
        """
        rows = self._rows('scientific_memory_interpretation')
        changes = {digest(r['source_ref']) for r in self._rows('scientific_memory_source_change')}
        pending_reviews = self._rows('scientific_memory_review_pending')
        pending, historical_pending, deferred, interpreted = [], [], [], 0
        for record in self.read_history(kind='investigation'):
            if self._head(record['id'])['revision'] != record['revision']:
                continue
            state = self._interpretation(record, rows, changes=changes, pending_reviews=pending_reviews)
            freshness = self._freshness(record, changes=changes, pending_reviews=pending_reviews)
            try:
                sources = self._sources_for(record)
            except (KeyError, ValueError, FileNotFoundError):
                sources = []
            ref = {'id': record['id'], 'revision': record['revision'], 'freshness': freshness,
                   'source_evidence_ids': sources, 'status': state['status'], 'historical': state['historical']}
            if state['status'] == 'interpreted' and freshness == 'current':
                interpreted += 1
            elif state['status'] == 'deferred':
                deferred.append(ref)
            elif state['historical']:
                historical_pending.append(ref)
            else:
                pending.append(ref)
        selected = pending + (historical_pending if include_historical else [])
        return {'pending_count': len(selected), 'current_pending_count': len(pending),
                'historical_pending_count': len(historical_pending), 'interpreted_count': interpreted,
                'pending': selected[:limit], 'pending_omitted': max(0, len(selected) - limit),
                'deferred_count': len(deferred), 'deferred': deferred[:limit],
                'support_checks': 'pending; recorded interpretation does not establish scientific validity'}

    def prepare_context(self, role, evidence_ids, query=None, budget=16384, *, read_only=False):
        """Select objects and their source closure for one request within ``budget``.

        The first call registers the root question. With ``read_only`` nothing
        is written: a root question still missing is selected as if it had
        just been appended. During ordinary decisions, current results and
        accumulated interpretations have separate, borrowable byte allowances.
        Explicit object IDs override those allowances; final review prioritizes
        current pending investigations. ``related_evidence_ids`` affects rank
        only: its source bytes remain charged and must be delivered in full.
        """
        if role not in {'lab', 'meta'}:
            raise ValueError('Scientific context requires an authorized run role')
        root = None
        if read_only:
            root = self._unregistered_question()
        else:
            self.ensure_question()
        _strings(list(evidence_ids), 'evidence_ids')
        limit = budget.get('max_bytes', 16384) if isinstance(budget, dict) else budget
        if type(limit) is not int or limit < 512:
            raise ValueError('Scientific context budget must be at least 512 bytes')
        query = {} if query is None else {'text': query} if isinstance(query, str) else query
        if not isinstance(query, dict):
            raise ValueError('Scientific query must be structured')
        # Preserve the existing blocked-context report for malformed explicit
        # object identities; preflight presents that diagnostic to the caller.
        wanted = set(query.get('object_ids', [])); questions = set(query.get('question_ids', []))
        related_sources = set(_strings(query.get('related_evidence_ids', []), 'related_evidence_ids'))
        if len(related_sources) > 128:
            raise ValueError('related_evidence_ids permits at most 128 ranking hints')
        if 'text' in query and not isinstance(query['text'], str):
            raise ValueError('Scientific query text must be a string')
        words = _terms(query.get('text', ''))
        changes = {digest(r['source_ref']) for r in self._rows('scientific_memory_source_change')}
        pending_reviews = self._rows('scientific_memory_review_pending')
        interpretation_rows = self._rows('scientific_memory_interpretation')
        revision_rows = self._rows(REVISION)
        recency = {(r['record']['id'], r['record']['revision']): r['created_seq'] for r in revision_rows}
        baseline = set(evidence_ids)
        candidates = []
        history = self.read_history()
        if root is not None:
            # A real call appends it after every existing revision.
            history.append(root)
            recency[(root['id'], root['revision'])] = max(recency.values(), default=0) + 1
        for record in history:
            if query.get('exclude_interpretations') and record['authority'] == 'model_interpretation':
                continue
            if query.get('final_review_only') and not query.get('include_historical', False) and self._historical(record):
                continue
            current = record is root or self._head(record['id'])['revision'] == record['revision']
            negative = record['content'].get('outcome') == 'negative'
            if not current and not negative:
                continue
            if questions and record['id'] not in questions and not questions.intersection(record['question_ids']):
                continue
            freshness = self._freshness(record, changes=changes, pending_reviews=pending_reviews)
            relevance = len(words.intersection(_terms(record['content'])))
            interpretation = self._interpretation(record, interpretation_rows, changes=changes,
                pending_reviews=pending_reviews) if record['kind'] == 'investigation' else None
            pending = interpretation is not None and interpretation['status'] == 'awaiting_interpretation'
            try:
                direct_sources = {self._local_source(ref) for ref in record['source_refs']}
            except (KeyError, ValueError, FileNotFoundError):
                direct_sources = set()
            priority = (record['id'] in wanted, bool(query.get('final_review_only')) and pending and not self._historical(record),
                        bool(direct_sources & baseline),
                        pending and not self._historical(record), freshness != 'current', relevance,
                        negative, recency[(record['id'], record['revision'])], record['kind'] == 'question')
            experience_priority = (bool(direct_sources & (baseline | related_sources)),
                                   relevance, negative, current, freshness == 'current',
                                   recency[(record['id'], record['revision'])])
            if interpretation is not None:
                record = {**record, 'interpretation': interpretation}
            candidates.append((priority, record, freshness, experience_priority))
        candidates.sort(key=lambda row: (row[0], row[1]['id']), reverse=True)
        selected, required, omissions, available_cards = [], [], [], []
        additional_source_bytes = 0
        # Reserve a bounded manifest budget; raw sources are charged by PacketBuilder.
        available = max(0, limit - min(2048, limit // 2))
        source_sizes = {}
        for candidate in candidates:
            _, record, freshness, _ = candidate
            try:
                sources = self._sources_for(record)
                for fid in sources:
                    if fid not in baseline and fid not in source_sizes:
                        source_sizes[fid] = len(self.store.read_blob(self.store.get(self.run_id, 'fact', fid)['content_ref']))
            except (KeyError, ValueError, FileNotFoundError):
                omissions.append({'id': record['id'], 'revision': record['revision'], 'reason': 'source_unavailable'})
                continue
            card = {**record, 'freshness': freshness, 'required_evidence_ids': sources}
            available_cards.append((candidate, card))

        selected_tokens = set()

        def cost(cards, sources):
            return (len(encode({'objects': cards, 'required_evidence_ids': sources}))
                    + sum(source_sizes.get(fid, 0) for fid in sources))

        def take(item, ceiling):
            nonlocal required, additional_source_bytes
            _, card = item
            token = (card['id'], card['revision'])
            if token in selected_tokens:
                return
            proposed = list(dict.fromkeys(required + card['required_evidence_ids']))
            if cost(selected + [card], proposed) <= ceiling:
                selected.append(card); selected_tokens.add(token); required = proposed
                additional_source_bytes = sum(source_sizes.get(fid, 0) for fid in required)

        if query.get('final_review_only'):
            for item in available_cards:
                take(item, available)
        else:
            # Mandatory records may use either share. Then reserve capacity for
            # interpretations before an arbitrarily long pending queue can use
            # it. Unused shares are borrowed in the final pass, so a small or
            # empty lane does not waste the context budget.
            for item in available_cards:
                if item[1]['id'] in wanted:
                    take(item, available)
            used = cost(selected, required)
            experience_allowance = max(0, available - used) // 2
            result_allowance = max(0, available - used) - experience_allowance
            experiences = sorted((item for item in available_cards if item[1]['kind'] in EXPERIENCE_KINDS),
                                 key=lambda item: (item[0][3], item[1]['id']), reverse=True)
            for item in experiences:
                take(item, used + experience_allowance)
            result_ceiling = cost(selected, required) + result_allowance
            for item in available_cards:
                if item[1]['kind'] not in EXPERIENCE_KINDS:
                    take(item, result_ceiling)
            for item in available_cards:
                take(item, available)
        # Presentation keeps the current result first even though experience
        # capacity was reserved first. Ranking and source closure stay stable.
        selected = [card for _, card in available_cards if (card['id'], card['revision']) in selected_tokens]
        for _, card in available_cards:
            if (card['id'], card['revision']) not in selected_tokens:
                omissions.append({'id': card['id'], 'revision': card['revision'], 'reason': 'byte_budget'})
        mandatory_missing = sorted(wanted - {r['id'] for r in selected})
        updates = self.store.list(self.run_id, 'registration', kind='scientific_memory_update_result', role=role)
        last_update = max(updates, key=lambda r: r['created_seq']) if updates else None
        update_feedback = None if last_update is None else {k: last_update[k] for k in
            ('effect_id', 'status', 'reason', 'objects') if k in last_update}
        result = {'schema_version': SCHEMA, 'status': 'blocked' if mandatory_missing else 'ready',
            'authority': 'Source-bound records; interpretations are not validated scientific facts',
            'objects': selected, 'required_evidence_ids': required, 'omissions': omissions[:8],
            'last_update_result': update_feedback,
            'additional_source_bytes': additional_source_bytes,
            'retrieval': {'policy': 'pending_review_first' if query.get('final_review_only') else 'balanced_results_and_experience',
                'experience_objects': sum(r['kind'] in EXPERIENCE_KINDS for r in selected),
                'result_objects': sum(r['kind'] not in EXPERIENCE_KINDS for r in selected)},
            'omission_count': len(omissions), 'omissions_truncated': max(0, len(omissions) - 8),
            'omissions_sha256': digest(omissions), 'missing_required_objects': mandatory_missing,
            'needs_review': [{'id': r['id'], 'revision': r['revision']} for r in selected if r['freshness'] != 'current'],
            'selection_hash': digest({'role': role, 'query': query, 'selected': selected, 'omissions': omissions})}
        while len(encode({**result, 'bytes': limit})) + additional_source_bytes > limit and result['omissions']:
            result['omissions'].pop(); result['omissions_truncated'] += 1
        if len(encode({**result, 'bytes': limit})) + additional_source_bytes > limit:
            # Very small budgets return an explicit blocked report, never truncated objects.
            result = {'schema_version': SCHEMA, 'status': 'blocked', 'objects': [], 'required_evidence_ids': [],
                'omissions': [], 'omission_count': len(candidates), 'omissions_truncated': len(candidates),
                'needs_review': [], 'selection_hash': result['selection_hash'], 'reason': 'byte_budget'}
        result['bytes'] = 0
        for _ in range(3):
            result['bytes'] = len(encode(result))
        return result

    def _validate_update(self, update, delivered, selected, tx, new_questions=()):
        if not isinstance(update, dict) or set(update) != UPDATE_FIELDS:
            raise ValueError('Scientific update requires the closed update schema')
        kind, oid = update['kind'], update['id']
        if kind not in OBJECT_KINDS - {'evidence'} or not isinstance(oid, str) or not oid or len(oid) > 160:
            raise ValueError('Invalid scientific object identity or unauthorized evidence write')
        content = update['content']
        if not isinstance(content, dict) or not content or len(encode(content)) > 8192:
            raise ValueError('Scientific content must be a bounded nonempty object')
        forbidden = {'support_check', 'validated', 'validated_scope', 'verification', 'freshness', 'checked'}
        if forbidden.intersection(content):
            raise ValueError('Models cannot self-certify scientific support or freshness')
        required = {'question': 'question', 'claim': 'statement', 'investigation': 'purpose',
                    'method_experience': 'summary', 'decision': 'decision'}[kind]
        if not isinstance(content.get(required), str) or not content[required].strip():
            raise ValueError('Scientific object missing ' + required)
        if kind != 'question' and not isinstance(content.get('scope'), dict):
            raise ValueError('Scientific interpretations require explicit scope')
        if kind == 'claim' and content.get('judgment', 'tentative') not in {'tentative', 'supported_in_scope', 'contested', 'rejected'}:
            raise ValueError('Invalid scientific judgment')
        if kind == 'decision' and ('result_interpretation' in content or 'result_disposition' in content):
            if (not isinstance(content.get('result_interpretation'), str) or not content['result_interpretation'].strip()
                    or content.get('result_disposition', 'interpreted') not in {'interpreted', 'deferred'}):
                raise ValueError('Result interpretation needs explicit text and interpreted/deferred disposition')
        questions = _strings(update['question_ids'], 'question_ids')
        if kind != 'question' and not questions:
            raise ValueError('Scientific state must link a research question')
        for qid in questions:
            if qid not in new_questions and self.get(qid)['kind'] != 'question':
                raise ValueError('Question reference names another object kind')
        ids = _strings(update['source_evidence_ids'], 'source_evidence_ids')
        if kind != 'question' and not ids:
            raise ValueError('Scientific interpretations need original delivered evidence')
        if not set(ids).issubset(delivered):
            raise ValueError('Source evidence was not fully delivered in this call')
        sources = [self._source(fid) for fid in ids]
        refs = update['object_refs']
        if not isinstance(refs, list):
            raise ValueError('Scientific object references must be explicit')
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {'id', 'revision', 'relation'}
                    or ref['relation'] not in RELATIONS or type(ref['revision']) is not int
                    or (ref['id'], ref['revision']) not in selected):
                raise ValueError('Scientific object version was not delivered in this call')
            dependency = self.get(ref['id'], ref['revision'])
            if not set(self._sources_for(dependency)).issubset(delivered):
                raise ValueError('Scientific judgment lacks its original dependency closure')
        old = self._head(oid)
        if old:
            previous = self.get(oid)
            if previous['kind'] != kind:
                raise ValueError('Scientific object kind cannot change')
            if previous['authority'] != 'model_interpretation':
                raise ValueError('Model cannot overwrite recorded execution evidence or task contract')
            if (oid, update['expected_revision']) not in selected:
                raise ValueError('Revision target was not delivered in this call')
        return self._record(kind, oid, content, question_ids=questions, sources=sources, refs=refs)

    def apply_updates(self, effect, packet, response):
        actual, receipt = self._certified_effect(effect)
        if actual['kind'] not in {'lab', 'meta'} or receipt.get('protocol_valid') is False:
            raise ValueError('Only certified model responses may propose scientific interpretations')
        if response != receipt.get('response'):
            raise ValueError('Scientific updates differ from the actual model receipt')
        plan = json.loads(self.store.read_blob(actual['metadata']['plan_ref']))
        envelope = json.loads(self.store.read_blob(actual['packet_ref']))
        if (packet.get('packet_sha256') != actual['packet_ref'] or packet.get('delivery_pages') != plan['delivery_pages']
                or receipt.get('sent_packet_sha256') != actual['packet_ref']
                or receipt.get('sent_prompt_sha256') != digest(envelope['prompt'].encode())
                or packet.get('task') != plan['task']):
            raise ValueError('Scientific updates require the exact actually sent packet')
        updates = response.get('scientific_memory_updates', []) if isinstance(response, dict) else None
        payload = {'effect_id': actual['id'], 'receipt_ref': actual['result_ref'], 'updates': updates}
        def apply(tx):
            try:
                values = json.loads(updates) if isinstance(updates, str) else updates
                if not isinstance(values, list) or len(values) > 12:
                    raise ValueError('Scientific updates must be a list of at most twelve objects')
                delivered = set()
                for page in plan['delivery_pages']:
                    if page.get('complete_source') and page.get('source_kind') in {'raw_fact', 'source_statement'}:
                        ref = self._source(page['evidence_id'])
                        if (page['source_version'] != ref['version'] or page['source_sha256'] != ref['content_sha256']
                                or digest(page['content'].encode()) != ref['content_sha256']):
                            raise ValueError('Delivered source differs from immutable fact')
                        delivered.add(page['evidence_id'])
                context = plan['task'].get('scientific_memory', {})
                selected = {(r['id'], r['revision']) for r in context.get('objects', [])}
                # Validate all entries before appending any, so a rejected response is atomic.
                if len({v.get('id') for v in values if isinstance(v, dict)}) != len(values):
                    raise ValueError('A response may revise each scientific object only once')
                new_questions = {v['id'] for v in values if isinstance(v, dict)
                                 and v.get('kind') == 'question' and v.get('expected_revision') == 0}
                records = [self._validate_update(v, delivered, selected, tx, new_questions) for v in values]
                for record, update in zip(records, values):
                    head = self._head(record['id'])
                    current = head['revision'] if head else 0
                    if type(update['expected_revision']) is not int or update['expected_revision'] != current:
                        raise ValueError('Stale scientific memory revision')
            except (ValueError, KeyError, TypeError) as exc:
                result = {'status': 'rejected', 'reason': str(exc), 'objects': []}
            else:
                # Storage failures after validation roll the complete event back;
                # they must never be converted into a partially written rejection.
                written = []
                for record, update in zip(records, values):
                    record['source_effect_id'] = actual['id']
                    written.append(self._append(tx, record, update['expected_revision']))
                for record in written:
                    interpretation = record['content'].get('result_interpretation')
                    if record['kind'] != 'decision' or not isinstance(interpretation, str) or not interpretation.strip():
                        continue
                    disposition = record['content'].get('result_disposition', 'interpreted')
                    for ref in record['object_refs']:
                        target = self.get(ref['id'], ref['revision'])
                        if target['kind'] != 'investigation':
                            continue
                        tx.insert('registration', _key('interpretation', record['id'], record['revision'], ref),
                            {'kind': 'scientific_memory_interpretation', 'schema_version': SCHEMA,
                             'investigation_id': target['id'], 'investigation_revision': target['revision'],
                             'decision_ref': {'id': record['id'], 'revision': record['revision']},
                             'source_effect_id': actual['id'], 'source_refs': record['source_refs'],
                             'disposition': disposition, 'support_check': 'pending'})
                result = {'status': 'applied' if written else 'unchanged',
                          'objects': [{'id': r['id'], 'revision': r['revision'], 'freshness': self._freshness(r)} for r in written]}
            tx.insert('registration', _key('updates', actual['id']), {'kind': 'scientific_memory_update_result',
                'schema_version': SCHEMA, 'effect_id': actual['id'], 'role': actual['kind'], **result})
            return result
        return self.store.mutate(self.run_id, 'sm:updates:' + actual['id'], 'scientific_memory_updates_applied', apply, payload=payload)

    def export_bundle(self):
        records = self.read_history()
        sources = {}
        for record in records:
            for ref in record['source_refs']:
                fid = self._local_source(ref)
                fact = self.store.get(self.run_id, 'fact', fid)
                sources[digest(ref)] = {'ref': ref, 'kind': fact['kind'],
                    'content': json.loads(self.store.read_blob(fact['content_ref'])), 'provenance': fact['provenance']}
        identities = [{k: v for k, v in row.items() if k not in {'id', 'run_id', 'version', 'created_seq', 'updated_seq'}}
                      for row in self._rows('scientific_memory_identity')]
        changes = [{k: row[k] for k in ('source_ref', 'replacement_ref', 'reason')}
                   for row in self._rows('scientific_memory_source_change')]
        reviews = [{k: row[k] for k in ('object_id', 'revision', 'source_refs', 'reason')}
                   for row in self._rows('scientific_memory_review_pending')]
        interpretations = [{k: v for k, v in row.items() if k not in {'id', 'run_id', 'version', 'created_seq', 'updated_seq'}}
                           for row in self._rows('scientific_memory_interpretation')]
        data = {'schema_version': SCHEMA, 'origin_run_id': self.run_id, 'records': records,
                'sources': list(sources.values()), 'identities': identities, 'source_changes': changes,
                'pending_reviews': reviews, 'interpretations': interpretations}
        return {'data': data, 'sha256': digest(data)}

    def import_bundle(self, bundle):
        """Trusted host transfer; imported interpretations retain pending support checks."""
        if not isinstance(bundle, dict) or set(bundle) != {'data', 'sha256'} or digest(bundle['data']) != bundle['sha256']:
            raise ValueError('Scientific memory export hash mismatch')
        data = bundle['data']
        if data.get('schema_version') != SCHEMA:
            raise ValueError('Unsupported scientific memory schema')
        def apply(tx):
            for item in data['sources']:
                ref = item['ref']
                if digest(item['content']) != ref['content_sha256']:
                    raise ValueError('Imported scientific source hash mismatch')
                fid = _key('import_fact', ref)
                if not tx.find('fact', id=fid):
                    tx.insert('fact', fid, {'kind': item['kind'], 'content_ref': tx.blob(item['content']),
                        'provenance': {'scientific_memory_import': bundle['sha256'], 'original_source': ref,
                                       'original_provenance': item['provenance']}})
                aid = _key('source_alias', ref)
                if not tx.find('registration', id=aid):
                    tx.insert('registration', aid, {'kind': 'scientific_memory_source_alias',
                        'schema_version': SCHEMA, 'source_ref': ref, 'local_fact_id': fid})
            for record in sorted(data['records'], key=lambda r: (r['id'], r['revision'])):
                if record['kind'] not in OBJECT_KINDS or record['support_check']['status'] != 'pending':
                    raise ValueError('Import cannot promote scientific interpretations to checked')
                old = self._head(record['id'])
                if old and old['revision'] >= record['revision']:
                    if self.get(record['id'], record['revision']) != record:
                        raise ValueError('Imported scientific revision conflicts with local history')
                    continue
                written = self._append(tx, record, record['revision'] - 1)
                if written != record:
                    raise ValueError('Imported revision was not preserved exactly')
            for row in data.get('identities', []):
                iid = _key('import_identity', row)
                if not tx.find('registration', id=iid):
                    tx.insert('registration', iid, row)
            for row in data.get('source_changes', []):
                cid = _key('import_change', row)
                if not tx.find('registration', id=cid):
                    tx.insert('registration', cid, {'kind': 'scientific_memory_source_change', 'schema_version': SCHEMA, **row})
            for row in data.get('pending_reviews', []):
                rid = _key('import_review', row)
                if not tx.find('registration', id=rid):
                    tx.insert('registration', rid, {'kind': 'scientific_memory_review_pending', 'schema_version': SCHEMA, **row})
            for row in data.get('interpretations', []):
                iid = _key('import_interpretation', row)
                if not tx.find('registration', id=iid):
                    tx.insert('registration', iid, row)
            return {'status': 'imported', 'records': len(data['records']), 'sources': len(data['sources']),
                    'bundle_sha256': bundle['sha256']}
        return self.store.mutate(self.run_id, 'sm:import:' + bundle['sha256'], 'scientific_memory_imported', apply, payload=bundle)

    def import_history(self, source_store, source_run_id, effect_ids=None):
        """Register old settled adapter results as historical, never current effects.

        The source store may be a read-only snapshot. Its event chain should be
        verified by the host before transfer; each publication/receipt/fact
        binding is additionally checked here. This does not run an old adapter.
        """
        selected = None if effect_ids is None else set(effect_ids)
        results = []
        for registration in source_store.list(source_run_id, 'registration', kind='adapter_publication'):
            eid = registration['effect_id']
            if selected is not None and eid not in selected:
                continue
            effect = source_store.get(source_run_id, 'effect', eid)
            if effect['status'] != 'succeeded' or effect.get('accounting_unknown'):
                raise ValueError('Historical scientific effects must be reconciled')
            source_store.read_blob(effect['result_ref'])
            publication = json.loads(source_store.read_blob(registration['publication_ref']))
            origin = {'run_id': source_run_id, 'effect_id': eid, 'receipt_ref': effect['result_ref'],
                      'publication_ref': registration['publication_ref'], 'historical': True}
            transferred = _copy(publication); mapping = {}
            for fact in transferred['facts']:
                old = source_store.get(source_run_id, 'fact', fact['id'])
                if (old['content_ref'] != digest(fact['content']) or old['provenance'] != fact['provenance']
                        or old['provenance'].get('effect_id') != eid):
                    raise ValueError('Historical publication/source binding mismatch')
                mapping[fact['id']] = _key('history_fact', source_run_id, fact['id'], old['content_ref'])
                fact['id'] = mapping[fact['id']]
                fact['provenance'] = {'historical_scientific_source': origin, 'original_provenance': old['provenance']}
            projection = self._projection(publication, source_store=source_store,
                                          source_run_id=source_run_id, effect=effect)
            for row in projection.get('identities', []):
                row['fact_id'] = mapping[row['fact_id']]
            def add_facts(tx, rows=transferred['facts']):
                for fact in rows:
                    if not tx.find('fact', id=fact['id']):
                        tx.insert('fact', fact['id'], {'kind': fact['kind'], 'content_ref': tx.blob(fact['content']),
                                                       'provenance': fact['provenance']})
                return [f['id'] for f in rows]
            op = 'sm:history:' + digest(origin)
            self.store.mutate(self.run_id, op + ':facts', 'scientific_historical_sources_imported', add_facts, payload=transferred)
            results.append(self._observe(transferred, effect_id=None, operation=op, origin=origin, projection=projection))
        return {'status': 'imported', 'historical_effects': len(results), 'results': results}
