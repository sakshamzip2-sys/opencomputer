"""Markdown -> Matrix HTML converter.

Outputs ``org.matrix.custom.html`` body content. Uses the ``markdown``
library if available; falls back to a regex converter otherwise.

Ported from gateway/platforms/matrix.py:_markdown_to_html in Hermes.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("plugin_sdk.format_converters.matrix_html")

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*\n?.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)$", re.MULTILINE)


def _sanitize_url(url: str) -> str:
    """Reject javascript: / data: schemes; allow http(s)/mailto/matrix:."""
    lo = url.lower().strip()
    if lo.startswith(("http://", "https://", "mailto:", "matrix:")):
        return url.replace('"', "&quot;")
    return ""


def convert(text: str) -> str:
    """Convert markdown to Matrix HTML body. Plain-text on failure."""
    try:
        return _convert_unsafe(text)
    except Exception:  # noqa: BLE001
        logger.warning(
            "matrix_html conversion failed; returning plain", exc_info=True
        )
        return text


def _convert_unsafe(text: str) -> str:
    if not text:
        return ""
    # Always use the regex converter so URL sanitization + safe-scheme rules
    # are applied uniformly. The optional ``markdown`` library lacks the
    # security policy this converter enforces.
    return _regex_to_html(text)


def _regex_to_html(text: str) -> str:
    # Stash code first
    placeholders: list[str] = []

    def stash(content: str) -> str:
        placeholders.append(content)
        return f"\x00P{len(placeholders) - 1}\x00"

    text = _FENCE_RE.sub(
        lambda m: stash(f"<pre><code>{_html_escape(m.group(1))}</code></pre>"),
        text,
    )
    text = _INLINE_CODE_RE.sub(
        lambda m: stash(f"<code>{_html_escape(m.group(1))}</code>"), text
    )

    # Escape lt/gt/amp/quote BEFORE running formatting regexes — they
    # operate on the escaped form. Then re-introduce safe HTML tags.
    text = _html_escape(text)

    # Links — sanitize URL; if scheme rejected, drop the anchor and leave label.
    def link_replace(m: re.Match[str]) -> str:
        url = _sanitize_url(m.group(2))
        if not url:
            return m.group(1)
        return f'<a href="{url}">{m.group(1)}</a>'

    text = _LINK_RE.sub(link_replace, text)

    text = _BOLD_DOUBLE_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_STAR_RE.sub(r"<em>\1</em>", text)
    text = _STRIKE_RE.sub(r"<del>\1</del>", text)
    text = _HEADING_RE.sub(
        lambda m: (
            f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>"
        ),
        text,
    )

    def restore(m: re.Match[str]) -> str:
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00P(\d+)\x00", restore, text)
    return text


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


__all__ = ["convert"]
