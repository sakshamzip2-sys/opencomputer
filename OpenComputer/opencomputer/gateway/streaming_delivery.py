"""A1 — live-streaming agent replies into an editable chat message.

Gateway-vs-CLI parity Wave 1. On the CLI you watch tokens arrive; on a
channel like Telegram the gateway used to wait for the whole turn and
then send a wall of text. The streaming infrastructure
(``BlockStreamingChunker``) existed but the gateway dispatcher never
wired it.

:class:`StreamingDelivery` owns the placeholder → incremental-edit →
finalize state machine for an adapter that advertises
``ChannelCapabilities.EDIT_MESSAGE``:

1. ``start()`` sends a thin placeholder message and captures its id.
2. ``feed(delta)`` — a *synchronous* sink for ``run_conversation``'s
   ``stream_callback``; deltas are queued and drained in order by a
   single consumer task (no interleave races), fed to a
   ``BlockStreamingChunker`` that emits paragraph/sentence blocks.
3. Each emitted block is appended and the live message is edited.
4. ``finalize(text)`` flushes the chunker and edits the message to the
   fully-formatted final reply (chunking into follow-up messages when
   it exceeds the platform cap).

Every failure path degrades to the non-streaming behaviour: a mid-stream
edit error stops further edits (``finalize`` still delivers the whole
text); a ``finalize`` failure returns ``False`` so the dispatcher falls
back to a normal one-shot send.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from opencomputer.gateway.reply_chunker import chunk_text
from opencomputer.gateway.streaming_chunker import (
    BlockStreamingChunker,
    ChunkerConfig,
)

logger = logging.getLogger("opencomputer.gateway.streaming_delivery")

#: Thin placeholder shown until the first streamed block lands.
_PLACEHOLDER = "…"

#: Fallback message-length cap when an adapter does not declare one.
_DEFAULT_CAP = 10_000

#: How long finalize() waits for the delta consumer to drain.
_DRAIN_TIMEOUT_S = 30.0


class StreamingDelivery:
    """Placeholder → incremental-edit → finalize streaming for one turn."""

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        *,
        chunker_config: ChunkerConfig | None = None,
    ) -> None:
        self._adapter = adapter
        self._chat_id = chat_id
        self._message_id: str | None = None
        self._accumulated = ""
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._consumer: asyncio.Task[None] | None = None
        # No artificial human-pacing delay: token arrival is already
        # paced by the model, and the chunker emits per block (not per
        # token) so edit frequency stays low. A rate-limit rejection is
        # caught in _emit and degrades the stream.
        self._chunker = BlockStreamingChunker(
            emit=self._emit,
            config=chunker_config or ChunkerConfig(human_delay_ms=0),
        )
        #: Set when a mid-stream edit fails — stop editing; finalize()
        #: still delivers the complete text in one go.
        self._degraded = False
        self._active = False

    @property
    def active(self) -> bool:
        """True once the placeholder is live and streaming is wired."""
        return self._active

    async def start(self) -> bool:
        """Send the placeholder message. Returns True if streaming is live.

        A False return (send failed, or the adapter gave no message id to
        edit) means the caller should run the turn without streaming.
        """
        try:
            result = await self._adapter.send(self._chat_id, _PLACEHOLDER)
        except Exception:  # noqa: BLE001 — fall back to one-shot delivery
            logger.warning(
                "streaming: placeholder send failed; one-shot fallback",
                exc_info=True,
            )
            return False
        message_id = getattr(result, "message_id", None)
        if not message_id:
            logger.debug("streaming: adapter returned no message_id; no stream")
            return False
        self._message_id = str(message_id)
        self._active = True
        self._consumer = asyncio.create_task(
            self._drain(), name="oc-stream-drain",
        )
        return True

    def feed(self, delta: str) -> None:
        """Synchronous delta sink for ``run_conversation``'s stream_callback.

        Deltas are queued; a single consumer task drains them in arrival
        order, so concurrent callback invocations never scramble text.
        """
        if self._active and delta:
            self._queue.put_nowait(delta)

    async def _drain(self) -> None:
        """Single ordered consumer: queue → chunker.feed."""
        while True:
            delta = await self._queue.get()
            if delta is None:  # finalize() sentinel
                return
            try:
                await self._chunker.feed(delta)
            except Exception:  # noqa: BLE001 — a feed glitch must not wedge
                logger.debug("streaming: chunker feed failed", exc_info=True)

    async def _emit(self, block: str) -> None:
        """Chunker emit callback — accumulate + edit the live message."""
        self._accumulated += block
        if self._degraded or not self._message_id:
            return
        try:
            await self._adapter.edit_message(
                self._chat_id, self._message_id, self._accumulated,
            )
        except Exception:  # noqa: BLE001 — rate-limited / too-fast / over-cap
            # Stop mid-stream edits; finalize() still delivers everything.
            self._degraded = True
            logger.debug(
                "streaming: mid-stream edit failed; finalize will deliver",
                exc_info=True,
            )

    async def finalize(self, final_text: str) -> bool:
        """Flush the stream and deliver ``final_text`` into the message.

        Returns True when delivery is fully owned here (the dispatcher
        must NOT re-send). Returns False on failure so the caller can
        fall back to a normal send.
        """
        if not self._active or not self._message_id:
            return False

        # Stop the consumer once it has drained every queued delta, then
        # flush whatever the chunker still holds.
        self._queue.put_nowait(None)
        if self._consumer is not None:
            try:
                await asyncio.wait_for(self._consumer, timeout=_DRAIN_TIMEOUT_S)
            except (TimeoutError, Exception):  # noqa: BLE001
                logger.debug("streaming: consumer drain slow/failed", exc_info=True)
        try:
            await self._chunker.close()
        except Exception:  # noqa: BLE001
            logger.debug("streaming: chunker close failed", exc_info=True)

        text = final_text or self._accumulated or "(no reply)"
        cap = getattr(self._adapter, "max_message_length", 0) or _DEFAULT_CAP
        try:
            parts = chunk_text(text, cap=cap) or [text]
            await self._adapter.edit_message(
                self._chat_id, self._message_id, parts[0],
            )
            for extra in parts[1:]:
                await self._adapter.send(self._chat_id, extra)
            return True
        except Exception:  # noqa: BLE001 — caller falls back to one-shot
            logger.warning(
                "streaming: finalize failed; falling back to one-shot send",
                exc_info=True,
            )
            return False


__all__ = ["StreamingDelivery"]
