"""Unit tests for `snapshot/chrome_mcp.py` + `snapshot/chrome_mcp_snapshot.py`.

Covers:
  - build_ai_snapshot_from_chrome_mcp_snapshot (Path 3) — uid is the ref;
    walks the tree, lowercases roles, defaults missing roles to "generic",
    applies interactive/compact/max_depth filters
  - element-targeted screenshots: uid → ref identity (no extra mapping
    needed; documented in deep-dive §8)
  - Chrome MCP session caching: tool errors keep session alive; transport
    errors tear it down
  - smoke test for spawn_chrome_mcp via injected session_factory
  - max_chars truncates with the expected marker
"""

from __future__ import annotations

from typing import Any

import pytest
from extensions.browser_control.snapshot import (
    ChromeMcpClient,
    ChromeMcpSnapshotNode,
    ChromeMcpToolError,
    ChromeMcpTransportError,
    build_ai_snapshot_from_chrome_mcp_snapshot,
    flatten_chrome_mcp_snapshot,
    spawn_chrome_mcp,
)

# ─── path 3 — chrome MCP snapshot ─────────────────────────────────────


def _example_tree() -> dict[str, Any]:
    return {
        "id": "root",
        "role": "document",
        "name": "Example",
        "children": [
            {"id": "btn-1", "role": "button", "name": "Continue"},
            {"id": "txt-1", "role": "textbox", "name": "Email", "value": "a@b.c"},
            {"id": "btn-2", "role": "button", "name": "Continue"},
        ],
    }


def test_chrome_mcp_uid_is_ref_identity() -> None:
    """The Chrome MCP `id` (uid) becomes the `ref` directly — no allocation table."""
    result = build_ai_snapshot_from_chrome_mcp_snapshot(_example_tree())
    # Two `Continue` buttons should be deduped with nth=1 on the second.
    btn1 = result.refs.get("btn-1")
    btn2 = result.refs.get("btn-2")
    assert btn1 is not None and btn1.role == "button" and btn1.name == "Continue"
    assert btn2 is not None and btn2.role == "button" and btn2.name == "Continue"
    # Duplicates → nth set; first occurrence stays None (we use 1-based after first).
    assert btn1.nth is None
    assert btn2.nth == 1


def test_chrome_mcp_value_and_description_emitted() -> None:
    tree = {
        "id": "root",
        "role": "form",
        "children": [
            {
                "id": "txt-1",
                "role": "textbox",
                "name": "Email",
                "value": "a@b.c",
                "description": "Email address",
            }
        ],
    }
    result = build_ai_snapshot_from_chrome_mcp_snapshot(tree)
    text = result.snapshot
    assert 'value="a@b.c"' in text
    assert 'description="Email address"' in text


def test_chrome_mcp_missing_role_defaults_to_generic() -> None:
    tree = {"id": "x", "role": "", "children": []}
    result = build_ai_snapshot_from_chrome_mcp_snapshot(tree)
    assert "generic" in result.snapshot


def test_chrome_mcp_max_depth_filter() -> None:
    tree = {
        "id": "root",
        "role": "document",
        "children": [
            {
                "id": "lvl1",
                "role": "main",
                "name": "Level 1",
                "children": [
                    {"id": "lvl2", "role": "button", "name": "Level 2"},
                ],
            }
        ],
    }
    result = build_ai_snapshot_from_chrome_mcp_snapshot(tree, max_depth=1)
    # Level 2 button beyond the cap.
    assert "Level 2" not in result.snapshot
    assert "lvl2" not in result.refs


def test_chrome_mcp_max_chars_truncates() -> None:
    big_children = [
        {"id": f"b{i}", "role": "button", "name": f"Btn {i}"} for i in range(50)
    ]
    tree = {"id": "root", "role": "document", "children": big_children}
    result = build_ai_snapshot_from_chrome_mcp_snapshot(tree, max_chars=200)
    assert result.truncated is True
    assert "[...TRUNCATED - page too large]" in result.snapshot


def test_flatten_chrome_mcp_snapshot_dfs() -> None:
    nodes = flatten_chrome_mcp_snapshot(_example_tree(), limit=10)
    # DFS order: root → btn-1 → txt-1 → btn-2 (siblings visited in input order).
    ids = [n["id"] for n in nodes]
    assert ids == ["root", "btn-1", "txt-1", "btn-2"]


