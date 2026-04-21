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
        # Adapter reference (set by Gateway) so we can send typing indicators
        self._adapters_by_platform: dict = {}

    def register_adapter(self, platform: str, adapter) -> None:
        self._adapters_by_platform[platform] = adapter

    def _session_id_for(self, event: MessageEvent) -> str:
        """Stable session id: hash(platform + chat_id). Keeps history per chat."""
        h = hashlib.sha256(f"{event.platform.value}:{event.chat_id}".encode())
        return h.hexdigest()[:32]

    async def handle_message(self, event: MessageEvent) -> str | None:
        """
        Handle one inbound message. Runs the agent loop and returns the
        final assistant text for the adapter to send back.

        Also starts a periodic typing-indicator heartbeat on the source
        channel so the user sees "..." while the agent thinks.
        """
        if not event.text.strip():
            return None
        session_id = self._session_id_for(event)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # Start a typing heartbeat (Telegram's typing state expires after
            # ~5s, so we re-send every 4s until the turn completes).
            heartbeat = asyncio.create_task(
                self._typing_heartbeat(event.platform.value, event.chat_id)
            )
            try:
                result = await self.loop.run_conversation(
                    user_message=event.text,
                    session_id=session_id,
                )
                return result.final_message.content or None
            except Exception as e:  # noqa: BLE001
                logger.exception("dispatch error for %s: %s", event.platform, e)
                return f"[error: {type(e).__name__}: {e}]"
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except (asyncio.CancelledError, Exception):
                    pass

    async def _typing_heartbeat(self, platform: str, chat_id: str) -> None:
        """Send typing indicator every 4s until cancelled."""
        adapter = self._adapters_by_platform.get(platform)
        if adapter is None:
            return
        try:
            while True:
                try:
                    await adapter.send_typing(chat_id)
                except Exception:
                    pass  # typing is best-effort
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            return


__all__ = ["Dispatch"]
