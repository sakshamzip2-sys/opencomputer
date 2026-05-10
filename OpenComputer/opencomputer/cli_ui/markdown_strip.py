"""Strip rendered markdown markup from CLI final assistant prose.

Hermes-CLI parity (doc line 77). Rich already styles ``**bold**`` and
``*italic*`` when fed through ``Markdown(text)``, but on terminals
without bold/italic the literal asterisks print as ugly noise. This
module gives the streaming renderer one place to deboldify final
text — with a careful exemption for any region inside fenced code
blocks, inline code, or tables (where the ``*`` glyphs are part of
the user-visible payload).

The function is pure; tests are golden-fixture-driven.
"""

from __future__ import annotations

import re

# Match a fenced code block — opening fence, body, closing fence on own line.
# Greedy across newlines via DOTALL, lazy body via ``*?``.
_FENCE_RE = re.compile(r"```[^\n]*\n.*?\n```", re.DOTALL)
# Match inline code: backticks containing no newline.
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# Match table rows — line starting with ``|`` and containing another ``|``.
_TABLE_RE = re.compile(r"(?m)^\|.*\|$")

# Markup patterns to strip in non-code regions.
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDER_RE = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])")
_ATX_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")


def strip_for_terminal(md: str) -> str:
    """Return *md* with rendered markup stripped, preserving code/tables.

    Algorithm:

    1. Mask off code fences, inline code, and table rows by splitting *md*
       into alternating segments of "preserve" (verbatim) and "strip"
       (apply markup-strip rules).
    2. Apply strip rules only to "strip" segments.
    3. Re-join.
    """
    parts = _split_preserve_strip(md)
    out: list[str] = []
    for kind, chunk in parts:
        if kind == "preserve":
            out.append(chunk)
        else:
            chunk = _BOLD_RE.sub(r"\1", chunk)
            chunk = _ITALIC_STAR_RE.sub(r"\1", chunk)
            chunk = _ITALIC_UNDER_RE.sub(r"\1", chunk)
            chunk = _ATX_HEADING_RE.sub("", chunk)
            out.append(chunk)
    return "".join(out)


def _split_preserve_strip(md: str) -> list[tuple[str, str]]:
    """Split *md* into ``(kind, text)`` segments where kind is
    ``"preserve"`` (verbatim — code/tables) or ``"strip"`` (apply rules).
    """
    spans: list[tuple[int, int]] = []
    for rx in (_FENCE_RE, _INLINE_CODE_RE, _TABLE_RE):
        for m in rx.finditer(md):
            spans.append(m.span())
    spans.sort()

    # Merge overlapping spans (rare but possible: inline code in a table row).
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    out: list[tuple[str, str]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            out.append(("strip", md[cursor:start]))
        out.append(("preserve", md[start:end]))
        cursor = end
    if cursor < len(md):
        out.append(("strip", md[cursor:]))
    return out
