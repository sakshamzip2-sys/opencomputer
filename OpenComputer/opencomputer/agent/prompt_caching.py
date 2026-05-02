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

``min_cache_tokens`` (default 0) filters out blocks whose estimated
token count is below the model's prompt-cache minimum (e.g. 4096 for
Opus). Marking a sub-threshold block is a silent server-side no-op
that wastes a breakpoint slot. When a candidate is too small we walk
back through earlier non-system messages (up to Anthropic's 20-block
server-side lookback window) to find one that clears the threshold.

``select_cache_ttl(supports_long_ttl, idle_seconds)`` decides between
the default 5-minute TTL and the optional 1-hour TTL: when a session
has been idle longer than ~4 minutes (the 5m cache would have expired)
and the provider supports it, we switch to 1h to avoid paying a full
re-prefill on the next turn.

Pure functions — no class state, no AIAgent dependency.
"""

import copy
from typing import Any

#: Anthropic's server-side cache lookback window. We mirror this client-side
#: when walking back to find the most recent block eligible for a cache_control
#: marker — going further than this can't pay off because the server stops
#: looking too.
_LOOKBACK_WINDOW = 20

#: Cheap chars→tokens approximation. We are filtering for cache eligibility,
#: not billing — over-counting (lower token estimate) errs on the side of
#: "skip the marker", which is the safe direction (a missed cache write is
#: cheaper than a wasted breakpoint slot).
_CHARS_PER_TOKEN = 4

#: Idle-aware long-TTL switch threshold. 4 minutes leaves a 1-minute
#: safety buffer below the default 5-minute cache TTL.
_LONG_TTL_THRESHOLD_SECONDS = 240.0


def _block_token_estimate(content: Any) -> int:
    """Cheap upper-bound token count for a message's content."""
    if isinstance(content, str):
        return len(content) // _CHARS_PER_TOKEN
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                total += len(block.get("text", ""))
        return total // _CHARS_PER_TOKEN
    return 0


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
    min_cache_tokens: int = 0,
) -> None:
    """Mark up to n_tail non-system messages from the end.

    When ``min_cache_tokens`` > 0, sub-threshold blocks are skipped:
    we walk back through earlier non-system messages (up to Anthropic's
    20-block server-side lookback window) to find ones that clear the
    threshold. Marking a sub-threshold block is a silent server-side
    no-op that wastes a breakpoint slot.
    """
    non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
    if min_cache_tokens <= 0:
        # Fast path — preserve existing behaviour for callers that don't filter.
        for idx in non_sys[-n_tail:]:
            _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)
        return

    chosen: list[int] = []
    used: set[int] = set()
    for slot_offset in range(n_tail):
        start = len(non_sys) - 1 - slot_offset
        if start < 0:
            break
        for walk in range(_LOOKBACK_WINDOW):
            cand = start - walk
            if cand < 0:
                break
            idx = non_sys[cand]
            if idx in used:
                continue
            est = _block_token_estimate(messages[idx].get("content"))
            if est >= min_cache_tokens:
                chosen.append(idx)
                used.add(idx)
                break

    for idx in chosen:
        _apply_cache_marker(messages[idx], marker, native_anthropic=native_anthropic)


def apply_anthropic_cache_control(
    api_messages: list[dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
    min_cache_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Legacy: 4 breakpoints on messages only (system + last 3).

    Preserved for backwards compatibility. Prefer ``apply_full_cache_control``
    when sending tools.

    ``min_cache_tokens`` (default 0) filters sub-threshold blocks; see
    module docstring.

    Returns:
        Deep copy of messages with cache_control breakpoints injected.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = _build_marker(cache_ttl)
    breakpoints_used = 0

    if messages[0].get("role") == "system":
        # System prompts are by convention always large enough to be worth
        # caching; don't filter the system slot.
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        breakpoints_used += 1

    _cache_tail_messages(
        messages,
        4 - breakpoints_used,
        marker,
        native_anthropic,
        min_cache_tokens=min_cache_tokens,
    )
    return messages


def apply_full_cache_control(
    api_messages: list[dict[str, Any]],
    api_tools: list[dict[str, Any]] | None,
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
    min_cache_tokens: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply 4-breakpoint strategy across messages AND tools array.

    With tools (non-empty):  tools[-1] + system + last 2 non-system msgs (4 total)
    Without tools:           system + last 3 non-system msgs            (4 total)

    Returns ``(messages, tools)`` — both deep-copied. ``api_tools=None`` is
    treated as ``[]``. Inputs are not mutated. ``min_cache_tokens`` forwards
    through to the message-tail filter.
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
        _cache_tail_messages(
            messages, remaining, marker, native_anthropic,
            min_cache_tokens=min_cache_tokens,
        )

    return messages, tools


def select_cache_ttl(*, supports_long_ttl: bool, idle_seconds: float) -> str:
    """Decide between '5m' (default) and '1h' (long) cache TTL.

    Returns ``"1h"`` only when:
      * the provider declares ``supports_long_ttl`` True, AND
      * the gap since the last assistant turn exceeds 4 minutes.

    The 4-minute threshold leaves a one-minute safety buffer below the
    default 5-minute cache lifetime. The 1h TTL costs 2x base on cache
    write but is recouped after one hit; for a typical coding session
    with multi-minute "thinking" gaps between turns this is a clean win.
    """
    if not supports_long_ttl:
        return "5m"
    if idle_seconds > _LONG_TTL_THRESHOLD_SECONDS:
        return "1h"
    return "5m"
