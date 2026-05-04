"""Reaction-based approval primitive for the Matrix channel (Wave 6.E.3).

Combines two halves:

1. **ApprovalQueue** — stores ``asyncio.Future[bool]`` keyed by Matrix
   event id. The matrix adapter's inbound ``/sync`` loop calls
   :meth:`on_reaction` when an ``m.reaction`` event arrives; if the
   reacted-to event id is in the queue the future resolves with
   ``True`` for ✅, ``False`` for ❌, and is otherwise ignored.

2. **request_approval()** — convenience: post a "want to run X?"
   message via the adapter, register the resulting event id, and
   return a Future the caller can ``await`` on. A timeout resolves
   the future to ``False`` so callers never block forever.

Why a Future-based primitive: this matches OC's existing consent-gate
shape (``set_prompt_handler``) so callers can later wire the matrix
queue in as an alternative prompt provider without rewriting the
caller side.

Security
========

The /sync loop runs with the bot's access token and receives reactions
on every message in every room the bot is in. We only resolve futures
for event ids WE registered — reactions on anything else are ignored
silently. This matches the design audit lens A10.

Open follow-ups (intentionally not in this PR)
==============================================

The primitive is exposed via :class:`ApprovalQueue`; integrating
callers (BashTool pre-flight, ConsentGate prompt handler) is a
separate change. This PR ships the queue + matrix /sync wiring; the
ergonomics for "BashTool now defaults to matrix approval if matrix
is the active channel" land in a follow-up.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger("opencomputer.matrix.approval")

# Default emoji conventions. Callers can override per-request.
DEFAULT_ALLOW_EMOJI = "✅"
DEFAULT_DENY_EMOJI = "❌"


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    future: asyncio.Future[bool]
    allow_emoji: str
    deny_emoji: str
    expires_at: float


class ApprovalQueue:
    """Maps Matrix event ids → pending approval futures.

    Thread-safety: this is async-only — all interaction is from the
    same event loop the matrix adapter runs in. No locks needed.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingApproval] = {}

    def register(
        self,
        event_id: str,
        *,
        future: asyncio.Future[bool],
        allow_emoji: str = DEFAULT_ALLOW_EMOJI,
        deny_emoji: str = DEFAULT_DENY_EMOJI,
        timeout: float = 300.0,
    ) -> None:
        """Track ``event_id`` → ``future`` until a reaction or timeout."""
        self._pending[event_id] = _PendingApproval(
            future=future,
            allow_emoji=allow_emoji,
            deny_emoji=deny_emoji,
            expires_at=time.monotonic() + timeout,
        )

    def on_reaction(self, target_event_id: str, emoji: str) -> bool:
        """Called by the /sync loop when an ``m.reaction`` arrives.

        Returns True if we resolved a registered future, False if the
        reaction was for an event we don't track (most of the time).
        """
        pending = self._pending.pop(target_event_id, None)
        if pending is None:
            return False
        if pending.future.done():
            return False
        if emoji == pending.allow_emoji:
            pending.future.set_result(True)
        elif emoji == pending.deny_emoji:
            pending.future.set_result(False)
        else:
            # Random emoji on our message — re-register so a real
            # ✅/❌ reaction still resolves the future. Don't leak
            # forever; respect the original timeout.
            self._pending[target_event_id] = pending
            return False
        return True

    def reap_expired(self, *, now: float | None = None) -> int:
        """Resolve any expired futures with False; return how many."""
        ts = now if now is not None else time.monotonic()
        expired_ids = [
            eid for eid, p in self._pending.items() if p.expires_at <= ts
        ]
        for eid in expired_ids:
            p = self._pending.pop(eid)
            if not p.future.done():
                p.future.set_result(False)
        return len(expired_ids)

    def cancel_all(self) -> None:
        """On adapter shutdown — cancel any still-pending futures."""
        for p in self._pending.values():
            if not p.future.done():
                p.future.cancel()
        self._pending.clear()

    def __len__(self) -> int:
        return len(self._pending)


async def request_approval(
    adapter,
    chat_id: str,
    prompt: str,
    *,
    allow_emoji: str = DEFAULT_ALLOW_EMOJI,
    deny_emoji: str = DEFAULT_DENY_EMOJI,
    timeout: float = 300.0,
) -> bool:
    """Post ``prompt`` to ``chat_id`` and await an emoji reaction.

    Resolves to True on ``allow_emoji``, False on ``deny_emoji`` or
    timeout. Adapter MUST have an ``approval_queue`` attribute (set in
    ``MatrixAdapter.__init__`` as part of the Wave 6.E.3 wiring).

    Returns False (not raises) on send failure — callers should treat
    it as "user said no" rather than crashing.
    """
    if getattr(adapter, "approval_queue", None) is None:
        logger.warning(
            "request_approval called on adapter %r with no approval_queue; "
            "returning False (deny)",
            adapter,
        )
        return False

    body = (
        f"{prompt}\n\n"
        f"React with {allow_emoji} to approve, {deny_emoji} to deny "
        f"(timeout {int(timeout)}s)."
    )
    try:
        result = await adapter.send(chat_id, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("matrix request_approval send failed: %s", exc)
        return False

    event_id = getattr(result, "platform_message_id", None) or getattr(
        result, "message_id", None,
    )
    if not event_id:
        # Either the send returned no id (older adapter) or the platform
        # didn't echo one. Generate a placeholder so the future has a
        # key, but it'll never resolve via reaction — only via timeout.
        event_id = f"local-{uuid.uuid4().hex}"
        logger.warning(
            "matrix request_approval: no event_id from send; "
            "approval will only resolve via timeout"
        )

    loop = asyncio.get_event_loop()
    fut: asyncio.Future[bool] = loop.create_future()
    adapter.approval_queue.register(
        event_id,
        future=fut,
        allow_emoji=allow_emoji,
        deny_emoji=deny_emoji,
        timeout=timeout,
    )

    try:
        return await asyncio.wait_for(fut, timeout=timeout + 1.0)
    except TimeoutError:
        return False


__all__ = [
    "ApprovalQueue",
    "request_approval",
    "DEFAULT_ALLOW_EMOJI",
    "DEFAULT_DENY_EMOJI",
]