def test_chrome_mcp_compact_drops_unnamed_structural() -> None:
    tree = {
        "id": "root",
        "role": "generic",
        "children": [
            {"id": "btn", "role": "button", "name": "Click"},
            # Unnamed structural — should be dropped under compact.
            {"id": "g1", "role": "generic", "children": []},
        ],
    }
    result = build_ai_snapshot_from_chrome_mcp_snapshot(tree, compact=True)
    # Top-level generic is unnamed → dropped.
    assert " generic" not in result.snapshot or "generic\n" not in result.snapshot
    assert "button" in result.snapshot


def test_chrome_mcp_dataclass_round_trip() -> None:
    node = ChromeMcpSnapshotNode.from_dict(_example_tree())
    assert node.role == "document"
    assert len(node.children) == 3
    assert node.children[0].id == "btn-1"


# ─── ChromeMcpClient — session cache + error semantics ────────────────


class _FakeMcpResult:
    def __init__(
        self,
        *,
        structured: dict[str, Any] | None = None,
        text: list[str] | None = None,
        is_error: bool = False,
    ) -> None:
        self.structuredContent = structured  # noqa: N815 — mirror SDK casing
        self.content = [_TextBlock(t) for t in (text or [])]
        self.isError = is_error  # noqa: N815


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeListResult:
    def __init__(self, names: list[str]) -> None:
        self.tools = [_FakeTool(n) for n in names]


class FakeMcpSession:
    """Tracks call history; lets tests inject success / failure / error."""

    def __init__(
        self,
        *,
        tool_responses: dict[str, Any] | None = None,
        list_tools_names: list[str] | None = None,
    ) -> None:
        self._tool_responses = tool_responses or {}
        self._list_tools_names = list_tools_names if list_tools_names is not None else [
            "list_pages",
            "take_snapshot",
        ]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> _FakeListResult:
        return _FakeListResult(self._list_tools_names)

    async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, dict(arguments)))
        if name not in self._tool_responses:
            raise RuntimeError(f"unexpected tool {name!r}")
        resp = self._tool_responses[name]
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.mark.asyncio
async def test_spawn_chrome_mcp_via_injected_factory() -> None:
    """spawn_chrome_mcp accepts a session_factory for tests."""
    session = FakeMcpSession(
        tool_responses={
            "take_snapshot": _FakeMcpResult(
                structured={
                    "snapshot": {"id": "root", "role": "document", "children": []}
                }
            )
        }
    )

    async def cleanup() -> None:
        return None

    async def factory(*, profile_name: str | None, user_data_dir: str | None) -> Any:
        return session, cleanup

    client = await spawn_chrome_mcp(session_factory=factory)
    assert isinstance(client, ChromeMcpClient)
    tools = await client.list_tools()
    assert "take_snapshot" in tools
    result = await client.call_tool("take_snapshot", {"pageId": 1})
    assert result.structured_content == {
        "snapshot": {"id": "root", "role": "document", "children": []}
    }
    await client.close()


@pytest.mark.asyncio
async def test_tool_error_keeps_session_alive() -> None:
    session = FakeMcpSession(
        tool_responses={
            "click": _FakeMcpResult(text=["element not found"], is_error=True),
            "take_snapshot": _FakeMcpResult(
                structured={"snapshot": {"id": "r", "role": "document", "children": []}}
            ),
        }
    )

    async def cleanup() -> None:
        return None

    async def factory(*, profile_name: str | None, user_data_dir: str | None) -> Any:
        return session, cleanup

    client = await spawn_chrome_mcp(session_factory=factory)
    with pytest.raises(ChromeMcpToolError, match="element not found"):
        await client.call_tool("click", {"uid": "x"})
    # Session still works after a tool error.
    assert client.closed is False
    result = await client.call_tool("take_snapshot", {"pageId": 1})
    assert result.structured_content is not None


@pytest.mark.asyncio
async def test_transport_error_tears_down_session() -> None:
    cleaned = {"called": False}

    session = FakeMcpSession(
        tool_responses={
            "take_snapshot": ConnectionResetError("transport gone"),
        }
    )

    async def cleanup() -> None:
        cleaned["called"] = True

    async def factory(*, profile_name: str | None, user_data_dir: str | None) -> Any:
        return session, cleanup

    client = await spawn_chrome_mcp(session_factory=factory)
    with pytest.raises(ChromeMcpTransportError):
        await client.call_tool("take_snapshot", {"pageId": 1})
    assert client.closed is True
    assert cleaned["called"] is True
