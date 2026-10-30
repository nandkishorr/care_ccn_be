"""Discover intent helpers (ONIX JSONPath catalog filters)."""

from collections.abc import Mapping
from typing import Any


def resource_attribute_jsonpath(conditions: Mapping[str, Any]) -> dict[str, str]:
    """JSONPath filter matching ``resourceAttributes`` conditions (AND-combined, falsy values skipped)."""
    clauses = [f'@.resourceAttributes.{key} == "{value}"' for key, value in conditions.items() if value]
    expression = "$.catalogs[*].resources[*] ? (" + " && ".join(clauses) + ")"
    return {"type": "jsonpath", "expression": expression}


def build_discover_intent(query: Mapping[str, Any] | None, filter_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    """``message.intent`` from a frontend query: ``textSearch``, a ready-made ``filters`` or per-key conditions."""
    query = query or {}
    intent: dict[str, Any] = {}
    if query.get("textSearch"):
        intent["textSearch"] = query["textSearch"]
    if query.get("filters"):
        intent["filters"] = query["filters"]
        return intent
    conditions = {key: query.get(key) for key in filter_keys}
    if any(conditions.values()):
        intent["filters"] = resource_attribute_jsonpath(conditions)
    return intent
