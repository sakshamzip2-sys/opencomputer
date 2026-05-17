"""Background coroutine that drains :class:`OutgoingQueue` through live adapters.

Lives inside the gateway daemon. Polls the queue every 1s, dispatches
each ``queued`` row to the matching channel adapter's ``send()``,
marks ``sent`` or ``failed`` based on the result.

Why poll instead of using a notify-on-insert mechanism (e.g.
``sqlite3`` pragma, sockets)?

- Insert source is a separate process (``opencomputer mcp serve``).
  Cross-process signalling adds complexity (pipes, sockets, sigint
  handling) for what amounts to a 1s lag.
- 1s polling is cheap — one indexed SELECT per second against a
  table that should rarely have more than a few rows.
- Robust to crashes: if the gateway dies mid-send, the row stays
  ``queued`` (no transactional state to recover) and the next gateway
  boot picks it up.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from .outgoing_queue import OutgoingQueue

logger = logging.getLogger("opencomputer.gateway.outgoing_drainer")


class OutgoingDrainer:
    """Long-lived coroutine that drains the outgoing-message queue.

    Wire from :class:`Gateway`::

        drainer = OutgoingDrainer(queue, adapters_by_platform)
        await drainer.expire_stale_on_boot()
        drainer_task = asyncio.create_task(drainer.run_forever())
        ...
        drainer.stop()
        await drainer_task

    ``adapters_by_platform`` maps a platform string (e.g. ``"telegram"``)
    to a live channel adapter exposing ``async send(chat_id, text) ->
    SendResult``. The drainer doesn't own the adapters' lifecycle — the
    gateway does.
    """

    def __init__(
        self,
        queue: OutgoingQueue,
        adapters_by_platform: Mapping[str, Any],
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.queue = queue
        self.adapters = adapters_by_platform
        self.poll_interval = poll_interval_seconds
        self._stop = asyncio.Event()
        # Exponential backoff state — protects errors.log from being filled
        # with 100k+ identical tracebacks when the drain pass fails on every
        # tick (observed: 33h-stuck daemon, 2026-05-07). Cap at ~5 min.
        self._consecutive_errors = 0
        self._max_backoff_seconds = 300.0

    def stop(self) -> None:
        self._stop.set()

    async def expire_stale_on_boot(self) -> int:
        """Mark queued rows older than the TTL as expired.

        Idempotent. Call once after gateway start, before
        :meth:`run_forever`.
        """
        n = self.queue.expire_stale()
        if n:
            logger.warning(
                "outgoing drainer: marked %d stale message(s) as expired", n,
            )
        return n

    async def run_forever(self) -> None:
        logger.info(
            "outgoing drainer: starting (poll=%.1fs, %d adapter(s))",
            self.poll_interval, len(self.adapters),
        )
        while not self._stop.is_set():
            try:
                await self._drain_once()
                if self._consecutive_errors:
                    logger.info(
                        "outgoing drainer: recovered after %d failed pass(es)",
                        self._consecutive_errors,
                    )
                self._consecutive_errors = 0
                wait_for = self.poll_interval
            except Exception:  # noqa: BLE001 — never break the loop
                self._consecutive_errors += 1
                # Log full traceback only on first, 10th, 100th, 1000th... so
                # errors.log doesn't fill up at 1Hz on a wedged DB path.
                n = self._consecutive_errors
                if n == 1 or (n & (n - 1)) == 0:  # powers of 2 only
                    logger.exception(
                        "outgoing drainer: drain pass raised "
                        "(consecutive_errors=%d); continuing", n,
                    )
                # Exponential backoff: 1s, 2s, 4s, ... up to 5min cap.
                wait_for = min(
                    self.poll_interval * (2 ** min(n - 1, 9)),
                    self._max_backoff_seconds,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=wait_for,
                )
            except TimeoutError:
                continue

    async def _drain_once(self) -> None:
        rows = self.queue.list_queued(limit=16)
        if not rows:
            return
        for msg in rows:
            adapter = self.adapters.get(msg.platform)
            if adapter is None:
                # No adapter for this platform — mark failed so the user
                # sees it rather than letting the row sit indefinitely.
                # If the adapter shows up later (plugin enabled mid-run),
                # the user can re-enqueue.
                self.queue.mark_failed(
                    msg.id, f"no live adapter for platform {msg.platform!r}",
                )
                continue
            body = msg.body
            cap = getattr(adapter, "max_message_length", 0)
            # Phase 3 (2026-05-06) — MESSAGE_SENDING fire-and-forget hook.
            # Plugins observe outgoing traffic; "skip" decision drops without
            # sending; "rewrite" replaces the body via modified_message.
            try:
                from opencomputer.hooks.engine import engine as _hook_engine_ms
                from plugin_sdk.hooks import HookContext as _MsCtx
                from plugin_sdk.hooks import HookEvent as _MsEvent

                d = await _hook_engine_ms.fire_blocking(
                    _MsCtx(
                        event=_MsEvent.MESSAGE_SENDING,
                        session_id=getattr(msg, "session_id", "") or "",
                        outgoing_text=body,
                        channel=msg.platform,
                        outgoing_chat_id=msg.chat_id,
                    )
                )
                if d is not None:
                    if getattr(d, "decision", "pass") == "skip":
                        logger.info(
                            "outgoing drainer: skipped %s by hook (%s)",
                            msg.id, getattr(d, "reason", ""),
                        )
                        self.queue.mark_sent(msg.id)
                        continue
                    if (
                        getattr(d, "decision", "pass") == "rewrite"
                        and getattr(d, "modified_message", "")
                    ):
                        body = d.modified_message
            except Exception as _e:  # noqa: BLE001 — hook failure must not wedge send
                logger.debug(
                    "MESSAGE_SENDING hook raised, ignoring: %r", _e
                )

            # M3 #3 fix — chunk-and-send instead of truncate. A body
            # over the adapter's cap (kanban summaries, build-log dumps)
            # used to be cut by ``truncate_smart`` + ``…[truncated]``,
            # silently dropping content. Now it is split into ordered
            # ``(i/N)``-marked messages so nothing is lost. Chunks for
            # one queue row send sequentially → per-chat order preserved.
            if cap and len(body) > cap:
                from opencomputer.gateway.reply_chunker import chunk_text

                parts = chunk_text(body, cap=cap)
            else:
                parts = [body]

            send_failed: str | None = None
            for part in parts:
                try:
                    result = await adapter.send(msg.chat_id, part)
                except Exception as e:  # noqa: BLE001 — capture for the user
                    logger.warning(
                        "outgoing drainer: send failed for %s — %s", msg.id, e,
                    )
                    send_failed = f"{type(e).__name__}: {e}"
                    break
                if not getattr(result, "success", True):
                    send_failed = (
                        getattr(result, "error", None)
                        or "adapter returned success=False"
                    )
                    break

            if send_failed is None:
                self.queue.mark_sent(msg.id)
            else:
                self.queue.mark_failed(msg.id, str(send_failed))
                continue

            # Phase 3 — MESSAGE_SENT fire-and-forget hook (post-send observability).
            try:
                from opencomputer.hooks.engine import engine as _hook_engine_ms2
                from plugin_sdk.hooks import HookContext as _MsCtx2
                from plugin_sdk.hooks import HookEvent as _MsEvent2

                _hook_engine_ms2.fire_and_forget(
                    _MsCtx2(
                        event=_MsEvent2.MESSAGE_SENT,
                        session_id=getattr(msg, "session_id", "") or "",
                        outgoing_text=body,
                        channel=msg.platform,
                        outgoing_chat_id=msg.chat_id,
                    )
                )
            except Exception as _e:  # noqa: BLE001 — observability must not wedge
                logger.debug(
                    "MESSAGE_SENT hook raised, ignoring: %r", _e
                )


__all__ = ["OutgoingDrainer"]
