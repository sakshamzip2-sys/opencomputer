"""Anthropic prompt caching.

Reduces input token costs by caching the conversation prefix using up to 4
``cache_control`` breakpoints (Anthropic max).

Two entry points:

- ``apply_anthropic_cache_control(messages)``: legacy. Caches system +
  last 3 non-system messages. Used by callers that don't send tools.

- ``apply_full_cache_control(messages, tools)``: preferred. Returns
  ``(messages, tools)`` with breakpoints allocated as
  ``tools[-1] + system + last 2 non-system messages`` (4 total). Tool
  definitions (~8-30k tokens for ~40 tools) change rarely → highest
  cache hit rate. The deepest message breakpoint has the lowest hit
  rate (every turn changes the tail), so dropping it costs least.

Pure functions — no class state, no AIAgent dependency.
"""

import copy
from typing import Any


def _build_marker(cache_ttl: str) -> dict[str, Any]:
    marker: dict[str, Any] = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def _apply_cache_marker(msg: dict, cache_marker: dict, native_anthropic: bool = False) -> None:
    """Add cache_control to a single message, handling all format variations."""
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool":
        if native_anthropic:
            msg["cache_control"] = cache_marker
        return

    if content is None or content == "":
        msg["cache_control"] = cache_marker
        return

    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": cache_marker}
        ]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = cache_marker


def _cache_tail_messages(
    messages: list[dict[str, Any]],
    n_tail: int,
    marker: dict[str, Any],
    native_anthropic: bool,
) -> None:
    """Mark up to n_tail non-system messages from the end."""
    non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
    for idx in non_sys[-n_tail:]:
        _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)


def apply_anthropic_cache_control(
    api_messages: list[dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
) -> list[dict[str, Any]]:
    """Legacy: 4 breakpoints on messages only (system + last 3).

    Preserved for backwards compatibility. Prefer ``apply_full_cache_control``
    when sending tools.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = _build_marker(cache_ttl)
    breakpoints_used = 0

    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        breakpoints_used += 1

    _cache_tail_messages(messages, 4 - breakpoints_used, marker, native_anthropic)
    return messages


def apply_full_cache_control(
    api_messages: list[dict[str, Any]],
    api_tools: list[dict[str, Any]] | None,
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply 4-breakpoint strategy across messages AND tools array.

    With tools (non-empty):  tools[-1] + system + last 2 non-system msgs (4 total)
    Without tools:           system + last 3 non-system msgs            (4 total)

    Returns ``(messages, tools)`` — both deep-copied. ``api_tools=None`` is
    treated as ``[]``. Inputs are not mutated.
    """
    messages = copy.deepcopy(api_messages)
    tools = copy.deepcopy(api_tools) if api_tools else []

    if not messages and not tools:
        return messages, tools

    marker = _build_marker(cache_ttl)

    tools_used = 0
    if tools:
        tools[-1]["cache_control"] = marker
        tools_used = 1

    sys_used = 0
    if messages and messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        sys_used = 1

    remaining = 4 - tools_used - sys_used
    if remaining > 0 and messages:
        _cache_tail_messages(messages, remaining, marker, native_anthropic)

    return messages, tools
