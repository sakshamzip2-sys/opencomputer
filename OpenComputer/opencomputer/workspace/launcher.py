"""Spawn and manage the hermes-workspace Node subprocess.

Responsibilities:

* Build the enriched environment (``HERMES_API_URL``, ``HERMES_API_TOKEN``,
  ``PORT``, ``HOST``, ``NODE_ENV``, ``OPENCOMPUTER_HOME``).
* Spawn ``node server-entry.js`` with stdout/stderr inherited (so the
  user sees the Node logs interleaved with their CLI output).
* Health-check ``http://host:port/`` until the server responds 200 or
  the configured timeout elapses.
* Forward SIGINT / SIGTERM to the child cleanly: 5s grace then SIGKILL.

The launcher does NOT touch the dashboard — that's :mod:`lifecycle`'s
job. The launcher's contract is "spawn one Node process and keep it
alive until told to stop."
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import httpx

__all__ = [
    "LaunchSpec",
    "WorkspaceProcess",
    "LaunchFailed",
    "spawn_workspace",
]

logger = logging.getLogger("opencomputer.workspace.launcher")


class LaunchFailed(RuntimeError):  # noqa: N818 — matches IsolationFailed / BuildFailed naming in OC
    """Raised when the Node subprocess fails before health-check completes."""


@dataclass(frozen=True)
class LaunchSpec:
    """Inputs to :func:`spawn_workspace`."""

    workspace_dir: Path
    host: str
    port: int
    dashboard_url: str
    dashboard_token: str | None
    profile_home: Path
    node_path: str
    health_timeout_seconds: float = 60.0


class WorkspaceProcess:
    """Wraps a running Node subprocess + provides clean shutdown."""

    def __init__(
        self,
        *,
        process: subprocess.Popen[bytes],
        host: str,
        port: int,
    ) -> None:
        self._process = process
        self.host = host
        self.port = port
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    @property
    def pid(self) -> int:
        return self._process.pid

    def is_running(self) -> bool:
        return self._process.poll() is None

    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def wait(self) -> int:
        """Block until the subprocess exits; return its exit code."""
        return self._process.wait()

    def shutdown(self, grace_seconds: float = 5.0) -> int:
        """Send SIGTERM, wait ``grace_seconds``, then SIGKILL if needed.

        Idempotent: repeated calls are no-ops after the first.
        Returns the child's exit code (or ``-9`` if killed).
        """
        with self._shutdown_lock:
            if self._shutdown_done:
                # Subsequent callers just await the final code.
                return self._process.poll() or 0
            self._shutdown_done = True

        if self._process.poll() is not None:
            return self._process.returncode

        # Try graceful SIGTERM first. On Unix we kill the process group
        # so node + any children (vite dev server etc.) all stop.
        try:
            if os.name == "posix":
                pgid = os.getpgid(self._process.pid)
                os.killpg(pgid, signal.SIGTERM)
            else:
                self._process.terminate()
        except (ProcessLookupError, PermissionError, OSError) as exc:
            logger.debug("workspace shutdown: terminate raised %s", exc)

        try:
            return self._process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            logger.warning(
                "workspace: SIGTERM did not stop pid=%d after %.1fs; "
                "sending SIGKILL",
                self._process.pid,
                grace_seconds,
            )

        try:
            if os.name == "posix":
                pgid = os.getpgid(self._process.pid)
                os.killpg(pgid, signal.SIGKILL)
            else:
                self._process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

        try:
            return self._process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            logger.error(
                "workspace: pid=%d did not exit after SIGKILL", self._process.pid,
            )
            return -9


def _port_in_use(host: str, port: int) -> bool:
    """Return True iff ``host:port`` already accepts TCP connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def _build_env(spec: LaunchSpec) -> dict[str, str]:
    env = os.environ.copy()
    env["PORT"] = str(spec.port)
    env["HOST"] = spec.host
    env["NODE_ENV"] = env.get("NODE_ENV", "production")
    env["HERMES_API_URL"] = spec.dashboard_url
    if spec.dashboard_token:
        env["HERMES_API_TOKEN"] = spec.dashboard_token
    # Surface the active profile for any Node-side helpers that want it.
    env["OPENCOMPUTER_HOME"] = str(spec.profile_home)
    env["OC_PROFILE_HOME"] = str(spec.profile_home)
    # The workspace's server-entry.js refuses non-loopback HOST without
    # HERMES_PASSWORD. We respect that. If user is binding to 0.0.0.0
    # they MUST set HERMES_PASSWORD themselves; we don't paper over it.
    return env


