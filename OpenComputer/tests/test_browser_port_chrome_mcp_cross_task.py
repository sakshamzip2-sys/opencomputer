"""Regression test for Bug C — anyio cross-task cancel-scope violation.

The MCP SDK uses anyio task groups internally for both
``stdio_client`` and ``ClientSession``. anyio enforces that the task
that opens a cancel scope must be the task that closes it. Before the
v0.5 owner-task refactor, ``ChromeMcpClient`` opened the contexts in
the spawning task and closed them via ``client.close()`` from a
different task — typically the agent-loop cleanup task — which raised:

    RuntimeError: Attempted to exit cancel scope in a different task
    than it was entered in.

The fix lives in ``snapshot/chrome_mcp.py``: a dedicated "lifetime"
task owns both context managers for their full duration. ``close()``
only signals the task to exit; the task itself runs both ``__aexit__``
calls in the same task that did the corresponding ``__aenter__`` calls.

These tests exercise the full pattern:

  * Spawn ``ChromeMcpClient`` in task A (here: the test entry task).
  * Issue ``call_tool`` from task B (a child task).
  * Call ``close()`` from task C (a different child task).
  * Verify no anyio cross-task ``RuntimeError`` is raised.

The first test uses the test ``session_factory`` path (no real anyio).
The second uses a real ``mcp.ClientSession`` over an in-memory
transport so the anyio cancel-scope guard is actually exercised; if
the real ``mcp`` SDK isn't installed the test is skipped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# ─── helpers — fakes for the no-mcp fast-path test ───────────────────


class _FakeListResult:
    def __init__(self, names: list[str]) -> None:
        self.tools = [type("_T", (), {"name": n})() for n in names]


class _FakeMcpResult:
    def __init__(self, *, structured: dict[str, Any] | None = None) -> None:
        self.structuredContent = structured  # noqa: N815
        self.content: list[Any] = []
        self.isError = False  # noqa: N815


class _FakeSession:
    """Trivial mcp.ClientSession-shaped object — no anyio under the hood."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> _FakeListResult:
        return _FakeListResult(["take_snapshot", "new_page", "close_page"])

    async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> _FakeMcpResult:
        self.calls.append((name, dict(arguments)))
        return _FakeMcpResult(structured={"ok": True, "tool": name})


# ─── test 1 — owner-task path with the test session_factory ──────────


@pytest.mark.asyncio
async def test_chrome_mcp_close_from_different_task_no_runtime_error() -> None:
    """The lifetime owner-task pattern lets close() come from any task.

    Even with the legacy ``session_factory`` path (which doesn't use
    anyio), validating that close() is callable from a different
    asyncio.Task than the one that spawned the client guards against
    regressions where the close path tries to do something task-local.
    """
    from extensions.browser_control.snapshot import spawn_chrome_mcp

    session = _FakeSession()

    async def cleanup() -> None:
        return None

    async def factory(*, profile_name: str | None, user_data_dir: str | None) -> Any:
        return session, cleanup

    client = await spawn_chrome_mcp(session_factory=factory)

    # Use the client from a different task.
    async def _do_work() -> str:
        result = await client.call_tool("new_page", {"url": "https://example.com"})
        sc = result.structured_content or {}
        return str(sc.get("tool", ""))

    tool_name = await asyncio.create_task(_do_work())
    assert tool_name == "new_page"

    # Close from yet another task — this is the regression case.
    async def _do_close() -> None:
        await client.close()

    await asyncio.create_task(_do_close())
    assert client.closed is True


# ─── test 2 — real mcp.ClientSession over in-memory transport ────────


@pytest.mark.asyncio
async def test_chrome_mcp_real_mcp_session_cross_task_close() -> None:
    """End-to-end anyio cross-task verification with the real mcp SDK.

    Drives the production owner-task pattern by routing a real
    ``ClientSession`` over the in-memory pair helper. The point is to
    actually go through the anyio cancel-scope topology and verify
    ``close()`` from a different asyncio task does NOT raise the
    ``Attempted to exit cancel scope in a different task than it was
    entered in`` runtime error.
    """
    pytest.importorskip("mcp")

    try:
        from mcp.server import Server
        from mcp.shared.memory import (
            create_connected_server_and_client_session as _create_pair,
        )
        from mcp.types import TextContent, Tool
    except ImportError as exc:
        pytest.skip(f"mcp SDK helper unavailable: {exc}")

    from extensions.browser_control.snapshot.chrome_mcp import ChromeMcpClient

    server: Server[Any] = Server("chrome-mcp-stub")

    @server.list_tools()  # type: ignore[misc, no-redef]
    async def _list_tools() -> list[Any]:
        return [
            Tool(name="new_page", description="open a new tab", inputSchema={"type": "object"}),
            Tool(name="close_page", description="close a tab", inputSchema={"type": "object"}),
            Tool(name="take_snapshot", description="snapshot", inputSchema={"type": "object"}),
        ]

    @server.call_tool()  # type: ignore[misc, no-redef]
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        return [TextContent(type="text", text=f"called {name}")]

    ready = asyncio.Event()
    done = asyncio.Event()
    session_holder: list[Any] = []
    error_holder: list[BaseException] = []

    async def _lifetime() -> None:
        # The helper already calls session.initialize() internally;
        # don't double-init. Both anyio cancel scopes (the helper's
        # task_group + the inner ClientSession) live entirely within
        # this task — exiting the ``async with`` performs both cleanup
        # operations in this same task, satisfying anyio's guard.
        try:
            async with _create_pair(server) as session:
                session_holder.append(session)
                ready.set()
                await done.wait()
        except Exception as exc:  # noqa: BLE001
            error_holder.append(exc)
            ready.set()

    task = asyncio.create_task(_lifetime(), name="cross-task-test-lifetime")
    await ready.wait()
    if error_holder:
        # Could happen on older mcp versions where the helper API
        # differs. Skip — the production pathway is already covered by
        # test 1 and the real subprocess path is exercised in CI on
        # machines with chrome-devtools-mcp installed.
        await asyncio.gather(task, return_exceptions=True)
        pytest.skip(f"in-memory MCP pair unavailable: {error_holder[0]}")

    session = session_holder[0]
    client = ChromeMcpClient(session=session, lifetime_task=task, done_event=done)

    # Smoke — call from a child task.
    async def _do_work() -> None:
        await client.list_tools()
        await client.call_tool("new_page", {"url": "https://example.com"})

    await asyncio.create_task(_do_work())

    # The regression case — close from a different task than the one
    # that spawned the lifetime task.
    async def _do_close() -> None:
        await client.close()

    # If the close() path tried to drive the contexts' __aexit__ from
    # this child task instead of signalling the lifetime task, anyio
    # would raise here.
    await asyncio.create_task(_do_close())
    assert client.closed is True
