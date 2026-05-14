"""Regression tests for the MCP cross-task cancel-scope shutdown wall.

History
-------
Before 2026-05-14, ``oc chat`` started with a wall of stderr like::

    an error occurred during closing of asynchronous generator
    <async_generator object stdio_client at 0x...>
      ...
    RuntimeError: Attempted to exit cancel scope in a different task
    than it was entered in

every time the MCP servers in ``cfg.mcp.servers`` were spun up. Two
production bugs collided:

* ``opencomputer/cli.py`` opened the MCP sessions inside a transient
  ``asyncio.run(mcp_mgr.connect_all(...))`` whose loop teardown then
  ran ``shutdown_asyncgens()`` in a fresh task — different from the
  task that entered the ``stdio_client`` ``async with``. anyio's
  cancel-scope guard refuses cross-task exits, so each registered
  MCP server printed an unhandled exception group on every chat start.
* :class:`MCPManager` never owned a stable event loop, so per-turn
  ``asyncio.run`` callers in chat could not safely dispatch tool calls
  back to the session that opened them.

The fix is two-fold and lives in ``opencomputer/mcp/client.py``:

1. ``MCPConnection`` spawns a dedicated *owner task* that enters AND
   exits the ``stdio_client`` / ``ClientSession`` contexts in the
   same task — anyio's same-task rule is satisfied even when the
   user-facing ``connect()`` / ``disconnect()`` calls happen from
   different tasks (or in a sync context after a loop tears down).
2. :class:`MCPManager` exposes a dedicated background event-loop
   thread (``start_background_loop`` / ``stop_background_loop`` /
   ``submit_sync``). Sessions live on that loop for the whole life
   of the chat; per-turn ``asyncio.run`` callers dispatch tool calls
   to it via ``run_coroutine_threadsafe`` + ``wrap_future`` (see
   :func:`opencomputer.mcp.client._run_on_session_loop`).

These tests guard both paths.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencomputer.agent.config import MCPServerConfig
from opencomputer.mcp.client import (
    MCPConnection,
    MCPManager,
    MCPTool,
    _run_on_session_loop,
)
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import ToolCall

# ──────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────


def _fake_tool_meta(name: str = "echo", description: str = "echo back") -> SimpleNamespace:
    """A minimal mcp.types.Tool stand-in for ``session.list_tools()``."""
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
        meta=None,
        annotations=None,
    )


def _build_fake_session(
    tools: list[SimpleNamespace], call_result_text: str = "ok"
) -> MagicMock:
    """Construct a MagicMock that mimics mcp.ClientSession's surface."""
    session = MagicMock()
    session.initialize = AsyncMock(
        return_value=SimpleNamespace(
            serverInfo=SimpleNamespace(version="0.0.0"),
            capabilities=SimpleNamespace(resources=None, prompts=None),
        )
    )
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=tools))
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text=call_result_text, type="text")],
            isError=False,
        )
    )
    return session


@contextlib.contextmanager
def _capture_stderr():
    """Capture sys.stderr writes for the duration of the block.

    Used to assert the absence of anyio's cross-task error wall — that
    wall is written by asyncio's asyncgen finalizer directly to
    ``sys.stderr`` (not via the logging framework), so we have to
    intercept the stream itself.
    """
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = old


def _stderr_clean(captured: str) -> bool:
    """Return True if the captured stderr is free of the cross-task wall."""
    if "Attempted to exit cancel scope" in captured:
        return False
    return "an error occurred during closing of asynchronous generator" not in captured


