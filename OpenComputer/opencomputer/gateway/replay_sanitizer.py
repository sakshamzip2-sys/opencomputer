"""Replay sanitization for cold-start message catch-up (OpenClaw 1.D port).

When the gateway restarts after a network blip or process crash, buffered
messages may contain:
  - Stale assistant turns that were already streamed to the user (replay=True)
  - Outgoing-queue items still in flight (in_flight=True)
  - User messages older than ``max_age_seconds`` that are likely stale

``sanitize_for_replay`` drops these before re-feeding into Dispatch.

**Status:** the function is correct AGAINST messages that carry the right
markers. As of this PR, no writer in OC sets ``replay`` / ``in_flight`` on
messages — that's a deliberate scope split:

  - This PR ships the sanitizer + tests so the logic is reviewable in
    isolation.
  - A FOLLOW-UP PR adds schema columns + writer changes (gateway sets
    in_flight on enqueue/clears on ACK; dispatch sets replay=True on
    pre-shutdown buffered text). Until that lands, the function is a no-op
    for real Message rows (none have these markers).

  - The sanitizer is fully backwards-compatible: messages without the
    markers pass through unchanged.

Per AMENDMENTS Fix H6: the originally-planned single-PR scope is split
into two for safety. Schema migration is the heavier change.
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal


def sanitize_for_replay(
    messages: Iterable[Any],
    *,
    max_age_seconds: int = 300,
    now: float | None = None,
) -> list[Any]:
    """Drop stale / in-flight / over-aged messages from a replay batch.

    Accepts an iterable of message-like objects (dicts or attribute-bearing
    dataclasses). Each message is inspected for these markers:
      - ``replay`` (truthy) → drop (already-delivered assistant turn)
      - ``in_flight`` (truthy) → drop (outgoing-queue retry will deliver)
      - role == "user" AND ``ts`` < (now - max_age_seconds) → drop

    Messages without these markers pass through unchanged. Order is preserved
    for survivors.

    Args:
        messages: iterable of dicts OR objects with attribute-style access.
        max_age_seconds: drop user messages older than this many seconds.
        now: optional override for time.time() (used by tests).

    Returns:
        Filtered list of messages (copies of survivors; never mutates input).
    """
    cutoff = (now if now is not None else time.time()) - max_age_seconds
    out: list[Any] = []
    for m in messages:
        if _get(m, "replay"):
            continue
        if _get(m, "in_flight"):
            continue
        role = _get(m, "role")
        ts = _get(m, "ts")
        if role == "user" and isinstance(ts, (int, float)) and ts < cutoff:
            continue
        out.append(m)
    return out


def _get(m: Any, key: str) -> Any:
    """Read ``key`` from dict or attribute-style object; return None if absent."""
    if isinstance(m, dict):
        return m.get(key)
    return getattr(m, key, None)


# ─── A4 — partial-message recovery (2026-05-06 OpenClaw deep-comparison) ───


@dataclass(frozen=True, slots=True)
class PartialRecoveryResult:
    """Outcome of attempting to recover a partial assistant stream.

    * ``status`` — what to do with the partial text:
      - ``"recoverable"`` → ``text`` is safe to re-emit to the channel
      - ``"unrecoverable"`` → mid-tool-call interruption; ``reason``
        explains why and the caller should drop the partial.
    * ``text`` — the cleaned-up text (may be shorter than input if
      a half-rendered tool-call XML chunk was trimmed).
    * ``reason`` — short human-readable diagnosis.
    """

    status: Literal["recoverable", "unrecoverable"]
    text: str
    reason: str


# Open-tag-without-close patterns. These detect partial XML/markup left
# behind by a stream that cut mid-tool-call. Order matters: we trim from
# the LATEST open tag since the rest of the text before it is intact.
_OPEN_BLOCK_TAGS: tuple[str, ...] = (
    "thinking",
    "function_calls",
    "antml:function_calls",
    "tool_use",
)


def _last_open_tag_index(text: str) -> int | None:
    """Return the index of the latest unbalanced `<tag>` opener in text.

    A tag is "unbalanced" if no matching `</tag>` appears AFTER its
    opener. Returns the byte offset of the offending `<` so the caller
    can trim the text.
    """
    candidates: list[int] = []
    for tag in _OPEN_BLOCK_TAGS:
        # Walk left-to-right; for each opener, look for a closer later in
        # the text. If no closer, this opener is unbalanced.
        pattern_open = re.compile(r"<" + re.escape(tag) + r"\b")
        pattern_close = re.compile(r"</" + re.escape(tag) + r">")
        for m in pattern_open.finditer(text):
            tail = text[m.end():]
            if not pattern_close.search(tail):
                candidates.append(m.start())
                break  # only the FIRST unbalanced opener for each tag matters
    if not candidates:
        return None
    return min(candidates)


def _has_dangling_minimax_invoke(text: str) -> bool:
    """Detect MiniMax-style malformed invoke fragment (no closing block)."""
    # MiniMax models sometimes emit `<|invoke|>...` without a matching close.
    # The existing inbound sanitizer strips full tokens; here we only care
    # whether a HALF-rendered fragment is present at the tail.
    pat = re.compile(r"<\|(invoke|tool_call|tool_use)\|>")
    matches = list(pat.finditer(text))
    if not matches:
        return False
    last = matches[-1]
    # If the last opener has no closing `<|/...|>` after it, it's dangling.
    closer = re.compile(r"<\|/(invoke|tool_call|tool_use)\|>")
    return not closer.search(text[last.end():])


def recover_partial_assistant(
    text: str,
    *,
    drop_threshold_chars: int = 8,
) -> PartialRecoveryResult:
    """Attempt to salvage a partial assistant stream interrupted mid-flight.

    Used by the gateway when a stream is cut by network drop, gateway
    restart, or upstream timeout. The partial text may contain a
    half-rendered tool-call XML chunk — we either trim it cleanly (if
    everything before the chunk is intact text) or mark the partial
    unrecoverable (if there's nothing salvageable left after trim).

    Args:
        text: raw partial assistant text as accumulated from the stream.
        drop_threshold_chars: if the recovered text is shorter than this,
            mark unrecoverable rather than emit a near-empty fragment.

    Returns:
        PartialRecoveryResult with status + recovered text + reason.
    """
    if not text or not text.strip():
        return PartialRecoveryResult(
            status="unrecoverable",
            text="",
            reason="empty partial",
        )

    if _has_dangling_minimax_invoke(text):
        return PartialRecoveryResult(
            status="unrecoverable",
            text="",
            reason="MiniMax-style invoke fragment cut mid-stream",
        )

    open_idx = _last_open_tag_index(text)
    if open_idx is None:
        # No open block tags — text is clean prose. Recoverable as-is.
        return PartialRecoveryResult(
            status="recoverable",
            text=text,
            reason="no dangling tool-call markup",
        )

    head = text[:open_idx].rstrip()
    if len(head) < drop_threshold_chars:
        return PartialRecoveryResult(
            status="unrecoverable",
            text="",
            reason=(
                f"only {len(head)} char(s) of clean prose before "
                f"unclosed tag at offset {open_idx}"
            ),
        )

    return PartialRecoveryResult(
        status="recoverable",
        text=head,
        reason=f"trimmed unclosed tag at offset {open_idx}",
    )


__all__ = [
    "PartialRecoveryResult",
    "recover_partial_assistant",
    "sanitize_for_replay",
]
