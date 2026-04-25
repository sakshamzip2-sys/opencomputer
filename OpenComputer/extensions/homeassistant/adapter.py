"""HomeAssistantAdapter — Home Assistant channel via REST API (G.29 / Tier 4.x).

Outbound = service calls. The "send" verb is overloaded here vs other
channels: a Home Assistant message ISN'T a chat post, it's a service
invocation (``light.turn_on``, ``notify.send_message``, ``script.run``,
``automation.trigger``). The adapter packs an OC ``send(chat_id, text)``
call into the equivalent HA ``POST /api/services/<domain>/<service>``.

Mapping
-------

* ``chat_id`` is interpreted as ``<domain>.<service>`` (e.g.
  ``notify.mobile_app_pixel_8`` or ``light.turn_on``).
* ``text`` becomes the ``message`` field for ``notify.*`` services, or
  the value of whatever field the caller passes via ``service_data``
  in kwargs.
* For non-notify services, callers should pass the full payload via
  ``service_data=...`` kwarg.

**Inbound is NOT in this adapter.** The right pattern: configure a HA
automation that POSTs events to OC's webhook adapter (G.3). That keeps
the OC ↔ HA contract explicit and auditable.

Setup:

1. Profile → Long-lived access tokens → Create token.
2. Set ``HOMEASSISTANT_URL`` (e.g. ``http://homeassistant.local:8123``)
   and ``HOMEASSISTANT_TOKEN``.

Capabilities: none — service calls aren't messages, so the chat-shape
flags don't apply.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult

logger = logging.getLogger("opencomputer.ext.homeassistant")


class HomeAssistantAdapter(BaseChannelAdapter):
    """Home Assistant REST API channel — service-call outbound only."""

    platform = Platform.WEB
    max_message_length = 4096

    capabilities = ChannelCapabilities(0)
    """Service calls aren't chat messages — capability flags don't apply."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._base_url: str = config["url"].rstrip("/")
        self._token: str = config["token"]
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
        # Pre-flight against /api/ to surface bad URL / bad token early.
        try:
            resp = await self.client.get(f"{self._base_url}/api/")
        except Exception as e:  # noqa: BLE001
            logger.warning("homeassistant connect probe failed: %s", e)
            return None
        if resp.status_code == 401:
            logger.warning(
                "homeassistant: token rejected (HTTP 401) — set "
                "HOMEASSISTANT_TOKEN to a valid long-lived access token"
            )
        elif resp.status_code >= 400:
            logger.warning(
                "homeassistant connect probe HTTP %d", resp.status_code
            )
        return None

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ─── Outbound: service call ─────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        text: str,
        **kwargs: Any,
    ) -> SendResult:
        """Call a Home Assistant service.

        ``chat_id`` is parsed as ``<domain>.<service>`` (e.g.
        ``notify.mobile_app_pixel_8``). For ``notify.*`` services
        ``text`` is sent as the ``message`` field; for everything else
        callers must supply ``service_data`` via kwargs.
        """
        if "." not in chat_id:
            return SendResult(
                success=False,
                error=(
                    f"chat_id must be '<domain>.<service>' (e.g. "
                    f"'notify.mobile_app_pixel_8'); got {chat_id!r}"
                ),
            )
        domain, service = chat_id.split(".", 1)
        body: dict[str, Any]
        passed_data = kwargs.get("service_data")
        if passed_data is not None:
            if not isinstance(passed_data, dict):
                return SendResult(
                    success=False,
                    error="service_data must be a dict",
                )
            body = dict(passed_data)
        elif domain == "notify":
            text_truncated = (text or "")[: self.max_message_length]
            if not text_truncated:
                return SendResult(
                    success=False,
                    error="empty message body for notify.* service",
                )
            body = {"message": text_truncated}
        else:
            # Other domains may accept zero-arg invocations
            # (e.g. ``script.morning_routine``) — pass empty body.
            body = {}
        url = f"{self._base_url}/api/services/{domain}/{service}"
        try:
            resp = await self.client.post(url, json=body)
        except Exception as e:  # noqa: BLE001
            return SendResult(success=False, error=f"http error: {e}")
        if resp.status_code >= 400:
            return SendResult(
                success=False, error=f"{resp.status_code}: {resp.text[:200]}"
            )
        # HA returns the list of changed states — no scalar message_id
        # exists for service calls. Echo the chat_id so callers have a
        # stable reference for logging.
        return SendResult(success=True, message_id=chat_id)
