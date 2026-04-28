"""
Channel contract — what plugin authors implement to add a messaging channel.

A channel adapter translates between a specific messaging platform
(Telegram, Discord, Slack, ...) and OpenComputer's common MessageEvent
format. The gateway is platform-agnostic; adapters absorb all the
platform-specific weirdness.

## Capabilities (Sub-project G — refactor R1)

Adapters declare which optional features they support via the
``capabilities`` class attribute (a :class:`ChannelCapabilities` flag).
The base class provides default no-op or NotImplementedError stubs for
every optional method; adapters override only the methods their
``capabilities`` flag advertises.

Callers that need to know whether a feature is supported should check
``adapter.capabilities & ChannelCapabilities.X`` before calling. The
gateway uses this to gracefully degrade (e.g., emoji ack → text "✓"
fallback when the channel doesn't support reactions).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from plugin_sdk.core import MessageEvent, Platform, ProcessingOutcome, SendResult

logger = logging.getLogger("plugin_sdk.channel_contract")


class ChannelCapabilities(enum.Flag):
    """Bitfield of optional features a :class:`BaseChannelAdapter` supports.

    Combine via ``|``: ``ChannelCapabilities.TYPING | ChannelCapabilities.REACTIONS``.

    The base class declares ``NONE`` so adapters that don't override get a
    safe default. Each adapter sets ``capabilities = ChannelCapabilities.X | ...``
    matching the optional methods it implements.
    """

    NONE = 0
    TYPING = enum.auto()
    REACTIONS = enum.auto()
    """Send emoji reactions on messages (e.g., 👀 / ✅ / ⚠️)."""

    VOICE_OUT = enum.auto()
    """Send voice / audio messages outbound."""

    VOICE_IN = enum.auto()
    """Receive voice messages inbound (delivered via MessageEvent.attachments)."""

    PHOTO_OUT = enum.auto()
    """Send images / photos outbound."""

    PHOTO_IN = enum.auto()
    """Receive photos inbound."""

    DOCUMENT_OUT = enum.auto()
    """Send arbitrary file documents outbound."""

    DOCUMENT_IN = enum.auto()
    """Receive arbitrary file documents inbound."""

    EDIT_MESSAGE = enum.auto()
    """Edit a previously-sent message in place (e.g., live streaming updates)."""

    DELETE_MESSAGE = enum.auto()
    """Delete a previously-sent message."""

    THREADS = enum.auto()
    """Threaded / topic-based replies (Discord threads, Slack threads, Matrix replies)."""


class BaseChannelAdapter(ABC):
    """Base class for a messaging channel plugin.

    Required overrides: :attr:`platform`, :meth:`connect`, :meth:`disconnect`,
    :meth:`send`. Optional overrides depend on what the platform supports —
    see :class:`ChannelCapabilities`.
    """

    #: The platform this adapter serves.
    platform: Platform

    #: Max message length this platform accepts (in chars unless noted).
    max_message_length: int = 10_000

    #: Optional features this adapter supports. Adapters override this to
    #: advertise their feature set; the gateway checks before calling
    #: optional methods.
    capabilities: ChannelCapabilities = ChannelCapabilities.NONE

    #: Substrings (lowercased) used by :meth:`_is_retryable_error` to
    #: classify transient errors. Adapters can extend this tuple with
    #: platform-specific patterns (e.g. Telegram's "Bad Gateway").
    _RETRYABLE_ERROR_PATTERNS: tuple[str, ...] = (
        "connecterror",
        "connectionerror",
        "connectionreset",
        "connectionrefused",
        "connecttimeout",
        "network",
        "broken pipe",
        "remotedisconnected",
        "eoferror",
    )

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

    # ------------------------------------------------------------------
    # Optional capabilities — override when ``capabilities`` advertises them.
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str) -> None:
        """Send a typing indicator. Default is a no-op.

        Adapters with ``ChannelCapabilities.TYPING`` should override.
        """
        return None

    async def send_image(
        self, chat_id: str, image_url: str, caption: str = ""
    ) -> SendResult:
        """Send an image *by URL* (legacy entry point).

        For local-file paths use :meth:`send_photo`. Default raises
        ``NotImplementedError``; adapters with ``PHOTO_OUT`` capability must
        override at least one of these two methods.
        """
        raise NotImplementedError(f"{self.platform} adapter has no image-by-URL support")

    async def send_photo(
        self, chat_id: str, photo_path: str | Path, caption: str = "", **kwargs: Any
    ) -> SendResult:
        """Send a photo from a local file path.

        Adapters with ``ChannelCapabilities.PHOTO_OUT`` should override.
        Default raises ``NotImplementedError``.
        """
        raise NotImplementedError(f"{self.platform} adapter has no PHOTO_OUT capability")

    async def send_document(
        self, chat_id: str, file_path: str | Path, caption: str = "", **kwargs: Any
    ) -> SendResult:
        """Send a generic file (PDF, ZIP, etc.) from a local path.

        Adapters with ``ChannelCapabilities.DOCUMENT_OUT`` should override.
        """
        raise NotImplementedError(f"{self.platform} adapter has no DOCUMENT_OUT capability")

    async def send_voice(
        self, chat_id: str, audio_path: str | Path, caption: str = "", **kwargs: Any
    ) -> SendResult:
        """Send a voice / audio message from a local path.

        Adapters with ``ChannelCapabilities.VOICE_OUT`` should override.
        """
        raise NotImplementedError(f"{self.platform} adapter has no VOICE_OUT capability")

    async def send_reaction(
        self, chat_id: str, message_id: str, emoji: str, **kwargs: Any
    ) -> SendResult:
        """Add an emoji reaction to a previously-sent message.

        Adapters with ``ChannelCapabilities.REACTIONS`` should override.
        """
        raise NotImplementedError(f"{self.platform} adapter has no REACTIONS capability")

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        """Edit a previously-sent text message in place.

        Useful for streaming responses (single growing message) and live-updating
        status. Adapters with ``ChannelCapabilities.EDIT_MESSAGE`` should override.
        """
        raise NotImplementedError(f"{self.platform} adapter has no EDIT_MESSAGE capability")

    async def delete_message(
        self, chat_id: str, message_id: str, **kwargs: Any
    ) -> SendResult:
        """Delete a previously-sent message.

        Adapters with ``ChannelCapabilities.DELETE_MESSAGE`` should override.
        """
        raise NotImplementedError(f"{self.platform} adapter has no DELETE_MESSAGE capability")

    async def download_attachment(
        self, *, file_id: str, dest_dir: str | Path, **kwargs: Any
    ) -> Path:
        """Download an inbound attachment by platform-specific ``file_id``.

        Returns the local path the file was written to.

        Adapters with any of ``PHOTO_IN`` / ``VOICE_IN`` / ``DOCUMENT_IN`` should
        override. The agent calls this to materialise attachments referenced in
        ``MessageEvent.attachments`` when it needs the bytes.
        """
        raise NotImplementedError(f"{self.platform} adapter has no attachment-IN capability")

    async def send_notification(
        self, chat_id: str, text: str, *, urgent: bool = False
    ) -> SendResult:
        """Send a push notification.

        Default: same as `send()` — works on every platform but uses the same
        delivery mechanism as a regular message. Adapters that have a richer
        notification API (Telegram silent vs loud, Discord push) override this
        to use the platform's actual push-notification path.

        `urgent=True` is a hint adapters MAY honour by overriding silent-mode.
        """
        return await self.send(chat_id, text)

    # ------------------------------------------------------------------
    # Retry helpers — Hermes channel-port (PR 2 Task 2.1)
    # ------------------------------------------------------------------

    def _is_retryable_error(self, exc: BaseException) -> bool:
        """Heuristic: is *exc* a transient error worth retrying?

        Read/write timeouts are deliberately excluded — they're
        non-idempotent (the request may have already reached the server
        and produced a side-effect; retrying could double-send). Pure
        connect-time errors (``ConnectTimeout`` etc.) are retryable
        because the request never made it out.

        Class-name match is the primary signal — many SDKs (httpx,
        Anthropic, OpenAI) raise distinct error classes that don't
        share a base. Falls back to message-text match for the long
        tail of platform-native errors that look like ``OSError("network
        unreachable")``.
        """
        cls = type(exc).__name__.lower()
        # Exclude pure read/write timeouts; allow connect-timeouts.
        if "timeout" in cls and "connect" not in cls:
            return False
        if any(p in cls for p in self._RETRYABLE_ERROR_PATTERNS):
            return True
        msg = str(exc).lower()
        return any(p in msg for p in self._RETRYABLE_ERROR_PATTERNS)

    async def _send_with_retry(
        self,
        send_fn: Callable[..., Awaitable[SendResult]],
        *args: Any,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        **kwargs: Any,
    ) -> SendResult:
        """Retry transient send failures with exponential backoff + jitter.

        Returns the wrapped function's :class:`SendResult` on success,
        or a failure :class:`SendResult` after exhausting *max_attempts*.
        Non-retryable exceptions (per :meth:`_is_retryable_error`)
        propagate immediately so callers can distinguish "the network
        flapped" from "your request was malformed".

        ``base_delay`` is the first sleep; subsequent attempts double
        with up to 25% jitter — keeps a thundering-herd of stuck
        adapters from synchronising their retries.
        """
        last_exc: BaseException | None = None
        for attempt in range(max_attempts):
            try:
                return await send_fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                if not self._is_retryable_error(exc):
                    raise
                last_exc = exc
                if attempt + 1 >= max_attempts:
                    break
                delay = base_delay * (2 ** attempt) + random.uniform(
                    0, base_delay * 0.25
                )
                logger.warning(
                    "send retry %d/%d after %s: %s",
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                    str(exc)[:200],
                )
                await asyncio.sleep(delay)
        err = (
            f"{type(last_exc).__name__ if last_exc else 'Unknown'}: "
            f"{str(last_exc)[:300] if last_exc else 'no exc'}"
        )
        return SendResult(success=False, error=err)

    # ------------------------------------------------------------------
    # Reaction lifecycle hooks — Hermes channel-port (PR 2 Task 2.2)
    # ------------------------------------------------------------------

    async def on_processing_start(
        self, chat_id: str, message_id: str | None
    ) -> None:
        """Hook: called when the agent begins processing this message.

        Default behaviour: if :attr:`ChannelCapabilities.REACTIONS` is set
        AND ``message_id`` is provided, post a 👀 reaction so the user
        sees the bot picked up their message. Override per-platform for
        custom UX (typing indicator, status thread, etc.).

        Errors raised by ``send_reaction`` are swallowed — a hook
        failure must never take dispatch down.
        """
        if not message_id:
            return
        if not (self.capabilities & ChannelCapabilities.REACTIONS):
            return
        await self._run_processing_hook(
            self.send_reaction(chat_id, message_id, "👀")
        )

    async def on_processing_complete(
        self,
        chat_id: str,
        message_id: str | None,
        outcome: ProcessingOutcome,
    ) -> None:
        """Hook: called when the agent finishes processing.

        Default behaviour: replace the 👀 reaction with ✅ on
        :attr:`ProcessingOutcome.SUCCESS`, ❌ on
        :attr:`ProcessingOutcome.FAILURE`, or leave the eye in place on
        :attr:`ProcessingOutcome.CANCELLED` (the user stopped the run;
        a final-state emoji would be misleading).
        """
        if not message_id:
            return
        if not (self.capabilities & ChannelCapabilities.REACTIONS):
            return
        emoji_map = {
            ProcessingOutcome.SUCCESS: "✅",
            ProcessingOutcome.FAILURE: "❌",
            ProcessingOutcome.CANCELLED: "",
        }
        emoji = emoji_map.get(outcome, "")
        if not emoji:
            return
        await self._run_processing_hook(
            self.send_reaction(chat_id, message_id, emoji)
        )

    async def _run_processing_hook(self, coro: Awaitable[Any]) -> None:
        """Swallow exceptions from a fire-and-forget lifecycle coroutine.

        Lifecycle hooks (reactions, status updates) are decoration —
        their failure must never bubble into the user-facing reply path.
        """
        try:
            await coro
        except Exception:  # noqa: BLE001
            logger.debug(
                "processing-hook coroutine raised; swallowing", exc_info=True
            )


__all__ = ["BaseChannelAdapter", "ChannelCapabilities"]
