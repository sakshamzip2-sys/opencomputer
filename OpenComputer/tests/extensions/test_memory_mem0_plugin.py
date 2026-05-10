"""Tests for the memory-mem0 plugin (Hermes A3).

Covers:
- Provider construction + provider_id.
- Graceful degrade when ``mem0ai`` SDK is not installed.
- Tool dispatch (search/remember/forget) via a fake mem0 client.
- Error path when ``handle_tool_call`` hits a Mem0 exception.
- ``register()`` survives both old cores (no register_memory_provider) and
  modern cores.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# tests/conftest.py registers ``extensions.memory_mem0`` for us.
from extensions.memory_mem0.provider import Mem0Config, Mem0Provider

from plugin_sdk.core import ToolCall


def _make_provider(client: Any | None = None) -> Mem0Provider:
    cfg = Mem0Config(api_key="test-key", user_id="test-user")
    p = Mem0Provider(cfg)
    if client is not None:
        # Bypass lazy init for tests.
        p._client = client  # noqa: SLF001
        p._client_ready = True  # noqa: SLF001
        p._init_attempted = True  # noqa: SLF001
    return p


def test_provider_id_stable() -> None:
    p = _make_provider()
    assert p.provider_id == "memory-mem0:client"


def test_tool_schemas_exposes_three_tools() -> None:
    p = _make_provider()
    names = {s.name for s in p.tool_schemas()}
    assert names == {"mem0_search", "mem0_remember", "mem0_forget"}


def test_tool_schemas_empty_when_disabled() -> None:
    p = Mem0Provider(Mem0Config(enabled=False))
    assert p.tool_schemas() == []


@pytest.mark.asyncio
async def test_handle_tool_call_no_sdk_returns_error() -> None:
    """Without a mem0ai install, every tool call returns is_error=True."""
    p = Mem0Provider(Mem0Config())
    # Force the lazy-init flag without a real SDK
    p._init_attempted = True  # noqa: SLF001
    p._sdk_missing = True  # noqa: SLF001
    out = await p.handle_tool_call(
        ToolCall(id="c1", name="mem0_search", arguments={"query": "anything"})
    )
    assert out.is_error is True
    assert "pip install mem0ai" in (out.content or "")


@pytest.mark.asyncio
async def test_handle_tool_call_search_returns_results() -> None:
    fake_client = MagicMock()
    fake_client.search.return_value = [
        {"id": "m1", "memory": "user prefers concise responses"},
        {"id": "m2", "memory": "based in Mac/zsh env"},
    ]
    p = _make_provider(client=fake_client)
    out = await p.handle_tool_call(
        ToolCall(id="c1", name="mem0_search", arguments={"query": "preferences", "limit": 5})
    )
    assert out.is_error is None or out.is_error is False
    assert "concise" in (out.content or "")
    fake_client.search.assert_called_once_with(
        query="preferences", user_id="test-user", limit=5
    )


@pytest.mark.asyncio
async def test_handle_tool_call_search_normalises_dict_response() -> None:
    """Some Mem0 SDK paths return ``{"results": [...]}`` instead of a list."""
    fake_client = MagicMock()
    fake_client.search.return_value = {"results": [{"memory": "x"}]}
    p = _make_provider(client=fake_client)
    out = await p.handle_tool_call(
        ToolCall(id="c1", name="mem0_search", arguments={"query": "x"})
    )
    assert "memory" in (out.content or "") or "x" in (out.content or "")


@pytest.mark.asyncio
async def test_handle_tool_call_remember() -> None:
    fake_client = MagicMock()
    p = _make_provider(client=fake_client)
    out = await p.handle_tool_call(
        ToolCall(id="c1", name="mem0_remember", arguments={"content": "loves opus"})
    )
    assert out.content == "ok"
    assert out.is_error is None or out.is_error is False
    fake_client.add.assert_called_once()


@pytest.mark.asyncio
async def test_handle_tool_call_remember_empty_content_errors() -> None:
    fake_client = MagicMock()
    p = _make_provider(client=fake_client)
    out = await p.handle_tool_call(
        ToolCall(id="c1", name="mem0_remember", arguments={"content": "   "})
    )
    assert out.is_error is True
    fake_client.add.assert_not_called()


@pytest.mark.asyncio
async def test_handle_tool_call_forget() -> None:
    fake_client = MagicMock()
    p = _make_provider(client=fake_client)
    out = await p.handle_tool_call(
        ToolCall(id="c1", name="mem0_forget", arguments={"memory_id": "m-42"})
    )
    assert out.content == "deleted"
    fake_client.delete.assert_called_once_with(memory_id="m-42")


@pytest.mark.asyncio
async def test_handle_tool_call_unknown_tool_errors() -> None:
    p = _make_provider(client=MagicMock())
    out = await p.handle_tool_call(
        ToolCall(id="c1", name="mem0_unknown", arguments={})
    )
    assert out.is_error is True
    assert "unknown tool" in (out.content or "")


@pytest.mark.asyncio
async def test_handle_tool_call_swallows_client_exception() -> None:
    """Per Memory contract, MUST NOT raise — return is_error=True."""
    fake_client = MagicMock()
    fake_client.search.side_effect = RuntimeError("network down")
    p = _make_provider(client=fake_client)
    out = await p.handle_tool_call(
        ToolCall(id="c1", name="mem0_search", arguments={"query": "anything"})
    )
    assert out.is_error is True
    assert "RuntimeError" in (out.content or "")


@pytest.mark.asyncio
async def test_prefetch_no_sdk_returns_none() -> None:
    p = Mem0Provider(Mem0Config())
    p._init_attempted = True  # noqa: SLF001
    p._sdk_missing = True  # noqa: SLF001
    assert await p.prefetch("any query", turn_index=1) is None


@pytest.mark.asyncio
async def test_prefetch_with_results() -> None:
    fake_client = MagicMock()
    fake_client.search.return_value = [
        {"memory": "fact 1"},
        {"memory": "fact 2"},
    ]
    p = _make_provider(client=fake_client)
    out = await p.prefetch("cur query", turn_index=1)
    assert out is not None
    assert "fact 1" in out
    assert "fact 2" in out


@pytest.mark.asyncio
async def test_prefetch_empty_returns_none() -> None:
    fake_client = MagicMock()
    fake_client.search.return_value = []
    p = _make_provider(client=fake_client)
    assert await p.prefetch("q", turn_index=1) is None


@pytest.mark.asyncio
async def test_sync_turn_swallows_errors() -> None:
    fake_client = MagicMock()
    fake_client.add.side_effect = RuntimeError("nope")
    p = _make_provider(client=fake_client)
    # Should not raise
    await p.sync_turn(user="hi", assistant="hello", turn_index=1)


@pytest.mark.asyncio
async def test_health_check_no_sdk_returns_false() -> None:
    p = Mem0Provider(Mem0Config())
    p._init_attempted = True  # noqa: SLF001
    p._sdk_missing = True  # noqa: SLF001
    assert await p.health_check() is False


@pytest.mark.asyncio
async def test_health_check_with_client_returns_true() -> None:
    p = _make_provider(client=MagicMock())
    assert await p.health_check() is True


@pytest.mark.asyncio
async def test_system_prompt_block_with_results() -> None:
    fake_client = MagicMock()
    fake_client.search.return_value = [
        {"memory": "user-fact-A"},
        {"memory": "user-fact-B"},
    ]
    p = _make_provider(client=fake_client)
    block = await p.system_prompt_block(session_id="any")
    assert block is not None
    assert "Mem0 user profile" in block
    assert "user-fact-A" in block


# ─── register() tests ───────────────────────────────────────────────────


def test_register_with_compat_api_calls_register_memory_provider(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register() invokes api.register_memory_provider on a modern core."""
    from extensions.memory_mem0 import plugin as plugin_mod

    # Plugin short-circuits silently when neither MEM0_API_KEY nor
    # MEM0_BASE_URL is set (M1.B1 follow-up); set one so the register
    # path is reached.
    monkeypatch.setenv("MEM0_API_KEY", "fake-test-key")
    api = MagicMock()
    plugin_mod.register(api)
    api.register_memory_provider.assert_called_once()


