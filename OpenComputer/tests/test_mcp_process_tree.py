"""Gap A — process-tree termination + orphan scanner (mcp-openclaw-port follow-up).

The MCP SDK (≥1.x) handles per-connection kill-tree via
``_terminate_process_tree`` + ``start_new_session=True`` so a normal
disconnect kills the subprocess and its children atomically. OC adds
two layers of defence-in-depth on top of that:

1. ``find_mcp_descendants(parent_pid)`` — psutil walk of OC's process
   tree filtered to MCP-server command signatures (``npx``, ``uvx``,
   ``python -m mcp_*``, plus the explicit list of preset commands).
2. ``kill_mcp_descendants(parent_pid, ...)`` — SIGTERM-with-grace then
   SIGKILL escalation, returns counts of (terminated, killed).

These run at OC startup (catch orphans from a prior crashed run) and
at clean shutdown (catch any subprocess the SDK's path missed).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from opencomputer.mcp.process_tree import (
    find_mcp_descendants,
    is_mcp_command,
    kill_mcp_descendants,
)


# ─── command signature detection ─────────────────────────────────


def test_npx_is_mcp_command() -> None:
    assert is_mcp_command(["npx", "-y", "@modelcontextprotocol/server-memory"])


def test_uvx_is_mcp_command() -> None:
    assert is_mcp_command(["uvx", "mcp-server-fetch"])


def test_python_module_is_mcp_command_when_mcp_module() -> None:
    assert is_mcp_command(["python3", "-m", "mcp_server_foo"])
    assert is_mcp_command(["python", "-m", "mcp.server"])


def test_python_module_NOT_mcp_command_when_unrelated() -> None:
    assert not is_mcp_command(["python3", "-m", "http.server"])


def test_random_command_not_mcp_command() -> None:
    assert not is_mcp_command(["/bin/ls"])
    assert not is_mcp_command(["bash", "-c", "echo hi"])


def test_empty_command_not_mcp_command() -> None:
    assert not is_mcp_command([])


def test_path_normalized_command_recognized() -> None:
    """``/usr/local/bin/npx`` is still an MCP command — basename wins."""
    assert is_mcp_command(["/usr/local/bin/npx", "-y", "x"])
    assert is_mcp_command(["/opt/uv/bin/uvx", "x"])


# ─── descendant enumeration ──────────────────────────────────────


def test_find_descendants_empty_for_no_children() -> None:
    found = find_mcp_descendants(os.getpid())
    assert isinstance(found, list)
    assert all(p.cmd_is_mcp for p in found)


def test_find_descendants_picks_no_match_for_plain_python_sleep() -> None:
    """A non-MCP-named subprocess is NOT picked up by the scanner."""
    proc = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
    )
    try:
        time.sleep(0.5)
        found = find_mcp_descendants(os.getpid())
        assert not any(p.pid == proc.pid for p in found)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_find_descendants_catches_real_npx_lookalike() -> None:
    """Spawn a subprocess whose argv[0] is ``npx`` — scanner picks it up."""
    if sys.platform == "win32":
        pytest.skip("POSIX-only argv[0] trick")
    proc = subprocess.Popen(
        ["bash", "-c", "exec -a npx sleep 30"],
    )
    try:
        time.sleep(0.5)
        found = find_mcp_descendants(os.getpid())
        matches = [p for p in found if p.pid == proc.pid or "npx" in (p.argv0 or "")]
        assert matches, (
            f"expected to find npx-named descendant, got "
            f"{[(p.pid, p.argv0) for p in found]}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_find_descendants_unknown_parent_pid_returns_empty() -> None:
    found = find_mcp_descendants(999_999)
    assert found == []


# ─── kill descendants — termination semantics ────────────────────


def test_kill_descendants_terminates_npx_lookalike() -> None:
    """SIGTERM the fake npx subprocess; assert it dies."""
    if sys.platform == "win32":
        pytest.skip("POSIX-only argv[0] trick")
    proc = subprocess.Popen(
        ["bash", "-c", "exec -a npx sleep 30"],
    )
    try:
        time.sleep(0.5)
        n_terminated, n_killed = kill_mcp_descendants(
            os.getpid(), graceful_seconds=2.0,
        )
        assert (n_terminated + n_killed) >= 1
        try:
            ec = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("subprocess survived kill_mcp_descendants")
        assert ec in (0, -signal.SIGTERM, -signal.SIGKILL, 137, 143)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_kill_descendants_returns_zero_when_none() -> None:
    """No MCP-named children → (0, 0). Idempotent + side-effect-free."""
    n_terminated, n_killed = kill_mcp_descendants(os.getpid())
    assert n_terminated == 0
    assert n_killed == 0


def test_kill_descendants_unknown_parent_pid_is_no_op() -> None:
    n_terminated, n_killed = kill_mcp_descendants(999_999)
    assert n_terminated == 0
    assert n_killed == 0
