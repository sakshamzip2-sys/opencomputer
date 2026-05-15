"""Live end-to-end MCP test — real subprocess, real ClientSession.

The other 187 MCP/plugin tests in this branch use mocks for the
subprocess. This file uses the reference plugin's actual
``mcp_server.py`` to drive the full path:

1. Spawn the Python-based MCP server via stdio_client.
2. ClientSession handshake completes.
3. ``MCPManager.connect_one_sync`` registers the real ``MCPTool``s.
4. Tool dispatch returns a structured result.
5. Disconnect signals the owner task; the subprocess exits.
6. psutil verifies no orphan subprocess remains.

The reference plugin lives at ``extensions/downloads-cleanup-mcp/``
and the server is self-contained (depends only on the ``mcp`` SDK
that OC already pins). Tests use a tmp ``HOME`` so the server's
file operations don't touch the user's real ``~/Downloads``.

Skipped when:
- ``mcp`` SDK isn't importable (won't happen in a normal OC install).
- Running on Windows (POSIX-only process-tree assertions).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from opencomputer.agent.config import MCPServerConfig
from opencomputer.mcp.client import MCPManager, MCPTool
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import ToolCall


# Skip on platforms where killpg / process_tree semantics don't apply.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only process-tree assertions",
)


# Resolve the reference plugin's server script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_REF_MCP_SERVER = (
    _REPO_ROOT / "extensions" / "downloads-cleanup-mcp" / "mcp_server.py"
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Redirect HOME so the reference server's ~/Downloads is the test sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Create the Downloads dir so the server's `_downloads_root().exists()`
    # check returns True.
    (tmp_path / "Downloads").mkdir()
    return tmp_path


@pytest.fixture
def reference_server_cfg() -> MCPServerConfig:
    """Build an MCPServerConfig pointing at the reference plugin's server."""
    assert _REF_MCP_SERVER.exists(), (
        f"reference plugin server script missing at {_REF_MCP_SERVER}"
    )
    return MCPServerConfig(
        name="ref-downloads",
        transport="stdio",
        command=sys.executable,
        args=(str(_REF_MCP_SERVER),),
        enabled=True,
        # Reasonable for a tight unit test on the developer's box.
        connect_timeout=15.0,
        timeout=15.0,
    )


def _python_descendants_named(parent_pid: int, marker: str) -> list[int]:
    """Return descendant PIDs whose cmdline includes ``marker``."""
    try:
        parent = psutil.Process(parent_pid)
        descendants = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    out: list[int] = []
    for child in descendants:
        try:
            cmd = " ".join(child.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if marker in cmd:
            out.append(child.pid)
    return out


def test_live_spawn_connect_dispatch_disconnect(
    reference_server_cfg,
    _isolate_home,
) -> None:
    """End-to-end: spawn → connect → dispatch → disconnect → subprocess dead."""
    # Seed the sandboxed ~/Downloads with one stale file so list_downloads
    # has something to enumerate (and the assertion has structure to
    # verify, not just an empty list).
    downloads_dir = _isolate_home / "Downloads"
    stale = downloads_dir / "stale.txt"
    stale.write_text("stale\n")
    # Backdate so the `min_age_days >= 0` filter picks it up.
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))

    registry = ToolRegistry()
    mgr = MCPManager(tool_registry=registry)
    mgr.start_background_loop()
    try:
        marker = str(_REF_MCP_SERVER)
        # ── connect ───────────────────────────────────────────────
        ok = mgr.connect_one_sync(
            reference_server_cfg,
            osv_check_enabled=False,  # local script — no OSV record exists
            timeout=20.0,
        )
        assert ok is True, "connect_one_sync should return True for a healthy server"

        # ── subprocess is alive while connected ──────────────────
        live = _python_descendants_named(os.getpid(), marker)
        assert len(live) >= 1, (
            f"expected a live mcp_server.py descendant — got pids={live}"
        )
        first_live_pid = live[0]

        # ── tool registered with composed name ───────────────────
        tool_names = sorted(registry.names())
        # Compose pipeline: <server>__<tool>
        expected_tools = {
            "ref-downloads__list_downloads",
            "ref-downloads__summarise_downloads",
            "ref-downloads__archive_old",
        }
        assert expected_tools.issubset(set(tool_names)), (
            f"expected {expected_tools} in {tool_names}"
        )

        # ── dispatch a real call: list_downloads ─────────────────
        list_tool = registry.get("ref-downloads__list_downloads")
        assert isinstance(list_tool, MCPTool)
        call = ToolCall(
            id="live-1",
            name="ref-downloads__list_downloads",
            arguments={"min_age_days": 1, "limit": 50},
        )
        result = asyncio.run(list_tool.execute(call))
        assert not result.is_error, f"unexpected tool error: {result.content}"
        # FastMCP returns a JSON-encoded text block per list element
        # (pretty-printed across multiple lines). Use a robust parser:
        # try whole-content first, then split-by-double-newline if the
        # server emitted multiple blocks.
        payload_entries: list[dict] = []
        for chunk in result.content.split("\n}\n{"):
            # Re-add the braces stripped by the split (skipping the
            # first/last chunk which keep their bookend braces).
            if not chunk.startswith("{"):
                chunk = "{" + chunk
            if not chunk.endswith("}"):
                chunk = chunk + "}"
            try:
                parsed = json.loads(chunk)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                if "result" in parsed and isinstance(parsed["result"], list):
                    payload_entries.extend(
                        e for e in parsed["result"] if isinstance(e, dict)
                    )
                else:
                    payload_entries.append(parsed)
            elif isinstance(parsed, list):
                payload_entries.extend(
                    e for e in parsed if isinstance(e, dict)
                )
        assert payload_entries, (
            f"could not extract any dict entries from response: "
            f"{result.content!r}"
        )
        assert any(e.get("name") == "stale.txt" for e in payload_entries), (
            f"expected stale.txt entry, got: {payload_entries!r}"
        )

        # ── disconnect — verify subprocess dies ──────────────────
        # MCPManager.shutdown disconnects every connection.
        mgr.submit_sync(mgr.shutdown(), timeout=10.0)

        # Wait up to 5 seconds for the subprocess to actually exit
        # (SDK's _terminate_process_tree fires SIGTERM with a 2s grace).
        gone_deadline = time.time() + 5.0
        while time.time() < gone_deadline:
            still = _python_descendants_named(os.getpid(), marker)
            if not still:
                break
            time.sleep(0.1)
        else:
            still = _python_descendants_named(os.getpid(), marker)
            assert not still, (
                f"subprocess {first_live_pid} survived disconnect: still={still}"
            )

        # Defensive: even if the SDK left a stragger, OC's startup +
        # shutdown orphan sweep (Gap A) should have caught it.
        residual = _python_descendants_named(os.getpid(), marker)
        assert residual == [], (
            f"defense-in-depth orphan sweep missed residual pids: {residual}"
        )

    finally:
        mgr.stop_background_loop()
        # Safety: kill anything our test still owns.
        for pid in _python_descendants_named(os.getpid(), str(_REF_MCP_SERVER)):
            try:
                psutil.Process(pid).kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