# ──────────────────────────────────────────────────────────────────────
# Mock-based tests (always run)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_task_unwinds_on_disconnect_in_same_task() -> None:
    """``async with`` enter and exit must happen in the same task.

    The simplest correctness check: the owner task should record the
    same task identity on entry and on exit. We patch ``stdio_client``
    and ``ClientSession`` to AsyncMock context managers and verify the
    enter/exit pair lands on the same ``asyncio.current_task()`` object.
    """
    entry_task: list[asyncio.Task[None] | None] = []
    exit_task: list[asyncio.Task[None] | None] = []

    class _Tracker:
        async def __aenter__(self) -> tuple[MagicMock, MagicMock]:
            entry_task.append(asyncio.current_task())
            return (MagicMock(), MagicMock())

        async def __aexit__(self, *_args: object) -> None:
            exit_task.append(asyncio.current_task())

    cfg = MCPServerConfig(
        name="srv", transport="stdio", command="echo", args=("hi",)
    )
    conn = MCPConnection(config=cfg)
    fake_session = _build_fake_session([_fake_tool_meta("public")])

    with patch(
        "opencomputer.mcp.client.stdio_client", return_value=_Tracker()
    ), patch("opencomputer.mcp.client.ClientSession") as session_cls:
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = fake_session
        session_cm.__aexit__.return_value = None
        session_cls.return_value = session_cm
        ok = await conn.connect(osv_check_enabled=False)
    assert ok
    assert len(entry_task) == 1
    assert entry_task[0] is conn._owner_task, "stdio_client entered in owner task"
    await conn.disconnect()
    assert len(exit_task) == 1
    assert exit_task[0] is entry_task[0], (
        "stdio_client must exit in the same task it entered — "
        "this is the property that prevents anyio's cross-task error."
    )


def test_manager_background_loop_lifecycle_is_clean() -> None:
    """Start + connect_all_sync + stop_background_loop leaves no
    cross-task wall on stderr.

    Uses fully mocked transports so the test runs offline. The
    invariant under test is the *plumbing* — anyio's same-task rule
    is checked against the actual owner-task pattern, not the mocks.
    """
    cfg = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="echo",
        args=("hi",),
        enabled=True,
        connect_timeout=5.0,
        timeout=5.0,
        env={},
        headers={},
        tools_deny=(),
    )
    fake_session = _build_fake_session([_fake_tool_meta("echo")])

    with _capture_stderr() as buf, patch(
        "opencomputer.mcp.client.stdio_client"
    ) as stdio_ctx, patch("opencomputer.mcp.client.ClientSession") as session_cls:
        stdio_cm = AsyncMock()
        stdio_cm.__aenter__.return_value = (MagicMock(), MagicMock())
        stdio_cm.__aexit__.return_value = None
        stdio_ctx.return_value = stdio_cm
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = fake_session
        session_cm.__aexit__.return_value = None
        session_cls.return_value = session_cm

        mgr = MCPManager(tool_registry=ToolRegistry())
        n = mgr.connect_all_sync([cfg], osv_check_enabled=False)
        assert n > 0
        assert len(mgr.connections) == 1
        # Tools should be tagged with the bg loop so cross-loop dispatch
        # routes correctly even from per-turn asyncio.run callers.
        for tool in mgr.connections[0].tools:
            if isinstance(tool, MCPTool):
                assert tool.session_loop is mgr.background_loop
        mgr.stop_background_loop()

    assert _stderr_clean(buf.getvalue()), (
        f"unexpected anyio cross-task / asyncgen error on stderr:\n"
        f"{buf.getvalue()}"
    )


