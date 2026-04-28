"""Markdown -> Slack mrkdwn converter.

Ported from gateway/platforms/slack.py:format_message in Hermes.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("plugin_sdk.format_converters.slack_mrkdwn")

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*\n.*?\n)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)$", re.MULTILINE)


def convert(text: str) -> str:
    """Convert markdown to Slack mrkdwn. Plain-text fallback on error."""
    try:
        return _convert_unsafe(text)
    except Exception:  # noqa: BLE001
        logger.warning(
            "mrkdwn conversion failed; returning plain text", exc_info=True
        )
        return text


def _convert_unsafe(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    def stash(content: str) -> str:
        placeholders.append(content)
        return f"\x00P{len(placeholders) - 1}\x00"

    # Stash fenced code + inline code unchanged
    text = _FENCE_RE.sub(lambda m: stash(m.group(0)), text)
    text = _INLINE_CODE_RE.sub(lambda m: stash(f"`{m.group(1)}`"), text)

    # Escape & < > BEFORE other transforms.
    # Avoid double-escape of named (&amp; &lt; ...) AND numeric (&#39; &#x2F;) entities.
    text = re.sub(r"&(?!(amp|lt|gt|quot|apos);|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")

    # Convert single *italic* -> _italic_ FIRST, before **bold** -> *bold*.
    # If we did the bold pass first the italic regex would then match the
    # newly-emitted single-asterisk pair and turn `*bold*` into `_bold_`.
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"_\1_", text)
    # **bold** -> *bold*
    text = _BOLD_DOUBLE_RE.sub(r"*\1*", text)
    # ~~strike~~ -> ~strike~
    text = _STRIKE_RE.sub(r"~\1~", text)
    # # Heading -> *Heading*
    text = _HEADING_RE.sub(r"*\2*", text)

    # Convert links: [label](url) -> <url|label>
    text = _LINK_RE.sub(
        lambda m: stash(f"<{m.group(2)}|{m.group(1)}>"), text
    )

    # Restore placeholders
    def restore(m: re.Match[str]) -> str:
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00P(\d+)\x00", restore, text)
    return text


__all__ = ["convert"]
