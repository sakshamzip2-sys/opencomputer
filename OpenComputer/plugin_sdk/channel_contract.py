"""
Channel contract — what plugin authors implement to add a messaging channel.

A channel adapter translates between a specific messaging platform
(Telegram, Discord, Slack, ...) and OpenComputer's common MessageEvent
format. The gateway is platform-agnostic; adapters absorb all the
platform-specific weirdness.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from plugin_sdk.core import MessageEvent, Platform, SendResult


class BaseChannelAdapter(ABC):
    """Base class for a messaging channel plugin."""

    #: The platform this adapter serves.
    platform: Platform

    #: Max message length this platform accepts (in chars unless noted).
    max_message_length: int = 10_000

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._message_handler: (
            Callable[[MessageEvent], Awaitable[str | None]] | None
        ) = None

    def set_message_handler(
        self, handler: Callable[[MessageEvent], Awaitable[str | None]]
    ) -> None:
        """Called by the gateway to register its inbound handler."""
        self._message_handler = handler

    async def handle_message(self, event: MessageEvent) -> None:
        """Adapters call this when an inbound message arrives. Dispatches to the gateway."""
        if self._message_handler is None:
            return
        response = await self._message_handler(event)
        if response:
            await self.send(event.chat_id, response)

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to the platform and start listening. Return True on success."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop listening and clean up."""
        ...

    @abstractmethod
    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send a text message to a chat."""
        ...

    async def send_typing(self, chat_id: str) -> None:
        """Send a typing indicator. Optional — default is a no-op."""
        return None

    async def send_image(
        self, chat_id: str, image_url: str, caption: str = ""
    ) -> SendResult:
        """Send an image. Optional — default raises NotImplementedError."""
        raise NotImplementedError(f"{self.platform} adapter has no image support")


__all__ = ["BaseChannelAdapter"]
