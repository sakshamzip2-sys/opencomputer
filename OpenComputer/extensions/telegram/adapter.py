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
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult

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


# Telegram MarkdownV2 requires escaping these characters
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"
_MDV2_RE = re.compile(f"([{re.escape(_MDV2_SPECIAL)}])")


def _escape_mdv2(text: str) -> str:
    return _MDV2_RE.sub(r"\\\1", text)


def _utf16_len(s: str) -> int:
    """Telegram's message length limit is in UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


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
        self._offset: int = 0
        self._polling_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
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

    async def connect(self) -> bool:
        self._client = httpx.AsyncClient(timeout=35.0)
        # getMe to verify token and cache our bot id
        try:
            resp = await self._client.get(f"{self.base_url}/getMe")
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                logger.error("telegram getMe failed: %s", data)
                return False
            self._bot_id = data["result"]["id"]
            logger.info(
                "telegram: connected as @%s (id=%s)",
                data["result"].get("username", "?"),
                self._bot_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("telegram connect failed: %s", e)
            return False
        # start long-polling loop
        self._polling_task = asyncio.create_task(self._poll_forever())
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._polling_task is not None:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        if self._client is not None:
            await self._client.aclose()

    async def _poll_forever(self) -> None:
        assert self._client is not None
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
                if resp.status_code != 200:
                    await asyncio.sleep(2)
                    continue
                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(2)
                    continue
                for update in data.get("result", []):
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("telegram polling error: %s — sleeping 5s", e)
                await asyncio.sleep(5)

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

        # Skip messages with no text and no attachments — they're metadata-only updates
        # (e.g., chat-photo-changed, member-added) we don't surface to the agent.
        if not text and not attachments:
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

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        assert self._client is not None
        # Send as plain text for Phase 2 (no formatting) — easier to debug.
        # Phase 3 can add MarkdownV2 handling with escape detection.
        for chunk in _chunk_for_telegram(text, limit=self.max_message_length):
            try:
                resp = await self._client.post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk, "disable_notification": False},
                )
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
            await self._client.post(
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
            with p.open("rb") as fh:
                files = {field_name: (p.name, fh, _guess_mime(p))}
                form: dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    form["caption"] = caption[:1024]  # Telegram caption limit
                resp = await self._client.post(
                    f"{self.base_url}/{endpoint}",
                    data=form,
                    files=files,
                )
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
            resp = await self._client.post(
                f"{self.base_url}/setMessageReaction",
                json={
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                },
            )
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
            resp = await self._client.post(
                f"{self.base_url}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "text": text[: self.max_message_length],
                },
            )
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
            resp = await self._client.post(
                f"{self.base_url}/deleteMessage",
                json={"chat_id": chat_id, "message_id": int(message_id)},
            )
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
            resp = await self._client.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": prompt_text,
                    "reply_markup": {"inline_keyboard": keyboard},
                },
            )
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
