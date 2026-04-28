"""Markdown -> WhatsApp syntax converter.

WhatsApp accepts: *bold*, _italic_, ~strike~, ```code```. Headers/links
are flattened (no native support). Code-fence + inline-code preserved.

Ported from gateway/platforms/whatsapp.py:format_message in Hermes.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("plugin_sdk.format_converters.whatsapp_format")

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*\n?.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+)__")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)$", re.MULTILINE)


def convert(text: str) -> str:
    try:
        return _convert_unsafe(text)
    except Exception:  # noqa: BLE001
        logger.warning(
            "whatsapp_format conversion failed; returning plain",
            exc_info=True,
        )
        return text


def _convert_unsafe(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    def stash(content: str) -> str:
        placeholders.append(content)
        return f"\x00P{len(placeholders) - 1}\x00"

    text = _FENCE_RE.sub(lambda m: stash(m.group(0)), text)
    text = _INLINE_CODE_RE.sub(lambda m: stash(f"`{m.group(1)}`"), text)

    # **bold** -> *bold*; __bold__ -> *bold*
    text = _BOLD_DOUBLE_RE.sub(r"*\1*", text)
    text = _BOLD_UNDER_RE.sub(r"*\1*", text)
    # ~~strike~~ -> ~strike~
    text = _STRIKE_RE.sub(r"~\1~", text)
    # # Heading -> *Heading*
    text = _HEADING_RE.sub(r"*\2*", text)
    # [label](url) -> label (url)
    text = _LINK_RE.sub(r"\1 (\2)", text)

    def restore(m: re.Match[str]) -> str:
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00P(\d+)\x00", restore, text)
    return text


__all__ = ["convert"]
