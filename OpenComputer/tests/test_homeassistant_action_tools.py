"""Tests for the Hermes-parity Home Assistant action tools (2026-05-01).

Mocks ``httpx.AsyncClient`` so no network is touched. Covers:

* Capability claims — read tier 1, call_service tier 2.
* Entity-id regex / service regex — security guardrails.
* Blocked-domain frozenset — shell_command etc. rejected.
* JSON ``data`` parameter parsing — string + invalid string + dict.
* Filter logic — domain + area filters in ``_filter_states``.
* All 4 tool happy paths.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from extensions.homeassistant.action_tools import (
    _BLOCKED_DOMAINS,
    _ENTITY_ID_RE,
    _SERVICE_NAME_RE,
    ALL_TOOLS,
    HACallServiceTool,
    HAGetStateTool,
    HAListEntitiesTool,
    HAListServicesTool,
    _filter_states,
)
from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall


@pytest.fixture(autouse=True)
def _set_hass_env(monkeypatch):
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://hass.local:8123")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "fake-token")
    yield


# ─── Capability claims ──────────────────────────────────────────


def test_all_tools_count():
    assert len(ALL_TOOLS) == 4


def test_read_tools_have_implicit_tier():
    for cls in (HAListEntitiesTool, HAGetStateTool, HAListServicesTool):
        claims = cls.capability_claims
        assert len(claims) == 1
        assert claims[0].capability_id == "homeassistant.read"
        assert claims[0].tier_required == ConsentTier.IMPLICIT


def test_call_service_tool_explicit_tier():
    claims = HACallServiceTool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "homeassistant.call_service"
    assert claims[0].tier_required == ConsentTier.EXPLICIT


# ─── Regex guardrails ───────────────────────────────────────────


def test_entity_id_regex_accepts_valid():
    for ok in ("light.living_room", "sensor.temp_1", "binary_sensor.front_door"):
        assert _ENTITY_ID_RE.match(ok), ok


def test_entity_id_regex_rejects_path_traversal():
    for bad in (
        "light/.../etc/passwd",
        "../../api/config",
        "light.../",
        "Light.Living_Room",  # uppercase rejected
        "1light.test",        # leading digit rejected (domain part)
        "light",              # no dot
    ):
        assert not _ENTITY_ID_RE.match(bad), f"should reject {bad!r}"


def test_service_name_regex_rejects_separators():
    for bad in ("shell_command/../light", "../config", "Domain", "shell.command"):
        assert not _SERVICE_NAME_RE.match(bad), f"should reject {bad!r}"


def test_service_name_regex_accepts_valid():
    for ok in ("light", "turn_on", "set_temperature", "hvac_mode"):
        assert _SERVICE_NAME_RE.match(ok), ok


def test_blocked_domains_includes_shell():
    assert "shell_command" in _BLOCKED_DOMAINS
    assert "python_script" in _BLOCKED_DOMAINS
    assert "rest_command" in _BLOCKED_DOMAINS
    assert "hassio" in _BLOCKED_DOMAINS


# ─── Filter logic ───────────────────────────────────────────────


def test_filter_states_by_domain():
    states = [
        {"entity_id": "light.a", "state": "on", "attributes": {"friendly_name": "A"}},
        {"entity_id": "switch.b", "state": "off", "attributes": {"friendly_name": "B"}},
    ]
    out = _filter_states(states, domain="light", area=None)
    assert out["count"] == 1
    assert out["entities"][0]["entity_id"] == "light.a"


def test_filter_states_by_area_friendly_name():
    states = [
        {"entity_id": "light.a", "state": "on",
         "attributes": {"friendly_name": "Kitchen Light"}},
        {"entity_id": "switch.b", "state": "off",
         "attributes": {"friendly_name": "Bedroom Switch"}},
    ]
    out = _filter_states(states, domain=None, area="kitchen")
    assert out["count"] == 1
    assert out["entities"][0]["entity_id"] == "light.a"


def test_filter_states_no_filter_returns_all():
    states = [{"entity_id": "x.y", "state": "on", "attributes": {}}] * 3
    out = _filter_states(states, domain=None, area=None)
    assert out["count"] == 3


# ─── Mocking helpers ────────────────────────────────────────────


def _make_async_client_mock(json_returns: list[Any]):
    """Build a mock for httpx.AsyncClient that returns successive responses on get/post."""
    responses = []
    for payload in json_returns:
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        responses.append(resp)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock()
    cm.__aexit__ = AsyncMock(return_value=None)
    client = AsyncMock()
    cm.__aenter__.return_value = client
    client.get = AsyncMock(side_effect=responses)
    client.post = AsyncMock(side_effect=responses)

    factory = MagicMock(return_value=cm)
    return factory, client


# ─── List entities ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_entities_happy_path():
    states = [
        {"entity_id": "light.a", "state": "on", "attributes": {"friendly_name": "A"}},
        {"entity_id": "switch.b", "state": "off", "attributes": {"friendly_name": "B"}},
    ]
    factory, _ = _make_async_client_mock([states])
    with patch("extensions.homeassistant.action_tools.httpx.AsyncClient", factory):
        tool = HAListEntitiesTool()
        result = await tool.execute(ToolCall(
            id="t1", name="ha_list_entities", arguments={"domain": "light"},
        ))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["result"]["count"] == 1
    assert payload["result"]["entities"][0]["entity_id"] == "light.a"


@pytest.mark.asyncio
async def test_list_entities_http_error():
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock()
    cm.__aexit__ = AsyncMock(return_value=None)
    client = AsyncMock()
    cm.__aenter__.return_value = client
    client.get = AsyncMock(side_effect=httpx.HTTPError("connection refused"))
    with patch("extensions.homeassistant.action_tools.httpx.AsyncClient",
               return_value=cm):
        tool = HAListEntitiesTool()
        result = await tool.execute(ToolCall(
            id="t1", name="ha_list_entities", arguments={},
        ))
    assert result.is_error
    payload = json.loads(result.content)
    assert "Failed to list entities" in payload["error"]


# ─── Get state ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_state_happy_path():
    state = {
        "entity_id": "light.living_room",
        "state": "on",
        "attributes": {"brightness": 200, "friendly_name": "Living Room"},
        "last_changed": "2026-01-01T00:00:00Z",
        "last_updated": "2026-01-01T00:00:00Z",
    }
    factory, _ = _make_async_client_mock([state])
    with patch("extensions.homeassistant.action_tools.httpx.AsyncClient", factory):
        tool = HAGetStateTool()
        result = await tool.execute(ToolCall(
            id="t1", name="ha_get_state",
            arguments={"entity_id": "light.living_room"},
        ))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["result"]["state"] == "on"
    assert payload["result"]["attributes"]["brightness"] == 200


@pytest.mark.asyncio
async def test_get_state_missing_entity_id():
    tool = HAGetStateTool()
    result = await tool.execute(ToolCall(
        id="t1", name="ha_get_state", arguments={},
    ))
    assert result.is_error
    assert "Missing required parameter" in json.loads(result.content)["error"]


@pytest.mark.asyncio
async def test_get_state_invalid_entity_id_rejected():
    tool = HAGetStateTool()
    result = await tool.execute(ToolCall(
        id="t1", name="ha_get_state",
        arguments={"entity_id": "../../api/config"},
    ))
    assert result.is_error
    assert "Invalid entity_id" in json.loads(result.content)["error"]


# ─── List services ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_services_filters_by_domain():
    services = [
        {"domain": "light", "services": {
            "turn_on": {"description": "Turn on", "fields": {
                "brightness": {"description": "0-255"},
            }},
        }},
        {"domain": "switch", "services": {
            "turn_off": {"description": "Off"},
        }},
    ]
    factory, _ = _make_async_client_mock([services])
    with patch("extensions.homeassistant.action_tools.httpx.AsyncClient", factory):
        tool = HAListServicesTool()
        result = await tool.execute(ToolCall(
            id="t1", name="ha_list_services", arguments={"domain": "light"},
        ))
    assert not result.is_error
    payload = json.loads(result.content)
    domains = payload["result"]["domains"]
    assert len(domains) == 1
    assert domains[0]["domain"] == "light"
    assert "brightness" in domains[0]["services"]["turn_on"]["fields"]


# ─── Call service — security ────────────────────────────────────


@pytest.mark.asyncio
async def test_call_service_blocks_shell_command():
    tool = HACallServiceTool()
    result = await tool.execute(ToolCall(
        id="t1", name="ha_call_service",
        arguments={"domain": "shell_command", "service": "do_thing"},
    ))
    assert result.is_error
    payload = json.loads(result.content)
    assert "blocked for security" in payload["error"]


@pytest.mark.asyncio
async def test_call_service_blocks_python_script():
    tool = HACallServiceTool()
    result = await tool.execute(ToolCall(
        id="t1", name="ha_call_service",
        arguments={"domain": "python_script", "service": "run"},
    ))
    assert result.is_error


@pytest.mark.asyncio
async def test_call_service_rejects_path_traversal_in_domain():
    tool = HACallServiceTool()
    result = await tool.execute(ToolCall(
        id="t1", name="ha_call_service",
        arguments={"domain": "shell_command/../light", "service": "turn_on"},
    ))
    assert result.is_error
    assert "Invalid domain format" in json.loads(result.content)["error"]


@pytest.mark.asyncio
async def test_call_service_rejects_invalid_entity_id():
    tool = HACallServiceTool()
    result = await tool.execute(ToolCall(
        id="t1", name="ha_call_service",
        arguments={
            "domain": "light", "service": "turn_on",
            "entity_id": "../../etc/passwd",
        },
    ))
    assert result.is_error
    assert "Invalid entity_id" in json.loads(result.content)["error"]


@pytest.mark.asyncio
async def test_call_service_missing_required():
    tool = HACallServiceTool()
    result = await tool.execute(ToolCall(
        id="t1", name="ha_call_service", arguments={"domain": "light"},
    ))
    assert result.is_error


@pytest.mark.asyncio
async def test_call_service_invalid_data_json():
    tool = HACallServiceTool()
    result = await tool.execute(ToolCall(
        id="t1", name="ha_call_service",
        arguments={
            "domain": "light", "service": "turn_on",
            "entity_id": "light.a", "data": "not-json",
        },
    ))
    assert result.is_error
    assert "Invalid JSON" in json.loads(result.content)["error"]


# ─── Call service — happy path ──────────────────────────────────


@pytest.mark.asyncio
async def test_call_service_happy_path():
    affected = [{"entity_id": "light.living_room", "state": "on"}]
    factory, client = _make_async_client_mock([affected])
    with patch("extensions.homeassistant.action_tools.httpx.AsyncClient", factory):
        tool = HACallServiceTool()
        result = await tool.execute(ToolCall(
            id="t1", name="ha_call_service",
            arguments={
                "domain": "light", "service": "turn_on",
                "entity_id": "light.living_room",
                "data": '{"brightness": 200}',
            },
        ))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["result"]["service"] == "light.turn_on"
    assert payload["result"]["affected_entities"][0]["entity_id"] == "light.living_room"

    # Verify the JSON body included both the parsed data + entity_id
    sent = client.post.call_args
    sent_json = sent.kwargs["json"]
    assert sent_json["brightness"] == 200
    assert sent_json["entity_id"] == "light.living_room"


@pytest.mark.asyncio
async def test_call_service_data_dict_passthrough():
    """A dict ``data`` (not a JSON string) should pass through unchanged."""
    factory, client = _make_async_client_mock([[]])
    with patch("extensions.homeassistant.action_tools.httpx.AsyncClient", factory):
        tool = HACallServiceTool()
        await tool.execute(ToolCall(
            id="t1", name="ha_call_service",
            arguments={
                "domain": "climate", "service": "set_temperature",
                "entity_id": "climate.thermostat",
                "data": {"temperature": 22, "hvac_mode": "heat"},
            },
        ))
    sent_json = client.post.call_args.kwargs["json"]
    assert sent_json["temperature"] == 22
    assert sent_json["hvac_mode"] == "heat"


# ─── Schema sanity ──────────────────────────────────────────────


def test_schemas_have_correct_names_and_required_fields():
    expected_required = {
        "ha_list_entities": [],
        "ha_get_state": ["entity_id"],
        "ha_list_services": [],
        "ha_call_service": ["domain", "service"],
    }
    for cls in ALL_TOOLS:
        tool = cls()
        schema = tool.schema
        assert schema.name in expected_required
        assert schema.parameters["required"] == expected_required[schema.name]
