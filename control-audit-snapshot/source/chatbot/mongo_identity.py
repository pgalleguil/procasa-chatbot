"""Small, explicit helpers for Mongo identifiers crossing API boundaries.

Canonical CRM documents use BSON ``ObjectId`` values for ``leads._id`` and
``crm_assignment_cycles.lead_id``.  Historical/API paths can carry the same
value as text, so compatibility is kept bounded and deterministic.
"""

from __future__ import annotations

from typing import Any

from bson import ObjectId


def mongo_id_variants(value: Any) -> tuple[Any, ...]:
    """Return the original Mongo value plus its safe legacy counterpart."""

    if value is None:
        return ()
    variants: list[Any] = [value]
    if isinstance(value, ObjectId):
        variants.append(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if text and text != value:
            variants.append(text)
        if ObjectId.is_valid(text):
            variants.append(ObjectId(text))
    result: list[Any] = []
    for candidate in variants:
        if not any(candidate == existing for existing in result):
            result.append(candidate)
    return tuple(result)


def mongo_id_type(value: Any) -> str:
    """Return a low-cardinality, non-sensitive type label for diagnostics."""

    if value is None:
        return "missing"
    if isinstance(value, ObjectId):
        return "ObjectId"
    if isinstance(value, str):
        return "str"
    return type(value).__name__
