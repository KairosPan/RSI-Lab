"""Bounded, deterministic checks of recommendation request parameters.

These checks establish request correspondence, never execution completion or
scientific benefit. Adapter normalization is trusted source-pinned code, not a
model judgment, and must be read-only and deterministic for the frozen run.
"""
import json
import math

from .store import digest, encode


MAX_BYTES = 65536
MAX_DEPTH = 32  # Root value is depth zero; each object value/list item adds one.
MAX_NODES = 8192  # JSON values, including containers; object keys use the byte cap.


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate execution contract JSON key: ' + key)
        result[key] = value
    return result


def _check_structure(value):
    """Bound work before serialization, independent of Python's recursion limit.

    Iterator frames avoid copying broad arrays onto the traversal stack. Shared
    subtrees are valid JSON values and count at every occurrence; cycles are not.
    """
    stack, ancestors, nodes = [(iter((value,)), 0, None)], set(), 0
    while stack:
        iterator, depth, owner = stack[-1]
        try:
            current = next(iterator)
        except StopIteration:
            stack.pop()
            if owner is not None:
                ancestors.remove(owner)
            continue
        nodes += 1
        if nodes > MAX_NODES:
            raise ValueError('Recommendation contract data exceeds its JSON node bound')
        if depth > MAX_DEPTH:
            raise ValueError('Recommendation contract data exceeds its nesting bound')
        if type(current) in (dict, list):
            identity = id(current)
            if identity in ancestors:
                raise ValueError('Recommendation contract data must not contain cycles')
            if type(current) is dict and any(type(key) is not str for key in current):
                raise ValueError('Recommendation contract object keys must be strings')
            ancestors.add(identity)
            children = current.values() if type(current) is dict else current
            stack.append((iter(children), depth + 1, identity))
        elif current is not None and type(current) not in (str, bool, int, float):
            raise ValueError('Recommendation contract data must contain only JSON values')
        elif type(current) is float and not math.isfinite(current):
            raise ValueError('Recommendation contract data requires finite numbers')


def bounded(value):
    _check_structure(value)
    try:
        raw = encode(value)
    except RecursionError as exc:
        raise ValueError('Recommendation contract data exceeds its nesting bound') from exc
    if len(raw) > MAX_BYTES:
        raise ValueError('Recommendation contract data exceeds 64 KiB')
    return json.loads(raw)


def parse(raw):
    if not isinstance(raw, str) or not raw or len(raw.encode()) > MAX_BYTES:
        raise ValueError('A bounded execution_contract_json string is required')
    def invalid_constant(value):
        raise ValueError('Non-finite execution contract JSON constant: ' + value)
    try:
        contract = json.loads(raw, object_pairs_hook=_object, parse_constant=invalid_constant)
    except RecursionError as exc:
        raise ValueError('Execution contract exceeds its nesting bound') from exc
    _check_structure(contract)
    if not isinstance(contract, dict) or type(contract.get('version')) is not int or contract['version'] != 1:
        raise ValueError('Execution contract requires version 1')
    if contract.get('kind') == 'exact_inputs':
        if set(contract) != {'version', 'kind', 'inputs'} or not isinstance(contract['inputs'], dict):
            raise ValueError('exact_inputs contract requires an explicit inputs object')
    elif contract.get('kind') == 'constraints':
        if set(contract) != {'version', 'kind', 'requirements'}:
            raise ValueError('constraints contract requires only version, kind and requirements')
        requirements = contract['requirements']
        if not isinstance(requirements, list) or not 1 <= len(requirements) <= 64:
            raise ValueError('Execution contract requires 1 to 64 explicit constraints')
        for item in requirements:
            if not isinstance(item, dict) or set(item) != {'path', 'operator', 'value'}:
                raise ValueError('Each constraint requires path, operator and value')
            path = item['path']
            if (not isinstance(path, list) or not 1 <= len(path) <= 16 or any(
                    not ((type(part) is str and 0 < len(part) <= 256)
                         or (type(part) is int and part >= 0)) for part in path)):
                raise ValueError('Constraint path needs 1 to 16 explicit object keys or array indices')
            op, value = item['operator'], item['value']
            if not isinstance(op, str) or op not in {'equals', 'one_of', 'contains_all', 'min', 'max'}:
                raise ValueError('Unsupported execution contract constraint operator')
            if op in {'one_of', 'contains_all'} and (not isinstance(value, list) or not value):
                raise ValueError(op + ' requires a nonempty list')
            if op in {'min', 'max'} and not _number(value):
                raise ValueError(op + ' requires a finite number, not a boolean')
    else:
        raise ValueError('Unsupported execution contract kind')
    return bounded(contract)


