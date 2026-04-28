"""
TelegramAdapter — Telegram Bot API channel adapter.

Uses raw Bot API via httpx with long-polling, zero external deps beyond
httpx (already a project dep).

Capabilities (Sub-project G.2): typing, photo IN/OUT, document IN/OUT,
voice IN/OUT, reactions, edit, delete. See ``ChannelCapabilities`` flags
on the class.

Bot API limits applied here:
- Text: 4096 UTF-16 units / message (chunked on send)
- Photo: 10 MB outbound, 20 MB max for ``getFile`` download
- Document: 50 MB outbound, 20 MB ``getFile`` download
- Edit window: 48h after send
- Reactions: ``setMessageReaction`` requires bot to have reaction permission
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from extensions.telegram.network import (
    TelegramFallbackTransport,
    discover_fallback_ips,
    is_auto_mode,
    parse_fallback_ip_env,
)
from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult
from plugin_sdk.format_converters.markdownv2 import convert as _to_mdv2
from plugin_sdk.sticker_cache import StickerCache

logger = logging.getLogger("opencomputer.ext.telegram")

# Round 2a P-5 — inline-button callback prefix. The callback_data field
# in CallbackQuery is limited to 64 bytes by the Bot API, so we keep the
# format compact: ``"oc:approve:<verb>:<request_token>"``. Verb is one
# of ``once`` / ``always`` / ``deny``; request_token is opaque (UUID4
# hex truncated) so the backend can map it to (session_id, capability_id)
# without leaking those onto the wire.
_APPROVAL_CALLBACK_PREFIX = "oc:approve:"
# Maximum number of recently-seen callback_query ids we remember to
# de-duplicate double-clicks. Telegram retries deliveries that don't get
# answerCallbackQuery'd in time, so we MUST remember at least the last
# few hundred to absorb retries cleanly. 1024 is overkill but cheap.
_CALLBACK_DEDUPE_CAPACITY = 1024


#: P-2 round 2a — leading prefix that routes a Telegram message into the
#: SteerRegistry instead of the agent loop. The space after ``/steer`` is
#: required so a future ``/steerable`` command (or similar) doesn't collide.
_STEER_PREFIX = "/steer "


# Telegram MarkdownV2 requires escaping these characters
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"
_MDV2_RE = re.compile(f"([{re.escape(_MDV2_SPECIAL)}])")

# PR 3a.2 — Telegram returns 400 with this error string when our
# MarkdownV2 escaping produces output the parser rejects (e.g. an
# entity offset overlap). On hit, we retry the same call WITHOUT
# parse_mode and with the ORIGINAL (un-converted) text so the user
# at least gets the message body.
_MDV2_PARSE_ERROR_MARKER = "can't parse entities"

# PR 4.2 — Telegram returns 400 with this marker when the bot tries to
# post into a forum topic that has been deleted, archived, or that the
# bot has been booted from. We retry once WITHOUT message_thread_id so
# the message lands in the chat's General topic instead of disappearing.
_THREAD_NOT_FOUND_MARKER = "message thread not found"

# PR 4.2 — Telegram's "General" topic in a forum is conceptually
# message_thread_id 1, but the API REJECTS explicit thread_id=1 with
# "message thread not found". So we must omit message_thread_id when
# the caller passes 1 (or "1") so the post lands in General.
_GENERAL_TOPIC_THREAD_ID = "1"


def _escape_mdv2(text: str) -> str:
    return _MDV2_RE.sub(r"\\\1", text)


def _utf16_len(s: str) -> int:
    """Telegram's message length limit is in UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


def _is_thread_not_found_error(resp: httpx.Response | None) -> bool:
    """PR 4.2 — match Telegram's "message thread not found" 400.

    Returns True iff the response is HTTP 400 AND the body (case-insens)
    contains the marker phrase. Used by ``send`` to decide whether to
    retry without ``message_thread_id``.
    """
    if resp is None:
        return False
    if resp.status_code != 400:
        return False
    try:
        body = resp.text or ""
    except Exception:  # noqa: BLE001
        return False
    return _THREAD_NOT_FOUND_MARKER in body.lower()


