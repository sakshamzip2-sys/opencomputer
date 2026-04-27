"""MatrixAdapter — Matrix channel via the Client-Server API (G.19 / Tier 3.x).

Outbound + reactions + edit (m.replace) + redaction (delete) via raw httpx
calls to the Matrix Client-Server API ``/_matrix/client/v3/...``.
**No end-to-end encryption** — works only in unencrypted rooms. Adding E2E
support would require ``matrix-nio`` + olm/megolm libs, deferred until
demand.

Inbound: not in this adapter. Use the webhook adapter (G.3) wired to a
Matrix bridge, hookshot, or appservice that POSTs message events to OC.

Setup:

1. Get an access token from your homeserver (e.g. via Element → Help &
   About → Advanced → Access Token, or via ``POST /_matrix/client/v3/login``).
2. Set ``MATRIX_HOMESERVER`` (e.g. ``https://matrix.org``) and
   ``MATRIX_ACCESS_TOKEN``. Disabled by default.

Capabilities: REACTIONS + EDIT_MESSAGE + DELETE_MESSAGE + THREADS.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult

logger = logging.getLogger("opencomputer.ext.matrix")


class MatrixAdapter(BaseChannelAdapter):
    """Matrix channel — Client-Server API. Unencrypted rooms only."""

    platform = Platform.MATRIX
    max_message_length = 60_000
    capabilities = (
        ChannelCapabilities.REACTIONS
        | ChannelCapabilities.EDIT_MESSAGE
        | ChannelCapabilities.DELETE_MESSAGE
        | ChannelCapabilities.THREADS
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._homeserver = str(config["homeserver"]).rstrip("/")
        self._access_token = str(config["access_token"])
        self._client: httpx.AsyncClient | None = None
        self._user_id: str | None = None

    async def connect(self) -> bool:
        """Verify the access token via ``GET /_matrix/client/v3/account/whoami``."""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            resp = await self._client.get(
                f"{self._homeserver}/_matrix/client/v3/account/whoami"
            )
            if resp.status_code != 200:
                logger.error(
                    "matrix whoami HTTP %s: %s",
                    resp.status_code, resp.text[:200],
                )
                return False
            data = resp.json()
            self._user_id = data.get("user_id")
            logger.info("matrix: connected as %s", self._user_id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("matrix connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Outbound — m.room.message
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send an ``m.text`` message to the given room id.

        ``chat_id`` is a room ID like ``!roomid:server.example`` or a room
        alias ``#alias:server.example``. Aliases are NOT auto-resolved;
        call your homeserver's ``/directory/room/{alias}`` first if you only
        have an alias.

        ``kwargs`` may include ``thread_root`` (an event id) to thread under.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        txn_id = uuid.uuid4().hex[:16]
        body = text[: self.max_message_length]
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if kwargs.get("thread_root"):
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": kwargs["thread_root"],
            }
        try:
            resp = await self._client.put(
                f"{self._homeserver}/_matrix/client/v3/rooms/{quote(chat_id)}/send/m.room.message/{txn_id}",
                json=content,
            )
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"matrix HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            return SendResult(success=True, message_id=str(data.get("event_id") or ""))
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Reactions — send an m.reaction event referencing the target event
    # ------------------------------------------------------------------

    async def send_reaction(
        self, chat_id: str, message_id: str, emoji: str, **kwargs: Any
    ) -> SendResult:
        """Add a reaction. Matrix uses unicode emoji directly (no name mapping)."""
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        if not emoji:
            return SendResult(success=False, error="emoji must be non-empty")
        txn_id = uuid.uuid4().hex[:16]
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": message_id,
                "key": emoji,
            }
        }
        try:
            resp = await self._client.put(
                f"{self._homeserver}/_matrix/client/v3/rooms/{quote(chat_id)}/send/m.reaction/{txn_id}",
                json=content,
            )
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"matrix HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Edit — Matrix represents edits via m.replace events
    # ------------------------------------------------------------------

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        """Edit via an ``m.replace`` event referencing ``message_id``.

        Matrix clients render edits by combining the original event with
        the latest m.replace; servers don't change the original event.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        txn_id = uuid.uuid4().hex[:16]
        body = text[: self.max_message_length]
        content = {
            "msgtype": "m.text",
            "body": f"* {body}",  # convention: "* " prefix in fallback body
            "m.new_content": {"msgtype": "m.text", "body": body},
            "m.relates_to": {"rel_type": "m.replace", "event_id": message_id},
        }
        try:
            resp = await self._client.put(
                f"{self._homeserver}/_matrix/client/v3/rooms/{quote(chat_id)}/send/m.room.message/{txn_id}",
                json=content,
            )
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"matrix HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            return SendResult(success=True, message_id=str(data.get("event_id") or ""))
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Delete — Matrix uses redactions
    # ------------------------------------------------------------------

    async def delete_message(
        self, chat_id: str, message_id: str, **kwargs: Any
    ) -> SendResult:
        """Redact (delete) via ``PUT /_matrix/client/v3/rooms/.../redact/...``.

        ``kwargs`` may include ``reason`` (string) which is recorded with
        the redaction.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        txn_id = uuid.uuid4().hex[:16]
        content: dict[str, Any] = {}
        if kwargs.get("reason"):
            content["reason"] = str(kwargs["reason"])
        try:
            resp = await self._client.put(
                f"{self._homeserver}/_matrix/client/v3/rooms/{quote(chat_id)}/redact/{quote(message_id)}/{txn_id}",
                json=content,
            )
            if resp.status_code != 200:
                return SendResult(
                    success=False,
                    error=f"matrix HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return SendResult(success=True)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")


__all__ = ["MatrixAdapter"]
