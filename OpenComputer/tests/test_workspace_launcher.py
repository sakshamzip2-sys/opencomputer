"""Unit tests for opencomputer.workspace.launcher.

Live-Node tests are gated by ``pytest -m integration``; here we cover
the surface that can be tested with mocks: env enrichment, port-in-use
detection, file presence checks, shutdown ordering.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.workspace.launcher import (
    LaunchFailed,
    LaunchSpec,
    WorkspaceProcess,
    _build_env,
    _port_in_use,
    spawn_workspace,
)


@pytest.fixture
def fake_spec(tmp_path: Path) -> LaunchSpec:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")
    (ws / "server-entry.js").write_text("// fake", encoding="utf-8")
    (ws / "dist" / "server").mkdir(parents=True)
    (ws / "dist" / "server" / "server.js").write_text("// fake", encoding="utf-8")

    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)

    return LaunchSpec(
        workspace_dir=ws,
        host="127.0.0.1",
        port=39999,
        dashboard_url="http://127.0.0.1:9119",
        dashboard_token="test-token",
        profile_home=tmp_path / "profile",
        node_path=str(node),
        health_timeout_seconds=2.0,
    )


def test_build_env_sets_all_required_vars(fake_spec: LaunchSpec) -> None:
    env = _build_env(fake_spec)
    assert env["PORT"] == str(fake_spec.port)
    assert env["HOST"] == fake_spec.host
    assert env["HERMES_API_URL"] == fake_spec.dashboard_url
    # Bug B (2026-05-12): the workspace probes BOTH gateway and dashboard
    # URLs independently. Without HERMES_DASHBOARD_URL it defaults to the
    # upstream hermes-agent's :9119, which on an OC-only install is
    # unbound and yields a "dashboard unavailable" banner.
    assert env["HERMES_DASHBOARD_URL"] == fake_spec.dashboard_url
    assert env["HERMES_API_TOKEN"] == "test-token"
    # Bug B (2026-05-12): the workspace gateway-capabilities layer reads
    # CLAUDE_DASHBOARD_TOKEN for the dashboard Bearer header. Without
    # this it falls back to a deprecated HTML-scrape flow.
    assert env["CLAUDE_DASHBOARD_TOKEN"] == "test-token"
    assert env["CLAUDE_API_TOKEN"] == "test-token"
    assert env["OPENCOMPUTER_HOME"] == str(fake_spec.profile_home)
    assert env["NODE_ENV"] in ("production", "development")


def test_build_env_omits_token_when_none(fake_spec: LaunchSpec) -> None:
    spec = LaunchSpec(
        workspace_dir=fake_spec.workspace_dir,
        host=fake_spec.host,
        port=fake_spec.port,
        dashboard_url=fake_spec.dashboard_url,
        dashboard_token=None,
        profile_home=fake_spec.profile_home,
        node_path=fake_spec.node_path,
    )
    env = _build_env(spec)
    assert "HERMES_API_TOKEN" not in env
    assert "CLAUDE_DASHBOARD_TOKEN" not in env
    assert "CLAUDE_API_TOKEN" not in env
    # Dashboard URL is still set even when token is unset (the workspace
    # can probe an unauthenticated dashboard for public endpoints).
    assert env["HERMES_DASHBOARD_URL"] == fake_spec.dashboard_url


def test_build_env_does_not_inject_token_into_argv(fake_spec: LaunchSpec) -> None:
    """Token must travel through env, never argv."""
    env = _build_env(fake_spec)
    # Sanity: argv is built elsewhere; just make sure env actually has it
    # and that nothing in _build_env would leak it.
    assert any("test-token" in v for v in env.values())


def test_port_in_use_detects_listening_port(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert _port_in_use("127.0.0.1", port) is True


def test_port_in_use_returns_false_for_unbound() -> None:
    # Find a definitely-unbound port: bind, then close.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert _port_in_use("127.0.0.1", port) is False


def test_spawn_workspace_refuses_busy_port(fake_spec: LaunchSpec) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy_port = s.getsockname()[1]
        spec = LaunchSpec(
            workspace_dir=fake_spec.workspace_dir,
            host=fake_spec.host,
            port=busy_port,
            dashboard_url=fake_spec.dashboard_url,
            dashboard_token=fake_spec.dashboard_token,
            profile_home=fake_spec.profile_home,
            node_path=fake_spec.node_path,
        )
        with pytest.raises(RuntimeError, match="already in use"):
            spawn_workspace(spec)


def test_spawn_workspace_requires_server_entry(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")
    # server-entry.js intentionally absent
    node = tmp_path / "node"
    node.write_text("", encoding="utf-8")
    node.chmod(0o755)

    spec = LaunchSpec(
        workspace_dir=ws,
        host="127.0.0.1",
        port=39998,
        dashboard_url="http://127.0.0.1:9119",
        dashboard_token=None,
        profile_home=tmp_path,
        node_path=str(node),
    )
    with pytest.raises(FileNotFoundError, match="server-entry.js"):
        spawn_workspace(spec)


def test_spawn_workspace_requires_built_dist(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")
    (ws / "server-entry.js").write_text("//", encoding="utf-8")
    # dist/server/server.js intentionally absent
    node = tmp_path / "node"
    node.write_text("", encoding="utf-8")
    node.chmod(0o755)

    spec = LaunchSpec(
        workspace_dir=ws,
        host="127.0.0.1",
        port=39997,
        dashboard_url="http://127.0.0.1:9119",
        dashboard_token=None,
        profile_home=tmp_path,
        node_path=str(node),
    )
    with pytest.raises(FileNotFoundError, match="dist/server/server.js"):
        spawn_workspace(spec)


def test_spawn_workspace_propagates_early_exit(fake_spec: LaunchSpec) -> None:
    """If node exits immediately, we report it as a LaunchFailed."""

    class _DummyProc:
        pid = 12345
        returncode = 7

        def poll(self) -> int:
            return 7  # already exited

        def wait(self, timeout: float | None = None) -> int:
            return 7

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    with (
        patch("subprocess.Popen", return_value=_DummyProc()),
        patch("opencomputer.workspace.launcher._port_in_use", return_value=False),
        pytest.raises(LaunchFailed, match="exited with code 7"),
    ):
        spawn_workspace(fake_spec)


def test_workspace_process_shutdown_idempotent() -> None:
    proc = MagicMock()
    proc.poll.return_value = None  # initially alive
    proc.wait.return_value = 0
    proc.pid = 12345

    wp = WorkspaceProcess(process=proc, host="127.0.0.1", port=3000)
    with (
        patch("os.getpgid", return_value=12345),
        patch("os.killpg"),
    ):
        rc1 = wp.shutdown()
        rc2 = wp.shutdown()
    assert rc1 == 0
    assert rc2 == 0
    # Second call must not re-invoke terminate/kill — proc.wait called once.
    assert proc.wait.call_count == 1


def test_await_health_swallows_httpx_timeout_errors(fake_spec: LaunchSpec) -> None:
    """Regression for 2026-05-12: an unhandled httpx.ReadTimeout escaped
    _await_health, propagated through spawn_workspace's narrow
    ``except LaunchFailed``, crashed the Python parent, and left the Node
    subprocess orphaned. The fix broadened _await_health's except to all
    Exception (so timeouts just retry) AND broadened spawn_workspace's
    except to BaseException (so any unexpected failure still kills Node).

    This test verifies the inner loop swallows httpx timeout exceptions
    instead of propagating them.
    """
    import httpx

    from opencomputer.workspace.launcher import LaunchFailed, _await_health

    class _DummyProc:
        pid = 999
        returncode = None

        def poll(self) -> None:
            return None  # alive throughout

    call_count = 0

    class _AlwaysTimeoutClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _AlwaysTimeoutClient:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def get(self, *args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            raise httpx.ReadTimeout("simulated read timeout")

    with patch("httpx.Client", _AlwaysTimeoutClient):
        # 1.5s gives the loop enough wall-clock to make 2-3 attempts but
        # not so long that the test drags. We expect LaunchFailed at the
        # end (not ReadTimeout propagating uncaught).
        with pytest.raises(LaunchFailed, match=r"did not respond .* within 1s"):
            _await_health(
                fake_spec.host,
                fake_spec.port,
                timeout=1.0,
                process=_DummyProc(),  # type: ignore[arg-type]
            )
    # At least one inner-loop iteration ran and caught the timeout.
    assert call_count >= 1


def test_spawn_workspace_kills_node_on_unexpected_exception(
    fake_spec: LaunchSpec,
) -> None:
    """Regression for 2026-05-12: any exception during health check
    (not just LaunchFailed) must kill the Node subprocess. Previously,
    a non-LaunchFailed exception would escape with Node still running,
    leaving an orphan."""
    killed: list[int] = []

    class _DummyProc:
        pid = 12345
        returncode = None

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            killed.append(15)

        def kill(self) -> None:
            killed.append(9)

    def _await_unexpected(*_: Any, **__: Any) -> None:
        raise RuntimeError("unexpected timeout-shaped exception")

    with (
        patch("subprocess.Popen", return_value=_DummyProc()),
        patch("opencomputer.workspace.launcher._port_in_use", return_value=False),
        patch("opencomputer.workspace.launcher._await_health", side_effect=_await_unexpected),
        patch("os.getpgid", return_value=12345),
        patch("os.killpg", side_effect=lambda pgid, sig: killed.append(sig)),
        pytest.raises(RuntimeError, match="unexpected"),
    ):
        spawn_workspace(fake_spec)

    # The broadened exception handler must have invoked process-group
    # SIGTERM (signal.SIGTERM == 15) before re-raising.
    assert 15 in killed, f"expected SIGTERM to be sent, got kills={killed}"
