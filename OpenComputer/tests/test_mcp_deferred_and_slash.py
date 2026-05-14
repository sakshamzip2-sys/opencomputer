"""Tests for the deferred-MCP-load + ``/mcp`` slash UX (2026-05-14).

These complement ``test_mcp_cross_task_shutdown.py``. That file guards
the anyio same-task invariant; this one guards the user-visible
behavior the Claude-Code-parity refactor added:

* ``MCPManager.start_in_background`` is truly non-blocking — chat
  startup never has to wait for ``npx`` to fetch a package.
* ``connect_one_sync`` / ``disconnect_one_sync`` work mid-session.
* ``connecting_names`` exposes in-flight server names so ``/mcp``
  can show "connecting…" status.
* The ``/mcp`` slash handler dispatches to the right callbacks for
  ``status`` / ``connect`` / ``disconnect`` / ``reload`` and rejects
  unknown subcommands.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opencomputer.agent.config import MCPServerConfig
from opencomputer.cli_ui.slash import SlashResult
from opencomputer.cli_ui.slash_handlers import SlashContext, _handle_mcp
from opencomputer.mcp.client import MCPConnection, MCPManager
from opencomputer.tools.registry import ToolRegistry


def _fake_tool_meta(name: str = "echo") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description="echo back",
        inputSchema={"type": "object", "properties": {}},
        meta=None,
        annotations=None,
    )


def _build_fake_session(tools: list[SimpleNamespace]) -> MagicMock:
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
            content=[SimpleNamespace(text="ok", type="text")], isError=False
        )
    )
    return session


# ──────────────────────────────────────────────────────────────────────
# start_in_background — non-blocking startup
# ──────────────────────────────────────────────────────────────────────


def test_start_in_background_returns_before_connect_completes() -> None:
    """The sync caller must not block on ``conn.connect``.

    We simulate a slow connect by holding ``__aenter__`` on the stdio
    context until released — and assert that ``start_in_background``
    has *already* returned by then. The released connect then registers
    its tools on its own time.
    """
    cfg = MCPServerConfig(
        name="slow",
        transport="stdio",
        command="echo",
        args=("hi",),
        enabled=True,
        connect_timeout=10.0,
        timeout=10.0,
        env={},
        headers={},
        tools_deny=(),
    )
    fake_session = _build_fake_session([_fake_tool_meta("echo")])

    release = threading.Event()

    class _SlowStdio:
        async def __aenter__(self) -> tuple[MagicMock, MagicMock]:
            # Block until the test thread releases — this would wedge
            # the caller if start_in_background were synchronous.
            while not release.is_set():
                import asyncio

                await asyncio.sleep(0.01)
            return (MagicMock(), MagicMock())

        async def __aexit__(self, *_args: object) -> None:
            return None

    with patch(
        "opencomputer.mcp.client.stdio_client", return_value=_SlowStdio()
    ), patch("opencomputer.mcp.client.ClientSession") as session_cls:
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = fake_session
        session_cm.__aexit__.return_value = None
        session_cls.return_value = session_cm

        mgr = MCPManager(tool_registry=ToolRegistry())
        try:
            t0 = time.monotonic()
            mgr.start_in_background([cfg], osv_check_enabled=False)
            elapsed = time.monotonic() - t0
            # Should return in milliseconds — generous bound to absorb
            # CI jitter without making the test brittle.
            assert elapsed < 0.5, (
                f"start_in_background blocked for {elapsed:.2f}s — "
                f"it should be fire-and-forget."
            )
            # The connect is genuinely still in flight. The bg loop
            # needs a few ms to start running ``connect_all`` and reach
            # the ``_connecting.add(...)`` line; poll briefly so the
            # assertion isn't racy on slow CI.
            deadline = time.monotonic() + 1.0
            while (
                not mgr.is_connecting("slow") and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert mgr.is_connecting("slow"), (
                "bg connect should be in-flight while the slow stdio "
                "__aenter__ blocks."
            )
            assert "slow" in mgr.connecting_names()
            # Let the connect finish + wait for the future to resolve
            # so we can assert the side effects.
            release.set()
            mgr.wait_for_deferred(timeout=10.0)
            assert not mgr.is_connecting("slow")
            assert any(c.config.name == "slow" for c in mgr.connections)
        finally:
            mgr.stop_background_loop()


def test_start_in_background_registers_tools_when_ready() -> None:
    """Tools appear in the registry after the deferred connect resolves."""
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
    fake_session = _build_fake_session([_fake_tool_meta("ping")])

    with patch(
        "opencomputer.mcp.client.stdio_client"
    ) as stdio_ctx, patch(
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

        registry = ToolRegistry()
        mgr = MCPManager(tool_registry=registry)
        try:
            mgr.start_in_background([cfg], osv_check_enabled=False)
            n = mgr.wait_for_deferred(timeout=5.0)
            assert n >= 1
            # Tool is registered AND tagged with the bg loop so
            # cross-loop calls dispatch correctly.
            from opencomputer.mcp.client import MCPTool

            mcp_tools = [
                t for t in mgr.connections[0].tools if isinstance(t, MCPTool)
            ]
            assert any(t.tool_name == "ping" for t in mcp_tools)
            assert all(
                t.session_loop is mgr.background_loop for t in mcp_tools
            )
        finally:
            mgr.stop_background_loop()


# ──────────────────────────────────────────────────────────────────────
# connect_one_sync / disconnect_one_sync — slash-command primitives
# ──────────────────────────────────────────────────────────────────────


def test_connect_one_sync_brings_up_named_server() -> None:
    cfg = MCPServerConfig(
        name="srv1",
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
    fake_session = _build_fake_session([_fake_tool_meta("alpha")])

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
            ok = mgr.connect_one_sync(cfg, osv_check_enabled=False)
            assert ok is True
            assert any(c.config.name == "srv1" for c in mgr.connections)
            # Idempotent — a second call with the same cfg name
            # disconnects the old one and re-connects cleanly.
            ok2 = mgr.connect_one_sync(cfg, osv_check_enabled=False)
            assert ok2 is True
            assert sum(1 for c in mgr.connections if c.config.name == "srv1") == 1
        finally:
            mgr.stop_background_loop()


def test_connect_one_sync_waits_for_in_flight_deferred_with_same_name() -> None:
    """A slash mid-deferred-connect for the same name must NOT spawn
    a duplicate connection — it must wait for the deferred to land,
    then disconnect+replace cleanly.
    """
    cfg = MCPServerConfig(
        name="alpha",
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
    fake_session = _build_fake_session([_fake_tool_meta("alpha-tool")])

    # Use a slow __aenter__ so the deferred connect is genuinely
    # in-flight while we trigger the slash.
    release = threading.Event()

    class _SlowStdio:
        async def __aenter__(self) -> tuple[MagicMock, MagicMock]:
            import asyncio

            while not release.is_set():
                await asyncio.sleep(0.01)
            return (MagicMock(), MagicMock())

        async def __aexit__(self, *_args: object) -> None:
            return None

    with patch(
        "opencomputer.mcp.client.stdio_client", return_value=_SlowStdio()
    ), patch("opencomputer.mcp.client.ClientSession") as session_cls:
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = fake_session
        session_cm.__aexit__.return_value = None
        session_cls.return_value = session_cm

        mgr = MCPManager(tool_registry=ToolRegistry())
        try:
            mgr.start_in_background([cfg], osv_check_enabled=False)
            # Wait until the deferred connect is actually in-flight,
            # then trigger the slash for the SAME name.
            deadline = time.monotonic() + 1.0
            while (
                not mgr.is_connecting("alpha")
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert mgr.is_connecting("alpha")

            # Release the deferred AFTER the slash submits, so the
            # slash genuinely raced the deferred. We do this on a
            # short timer thread.
            def _release_soon() -> None:
                time.sleep(0.1)
                release.set()

            threading.Thread(target=_release_soon, daemon=True).start()
            # connect_one_sync must wait for in-flight, then either
            # find the connection already there (and replace it) or
            # spawn fresh — either way exactly ONE connection ends up
            # in self.connections for this name.
            ok = mgr.connect_one_sync(cfg, osv_check_enabled=False, timeout=10.0)
            assert ok is True
            assert (
                sum(1 for c in mgr.connections if c.config.name == "alpha")
                == 1
            ), "duplicate connection — race fix didn't hold"
        finally:
            release.set()
            mgr.stop_background_loop()


def test_disconnect_one_sync_returns_false_for_unknown() -> None:
    mgr = MCPManager(tool_registry=ToolRegistry())
    # Background loop never started — graceful False.
    assert mgr.disconnect_one_sync("nonexistent") is False
    # After starting, still False for an unknown name.
    mgr.start_background_loop()
    try:
        assert mgr.disconnect_one_sync("nonexistent") is False
    finally:
        mgr.stop_background_loop()


# ──────────────────────────────────────────────────────────────────────
# /mcp slash handler
# ──────────────────────────────────────────────────────────────────────


def _make_ctx(**overrides) -> SlashContext:
    """SlashContext fixture with the minimum fields the handler reads.

    ``SlashContext`` requires three positional-ish fields without
    defaults (``on_clear`` / ``get_cost_summary`` / ``get_session_list``).
    We supply harmless no-op defaults so individual tests can override
    only the field they care about.
    """
    console = MagicMock()
    base: dict[str, object] = dict(
        console=console,
        session_id="t",
        config=SimpleNamespace(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=lambda: [],
    )
    base.update(overrides)
    return SlashContext(**base)  # type: ignore[arg-type]


def test_handle_mcp_status_prints_servers_and_connecting() -> None:
    """No-args ``/mcp`` calls the status callback and prints a table."""
    snapshots = {
        "servers": [
            {
                "name": "alpha",
                "connection_state": "connected",
                "tool_count": 3,
                "version": "1.0",
                "last_error": None,
            },
            {
                "name": "beta",
                "connection_state": "error",
                "tool_count": 0,
                "version": None,
                "last_error": "ECONNREFUSED",
            },
        ],
        "connecting": ["gamma"],
    }
    ctx = _make_ctx(on_mcp_status=lambda: snapshots)
    result = _handle_mcp(ctx, [])
    assert result == SlashResult(handled=True)
    # Console.print was called with at least one Rich Table.
    print_calls = ctx.console.print.call_args_list
    assert print_calls, "/mcp status should print at least once"


def test_handle_mcp_status_warns_when_unwired() -> None:
    ctx = _make_ctx(on_mcp_status=lambda: {})
    result = _handle_mcp(ctx, [])
    assert result == SlashResult(handled=True)
    assert any(
        "not wired" in str(c) for c in ctx.console.print.call_args_list
    )


def test_handle_mcp_connect_dispatches_to_callback() -> None:
    captured: list[str] = []

    def _cb(name: str) -> tuple[bool, str]:
        captured.append(name)
        return (True, f"connected {name}")

    ctx = _make_ctx(on_mcp_connect=_cb)
    result = _handle_mcp(ctx, ["connect", "alpha"])
    assert result == SlashResult(handled=True)
    assert captured == ["alpha"]


def test_handle_mcp_connect_needs_name() -> None:
    ctx = _make_ctx(on_mcp_connect=lambda _n: (True, ""))
    _handle_mcp(ctx, ["connect"])
    msg = " ".join(str(c) for c in ctx.console.print.call_args_list)
    assert "usage" in msg.lower()


def test_handle_mcp_disconnect_dispatches_to_callback() -> None:
    captured: list[str] = []

    def _cb(name: str) -> tuple[bool, str]:
        captured.append(name)
        return (True, f"dropped {name}")

    ctx = _make_ctx(on_mcp_disconnect=_cb)
    result = _handle_mcp(ctx, ["disconnect", "alpha"])
    assert result == SlashResult(handled=True)
    assert captured == ["alpha"]


def test_handle_mcp_reload_aliases_to_reload_mcp() -> None:
    """``/mcp reload`` shares ``on_reload_mcp`` with ``/reload-mcp``."""
    captured: list[str] = []

    def _reload_cb() -> dict:
        captured.append("hit")
        return {
            "servers_before": 0,
            "servers_after": 0,
            "tools_after": 0,
            "error": None,
        }

    ctx = _make_ctx(on_reload_mcp=_reload_cb)
    result = _handle_mcp(ctx, ["reload"])
    assert result == SlashResult(handled=True)
    assert captured == ["hit"]


def test_handle_mcp_rejects_unknown_subcommand() -> None:
    ctx = _make_ctx()
    result = _handle_mcp(ctx, ["frobnicate"])
    assert result == SlashResult(handled=True)
    msg = " ".join(str(c) for c in ctx.console.print.call_args_list)
    assert "unknown subcommand" in msg.lower()


# ──────────────────────────────────────────────────────────────────────
# CommandDef registration
# ──────────────────────────────────────────────────────────────────────


def test_mcp_is_in_slash_registry() -> None:
    """``/mcp`` must be discoverable from /help + the picker dropdown."""
    from opencomputer.cli_ui.slash import SLASH_REGISTRY

    names = {cmd.name for cmd in SLASH_REGISTRY}
    assert "mcp" in names
    assert "reload-mcp" in names  # legacy alias preserved


def test_mcp_dispatch_via_dispatch_slash() -> None:
    """``/mcp status`` routes through the full dispatcher."""
    from opencomputer.cli_ui.slash_handlers import dispatch_slash

    ctx = _make_ctx(on_mcp_status=lambda: {"servers": [], "connecting": []})
    result = dispatch_slash("/mcp status", ctx)
    assert result.handled is True


# ──────────────────────────────────────────────────────────────────────
# MCPConnection — defensive: connecting set cleared on every path
# ──────────────────────────────────────────────────────────────────────


def test_connecting_set_clears_even_on_connect_failure() -> None:
    """``_connecting`` must not leak names if a connect raises."""
    cfg = MCPServerConfig(
        name="broken",
        transport="stdio",
        command="echo",
        args=("hi",),
        enabled=True,
        connect_timeout=2.0,
        timeout=2.0,
        env={},
        headers={},
        tools_deny=(),
    )

    class _BrokenStdio:
        async def __aenter__(self) -> tuple[MagicMock, MagicMock]:
            raise RuntimeError("simulated transport failure")

        async def __aexit__(self, *_args: object) -> None:
            return None

    with patch(
        "opencomputer.mcp.client.stdio_client", return_value=_BrokenStdio()
    ):
        mgr = MCPManager(tool_registry=ToolRegistry())
        try:
            mgr.start_in_background([cfg], osv_check_enabled=False)
            mgr.wait_for_deferred(timeout=5.0)
            # Failed-connect must not leave "broken" stuck in connecting set.
            assert not mgr.is_connecting("broken")
            assert "broken" not in mgr.connecting_names()
        finally:
            mgr.stop_background_loop()


# ──────────────────────────────────────────────────────────────────────
# MCPConnection used directly — sanity for the existing test pattern
# ──────────────────────────────────────────────────────────────────────


def test_mcpconnection_disconnect_clears_owner_state() -> None:
    """After disconnect, owner-task plumbing is reset so a follow-up
    ``connect()`` starts cleanly.
    """
    import asyncio

    cfg = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="echo",
        args=("hi",),
    )
    conn = MCPConnection(config=cfg)
    fake_session = _build_fake_session([_fake_tool_meta("x")])

    async def _go() -> None:
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
            await conn.connect(osv_check_enabled=False)
        await conn.disconnect()

    asyncio.run(_go())
    assert conn._owner_task is None
    assert conn._owner_done is None
    assert conn._owner_ready is None
    assert conn.session is None
    assert conn.state == "disconnected"
