"""WhatsAppAdapter — WhatsApp channel via Meta's Cloud API (G.26 / Tier 4.x).

Outbound text + reactions via Graph API ``/v18.0/{phone_number_id}/messages``.
Mocks via ``httpx.MockTransport``.

**Inbound is NOT in this adapter.** Use the webhook adapter (G.3) wired to
a Meta Cloud API webhook. The Cloud API delivers inbound messages by POST
to a configured callback URL — the webhook adapter handles that contract.

Setup:

1. Create a Meta Business app at developers.facebook.com.
2. Add the "WhatsApp" product. Copy the temporary access token (or set up
   a System User token for production).
3. Note the Phone Number ID (NOT the phone number itself).
4. Set ``WHATSAPP_ACCESS_TOKEN`` and ``WHATSAPP_PHONE_NUMBER_ID``.

Capabilities: REACTIONS only. WhatsApp Cloud API does not currently support
edit/delete on outbound messages from a business account, so the adapter
declines those flags.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult

logger = logging.getLogger("opencomputer.ext.whatsapp")

_API_VERSION = "v18.0"
"""Meta pins the Graph API version per request. v18 is the current LTS."""


class WhatsAppAdapter(BaseChannelAdapter):
    """WhatsApp Cloud API channel — outbound + reactions."""

    platform = Platform.WEB
    max_message_length = 4096
    """WhatsApp text message limit (per Meta's docs)."""

    capabilities = ChannelCapabilities.REACTIONS

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._token: str = config["access_token"]
        self._phone_id: str = config["phone_number_id"]
        self._base_url = f"https://graph.facebook.com/{_API_VERSION}"
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )
        return self._client

    async def connect(self) -> None:
        # No connection check upfront — Cloud API is stateless. The first
        # send() will surface auth errors; we don't pre-flight to keep
        # startup latency low.
        return None

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ─── Outbound: text ─────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        text: str,
        **kwargs: Any,
    ) -> SendResult:
        """Send a text message to a WhatsApp recipient.

        ``chat_id`` is the recipient's WhatsApp phone number in E.164
        format (e.g. ``+919876543210``). The Cloud API uses the
        ``to`` field which expects the phone number; we strip the
        leading ``+`` since Meta accepts both forms but stores it
        without.
        """
        body = (text or "")[: self.max_message_length]
        if not body:
            return SendResult(success=False, error="empty message body")
        recipient = chat_id.lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": body},
        }
        url = f"{self._base_url}/{self._phone_id}/messages"
        try:
            resp = await self.client.post(url, json=payload)
        except Exception as e:  # noqa: BLE001
            return SendResult(success=False, error=f"http error: {e}")
        if resp.status_code >= 400:
            return SendResult(
                success=False, error=f"{resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        msg_id = (data.get("messages") or [{}])[0].get("id")
        return SendResult(success=True, message_id=msg_id)

    # ─── Outbound: reaction ─────────────────────────────────────────

    async def send_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
    ) -> SendResult:
        """React to a WhatsApp message. Empty emoji unsets the reaction.

        Cloud API spec: ``type=reaction`` payload with ``{message_id,
        emoji}``. Empty-string emoji removes the reaction (per Meta's
        docs). We surface a clear error for empty input here so callers
        don't accidentally clear reactions when meaning to add one.
        """
        if not emoji:
            return SendResult(success=False, error="empty emoji")
        recipient = chat_id.lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "reaction",
            "reaction": {"message_id": message_id, "emoji": emoji},
        }
        url = f"{self._base_url}/{self._phone_id}/messages"
        try:
            resp = await self.client.post(url, json=payload)
        except Exception as e:  # noqa: BLE001
            return SendResult(success=False, error=f"http error: {e}")
        if resp.status_code >= 400:
            return SendResult(
                success=False, error=f"{resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        msg_id = (data.get("messages") or [{}])[0].get("id")
        return SendResult(success=True, message_id=msg_id)