def _chunk_for_telegram(text: str, limit: int = 4096) -> list[str]:
    """Split `text` so each chunk is ≤ `limit` UTF-16 units, respecting line breaks."""
    if _utf16_len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        line_len = _utf16_len(line)
        if current_len + line_len > limit and current:
            chunks.append("".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("".join(current))
    return chunks


class TelegramAdapter(BaseChannelAdapter):
    platform = Platform.TELEGRAM
    max_message_length = 4096  # UTF-16 units
    capabilities = (
        ChannelCapabilities.TYPING
        | ChannelCapabilities.REACTIONS
        | ChannelCapabilities.PHOTO_OUT
        | ChannelCapabilities.PHOTO_IN
        | ChannelCapabilities.DOCUMENT_OUT
        | ChannelCapabilities.DOCUMENT_IN
        | ChannelCapabilities.VOICE_OUT
        | ChannelCapabilities.VOICE_IN
        | ChannelCapabilities.EDIT_MESSAGE
        | ChannelCapabilities.DELETE_MESSAGE
    )

    # Telegram Bot API ceilings (bot accounts only — user accounts have higher limits)
    _MAX_PHOTO_SEND_BYTES = 10 * 1024 * 1024
    _MAX_DOCUMENT_SEND_BYTES = 50 * 1024 * 1024
    _MAX_GETFILE_BYTES = 20 * 1024 * 1024
    _EDIT_WINDOW_SECONDS = 48 * 3600

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.token = config["bot_token"]
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._client: httpx.AsyncClient | None = None
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._offset: int = 0
        self._polling_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # Hermes PR 3a.1 — mention-boundary gating (DEFAULT OFF). When
        # ``require_mention`` is True, group messages must explicitly
        # @-mention the bot (entity-based, NEVER substring) OR reply to
        # one of the bot's messages, OR match a configured wake-word
        # regex. ``free_response_chats`` lists chat ids exempt from the
        # gate (e.g. the operator's 1:1 DM). 1:1 chats also bypass the
        # gate by default — only group/supergroup chats are filtered.
        self._require_mention: bool = bool(config.get("require_mention") or False)
        self._free_response_chats: set[str] = {
            str(c) for c in (config.get("free_response_chats") or [])
        }
        # Compile wake-word regexes once. Bad patterns are logged + dropped
        # so a single mistyped entry doesn't break inbound delivery.
        self._mention_patterns: list[re.Pattern[str]] = []
        for pat in config.get("mention_patterns") or []:
            try:
                self._mention_patterns.append(re.compile(pat, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "telegram mention_patterns: ignoring invalid regex %r: %s",
                    pat, exc,
                )
        # PR 3a.5 — persistent sticker description cache. Keyed on
        # ``file_unique_id`` (stable across sends). Cache hit short-
        # circuits the vision-describe pipeline; misses are passed
        # through unchanged (the agent / provider does the actual
        # describing). ``profile_home`` is taken from config for
        # plugin-side flexibility; falls back to ``~/.opencomputer``.
        profile_home = Path(
            config.get("profile_home")
            or Path.home() / ".opencomputer"
        )
        try:
            profile_home.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._sticker_cache = StickerCache(profile_home)
        # Round 4 Item 3 — webhook mode config. Defaults to "polling"
        # so existing users see no change. Set "webhook" + a public
        # ``webhook_url`` (HTTPS) to switch. ``webhook_port`` defaults
        # to 8443 (Telegram's recommended port for self-hosted webhooks).
        self._mode: str = str(config.get("mode") or "polling").lower()
        self._webhook_url: str = str(config.get("webhook_url") or "")
        self._webhook_port: int = int(config.get("webhook_port") or 8443)
        self._webhook_secret: str = str(config.get("webhook_secret") or "")
        self._webhook_runner: Any = None  # aiohttp.web.AppRunner
        # Round 2a P-5 — inline approval-button bookkeeping.
        # ``_approval_callback`` is the function the gateway / agent loop
        # registers via :meth:`set_approval_callback` to receive button
        # clicks. The adapter intentionally doesn't import ConsentGate —
        # it routes raw ``(verb, token)`` tuples and lets the gateway
        # translate to (session_id, capability_id, decision, persist).
        self._approval_callback: (
            Callable[[str, str], Awaitable[None]] | None
        ) = None
        # ``_approval_tokens`` maps the opaque request_token we sent in
        # callback_data back to the chat_id + message_id we posted the
        # buttons in, so the callback handler can edit the message to
        # show the resolution ("✓ Allowed once" etc.) and stop accepting
        # further clicks for the same request.
        self._approval_tokens: dict[str, dict[str, Any]] = {}
        # Bounded dedupe set keyed on Telegram callback_query.id —
        # absorbs retries from the Bot API and double-clicks within a
        # single response window. Insertion order eviction keeps the
        # working set small.
        self._seen_callback_ids: OrderedDict[str, None] = OrderedDict()

    async def _post_with_retry(
        self,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response | SendResult:
        """PR 3a.3 — wrap ``self._client.post`` with the base
        ``_send_with_retry`` helper.

        Returns either the live :class:`httpx.Response` on success (so
        callers can inspect ``status_code`` and ``json()``) OR a
        :class:`SendResult` carrying ``success=False`` after the helper
        exhausts its retry budget on transient errors. Non-retryable
        exceptions propagate.
        """
        assert self._client is not None
        return await self._send_with_retry(  # type: ignore[return-value]
            self._client.post, url, **kwargs,
        )

    async def _build_fallback_transport(
        self,
    ) -> TelegramFallbackTransport | None:
        """Build the IP-fallback transport iff the env opts in.

        Reads ``TELEGRAM_FALLBACK_IPS``:
        - unset / empty → ``None`` (default; caller uses plain httpx).
        - ``"auto"`` → DoH discovery; if discovery returns at least one
          IP, wrap with that list.
        - comma-separated IPs → wrap with those after validation.
        """
        env_value = os.environ.get("TELEGRAM_FALLBACK_IPS", "")
        if not env_value:
            return None
        if is_auto_mode(env_value):
            try:
                ips = await discover_fallback_ips()
            except Exception as exc:  # noqa: BLE001 — discovery best-effort
                logger.warning(
                    "telegram fallback IP discovery failed (%s); "
                    "continuing without fallback",
                    exc,
                )
                return None
            if not ips:
                return None
            logger.info("telegram fallback IPs (auto-discovered): %s", ips)
            return TelegramFallbackTransport(ips)
        ips = parse_fallback_ip_env(env_value)
        if not ips:
            logger.warning(
                "TELEGRAM_FALLBACK_IPS=%r yielded no valid IPs; "
                "continuing without fallback",
                env_value,
            )
            return None
        logger.info("telegram fallback IPs (configured): %s", ips)
        return TelegramFallbackTransport(ips)

    #: Scope name for the per-bot-token machine-local lock. Mirrors
    #: hermes' ``"telegram-bot-token"`` scope so a future cross-tool
    #: convention could be uniformly recognised by tooling.
    _LOCK_SCOPE = "telegram-bot-token"

    async def connect(self) -> bool:
        # Hermes parity (gateway/status.py:464): take a machine-local
        # lock on the bot token BEFORE we open any HTTP connection.
        # Telegram's getUpdates long-poll only delivers each update to
        # ONE polling client — so two processes silently steal updates
        # from each other. The lock turns that into a fail-fast refusal
        # that names the holding PID.
        from opencomputer.security.scope_lock import acquire_scoped_lock

        ok, holder = acquire_scoped_lock(
            self._LOCK_SCOPE,
            self.token,
            metadata={"adapter": "telegram"},
        )
        if not ok:
            holder_pid = (holder or {}).get("pid", "unknown")
            logger.error(
                "telegram bot token already in use by PID %s — "
                "stop that process or run with a different bot token. "
                "Lock file: %s",
                holder_pid,
                self._scope_lock_path(),
            )
            self._lock_held = False
            return False
        self._lock_held = True

        # PR 4.1 — optional IP-fallback transport for users in geo-blocked
        # regions. Default behaviour (env unset) is unchanged: a regular
        # httpx client. ``TELEGRAM_FALLBACK_IPS=auto`` triggers DoH
        # discovery; a comma-separated list of IPs uses those directly.
        transport = await self._build_fallback_transport()
        if transport is not None:
            self._client = httpx.AsyncClient(timeout=35.0, transport=transport)
        else:
            self._client = httpx.AsyncClient(timeout=35.0)
        # getMe to verify token and cache our bot id
        try:
            resp = await self._client.get(f"{self.base_url}/getMe")
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                logger.error("telegram getMe failed: %s", data)
                self._release_lock()
                return False
            self._bot_id = data["result"]["id"]
            # PR 3a.1 — capture the bot's @username so entity-based
            # mention matching can compare exactly (no substring).
            self._bot_username = data["result"].get("username") or None
            logger.info(
                "telegram: connected as @%s (id=%s)",
                self._bot_username or "?",
                self._bot_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("telegram connect failed: %s", e)
            self._release_lock()
            return False

        # Round 4 Item 3 — webhook mode branch.
        if self._mode == "webhook":
            ok = await self._start_webhook_mode()
            if not ok:
                self._release_lock()
                return False
            return True

        # Default: long-polling loop.
        self._polling_task = asyncio.create_task(self._poll_forever())
        return True

    async def _start_webhook_mode(self) -> bool:
        """Spin up the aiohttp webhook server + register URL with Telegram."""
        if not self._webhook_url:
            logger.error(
                "telegram webhook mode: webhook_url not configured. "
                "Set telegram.webhook_url to your public HTTPS URL "
                "(e.g. https://your-tunnel.ngrok.io/telegram/webhook) "
                "or run `opencomputer telegram tunnel detect`."
            )
            return False

        from extensions.telegram.webhook_helper import (
            generate_secret_token,
            set_webhook,
            start_webhook_server,
        )

        # Generate a secret on first connect if the user didn't provide
        # one — Telegram echoes it on every push, we verify constant-
        # time on receive.
        secret = self._webhook_secret or generate_secret_token()
        self._webhook_secret = secret

        try:
            self._webhook_runner = await start_webhook_server(
                secret_token=secret,
                port=self._webhook_port,
                handle_update=self._handle_update,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("telegram webhook server failed to start: %s", exc)
            return False

        ok, msg = await set_webhook(
            token=self.token,
            url=self._webhook_url,
            secret_token=secret,
            drop_pending=True,
            allowed_updates=["message", "callback_query"],
        )
        if not ok:
            logger.error("telegram setWebhook failed: %s", msg)
            await self._webhook_runner.cleanup()
            self._webhook_runner = None
            return False
        logger.info("telegram webhook registered: %s", self._webhook_url)
        return True

    def _scope_lock_path(self) -> str:
        from opencomputer.security.scope_lock import _get_scope_lock_path

        return str(_get_scope_lock_path(self._LOCK_SCOPE, self.token))

    def _release_lock(self) -> None:
        if not getattr(self, "_lock_held", False):
            return
        try:
            from opencomputer.security.scope_lock import release_scoped_lock

            release_scoped_lock(self._LOCK_SCOPE, self.token)
        except Exception as exc:  # noqa: BLE001 — release is best-effort
            logger.debug("telegram lock release failed: %s", exc)
        self._lock_held = False

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        # Round 4 Item 3 — webhook teardown. Deregister at Telegram
        # first so they stop pushing, then shut down our server.
        if self._mode == "webhook":
            try:
                from extensions.telegram.webhook_helper import delete_webhook

                await delete_webhook(token=self.token)
            except Exception as exc:  # noqa: BLE001 — disconnect must not raise
                logger.debug("telegram deleteWebhook on disconnect: %s", exc)
            if self._webhook_runner is not None:
                try:
                    await self._webhook_runner.cleanup()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("webhook server cleanup: %s", exc)
                self._webhook_runner = None
        if self._client is not None:
            await self._client.aclose()
        self._release_lock()

    # PR 3a.4 — exponential-ish backoff schedule for transient network
    # errors during long-poll. After the 10th consecutive failure the
    # supervisor is asked to take over (retryable=True so it restarts).
    _NETWORK_BACKOFF_SCHEDULE: tuple[int, ...] = (5, 10, 20, 40, 60, 60, 60, 60, 60, 60)
    _MAX_CONSECUTIVE_409S: int = 3
    _CONFLICT_BACKOFF_SECONDS: int = 10

    async def _poll_forever(self) -> None:
        assert self._client is not None
        # OpenClaw parity (CHANGELOG #69873): a 409 "Conflict" from
        # getUpdates means another client snatched the long-poll
        # subscription out from under us — the lock prevents this
        # locally, but cross-machine duplicates can still happen. Log
        # loudly + back off so we don't spam Telegram with rapid
        # re-polls if the conflict persists.
        # PR 3a.4 — after 3 consecutive 409s (with 10s sleeps between),
        # we set a fatal-non-retryable error and break: another process
        # is durably holding the polling slot, restarting won't help.
        # Network errors get up to 10 retries with the schedule above
        # before we set fatal-retryable and let the gateway supervisor
        # decide whether to reconnect.
        consecutive_409s = 0
        consecutive_network_errors = 0
        while not self._stop_event.is_set():
            try:
                # Round 2a P-5 — also subscribe to ``callback_query``
                # so inline-keyboard button clicks reach the adapter.
                params = {
                    "timeout": 30,
                    "offset": self._offset,
                    "allowed_updates": ["message", "callback_query"],
                }
                resp = await self._client.get(f"{self.base_url}/getUpdates", params=params)
                if resp.status_code == 409:
                    consecutive_409s += 1
                    if consecutive_409s > self._MAX_CONSECUTIVE_409S:
                        self._set_fatal_error(
                            "telegram-conflict",
                            "another process is polling — stop it or rotate token",
                            retryable=False,
                        )
                        break
                    logger.warning(
                        "telegram getUpdates returned 409 Conflict — another "
                        "process is also polling this bot's updates. "
                        "Sleeping %ss (attempt #%d). Stop the other client "
                        "or use a different bot token.",
                        self._CONFLICT_BACKOFF_SECONDS, consecutive_409s,
                    )
                    await asyncio.sleep(self._CONFLICT_BACKOFF_SECONDS)
                    continue
                if resp.status_code != 200:
                    await asyncio.sleep(2)
                    continue
                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(2)
                    continue
                # Successful poll — reset both counters.
                consecutive_409s = 0
                consecutive_network_errors = 0
                for update in data.get("result", []):
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                consecutive_network_errors += 1
                if consecutive_network_errors > len(self._NETWORK_BACKOFF_SCHEDULE):
                    # PR 3a.4 — past the 10-retry budget. Hand off to
                    # the gateway supervisor (retryable=True so it tries
                    # to restart the adapter).
                    self._set_fatal_error(
                        "telegram-network",
                        f"transport down: {type(e).__name__}: {str(e)[:200]}",
                        retryable=True,
                    )
                    break
                # Index is 1-based attempt number → 0-based schedule.
                sleep_secs = self._NETWORK_BACKOFF_SCHEDULE[
                    consecutive_network_errors - 1
                ]
                logger.warning(
                    "telegram polling error #%d/%d: %s — sleeping %ss",
                    consecutive_network_errors,
                    len(self._NETWORK_BACKOFF_SCHEDULE),
                    e,
                    sleep_secs,
                )
                await asyncio.sleep(sleep_secs)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        # Round 2a P-5 — route inline-button clicks to the callback path
        # before checking for a regular message. Telegram delivers each
        # update with at most one of these populated.
        cbq = update.get("callback_query")
        if cbq is not None:
            await self._handle_callback_query(cbq)
            return
        msg = update.get("message")
        if msg is None:
            return
        frm = msg.get("from", {})
        # Skip self-messages (some platforms echo)
        if self._bot_id is not None and frm.get("id") == self._bot_id:
            return

        # PR 3a.1 — mention-boundary gate. Default-OFF; when enabled
        # group messages must explicitly mention the bot or be replies
        # to it. 1:1 DMs always pass through.
        if not self._should_process_message(msg):
            return

        # Text — may be empty if the message is just an attachment with no caption
        text = msg.get("text") or msg.get("caption", "")

        # Attachments — extract file_ids so the agent can download lazily.
        # Stored as ``"telegram:<file_id>"`` references in MessageEvent.attachments;
        # call adapter.download_attachment(file_id=...) when bytes are needed.
        attachments: list[str] = []
        attachment_meta: list[dict[str, Any]] = []
        if photos := msg.get("photo"):
            # `photo` is an array of size variants; the last entry is largest.
            largest = photos[-1]
            file_id = largest.get("file_id")
            if file_id:
                attachments.append(f"telegram:{file_id}")
                attachment_meta.append(
                    {"type": "photo", "file_id": file_id, "mime": "image/jpeg",
                     "size": largest.get("file_size"), "width": largest.get("width"),
                     "height": largest.get("height")}
                )
        if doc := msg.get("document"):
            file_id = doc.get("file_id")
            if file_id:
                attachments.append(f"telegram:{file_id}")
                attachment_meta.append(
                    {"type": "document", "file_id": file_id,
                     "mime": doc.get("mime_type"), "size": doc.get("file_size"),
                     "filename": doc.get("file_name")}
                )
        if voice := msg.get("voice"):
            file_id = voice.get("file_id")
            if file_id:
                attachments.append(f"telegram:{file_id}")
                attachment_meta.append(
                    {"type": "voice", "file_id": file_id,
                     "mime": voice.get("mime_type") or "audio/ogg",
                     "size": voice.get("file_size"),
                     "duration": voice.get("duration")}
                )

        # PR 3a.5 — sticker handling. Cache hit short-circuits to
        # ``[sticker: <description>]`` injected into the text body so
        # the agent has something semantic to react to. Cache miss:
        # surface as an attachment-style ``telegram:<file_id>`` ref
        # plus an ``attachment_meta`` entry so the provider/vision
        # pipeline can describe and ``put()`` the result later.
        if sticker := msg.get("sticker"):
            uniq = sticker.get("file_unique_id")
            sticker_file_id = sticker.get("file_id")
            cached = self._sticker_cache.get(uniq) if uniq else None
            if cached:
                # Inject as readable text so downstream agent code
                # treats it like any other utterance.
                text = (text + " " if text else "") + f"[sticker: {cached}]"
            elif sticker_file_id:
                # Pass-through — provider-side vision will describe and
                # may call back into the cache via ``put()``.
                attachments.append(f"telegram:{sticker_file_id}")
                attachment_meta.append(
                    {
                        "type": "sticker",
                        "file_id": sticker_file_id,
                        "file_unique_id": uniq,
                        "is_animated": bool(sticker.get("is_animated")),
                        "is_video": bool(sticker.get("is_video")),
                        "emoji": sticker.get("emoji"),
                        "set_name": sticker.get("set_name"),
                    }
                )

        # Skip messages with no text and no attachments — they're metadata-only updates
        # (e.g., chat-photo-changed, member-added) we don't surface to the agent.
        if not text and not attachments:
            return

        # P-2 round 2a — ``/steer <text>`` is intercepted BEFORE the message
        # reaches the gateway. We route the body into SteerRegistry keyed by
        # the same session_id the dispatcher would have used for this chat,
        # then send a short ack back to the user. The agent loop picks up
        # the nudge between turns on its next iteration boundary.
        if text and text.startswith(_STEER_PREFIX):
            await self._handle_steer_command(
                chat_id=str(msg["chat"]["id"]),
                text=text,
            )
            return

        metadata: dict[str, Any] = {"message_id": msg.get("message_id")}
        if attachment_meta:
            metadata["attachment_meta"] = attachment_meta

        event = MessageEvent(
            platform=Platform.TELEGRAM,
            chat_id=str(msg["chat"]["id"]),
            user_id=str(frm.get("id", "")),
            text=text,
            attachments=attachments,
            timestamp=float(msg.get("date", time.time())),
            metadata=metadata,
        )
        await self.handle_message(event)

    # ------------------------------------------------------------------
    # PR 3a.1 — mention-boundary helpers
    # ------------------------------------------------------------------

    def _message_mentions_bot(self, msg: dict[str, Any]) -> bool:
        """Entity-based @-mention detection.

        Telegram puts every @mention in the ``entities`` (or
        ``caption_entities``) array with ``type="mention"`` for plain
        ``@username`` references, or ``type="text_mention"`` for users
        without a public username (which carries the user object inline).

        We match strictly against ``self._bot_username`` (exact, case-
        insensitive) and ``self._bot_id``. NEVER substring — that would
        treat ``@hermes_bot_admin`` as a mention of ``@hermes_bot``.
        """
        entities = msg.get("entities") or msg.get("caption_entities") or []
        if not entities:
            return False
        text = msg.get("text") or msg.get("caption") or ""
        # UTF-16 indexing: Telegram entity offsets are in UTF-16 code units,
        # but for the @mention case we just need the substring at the entity
        # range; Python's str slice on the BMP-only ASCII bot username is
        # fine because @-usernames are 7-bit ASCII per Telegram rules.
        text_utf16 = text.encode("utf-16-le")
        for ent in entities:
            etype = ent.get("type")
            if etype == "mention":
                offset = int(ent.get("offset", 0))
                length = int(ent.get("length", 0))
                # Slice in UTF-16 code units, decode back to str.
                try:
                    raw = text_utf16[offset * 2 : (offset + length) * 2].decode(
                        "utf-16-le"
                    )
                except UnicodeDecodeError:
                    continue
                # ``raw`` is e.g. "@hermes_bot". Compare case-insensitively
                # against our @username; exact equality only.
                if (
                    raw.startswith("@")
                    and self._bot_username is not None
                    and raw[1:].lower() == self._bot_username.lower()
                ):
                    return True
            elif etype == "text_mention":
                user = ent.get("user") or {}
                if (
                    self._bot_id is not None
                    and user.get("id") == self._bot_id
                ):
                    return True
        return False

    def _is_reply_to_bot(self, msg: dict[str, Any]) -> bool:
        """``True`` iff the message is a reply to one of our messages."""
        reply_to = msg.get("reply_to_message")
        if not reply_to:
            return False
        sender = reply_to.get("from") or {}
        return self._bot_id is not None and sender.get("id") == self._bot_id

    def _should_process_message(self, msg: dict[str, Any]) -> bool:
        """Apply the mention-boundary gate to a raw inbound message dict.

        Default-OFF: when ``require_mention`` is False, every message
        passes (preserves pre-3a behaviour exactly — see audit C4 mandate).

        When enabled, the gate applies ONLY to group/supergroup chats.
        Private (1:1) chats always pass — there's no ambiguity about
        addressee in a DM. ``free_response_chats`` exempts specific
        chat ids from the gate even in groups.
        """
        if not self._require_mention:
            return True

        chat = msg.get("chat") or {}
        chat_type = chat.get("type") or "private"
        if chat_type == "private":
            return True

        chat_id = str(chat.get("id", ""))
        if chat_id in self._free_response_chats:
            return True

        if self._is_reply_to_bot(msg):
            return True

        if self._message_mentions_bot(msg):
            return True

        # Wake-word regex patterns operate on plain text — the user
        # opted into matching specific tokens (e.g. r"\bhey hermes\b").
        if self._mention_patterns:
            text = msg.get("text") or msg.get("caption") or ""
            if any(p.search(text) for p in self._mention_patterns):
                return True

        return False

    async def _handle_steer_command(self, *, chat_id: str, text: str) -> None:
        """Route a ``/steer <text>`` Telegram message into SteerRegistry.

        Called from :meth:`_handle_update` when an inbound message starts
        with ``/steer ``. Latest-wins is enforced inside
        :meth:`SteerRegistry.submit`; here we just normalize, derive the
        session id the dispatcher would key on for this chat, and ack
        the user. Empty bodies (``/steer`` with nothing after it) get a
        usage hint instead of being silently dropped.
        """
        # Lazy imports keep the SteerRegistry / dispatch coupling out
        # of plugin discovery — the adapter must still import cleanly
        # in environments where the gateway hasn't been initialised.
        from opencomputer.agent.steer import default_registry as _steer_registry
        from opencomputer.gateway.dispatch import session_id_for

        body = text[len(_STEER_PREFIX) :].strip()
        if not body:
            await self.send(
                chat_id,
                "usage: /steer <prompt>\n"
                "(injects a mid-run nudge into the next agent turn).",
            )
            return

        session_id = session_id_for(Platform.TELEGRAM.value, chat_id)
        had_pending = _steer_registry.has_pending(session_id)
        _steer_registry.submit(session_id, body)
        ack = (
            f"steer queued for this chat ({len(body)} chars). "
            "It will be applied at the next turn boundary."
        )
        if had_pending:
            ack = "steer override: previous nudge discarded.\n" + ack
        await self.send(chat_id, ack)

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        assert self._client is not None
        # PR 4.2 — forum-topic support. Caller may pass
        # ``message_thread_id`` to target a specific topic. We omit
        # thread_id == "1" (Telegram's General topic) because the API
        # rejects explicit thread_id=1 with "message thread not found".
        # On a thread-not-found 400 from a non-General topic, we retry
        # the same call WITHOUT message_thread_id so the message at
        # least lands in General instead of vanishing.
        thread_id = kwargs.get("message_thread_id")
        if thread_id is not None and str(thread_id) == _GENERAL_TOPIC_THREAD_ID:
            thread_id = None

        # PR 3a.2 — outbound MarkdownV2 formatting. Each chunk is
        # converted; on a parse-error (rare — usually overlap with
        # user-supplied backticks) we retry the SAME call without
        # parse_mode and with the ORIGINAL text so the user gets the
        # body.
        # PR 3a.3 — httpx POST wrapped in _send_with_retry for transient
        # network errors (ConnectError etc.) before we even see HTTP.
        for chunk in _chunk_for_telegram(text, limit=self.max_message_length):
            converted = _to_mdv2(chunk)
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": converted,
                "parse_mode": "MarkdownV2",
                "disable_notification": False,
            }
            if thread_id is not None:
                payload["message_thread_id"] = thread_id
            try:
                resp = await self._post_with_retry(
                    f"{self.base_url}/sendMessage",
                    json=payload,
                )
                if isinstance(resp, SendResult):
                    return resp  # exhausted retries on transient errors
                # PR 4.2 — thread-not-found fallback: retry without the
                # thread id so the post lands in General. Only triggers
                # when we actually had a thread id to begin with.
                if thread_id is not None and _is_thread_not_found_error(resp):
                    logger.warning(
                        "telegram: message_thread_id=%s not found in chat %s; "
                        "retrying without thread",
                        thread_id, chat_id,
                    )
                    fallback_payload = {
                        k: v for k, v in payload.items() if k != "message_thread_id"
                    }
                    resp = await self._post_with_retry(
                        f"{self.base_url}/sendMessage",
                        json=fallback_payload,
                    )
                    if isinstance(resp, SendResult):
                        return resp
                # Parse-error fallback: re-send with the original (un-converted)
                # text and no parse_mode.
                if (
                    resp.status_code == 400
                    and _MDV2_PARSE_ERROR_MARKER in resp.text.lower()
                ):
                    plain_payload: dict[str, Any] = {
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_notification": False,
                    }
                    if thread_id is not None:
                        plain_payload["message_thread_id"] = thread_id
                    resp = await self._post_with_retry(
                        f"{self.base_url}/sendMessage",
                        json=plain_payload,
                    )
                    if isinstance(resp, SendResult):
                        return resp
                if resp.status_code != 200:
                    return SendResult(
                        success=False,
                        error=f"telegram HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                data = resp.json()
                if not data.get("ok"):
                    return SendResult(success=False, error=str(data))
            except Exception as e:  # noqa: BLE001
                return SendResult(success=False, error=f"{type(e).__name__}: {e}")
        return SendResult(success=True)

    async def send_typing(self, chat_id: str) -> None:
        if self._client is None:
            return
        try:
            # PR 3a.3 — typing is best-effort; retry transient errors but
            # swallow any final-state failure (don't block the agent).
            await self._post_with_retry(
                f"{self.base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # G.2 — file attachments + reactions + edit/delete (ChannelCapabilities)
    # ------------------------------------------------------------------

    async def send_photo(
        self,
        chat_id: str,
        photo_path: str | Path,
        caption: str = "",
        **kwargs: Any,
    ) -> SendResult:
        """Send a photo from a local file path. Returns SendResult."""
        return await self._send_media(
            chat_id, photo_path, "sendPhoto", "photo", caption,
            self._MAX_PHOTO_SEND_BYTES, "photo",
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str | Path,
        caption: str = "",
        **kwargs: Any,
    ) -> SendResult:
        """Send a generic file (PDF, ZIP, etc.) from a local path."""
        return await self._send_media(
            chat_id, file_path, "sendDocument", "document", caption,
            self._MAX_DOCUMENT_SEND_BYTES, "document",
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str | Path,
        caption: str = "",
        **kwargs: Any,
    ) -> SendResult:
        """Send a voice message (.ogg/.opus) from a local path."""
        return await self._send_media(
            chat_id, audio_path, "sendVoice", "voice", caption,
            self._MAX_DOCUMENT_SEND_BYTES, "voice",
        )

    async def _send_media(
        self,
        chat_id: str,
        path: str | Path,
        endpoint: str,
        field_name: str,
        caption: str,
        size_limit: int,
        kind: str,
    ) -> SendResult:
        """Common multipart-upload path for sendPhoto / sendDocument / sendVoice."""
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")

        p = Path(path)
        if not p.exists():
            return SendResult(success=False, error=f"file not found: {p}")
        if not p.is_file():
            return SendResult(success=False, error=f"not a file: {p}")

        size = p.stat().st_size
        if size > size_limit:
            return SendResult(
                success=False,
                error=(
                    f"telegram bot {kind} limit is {size_limit // 1024 // 1024}MB; "
                    f"file is {size // 1024 // 1024}MB"
                ),
            )

        try:
            # PR 3a.2 — outbound caption uses MarkdownV2 with parse-error
            # fallback. We open the file twice (once per attempt) so the
            # multipart upload sees a fresh file pointer if we have to
            # retry without parse_mode.
            def _build_form(use_mdv2: bool) -> dict[str, Any]:
                form: dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    if use_mdv2:
                        form["caption"] = _to_mdv2(caption)[:1024]
                        form["parse_mode"] = "MarkdownV2"
                    else:
                        form["caption"] = caption[:1024]
                return form

            with p.open("rb") as fh:
                files = {field_name: (p.name, fh, _guess_mime(p))}
                resp = await self._post_with_retry(
                    f"{self.base_url}/{endpoint}",
                    data=_build_form(use_mdv2=bool(caption)),
                    files=files,
                )
            if isinstance(resp, SendResult):
                return resp
            # Parse-error fallback — re-upload without parse_mode and
            # with the ORIGINAL caption text.
            if (
                caption
                and resp.status_code == 400
                and _MDV2_PARSE_ERROR_MARKER in resp.text.lower()
            ):
                with p.open("rb") as fh:
                    files = {field_name: (p.name, fh, _guess_mime(p))}
                    resp = await self._post_with_retry(
                        f"{self.base_url}/{endpoint}",
                        data=_build_form(use_mdv2=False),
                        files=files,
                    )
                    if isinstance(resp, SendResult):
                        return resp
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"telegram {endpoint} HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            if not data.get("ok"):
                return SendResult(success=False, error=str(data))
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def send_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
        **kwargs: Any,
    ) -> SendResult:
        """Add an emoji reaction to a message via setMessageReaction.

        Telegram supports a limited set of reaction emoji per chat policy.
        Common safe choices: 👍 👎 ❤️ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤️‍🔥 🌚 🌭 💯 🤣 ⚡️ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍️ 🤗 🫡 🎅 🎄 ☃️ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂️ 🤷 🤷‍♀️ 😡
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        try:
            resp = await self._post_with_retry(
                f"{self.base_url}/setMessageReaction",
                json={
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
            )
            if isinstance(resp, SendResult):
                return resp
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"telegram setMessageReaction HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            if not data.get("ok"):
                return SendResult(success=False, error=str(data))
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        **kwargs: Any,
    ) -> SendResult:
        """Edit a previously-sent text message in place.

        Telegram allows edits up to 48h after the original send. Beyond that
        window, the API returns 400 ``MESSAGE_CAN'T_BE_EDITED`` — caller should
        fall back to a new ``send()``.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        try:
            # PR 3a.2 — MarkdownV2 + parse-error fallback for edits too.
            truncated = text[: self.max_message_length]
            converted = _to_mdv2(truncated)
            resp = await self._post_with_retry(
                f"{self.base_url}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "text": converted,
                    "parse_mode": "MarkdownV2",
                },
            )
            if isinstance(resp, SendResult):
                return resp
            if (
                resp.status_code == 400
                and _MDV2_PARSE_ERROR_MARKER in resp.text.lower()
            ):
                resp = await self._post_with_retry(
                    f"{self.base_url}/editMessageText",
                    json={
                        "chat_id": chat_id,
                        "message_id": int(message_id),
                        "text": truncated,
                    },
                )
                if isinstance(resp, SendResult):
                    return resp
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"telegram editMessageText HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            if not data.get("ok"):
                return SendResult(success=False, error=str(data))
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
        **kwargs: Any,
    ) -> SendResult:
        """Delete a previously-sent message."""
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        try:
            resp = await self._post_with_retry(
                f"{self.base_url}/deleteMessage",
                json={"chat_id": chat_id, "message_id": int(message_id)},
            )
            if isinstance(resp, SendResult):
                return resp
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"telegram deleteMessage HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            if not data.get("ok"):
                return SendResult(success=False, error=str(data))
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def download_attachment(
        self,
        *,
        file_id: str,
        dest_dir: str | Path,
        **kwargs: Any,
    ) -> Path:
        """Download an inbound attachment.

        ``file_id`` is the Telegram ``file_id`` referenced in
        ``MessageEvent.attachments`` as ``"telegram:<file_id>"``. Strip the
        prefix before passing.

        Returns the absolute path to the downloaded file.

        Raises:
            RuntimeError: download failed or file exceeds 20 MB ``getFile`` limit.
        """
        if self._client is None:
            raise RuntimeError("adapter not connected")

        # Strip "telegram:" prefix if caller forgot
        if file_id.startswith("telegram:"):
            file_id = file_id.removeprefix("telegram:")

        # Step 1: getFile to resolve the file_path on Telegram's CDN
        resp = await self._client.post(
            f"{self.base_url}/getFile",
            json={"file_id": file_id},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"telegram getFile HTTP {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram getFile failed: {data}")

        result = data["result"]
        size = result.get("file_size", 0)
        if size > self._MAX_GETFILE_BYTES:
            raise RuntimeError(
                f"telegram getFile limit is {self._MAX_GETFILE_BYTES // 1024 // 1024}MB; "
                f"file is {size // 1024 // 1024}MB"
            )

        relative_path = result.get("file_path")
        if not relative_path:
            raise RuntimeError(f"telegram getFile returned no file_path: {result}")

        # Step 2: download from CDN
        download_url = f"https://api.telegram.org/file/bot{self.token}/{relative_path}"
        download_resp = await self._client.get(download_url)
        if download_resp.status_code != 200:
            raise RuntimeError(
                f"telegram CDN download HTTP {download_resp.status_code}"
            )

        # Step 3: persist to disk under dest_dir using the original filename if available
        dest_dir_path = Path(dest_dir)
        dest_dir_path.mkdir(parents=True, exist_ok=True)
        # file_path looks like "photos/file_3.jpg" — keep just the basename
        out_name = Path(relative_path).name or f"{file_id}.bin"
        out_path = dest_dir_path / out_name
        out_path.write_bytes(download_resp.content)
        return out_path.resolve()

    # ------------------------------------------------------------------
    # Round 2a P-5 — F1 consent inline-approval buttons
    # ------------------------------------------------------------------

    def set_approval_callback(
        self, callback: Callable[[str, str], Awaitable[None]]
    ) -> None:
        """Register the coroutine that receives ``(verb, request_token)`` clicks.

        ``verb`` is one of ``"once"``, ``"always"``, ``"deny"``;
        ``request_token`` is the opaque token the caller minted when it
        invoked :meth:`send_approval_request`. The gateway is responsible
        for translating those back into a ``ConsentGate.resolve_pending``
        call (it owns the session_id ↔ token map).

        Replaces any previously-registered callback.
        """
        self._approval_callback = callback

    async def send_approval_request(
        self,
        chat_id: str,
        prompt_text: str,
        request_token: str,
        **kwargs: Any,
    ) -> SendResult:
        """Post an inline-keyboard approval prompt with three buttons.

        ``prompt_text`` SHOULD be the result of
        ``ConsentGate.render_prompt(claim, scope)`` so we don't introduce
        a parallel risk classifier (per "no regex layer" / "F1 owns
        tier model" rule).

        ``request_token`` is the opaque correlation id the caller
        provides; the same token shows up on the resulting
        ``callback_query`` so the gateway can map clicks back to the
        original (session_id, capability_id) pair without leaking those
        onto the wire.

        The button layout is a single row of three buttons:
        ``[✓ Allow once] [✓ Allow always] [✗ Deny]``. Each
        ``callback_data`` is ``"oc:approve:<verb>:<token>"`` where
        ``<verb>`` is ``once`` / ``always`` / ``deny`` — under the 64-byte
        Telegram limit even for long-ish tokens (UUID4 hex = 32 chars,
        so total ≤ 50 chars).
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")

        # Compose buttons. Each button's callback_data is opaque; the
        # gateway maps token → (session_id, capability_id) via its own
        # registry. We never put the session id on the wire.
        keyboard = [
            [
                {
                    "text": "✓ Allow once",
                    "callback_data": f"{_APPROVAL_CALLBACK_PREFIX}once:{request_token}",
                },
                {
                    "text": "✓ Allow always",
                    "callback_data": f"{_APPROVAL_CALLBACK_PREFIX}always:{request_token}",
                },
                {
                    "text": "✗ Deny",
                    "callback_data": f"{_APPROVAL_CALLBACK_PREFIX}deny:{request_token}",
                },
            ]
        ]
        try:
            # PR 3a.2 — MarkdownV2 + parse-error fallback for approval prompt.
            converted = _to_mdv2(prompt_text)
            resp = await self._post_with_retry(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": converted,
                    "parse_mode": "MarkdownV2",
                    "reply_markup": {"inline_keyboard": keyboard},
                },
            )
            if isinstance(resp, SendResult):
                return resp
            if (
                resp.status_code == 400
                and _MDV2_PARSE_ERROR_MARKER in resp.text.lower()
            ):
                resp = await self._post_with_retry(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": prompt_text,
                        "reply_markup": {"inline_keyboard": keyboard},
                    },
                )
                if isinstance(resp, SendResult):
                    return resp
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=(
                        f"telegram sendMessage HTTP {resp.status_code}: "
                        f"{resp.text[:200]}"
                    ),
                )
            data = resp.json()
            if not data.get("ok"):
                return SendResult(success=False, error=str(data))
            # Stash chat_id + message_id so the callback handler can edit
            # the message in place after resolution (removes buttons,
            # confirms what was clicked).
            sent_msg = data.get("result") or {}
            self._approval_tokens[request_token] = {
                "chat_id": chat_id,
                "message_id": sent_msg.get("message_id"),
            }
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def _handle_callback_query(self, cbq: dict[str, Any]) -> None:
        """Dispatch an inline-button click to the registered approval callback.

        Dedupe by ``callback_query.id`` — Telegram retries deliveries
        until we ``answerCallbackQuery``, and a fast double-click sends
        two distinct ids in quick succession so we ALSO key on the
        underlying request_token to drop the second click.
        """
        cbq_id = cbq.get("id")
        if not cbq_id:
            return

        # Drop already-seen callback ids (Telegram retry).
        if cbq_id in self._seen_callback_ids:
            return
        self._seen_callback_ids[cbq_id] = None
        # Bound the dedupe set so it doesn't grow unbounded over a long
        # uptime; eviction in insertion order is fine because the only
        # thing we need to remember is "very recent ids".
        while len(self._seen_callback_ids) > _CALLBACK_DEDUPE_CAPACITY:
            self._seen_callback_ids.popitem(last=False)

        # Always ack the callback so the user's button stops spinning,
        # even if the data is malformed or stale.
        await self._answer_callback_query(cbq_id)

        data = cbq.get("data") or ""
        if not data.startswith(_APPROVAL_CALLBACK_PREFIX):
            return  # not for us — silently ignore
        rest = data[len(_APPROVAL_CALLBACK_PREFIX):]
        try:
            verb, token = rest.split(":", 1)
        except ValueError:
            logger.warning("telegram approval callback malformed data: %r", data)
            return

        # Token-level dedupe: once a verb has been processed for a token,
        # subsequent clicks (even with new callback_query ids) must not
        # re-fire the callback. We pop the token from the registry on
        # first successful dispatch.
        token_meta = self._approval_tokens.pop(token, None)
        if token_meta is None:
            logger.info(
                "telegram approval click for unknown token=%s — stale callback ignored",
                token,
            )
            return

        if self._approval_callback is None:
            logger.warning(
                "telegram approval click for token=%s but no callback registered",
                token,
            )
            return

        try:
            await self._approval_callback(verb, token)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "telegram approval callback raised for verb=%s token=%s: %s",
                verb, token, exc,
            )
            # Re-register the token so a retry could resolve it; safer
            # than leaving the user staring at a half-broken UI.
            self._approval_tokens[token] = token_meta
            return

        # Best-effort UI confirmation: edit the original message to remove
        # the buttons and append the resolution. Failures here are
        # logged-only — the consent decision has already been routed.
        chat_id = token_meta.get("chat_id")
        message_id = token_meta.get("message_id")
        if chat_id is not None and message_id is not None:
            label = {
                "once": "✓ Allowed once",
                "always": "✓ Allowed always",
                "deny": "✗ Denied",
            }.get(verb, verb)
            try:
                await self._client.post(  # type: ignore[union-attr]
                    f"{self.base_url}/editMessageReplyMarkup",
                    json={
                        "chat_id": chat_id,
                        "message_id": int(message_id),
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
                await self._client.post(  # type: ignore[union-attr]
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"Decision recorded: {label}",
                        "reply_to_message_id": int(message_id),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "telegram approval UI confirmation failed (non-fatal): %s", exc,
                )

    async def _answer_callback_query(self, cbq_id: str) -> None:
        """Tell Telegram we received the callback so the spinner stops.

        Best-effort — failures are swallowed because the underlying
        consent flow doesn't depend on the ack.
        """
        if self._client is None:
            return
        try:
            await self._client.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": cbq_id},
            )
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
}


def _guess_mime(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


__all__ = ["TelegramAdapter", "_escape_mdv2", "_utf16_len", "_chunk_for_telegram"]
