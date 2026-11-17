"""Host-owned, scoped research-state dependencies for an exact model request.

Capture belongs in the same SQLite read snapshot as request construction;
validation belongs inside the transaction admitting each new external intent.
These tokens bind semantic projections, not the run's event counter or budget.
Selectors are trusted host input, never model-selected freshness exemptions.
"""
from __future__ import annotations

import json
import re

from .store import KINDS, digest, encode

SCHEMA = 'decision_dependencies_v1'
MAX_SELECTORS = 256
MAX_BYTES = 131072
MAX_MEMBERS = 100000
_META = {'version', 'created_seq', 'updated_seq', 'run_id'}
_PATH = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,7}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_MISSING = object()


class StaleDecision(ValueError):
    """A dependency read by the frozen decision has changed."""


def _text(value, label):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(label + ' requires a bounded nonempty string')
    return value


def _path(value):
    if not isinstance(value, str) or len(value) > 256 or not _PATH.fullmatch(value):
        raise ValueError('Dependency fields require bounded dotted object paths')
    return value


def _value(row, path):
    value = row
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _predicate(value):
    if isinstance(value, dict) and any(str(key).startswith('$') for key in value):
        if len(value) != 1 or next(iter(value)) not in {'$eq', '$in', '$contains', '$exists'}:
            raise ValueError('Unknown dependency selector predicate')
        op, operand = next(iter(value.items()))
        if op == '$exists' and type(operand) is not bool:
            raise ValueError('$exists requires a boolean')
        if op == '$in' and (not isinstance(operand, list) or len(operand) > 256):
            raise ValueError('$in requires a bounded list')
    if len(encode(value)) > 16384:
        raise ValueError('Dependency predicate exceeds its bound')


def _equal(left, right):
    # JSON booleans and numbers have distinct identities (Python True == 1).
    return left is not _MISSING and encode(left) == encode(right)


def _matches(row, where):
    for path, predicate in where.items():
        actual = _value(row, path)
        if isinstance(predicate, dict) and any(key.startswith('$') for key in predicate):
            op, operand = next(iter(predicate.items()))
            if op == '$exists':
                match = (actual is not _MISSING) == operand
            elif op == '$in':
                match = any(_equal(actual, item) for item in operand)
            elif op == '$contains':
                match = isinstance(actual, list) and any(_equal(item, operand) for item in actual)
            else:
                match = _equal(actual, operand)
        else:
            match = _equal(actual, predicate)
        if not match:
            return False
    return True


def _selector(value):
    if (not isinstance(value, dict) or set(value) - {'kind', 'id', 'where', 'fields'}
            or value.get('kind') not in KINDS or ('id' in value) == ('where' in value)):
        raise ValueError('Dependency selector needs kind and exactly one id or where')
    result = {'kind': value['kind']}
    if 'id' in value:
        result['id'] = _text(value['id'], 'Dependency identity')
    else:
        where = value['where']
        if not isinstance(where, dict) or len(where) > 32:
            raise ValueError('Dependency query requires at most 32 predicates')
        for path, predicate in where.items():
            _path(path)
            _predicate(predicate)
        result['where'] = json.loads(encode(where))
    if 'fields' in value:
        fields = value['fields']
        if (not isinstance(fields, list) or len(fields) > 64
                or any(not isinstance(field, str) for field in fields)
                or len(fields) != len(set(fields))):
            raise ValueError('Dependency projection requires at most 64 unique fields')
        result['fields'] = sorted(_path(field) for field in fields)
    if len(encode(result)) > 32768:
        raise ValueError('Dependency selector exceeds its byte bound')
    return result


