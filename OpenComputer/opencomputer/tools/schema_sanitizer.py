"""Sanitize tool JSON schemas for broad LLM-backend compatibility.

Ported verbatim from Hermes Agent's ``tools/schema_sanitizer.py`` and
extended with Anthropic-specific numeric-constraint stripping (Anthropic's
2025 tool-validator rejects ``minimum``/``maximum``/``exclusiveMinimum``/
``exclusiveMaximum``/``multipleOf`` on ``integer``/``number`` types — both
Hermes and OC tool schemas hit this once you exercise enough tools).

Some local inference backends (notably llama.cpp's ``json-schema-to-grammar``
converter used to build GBNF tool-call parsers) are strict about what JSON
Schema shapes they accept. Schemas that OpenAI / Anthropic / most cloud
providers silently accept can make llama.cpp fail the entire request with::

    HTTP 400: Unable to generate parser for this template.

The failure modes we've seen in the wild:

* ``{"type": "object"}`` with no ``properties`` — rejected as a node the
  grammar generator can't constrain.
* A schema value that is the bare string ``"object"`` instead of a dict.
* ``"type": ["string", "null"]`` array types — many converters only accept
  single-string ``type``.
* ``anyOf`` / ``oneOf`` unions whose only purpose is to permit ``null`` for
  optional fields. Anthropic rejects these at the top of ``input_schema``;
  collapse them to the non-null branch.
* Numeric constraints on integer/number types — Anthropic-only rejection.

This module walks the final tool schema tree (after MCP-level normalization
and any per-tool dynamic rebuilds) and fixes the known-hostile constructs
in-place on a deep copy. It is intentionally conservative: it only modifies
shapes the LLM backend couldn't use anyway.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)


# Anthropic's tool validator rejects these on integer/number types
# (added to its strict mode in 2025). The constraints are perfectly valid
# JSON Schema and OpenAI accepts them — only Anthropic strips them.
_NUMERIC_CONSTRAINT_KEYS: frozenset[str] = frozenset({
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
})

# Anthropic's array-type validator only accepts ``minItems`` values 0 or 1
# (the wire error: "minItems values other than 0 or 1 are not supported").
# It also rejects ``maxItems`` and ``uniqueItems``. Same OpenAI-accepts /
# Anthropic-rejects pattern. Tools that need bounded arrays still validate
# at the tool layer in their ``execute()`` method (see
# ``opencomputer/tools/clarify.py`` for the canonical example).
_ARRAY_CONSTRAINT_KEYS: frozenset[str] = frozenset({
    "minItems",
    "maxItems",
    "uniqueItems",
    "minContains",
    "maxContains",
})

# Anthropic also rejects object-level cardinality bounds. Strip
# defensively in case a generated schema has them.
_OBJECT_CONSTRAINT_KEYS: frozenset[str] = frozenset({
    "minProperties",
    "maxProperties",
})


def sanitize_tool_schemas(tools: list[dict]) -> list[dict]:
    """Return a copy of ``tools`` with each tool's parameter schema sanitized.

    Input is an OpenAI-format tool list:
    ``[{"type": "function", "function": {"name": ..., "parameters": {...}}}]``

    The returned list is a deep copy — callers can safely mutate it without
    affecting the original registry entries.

    Hermes-compatible sanitization for llama.cpp / strict converters:
    handles bare-string schemas, object-without-properties, type-as-array,
    and nullable unions.
    """
    if not tools:
        return tools

    sanitized: list[dict] = []
    for tool in tools:
        sanitized.append(_sanitize_single_tool(tool))
    return sanitized


def _sanitize_single_tool(tool: dict) -> dict:
    """Deep-copy and sanitize a single OpenAI-format tool entry."""
    out = copy.deepcopy(tool)
    fn = out.get("function") if isinstance(out, dict) else None
    if not isinstance(fn, dict):
        return out

    params = fn.get("parameters")
    if not isinstance(params, dict):
        fn["parameters"] = {"type": "object", "properties": {}}
        return out

    fn["parameters"] = _sanitize_node(params, path=fn.get("name", "<tool>"))
    top = fn["parameters"]
    if not isinstance(top, dict):
        fn["parameters"] = {"type": "object", "properties": {}}
    else:
        if top.get("type") != "object":
            top["type"] = "object"
        if "properties" not in top or not isinstance(top.get("properties"), dict):
            top["properties"] = {}
    fn["parameters"] = strip_nullable_unions(fn["parameters"], keep_nullable_hint=True)
    return out


def strip_nullable_unions(
    schema: Any,
    *,
    keep_nullable_hint: bool = True,
) -> Any:
    """Collapse ``anyOf`` / ``oneOf`` nullable unions to the non-null branch.

    MCP / Pydantic optional fields commonly arrive as::

        {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}

    Anthropic's tool input-schema validator rejects the null branch. Tool
    optionality is already represented by the parent object's ``required``
    array, so we collapse the union to the single non-null variant.

    Metadata (``title``, ``description``, ``default``, ``examples``) on the
    outer union node is carried over to the replacement variant.
    """
    if isinstance(schema, list):
        return [
            strip_nullable_unions(item, keep_nullable_hint=keep_nullable_hint)
            for item in schema
        ]
    if not isinstance(schema, dict):
        return schema

    stripped = {
        k: strip_nullable_unions(v, keep_nullable_hint=keep_nullable_hint)
        for k, v in schema.items()
    }
    for key in ("anyOf", "oneOf"):
        variants = stripped.get(key)
        if not isinstance(variants, list):
            continue
        non_null = [
            item for item in variants
            if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        if len(non_null) == 1 and len(non_null) != len(variants):
            replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
            if keep_nullable_hint:
                replacement.setdefault("nullable", True)
            for meta_key in ("title", "description", "default", "examples"):
                if meta_key in stripped and meta_key not in replacement:
                    replacement[meta_key] = stripped[meta_key]
            return strip_nullable_unions(
                replacement, keep_nullable_hint=keep_nullable_hint
            )
    return stripped


def strip_anthropic_unsupported_constraints(schema: Any) -> Any:
    """Recursively strip schema constraints Anthropic's validator rejects.

    OC-specific extension over Hermes's sanitizer (Hermes will hit the same
    bugs as their tool surface grows — these are fixes for both projects).
    Anthropic's 2025 strict validator returns 400 for several JSON-Schema
    constraints that OpenAI accepts:

      Numeric (on ``integer`` / ``number`` nodes):
        - ``minimum`` / ``maximum``
        - ``exclusiveMinimum`` / ``exclusiveMaximum``
        - ``multipleOf``

      Array (on ``array`` nodes):
        - ``minItems`` (only 0 or 1 accepted; we strip rather than guess)
        - ``maxItems``
        - ``uniqueItems``
        - ``minContains`` / ``maxContains``

      Object (on ``object`` nodes):
        - ``minProperties``
        - ``maxProperties``

    Walker drops them anywhere they appear under their respective types,
    leaving everything else (description, default, enum, string-type
    minLength/maxLength/pattern, items shape, properties, required, etc.)
    untouched. Returns a deep copy — original schema is unchanged.

    Tools that need to enforce these bounds (e.g. ``ClarifyTool`` requires
    2-4 options) MUST validate at the tool layer in their ``execute()``
    method since the schema-level enforcement is gone for Anthropic.
    """
    return _strip_unsupported_constraints(copy.deepcopy(schema))


def _strip_unsupported_constraints(node: Any) -> Any:
    """In-place strip helper used by ``strip_anthropic_unsupported_constraints``.

    Operates on an already-deep-copied node so the caller's input is never
    mutated.

    Beyond stripping unsupported constraints, also INJECTS
    ``additionalProperties: false`` on every object node — Anthropic's
    strict tool validator returns 400 with "For 'object' type,
    'additionalProperties' must be explicitly set to false" when this
    field is missing. We unconditionally set it to ``False`` (the
    Anthropic-canonical value); tools that previously used
    ``additionalProperties: true`` or a sub-schema get downgraded to the
    strict shape. This is consistent with how Anthropic's MCP/tool
    schemas treat tool inputs.
    """
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type in {"integer", "number"}:
            for key in _NUMERIC_CONSTRAINT_KEYS:
                node.pop(key, None)
        elif node_type == "array":
            for key in _ARRAY_CONSTRAINT_KEYS:
                node.pop(key, None)
        elif node_type == "object":
            for key in _OBJECT_CONSTRAINT_KEYS:
                node.pop(key, None)
            # Anthropic requires every object node to declare this
            # explicitly; missing field → 400. Always set to False.
            node["additionalProperties"] = False
        for key, value in list(node.items()):
            node[key] = _strip_unsupported_constraints(value)
        return node
    if isinstance(node, list):
        return [_strip_unsupported_constraints(v) for v in node]
    return node


def _sanitize_node(node: Any, path: str) -> Any:
    """Recursively sanitize a JSON-Schema fragment.

    Verbatim port of Hermes's ``_sanitize_node`` from
    ``tools/schema_sanitizer.py``.
    """
    if isinstance(node, str):
        if node in {"object", "string", "number", "integer", "boolean", "array", "null"}:
            logger.debug(
                "schema_sanitizer[%s]: replacing bare-string schema %r "
                "with {'type': %r}",
                path, node, node,
            )
            return {"type": node} if node != "object" else {
                "type": "object",
                "properties": {},
            }
        logger.debug(
            "schema_sanitizer[%s]: replacing non-schema string %r "
            "with empty object schema", path, node,
        )
        return {"type": "object", "properties": {}}

    if isinstance(node, list):
        return [_sanitize_node(item, f"{path}[{i}]") for i, item in enumerate(node)]

    if not isinstance(node, dict):
        return node

    out: dict = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, list):
            non_null = [t for t in value if t != "null"]
            if len(non_null) == 1 and isinstance(non_null[0], str):
                out["type"] = non_null[0]
                if "null" in value:
                    out.setdefault("nullable", True)
                continue
            first_str = next(
                (t for t in value if isinstance(t, str) and t != "null"),
                None,
            )
            if first_str:
                out["type"] = first_str
                continue
            out["type"] = "object"
            continue

        if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
            out[key] = {
                sub_k: _sanitize_node(sub_v, f"{path}.{key}.{sub_k}")
                for sub_k, sub_v in value.items()
            }
        elif key in {"items", "additionalProperties"}:
            if isinstance(value, bool):
                out[key] = value
            else:
                out[key] = _sanitize_node(value, f"{path}.{key}")
        elif key in {"anyOf", "oneOf", "allOf"} and isinstance(value, list):
            out[key] = [
                _sanitize_node(item, f"{path}.{key}[{i}]")
                for i, item in enumerate(value)
            ]
        elif key in {"required", "enum", "examples"}:
            out[key] = (
                copy.deepcopy(value)
                if isinstance(value, (list, dict))
                else value
            )
        else:
            out[key] = (
                _sanitize_node(value, f"{path}.{key}")
                if isinstance(value, (dict, list))
                else value
            )

    if out.get("type") == "object" and not isinstance(out.get("properties"), dict):
        out["properties"] = {}

    if out.get("type") == "object" and isinstance(out.get("required"), list):
        props = out.get("properties") or {}
        valid = [r for r in out["required"] if isinstance(r, str) and r in props]
        if not valid:
            out.pop("required", None)
        elif len(valid) != len(out["required"]):
            out["required"] = valid

    return out


def normalize_tool_input_schema_for_anthropic(schema: Any) -> dict[str, Any]:
    """Normalize a tool ``input_schema`` for Anthropic.

    Mirrors Hermes's ``_normalize_tool_input_schema`` in
    ``agent/anthropic_adapter.py`` and adds the numeric-constraint
    stripping that fixes the 400 "minimum/maximum not supported" error.

    Pipeline:
      1. Deep-copy + ``strip_nullable_unions(keep_nullable_hint=False)`` —
         Anthropic doesn't recognize the OpenAPI ``nullable`` extension.
      2. ``strip_anthropic_unsupported_constraints`` — drop minimum/maximum/
         exclusiveMin/exclusiveMax/multipleOf from int/number nodes.
      3. Ensure top-level ``type: object`` with a ``properties`` dict.
    """
    if not schema:
        return {"type": "object", "properties": {}}

    normalized = strip_nullable_unions(schema, keep_nullable_hint=False)
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}

    normalized = strip_anthropic_unsupported_constraints(normalized)

    if normalized.get("type") == "object" and not isinstance(
        normalized.get("properties"), dict
    ):
        normalized = {**normalized, "properties": {}}
    return normalized


__all__ = [
    "normalize_tool_input_schema_for_anthropic",
    "sanitize_tool_schemas",
    "strip_anthropic_unsupported_constraints",
    "strip_nullable_unions",
]
