"""Context pruning modes — drop entries by rule before compaction runs.

OpenClaw parity (``contextPruning.mode``): a cheap, lossy alternative
to (or complement to) LLM-driven summarisation. Two strategies:

* ``sliding`` — keep the last ``window_turns`` *user* turns verbatim
  along with everything that follows them; drop earlier traffic.
  Per-turn semantics rather than per-message because drops must
  always preserve ``tool_use`` ↔ ``tool_result`` pairs (Anthropic
  400s on a tool_use without its tool_result; see
  ``CompactionEngine._safe_split_index`` for the established
  invariant).

* ``cache-ttl`` — drop messages older than ``ttl_seconds`` based on
  their ``timestamp`` attribute. Messages without timestamps survive
  (we never gamble with messages we can't time-stamp).

Distinct from :class:`opencomputer.agent.compaction.CompactionEngine`.
Pruning runs *first*, then if the pruned set still exceeds the
context budget the compactor's aux-LLM summarisation kicks in. The
two work together — pruning bounds the cheap-to-keep window;
compaction handles the long tail.

Defaults to ``mode="none"`` so existing sessions are byte-identical
until the operator opts in.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from plugin_sdk.core import Message

__all__ = [
    "ContextPruningConfig",
    "ContextPruningMode",
    "prune_messages",
]


_log = logging.getLogger("opencomputer.agent.context_pruning")


ContextPruningMode = Literal["none", "sliding", "cache-ttl"]


@dataclass(frozen=True, slots=True)
class ContextPruningConfig:
    """OpenClaw-parity ``contextPruning`` settings.

    Attributes:
        mode:           ``none`` (default), ``sliding``, or ``cache-ttl``.
        window_turns:   for ``sliding`` mode, how many user turns to keep
                        verbatim. The first system message (if any) is
                        always preserved; tool_use/tool_result pairs
                        attached to kept turns are preserved.
        ttl_seconds:    for ``cache-ttl`` mode, drop entries older than
                        this many seconds. Messages without timestamps
                        are kept (cannot evaluate freshness safely).
        always_keep_system: keep the leading system message regardless
                        of mode. True by default — losing the system
                        prompt mid-session would change behaviour.
    """

    mode: ContextPruningMode = "none"
    window_turns: int = 12
    ttl_seconds: int = 60 * 60  # 1 hour
    always_keep_system: bool = True


def prune_messages(
    messages: list[Message],
    config: ContextPruningConfig,
    *,
    now: float | None = None,
) -> list[Message]:
    """Apply the configured pruning strategy. Returns a new list.

    Pure function — never mutates *messages*. Returns the input
    unchanged when ``mode == "none"`` so the call is essentially a
    no-op when pruning is disabled.

    *now* lets tests inject a fixed clock; production callers omit it.
    """
    if config.mode == "none" or not messages:
        return list(messages)
    try:
        if config.mode == "sliding":
            return _prune_sliding(messages, config)
        if config.mode == "cache-ttl":
            return _prune_cache_ttl(messages, config, now=now or time.time())
    except Exception:  # noqa: BLE001 — defensive: never break the loop
        _log.warning(
            "context_pruning: %s strategy crashed — returning original messages",
            config.mode, exc_info=True,
        )
    return list(messages)


# ─── strategies ───────────────────────────────────────────────────────


def _is_system(msg: Message) -> bool:
    return getattr(msg, "role", "") == "system"


def _is_user(msg: Message) -> bool:
    return getattr(msg, "role", "") == "user"


def _has_tool_use(msg: Message) -> bool:
    """Detect an assistant message that includes ``tool_use`` blocks.

    Anthropic-shaped content is a ``list[dict]`` where each item has a
    ``type`` field; OpenAI-shaped is ``tool_calls`` on the message.
    Either way: if we see a tool reference, treat the message as
    "carries a pair" so we don't orphan it.
    """
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                return True
    return bool(getattr(msg, "tool_calls", None))


def _has_tool_result(msg: Message) -> bool:
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                return True
    return bool(getattr(msg, "tool_call_id", None))


def _prune_sliding(
    messages: list[Message], config: ContextPruningConfig,
) -> list[Message]:
    """Keep the last ``window_turns`` user turns + everything after them."""
    if config.window_turns <= 0:
        return list(messages)

    # Walk backwards counting user turns.
    user_indices: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        if _is_user(messages[i]):
            user_indices.append(i)
            if len(user_indices) >= config.window_turns:
                break
    if len(user_indices) < config.window_turns:
        # We don't have that many user turns — pruning would either be
        # a no-op or drop everything. Return the input unchanged.
        return list(messages)

    cutoff = user_indices[-1]
    # Walk back further to capture any preceding assistant message
    # that has tool_use blocks whose tool_result lives at cutoff or
    # later. Anthropic 400s on tool_use without its tool_result, so
    # we extend the kept window to keep pairs intact. We never *drop*
    # a tool_result without its parent tool_use either, but since
    # tool_result always follows its tool_use this only matters going
    # backward.
    while cutoff > 0:
        prev = messages[cutoff - 1]
        if _has_tool_use(prev) or _has_tool_result(prev):
            cutoff -= 1
            continue
        break

    head: list[Message] = []
    if config.always_keep_system and messages and _is_system(messages[0]):
        head.append(messages[0])
        # If cutoff already includes the system message, don't double it.
        if cutoff == 0:
            cutoff = 1

    pruned = head + list(messages[cutoff:])
    _log.debug(
        "context_pruning: sliding kept %d/%d msgs (window_turns=%d)",
        len(pruned), len(messages), config.window_turns,
    )
    return pruned


def _prune_cache_ttl(
    messages: list[Message],
    config: ContextPruningConfig,
    *,
    now: float,
) -> list[Message]:
    """Drop messages older than ``ttl_seconds``.

    "Older" is computed against ``msg.timestamp`` if present, treating
    timestamps as UNIX seconds. Messages without a timestamp survive
    by default — pruning blind would silently corrupt history.

    Tool-pair preservation: a ``tool_result`` whose parent ``tool_use``
    has been pruned is also dropped (and vice versa) so the wire
    invariant stays intact.
    """
    if config.ttl_seconds <= 0:
        return list(messages)
    cutoff_ts = now - config.ttl_seconds

    # First pass — mark indices to keep.
    keep = [True] * len(messages)
    for i, msg in enumerate(messages):
        if config.always_keep_system and _is_system(msg):
            continue
        ts = _msg_timestamp(msg)
        if ts is None:
            continue  # untimed messages survive
        if ts < cutoff_ts:
            keep[i] = False

    # Second pass — preserve tool_use/tool_result pairs.
    # If a tool_use is kept but its result is dropped (or vice versa),
    # we must drop both to keep the wire balanced. Pairs are typically
    # adjacent (assistant tool_use followed by user tool_result), so a
    # localised pair-walk handles the common case.
    for i, msg in enumerate(messages):
        if not _has_tool_use(msg):
            continue
        # Find the next message — if it's the matching tool_result,
        # the pair must agree.
        if i + 1 < len(messages) and _has_tool_result(messages[i + 1]):
            if keep[i] != keep[i + 1]:
                # Drop both for safety.
                keep[i] = False
                keep[i + 1] = False

    pruned = [m for i, m in enumerate(messages) if keep[i]]
    _log.debug(
        "context_pruning: cache-ttl kept %d/%d msgs (ttl=%ds)",
        len(pruned), len(messages), config.ttl_seconds,
    )
    return pruned


def _msg_timestamp(msg: Any) -> float | None:
    """Best-effort timestamp lookup. Accepts float seconds, datetime, or None."""
    ts = getattr(msg, "timestamp", None)
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    # datetime objects.
    epoch = getattr(ts, "timestamp", None)
    if callable(epoch):
        try:
            return float(epoch())
        except Exception:  # noqa: BLE001
            return None
    return None