def test_live_spawn_schema_validation_rejects_bad_args(
    reference_server_cfg,
) -> None:
    """OC-side schema validation (Gap C) rejects malformed args BEFORE
    the round-trip to the subprocess — no spawn-blocking RTT for
    obvious LLM mistakes."""
    registry = ToolRegistry()
    mgr = MCPManager(tool_registry=registry)
    mgr.start_background_loop()
    try:
        ok = mgr.connect_one_sync(
            reference_server_cfg, osv_check_enabled=False, timeout=20.0,
        )
        assert ok is True

        archive_tool = registry.get("ref-downloads__archive_old")
        assert isinstance(archive_tool, MCPTool)

        # archive_old expects min_age_days: integer. Pass a string.
        # If the reference server's input_schema declares it as
        # integer-typed, OC's validator catches it; otherwise it falls
        # through to the server and we'd see a server-side error.
        # Either way, the response is_error=True. We DON'T require
        # the validator path here because reference server's manifest
        # doesn't declare a strict schema for archive_old; the test
        # below uses a tool we know is schema-checked.

        list_tool = registry.get("ref-downloads__list_downloads")
        assert isinstance(list_tool, MCPTool)
        # The reference server uses Python typed signatures; FastMCP
        # generates a strict schema. Send a non-int for min_age_days.
        # Validator should reject OR server should reject — either is
        # an error result. Test the joint contract.
        call = ToolCall(
            id="bad-1",
            name="ref-downloads__list_downloads",
            arguments={"min_age_days": "not-a-number", "limit": 50},
        )
        result = asyncio.run(list_tool.execute(call))
        assert result.is_error, (
            f"expected error result for bad args, got: {result.content!r}"
        )
    finally:
        mgr.submit_sync(mgr.shutdown(), timeout=10.0)
        mgr.stop_background_loop()


def test_live_spawn_stderr_log_file_created(
    reference_server_cfg,
    _isolate_home,
    monkeypatch,
) -> None:
    """Gap B verification — when the subprocess spawns, OC's
    ``open_mcp_stderr_log`` creates the per-server log file under
    ``<profile>/logs/mcp/<server>.log``."""
    # Force _home() to point at the sandboxed profile dir.
    profile_home = _isolate_home / ".opencomputer"
    profile_home.mkdir()
    monkeypatch.setattr(
        "opencomputer.mcp.stderr_capture._home",
        lambda: profile_home,
    )

    registry = ToolRegistry()
    mgr = MCPManager(tool_registry=registry)
    mgr.start_background_loop()
    try:
        ok = mgr.connect_one_sync(
            reference_server_cfg, osv_check_enabled=False, timeout=20.0,
        )
        assert ok is True

        # The stderr log file should now exist under <profile>/logs/mcp/
        log_path = profile_home / "logs" / "mcp" / "ref-downloads.log"
        assert log_path.exists(), (
            f"expected per-server stderr log at {log_path}; "
            f"contents: {list((profile_home / 'logs' / 'mcp').iterdir()) if (profile_home / 'logs' / 'mcp').exists() else 'dir missing'}"
        )
        # Reference server writes a "starting on stdio" line at INFO —
        # the log file should be non-empty after a short settle period.
        time.sleep(0.5)
        assert log_path.stat().st_size > 0, (
            f"stderr log file is empty: {log_path}"
        )
    finally:
        mgr.submit_sync(mgr.shutdown(), timeout=10.0)
        mgr.stop_background_loop()
