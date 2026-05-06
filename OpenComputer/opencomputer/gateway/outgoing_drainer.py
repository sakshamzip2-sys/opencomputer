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
            except Exception:  # noqa: BLE001 — never break the loop
                logger.exception("outgoing drainer: drain pass raised; continuing")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_interval,
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
            # Wave 6.E.6 — Hermes parity. Long notification bodies
            # (kanban completion summaries, build-log dumps) get
            # platform-truncated so they don't exceed Telegram /
            # Discord / Matrix message-length caps. The truncate_smart
            # helper preserves markdown code-fence boundaries.
            body = msg.body
            cap = getattr(adapter, "max_message_length", 0)
            if cap and len(body) > cap:
                from opencomputer.gateway._truncate import truncate_smart
                body = truncate_smart(body, max_len=cap)
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

            try:
                result = await adapter.send(msg.chat_id, body)
            except Exception as e:  # noqa: BLE001 — capture for the user
                logger.warning(
                    "outgoing drainer: send failed for %s — %s", msg.id, e,
                )
                self.queue.mark_failed(msg.id, f"{type(e).__name__}: {e}")
                continue

            success = getattr(result, "success", True)
            if success:
                self.queue.mark_sent(msg.id)
            else:
                err = getattr(result, "error", None) or "adapter returned success=False"
                self.queue.mark_failed(msg.id, str(err))

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
