"""Block streaming chunker — humanlike pacing for chat-channel adapters.

Async-only API: ``feed`` and ``close`` MUST be called from coroutine
context. The chunker is designed for channel adapters (Telegram, Discord,
Slack) that want to deliver streaming agent responses in human-paced
"blocks" rather than raw token deltas — ie one paragraph or sentence at
a time, with a small natural delay between deliveries.

Boundary preference (highest first):

    paragraph (``\\n\\n``) > newline (``\\n``) > sentence (``. ``) > whitespace

Inside a fenced code block (``\\`\\`\\``) the chunker NEVER splits — fenced
content is held until the closing fence arrives so the adapter never
emits a half-fence. ``close`` flushes whatever is left, fence-open or
not.

Idle coalesce: when ``feed`` is called and then no further input arrives
for ``idle_ms``, the buffered text is flushed via the emit callback so
slow streams don't stall behind a missing boundary.

If ``emit`` raises, the chunk is logged and reinserted at the head of
the buffer so the next emit attempt picks it up — channel send failures
should NOT silently drop response text.

Reference: ``openclaw-2026.4.23/extensions/discord/src/chunk.ts``.

Differences vs the existing sync ``plugin_sdk.streaming.BlockChunker``:

* This module is async-only with humanlike pacing built in. The
  sync chunker is intended for callers that already do their own
  pacing (eg the CLI's incremental renderer).
* This module ships an idle-coalesce timer; the sync one does not.
* Emit is an async callback (``Awaitable[None]``); the sync chunker
  returns a list of ``Block`` objects for the caller to handle.

Channel adapters wire it like::

    from opencomputer.gateway.streaming_chunker import (
        BlockStreamingChunker,
        ChunkerConfig,
    )

    async def emit(chunk: str) -> None:
        await self.send(chat_id, chunk)

    chunker = BlockStreamingChunker(emit=emit)
    async for delta in stream:
        await chunker.feed(delta)
    await chunker.close()
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Async emit callback signature. Channels implement this to forward a
#: ready-to-send block to their outbound queue.
EmitFn = Callable[[str], Awaitable[None]]

_FENCE = "```"


@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    """Tunables for ``BlockStreamingChunker``.

    Attributes:
        idle_ms: Milliseconds without new ``feed`` input before the
            buffer is auto-flushed. Default 250ms.
        human_delay_min_ms / human_delay_max_ms: Random pause range
            inserted before each emit, mimicking human typing cadence.
            Defaults: 800-2500ms (matches OpenClaw's discord chunker).
        human_delay_ms: When set, overrides the random range with a
            fixed delay (test injection — pass 0 to disable pacing).
        min_emit_chars: Blocks whose ``.strip()`` is shorter than this
            are held back. Default 1 (any non-whitespace emits).
    """

    idle_ms: int = 250
    human_delay_min_ms: int = 800
    human_delay_max_ms: int = 2500
    human_delay_ms: int | None = None
    min_emit_chars: int = 1


class BlockStreamingChunker:
    """Async block-streaming chunker with idle-coalesce + humanlike pacing.

    Args:
        emit: Async callback invoked with each emitted block.
        config: Optional ``ChunkerConfig`` (defaults are used otherwise).

    Usage::

        chunker = BlockStreamingChunker(emit=my_send, config=ChunkerConfig())
        await chunker.feed(delta)
        ...
        await chunker.close()
    """

    def __init__(
        self,
        emit: EmitFn,
        *,
        config: ChunkerConfig | None = None,
    ) -> None:
        self._emit = emit
        self._cfg = config or ChunkerConfig()
        self._buf: list[str] = []
        self._fence_open = False
        self._idle_task: asyncio.Task | None = None
        self._closed = False

    async def feed(self, text: str) -> None:
        """Append text and emit any complete blocks.

        Closed chunkers are inert — calls return without buffering. The
        idle timer is reset on every ``feed`` so a steady-stream never
        triggers idle-flush.
        """
        if self._closed or not text:
            return
        self._buf.append(text)
        # Toggle fence-open for every triple-backtick observed in the
        # appended text. We only count fences in the new text — the
        # cumulative state is held in ``self._fence_open``.
        for _ in self._scan_fence(text):
            self._fence_open = not self._fence_open
        await self._maybe_emit()
        self._reset_idle()

    async def close(self) -> None:
        """Cancel the idle timer and flush any buffered text.

        Called once at end-of-stream. Even if a fence is still open
        (truncated stream), close MUST flush so partial responses are
        delivered rather than silently swallowed.
        """
        if self._closed:
            return
        self._closed = True
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            try:
                await self._idle_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._buf:
            remaining = "".join(self._buf)
            self._buf.clear()
            await self._do_emit(remaining)

    # --- internals -------------------------------------------------------

    def _scan_fence(self, text: str) -> list[int]:
        """Return positions of every ``\\`\\`\\``` in *text*."""
        out: list[int] = []
        i = 0
        while True:
            j = text.find(_FENCE, i)
            if j < 0:
                return out
            out.append(j)
            i = j + 3

    async def _maybe_emit(self) -> None:
        """Try to emit one block at the highest-priority boundary."""
        if self._fence_open:
            return
        s = "".join(self._buf)
        b = self._find_boundary(s)
        if b < 0:
            return
        chunk = s[:b]
        rest = s[b:]
        if len(chunk.strip()) < self._cfg.min_emit_chars:
            return
        # Reset buffer to remaining text (may be empty).
        self._buf = [rest] if rest else []
        await self._do_emit(chunk)

    def _find_boundary(self, s: str) -> int:
        """Locate the highest-priority boundary in *s*.

        Returns the END index (exclusive — so ``s[:idx]`` is the chunk
        and ``s[idx:]`` is the remainder). Boundary preference: paragraph
        > newline > sentence (``. `` / ``? `` / ``! ``) > -1 (none).
        Whitespace is intentionally NOT a fallback here — short streams
        flush via the idle timer + ``close`` instead, which keeps emits
        coarse. ``rfind`` is used so the LAST boundary in the buffer is
        chosen — this minimises the rest-buffer carried into the next
        emit attempt and keeps blocks roughly paragraph-shaped.
        """
        idx = s.rfind("\n\n")
        if idx >= 0:
            return idx + 2
        idx = s.rfind("\n")
        if idx >= 0:
            return idx + 1
        for end in (". ", "? ", "! "):
            idx = s.rfind(end)
            if idx >= 0:
                return idx + len(end)
        return -1

    async def _do_emit(self, chunk: str) -> None:
        """Pace + dispatch a single chunk to ``self._emit``.

        Safe-mode: if the emit callback raises, log + reinsert the chunk
        at the head of the buffer so the next attempt picks it up rather
        than dropping the text.
        """
        delay = self._cfg.human_delay_ms
        if delay is None:
            delay = random.randint(
                self._cfg.human_delay_min_ms, self._cfg.human_delay_max_ms
            )
        if delay > 0:
            await asyncio.sleep(delay / 1000.0)
        try:
            await self._emit(chunk)
        except Exception:  # noqa: BLE001 — defensive, never lose data
            logger.exception("chunker emit failed; reinserting buffer")
            self._buf.insert(0, chunk)

    def _reset_idle(self) -> None:
        """(Re)start the idle-flush timer.

        ``asyncio.get_running_loop`` is used (not the deprecated
        ``get_event_loop``). When called outside an event loop the timer
        is silently skipped — caller is responsible for using ``feed``
        from coroutine context.
        """
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_task = loop.create_task(self._idle_flush())

    async def _idle_flush(self) -> None:
        """Wait ``idle_ms`` then flush — unless a fence is still open."""
        try:
            await asyncio.sleep(self._cfg.idle_ms / 1000.0)
        except asyncio.CancelledError:
            return
        if self._buf and not self._fence_open and not self._closed:
            remaining = "".join(self._buf)
            self._buf.clear()
            await self._do_emit(remaining)


__all__ = ["BlockStreamingChunker", "ChunkerConfig", "EmitFn"]