def normalize(adapter, store, run_id, inputs):
    if not isinstance(inputs, dict):
        raise ValueError('Recommendation execution inputs must be an object')
    value = bounded(inputs)
    if adapter.recommendation_normalize is not None:
        value = adapter.recommendation_normalize(store, run_id, value)
    if not isinstance(value, dict):
        raise ValueError('Recommendation normalization must return an object')
    return bounded(value)


def _allowed(adapter, contract):
    kinds = adapter.manifest.get('recommendation_contract_kinds', ['exact_inputs', 'constraints'])
    if contract['kind'] not in kinds:
        raise ValueError('Execution contract kind is not permitted by this adapter: ' + contract['kind'])


def prepare(runtime, payload):
    adapter = runtime.adapters.by_id.get(payload['capability_id'])
    cap = runtime.capability_catalog().get(payload['capability_id'])
    if adapter is None or not cap or adapter.version != payload['capability_version'] or cap['version'] != adapter.version:
        raise ValueError('Execution contract requires an executable registered capability version')
    contract = parse(payload.get('execution_contract_json'))
    _allowed(adapter, contract)
    if contract['kind'] == 'exact_inputs':
        contract['inputs'] = normalize(adapter, runtime.store, runtime.run_id, contract['inputs'])
        if adapter.recommendation_normalize is None:
            # The registered prepare is explicitly side-effect-free. Domains
            # with stateful admission rules provide their own pure normalizer
            # so a contract does not depend on a transient queue/budget check.
            adapter.prepare(contract['inputs'])
    return bounded(contract), adapter.descriptor()


def _matches(contract, actual):
    if contract['kind'] == 'exact_inputs':
        return encode(contract['inputs']) == encode(actual)
    for requirement in contract['requirements']:
        found = actual
        for part in requirement['path']:
            if type(part) is str and isinstance(found, dict) and part in found:
                found = found[part]
            elif type(part) is int and isinstance(found, list) and part < len(found):
                found = found[part]
            else:
                return False
        op, value = requirement['operator'], requirement['value']
        if op == 'equals':
            matched = encode(found) == encode(value)
        elif op == 'one_of':
            matched = any(encode(found) == encode(v) for v in value)
        elif op == 'contains_all':
            matched = isinstance(found, list) and all(any(encode(v) == encode(x) for x in found) for v in value)
        else:
            matched = _number(found)
            if matched:
                matched = found >= value if op == 'min' else found <= value
        if not matched:
            return False
    return True


def verify(runtime, row, command):
    contract = row.get('execution_contract')
    if contract is None or not row.get('contract_adapter_descriptor_ref'):
        raise ValueError('Historical recommendation has no frozen execution contract; adoption cannot be verified')
    rec, payload = row['recommendation'], command['payload']
    if command['action'] != 'request_diagnostic' or (
            payload.get('capability_id'), payload.get('capability_version')) != (
            rec['capability_id'], rec['capability_version']):
        raise ValueError('Adoption must bind the same-call diagnostic to the recommended capability')
    # Re-validate stored syntax and provenance instead of trusting a label.
    contract = parse(encode(bounded(contract)).decode())
    if digest(contract) != row.get('contract_sha256'):
        raise ValueError('Frozen recommendation execution contract digest differs')
    descriptor = json.loads(runtime.store.read_blob(row['contract_adapter_descriptor_ref']))
    adapter = runtime.adapters.by_id.get(rec['capability_id'])
    if (digest(descriptor) != row.get('contract_adapter_descriptor_sha256')
            or adapter is None or descriptor != adapter.descriptor()):
        raise ValueError('Recommendation execution contract adapter implementation changed')
    _allowed(adapter, contract)
    try:
        inputs = json.loads(payload['inputs_json'], object_pairs_hook=_object)
    except RecursionError as exc:
        raise ValueError('Recommendation execution inputs exceed their nesting bound') from exc
    actual = normalize(adapter, runtime.store, runtime.run_id, inputs)
    if not _matches(contract, actual):
        raise ValueError('Actual diagnostic inputs do not satisfy the frozen recommendation execution contract')
    return {'verification_scope': 'request_inputs', 'status': 'matched',
            'contract_sha256': row['contract_sha256'], 'inputs_sha256': digest(inputs),
            'normalized_inputs_sha256': digest(actual),
            'adapter_descriptor_sha256': row['contract_adapter_descriptor_sha256'],
            'execution_completion': 'not_established', 'causal_benefit': 'not_established'}
