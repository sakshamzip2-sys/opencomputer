"""Coordinate the dashboard + workspace boot order.

The contract:

1. Start a fresh in-process :class:`DashboardServer` thread on
   ``dashboard_port`` bound to ``dashboard_host`` (defaults
   ``127.0.0.1:9119``). Capture its session token.
2. Health-check ``GET /api/health`` until 200.
3. Spawn the Node workspace via :func:`launcher.spawn_workspace`
   (which itself health-checks).
4. Optionally open the browser.
5. Block on the workspace subprocess. Forward SIGINT/SIGTERM.
6. On exit, stop the dashboard thread.

If the dashboard port is in use, we refuse to start (the audit-plan
decision: don't attempt to reuse an existing dashboard, because the
session token is per-process and not on-disk in v1).
"""

from __future__ import annotations

import logging
import signal
import socket
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from opencomputer.workspace.launcher import (
    LaunchSpec,
    WorkspaceProcess,
    spawn_workspace,
)

__all__ = [
    "LifecycleConfig",
    "WorkspaceLifecycle",
    "DashboardPortInUse",
]

logger = logging.getLogger("opencomputer.workspace.lifecycle")


class DashboardPortInUse(RuntimeError):  # noqa: N818 — matches SubagentStoreUnavailable naming in OC
    """Raised when the requested dashboard port is already bound."""


@dataclass(frozen=True)
class LifecycleConfig:
    """Inputs to :class:`WorkspaceLifecycle`."""

    workspace_dir: Path
    profile_home: Path
    workspace_host: str = "127.0.0.1"
    workspace_port: int = 3000
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 9119
    node_path: str = "node"
    open_browser: bool = True
    health_timeout_seconds: float = 60.0


def _port_bound(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


class WorkspaceLifecycle:
    """Owns the dashboard thread + the workspace subprocess + shutdown."""

    def __init__(self, config: LifecycleConfig) -> None:
        self.config = config
        self._dashboard: Any = None  # DashboardServer (lazy import)
        self._workspace: WorkspaceProcess | None = None
        self._dashboard_token: str | None = None
        self._stop_requested = threading.Event()

    # --- dashboard ----------------------------------------------------

    def _start_dashboard(self) -> None:
        if _port_bound(self.config.dashboard_host, self.config.dashboard_port):
            raise DashboardPortInUse(
                f"dashboard port {self.config.dashboard_port} on "
                f"{self.config.dashboard_host} is already in use. Pass "
                "--dashboard-port to choose a free one, or stop the "
                "existing process."
            )

        from opencomputer.dashboard.server import DashboardServer

        server = DashboardServer(
            host=self.config.dashboard_host,
            port=self.config.dashboard_port,
        )
        server.start()  # daemon thread inside DashboardServer
        self._dashboard = server

        # Pull the ephemeral session token off the FastAPI app for the
        # workspace's HERMES_API_TOKEN env var. The DashboardServer
        # constructs the app eagerly in start(); the token lives on
        # ``app.state.session_token``.
        try:
            self._dashboard_token = getattr(
                server.app.state, "session_token", None
            )
        except AttributeError:
            self._dashboard_token = None
        if not self._dashboard_token:
            logger.warning(
                "workspace lifecycle: dashboard started without a session "
                "token — chat completions will be open to any local caller"
            )

        # Block until /api/health responds.
        self._await_dashboard_health()

    def _await_dashboard_health(self) -> None:
        url = (
            f"http://{self.config.dashboard_host}:"
            f"{self.config.dashboard_port}/api/health"
        )
        deadline = time.monotonic() + 30.0
        last_err: str = ""
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=2.0) as c:
                    r = c.get(url)
                if r.status_code == 200:
                    return
                last_err = f"HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            time.sleep(0.3)
        raise RuntimeError(
            f"dashboard did not become ready at {url} within 30s "
            f"(last error: {last_err})"
        )

    def _stop_dashboard(self) -> None:
        if self._dashboard is None:
            return
        try:
            self._dashboard.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("workspace lifecycle: dashboard stop raised %s", exc)
        finally:
            self._dashboard = None

    # --- workspace ----------------------------------------------------

    def _start_workspace(self) -> None:
        spec = LaunchSpec(
            workspace_dir=self.config.workspace_dir,
            host=self.config.workspace_host,
            port=self.config.workspace_port,
            dashboard_url=(
                f"http://{self.config.dashboard_host}:"
                f"{self.config.dashboard_port}"
            ),
            dashboard_token=self._dashboard_token,
            profile_home=self.config.profile_home,
            node_path=self.config.node_path,
            health_timeout_seconds=self.config.health_timeout_seconds,
        )
        self._workspace = spawn_workspace(spec)

    def _open_browser_if_requested(self) -> None:
        if not self.config.open_browser:
            return
        if self._workspace is None:
            return
        url = self._workspace.url()
        try:
            webbrowser.open(url, new=1, autoraise=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workspace lifecycle: webbrowser.open(%s) raised %s — "
                "open the URL manually", url, exc,
            )

    # --- signal handling ---------------------------------------------

    @contextmanager
    def _install_signal_handlers(self) -> Any:
        previous: dict[int, Any] = {}

        def _on_signal(signum: int, frame: Any) -> None:
            logger.info(
                "workspace lifecycle: signal %s received; shutting down",
                signal.Signals(signum).name if signum else signum,
            )
            self._stop_requested.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                # Not in main thread or unsupported on platform —
                # caller has to drive shutdown some other way.
                pass
        try:
            yield
        finally:
            for sig, handler in previous.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass

    # --- public entrypoint -------------------------------------------

    def run(self) -> int:
        """Start everything, block until workspace exits, return exit code.

        Stops the dashboard thread before returning regardless of exit path.
        """
        exit_code = 0
        try:
            self._start_dashboard()
        except DashboardPortInUse:
            raise
        except Exception:
            self._stop_dashboard()
            raise

        try:
            self._start_workspace()
        except BaseException:
            # Broad on purpose: any failure (LaunchFailed,
            # FileNotFoundError, RuntimeError, KeyboardInterrupt, an
            # unexpected exception) must stop the dashboard thread.
            # Without this the dashboard would silently linger if a
            # caller hit Ctrl-C during workspace boot. (2026-05-12 audit
            # follow-up — paired with the launcher's broadened catch
            # that now kills Node on every failure path.)
            self._stop_dashboard()
            raise

        self._open_browser_if_requested()

        assert self._workspace is not None
        try:
            with self._install_signal_handlers():
                # Sleep-poll: wake on a signal OR when the workspace child
                # exits on its own.
                while not self._stop_requested.is_set():
                    if not self._workspace.is_running():
                        break
                    time.sleep(0.5)
        finally:
            if self._workspace and self._workspace.is_running():
                code = self._workspace.shutdown()
                if exit_code == 0:
                    exit_code = code if code >= 0 else 0
            elif self._workspace is not None:
                code = self._workspace._process.returncode
                if code is not None and exit_code == 0:
                    exit_code = code
            self._stop_dashboard()
        return exit_code