def _selection(store, run_id, selector, virtual=(), cache=None):
    if 'id' in selector:
        rows = store.list(run_id, selector['kind'], id=selector['id'])
        rows += [row for kind, row in virtual if kind == selector['kind']
                 and row['id'] == selector['id'] and not rows]
    else:
        if cache is None:
            available = store.list(run_id, selector['kind'])
        else:
            if selector['kind'] not in cache:
                cache[selector['kind']] = store.list(run_id, selector['kind'])
            available = list(cache[selector['kind']])
        actual = {row['id'] for row in available}
        available += [row for kind, row in virtual if kind == selector['kind'] and row['id'] not in actual]
        rows = [row for row in available
                if _matches(row, selector['where'])]
    if len(rows) > MAX_MEMBERS:
        raise ValueError('Dependency selection exceeds its membership bound')
    members = []
    for row in sorted(rows, key=lambda row: row['id']):
        if 'fields' not in selector:
            projected = {key: value for key, value in row.items() if key not in _META}
        else:
            projected = {'id': row['id'], 'fields': {}}
            for field in selector['fields']:
                value = _value(row, field)
                # Absence is distinct from JSON null, with no sentinel collision.
                projected['fields'][field] = {'present': False} if value is _MISSING else {
                    'present': True, 'value': value}
        members.append({'id': row['id'], 'token': digest(projected)})
    return {'selector': selector, 'count': len(members), 'membership_hash': digest(members)}


def _virtual_root(store, run_id, task):
    """Preview only the one task-contract question the host will ensure first.

    No row supplied by a model or arbitrary caller becomes virtual authority.
    Its identity and complete contents are recomputed from immutable run config.
    Live validation never uses this preview: the real rows must have been written.
    """
    from .scientific_memory import ScientificMemory, HEAD, REVISION, SCHEMA as SM_SCHEMA, _key
    selected = task.get('scientific_memory', {}).get('objects', [])
    if not selected:
        return []
    root = ScientificMemory(store, run_id)._unregistered_question()
    if root is None:
        return []
    matches = [row for row in selected if row.get('id') == root['id']]
    if not matches:
        return []
    if any(any(row.get(key) != value for key, value in root.items()) for row in matches):
        raise ValueError('Virtual scientific question differs from the immutable task contract')
    rid = _key('revision', root['id'], 1)
    common = {'run_id': run_id, 'version': 1, 'schema_version': SM_SCHEMA,
              'object_id': root['id'], 'object_kind': 'question'}
    return [('registration', {**common, 'id': _key('head', root['id']), 'kind': HEAD,
                              'revision': 1, 'record_id': rid}),
            ('registration', {**common, 'id': rid, 'kind': REVISION,
                              'record': root, 'record_sha256': digest(root)})]


