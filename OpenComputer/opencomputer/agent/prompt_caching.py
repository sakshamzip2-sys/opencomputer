"""Anthropic prompt caching (system_and_3 strategy).

Reduces input token costs by ~75% on multi-turn conversations by caching
the conversation prefix. Uses 4 cache_control breakpoints (Anthropic max):
  1. System prompt (stable across all turns)
  2-4. Last 3 non-system messages (rolling window)

Pure functions -- no class state, no AIAgent dependency.
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


def apply_anthropic_cache_control(
    api_messages: list[dict[str, Any]],
    cache_ttl: str = "5m",
    native_anthropic: bool = False,
    min_cache_tokens: int = 0,
) -> list[dict[str, Any]]:
    """Apply system_and_3 caching strategy to messages for Anthropic models.

    Places up to 4 cache_control breakpoints: system prompt + last 3 non-system
    messages.

    ``min_cache_tokens`` filters out blocks whose estimated token count is
    below the model's prompt-cache minimum (e.g. 4096 for Opus). Marking a
    sub-threshold block is a silent server-side no-op that wastes a
    breakpoint slot. When a candidate is too small, we walk back through
    earlier non-system messages (up to the Anthropic 20-block lookback
    window) to find one that clears the threshold. If none does, the
    breakpoint slot is simply not used; the request proceeds with fewer
    or zero cache markers.

    Returns:
        Deep copy of messages with cache_control breakpoints injected.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"

    breakpoints_used = 0

    if messages[0].get("role") == "system":
        # System prompts are by convention always large enough to be worth
        # caching; don't filter the system slot. Cheap insurance against a
        # tiny test-only system prompt failing to threshold.
        _apply_cache_marker(messages[0], marker, native_anthropic=native_anthropic)
        breakpoints_used += 1

    remaining = 4 - breakpoints_used
    non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]

    # Pick up to ``remaining`` indices from the tail of non_sys, skipping
    # blocks that fall below ``min_cache_tokens``. For each slot we walk
    # back through unused indices up to the lookback window.
    chosen: list[int] = []
    used: set[int] = set()
    for slot_offset in range(remaining):
        # Tail-anchored start point for this slot.
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

    return messages


_LONG_TTL_THRESHOLD_SECONDS = 240.0  # 4 minutes — leaves 1m below the 5m cache TTL


def select_cache_ttl(*, supports_long_ttl: bool, idle_seconds: float) -> str:
    """Decide between '5m' (default) and '1h' (long) cache TTL.

    Returns ``"1h"`` only when:
      * the provider declares ``supports_long_ttl`` True, AND
      * the gap since the last assistant turn exceeds 4 minutes.

    The 4-minute threshold leaves a one-minute safety buffer below the
    default 5-minute cache lifetime, so a session that pauses for 5+
    minutes would otherwise pay a full re-prefill on the next turn.
    The 1h TTL costs 2x base on cache write but the spend is recouped
    after one hit; for a typical coding session with multi-minute
    "thinking" gaps between turns this is a clean win.
    """
    if not supports_long_ttl:
        return "5m"
    if idle_seconds > _LONG_TTL_THRESHOLD_SECONDS:
        return "1h"
    return "5m"