def test_cross_loop_tool_call_via_run_coroutine_threadsafe() -> None:
    """An MCPTool whose session lives on the bg loop must be callable
    from a *different* event loop — that's the per-turn ``asyncio.run``
    pattern in ``oc chat``.
    """
    cfg = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="echo",
        args=("hi",),
        enabled=True,
        connect_timeout=5.0,
        timeout=5.0,
        env={},
        headers={},
        tools_deny=(),
    )
    fake_session = _build_fake_session(
        [_fake_tool_meta("echo")], call_result_text="cross-loop-ok"
    )

    with patch("opencomputer.mcp.client.stdio_client") as stdio_ctx, patch(
        "opencomputer.mcp.client.ClientSession"
    ) as session_cls:
        stdio_cm = AsyncMock()
        stdio_cm.__aenter__.return_value = (MagicMock(), MagicMock())
        stdio_cm.__aexit__.return_value = None
        stdio_ctx.return_value = stdio_cm
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = fake_session
        session_cm.__aexit__.return_value = None
        session_cls.return_value = session_cm

        mgr = MCPManager(tool_registry=ToolRegistry())
        try:
            mgr.connect_all_sync([cfg], osv_check_enabled=False)
            tool = next(
                t for t in mgr.connections[0].tools if isinstance(t, MCPTool)
            )
            # The tool's session_loop is the bg loop. Dispatch from a
            # fresh asyncio.run loop simulates per-turn chat behaviour.
            result = asyncio.run(
                tool.execute(
                    ToolCall(id="t1", name=tool.tool_name, arguments={})
                )
            )
            assert result.is_error is False
            assert "cross-loop-ok" in result.content
        finally:
            mgr.stop_background_loop()


@pytest.mark.asyncio
async def test_run_on_session_loop_same_loop_skips_trampoline() -> None:
    """When the session loop is the current loop, the helper must NOT
    use ``run_coroutine_threadsafe`` — that would deadlock (you can't
    submit a coroutine to the loop you're currently awaiting on).
    """
    current = asyncio.get_running_loop()
    sentinel = object()

    async def _factory():
        return sentinel

    result = await _run_on_session_loop(_factory, current, timeout=1.0)
    assert result is sentinel


@pytest.mark.asyncio
async def test_run_on_session_loop_no_loop_skips_trampoline() -> None:
    """``session_loop=None`` (test-double construction path) must await
    directly. This is the path exercised by ``MCPTool.__new__(MCPTool)``
    test doubles in :mod:`tests.test_mcp_redaction`.
    """
    sentinel = object()

    async def _factory():
        return sentinel

    result = await _run_on_session_loop(_factory, None, timeout=1.0)
    assert result is sentinel


# ──────────────────────────────────────────────────────────────────────
# Live test (requires npx)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    shutil.which("npx") is None,
    reason="npx not installed — live MCP test skipped",
)
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="live network test skipped in CI",
)
@pytest.mark.slow
def test_live_context7_no_cross_task_wall() -> None:
    """Real-subprocess regression: connect to an actual MCP server and
    verify a clean shutdown produces no anyio cross-task error wall.

    This is the test that would have failed *before* the 2026-05-14
    fix — it spawns a real ``@upstash/context7-mcp`` over stdio,
    captures stderr, and asserts the regex that signals the bug never
    appears. Skipped when ``npx`` is missing or in CI to keep the
    suite hermetic.
    """
    cfg = MCPServerConfig(
        name="context7",
        command="npx",
        args=("-y", "@upstash/context7-mcp"),
        transport="stdio",
        enabled=True,
        connect_timeout=30.0,
        timeout=30.0,
        env={},
        headers={},
        tools_deny=(),
    )

    with _capture_stderr() as buf:
        mgr = MCPManager(tool_registry=ToolRegistry())
        try:
            n = mgr.connect_all_sync(
                [cfg], osv_check_enabled=False, timeout=60.0
            )
            assert n > 0, "Expected at least one tool from context7"
            # Call a tool from a fresh per-turn loop to exercise the
            # cross-loop dispatch path under real anyio streams.
            tool = next(
                t for t in mgr.connections[0].tools if isinstance(t, MCPTool)
            )

            async def _call():
                return await tool.execute(
                    ToolCall(
                        id="live-1",
                        name=tool.tool_name,
                        arguments={"libraryName": "react"},
                    )
                )

            asyncio.run(_call())
        finally:
            mgr.stop_background_loop()

    captured = buf.getvalue()
    assert _stderr_clean(captured), (
        "the cross-task cancel-scope wall regressed — stderr:\n"
        f"{captured}"
    )
