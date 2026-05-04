"""Truncation helper for platform message-length caps (Wave 6.E.6).

Hermes truncates kanban notification deliveries to 3800 chars + an
ellipsis marker so they fit Telegram (4096) / Discord (2000-ish per
chunk) / Matrix (60k but practically smaller) without breaking
markdown rendering on the channel.

Two helpers:

- :func:`truncate_for_platform` — naive cut at ``max_len`` minus the
  ellipsis suffix length. Safe for plain text; will sometimes split a
  markdown code fence or link.
- :func:`truncate_smart` — looks back from the cut point for a closed
  code fence and prefers cutting there. Falls back to the naive cut
  if no fence boundary is found within ``lookback`` characters.
"""

from __future__ import annotations

ELLIPSIS = "\n\n…[truncated]"
DEFAULT_MAX_LEN = 3800


def truncate_for_platform(text: str, max_len: int = DEFAULT_MAX_LEN) -> str:
    """Cut ``text`` to fit in ``max_len`` characters total.

    If ``text`` already fits, it's returned unchanged. Otherwise we
    take the first ``max_len - len(ELLIPSIS)`` characters and append
    the ellipsis marker. The marker counts toward ``max_len`` so the
    returned string is always ≤ ``max_len``.
    """
    if len(text) <= max_len:
        return text
    keep = max_len - len(ELLIPSIS)
    if keep <= 0:
        return ELLIPSIS[:max_len]
    return text[:keep] + ELLIPSIS


def truncate_smart(
    text: str,
    max_len: int = DEFAULT_MAX_LEN,
    *,
    lookback: int = 200,
) -> str:
    """Like :func:`truncate_for_platform` but tries to preserve markdown.

    Audit lens A5: a naive cut can split a code fence or markdown link
    mid-token, producing broken render on the destination platform.
    This helper:

    1. Computes the naive cut point ``keep``.
    2. Counts triple-backticks in ``text[:keep]``. If the count is even,
       no fence is open at the cut → naive cut is fine.
    3. If a fence is open, scans backwards up to ``lookback`` chars
       for the most recent ``\\n``\\`\\`\\`\\n` boundary and cuts there.
    4. Falls back to the naive cut if no clean boundary is found.
    """
    if len(text) <= max_len:
        return text
    keep = max_len - len(ELLIPSIS)
    if keep <= 0:
        return ELLIPSIS[:max_len]

    # Open-fence detection: count ``` occurrences in the kept range.
    head = text[:keep]
    fence_count = head.count("```")
    if fence_count % 2 == 0:
        return head + ELLIPSIS

    # We're inside a code fence at the cut point. The most recent
    # ``` in the kept range is the OPENING of the unclosed fence;
    # cut just before it so the kept portion has an even fence count
    # (only fully-closed pairs survive).
    floor = max(0, keep - lookback)
    chunk = text[floor:keep]
    idx = chunk.rfind("```")
    if idx >= 0:
        cut_at = floor + idx
        return text[:cut_at] + ELLIPSIS

    # No fence boundary found; fall back to naive cut.
    return head + ELLIPSIS


__all__ = ["truncate_for_platform", "truncate_smart", "ELLIPSIS", "DEFAULT_MAX_LEN"]
