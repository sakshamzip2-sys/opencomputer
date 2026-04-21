"""
Gateway dispatch — route inbound MessageEvents to the agent loop.

This is the glue between channel adapters (Telegram, Discord, etc.)
and the AgentLoop. Each adapter calls `Dispatch.handle_message(event)`;
we map chat_id → session_id and invoke the loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from opencomputer.agent.loop import AgentLoop
from plugin_sdk.core import MessageEvent

logger = logging.getLogger("opencomputer.gateway.dispatch")


class Dispatch:
    """Map channel messages to agent-loop runs, keeping per-chat sessions separate."""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop
        # One lock per chat_id — prevents interleaved turns from the same chat
        self._locks: dict[str, asyncio.Lock] = {}

    def _session_id_for(self, event: MessageEvent) -> str:
        """Stable session id: hash(platform + chat_id). Keeps history per chat."""
        h = hashlib.sha256(f"{event.platform.value}:{event.chat_id}".encode())
        return h.hexdigest()[:32]

    async def handle_message(self, event: MessageEvent) -> str | None:
        """
        Handle one inbound message. Runs the agent loop and returns the
        final assistant text for the adapter to send back.

        Returns None if there's nothing to say (empty user message, etc.).
        """
        if not event.text.strip():
            return None
        session_id = self._session_id_for(event)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                result = await self.loop.run_conversation(
                    user_message=event.text,
                    session_id=session_id,
                )
                return result.final_message.content or None
            except Exception as e:  # noqa: BLE001
                logger.exception("dispatch error for %s: %s", event.platform, e)
                return f"[error: {type(e).__name__}: {e}]"


__all__ = ["Dispatch"]
