"""Tests for the Hermes-parity discord_server tool (2026-05-01).

Mocks ``_discord_request`` so no network is touched. Covers:

* Schema construction — action manifest renders, intents filter members tools.
* Allowlist gate — config restricts the visible set.
* 403 enrichment — actionable hint per action.
* Token absence → tool errors out cleanly.
* Action dispatch — list_guilds, fetch_messages, member_info, role mutations.
* Required-param validation — missing args return is_error before any HTTP.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from extensions.discord.server_tool import (
    _ACTIONS,
    _READ_ACTIONS,
    _WRITE_ACTIONS,
    DiscordAPIError,
    DiscordServerTool,
    _available_actions,
    _build_description,
    _build_parameters,
    _enrich_403,
    _reset_capability_cache,
)
from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall


@pytest.fixture(autouse=True)
def _clean_caps_cache():
    _reset_capability_cache()
    yield
    _reset_capability_cache()


# ─── Capability claim shape ─────────────────────────────────────


def test_capability_claims_read_and_manage():
    claims = DiscordServerTool.capability_claims
    ids = {c.capability_id for c in claims}
    assert ids == {"discord.read", "discord.manage"}

    by_id = {c.capability_id: c for c in claims}
    assert by_id["discord.read"].tier_required == ConsentTier.IMPLICIT
    assert by_id["discord.manage"].tier_required == ConsentTier.EXPLICIT


# ─── Schema construction ────────────────────────────────────────


def test_action_partition_read_vs_write():
    assert _READ_ACTIONS.isdisjoint(_WRITE_ACTIONS)
    assert set(_ACTIONS) == _READ_ACTIONS | _WRITE_ACTIONS


def test_available_actions_with_full_intents():
    caps = {"has_members_intent": True, "has_message_content": True, "detected": True}
    actions = _available_actions(caps, allowlist=None)
    assert set(actions) == set(_ACTIONS)


def test_available_actions_without_members_intent():
    caps = {"has_members_intent": False, "has_message_content": True, "detected": True}
    actions = _available_actions(caps, allowlist=None)
    assert "member_info" not in actions
    assert "search_members" not in actions
    # everything else still there
    assert "list_guilds" in actions
    assert "fetch_messages" in actions


def test_available_actions_allowlist_restricts():
    caps = {"has_members_intent": True, "has_message_content": True, "detected": True}
    actions = _available_actions(caps, allowlist=["list_guilds", "server_info"])
    assert actions == ["list_guilds", "server_info"]


def test_available_actions_allowlist_and_intent_combine():
    caps = {"has_members_intent": False, "has_message_content": True, "detected": True}
    actions = _available_actions(
        caps, allowlist=["list_guilds", "search_members", "server_info"],
    )
    # search_members blocked by missing intent
    assert "search_members" not in actions
    assert set(actions) == {"list_guilds", "server_info"}


def test_description_includes_manifest_lines():
    actions = ["list_guilds", "server_info"]
    caps = {"has_message_content": True, "detected": True}
    desc = _build_description(actions, caps)
    assert "list_guilds()" in desc
    assert "server_info(guild_id)" in desc
    # gated action not present
    assert "search_members" not in desc


def test_description_warns_about_missing_message_content():
    actions = ["fetch_messages"]
    caps = {"has_message_content": False, "detected": True}
    desc = _build_description(actions, caps)
    assert "MESSAGE_CONTENT" in desc


def test_parameters_required_action_only():
    params = _build_parameters(list(_ACTIONS))
    assert params["type"] == "object"
    assert params["required"] == ["action"]
    assert "guild_id" in params["properties"]
    assert "auto_archive_duration" in params["properties"]


def test_parameters_action_enum_matches_actions():
    actions = ["list_guilds", "server_info"]
    params = _build_parameters(actions)
    assert params["properties"]["action"]["enum"] == actions


def test_schema_no_token_falls_back_to_full_action_set(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    tool = DiscordServerTool()
    schema = tool.schema
    assert schema.name == "discord_server"
    assert set(schema.parameters["properties"]["action"]["enum"]) == set(_ACTIONS)


# ─── 403 enrichment ─────────────────────────────────────────────


def test_403_hint_for_known_action():
    msg = _enrich_403("add_role", "missing_permissions")
    assert "MANAGE_ROLES" in msg
    assert "missing_permissions" in msg


def test_403_hint_for_unknown_action_falls_back_to_raw():
    msg = _enrich_403("nonexistent_action", "raw body")
    assert "Discord API 403" in msg
    assert "raw body" in msg


# ─── execute() error paths ──────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_no_token(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    tool = DiscordServerTool()
    result = await tool.execute(ToolCall(
        id="t1", name="discord_server", arguments={"action": "list_guilds"},
    ))
    assert result.is_error
    payload = json.loads(result.content)
    assert "DISCORD_BOT_TOKEN" in payload["error"]


@pytest.mark.asyncio
async def test_execute_unknown_action(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    tool = DiscordServerTool()
    result = await tool.execute(ToolCall(
        id="t1", name="discord_server", arguments={"action": "delete_universe"},
    ))
    assert result.is_error
    payload = json.loads(result.content)
    assert "Unknown action" in payload["error"]
    assert "available_actions" in payload


@pytest.mark.asyncio
async def test_execute_missing_required_params(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            tool = DiscordServerTool()
            result = await tool.execute(ToolCall(
                id="t1", name="discord_server",
                arguments={"action": "add_role", "guild_id": "g"},  # missing user_id, role_id
            ))
    assert result.is_error
    payload = json.loads(result.content)
    assert "user_id" in payload["error"]
    assert "role_id" in payload["error"]


@pytest.mark.asyncio
async def test_execute_action_blocked_by_intent(monkeypatch):
    """search_members called when bot lacks GUILD_MEMBERS intent."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": False, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            tool = DiscordServerTool()
            result = await tool.execute(ToolCall(
                id="t1", name="discord_server",
                arguments={"action": "search_members", "guild_id": "g", "query": "alice"},
            ))
    assert result.is_error
    payload = json.loads(result.content)
    assert "not available" in payload["error"]


