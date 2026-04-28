"""Channel-utility functions: UTF-16 budgeting, smart truncation, type registries.

Ported from gateway/platforms/base.py in Hermes Agent (2026.4.23).
Pure stdlib; respects the plugin_sdk import boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# ---------------------------------------------------------------------------
# UTF-16 length math (Telegram measures 4096 limit in UTF-16 code units, not codepoints)
# ---------------------------------------------------------------------------


def utf16_len(s: str) -> int:
    """Number of UTF-16 code units in s.

    Surrogate pairs (emoji, etc.) count as 2.
    """
    if not s:
        return 0
    return len(s.encode("utf-16-le")) // 2


def _prefix_within_utf16_limit(s: str, budget: int) -> int:
    """Largest codepoint-prefix length whose UTF-16 length is <= budget."""
    if utf16_len(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------------------------------------------------------------------------
# Smart truncation (code-fence-aware, UTF-16-aware via len_fn)
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\s*$", re.MULTILINE)
_FENCE_CLOSE_LITERAL = "```"


def _find_clean_break(text: str, budget: int, len_fn: Callable[[str], int]) -> int:
    """Largest cut index <= budget that prefers a newline boundary."""
    max_idx = (
        min(budget, len(text))
        if len_fn is len
        else _prefix_within_utf16_limit(text, budget)
    )
    if max_idx >= len(text):
        return len(text)
    if max_idx <= 0:
        return 1
    # Prefer last newline before max_idx
    nl = text.rfind("\n", 0, max_idx + 1)
    if nl > max_idx // 2:  # don't break too early
        return nl + 1
    sp = text.rfind(" ", 0, max_idx + 1)
    if sp > max_idx // 2:
        return sp + 1
    return max_idx


def truncate_message_smart(
    content: str,
    max_length: int = 4096,
    len_fn: Callable[[str], int] | None = None,
) -> list[str]:
    """Split content into chunks of <= max_length each, preserving code fences.

    Behaviour:
    - If a chunk boundary falls inside a fenced code block, reopen ```lang
      at the start of the next chunk so syntax highlighting survives.
    - Inline code spans (`...`) are not split mid-span (best-effort via
      newline/space boundary preference).
    - Multi-chunk output gets " (i/N)" indicator appended (chunk index +
      total). For single chunks, no indicator.
    - len_fn lets callers swap codepoint length for UTF-16 length
      (Telegram) or any other custom unit. Default: codepoint count.

    Returns:
        Non-empty list of strings, each within budget.
    """
    if len_fn is None:
        len_fn = len
    if not content:
        return [""]
    if len_fn(content) <= max_length:
        return [content]

    # Reserve indicator overhead. The worst-case " (i/N)" string scales with
    # the chunk count, so estimate an upper bound from total content length
    # rather than hard-coding 10 (which underestimates for n >= 100 chunks).
    estimated_chunks = max(1, (len_fn(content) // max_length) + 1)
    indicator_overhead = len(f" ({estimated_chunks}/{estimated_chunks})") + 1

    chunks: list[str] = []
    remaining = content
    open_fence_lang: str | None = None

    while remaining:
        # When inside a fenced code block, each emitted chunk gets a
        # ```lang\n prefix and a \n``` suffix wrapped around its slice. The
        # cut budget must account for both so the wrapped chunk stays within
        # max_length. Recompute on every iteration: the second-and-later
        # chunks pay this overhead, the first one doesn't.
        fence_overhead = 0
        if open_fence_lang is not None:
            fence_overhead = len(f"```{open_fence_lang}\n") + len("\n```")
        budget = max(1, max_length - indicator_overhead - fence_overhead)

        if len_fn(remaining) <= budget:
            chunk = remaining
            remaining = ""
        else:
            cut = _find_clean_break(remaining, budget, len_fn)
            chunk = remaining[:cut]
            remaining = remaining[cut:]

        # Reopen lang if previous chunk had unclosed fence
        if open_fence_lang is not None:
            lang_prefix = open_fence_lang
            chunk = f"```{lang_prefix}\n" + chunk

        # Detect open fence at end of THIS chunk: count ``` markers
        fence_count = chunk.count(_FENCE_CLOSE_LITERAL)
        if fence_count % 2 == 1:
            # We have an unclosed fence — close it and remember lang
            m = _FENCE_OPEN_RE.search(chunk)
            open_fence_lang = m.group(1) if m else ""
            chunk = chunk + "\n" + _FENCE_CLOSE_LITERAL
        else:
            open_fence_lang = None

        chunks.append(chunk)

    if len(chunks) == 1:
        return chunks
    n = len(chunks)
    return [f"{c} ({i + 1}/{n})" for i, c in enumerate(chunks)]


# ---------------------------------------------------------------------------
# Document / video type registries
# ---------------------------------------------------------------------------

SUPPORTED_DOCUMENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

SUPPORTED_VIDEO_TYPES: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mov",
        ".webm",
        ".mkv",
        ".avi",
    }
)


__all__ = [
    "SUPPORTED_DOCUMENT_TYPES",
    "SUPPORTED_VIDEO_TYPES",
    "truncate_message_smart",
    "utf16_len",
]
