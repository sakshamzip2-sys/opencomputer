"""Tests for opencomputer/tools/schema_sanitizer.py.

Mirrors Hermes's schema_sanitizer test surface and adds coverage for the
OC-specific numeric-constraint stripping that fixes the Anthropic 400
"minimum/maximum not supported" error.
"""
from __future__ import annotations

from opencomputer.tools.schema_sanitizer import (
    normalize_tool_input_schema_for_anthropic,
    sanitize_tool_schemas,
    strip_anthropic_unsupported_constraints,
    strip_nullable_unions,
)

# --------------------------------------------------------------------------- #
# strip_nullable_unions (verbatim Hermes behavior)
# --------------------------------------------------------------------------- #

def test_strip_nullable_unions_collapses_string_or_null():
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    out = strip_nullable_unions(schema, keep_nullable_hint=False)
    assert out == {"type": "string"}


def test_strip_nullable_unions_keeps_nullable_hint_when_requested():
    schema = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    out = strip_nullable_unions(schema, keep_nullable_hint=True)
    assert out == {"type": "string", "nullable": True}


def test_strip_nullable_unions_preserves_metadata():
    schema = {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "description": "name",
        "default": None,
    }
    out = strip_nullable_unions(schema, keep_nullable_hint=False)
    assert out["type"] == "string"
    assert out["description"] == "name"


def test_strip_nullable_unions_leaves_meaningful_unions():
    """Don't collapse a non-nullable anyOf — only nullable patterns."""
    schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    out = strip_nullable_unions(schema, keep_nullable_hint=False)
    assert "anyOf" in out


# --------------------------------------------------------------------------- #
# strip_anthropic_unsupported_constraints (OC-specific bug fix)
# --------------------------------------------------------------------------- #

def test_strips_minimum_maximum_from_integer():
    schema = {"type": "integer", "minimum": 1, "maximum": 600}
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["type"] == "integer"
    assert "minimum" not in out
    assert "maximum" not in out


def test_strips_minimum_maximum_from_number():
    schema = {"type": "number", "minimum": 0.0, "maximum": 1.0}
    out = strip_anthropic_unsupported_constraints(schema)
    assert "minimum" not in out
    assert "maximum" not in out


def test_strips_exclusive_min_max_and_multiple_of():
    schema = {
        "type": "integer",
        "exclusiveMinimum": 0,
        "exclusiveMaximum": 100,
        "multipleOf": 2,
    }
    out = strip_anthropic_unsupported_constraints(schema)
    for key in ("exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        assert key not in out


def test_does_not_strip_string_constraints():
    schema = {
        "type": "string",
        "minLength": 1,
        "maxLength": 100,
        "pattern": "^[a-z]+$",
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["minLength"] == 1
    assert out["maxLength"] == 100
    assert out["pattern"] == "^[a-z]+$"


def test_does_not_mutate_original():
    original = {"type": "integer", "minimum": 1, "maximum": 600}
    strip_anthropic_unsupported_constraints(original)
    assert original["minimum"] == 1
    assert original["maximum"] == 600


def test_recursive_in_array_items():
    schema = {
        "type": "object",
        "properties": {
            "ports": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 65535},
            },
        },
    }
    out = strip_anthropic_unsupported_constraints(schema)
    items = out["properties"]["ports"]["items"]
    assert "minimum" not in items
    assert "maximum" not in items


def test_recursive_in_nested_object():
    schema = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {
                    "retries": {"type": "integer", "minimum": 0, "maximum": 10},
                },
            },
        },
    }
    out = strip_anthropic_unsupported_constraints(schema)
    retries = out["properties"]["config"]["properties"]["retries"]
    assert "minimum" not in retries


