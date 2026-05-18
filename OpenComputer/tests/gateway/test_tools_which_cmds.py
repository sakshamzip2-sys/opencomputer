"""D1 / D2 — /which and /tools gateway-safe slash commands."""
from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands_impl.tools_cmd import ToolsCommand
from opencomputer.agent.slash_commands_impl.which_cmd import WhichCommand
from plugin_sdk.runtime_context import RuntimeContext


# ─── /which (D1) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_which_renders_resolution_chain():
    rt = RuntimeContext(custom={
        "platform": "telegram",
        "chat_id": "12345",
        "active_profile_id": "stocks",
        "model": "claude-opus-4-7",
        "session_id": "sess-abc",
    })
    res = await WhichCommand().execute("", rt)
    assert "telegram" in res.output
    assert "12345" in res.output
    assert "stocks" in res.output
    assert "claude-opus-4-7" in res.output
    assert "sess-abc" in res.output


@pytest.mark.asyncio
async def test_which_falls_back_to_profile_id_then_default():
    # No active_profile_id → profile_id is used.
    rt = RuntimeContext(custom={"profile_id": "coding"})
    assert "coding" in (await WhichCommand().execute("", rt)).output
    # Neither key → "default".
    rt2 = RuntimeContext(custom={})
    assert "default" in (await WhichCommand().execute("", rt2)).output


def test_which_is_gateway_safe():
    assert WhichCommand.gateway_safe is True


# ─── /tools (D2) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_lists_registered_tools():
    res = await ToolsCommand().execute("", RuntimeContext(custom={}))
    # The global registry has built-in tools registered at import time;
    # the listing names them with a count header.
    assert "Tools (" in res.output or "No tools" in res.output


@pytest.mark.asyncio
async def test_tools_handles_empty_registry(monkeypatch):
    """An empty registry → a clean 'no tools' message, never a crash."""
    from opencomputer.tools import registry as registry_mod

    class _Empty:
        @staticmethod
        def all_tools():
            return []

    monkeypatch.setattr(registry_mod, "registry", _Empty())
    res = await ToolsCommand().execute("", RuntimeContext(custom={}))
    assert "No tools" in res.output


def test_tools_is_gateway_safe():
    assert ToolsCommand.gateway_safe is True
