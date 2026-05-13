"""Open-design daemon lifecycle — start/stop/status with PID + log files.

The plugin spawns the open-design Node daemon (``apps/daemon/dist/cli.js``)
as a long-running child process. We track it via a profile-scoped PID
file under ``~/.opencomputer/<profile>/locks/open-design.pid`` and write
its stdout/stderr to ``~/.opencomputer/<profile>/logs/open-design.log``.

Source-tree discovery rules (in order):
  1. ``OPEN_DESIGN_HOME`` env var (explicit override)
  2. ``~/Vscode/claude/open-design`` (saksham's local dev path)
  3. ``~/.open-design`` (system-wide install convention)
  4. ``/usr/local/share/open-design`` (linux convention)

If none resolve to a directory containing ``apps/daemon/dist/cli.js``,
:func:`resolve_open_design_home` returns ``None`` and start/status raise
:class:`OpenDesignNotInstalledError`.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugin_sdk.profile_context import current_profile_home as _current_profile_home_var

_log = logging.getLogger("opencomputer.open_design.lifecycle")

DEFAULT_PORT = 7456
DAEMON_REL_PATH = "apps/daemon/dist/cli.js"
#: The daemon serves the built Next.js SPA from ``apps/web/out`` via
#: ``express.static`` (see ``apps/daemon/src/server.ts``: STATIC_DIR =
#: PROJECT_ROOT/apps/web/out). If ``out/`` is missing, GET / returns
#: "Cannot GET /" — the daemon is alive but the UI is unbuilt. Doctor
#: flags this and the iframe-wrapper shows an actionable hint.
WEB_OUT_REL_PATH = "apps/web/out"
WEB_INDEX_REL_PATH = "apps/web/out/index.html"
#: Paths we probe for daemon liveness. Open-design's HTTP surface doesn't
#: expose a canonical health endpoint (POSTs to /api/* are real routes,
#: but GET / and GET /api/* may 404). Any HTTP response — including 404 —
#: means the listener is alive; that's what we treat as "running".
HEALTH_PROBE_PATHS = ("/", "/api/", "/api/healthz", "/api/status")

#: Inclusive port range we accept for OD_PORT. Below 1024 = privileged;
#: above 65535 = invalid. We refuse to spawn outside this range rather
#: than letting Node raise EACCES / RangeError downstream.
MIN_PORT = 1024
MAX_PORT = 65_535

#: Daemon-spawn watchdog. Five seconds is the upper bound after which
#: we declare the start failed; the daemon should print "listening on
#: port N" within ~1s in practice but we give 5x headroom for cold
#: starts (better-sqlite3 native load, esm cache miss, etc.).
SPAWN_WATCHDOG_S = 5.0

#: Stop watchdog. SIGTERM → wait up to 5s → SIGKILL.
STOP_WATCHDOG_S = 5.0


class OpenDesignNotInstalledError(RuntimeError):
    """open-design source tree not found at any known location."""


class DaemonAlreadyRunningError(RuntimeError):
    """Daemon already running (live PID file)."""


class PortInUseError(RuntimeError):
    """Configured port is held by another (unknown) process.

    Distinct from :class:`DaemonAlreadyRunningError` because the
    incumbent owner is not under our PID-file tracking — we can't kill
    it via ``oc design stop``. The user has to identify and stop the
    foreign process themselves (e.g. via ``lsof -i :7456``).
    """


@dataclass(frozen=True, slots=True)
class DaemonStatus:
    """Snapshot of daemon + SPA readiness.

    ``running`` means the HTTP listener is up. ``web_served`` means
    ``GET /`` returns the built SPA, not "Cannot GET /". Both must be
    true for the Hermes Design tab to embed the iframe cleanly.
    """

    running: bool
    pid: int | None
    port: int
    url: str
    home: Path | None
    log_path: Path
    web_served: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "port": self.port,
            "url": self.url,
            "home": str(self.home) if self.home else None,
            "log_path": str(self.log_path),
            "web_served": self.web_served,
            "error": self.error,
        }


def _candidate_homes() -> list[Path]:
    """Return paths to probe, in priority order.

    If ``OPEN_DESIGN_HOME`` is set, it is the **only** candidate — an
    explicit env var must not silently fall back to a system default
    (would mask typos / wrong-laptop misconfigs). If unset, we try the
    well-known locations in turn.
    """
    env = os.environ.get("OPEN_DESIGN_HOME")
    if env:
        return [Path(env).expanduser().resolve()]
    home = Path.home()
    return [
        home / "Vscode" / "claude" / "open-design",
        home / ".open-design",
        Path("/usr/local/share/open-design"),
    ]


def resolve_open_design_home() -> Path | None:
    """Return the first candidate that looks like an open-design tree.

    A candidate qualifies if it contains either the *built* daemon
    entry (``apps/daemon/dist/cli.js``) or the *source* package manifest
    (``apps/daemon/package.json``). The unbuilt case surfaces a clear
    "not built" error at :func:`start` rather than a confusing
    "not found" mid-launch.
    """
    for candidate in _candidate_homes():
        if (candidate / DAEMON_REL_PATH).is_file():
            return candidate
        if (candidate / "apps" / "daemon" / "package.json").is_file():
            return candidate
    return None


def _validate_port(port: int, *, source: str) -> int:
    """Clamp the port to the safe range, logging when we override.

    Returns the validated port. Refuses to *raise* — we substitute the
    default rather than crashing because OD_PORT typos shouldn't take
    down ``oc design status``.
    """
    if port < MIN_PORT or port > MAX_PORT:
        _log.warning(
            "%s=%d outside safe range [%d, %d]; falling back to %d",
            source, port, MIN_PORT, MAX_PORT, DEFAULT_PORT,
        )
        return DEFAULT_PORT
    return port


def _resolve_port() -> int:
    raw = os.environ.get("OD_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return _validate_port(int(raw), source="OD_PORT")
    except ValueError:
        _log.warning("OD_PORT=%r is not an integer; falling back to %d", raw, DEFAULT_PORT)
        return DEFAULT_PORT


def _profile_home() -> Path:
    """Resolve the active OC profile home with three-tier fallback.

    Plugins normally read this through the ContextVar (set by
    ``gateway/dispatch.py`` once a request is bound to a profile).
    Outside a request scope — e.g. when ``oc design start`` runs from
    the CLI without an active session — we fall back to the
    ``OPENCOMPUTER_HOME`` env var and finally to the ``default`` profile
    under ``~/.opencomputer/``.
    """
    home = _current_profile_home_var.get()
    if home is not None:
        return Path(home)
    env = os.environ.get("OPENCOMPUTER_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".opencomputer" / "default"


def _profile_path(*parts: str) -> Path:
    return _profile_home().joinpath(*parts)


def _pid_file() -> Path:
    return _profile_path("locks", "open-design.pid")


def _log_file() -> Path:
    return _profile_path("logs", "open-design.log")


def _read_pid() -> int | None:
    pid_path = _pid_file()
    if not pid_path.is_file():
        return None
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _is_alive(pid: int) -> bool:
    """POSIX kill(pid, 0) — returns True if process exists and we can signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is not ours — treat as alive (don't double-spawn)
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True


def _clean_stale_pid() -> None:
    pid = _read_pid()
    if pid is not None and not _is_alive(pid):
        try:
            _pid_file().unlink()
            _log.debug("open-design: cleaned stale pid file (pid=%s)", pid)
        except OSError:
            pass


def _port_in_use(port: int, *, host: str = "127.0.0.1") -> bool:
    """True if a TCP listener already holds ``host:port``.

    Uses a non-blocking bind probe (SO_REUSEADDR=0 by default on macOS
    and Linux) — fast, no privileges needed. We do this rather than a
    full ``lsof`` call so the check stays portable and millisecond-fast.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.25)
        sock.bind((host, port))
    except OSError:
        return True
    finally:
        sock.close()
    return False


def _probe_url(url: str, timeout: float = 0.5) -> bool:
    """Lightweight HTTP HEAD probe — daemon healthy if any 2xx/3xx/4xx replies.

    A connection refusal means not running; any HTTP response (even 404)
    means the daemon's listener is up.
    """
    import urllib.error
    import urllib.request

    for path in HEALTH_PROBE_PATHS:
        try:
            req = urllib.request.Request(url + path, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _ = resp.status
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return False


def _probe_spa_index(url: str, timeout: float = 0.5) -> bool:
    """GET / and verify it returns the SPA, not the daemon's "Cannot GET /".

    The daemon's express.static middleware only fires when
    ``apps/web/out/index.html`` exists. If the SPA isn't built, GET /
    falls through to Express's default 404 with text "Cannot GET /".
    We probe by GET-ing / and checking for an HTML response — a 404
    text/plain response or a non-HTML 200 both mean "UI missing".
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url + "/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            content_type = (resp.headers.get("content-type") or "").lower()
            if "html" not in content_type:
                return False
            body_head = resp.read(512).decode("utf-8", errors="ignore").lower()
            # Heuristic: real SPA serves <!doctype html ...> or <html ...>
            # Daemon 404 body is exactly "Cannot GET /" with text/html;charset=utf-8
            if "cannot get" in body_head:
                return False
            return ("<!doctype" in body_head) or ("<html" in body_head)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def status() -> DaemonStatus:
    """Return the current daemon status without side effects beyond pid-cleanup.

    Returns three signals:

    * ``running`` — the HTTP listener responds on the configured port.
    * ``web_served`` — ``GET /`` returns the built SPA (HTML index). If
      the listener is up but the SPA is missing, ``web_served`` is
      False and ``error`` carries an actionable build hint.
    * ``error`` — set when the daemon is up but the UI is unbuilt, OR
      when the daemon is down with a recorded log.
    """
    _clean_stale_pid()
    pid = _read_pid()
    port = _resolve_port()
    url = f"http://127.0.0.1:{port}"
    home = resolve_open_design_home()
    running = pid is not None and _is_alive(pid) and _probe_url(url)

    web_served = False
    error: str | None = None
    if running:
        web_served = _probe_spa_index(url)
        if not web_served:
            hint = "unknown reason"
            if home is not None:
                index = home / WEB_INDEX_REL_PATH
                if not index.is_file():
                    hint = (
                        f"SPA not built — {index} missing. "
                        f"Run `pnpm --filter @open-design/web build` in {home} "
                        "and then `oc design restart`."
                    )
                else:
                    hint = (
                        f"SPA built at {index} but daemon is not serving it. "
                        "The daemon may have started before the build "
                        "finished — try `oc design restart`."
                    )
            error = hint

    return DaemonStatus(
        running=running,
        pid=pid if running else None,
        port=port,
        url=url,
        home=home,
        log_path=_log_file(),
        web_served=web_served,
        error=error,
    )


def start(*, port: int | None = None, env_overrides: dict[str, str] | None = None) -> DaemonStatus:
    """Spawn the open-design daemon as a detached child.

    Raises:
        OpenDesignNotInstalledError: ``OPEN_DESIGN_HOME`` not discoverable.
        DaemonAlreadyRunningError: a live daemon already owns the PID file.
    """
    _clean_stale_pid()
    existing = _read_pid()
    if existing is not None and _is_alive(existing):
        raise DaemonAlreadyRunningError(
            f"open-design daemon already running (pid={existing}); "
            f"use `oc design stop` first"
        )

    home = resolve_open_design_home()
    if home is None:
        raise OpenDesignNotInstalledError(
            "open-design source tree not found. Set OPEN_DESIGN_HOME to its "
            "directory (containing apps/daemon/), or clone "
            "https://github.com/nexu-io/open-design to ~/.open-design."
        )

    daemon_js = home / DAEMON_REL_PATH
    if not daemon_js.is_file():
        # Source present but unbuilt — surface a clear instruction.
        raise OpenDesignNotInstalledError(
            f"open-design source at {home} is not built. "
            f"Run `pnpm install && pnpm --filter @open-design/daemon build` "
            f"in {home} first."
        )

    effective_port = (
        _validate_port(port, source="start(port=)")
        if port is not None
        else _resolve_port()
    )

    # Port-conflict guard. The earlier _read_pid + _is_alive check
    # only catches OUR previous daemons. A foreign process on the
    # configured port — a stray daemon from a different profile, a
    # crashed-but-orphaned spawn, an unrelated server — would otherwise
    # cause the spawn to crash silently with EADDRINUSE and leave a
    # corrupt PID file pointing at the dead child.
    if _port_in_use(effective_port):
        raise PortInUseError(
            f"port {effective_port} is already in use by another process "
            f"(not tracked in {_pid_file()}). "
            f"Run `lsof -i :{effective_port}` to find it, stop it, then "
            f"retry. Or set OD_PORT to a free port."
        )

    log_path = _log_file()
    pid_path = _pid_file()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["OD_PORT"] = str(effective_port)
    # Default to allowing Hermes Workspace + OC dashboard as iframe parents
    # so the Design tab works out of the box. Caller can override.
    env.setdefault(
        "OD_ALLOWED_FRAME_ANCESTORS",
        "http://localhost:9119 http://127.0.0.1:9119 http://localhost:3000",
    )
    if env_overrides:
        env.update(env_overrides)

    # Append to keep history across restarts; tail -f works.
    log_handle = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            ["node", str(daemon_js), "--no-open"],
            cwd=str(home),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        log_handle.close()
        raise OpenDesignNotInstalledError(
            "`node` not found on PATH — install Node 24.x first."
        ) from exc
    finally:
        # The child inherits the fd; we can close our handle safely.
        try:
            log_handle.close()
        except OSError:
            pass

    pid_path.write_text(str(proc.pid), encoding="utf-8")
    _log.info("open-design daemon spawned (pid=%d, port=%d)", proc.pid, effective_port)

    # Best-effort: wait up to SPAWN_WATCHDOG_S for the port to start
    # listening so callers see a `running=True` status immediately after
    # start(). If the child dies mid-spawn we surface its exit code so
    # callers can tail the log.
    url = f"http://127.0.0.1:{effective_port}"
    deadline = time.monotonic() + SPAWN_WATCHDOG_S
    while time.monotonic() < deadline:
        if _probe_url(url, timeout=0.25):
            break
        if proc.poll() is not None:
            # Child died before listening — surface logs to caller via status().
            return DaemonStatus(
                running=False,
                pid=None,
                port=effective_port,
                url=url,
                home=home,
                log_path=log_path,
                error=f"daemon exited with code {proc.returncode}; see {log_path}",
            )
        time.sleep(0.2)

    return status()


def stop(*, sig: int = signal.SIGTERM, wait_seconds: float = STOP_WATCHDOG_S) -> DaemonStatus:
    """Signal the daemon to terminate, escalating to SIGKILL if needed."""
    pid = _read_pid()
    if pid is None or not _is_alive(pid):
        _clean_stale_pid()
        return status()

    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError) as exc:
        _log.warning("open-design stop: kill(%d, %d) failed: %s", pid, sig, exc)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            break
        time.sleep(0.1)

    if _is_alive(pid):
        _log.warning("open-design stop: SIGTERM ignored, escalating to SIGKILL (pid=%d)", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(0.3)

    try:
        _pid_file().unlink()
    except OSError:
        pass
    return status()


def restart(*, port: int | None = None) -> DaemonStatus:
    stop()
    return start(port=port)


def status_json() -> str:
    return json.dumps(status().to_dict(), indent=2)


__all__ = [
    "DAEMON_REL_PATH",
    "DEFAULT_PORT",
    "WEB_INDEX_REL_PATH",
    "WEB_OUT_REL_PATH",
    "DaemonAlreadyRunningError",
    "DaemonStatus",
    "OpenDesignNotInstalledError",
    "PortInUseError",
    "resolve_open_design_home",
    "restart",
    "start",
    "status",
    "status_json",
    "stop",
]