def test_strips_min_items_max_items_from_array():
    """Anthropic rejects minItems > 1 and maxItems anywhere on arrays."""
    schema = {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 2,
        "maxItems": 4,
        "uniqueItems": True,
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["type"] == "array"
    assert "minItems" not in out
    assert "maxItems" not in out
    assert "uniqueItems" not in out
    # Non-array fields preserved
    assert out["items"] == {"type": "string"}


def test_strips_min_max_contains_from_array():
    schema = {
        "type": "array",
        "items": {"type": "integer"},
        "minContains": 1,
        "maxContains": 3,
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert "minContains" not in out
    assert "maxContains" not in out


def test_injects_additional_properties_false_on_object():
    """Anthropic 400s without `additionalProperties: false` on objects."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["additionalProperties"] is False


def test_overwrites_additional_properties_true_with_false():
    """Tools that explicitly set additionalProperties: true get downgraded."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": True,
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["additionalProperties"] is False


def test_overwrites_additional_properties_subschema_with_false():
    """A sub-schema additionalProperties also gets downgraded to strict."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": {"type": "string"},
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["additionalProperties"] is False


def test_injects_additional_properties_on_nested_object():
    schema = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {"k": {"type": "string"}},
            },
        },
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["additionalProperties"] is False
    assert out["properties"]["config"]["additionalProperties"] is False


def test_strips_min_max_properties_from_object():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "minProperties": 1,
        "maxProperties": 5,
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["type"] == "object"
    assert "minProperties" not in out
    assert "maxProperties" not in out
    # Required + properties preserved
    assert out["properties"] == {"a": {"type": "string"}}


def test_recursive_array_constraints_in_nested_object():
    """Tools wrap arrays inside their object parameters — recursion must reach them."""
    schema = {
        "type": "object",
        "properties": {
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 4,
            },
        },
    }
    out = strip_anthropic_unsupported_constraints(schema)
    options = out["properties"]["options"]
    assert "minItems" not in options
    assert "maxItems" not in options


def test_anthropic_format_drops_strict_flag_to_avoid_20_cap():
    """Anthropic caps strict tools at 20; OC ships >20 strict-mode tools.

    The provider boundary drops ``strict: true`` so we never trip the
    "Too many strict tools (33). The maximum number of strict tools
    supported is 20" 400.
    """
    import importlib.util
    import sys
    from dataclasses import replace
    from pathlib import Path

    from plugin_sdk.tool_contract import ToolSchema

    repo = Path(__file__).parent.parent
    openai_provider_py = repo / "extensions" / "openai-provider" / "provider.py"
    anthropic_provider_py = repo / "extensions" / "anthropic-provider" / "provider.py"

    def _load(name: str, path: Path):
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m

    _load("provider", openai_provider_py)
    _load("provider_a", anthropic_provider_py)
    sys.modules.pop("provider", None)
    _load("provider", anthropic_provider_py)
    anth = sys.modules["provider"]

    schema = ToolSchema(
        name="bash_strict",
        description="x",
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}}},
        strict=True,
    )
    out = anth._format_tools_for_anthropic([schema])
    assert "strict" not in out[0], (
        "strict flag must be dropped to avoid Anthropic's 20-tool cap"
    )
    # Schema-level enforcement still in place
    assert out[0]["input_schema"]["additionalProperties"] is False


def test_real_clarify_tool_passes_through_clean():
    """ClarifyTool has minItems=2 maxItems=4 on its options array — must be stripped."""
    from opencomputer.tools.clarify import ClarifyTool

    schema = ClarifyTool().schema
    out = normalize_tool_input_schema_for_anthropic(schema.parameters)

    def walk(node):
        if isinstance(node, dict):
            t = node.get("type")
            if t == "array":
                for forbidden in ("minItems", "maxItems", "uniqueItems"):
                    assert forbidden not in node, (
                        f"{forbidden} still on array node: {node!r}"
                    )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(out)


def test_preserves_description_default_enum_on_integer():
    schema = {
        "type": "integer",
        "description": "count",
        "default": 5,
        "enum": [1, 2, 3],
        "minimum": 1,
    }
    out = strip_anthropic_unsupported_constraints(schema)
    assert out["description"] == "count"
    assert out["default"] == 5
    assert out["enum"] == [1, 2, 3]
    assert "minimum" not in out


# --------------------------------------------------------------------------- #
# normalize_tool_input_schema_for_anthropic (boundary)
# --------------------------------------------------------------------------- #

def test_normalize_returns_minimal_object_for_empty():
    assert normalize_tool_input_schema_for_anthropic(None) == {
        "type": "object",
        "properties": {},
    }
    assert normalize_tool_input_schema_for_anthropic({}) == {
        "type": "object",
        "properties": {},
    }


def test_normalize_strips_min_max_and_nullable_unions():
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 1, "maximum": 600},
            "label": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }
    out = normalize_tool_input_schema_for_anthropic(schema)
    assert "minimum" not in out["properties"]["n"]
    assert "maximum" not in out["properties"]["n"]
    assert out["properties"]["label"]["type"] == "string"
    assert "nullable" not in out["properties"]["label"]  # hint=False for Anthropic


def test_normalize_injects_properties_when_missing():
    schema = {"type": "object"}  # no properties
    out = normalize_tool_input_schema_for_anthropic(schema)
    assert out["properties"] == {}


# --------------------------------------------------------------------------- #
# sanitize_tool_schemas (Hermes-compatible OpenAI-format entry point)
# --------------------------------------------------------------------------- #

def test_sanitize_tool_schemas_handles_missing_parameters():
    tools = [{"type": "function", "function": {"name": "x"}}]
    out = sanitize_tool_schemas(tools)
    assert out[0]["function"]["parameters"]["type"] == "object"
    assert out[0]["function"]["parameters"]["properties"] == {}


def test_sanitize_tool_schemas_replaces_bare_string():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "x",
                "parameters": "object",  # malformed bare string
            },
        }
    ]
    out = sanitize_tool_schemas(tools)
    params = out[0]["function"]["parameters"]
    assert params["type"] == "object"


def test_sanitize_tool_schemas_returns_deep_copy():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "x",
                "parameters": {
                    "type": "object",
                    "properties": {"y": {"type": "integer"}},
                },
            },
        }
    ]
    out = sanitize_tool_schemas(tools)
    out[0]["function"]["parameters"]["properties"]["y"]["type"] = "string"
    # Original unchanged
    assert tools[0]["function"]["parameters"]["properties"]["y"]["type"] == "integer"


# --------------------------------------------------------------------------- #
# Integration: real OC tool schemas pass through cleanly
# --------------------------------------------------------------------------- #

def _walk_assert_no_numeric_constraints(node):
    if isinstance(node, dict):
        if node.get("type") in {"integer", "number"}:
            for forbidden in (
                "minimum", "maximum",
                "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
            ):
                assert forbidden not in node, (
                    f"{forbidden!r} still present on {node.get('type')}: {node!r}"
                )
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, (
                f"object node missing additionalProperties: false — {node!r}"
            )
        for v in node.values():
            _walk_assert_no_numeric_constraints(v)
    elif isinstance(node, list):
        for v in node:
            _walk_assert_no_numeric_constraints(v)


def test_real_bash_tool_passes_through_clean():
    from opencomputer.tools.bash import BashTool
    schema = BashTool().schema
    out = normalize_tool_input_schema_for_anthropic(schema.parameters)
    _walk_assert_no_numeric_constraints(out)


def test_real_read_tool_passes_through_clean():
    from opencomputer.tools.read import ReadTool
    schema = ReadTool().schema
    out = normalize_tool_input_schema_for_anthropic(schema.parameters)
    _walk_assert_no_numeric_constraints(out)