def _defaults(store, run_id, task, evidence_ids, virtual):
    """Select only objects actually named in this request and their source closure."""
    from .scientific_memory import _key as sm_key

    selectors = []
    facts, objects = set(), set()
    interpretations = None

    def questions(record):
        qids = list(record.get('question_ids', []))
        if record.get('kind') == 'question':
            qids.append(record['id'])
        for qid in qids:
            _text(qid, 'Scientific question identity')
            # Bind new records within the questions actually read, including
            # new claims absent from the old object closure. Other questions
            # and unselected global SM state are outside this selector.
            query({'kind': 'scientific_memory_revision', 'record.question_ids': {'$contains': qid}},
                  ['object_id', 'record_sha256'])

    def exact(kind, oid, fields=None):
        selector = {'kind': kind, 'id': oid}
        if fields is not None:
            selector['fields'] = fields
        selectors.append(selector)

    def query(where, fields=None):
        selector = {'kind': 'registration', 'where': where}
        if fields is not None:
            selector['fields'] = fields
        selectors.append(selector)

    def source(ref):
        if not isinstance(ref, dict) or not isinstance(ref.get('fact_id'), str):
            raise ValueError('Scientific dependency needs a source identity')
        query({'kind': 'scientific_memory_source_change',
               'source_ref.run_id': ref['run_id'], 'source_ref.fact_id': ref['fact_id']})
        if ref['run_id'] == run_id:
            fact(ref['fact_id'], bind_source=False)
        else:
            alias_id = sm_key('source_alias', ref)
            exact('registration', alias_id)
            aliases = store.list(run_id, 'registration', id=alias_id)
            if aliases:
                fact(aliases[0]['local_fact_id'], bind_source=False)

    def fact(fid, *, bind_source=True):
        if fid in facts:
            return
        facts.add(fid)
        exact('fact', fid, ['kind', 'version', 'content_ref', 'provenance'])
        if bind_source:
            rows = store.list(run_id, 'fact', id=fid)
            if rows and rows[0]['kind'] in {'raw_fact', 'source_statement'}:
                row = rows[0]
                provenance = row.get('provenance', {})
                provenance = provenance if isinstance(provenance, dict) else {}
                original = provenance.get('original_source')
                ref = original if provenance.get('scientific_memory_import') and original else {
                    'run_id': run_id, 'fact_id': fid}
                source(ref)

    def object_ref(oid, revision):
        nonlocal interpretations
        if not isinstance(oid, str) or type(revision) is not int or revision < 1:
            raise ValueError('Scientific dependency needs an exact object revision')
        token = (oid, revision)
        if token in objects:
            return
        objects.add(token)
        if len(objects) > MAX_SELECTORS:
            raise ValueError('Scientific dependency closure exceeds its bound')
        exact('registration', sm_key('head', oid), ['object_id', 'object_kind', 'revision', 'record_id'])
        rid = sm_key('revision', oid, revision)
        exact('registration', rid, ['object_id', 'object_kind', 'record_sha256'])
        query({'kind': 'scientific_memory_review_pending', 'object_id': oid})
        where = {'kind': 'scientific_memory_interpretation',
                 'investigation_id': oid, 'investigation_revision': revision}
        query(where)
        if interpretations is None:
            interpretations = store.list(run_id, 'registration', kind='scientific_memory_interpretation')
        for row in interpretations:
            if row['investigation_id'] != oid or row['investigation_revision'] != revision:
                continue
            ref = row['decision_ref']
            object_ref(ref['id'], ref['revision'])
        rows = store.list(run_id, 'registration', id=rid)
        rows += [row for kind, row in virtual if kind == 'registration' and row['id'] == rid and not rows]
        if rows:
            record = rows[0]['record']
            questions(record)
            for ref in record['object_refs']:
                object_ref(ref['id'], ref['revision'])
            for ref in record['source_refs']:
                source(ref)

    for fid in evidence_ids:
        fact(fid)
    if task.get('issue_id'):
        exact('issue', task['issue_id'])
    for kind, key in (('request', 'requests'), ('dependency', 'dependencies')):
        for row in task.get(key, []):
            # prepare_effect itself changes this scheduler-owned capacity.
            if kind != 'dependency' or row['id'] != 'external_capacity':
                exact(kind, row['id'])
    constraints = task.get('constraints', {})
    for row in constraints.get('waiting', []):
        exact('wait', row['id'], ['issue_id', 'dependency_id', 'producer', 'observed_version',
              'wake_condition', 'deadline_at', 'expiry_action', 'wait_epoch'])
    for record in task.get('scientific_memory', {}).get('objects', []):
        object_ref(record['id'], record['revision'])
    loop = constraints.get('research_loop', {})
    for key in ('recommendations', 'improvement_reviews'):
        for row in loop.get(key, []):
            exact('registration', row['id'])
    for item in task.get('capability_catalog', {}).values():
        if item.get('registration_id'):
            exact('capability', item['registration_id'])
    if 'orchestration' in constraints:
        # A new active change is a phantom even when initial settings had no row.
        # Trial progress, step counters and elapsed budgets are deliberately absent.
        query({'kind': 'orchestration_change', 'status': {'$in': ['trial', 'kept']}},
              ['status', 'settings'])
    return selectors


