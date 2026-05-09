"""Hermes parity G12: conversations_list / conversation_get aliases."""
from __future__ import annotations

import pytest

from opencomputer.mcp.server import build_server


@pytest.mark.asyncio
async def test_conversations_list_alias_exists():
    server = build_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "conversations_list" in names, (
        f"missing alias; have: {sorted(names)[:20]}"
    )
    assert "sessions_list" in names, "canonical name removed"


@pytest.mark.asyncio
async def test_conversation_get_alias_exists():
    server = build_server()
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert "conversation_get" in names
    assert "session_get" in names
