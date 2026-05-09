"""Hermes parity G13: permissions_list_open returns OPEN approval requests."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import patch

import pytest

from opencomputer.mcp.server import build_server


@pytest.fixture
def isolated_home(tmp_path):
    with patch("opencomputer.mcp.server._home", return_value=tmp_path):
        yield tmp_path


def _decode_tool_result(result):
    """FastMCP returns (content_list, structured_dict). Pull the structured form."""
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return structured
    content = getattr(result, "content", None)
    if content is None and isinstance(result, list):
        content = result
    if content:
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except (ValueError, TypeError):
                    return text
    return result


def test_permissions_list_open_tool_registered():
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "permissions_list_open" in names


def test_returns_empty_when_no_consent_requests_table(isolated_home):
    """G13 part-1: missing-table case returns [] (back-compat)."""
    server = build_server()
    result = asyncio.run(
        server.call_tool("permissions_list_open", {})
    )
    data = _decode_tool_result(result)
    assert data == [] or data == [[]]


def test_returns_pending_requests(isolated_home):
    """G13 part-2: queries the consent_requests table when present."""
    db_path = isolated_home / "sessions.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE consent_requests ("
            "capability_id TEXT, scope TEXT, requested_at REAL, "
            "requested_by TEXT, state TEXT)"
        )
        conn.execute(
            "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
            ("fs.write", "/etc/passwd", 1700000000.0, "tool:Edit", "pending"),
        )
        conn.execute(
            "INSERT INTO consent_requests VALUES (?, ?, ?, ?, ?)",
            ("shell.exec", "rm -rf /", 1700000001.0, "tool:Bash", "granted"),
        )
        conn.commit()

    server = build_server()
    result = asyncio.run(
        server.call_tool("permissions_list_open", {})
    )
    data = _decode_tool_result(result)
    assert isinstance(data, list), f"expected list, got: {type(data).__name__} {data!r}"
    assert len(data) == 1, f"expected 1 pending entry, got: {data!r}"
    assert data[0]["capability_id"] == "fs.write"
    assert data[0]["scope"] == "/etc/passwd"
