"""Markdown -> Telegram MarkdownV2 converter.

Telegram's MarkdownV2 grammar requires escaping a wide set of special
characters (`_*[]()~\\`>#+-=|{}.!\\`) wherever they appear OUTSIDE of code
spans. This converter:

1. Protects fenced code blocks and inline code via placeholder substitution
2. Converts markdown formatting (`**bold**` -> `*bold*`, `*ital*` -> `_ital_`)
3. Escapes remaining special chars in the cleaned-of-code text
4. Restores code blocks and inline code unchanged
5. Falls back to escaping plain text if any pattern fails

Ported from Hermes ``telegram.py:_escape_mdv2`` / ``format_message``.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("plugin_sdk.format_converters.markdownv2")

# MarkdownV2 special chars that MUST be escaped outside code spans
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
_MDV2_ESCAPE_RE = re.compile(f"([{re.escape(_MDV2_SPECIAL)}])")

# Placeholder format: \x00P<n>\x00 (NUL won't appear in user-input text)
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*\n?.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+)__")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)$", re.MULTILINE)


def escape_mdv2(text: str) -> str:
    """Backslash-escape every MarkdownV2 special character."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def convert(text: str) -> str:
    """Convert plain markdown to Telegram MarkdownV2.

    Falls back to fully-escaped plain text if anything goes wrong.
    """
    try:
        return _convert_unsafe(text)
    except Exception:  # noqa: BLE001
        logger.warning(
            "MarkdownV2 conversion failed; falling back to plain", exc_info=True
        )
        return escape_mdv2(text)


def _convert_unsafe(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    # 1. Stash fenced code (preserve fully)
    def stash_fence(m: re.Match[str]) -> str:
        idx = len(placeholders)
        placeholders.append(m.group(0))
        return f"\x00P{idx}\x00"

    text = _FENCE_RE.sub(stash_fence, text)

    # 2. Stash inline code
    def stash_inline(m: re.Match[str]) -> str:
        idx = len(placeholders)
        placeholders.append(f"`{m.group(1)}`")
        return f"\x00P{idx}\x00"

    text = _INLINE_CODE_RE.sub(stash_inline, text)

    # 3. Stash links — label gets escaped, URL gets `)` and `\` escaped
    def stash_link(m: re.Match[str]) -> str:
        idx = len(placeholders)
        label = escape_mdv2(m.group(1))
        url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
        placeholders.append(f"[{label}]({url})")
        return f"\x00P{idx}\x00"

    text = _LINK_RE.sub(stash_link, text)

    # 4. Convert formatting BEFORE escaping (so `**` doesn't get escaped first).
    # IMPORTANT: do NOT call escape_mdv2 on the inner content here — step 5
    # below escapes inter-marker text uniformly, so pre-escaping inside the
    # markers would produce double-escaped output (e.g. `**1.5**` -> `*1\\.5*`
    # instead of the correct `*1\.5*`).
    text = _BOLD_DOUBLE_RE.sub(lambda m: f"\x01B{m.group(1)}\x01B", text)
    text = _BOLD_UNDER_RE.sub(lambda m: f"\x01B{m.group(1)}\x01B", text)
    text = _STRIKE_RE.sub(lambda m: f"\x01S{m.group(1)}\x01S", text)
    # Single-asterisk italic: only when surrounded by non-asterisk
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        lambda m: f"\x01I{m.group(1)}\x01I",
        text,
    )
    # Single-underscore italic
    text = re.sub(
        r"(?<!_)_([^_\n]+)_(?!_)",
        lambda m: f"\x01I{m.group(1)}\x01I",
        text,
    )
    # Headings -> bold
    text = _HEADING_RE.sub(lambda m: f"\x01B{m.group(2)}\x01B", text)

    # 5. Escape ALL remaining special chars in non-marker text.
    # Split on placeholders so we don't escape them.
    parts = re.split(r"(\x00P\d+\x00|\x01[BIS])", text)
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        if p.startswith("\x00P") or p.startswith("\x01"):
            out.append(p)
        else:
            out.append(escape_mdv2(p))
    text = "".join(out)

    # 6. Replace formatting markers with MarkdownV2 syntax
    text = text.replace("\x01B", "*").replace("\x01I", "_").replace("\x01S", "~")

    # 7. Restore placeholders
    def restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return placeholders[idx]

    text = re.sub(r"\x00P(\d+)\x00", restore, text)
    return text


__all__ = ["convert", "escape_mdv2"]
