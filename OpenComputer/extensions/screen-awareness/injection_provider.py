"""ScreenContextProvider — emits <screen_context> overlay each turn.

Reads the latest capture from the ring buffer; emits it as a system-
prompt overlay if (a) buffer non-empty and (b) the latest capture is
within ``freshness_seconds`` (default 60s). Truncates body text to
``max_chars`` (default 4000 chars ≈ ~1000 tokens).
"""
from __future__ import annotations

import time

from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

from .ring_buffer import ScreenRingBuffer

#: Default freshness window — emit only if latest capture is within 60s.
DEFAULT_FRESHNESS_SECONDS = 60.0
#: Default body cap — ~1000 tokens of OCR text.
DEFAULT_MAX_CHARS = 4_000


class ScreenContextProvider(DynamicInjectionProvider):
    """Inject <screen_context> overlay from latest ring entry."""

    def __init__(
        self,
        *,
        ring_buffer: ScreenRingBuffer,
        freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._ring = ring_buffer
        self._freshness = freshness_seconds
        self._max_chars = max_chars

    @property
    def provider_id(self) -> str:
        return "screen_context"

    async def collect(self, ctx: InjectionContext) -> str | None:
        latest = self._ring.latest()
        if latest is None:
            return ""
        age = time.time() - latest.captured_at
        if age > self._freshness:
            return ""
        body = latest.text
        if len(body) > self._max_chars:
            body = body[: self._max_chars - 1] + "…"
        return (
            "<screen_context>\n"
            f"(captured {age:.1f}s ago, sha={latest.sha256[:8]})\n"
            f"{body}\n"
            "</screen_context>"
        )


__all__ = [
    "DEFAULT_FRESHNESS_SECONDS",
    "DEFAULT_MAX_CHARS",
    "ScreenContextProvider",
]
