"""SignalAdapter — Signal channel via signal-cli REST API (G.27 / Tier 4.x).

Outbound text + reactions via the JSON-RPC HTTP endpoints exposed by
signal-cli's daemon mode (``signal-cli daemon --http``). Mocks via
``httpx.MockTransport``.

**Inbound is NOT in this adapter.** signal-cli's daemon exposes a
``/receive`` endpoint that delivers inbound messages — wire that to OC's
webhook adapter (G.3) for the inbound contract, or run a polling client
inside a custom plugin.

Setup:

1. Install signal-cli: ``brew install signal-cli`` (Mac) or build from
   source per AsamK's GitHub repo.
2. Register a phone number: ``signal-cli -a <phone> register`` and follow
   the SMS verification prompts.
3. Run the daemon: ``signal-cli -a <phone> daemon --http localhost:8080``.
4. Set ``SIGNAL_CLI_URL`` (e.g. ``http://localhost:8080``) and
   ``SIGNAL_PHONE_NUMBER`` (e.g. ``+15551234567``).

Capabilities: REACTIONS only. signal-cli supports edit + delete via newer
JSON-RPC methods, but they are inconsistently available across signal-cli
versions; deferred to a follow-up that gates on the version check.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.channel_helpers import redact_phone
from plugin_sdk.core import Platform, SendResult

logger = logging.getLogger("opencomputer.ext.signal")


class SignalAdapter(BaseChannelAdapter):
    """Signal channel — signal-cli JSON-RPC HTTP wrapper. Text + reactions."""

    platform = Platform.SIGNAL
    max_message_length = 4096
    """Signal protocol's per-message text limit. signal-cli will fragment
    longer payloads into multi-part messages, but we keep the API surface
    predictable by truncating before send."""

    capabilities = ChannelCapabilities.REACTIONS

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._base_url: str = config["signal_cli_url"].rstrip("/")
        self._phone: str = config["phone_number"]
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Content-Type": "application/json"},
                timeout=15.0,
            )
        return self._client

    async def connect(self) -> None:
        # No upfront check — signal-cli's first /send will surface auth /
        # connectivity errors. We don't pre-flight to keep startup fast.
        logger.info(
            "signal: connected (account=%s, base_url=%s)",
            redact_phone(self._phone),
            self._base_url,
        )
        return None

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        logger.info("signal: disconnected (account=%s)", redact_phone(self._phone))

    # ─── Outbound: text ─────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        text: str,
        **kwargs: Any,
    ) -> SendResult:
        """Send a text message via signal-cli JSON-RPC.

        ``chat_id`` is either an E.164 phone number (``+15551234567``) or
        a Signal group id. signal-cli accepts both via the ``recipient``
        field; group ids start with ``group.``.
        """
        body = (text or "")[: self.max_message_length]
        if not body:
            return SendResult(success=False, error="empty message body")
        payload = {
            "jsonrpc": "2.0",
            "id": "send",
            "method": "send",
            "params": {
                "account": self._phone,
                "recipient": [chat_id],
                "message": body,
            },
        }
        try:
            resp = await self.client.post(
                f"{self._base_url}/api/v1/rpc", json=payload
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "signal send: http error to %s: %s", redact_phone(chat_id), e
            )
            return SendResult(success=False, error=f"http error: {e}")
        if resp.status_code >= 400:
            logger.warning(
                "signal send: HTTP %d to %s",
                resp.status_code,
                redact_phone(chat_id),
            )
            return SendResult(
                success=False, error=f"{resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        if "error" in data:
            err = data["error"]
            logger.warning(
                "signal send: signal-cli error to %s: %s",
                redact_phone(chat_id),
                err.get("message", err),
            )
            return SendResult(
                success=False, error=f"signal-cli error: {err.get('message', err)}"
            )
        result = data.get("result", {})
        # signal-cli returns a timestamp that doubles as the message id
        # for reactions / edits.
        msg_id = str(result.get("timestamp", "")) or None
        logger.info("signal send ok: %s msg_id=%s", redact_phone(chat_id), msg_id)
        return SendResult(success=True, message_id=msg_id)

    # ─── Outbound: reaction ─────────────────────────────────────────

    async def send_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
    ) -> SendResult:
        """React to a Signal message via signal-cli's ``sendReaction`` RPC.

        ``message_id`` is the timestamp of the target message (same value
        we returned from ``send``). ``targetAuthor`` is the original
        sender — for reactions to messages we sent ourselves, that's the
        adapter's own phone number.
        """
        if not emoji:
            return SendResult(success=False, error="empty emoji")
        try:
            ts = int(message_id)
        except (TypeError, ValueError):
            return SendResult(
                success=False, error=f"invalid message_id (must be timestamp): {message_id!r}"
            )
        payload = {
            "jsonrpc": "2.0",
            "id": "sendReaction",
            "method": "sendReaction",
            "params": {
                "account": self._phone,
                "recipient": [chat_id],
                "emoji": emoji,
                "targetAuthor": self._phone,
                "targetTimestamp": ts,
            },
        }
        try:
            resp = await self.client.post(
                f"{self._base_url}/api/v1/rpc", json=payload
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "signal reaction: http error to %s: %s", redact_phone(chat_id), e
            )
            return SendResult(success=False, error=f"http error: {e}")
        if resp.status_code >= 400:
            logger.warning(
                "signal reaction: HTTP %d to %s",
                resp.status_code,
                redact_phone(chat_id),
            )
            return SendResult(
                success=False, error=f"{resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        if "error" in data:
            err = data["error"]
            logger.warning(
                "signal reaction: signal-cli error to %s: %s",
                redact_phone(chat_id),
                err.get("message", err),
            )
            return SendResult(
                success=False, error=f"signal-cli error: {err.get('message', err)}"
            )
        result = data.get("result", {})
        msg_id = str(result.get("timestamp", "")) or None
        return SendResult(success=True, message_id=msg_id)
