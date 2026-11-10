"""Closed host-owned domain policy for otherwise generic research roles.

This policy supplies vocabulary and guidance, never capabilities, executable
code, permissions, metric direction, or evidence. Omission means generic roles.
"""
from __future__ import annotations

import json


FIELDS = {'recommendation_kinds', 'recommendation_guidance', 'final_review_guidance'}
MAX_KINDS = 32
MAX_KIND_CHARS = 120
MAX_GUIDANCE_CHARS = 8192
MAX_POLICY_BYTES = 20000


def normalize_research_policy(value=None):
    """Validate and copy a JSON-object policy without rewriting host text.

    All three fields are optional. Supplied kind lists must be nonempty and
    unique; supplied guidance must be nonblank. None is equivalent to {}.
    """
    if value is None:
        return {}
    if type(value) is not dict or set(value) - FIELDS:
        raise ValueError('Research policy must be an object containing only registered policy fields')
    result = {}
    if 'recommendation_kinds' in value:
        kinds = value['recommendation_kinds']
        if (type(kinds) is not list or not 1 <= len(kinds) <= MAX_KINDS
                or any(type(kind) is not str or not kind.strip() or len(kind) > MAX_KIND_CHARS for kind in kinds)
                or len(kinds) != len(set(kinds))):
            raise ValueError('Recommendation kinds require a bounded nonempty list of unique nonblank strings')
        result['recommendation_kinds'] = list(kinds)
    for field in ('recommendation_guidance', 'final_review_guidance'):
        if field in value:
            text = value[field]
            if type(text) is not str or not text.strip() or len(text) > MAX_GUIDANCE_CHARS:
                raise ValueError('Research guidance must be bounded nonblank text')
            result[field] = text
    try:
        raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError('Research policy must be finite UTF-8 JSON') from exc
    if len(raw) > MAX_POLICY_BYTES:
        raise ValueError('Research policy exceeds its complete JSON byte limit')
    return result
