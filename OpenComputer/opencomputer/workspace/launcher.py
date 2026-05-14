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


def _find_orphan_workspace_holder(
    workspace_dir: Path, port: int  # noqa: ARG001 — kept for signature stability
) -> tuple[int, str] | None:
    """Return (pid, cmdline) of an orphaned workspace server matching us.

    Caller has already confirmed ``port`` is bound; this function identifies
    whether the holder is a stale ``node server-entry.js`` from OUR workspace
    directory so we can safely reclaim the port. We deliberately do NOT call
    :func:`psutil.net_connections` — macOS sandboxes that to root and would
    raise :class:`psutil.AccessDenied` on every non-priv call. Instead we
    iterate processes and match on cmdline + cwd, which needs no special
    capabilities for processes owned by the current user.

    Returns ``None`` if no qualifying process is found OR if psutil isn't
    importable (we degrade silently rather than mask the underlying error).
    """
    try:
        import psutil  # local import keeps cold-start cost off the hot path
    except Exception:  # noqa: BLE001
        return None

    workspace_path = workspace_dir.resolve()
    candidates: list[tuple[int, str]] = []
    for proc in psutil.process_iter(attrs=["pid", "name", "cmdline", "ppid"]):
        try:
            cmdline_parts = proc.info.get("cmdline") or []
            if not cmdline_parts:
                continue
            name = (proc.info.get("name") or "").lower()
            if "node" not in name and not any(
                "node" in part.lower() for part in cmdline_parts[:1]
            ):
                continue
            cmdline = " ".join(cmdline_parts)
            # Must be a node server-entry.js — anything else (e.g. a user's
            # own dev server) we leave well alone.
            if "server-entry.js" not in cmdline:
                continue
            # Match cwd to our workspace_dir so we never kill an unrelated
            # workspace running from a different checkout.
            try:
                cwd = Path(proc.cwd()).resolve()
            except (psutil.AccessDenied, OSError):
                # Can't verify cwd → skip rather than risk killing the
                # wrong process. This is the safer failure mode.
                continue
            if cwd != workspace_path:
                continue
            # CRITICAL: only flag as "orphan" if the parent is gone. A node
            # workspace with a live `oc workspace` python parent is a
            # HEALTHY session (e.g. running in another terminal); killing
            # it would be a footgun. Orphans are reparented to launchd
            # (PID 1) when their python parent dies without cleanup.
            ppid = proc.info.get("ppid")
            if ppid is None:
                # Can't determine parentage → skip rather than risk
                # killing a live session.
                continue
            if ppid != 1:
                # Has a live parent — not an orphan. Don't touch it.
                # The user has another `oc workspace` running; they need
                # to stop it themselves or use a different --port.
                continue
            candidates.append((proc.info["pid"], cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not candidates:
        return None
    # Multiple matches would mean multiple stale servers from the same
    # workspace dir — return the first; the caller's terminate path will be
    # called again on the next port-in-use check if any survive.
    return candidates[0]


def _terminate_orphan(pid: int, timeout: float = 3.0) -> bool:
    """Politely terminate the orphan, escalating to SIGKILL if it lingers.

    Returns True if the process is gone (or never existed) at the end.
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            os.kill(pid, 0)  # raises if dead
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2.0)
            return True
        except Exception:  # noqa: BLE001
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


def _build_env(spec: LaunchSpec) -> dict[str, str]:
    env = os.environ.copy()
    env["PORT"] = str(spec.port)
    env["HOST"] = spec.host
    env["NODE_ENV"] = env.get("NODE_ENV", "production")
    # The workspace separates "gateway" (chat completions, OpenAI compat)
    # from "dashboard" (sessions, skills, jobs, MCP) and probes BOTH
    # independently at startup. In OC's world both surfaces live on the
    # same FastAPI app, so we point both URLs at the same place. Without
    # the dashboard URL the workspace defaults to its upstream's 9119,
    # which on an OC-only install is unbound and yields a noisy
    # "dashboard unavailable" banner.
    env["HERMES_API_URL"] = spec.dashboard_url
    env["HERMES_DASHBOARD_URL"] = spec.dashboard_url
    if spec.dashboard_token:
        env["HERMES_API_TOKEN"] = spec.dashboard_token
        # Mirror as CLAUDE_DASHBOARD_TOKEN — that's the env var the
        # workspace's gateway-capabilities layer reads for the dashboard
        # Bearer header (see workspace's #124 migration). Without this,
        # the dashboard probe falls back to the legacy HTML-scrape token
        # flow and prints a deprecation warning every boot.
        env["CLAUDE_DASHBOARD_TOKEN"] = spec.dashboard_token
        env["CLAUDE_API_TOKEN"] = spec.dashboard_token
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
    completes (caller surfaces stderr) OR if no response arrives within
    ``timeout`` seconds.

    Exception handling is INTENTIONALLY broad: any httpx-side error
    (ConnectError / ReadError / TimeoutException family / etc.) means
    "not ready yet, retry". Letting ``httpx.ReadTimeout`` or
    ``httpx.ConnectTimeout`` escape would propagate uncaught into the
    lifecycle's narrow exception handler and orphan the Node process —
    the bug behind the 2026-05-12 "error: timed out" report.
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
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(probe_url, follow_redirects=False)
            if resp.status_code < 500:
                # Any non-5xx means the server is up and routing.
                # /  serves HTML; 200 expected but 301/302 also fine.
                return
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 — broad on purpose, see docstring
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
        # Most of the "port already in use" reports trace back to a previous
        # ``oc workspace`` shell that didn't run cleanup — see
        # ``lifecycle._install_signal_handlers`` for the SIGHUP fix that
        # prevents new orphans. For pre-existing orphans (and future
        # SIGKILL'd parents) we identify and offer to clean ours up so the
        # user isn't stuck running ``lsof | grep 3000 | kill`` by hand.
        orphan = _find_orphan_workspace_holder(spec.workspace_dir, spec.port)
        if orphan is None:
            raise RuntimeError(
                f"port {spec.port} on {spec.host} is already in use — pass "
                "--port to choose a different one"
            )
        pid, cmdline = orphan
        kill_orphan = os.environ.get("OC_WORKSPACE_KILL_ORPHAN", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if not kill_orphan:
            raise RuntimeError(
                f"port {spec.port} on {spec.host} is held by a stale workspace "
                f"server (pid={pid}: {cmdline[:80]}). This is almost certainly a "
                f"leftover from a previous `oc workspace` shell that closed "
                f"without cleanup. Re-run with OC_WORKSPACE_KILL_ORPHAN=1 to "
                f"reclaim the port automatically, or run "
                f"`kill {pid}` first."
            )
        logger.warning(
            "workspace: reclaiming port %d from stale server pid=%d",
            spec.port,
            pid,
        )
        if not _terminate_orphan(pid):
            raise RuntimeError(
                f"failed to terminate stale workspace server pid={pid} on port "
                f"{spec.port}; kill it manually with `kill -9 {pid}` and retry"
            )
        # Give the kernel a moment to release the socket.
        for _ in range(10):
            if not _port_in_use(spec.host, spec.port):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(
                f"port {spec.port} still bound after terminating pid={pid}; "
                f"another process may be racing for it"
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
    except BaseException:
        # ANY failure (LaunchFailed, KeyboardInterrupt, an unexpected
        # exception from httpx, signal-driven cancellation) must kill the
        # child cleanly — otherwise the node process is orphaned, holding
        # the workspace port and presenting a dead UI. 2026-05-12 bug:
        # an unhandled httpx.ReadTimeout escaped here and left node
        # running on :3000 with the python parent dead.
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=3.0)
        except Exception:  # noqa: BLE001
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        raise
    return WorkspaceProcess(
        process=process, host=spec.host, port=spec.port,
    )
