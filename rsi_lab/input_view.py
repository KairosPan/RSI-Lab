#!/usr/bin/env python3
"""Reversible JSON presentations; selection, ranking and evidence stay unchanged.

Only JSON values are accepted. Dict insertion order, list order, scalar values
and numeric JSON representations survive render/decode. No summarization occurs.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import math

SCHEMA = "lab_input_view_v1"
MODES = {"expanded_json_v1", "compact_json_v1", "lossless_deduplicated_json_v1"}
REF, LITERAL = "$ref", "$literal"
DECODING_RULES = (
    "This is a lossless representation of the complete TASK_INPUT_JSON, not additional evidence. "
    "Decode root recursively. A dictionary whose only key is '$ref' means dictionary[index]; "
    "recursively decode that entry. A dictionary whose only key is '$literal' contains ordered "
    "[key, encoded_value] pairs: restore that ordinary object, decoding each value. Otherwise "
    "retain every object key in its written order and recursively decode its value. "
    "Retain every array element in order and recursively decode it. Scalars are unchanged. "
    "References may be nested; there are no cycles. Resolve references before making decisions. "
    "No fields, candidate order, evidence, memory or unknown values have been removed."
)


def validate_json(value, ancestors=None):
    ancestors = set() if ancestors is None else ancestors
    if value is None or type(value) in (bool, str, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite JSON number")
        return
    if type(value) not in (dict, list):
        raise ValueError("Only JSON objects, arrays and scalar values are supported")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("Cyclic input is not JSON")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("JSON object keys must be strings")
            values = value.values()
        else:
            values = value
        for child in values:
            validate_json(child, ancestors)
    finally:
        ancestors.remove(identity)


def compact(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def effective_mode(mode):
    if mode not in MODES:
        raise ValueError(f"Unsupported presentation mode: {mode}")
    return mode


def validate_config(config):
    if type(config) is not dict or set(config) != {"mode"}:
        raise ValueError("Input view config requires exactly mode")
    return effective_mode(config["mode"])


def _deduplicate(value):
    occurrences, first_seen = Counter(), {}

    def count(node):
        if type(node) not in (dict, list):
            return
        key = compact(node)  # Deliberately preserve object-key and array order.
        occurrences[key] += 1
        first_seen.setdefault(key, node)
        for child in node.values() if type(node) is dict else node:
            count(child)

    count(value)
    # Small subtrees are cheaper written literally than with a table entry.
    keys = [key for key in first_seen if occurrences[key] > 1 and len(key.encode()) >= 32]
    ids = {key: index for index, key in enumerate(keys)}

    def encode(node, bypass_reference=False):
        if type(node) not in (dict, list):
            return node
        key = compact(node)
        if not bypass_reference and key in ids:
            return {REF: ids[key]}
        if type(node) is list:
            return [encode(child) for child in node]
        if REF in node or LITERAL in node:
            # Reserved-key collisions are represented as ordered object pairs.
            return {LITERAL: [[name, encode(child)] for name, child in node.items()]}
        return {name: encode(child) for name, child in node.items()}

    return {
        "format": "lossless_deduplicated_json_v1",
        "decoding_rules": DECODING_RULES,
        "dictionary": [encode(first_seen[key], bypass_reference=True) for key in keys],
        "root": encode(value),
    }


def render(value, mode):
    validate_json(value)
    mode = effective_mode(mode)
    if mode == "expanded_json_v1":
        return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if mode == "compact_json_v1":
        return compact(value) + "\n"
    return compact(_deduplicate(value)) + "\n"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def decode(rendered, mode):
    mode = effective_mode(mode)
    value = json.loads(rendered, object_pairs_hook=_unique_object)
    validate_json(value)
    if mode != "lossless_deduplicated_json_v1":
        return value
    if (type(value) is not dict or set(value) != {"format", "decoding_rules", "dictionary", "root"}
            or value["format"] != mode or value["decoding_rules"] != DECODING_RULES
            or type(value["dictionary"]) is not list):
        raise ValueError("Invalid lossless presentation envelope or decoding rules")
    entries, active, cache = value["dictionary"], set(), {}

    def expand(node):
        if type(node) is list:
            return [expand(child) for child in node]
        if type(node) is not dict:
            return node
        if REF in node:
            index = node[REF]
            if set(node) != {REF} or type(index) is not int or not 0 <= index < len(entries):
                raise ValueError("Invalid/dangling dictionary reference")
            if index in active:
                raise ValueError("Cyclic dictionary references")
            if index not in cache:
                active.add(index)
                cache[index] = expand(entries[index])
                active.remove(index)
            return copy.deepcopy(cache[index])
        if LITERAL in node:
            if set(node) != {LITERAL} or type(node[LITERAL]) is not list:
                raise ValueError("Invalid literal object escape")
            result = {}
            for pair in node[LITERAL]:
                if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str or pair[0] in result:
                    raise ValueError("Invalid/duplicate ordered literal object member")
                result[pair[0]] = expand(pair[1])
            return result
        return {key: expand(child) for key, child in node.items()}

    return expand(value["root"])


def audit_render(original, rendered, mode):
    """Fail closed on any value/type/order loss, without estimating model tokens."""
    mode = effective_mode(mode)
    decoded = decode(rendered, mode)
    original_json, restored_json = compact(original), compact(decoded)
    if original_json != restored_json:
        raise ValueError("Input presentation did not preserve all ordered JSON values")
    expanded = render(original, "expanded_json_v1")
    digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
    return {
        "schema_version": "lab_input_render_audit_v1",
        "mode": mode,
        "ordered_roundtrip_equal": True,
        "input_ordered_json_sha256": digest(original_json),
        "decoded_ordered_json_sha256": digest(restored_json),
        "view_sha256": digest(rendered),
        "expanded_input_sha256": digest(expanded),
        "expanded_input_bytes": len(expanded.encode()),
        "view_bytes": len(rendered.encode()),
        "expanded_input_characters": len(expanded),
        "view_characters": len(rendered),
        "view_to_expanded_byte_ratio": len(rendered.encode()) / len(expanded.encode()),
        "token_measurement": "No token savings inferred; use actual CLI usage after execution",
    }
