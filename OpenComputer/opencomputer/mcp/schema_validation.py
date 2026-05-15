"""Tool-argument schema validation for MCP calls (Gap C).

mcp-openclaw-port follow-up. Each MCP tool's manifest declares an
``inputSchema`` (a JSON Schema dict) describing what arguments the
tool accepts. OC's :class:`MCPTool.parameters` carries that schema —
this module validates ``ToolCall.arguments`` against it BEFORE
dispatching to ``ClientSession.call_tool``.

Why bother — the MCP server validates too, doesn't it?
* MCP servers return generic "invalid params" errors. OC's validation
  surfaces the offending field path inline so the agent sees a clear
  error like ``required 'name' is missing`` instead of ``-32602 invalid
  params``.
* Cuts a round-trip when args are clearly wrong (the LLM gets retry
  feedback faster).
* Defence-in-depth — a buggy MCP server schema mismatch (e.g. the
  server declares one schema and accepts a different one) is caught.

Permissive on missing schema. When ``parameters`` is empty, ``None``,
or non-object-typed, we skip validation entirely and let the MCP
server be the authority. This is the same posture OpenClaw and Hermes
take.
"""

from __future__ import annotations

import logging
from typing import Any

import jsonschema
from jsonschema import Draft7Validator

logger = logging.getLogger("opencomputer.mcp.schema_validation")


class SchemaValidationError(ValueError):
    """Raised when ``ToolCall.arguments`` fails the tool's inputSchema.

    Carries a one-line summary suitable for surfacing back to the agent
    as the tool's error response. The full jsonschema error is
    available via :attr:`original` for advanced callers.
    """

    def __init__(self, message: str, original: Exception | None = None) -> None:
        super().__init__(message)
        self.original = original


def validate_tool_arguments(
    arguments: dict[str, Any],
    schema: dict[str, Any] | None,
) -> None:
    """Validate ``arguments`` against ``schema`` (JSON Schema Draft 7).

    Raises :class:`SchemaValidationError` on the first failed check.
    Returns ``None`` on success.

    Permissive cases (no exception):

    * ``schema`` is ``None``, empty dict, or ``type != "object"``.
    * ``schema`` lacks a ``properties`` field AND has no ``required``
      and no ``additionalProperties`` constraint — i.e. it's
      effectively "anything goes".

    All other cases run the schema through :class:`Draft7Validator`.
    The first error wins; we don't enumerate every failure (the LLM
    only needs to fix one to get further along).
    """
    if not schema:
        return
    if not isinstance(schema, dict):
        return
    schema_type = schema.get("type")
    if schema_type and schema_type != "object":
        # The tool takes a non-object argument (rare — MCP convention
        # is object-shaped args). Skip validation; let the server decide.
        return
    try:
        validator = Draft7Validator(schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: e.path)
    except jsonschema.SchemaError as e:
        # The schema itself is malformed. Don't block — let the server
        # surface its own error if any. Log for ops visibility.
        logger.debug(
            "MCP tool schema is malformed (skipping validation): %s", e,
        )
        return
    if not errors:
        return
    first = errors[0]
    # Build a path like "user.name" or "items[2]" so the error message
    # tells the agent exactly which field to fix.
    parts: list[str] = []
    for segment in first.absolute_path:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            if parts:
                parts.append(f".{segment}")
            else:
                parts.append(str(segment))
    field_path = "".join(parts) or "<root>"
    message = (
        f"tool arguments failed schema validation: {field_path}: {first.message}"
    )
    raise SchemaValidationError(message, original=first)


__all__ = [
    "SchemaValidationError",
    "validate_tool_arguments",
]