def _manifest(value):
    if (not isinstance(value, dict)
            or set(value) != {'schema_version', 'run_id', 'config_hash', 'dependencies', 'token'}
            or value['schema_version'] != SCHEMA):
        raise ValueError('Malformed decision dependency manifest')
    _text(value['run_id'], 'Dependency run identity')
    for field in ('config_hash', 'token'):
        if not isinstance(value[field], str) or not _HASH.fullmatch(value[field]):
            raise ValueError('Decision dependency hashes must be exact SHA256 values')
    dependencies = value['dependencies']
    if not isinstance(dependencies, list) or len(dependencies) > MAX_SELECTORS:
        raise ValueError('Decision dependency manifest exceeds its selector bound')
    keys = []
    for item in dependencies:
        if not isinstance(item, dict) or set(item) != {'selector', 'count', 'membership_hash'}:
            raise ValueError('Malformed decision dependency selection')
        selector = _selector(item['selector'])
        if selector != item['selector']:
            raise ValueError('Decision dependency selector is not canonical')
        if (type(item['count']) is not int or not 0 <= item['count'] <= MAX_MEMBERS
                or ('id' in selector and item['count'] > 1)
                or not isinstance(item['membership_hash'], str)
                or not _HASH.fullmatch(item['membership_hash'])):
            raise ValueError('Malformed decision dependency membership')
        keys.append(encode(selector))
    if keys != sorted(set(keys)) or len(encode(value)) > MAX_BYTES:
        raise ValueError('Decision dependency manifest is not bounded and canonical')
    if digest({key: item for key, item in value.items() if key != 'token'}) != value['token']:
        raise ValueError('Decision dependency manifest token mismatch')
    return value


def capture(store, run_id, task, evidence_ids, selectors=()):
    """Bind request reads inside its caller-owned SQLite snapshot; writes nothing.

    Exact selectors use ``kind/id``. Query selectors use ``kind/where`` with
    dotted object paths and equality, $eq, $in, $contains or $exists predicates.
    Optional fields project semantic values; omitted fields exclude only ledger
    bookkeeping. Every query binds both membership and projected member content.
    """
    if not isinstance(task, dict) or task.get('run_id', run_id) != run_id:
        raise ValueError('Decision task belongs to another run')
    if (not isinstance(evidence_ids, (list, tuple)) or len(evidence_ids) > MAX_SELECTORS
            or not isinstance(selectors, (list, tuple)) or len(selectors) > MAX_SELECTORS):
        raise ValueError('Decision dependency input exceeds its bound')
    for fid in evidence_ids:
        _text(fid, 'Evidence identity')
    selected = {}
    virtual = _virtual_root(store, run_id, task)
    for value in _defaults(store, run_id, task, evidence_ids, virtual) + list(selectors):
        selector = _selector(value)
        selected[encode(selector)] = selector
    if len(selected) > MAX_SELECTORS:
        raise ValueError('Decision dependency closure exceeds its selector bound')
    cache = {}
    result = {'schema_version': SCHEMA, 'run_id': run_id,
              'config_hash': store.run(run_id)['config_hash'],
              'dependencies': [_selection(store, run_id, selected[key], virtual, cache) for key in sorted(selected)]}
    result['token'] = digest(result)
    return _manifest(result)


def validate(store, run_id, manifest):
    """Recheck a captured read/selector set; call within the admitting transaction."""
    manifest = _manifest(manifest)
    if manifest['run_id'] != run_id or manifest['config_hash'] != store.run(run_id)['config_hash']:
        raise StaleDecision('Decision research contract belongs to another run/configuration')
    cache = {}
    for dependency in manifest['dependencies']:
        current = _selection(store, run_id, dependency['selector'], cache=cache)
        if current != dependency:
            selector = dependency['selector']
            scope = selector['id'] if 'id' in selector else 'query:' + digest(selector['where'])[:16]
            raise StaleDecision('Decision dependency changed: ' + selector['kind'] + '/' + scope)


def validate_binding(effect, packet_task):
    """Require the same frozen manifest in metadata and packet; preserve legacy absence."""
    metadata = effect.get('metadata', {})
    if 'decision_dependencies' not in metadata and 'decision_dependencies' not in packet_task:
        return None
    a, b = metadata.get('decision_dependencies'), packet_task.get('decision_dependencies')
    if a is None or b is None or encode(a) != encode(b):
        raise ValueError('Effect and packet decision dependency bindings differ')
    result = _manifest(a)
    if (effect.get('run_id', result['run_id']) != result['run_id']
            or packet_task.get('run_id', result['run_id']) != result['run_id']):
        raise ValueError('Decision dependency binding belongs to another run')
    return result
