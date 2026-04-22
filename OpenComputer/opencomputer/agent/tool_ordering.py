"""Deterministic tool ordering for API requests.

Both Anthropic and OpenAI prefix-cache the request based on byte-level
equality of the tools block. If tool order drifts between turns — a plugin
reloaded, an MCP server appeared or disappeared, dict iteration shifted —
the prefix cache misses and the next turn is billed at full input cost.

This module provides the single chokepoint that every request-assembly site
is supposed to route through. Sorting here is *defensive*: the tool registry
already keeps insertion order (Python dicts since 3.7), but plugin-load
order varies with filesystem enumeration and MCP hot-reconnects add tools
mid-process — so an explicit `sorted()` is correctness, not optimization.

Source: openclaw src/agents/pi-bundle-mcp-materialize.ts:115-118 documents
the same defensive sort with the comment "Sort tools deterministically by
name so the tools block in API requests is stable across turns".
See /Users/saksham/.claude/plans/what-all-do-you-misty-cookie.md §3.2.
"""

from __future__ import annotations

from plugin_sdk.tool_contract import ToolSchema


def sort_tools_for_request(tools: list[ToolSchema]) -> list[ToolSchema]:
    """Return tools ordered by name ascending. Stable, deterministic, cheap."""
    return sorted(tools, key=lambda t: t.name)


__all__ = ["sort_tools_for_request"]
