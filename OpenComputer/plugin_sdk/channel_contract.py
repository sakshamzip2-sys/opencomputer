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
import os
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugin_sdk.core import MessageEvent, Platform, ProcessingOutcome, SendResult

logger = logging.getLogger("plugin_sdk.channel_contract")


# ─── extract_media helpers ───────────────────────────────────────────


@dataclass(frozen=True)
class MediaItem:
    """A media attachment parsed from agent output (Hermes PR 2 Task 2.4).

    Produced by :meth:`BaseChannelAdapter.extract_media`. Frozen so the
    extracted path can't be mutated between extraction and the
    platform-specific ``send_*`` call (defence in depth against
    accidental rewrite).
    """

    path: str
    as_voice: bool
    ext: str


_MEDIA_EXT_WHITELIST: frozenset[str] = frozenset(
    {
        # images
        "png", "jpg", "jpeg", "gif", "webp",
        # video
        "mp4", "mov", "avi", "mkv", "webm",
        # audio
        "ogg", "opus", "mp3", "wav", "m4a",
        # documents
        "epub", "pdf", "zip", "docx", "doc",
        "xlsx", "xls", "pptx", "ppt",
        # text
        "txt", "csv", "md",
    }
)

_MEDIA_DIRECTIVE_RE = re.compile(
    r"(?:\[\[audio_as_voice\]\]\s*|MEDIA:\s*)"
    r"(?:\"([^\"]+)\"|'([^']+)'|`([^`]+)`|(\S+))",
)


# ─── extract_local_files helpers ─────────────────────────────────────


