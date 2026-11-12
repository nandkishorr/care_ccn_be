"""Discover intent helpers (ONIX JSONPath catalog filters)."""

from collections.abc import Mapping
from typing import Any

from care_ccn_be.resources.beckn.envelope import CatalogFilter, Intent


def resource_attribute_jsonpath(conditions: Mapping[str, Any]) -> dict[str, str]:
    """JSONPath filter matching ``resourceAttributes`` conditions (AND-combined, falsy values skipped)."""
    clauses = [f'@.resourceAttributes.{key} == "{value}"' for key, value in conditions.items() if value]
    expression = "$.catalogs[*].resources[*] ? (" + " && ".join(clauses) + ")"
    return CatalogFilter(type="jsonpath", expression=expression).dump()


def build_discover_intent(query: Mapping[str, Any] | None, filter_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    """``message.intent`` from a frontend query: ``textSearch``, a ready-made ``filters`` or per-key conditions."""
    query = query or {}
    filters = query.get("filters")
    if not filters:
        conditions = {key: query.get(key) for key in filter_keys}
        filters = resource_attribute_jsonpath(conditions) if any(conditions.values()) else None
    return Intent(text_search=query.get("textSearch") or None, filters=filters).dump()
