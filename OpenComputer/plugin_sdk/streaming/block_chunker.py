"""Block-aware streaming chunker (OpenClaw 1.A port).

Standalone — depends only on stdlib. Mirrors OpenClaw's streaming.md
boundary-priority approach. Never splits inside fenced code blocks.

Design choices vs OpenClaw upstream:
  - ``human_delay()`` returns a float (seconds), not raw ms.
  - ``feed`` and ``flush`` are sync — async pacing is the caller's
    responsibility (so the chunker stays usable from sync test code).
  - ``wrap_stream_callback`` is the recommended integration helper for
    channel adapters: pass it to ``AgentLoop.run_conversation``'s
    ``stream_callback=`` kwarg to opt in.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

BoundaryKind = Literal["paragraph", "newline", "sentence", "whitespace", "max"]
_FENCE = "```"
_PARAGRAPH = "\n\n"
_NEWLINE = "\n"
_SENTENCE_TERMINATORS = (". ", "? ", "! ", ".\n", "?\n", "!\n")


@dataclass(frozen=True, slots=True)
class Block:
    """One ready-to-deliver unit emitted by the chunker."""

    text: str
    boundary: BoundaryKind


class BlockChunker:
    """Buffers stream deltas; emits blocks at natural boundaries.

    Boundary priority: paragraph → newline → sentence → whitespace → max.
    Never splits inside a fenced code block (``\\`\\`\\``).

    Args:
        min_chars: blocks below this are buffered until larger.
        max_chars: blocks above this are split at the highest-priority
            boundary that fits within ``max_chars``.
        human_delay_min_ms / human_delay_max_ms: random pause between blocks.
    """

    def __init__(
        self,
        min_chars: int = 80,
        max_chars: int = 1500,
        human_delay_min_ms: int = 800,
        human_delay_max_ms: int = 2500,
    ) -> None:
        if min_chars < 1 or max_chars < min_chars:
            raise ValueError(f"invalid min/max: {min_chars}/{max_chars}")
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.human_delay_min_ms = human_delay_min_ms
        self.human_delay_max_ms = human_delay_max_ms
        self._buf: str = ""

    def feed(self, delta: str) -> list[Block]:
        """Append delta to buffer; return blocks ready to emit."""
        self._buf += delta
        out: list[Block] = []
        while True:
            block = self._extract_one()
            if block is None:
                break
            out.append(block)
        return out

    def flush(self) -> list[Block]:
        """Emit whatever remains in the buffer, regardless of size."""
        if not self._buf.strip():
            self._buf = ""
            return []
        text = self._buf.rstrip()
        self._buf = ""
        return [Block(text=text, boundary="max")]

    def human_delay(self) -> float:
        """Random pause in seconds between block deliveries."""
        ms = random.uniform(self.human_delay_min_ms, self.human_delay_max_ms)
        return max(0.0, ms / 1000.0)

    # --- internals -------------------------------------------------------

    def _extract_one(self) -> Block | None:
        buf = self._buf
        if len(buf) < self.min_chars:
            return None
        if len(buf) > self.max_chars:
            return self._force_split()

        # paragraph
        idx = self._find_boundary(buf, _PARAGRAPH)
        if idx is not None and idx >= self.min_chars:
            return self._consume(idx, len(_PARAGRAPH), "paragraph")

        # newline
        idx = self._find_boundary(buf, _NEWLINE)
        if idx is not None and idx >= self.min_chars:
            return self._consume(idx, len(_NEWLINE), "newline")

        # sentence terminator
        for term in _SENTENCE_TERMINATORS:
            idx = self._find_boundary(buf, term)
            if idx is not None and idx >= self.min_chars:
                return self._consume(idx + len(term) - 1, 1, "sentence")
        return None

    def _force_split(self) -> Block:
        buf = self._buf
        cap = self.max_chars
        for sep, kind in ((_PARAGRAPH, "paragraph"), (_NEWLINE, "newline")):
            idx = self._find_boundary(buf[:cap], sep)
            if idx is not None and idx >= self.min_chars:
                return self._consume(idx, len(sep), kind)
        for term in _SENTENCE_TERMINATORS:
            idx = self._find_boundary(buf[:cap], term)
            if idx is not None and idx >= self.min_chars:
                return self._consume(idx + len(term) - 1, 1, "sentence")
        idx = buf.rfind(" ", self.min_chars, cap)
        if idx > 0 and not self._inside_fence(buf, idx):
            return self._consume(idx, 1, "whitespace")
        idx = self._latest_safe_cut(buf, cap)
        return self._consume(idx, 0, "max")

    def _find_boundary(self, buf: str, sep: str) -> int | None:
        start = self.min_chars
        while True:
            idx = buf.find(sep, start)
            if idx == -1:
                return None
            if not self._inside_fence(buf, idx):
                return idx
            start = idx + 1

    def _inside_fence(self, buf: str, idx: int) -> bool:
        return buf[:idx].count(_FENCE) % 2 == 1

    def _latest_safe_cut(self, buf: str, cap: int) -> int:
        for i in range(min(cap, len(buf)), self.min_chars, -1):
            if not self._inside_fence(buf, i):
                return i
        return self.min_chars

    def _consume(self, length: int, sep_len: int, kind: BoundaryKind) -> Block:
        text = self._buf[:length].rstrip()
        self._buf = self._buf[length + sep_len :].lstrip()
        return Block(text=text, boundary=kind)


def wrap_stream_callback(
    inner: Callable[[str], None] | None,
    *,
    min_chars: int = 80,
    max_chars: int = 1500,
    human_delay_min_ms: int = 800,
    human_delay_max_ms: int = 2500,
) -> Callable[[str], None]:
    """Wrap a raw delta callback so deltas are emitted as paragraph-bounded blocks.

    Use case: a channel adapter (Telegram/Discord/Slack) wraps its raw
    streaming callback before passing to ``AgentLoop.run_conversation``::

        from plugin_sdk.streaming import wrap_stream_callback
        chunked = wrap_stream_callback(self._on_delta, min_chars=80, max_chars=1500)
        await loop.run_conversation(user_msg, stream_callback=chunked)

    The returned callback buffers raw deltas and only invokes ``inner``
    once per emitted block. ``inner=None`` produces a no-op chunker
    (useful for tests). Sync-only — no humanDelay sleep, since the
    inner callback is sync. Use ``wrap_stream_callback_async`` for async
    pacing if needed.
    """
    chunker = BlockChunker(
        min_chars=min_chars,
        max_chars=max_chars,
        human_delay_min_ms=human_delay_min_ms,
        human_delay_max_ms=human_delay_max_ms,
    )

    def callback(delta: str) -> None:
        if inner is None:
            return
        for block in chunker.feed(delta):
            inner(block.text)

    return callback


async def stream_with_human_delay(
    chunker: BlockChunker,
    deltas: list[str],
    send_block: Callable[[str], asyncio.Future | asyncio.Task | None],
) -> None:
    """Helper for channel adapters that want full async pacing.

    Iterates ``deltas`` and ``send_block(text)`` for each emitted block,
    awaiting ``human_delay`` between deliveries. Drains the chunker at
    end-of-stream.
    """
    for d in deltas:
        for block in chunker.feed(d):
            await send_block(block.text)
            await asyncio.sleep(chunker.human_delay())
    for block in chunker.flush():
        await send_block(block.text)
