"""``ToolSearch`` — discover registered tools and fetch full schemas on demand.

CC §4 from docs/OC-FROM-CLAUDE-CODE.md (partial implementation — the
user-visible primitive). The full architectural optimization (inject
only names at startup, fetch schemas lazily) is documented in CLAUDE.md
as next-pass work; this tool gives the agent the lookup surface today
so plugins / curious agents can introspect the tool catalog.

Two modes:

  - ``ToolSearch(query="<name-or-keyword>")`` — fuzzy match against
    registered tool names + descriptions; returns matching tools
    with their full schemas.
  - ``ToolSearch(name="<exact-tool-name>")`` — exact lookup; returns
    a single tool's schema or an error.

Output is JSON-serialised so the agent can parse it deterministically.
Empty result is a 2xx with ``{"matches": []}`` rather than an error —
the agent can branch on shape.
"""

from __future__ import annotations

import json

from opencomputer.tools.registry import registry
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

#: Max matches returned in one call. Keeps the result small enough
#: that the agent doesn't waste tokens on a 50-tool dump when it
#: probably wanted just one.
_MAX_MATCHES: int = 10


class ToolSearch(BaseTool):
    """Discover registered tools by name or keyword and fetch full schemas.

    Use cases:
      - Plugin / agent introspection: "what tools exist?"
      - Schema fetch by name: "what arguments does ``Edit`` take?"
      - Fuzzy lookup: "I want to delete a file, what's that tool called?"
    """

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ToolSearch",
            description=(
                "Discover registered tools and fetch their full schemas. "
                "Use ``name`` for an exact lookup (returns one schema) or "
                "``query`` for a fuzzy substring match against names + "
                "descriptions (returns up to "
                f"{_MAX_MATCHES} matches). Returns JSON: "
                '``{"matches": [{"name": "...", "description": "...", '
                '"parameters": {...}}]}``.'
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Exact tool name (case-sensitive). When set, "
                            "``query`` is ignored. Returns the tool's full "
                            "schema or ``{}`` if no tool with that name."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Keyword to substring-match against tool names "
                            "AND descriptions (case-insensitive). Empty / "
                            "missing returns the full tool list (capped)."
                        ),
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        name = (args.get("name") or "").strip()
        query = (args.get("query") or "").strip().lower()

        if name:
            tool = registry.get(name)
            if tool is None:
                payload = {
                    "matches": [],
                    "error": (
                        f"no registered tool named {name!r}. "
                        f"Try ToolSearch(query=...) for a fuzzy lookup."
                    ),
                }
                return ToolResult(
                    tool_call_id=call.id,
                    content=json.dumps(payload),
                    is_error=False,  # 'not found' is not a tool error
                )
            payload = {"matches": [_serialize(tool.schema)]}
            return ToolResult(
                tool_call_id=call.id, content=json.dumps(payload)
            )

        # Fuzzy / unrestricted mode.
        all_schemas = registry.schemas()
        matched: list[ToolSchema] = []
        if not query:
            matched = list(all_schemas)[:_MAX_MATCHES]
        else:
            for s in all_schemas:
                hay = (s.name + " " + (s.description or "")).lower()
                if query in hay:
                    matched.append(s)
                if len(matched) >= _MAX_MATCHES:
                    break
        payload = {
            "matches": [_serialize(s) for s in matched],
            "total_registered": len(all_schemas),
            "returned": len(matched),
            "capped_at": _MAX_MATCHES,
        }
        return ToolResult(
            tool_call_id=call.id, content=json.dumps(payload)
        )


def _serialize(schema: ToolSchema) -> dict:
    """Render a ToolSchema as a JSON-friendly dict.

    Strips any non-JSON-able fields defensively (the schema dataclass
    is documented as JSON-friendly but a plugin could pass odd
    parameter shapes).
    """
    try:
        return {
            "name": schema.name,
            "description": schema.description,
            "parameters": json.loads(json.dumps(schema.parameters)),
        }
    except (TypeError, ValueError):
        return {
            "name": schema.name,
            "description": schema.description,
            "parameters": {
                "type": "object",
                "error": "parameters not JSON-serialisable",
            },
        }


__all__ = ["ToolSearch"]