def test_register_on_old_core_warns_and_returns(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old core (no method) → warning logged, no crash."""
    from extensions.memory_mem0 import plugin as plugin_mod

    monkeypatch.setenv("MEM0_API_KEY", "fake-test-key")

    class _OldAPI:
        # No register_memory_provider attr.
        pass

    with caplog.at_level("WARNING", logger="memory-mem0"):
        plugin_mod.register(_OldAPI())
    assert any(
        "register_memory_provider" in rec.message
        for rec in caplog.records
    )


def test_user_id_from_profile_default(monkeypatch) -> None:
    from extensions.memory_mem0 import plugin as plugin_mod

    monkeypatch.delenv("OPENCOMPUTER_PROFILE", raising=False)
    monkeypatch.delenv("MEM0_USER_ID", raising=False)
    cfg = plugin_mod._config_from_env()  # noqa: SLF001
    assert cfg.user_id == "opencomputer"


def test_user_id_from_profile_custom(monkeypatch) -> None:
    from extensions.memory_mem0 import plugin as plugin_mod

    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "work")
    monkeypatch.delenv("MEM0_USER_ID", raising=False)
    cfg = plugin_mod._config_from_env()  # noqa: SLF001
    assert cfg.user_id == "opencomputer.work"


def test_user_id_explicit_override(monkeypatch) -> None:
    from extensions.memory_mem0 import plugin as plugin_mod

    monkeypatch.setenv("OPENCOMPUTER_PROFILE", "work")
    monkeypatch.setenv("MEM0_USER_ID", "saksham-personal")
    cfg = plugin_mod._config_from_env()  # noqa: SLF001
    assert cfg.user_id == "saksham-personal"