def _await_health(
    host: str,
    port: int,
    *,
    timeout: float,
    process: subprocess.Popen[bytes],
) -> None:
    """Poll ``http://host:port/`` until it responds 200 or we time out.

    Raises :class:`LaunchFailed` if the subprocess exits before health
    completes (caller surfaces stderr).
    """
    deadline = time.monotonic() + timeout
    last_error: str = ""
    probe_url = f"http://{host}:{port}/"
    poll_interval = 0.5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LaunchFailed(
                f"workspace node process exited with code "
                f"{process.returncode} before health-check completed"
            )
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(probe_url, follow_redirects=False)
            if resp.status_code < 500:
                # Any non-5xx means the server is up and routing.
                # /  serves HTML; 200 expected but 301/302 also fine.
                return
            last_error = f"HTTP {resp.status_code}"
        except (httpx.ConnectError, httpx.ReadError, OSError) as exc:
            last_error = str(exc) or exc.__class__.__name__
        time.sleep(poll_interval)
        # Back off a touch on long waits but cap at 2s to keep boot time tight.
        poll_interval = min(poll_interval * 1.2, 2.0)
    raise LaunchFailed(
        f"workspace did not respond at {probe_url} within {timeout:.0f}s "
        f"(last error: {last_error})"
    )


def spawn_workspace(spec: LaunchSpec) -> WorkspaceProcess:
    """Spawn the Node server and block until it passes health-check.

    Raises:
        LaunchFailed: if Node exits early or health-check times out.
        RuntimeError: if the port is already in use.
        FileNotFoundError: if ``spec.node_path`` doesn't resolve.
    """
    if not Path(spec.node_path).is_file():
        resolved = shutil.which(spec.node_path)
        if not resolved:
            raise FileNotFoundError(
                f"node binary not found at {spec.node_path!r}"
            )
        spec = LaunchSpec(  # type: ignore[misc]
            workspace_dir=spec.workspace_dir,
            host=spec.host,
            port=spec.port,
            dashboard_url=spec.dashboard_url,
            dashboard_token=spec.dashboard_token,
            profile_home=spec.profile_home,
            node_path=resolved,
            health_timeout_seconds=spec.health_timeout_seconds,
        )

    if _port_in_use(spec.host, spec.port):
        raise RuntimeError(
            f"port {spec.port} on {spec.host} is already in use — pass "
            "--port to choose a different one"
        )

    entry = spec.workspace_dir / "server-entry.js"
    if not entry.is_file():
        raise FileNotFoundError(
            f"server-entry.js missing in {spec.workspace_dir}"
        )
    dist_server = spec.workspace_dir / "dist" / "server" / "server.js"
    if not dist_server.is_file():
        raise FileNotFoundError(
            f"dist/server/server.js missing in {spec.workspace_dir} — "
            "run `oc workspace build` first"
        )

    env = _build_env(spec)
    cmd = [spec.node_path, "server-entry.js"]
    # ``start_new_session=True`` puts the child in its own process group
    # so SIGINT to the parent doesn't double-deliver and we can kill the
    # whole group on shutdown. Inherit stdout/stderr so the user sees
    # Node logs live.
    popen_kwargs: dict[str, object] = {
        "cwd": str(spec.workspace_dir),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": sys.stdout.fileno() if isinstance(sys.stdout, IO) else None,
        "stderr": sys.stderr.fileno() if isinstance(sys.stderr, IO) else None,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    process = subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[arg-type]
    logger.info(
        "workspace: spawned node pid=%d at %s:%d",
        process.pid,
        spec.host,
        spec.port,
    )
    try:
        _await_health(
            spec.host,
            spec.port,
            timeout=spec.health_timeout_seconds,
            process=process,
        )
    except LaunchFailed:
        # Health failed — kill the child cleanly before raising.
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        raise
    return WorkspaceProcess(
        process=process, host=spec.host, port=spec.port,
    )