@pytest.mark.asyncio
async def test_execute_action_blocked_by_allowlist(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=["list_guilds"]):
            tool = DiscordServerTool()
            result = await tool.execute(ToolCall(
                id="t1", name="discord_server",
                arguments={"action": "add_role", "guild_id": "g", "user_id": "u", "role_id": "r"},
            ))
    assert result.is_error


@pytest.mark.asyncio
async def test_execute_404_returns_raw_error(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            with patch("extensions.discord.server_tool._discord_request",
                       side_effect=DiscordAPIError(404, '{"message":"Unknown Guild"}')):
                tool = DiscordServerTool()
                result = await tool.execute(ToolCall(
                    id="t1", name="discord_server",
                    arguments={"action": "server_info", "guild_id": "missing"},
                ))
    assert result.is_error
    payload = json.loads(result.content)
    assert "404" in payload["error"]


@pytest.mark.asyncio
async def test_execute_403_uses_enrichment(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            with patch("extensions.discord.server_tool._discord_request",
                       side_effect=DiscordAPIError(403, "missing perms")):
                tool = DiscordServerTool()
                result = await tool.execute(ToolCall(
                    id="t1", name="discord_server",
                    arguments={"action": "add_role", "guild_id": "g",
                               "user_id": "u", "role_id": "r"},
                ))
    assert result.is_error
    payload = json.loads(result.content)
    assert "MANAGE_ROLES" in payload["error"]


# ─── Happy paths ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_list_guilds_happy(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    sample = [
        {"id": "1", "name": "Test Guild", "icon": None, "owner": True, "permissions": "8"},
    ]
    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            with patch("extensions.discord.server_tool._discord_request",
                       return_value=sample):
                tool = DiscordServerTool()
                result = await tool.execute(ToolCall(
                    id="t1", name="discord_server", arguments={"action": "list_guilds"},
                ))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["count"] == 1
    assert payload["guilds"][0]["name"] == "Test Guild"


@pytest.mark.asyncio
async def test_execute_fetch_messages_happy(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    sample = [
        {
            "id": "100", "content": "hello",
            "author": {"id": "9", "username": "alice", "global_name": "Alice", "bot": False},
            "timestamp": "2026-01-01T00:00:00Z",
            "edited_timestamp": None,
            "attachments": [],
            "reactions": [{"emoji": {"name": "👍"}, "count": 3}],
            "pinned": False,
        },
    ]
    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            with patch("extensions.discord.server_tool._discord_request",
                       return_value=sample):
                tool = DiscordServerTool()
                result = await tool.execute(ToolCall(
                    id="t1", name="discord_server",
                    arguments={"action": "fetch_messages", "channel_id": "ch1", "limit": 10},
                ))
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["count"] == 1
    msg = payload["messages"][0]
    assert msg["content"] == "hello"
    assert msg["reactions"][0]["emoji"] == "👍"


@pytest.mark.asyncio
async def test_execute_create_thread_with_message_id(monkeypatch):
    """Using message_id should hit the /messages/{id}/threads endpoint."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    captured = {}

    def fake_request(method, path, token, **kw):  # noqa: ANN001
        captured["method"] = method
        captured["path"] = path
        return {"id": "thread1", "name": "discussion"}

    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            with patch("extensions.discord.server_tool._discord_request",
                       side_effect=fake_request):
                tool = DiscordServerTool()
                result = await tool.execute(ToolCall(
                    id="t1", name="discord_server",
                    arguments={
                        "action": "create_thread", "channel_id": "ch1",
                        "name": "discussion", "message_id": "msg42",
                    },
                ))
    assert not result.is_error
    assert captured["method"] == "POST"
    assert captured["path"] == "/channels/ch1/messages/msg42/threads"


@pytest.mark.asyncio
async def test_execute_create_thread_without_message_id(monkeypatch):
    """No message_id should hit the standalone /threads endpoint."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    captured = {}

    def fake_request(method, path, token, **kw):  # noqa: ANN001
        captured["path"] = path
        captured["body"] = kw.get("body")
        return {"id": "thread1", "name": "discussion"}

    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            with patch("extensions.discord.server_tool._discord_request",
                       side_effect=fake_request):
                tool = DiscordServerTool()
                result = await tool.execute(ToolCall(
                    id="t1", name="discord_server",
                    arguments={
                        "action": "create_thread", "channel_id": "ch1",
                        "name": "discussion",
                    },
                ))
    assert not result.is_error
    assert captured["path"] == "/channels/ch1/threads"
    assert captured["body"]["type"] == 11  # PUBLIC_THREAD


@pytest.mark.asyncio
async def test_execute_search_members_clamps_limit(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake")
    captured = {}

    def fake_request(method, path, token, **kw):  # noqa: ANN001
        captured["params"] = kw.get("params")
        return []

    with patch("extensions.discord.server_tool._detect_capabilities",
               return_value={"has_members_intent": True, "has_message_content": True, "detected": True}):
        with patch("extensions.discord.server_tool._load_allowed_actions_config",
                   return_value=None):
            with patch("extensions.discord.server_tool._discord_request",
                       side_effect=fake_request):
                tool = DiscordServerTool()
                await tool.execute(ToolCall(
                    id="t1", name="discord_server",
                    arguments={
                        "action": "search_members", "guild_id": "g",
                        "query": "alice", "limit": 500,
                    },
                ))
    assert captured["params"]["limit"] == "100"  # clamped


# ─── Allowlist config parsing ───────────────────────────────────


def test_load_allowed_actions_filters_unknown(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "discord:\n  server_actions: list_guilds, server_info, no_such_action\n",
    )

    with patch("opencomputer.agent.config_store.config_file_path",
               return_value=cfg):
        from extensions.discord.server_tool import _load_allowed_actions_config
        actions = _load_allowed_actions_config()
    assert actions == ["list_guilds", "server_info"]


def test_load_allowed_actions_yaml_list(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "discord:\n  server_actions:\n    - list_guilds\n    - fetch_messages\n",
    )
    with patch("opencomputer.agent.config_store.config_file_path",
               return_value=cfg):
        from extensions.discord.server_tool import _load_allowed_actions_config
        actions = _load_allowed_actions_config()
    assert actions == ["list_guilds", "fetch_messages"]


def test_load_allowed_actions_unset_returns_none(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  name: x\n")
    with patch("opencomputer.agent.config_store.config_file_path",
               return_value=cfg):
        from extensions.discord.server_tool import _load_allowed_actions_config
        actions = _load_allowed_actions_config()
    assert actions is None


def test_load_allowed_actions_missing_file_returns_none(tmp_path):
    cfg = tmp_path / "does_not_exist.yaml"
    with patch("opencomputer.agent.config_store.config_file_path",
               return_value=cfg):
        from extensions.discord.server_tool import _load_allowed_actions_config
        actions = _load_allowed_actions_config()
    assert actions is None
