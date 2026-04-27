"""Tests for the Home Assistant channel adapter (G.29 / Tier 4.x).

Service-call outbound via HA REST API. Mocks via ``httpx.MockTransport``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from plugin_sdk import ChannelCapabilities


def _load():
    spec = importlib.util.spec_from_file_location(
        "homeassistant_adapter_test_g29",
        Path(__file__).resolve().parent.parent / "extensions" / "homeassistant" / "adapter.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HomeAssistantAdapter, mod


@pytest.fixture
def adapter_with_mock():
    HomeAssistantAdapter, _ = _load()
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "POST" and "/api/services/" in req.url.path:
            # HA returns a list of state changes; we don't inspect it.
            return httpx.Response(200, json=[])
        if req.method == "GET" and req.url.path.endswith("/api/"):
            return httpx.Response(200, json={"message": "API running."})
        return httpx.Response(404, json={"message": "not found"})

    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "long-lived-token",
        }
    )
    a._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer long-lived-token",
            "Content-Type": "application/json",
        },
    )
    return a, requests


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_no_message_capabilities(self) -> None:
        HomeAssistantAdapter, _ = _load()
        # Service calls aren't messages — none of the chat-shape flags
        # apply.
        assert HomeAssistantAdapter.capabilities == ChannelCapabilities(0)


# ---------------------------------------------------------------------------
# Service call shapes
# ---------------------------------------------------------------------------


class TestServiceCalls:
    @pytest.mark.asyncio
    async def test_notify_call_packs_message_field(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send(
            "notify.mobile_app_pixel_8", "good morning saksham"
        )
        assert result.success
        assert result.message_id == "notify.mobile_app_pixel_8"
        # Find the POST.
        post = next(r for r in requests if r.method == "POST")
        assert post.url.path == "/api/services/notify/mobile_app_pixel_8"
        body = json.loads(post.read())
        assert body == {"message": "good morning saksham"}

    @pytest.mark.asyncio
    async def test_non_notify_service_with_explicit_data(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        # Calling light.turn_on with an explicit service_data dict
        result = await adapter.send(
            "light.turn_on",
            "ignored — non-notify domain",
            service_data={
                "entity_id": "light.living_room",
                "brightness": 200,
            },
        )
        assert result.success
        post = next(r for r in requests if r.method == "POST")
        assert post.url.path == "/api/services/light/turn_on"
        body = json.loads(post.read())
        assert body == {
            "entity_id": "light.living_room",
            "brightness": 200,
        }

    @pytest.mark.asyncio
    async def test_zero_arg_service_call_sends_empty_body(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        # script.run_morning_routine — no payload needed
        result = await adapter.send(
            "script.morning_routine", ""
        )
        assert result.success
        post = next(r for r in requests if r.method == "POST")
        assert post.url.path == "/api/services/script/morning_routine"
        assert json.loads(post.read()) == {}

    @pytest.mark.asyncio
    async def test_chat_id_without_dot_rejected(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send("not_a_service", "hello")
        assert not result.success
        assert "<domain>.<service>" in result.error
        # No HTTP request issued.
        assert all(r.method != "POST" for r in requests)

    @pytest.mark.asyncio
    async def test_notify_with_empty_message_rejected(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send("notify.any", "")
        assert not result.success
        assert "empty" in result.error.lower()
        assert all(r.method != "POST" for r in requests)

    @pytest.mark.asyncio
    async def test_notify_truncates_long_message(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        await adapter.send("notify.any", "x" * 10_000)
        post = next(r for r in requests if r.method == "POST")
        body = json.loads(post.read())
        assert len(body["message"]) == 4096

    @pytest.mark.asyncio
    async def test_service_data_must_be_dict(
        self, adapter_with_mock
    ) -> None:
        adapter, requests = adapter_with_mock
        result = await adapter.send(
            "light.turn_on", "x", service_data="not a dict"
        )
        assert not result.success
        assert "dict" in result.error.lower()
        assert all(r.method != "POST" for r in requests)

    @pytest.mark.asyncio
    async def test_http_error_returned(self) -> None:
        HomeAssistantAdapter, _ = _load()

        def fail_handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, json={"message": "Unauthorized"}
            )

        a = HomeAssistantAdapter(
            config={"url": "http://homeassistant.local:8123", "token": "bad"}
        )
        a._client = httpx.AsyncClient(
            transport=httpx.MockTransport(fail_handler),
            headers={
                "Authorization": "Bearer bad",
                "Content-Type": "application/json",
            },
        )
        result = await a.send("notify.x", "hi")
        assert not result.success
        assert "401" in result.error


# ─── Inbound (WebSocket) tests — 2026-04-28 follow-up ─────────────────


def _load_inbound_helpers():
    """Reload the module to access _format_state_change + class for inbound."""
    HomeAssistantAdapter, mod = _load()
    return HomeAssistantAdapter, mod._format_state_change


def test_inbound_disabled_by_default():
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
        }
    )
    assert a._inbound_enabled is False


def test_inbound_enabled_when_watch_all():
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
            "watch_all": True,
        }
    )
    assert a._inbound_enabled is True


def test_inbound_enabled_when_watch_domains_set():
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
            "watch_domains": ["binary_sensor"],
        }
    )
    assert a._inbound_enabled is True


def test_inbound_enabled_when_watch_entities_set():
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
            "watch_entities": ["light.front_door"],
        }
    )
    assert a._inbound_enabled is True


# Format helper — domain-specific shaping


def test_format_state_change_binary_sensor_triggered():
    _, fmt = _load_inbound_helpers()
    out = fmt(
        "binary_sensor.front_door",
        {"state": "off"},
        {"state": "on", "attributes": {"friendly_name": "Front Door"}},
    )
    assert out is not None
    assert "Front Door" in out
    assert "triggered" in out
    assert "cleared" in out


def test_format_state_change_climate():
    _, fmt = _load_inbound_helpers()
    out = fmt(
        "climate.living_room",
        {"state": "off"},
        {
            "state": "heat",
            "attributes": {
                "friendly_name": "Living Room",
                "current_temperature": 68,
                "temperature": 72,
            },
        },
    )
    assert out is not None
    assert "Living Room" in out
    assert "68" in out
    assert "72" in out


def test_format_state_change_sensor_with_unit():
    _, fmt = _load_inbound_helpers()
    out = fmt(
        "sensor.temp",
        {"state": "20"},
        {"state": "22", "attributes": {"unit_of_measurement": "°C"}},
    )
    assert out is not None
    assert "20°C" in out
    assert "22°C" in out


def test_format_state_change_skips_no_change():
    _, fmt = _load_inbound_helpers()
    out = fmt(
        "sensor.x",
        {"state": "5"},
        {"state": "5"},
    )
    assert out is None


def test_format_state_change_skips_no_new_state():
    _, fmt = _load_inbound_helpers()
    out = fmt("sensor.x", {"state": "5"}, None)
    assert out is None


def test_format_state_change_light_on():
    _, fmt = _load_inbound_helpers()
    out = fmt(
        "light.kitchen",
        {"state": "off"},
        {"state": "on", "attributes": {"friendly_name": "Kitchen"}},
    )
    assert "Kitchen" in out
    assert "turned on" in out


def test_format_state_change_generic_fallback():
    _, fmt = _load_inbound_helpers()
    out = fmt(
        "scene.morning",
        {"state": "scening"},
        {"state": "active"},
    )
    assert out is not None
    assert "scene.morning" in out


# Filter logic + cooldown — exercised via _handle_ha_event with mocked dispatch


@pytest.mark.asyncio
async def test_handle_ha_event_filters_drop_unwatched():
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
            "watch_domains": ["binary_sensor"],
        }
    )
    captured: list = []

    async def handler(event):  # noqa: ANN001
        captured.append(event)
        return None

    a.set_message_handler(handler)
    # light.x is NOT in watch_domains → dropped
    await a._handle_ha_event(
        {
            "data": {
                "entity_id": "light.kitchen",
                "old_state": {"state": "off"},
                "new_state": {"state": "on"},
            }
        }
    )
    assert captured == []


@pytest.mark.asyncio
async def test_handle_ha_event_filters_allow_watched_domain():
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
            "watch_domains": ["binary_sensor"],
            "cooldown_seconds": 0,
        }
    )
    captured: list = []

    async def handler(event):  # noqa: ANN001
        captured.append(event)
        return None

    a.set_message_handler(handler)
    await a._handle_ha_event(
        {
            "data": {
                "entity_id": "binary_sensor.front_door",
                "old_state": {"state": "off"},
                "new_state": {"state": "on"},
            }
        }
    )
    assert len(captured) == 1
    assert captured[0].text.startswith("[Home Assistant]")
    assert captured[0].metadata["entity_id"] == "binary_sensor.front_door"


@pytest.mark.asyncio
async def test_handle_ha_event_ignore_list_takes_precedence():
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
            "watch_all": True,
            "ignore_entities": ["sensor.spammy"],
            "cooldown_seconds": 0,
        }
    )
    captured: list = []

    async def handler(event):  # noqa: ANN001
        captured.append(event)
        return None

    a.set_message_handler(handler)
    await a._handle_ha_event(
        {
            "data": {
                "entity_id": "sensor.spammy",
                "old_state": {"state": "5"},
                "new_state": {"state": "6"},
            }
        }
    )
    assert captured == []


@pytest.mark.asyncio
async def test_handle_ha_event_cooldown_drops_repeat():
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
            "watch_all": True,
            "cooldown_seconds": 60,  # long cooldown
        }
    )
    captured: list = []

    async def handler(event):  # noqa: ANN001
        captured.append(event)
        return None

    a.set_message_handler(handler)
    payload = {
        "data": {
            "entity_id": "sensor.x",
            "old_state": {"state": "1"},
            "new_state": {"state": "2"},
        }
    }
    await a._handle_ha_event(payload)
    payload["data"]["old_state"] = {"state": "2"}
    payload["data"]["new_state"] = {"state": "3"}
    await a._handle_ha_event(payload)
    # First fired; second is within cooldown → dropped
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_handle_ha_event_closed_default_drops_when_no_filter():
    """Without watch_all/watch_domains/watch_entities, all events drop."""
    HomeAssistantAdapter, _ = _load_inbound_helpers()
    a = HomeAssistantAdapter(
        config={
            "url": "http://homeassistant.local:8123",
            "token": "tok",
            # No watch_* keys at all
        }
    )
    captured: list = []

    async def handler(event):  # noqa: ANN001
        captured.append(event)
        return None

    a.set_message_handler(handler)
    await a._handle_ha_event(
        {
            "data": {
                "entity_id": "binary_sensor.x",
                "old_state": {"state": "off"},
                "new_state": {"state": "on"},
            }
        }
    )
    assert captured == []
