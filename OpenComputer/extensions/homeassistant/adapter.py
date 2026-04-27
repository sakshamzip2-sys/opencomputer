"""HomeAssistantAdapter — Home Assistant channel via REST + optional WebSocket events.

**Outbound = service calls.** The "send" verb is overloaded here vs
other channels: a Home Assistant message ISN'T a chat post, it's a
service invocation (``light.turn_on``, ``notify.send_message``,
``script.run``, ``automation.trigger``). The adapter packs an OC
``send(chat_id, text)`` call into the equivalent HA
``POST /api/services/<domain>/<service>``.

**Outbound mapping:**
- ``chat_id`` is interpreted as ``<domain>.<service>`` (e.g.
  ``notify.mobile_app_pixel_8`` or ``light.turn_on``).
- ``text`` becomes the ``message`` field for ``notify.*`` services, or
  the value of whatever field the caller passes via ``service_data``
  in kwargs.
- For non-notify services, callers should pass the full payload via
  ``service_data=...`` kwarg.

**Inbound = WebSocket state_changed events** (optional, gated on
filter env vars). 2026-04-28 follow-up that ports hermes' inbound
pattern. When ``HASS_WATCH_ALL=true`` or any of ``HASS_WATCH_DOMAINS`` /
``HASS_WATCH_ENTITIES`` is set, the adapter opens a WebSocket to
``/api/websocket``, authenticates with the token, subscribes to
``state_changed`` events, and dispatches readable text as
``MessageEvent`` objects (``chat_id="ha_events"``). Per-entity
cooldown defaults to 30s to prevent event floods.

When NO filter env var is set, only outbound mode runs (legacy behavior).

Setup:

1. Profile → Long-lived access tokens → Create token.
2. Set ``HOMEASSISTANT_URL`` (e.g. ``http://homeassistant.local:8123``)
   and ``HOMEASSISTANT_TOKEN``.
3. Optionally set ``HASS_WATCH_DOMAINS`` (CSV) /
   ``HASS_WATCH_ENTITIES`` (CSV) / ``HASS_IGNORE_ENTITIES`` (CSV) /
   ``HASS_WATCH_ALL=true`` to enable inbound events.

Capabilities: none — service calls aren't messages, so the chat-shape
flags don't apply. Inbound events are dispatched but the adapter has
no notion of "reactions" or "typing" on a state-change stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.homeassistant")

#: Reconnection backoff (seconds) for the inbound WebSocket. Capped so
#: a temporarily-down HA doesn't block forever; the listener resumes
#: as soon as HA comes back.
_BACKOFF_STEPS = (5, 10, 30, 60)


def _format_state_change(
    entity_id: str,
    old_state: dict[str, Any] | None,
    new_state: dict[str, Any] | None,
) -> str | None:
    """Convert a state_changed event into human-readable text.

    Returns None when the change is uninteresting (no new_state, or
    state didn't actually change). Domain-specific shaping for
    climate/sensor/binary_sensor/light/switch/fan/alarm; generic
    fallback for everything else.
    """
    if not new_state:
        return None
    old_val = (old_state or {}).get("state", "unknown")
    new_val = new_state.get("state", "unknown")
    if old_val == new_val:
        return None
    friendly_name = (
        new_state.get("attributes", {}).get("friendly_name") or entity_id
    )
    domain = entity_id.split(".")[0] if "." in entity_id else ""

    if domain == "climate":
        attrs = new_state.get("attributes", {})
        temp = attrs.get("current_temperature", "?")
        target = attrs.get("temperature", "?")
        return (
            f"[Home Assistant] {friendly_name}: HVAC {old_val!r} → "
            f"{new_val!r} (current: {temp}, target: {target})"
        )
    if domain == "sensor":
        unit = new_state.get("attributes", {}).get("unit_of_measurement", "")
        return (
            f"[Home Assistant] {friendly_name}: "
            f"{old_val}{unit} → {new_val}{unit}"
        )
    if domain == "binary_sensor":
        new_label = "triggered" if new_val == "on" else "cleared"
        old_label = "triggered" if old_val == "on" else "cleared"
        return f"[Home Assistant] {friendly_name}: {new_label} (was {old_label})"
    if domain in ("light", "switch", "fan"):
        return (
            f"[Home Assistant] {friendly_name}: turned "
            f"{'on' if new_val == 'on' else 'off'}"
        )
    if domain == "alarm_control_panel":
        return (
            f"[Home Assistant] {friendly_name}: alarm state "
            f"{old_val!r} → {new_val!r}"
        )
    return (
        f"[Home Assistant] {friendly_name} ({entity_id}): "
        f"{old_val!r} → {new_val!r}"
    )


class HomeAssistantAdapter(BaseChannelAdapter):
    """Home Assistant channel — service-call outbound + optional WS inbound."""

    platform = Platform.HOMEASSISTANT
    max_message_length = 4096

    capabilities = ChannelCapabilities(0)
    """Service calls aren't chat messages — capability flags don't apply."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._base_url: str = config["url"].rstrip("/")
        self._token: str = config["token"]
        self._client: httpx.AsyncClient | None = None
        # ── Inbound (WebSocket) — optional ────────────────────────
        self._watch_domains: set[str] = set(config.get("watch_domains") or [])
        self._watch_entities: set[str] = set(config.get("watch_entities") or [])
        self._ignore_entities: set[str] = set(config.get("ignore_entities") or [])
        self._watch_all: bool = bool(config.get("watch_all", False))
        self._cooldown_seconds: int = int(config.get("cooldown_seconds", 30))
        self._ws_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._listen_task: asyncio.Task[None] | None = None
        self._ws_msg_id: int = 0
        self._last_event_time: dict[str, float] = {}
        self._running_inbound: bool = False

    @property
    def _inbound_enabled(self) -> bool:
        return (
            self._watch_all
            or bool(self._watch_domains)
            or bool(self._watch_entities)
        )

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
        # Optional inbound: spin up WebSocket subscription if any filter
        # is configured. Default is OFF to preserve the legacy
        # outbound-only behaviour.
        if self._inbound_enabled:
            self._running_inbound = True
            self._listen_task = asyncio.create_task(self._listen_loop())
            logger.info(
                "homeassistant inbound: subscribing to state_changed "
                "(watch_all=%s, %d domains, %d entities)",
                self._watch_all,
                len(self._watch_domains),
                len(self._watch_entities),
            )
        return None

    async def disconnect(self) -> None:
        # Stop inbound first
        self._running_inbound = False
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        await self._cleanup_ws()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ─── Inbound: WebSocket state_changed subscription ─────────────

    def _next_ws_id(self) -> int:
        self._ws_msg_id += 1
        return self._ws_msg_id

    async def _ws_connect(self) -> bool:
        """Open WS, authenticate, subscribe to state_changed."""
        ws_url = self._base_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )
        ws_url = f"{ws_url}/api/websocket"
        self._ws_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._ws = await self._ws_session.ws_connect(
            ws_url, heartbeat=30, timeout=30,
        )
        # 1. auth_required
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_required":
            logger.error(
                "homeassistant WS: expected auth_required, got %r",
                msg.get("type"),
            )
            await self._cleanup_ws()
            return False
        # 2. send auth
        await self._ws.send_json(
            {"type": "auth", "access_token": self._token}
        )
        # 3. expect auth_ok
        msg = await self._ws.receive_json()
        if msg.get("type") != "auth_ok":
            logger.error("homeassistant WS auth failed: %r", msg)
            await self._cleanup_ws()
            return False
        # 4. subscribe to state_changed
        await self._ws.send_json(
            {
                "id": self._next_ws_id(),
                "type": "subscribe_events",
                "event_type": "state_changed",
            }
        )
        msg = await self._ws.receive_json()
        if not msg.get("success"):
            logger.error("homeassistant WS subscribe failed: %r", msg)
            await self._cleanup_ws()
            return False
        return True

    async def _cleanup_ws(self) -> None:
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None
        if self._ws_session is not None and not self._ws_session.closed:
            try:
                await self._ws_session.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws_session = None

    async def _listen_loop(self) -> None:
        """Background task: maintain WS, dispatch events, reconnect on drop."""
        backoff_idx = 0
        while self._running_inbound:
            try:
                ok = await self._ws_connect()
                if not ok:
                    raise RuntimeError("ws_connect returned False")
                await self._read_events()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("homeassistant WS error: %s", exc)
            if not self._running_inbound:
                return
            delay = _BACKOFF_STEPS[min(backoff_idx, len(_BACKOFF_STEPS) - 1)]
            logger.info("homeassistant WS reconnecting in %ds", delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            backoff_idx += 1
            await self._cleanup_ws()
            # On successful reconnect we'll come back through _ws_connect
            # at the top of the loop and reset backoff_idx if it works.
            backoff_idx = 0

    async def _read_events(self) -> None:
        if self._ws is None or self._ws.closed:
            return
        async for ws_msg in self._ws:
            if ws_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(ws_msg.data)
                    if data.get("type") == "event":
                        await self._handle_ha_event(data.get("event", {}))
                except json.JSONDecodeError:
                    logger.debug("non-JSON WS payload: %s", ws_msg.data[:200])
            elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def _handle_ha_event(self, event: dict[str, Any]) -> None:
        """Filter, format, and dispatch a single state_changed event."""
        event_data = event.get("data", {})
        entity_id: str = event_data.get("entity_id", "")
        if not entity_id:
            return
        if entity_id in self._ignore_entities:
            return

        domain = entity_id.split(".")[0] if "." in entity_id else ""
        # Closed-by-default filter: explicit watch_domains / watch_entities
        # OR watch_all=True must allow this event through.
        if self._watch_domains or self._watch_entities:
            domain_match = (
                domain in self._watch_domains if self._watch_domains else False
            )
            entity_match = (
                entity_id in self._watch_entities if self._watch_entities else False
            )
            if not domain_match and not entity_match:
                return
        elif not self._watch_all:
            return

        # Per-entity cooldown — prevents event floods on chatty sensors.
        now = time.time()
        last = self._last_event_time.get(entity_id, 0.0)
        if (now - last) < self._cooldown_seconds:
            return
        self._last_event_time[entity_id] = now

        text = _format_state_change(
            entity_id,
            event_data.get("old_state"),
            event_data.get("new_state"),
        )
        if not text:
            return

        msg_event = MessageEvent(
            platform=Platform.HOMEASSISTANT,
            chat_id="ha_events",
            user_id="homeassistant",
            text=text,
            timestamp=now,
            metadata={"entity_id": entity_id, "domain": domain},
        )
        await self.handle_message(msg_event)

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