_BARE_PATH_RE = re.compile(
    r"(?<![/\w])(/[^\s`'\"<>]+\.[a-zA-Z0-9]{1,5})(?=\s|$|[.,;:!?])"
)
_HOME_PATH_RE = re.compile(
    r"(?<![/\w])(~/[^\s`'\"<>]+\.[a-zA-Z0-9]{1,5})(?=\s|$|[.,;:!?])"
)
_FENCE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


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
        # Hermes channel-port (PR 2 Task 2.3 + amendment §A.5):
        # adapter-level fatal-error state. Adapters call
        # ``_set_fatal_error`` from inside their poll loop / send path
        # when a non-recoverable condition is detected. The gateway's
        # ``_check_fatal_errors_periodic`` supervisor reads
        # ``has_fatal_error()`` every 60s and either reconnects (when
        # retryable=True) or logs ERROR (retryable=False).
        self._fatal_error_code: str | None = None
        self._fatal_error_message: str | None = None
        self._fatal_error_retryable: bool = False

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

    # ------------------------------------------------------------------
    # Fatal-error handoff — Hermes channel-port (PR 2 Task 2.3 / §A.5)
    # ------------------------------------------------------------------

    def _set_fatal_error(
        self, code: str, message: str, *, retryable: bool
    ) -> None:
        """Mark this adapter as fatally errored.

        Called by the adapter's own poll loop / send path when a
        non-recoverable condition is detected. The gateway supervisor
        reads ``has_fatal_error()`` periodically and either retries
        (when ``retryable=True``) or logs ERROR.

        ``code`` is a short token (``"conflict"``, ``"auth_failed"``,
        ``"network"``) suitable for log greping. ``message`` is the
        human-readable detail.
        """
        self._fatal_error_code = code
        self._fatal_error_message = message
        self._fatal_error_retryable = retryable
        logger.error(
            "adapter fatal error: platform=%s code=%s msg=%s retryable=%s",
            getattr(self, "platform", "?"),
            code,
            message,
            retryable,
        )

    def clear_fatal_error(self) -> None:
        """Reset fatal-error state.

        Per amendment §A.5: the gateway supervisor calls this after a
        successful disconnect/reconnect cycle rather than mutating the
        private fields directly — preserves encapsulation. Adapters can
        also call this themselves if they detect recovery (e.g. a
        long-poll error transient cleared on the next request).
        """
        self._fatal_error_code = None
        self._fatal_error_message = None
        self._fatal_error_retryable = False

    def has_fatal_error(self) -> bool:
        """``True`` iff ``_set_fatal_error`` was called and not cleared."""
        return self._fatal_error_code is not None

    # ------------------------------------------------------------------
    # Agent-output post-processing — Hermes channel-port (PR 2 Task 2.4)
    # ------------------------------------------------------------------

    def extract_local_files(
        self,
        content: str,
        allowed_dirs: list[Path] | None = None,
    ) -> tuple[str, list[Path]]:
        """Extract bare absolute file paths from agent output.

        Per amendment §A.8: paths outside ``allowed_dirs`` are NOT
        extracted, even if they exist on disk. Default allowlist is
        ``[~/Documents, /tmp]`` — the agent's two normal scratch dirs.
        Override via ``allowed_dirs=`` (channel adapters typically read
        ``self.config["attachments"]["allowed_dirs"]``).

        Excludes paths inside fenced code blocks or inline code so
        that a ``rm /etc/passwd`` example in a markdown explanation
        doesn't get attached as a real file. Validates path existence
        via ``os.path.isfile``. Returns ``(cleaned_text, [Path, ...])``.

        Relative paths are NOT extracted — only absolute ``/...`` and
        ``~/...`` paths qualify. The matched substring is removed from
        the cleaned text; surrounding whitespace is collapsed.
        """
        if not content:
            return content, []

        if allowed_dirs is None:
            allowed_dirs = [Path.home() / "Documents", Path("/tmp")]

        # Mask code regions so paths inside them aren't matched.
        masked = _FENCE_BLOCK_RE.sub(
            lambda m: "\x00" * len(m.group(0)), content
        )
        masked = _INLINE_CODE_RE.sub(
            lambda m: "\x00" * len(m.group(0)), masked
        )

        paths: list[Path] = []
        cleaned = content

        for regex in (_BARE_PATH_RE, _HOME_PATH_RE):
            for match in regex.finditer(masked):
                raw = match.group(1)
                expanded = Path(os.path.expanduser(raw))
                # Existence check — agent paths are real files only.
                try:
                    if not expanded.is_file():
                        continue
                except OSError:
                    continue
                # Allowlist check — defence in depth against
                # ``/etc/passwd``-style exfiltration.
                if not any(
                    self._is_subpath(expanded, d) for d in allowed_dirs
                ):
                    continue
                paths.append(expanded)
                cleaned = cleaned.replace(raw, "")

        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned, paths

    @staticmethod
    def _is_subpath(path: Path, allowed_dir: Path) -> bool:
        """``True`` iff ``path`` resolves into ``allowed_dir``.

        Uses ``Path.resolve()`` first so ``..`` / symlinks can't escape
        the allowlist. Returns False on any resolve / relative_to
        error (treats unresolvable paths as outside).
        """
        try:
            resolved = path.resolve()
            allowed = allowed_dir.resolve()
            resolved.relative_to(allowed)
            return True
        except (ValueError, OSError):
            return False

    def extract_media(self, content: str) -> tuple[str, list[MediaItem]]:
        """Parse ``MEDIA: <path>`` and ``[[audio_as_voice]] <path>`` directives.

        Whitelist-checks the file extension (see
        :data:`_MEDIA_EXT_WHITELIST`). The matched directive substring
        is removed from the cleaned text. Returns
        ``(cleaned_text, [MediaItem, ...])``.

        Note: ``extract_media`` does NOT verify the file exists or
        check an allowlist — that's the adapter's responsibility before
        attaching. The directive form is itself the agent's explicit
        instruction; allowlist enforcement happens at
        ``extract_local_files`` for the bare-path inference path.
        """
        if not content:
            return content, []
        items: list[MediaItem] = []
        cleaned = content
        for match in _MEDIA_DIRECTIVE_RE.finditer(content):
            path = next(g for g in match.groups() if g is not None)
            as_voice = "[[audio_as_voice]]" in match.group(0)
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext not in _MEDIA_EXT_WHITELIST:
                continue
            items.append(MediaItem(path=path, as_voice=as_voice, ext=ext))
            cleaned = cleaned.replace(match.group(0), "")
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned, items


__all__ = ["BaseChannelAdapter", "ChannelCapabilities", "MediaItem"]
