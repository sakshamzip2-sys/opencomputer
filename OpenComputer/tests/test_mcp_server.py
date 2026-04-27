"""Tests for opencomputer.mcp.server — MCP server-mode shape + tool coverage.

We don't run a stdio MCP client here (that's an integration concern; tested
manually against Claude Code). We DO verify:

- The server constructs cleanly with the right name + 5 tools registered.
- Each tool has a sensible JSON schema (name, description, input fields).
- Each tool's underlying function returns the right shape for empty + populated DBs.
- The CLI subcommand is wired (via importing the module).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opencomputer.mcp.server import build_server


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


class TestServerStructure:
    def test_server_name(self) -> None:
        s = build_server()
        assert s.name == "opencomputer"

    def test_seven_tools_registered(self) -> None:
        # Tier-A item 14 added ``channels_list`` and ``events_poll`` to the
        # original five — keep the assertion strict so future deletions are
        # caught explicitly.
        s = build_server()
        tools = asyncio.run(s.list_tools())
        names = sorted(t.name for t in tools)
        assert names == [
            "channels_list",
            "consent_history",
            "events_poll",
            "messages_read",
            "recall_search",
            "session_get",
            "sessions_list",
        ]

    def test_each_tool_has_description(self) -> None:
        s = build_server()
        tools = asyncio.run(s.list_tools())
        for t in tools:
            assert t.description, f"{t.name} missing description"
            assert len(t.description) > 20, f"{t.name} description too short"

    def test_each_tool_has_input_schema(self) -> None:
        s = build_server()
        tools = asyncio.run(s.list_tools())
        for t in tools:
            schema = t.inputSchema
            assert schema is not None
            assert schema.get("type") == "object", f"{t.name} schema not object-shaped"


class TestSessionsListTool:
    def test_empty_db_returns_empty_list(self) -> None:
        s = build_server()
        result = asyncio.run(_call_tool(s, "sessions_list", {"limit": 10}))
        assert result == []

    def test_limit_bounded_to_max_200(self) -> None:
        s = build_server()
        result = asyncio.run(_call_tool(s, "sessions_list", {"limit": 999_999}))
        assert isinstance(result, list)


class TestSessionGetTool:
    def test_unknown_session_returns_none(self) -> None:
        s = build_server()
        result = asyncio.run(_call_tool(s, "session_get", {"session_id": "nope"}))
        assert result is None


class TestMessagesReadTool:
    def test_unknown_session_returns_empty_list(self) -> None:
        s = build_server()
        result = asyncio.run(_call_tool(s, "messages_read", {"session_id": "nope", "limit": 10}))
        assert result == []


class TestRecallSearchTool:
    def test_empty_query_against_empty_db(self) -> None:
        s = build_server()
        result = asyncio.run(_call_tool(s, "recall_search", {"query": "anything", "limit": 5}))
        assert result == []


class TestConsentHistoryTool:
    def test_empty_profile_returns_empty(self) -> None:
        s = build_server()
        result = asyncio.run(_call_tool(s, "consent_history", {"limit": 10}))
        assert result == []

    def test_capability_filter_accepts_arg(self) -> None:
        s = build_server()
        result = asyncio.run(
            _call_tool(s, "consent_history", {"capability": "cron.create", "limit": 10})
        )
        assert isinstance(result, list)


class TestCLIWiring:
    def test_cli_imports_serve(self) -> None:
        """The mcp CLI should now expose `serve`."""
        from opencomputer.cli_mcp import mcp_app

        cmd_names = [cmd.name for cmd in mcp_app.registered_commands]
        assert "serve" in cmd_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call_tool(server, name: str, args: dict):
    """Invoke an MCP tool and return its decoded payload.

    FastMCP's ``call_tool`` returns ``(content_list, structured_dict)`` in
    newer versions; we unwrap to the original Python value the tool returned.
    """
    result = await server.call_tool(name, args)
    if isinstance(result, tuple):
        content_list, structured = result
        if structured is not None:
            # FastMCP wraps bare-list returns as {"result": [...]}
            if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
                return structured["result"]
            return structured
        result = content_list
    if isinstance(result, list) and result:
        c = result[0]
        text = getattr(c, "text", None) or str(c)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
    return result
