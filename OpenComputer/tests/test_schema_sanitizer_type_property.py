"""Regression test: schema_sanitizer must not crash on adapters whose
arg name is literally ``type``.

Bug history (Wave 4 hotfix):
The recursive walker did ``if node.get("type") in {"integer", "number"}:``
unconditionally. When the walker descended into ``properties: {...}``
and one of the property names was ``"type"`` (e.g. the LearnX
``assignments`` adapter has a ``type`` arg for filtering activity type),
``node.get("type")`` returned that property's schema (a dict), not a
JSON Schema type string. Hashing a dict into the literal set crashed
with::

    TypeError: cannot use 'dict' as a set element (unhashable type: 'dict')

Fix: guard the type-specific cleanup with ``isinstance(node_type, str)``.

This test asserts the fix holds — schemas with a ``type``-named property
sanitize cleanly without raising.
"""
from __future__ import annotations

from opencomputer.tools.schema_sanitizer import (
    normalize_tool_input_schema_for_anthropic,
    strip_anthropic_unsupported_constraints,
)


def test_schema_with_property_named_type_does_not_crash() -> None:
    """An adapter arg named ``type`` produces a property whose key is
    ``"type"``. The sanitizer must not interpret that as a JSON Schema
    type annotation."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "course": {"type": "string", "description": "Filter by course"},
            "type": {"type": "string", "description": "Filter by activity type"},
        },
    }
    # Both entry points must survive the bad shape without TypeError.
    result_strip = strip_anthropic_unsupported_constraints(schema)
    assert result_strip["properties"]["type"]["type"] == "string"

    result_normalize = normalize_tool_input_schema_for_anthropic(schema)
    assert result_normalize["properties"]["type"]["type"] == "string"


def test_schema_with_nested_property_named_type() -> None:
    """Nested object property named ``type`` — same defense; recursion
    deeper than the top level must not trip."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "filter": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string"},  # deeper "type" property
                    "course": {"type": "string"},
                },
            },
        },
    }
    result = normalize_tool_input_schema_for_anthropic(schema)
    assert (
        result["properties"]["filter"]["properties"]["type"]["type"] == "string"
    )
