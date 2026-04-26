"""Docker lifecycle helpers for the self-hosted Honcho bundle.

Pure functions — no Typer / Rich dependency — so this module can be
imported from tests, CLI subcommands, and the first-run wizard alike.

Design rules:
  - NEVER crash the caller. Every function returns a status / None /
    False on failure; the caller decides UX.
  - Docker shells out via subprocess — NO docker-py dependency.
  - Commands target ``docker compose`` (v2 plugin syntax), NOT legacy
    ``docker-compose``. We check for that at detect_docker() time.
"""

from __future__ import annotations

import errno
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = PLUGIN_DIR / "docker-compose.yml"
IMAGE_VERSION_FILE = PLUGIN_DIR / "IMAGE_VERSION"
HEALTH_URL = "http://127.0.0.1:8000/health"

# Host ports the Honcho stack binds on 127.0.0.1. Derived from
# ``docker-compose.yml`` (only the API is exposed externally — postgres
# and redis stay inside the compose network). If the compose file ever
# grows new host-level ports, extend this tuple in lockstep.
_PORTS: tuple[int, ...] = (8000,)

# Poll cadence for ``ensure_started`` health-wait. 2s aligns with the
# compose healthcheck intervals in ``docker-compose.yml``.
_HEALTH_POLL_INTERVAL_S: float = 2.0


@dataclass(frozen=True, slots=True)
class DockerStatus:
    """What we know about the Docker + Honcho state on this machine."""

    docker_installed: bool
    compose_v2: bool
    honcho_running: bool
    honcho_healthy: bool
    message: str  # human-readable, for status display


def detect_docker() -> tuple[bool, bool]:
    """Check whether Docker + compose v2 are available.

    Returns ``(docker_installed, compose_v2_plugin)``. Both False means
    the bootstrap commands will refuse cleanly with install instructions.
    """
    if not shutil.which("docker"):
        return (False, False)
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=5,
        )
        return (True, r.returncode == 0)
    except (subprocess.TimeoutExpired, OSError):
        return (True, False)


def is_docker_daemon_running() -> bool:
    """Cheap probe — is the docker daemon currently reachable?

    ``detect_docker`` only checks the binary + compose plugin; on a
    fresh Mac install Docker Desktop ships those but the daemon doesn't
    start until the user opens the app. The v2026.4.26 incident traced
    here: ``ensure_started`` would call ``docker compose up`` which
    would hang for the full 120s timeout trying to reach the dead
    daemon, then the wizard would fall back to baseline memory.

    ``docker info`` exits non-zero immediately when the socket is
    unreachable, so this is a sub-second check we can do BEFORE
    paying compose's startup cost.
    """
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ID}}"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def try_start_docker_daemon() -> bool:
    """Attempt to start the Docker daemon non-interactively.

    macOS: ``open -a Docker`` launches Docker Desktop. The daemon
    becomes reachable ~10-30s after that on a typical machine.

    Linux: we don't try systemctl — many distros require sudo, and
    a sudo prompt mid-wizard would be a worse UX than a clear
    "please start docker" message. Caller falls back to instructing
    the user.

    Returns True if a start attempt was made (caller should then poll
    :func:`is_docker_daemon_running` for up to ~60s). Returns False
    when the platform is unsupported or the launcher is missing.
    """
    import sys

    if sys.platform != "darwin":
        return False
    if not shutil.which("open"):
        return False
    try:
        subprocess.run(
            ["open", "-a", "Docker"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return True
    except (subprocess.TimeoutExpired, OSError):
        return False


def wait_for_docker_daemon(timeout_s: float = 60.0, poll_interval: float = 2.0) -> bool:
    """Poll :func:`is_docker_daemon_running` until True or timeout.

    Used after :func:`try_start_docker_daemon` — Docker Desktop's
    daemon takes 10-30s to come up cold, so a fixed sleep is wrong
    (too short on a slow machine, too long on a fast one).
    """
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        if is_docker_daemon_running():
            return True
        time.sleep(poll_interval)
    return False


def _compose(
    *args: str, env: dict | None = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Run ``docker compose -f <plugin>/docker-compose.yml <args>``.

    ``timeout`` defaults to 120s (fits ``ps`` / ``down`` / cached
    ``up``); pass a longer value for ``up -d`` on a fresh install
    where 600 MB of images need pulling — see :func:`honcho_up`.
    """
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _compose_env() -> dict:
    """Build the env dict for docker compose: read IMAGE_VERSION into HONCHO_IMAGE_TAG."""
    import os

    env = dict(os.environ)
    try:
        tag = IMAGE_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        tag = "latest"
    env["HONCHO_IMAGE_TAG"] = tag or "latest"
    return env


def honcho_up() -> tuple[bool, str]:
    """``docker compose up -d``. Returns ``(ok, message)``."""
    docker, compose_v2 = detect_docker()
    if not docker:
        return (False, "Docker is not installed. See README for install steps.")
    if not compose_v2:
        return (
            False,
            "Docker found, but 'docker compose' v2 plugin is missing. "
            "Install docker-compose-plugin or upgrade Docker Desktop.",
        )
    try:
        # 300s tolerates the first-pull case (postgres + redis + api
        # ~600 MB on a fresh install). Cached up-d returns in <5s, so
        # the longer ceiling has no observable cost on subsequent runs.
        r = _compose("up", "-d", env=_compose_env(), timeout=300)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"docker compose up failed: {e}")
    if r.returncode != 0:
        return (False, f"docker compose up failed:\n{r.stderr or r.stdout}")
    return (True, "Honcho stack started (postgres + redis + api on 127.0.0.1:8000).")


def honcho_down() -> tuple[bool, str]:
    """``docker compose down``. Keeps volumes. Returns ``(ok, message)``."""
    docker, compose_v2 = detect_docker()
    if not docker or not compose_v2:
        return (False, "Docker / compose v2 not available.")
    try:
        r = _compose("down", env=_compose_env())
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"docker compose down failed: {e}")
    if r.returncode != 0:
        return (False, f"docker compose down failed:\n{r.stderr or r.stdout}")
    return (True, "Honcho stack stopped (volumes preserved).")


def honcho_reset() -> tuple[bool, str]:
    """``docker compose down -v``. Wipes ALL data. Returns ``(ok, message)``."""
    docker, compose_v2 = detect_docker()
    if not docker or not compose_v2:
        return (False, "Docker / compose v2 not available.")
    try:
        r = _compose("down", "-v", env=_compose_env())
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"docker compose down -v failed: {e}")
    if r.returncode != 0:
        return (False, f"docker compose down -v failed:\n{r.stderr or r.stdout}")
    return (True, "Honcho stack torn down AND volumes wiped. All Honcho data gone.")


def ps_status() -> list[dict]:
    """Return a list of service states from ``docker compose ps --format json``.

    Each dict has keys: ``Service``, ``State``, ``Status`` (at minimum,
    depending on compose version). Empty list on any failure.
    """
    docker, compose_v2 = detect_docker()
    if not docker or not compose_v2:
        return []
    try:
        r = _compose("ps", "--format", "json", env=_compose_env())
    except (subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0 or not r.stdout.strip():
        return []
    import json

    try:
        parsed = json.loads(r.stdout)
    except json.JSONDecodeError:
        # Some compose versions emit one JSON object per line
        rows: list[dict] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
    return parsed if isinstance(parsed, list) else [parsed]


def health_probe(url: str = HEALTH_URL, timeout: float = 2.0) -> bool:
    """Best-effort HTTP GET to the Honcho health endpoint. True on 200."""
    try:
        import httpx

        r = httpx.get(url, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def status() -> DockerStatus:
    """One-call status summary for CLI display."""
    docker, compose_v2 = detect_docker()
    if not docker:
        return DockerStatus(
            docker_installed=False,
            compose_v2=False,
            honcho_running=False,
            honcho_healthy=False,
            message="Docker not installed.",
        )
    if not compose_v2:
        return DockerStatus(
            docker_installed=True,
            compose_v2=False,
            honcho_running=False,
            honcho_healthy=False,
            message="Docker found, but 'docker compose' v2 plugin missing.",
        )
    rows = ps_status()
    running = any((row.get("State") or "").lower() == "running" for row in rows)
    healthy = health_probe()
    if not rows:
        msg = "No Honcho containers found. Run 'opencomputer memory setup'."
    elif running and healthy:
        msg = "Honcho is up and healthy."
    elif running:
        msg = "Honcho containers running but /health not responding yet."
    else:
        msg = "Honcho containers exist but none are running."
    return DockerStatus(
        docker_installed=True,
        compose_v2=True,
        honcho_running=running,
        honcho_healthy=healthy,
        message=msg,
    )


def _check_port_available(port: int) -> bool:
    """Return True if ``127.0.0.1:<port>`` can be bound (i.e. nothing else
    is listening on it). False if the bind fails with EADDRINUSE.

    Uses a short-lived socket — caller keeps no state. A port bound by our
    own Honcho stack will also report False; callers who care about that
    distinction should check ``_is_stack_healthy()`` first.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        return True
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            return False
        # Any other OSError (permission, invalid fd, …) is also a reason
        # to treat the port as unusable — be conservative.
        return False


def _is_stack_healthy() -> bool:
    """Return True iff *our* compose stack is already up and healthy.

    Tolerant: if docker/compose are missing, or ``docker compose ps``
    returns no services, or the stack exists but isn't healthy yet, we
    return False. Only explicit ``Health: healthy`` on at least one
    service counts as healthy.
    """
    docker, compose_v2 = detect_docker()
    if not docker or not compose_v2:
        return False
    try:
        r = _compose("ps", "--format", "json", env=_compose_env())
    except (subprocess.TimeoutExpired, OSError):
        return False
    if r.returncode != 0 or not r.stdout.strip():
        return False
    import json

    rows: list[dict] = []
    try:
        parsed = json.loads(r.stdout)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        # Some compose versions emit one JSON object per line
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        return False
    # A row reports healthy when ``Health`` is the literal string
    # ``healthy`` (compose emits that key for services with a healthcheck).
    # Fall back to the legacy ``State`` check if ``Health`` is absent —
    # that's a degraded signal but better than none.
    for row in rows:
        health = (row.get("Health") or "").lower()
        if health == "healthy":
            return True
    return False


def ensure_started(timeout_s: int = 60) -> tuple[bool, str]:
    """Bring the Honcho stack up safely and idempotently.

    Returns ``(ok, message)``. On success: pulled (if needed), started,
    and polled-healthy. On any failure along the way: a human-readable
    explanation suitable for the wizard / ``memory doctor`` to display.

    Steps:
      1. Detect Docker + compose v2.
      2. If the stack is already healthy, short-circuit with
         ``(True, "already running and healthy")``.
      3. Check each host-level port in ``_PORTS`` is free (port collision
         with some *other* process aborts before any docker work).
      4. ``docker compose pull --quiet`` — missing image is a common
         first-run failure mode, better to surface it here than mid-``up``.
      5. ``docker compose up -d``.
      6. Poll ``_is_stack_healthy()`` every ~2s until healthy or
         ``timeout_s`` elapses.
    """
    # Step 1: pre-flight docker detection.
    docker, compose_v2 = detect_docker()
    if not docker:
        return (
            False,
            "Docker is not installed. "
            "Install from https://docs.docker.com/get-docker/",
        )
    if not compose_v2:
        return (
            False,
            "Docker found but 'docker compose' v2 plugin missing. "
            "Install docker-compose-plugin or upgrade Docker Desktop.",
        )

    # Step 2: idempotency — if already healthy, don't touch anything.
    if _is_stack_healthy():
        return (True, "Honcho stack already running and healthy.")

    # Step 3: port collision. A port bound by some *other* process (not
    # our stack, which we'd have detected above) means ``up`` will fail
    # in a hard-to-debug way — catch it now with a clear error.
    for port in _PORTS:
        if not _check_port_available(port):
            return (
                False,
                f"Port {port} already in use by another process — stop "
                f"that process or adjust the compose file port mapping.",
            )

    # Step 4: pull image(s). Quiet so the terminal doesn't fill with
    # layer-download progress; stderr is what we surface on failure.
    env = _compose_env()
    try:
        r = _compose("pull", "--quiet", env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"docker compose pull failed: {e}")
    if r.returncode != 0:
        stderr = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode(
            "utf-8", errors="replace"
        )
        snippet = stderr[:200] if stderr else (r.stdout or "")[:200]
        return (False, f"docker compose pull failed: {snippet}")

    # Step 5: up -d.
    try:
        r = _compose("up", "-d", env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        return (False, f"docker compose up failed: {e}")
    if r.returncode != 0:
        stderr = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode(
            "utf-8", errors="replace"
        )
        snippet = stderr[:200] if stderr else (r.stdout or "")[:200]
        return (False, f"docker compose up failed: {snippet}")

    # Step 6: health poll. Deadline-based so the sleep granularity
    # doesn't accidentally shift the effective timeout.
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        if _is_stack_healthy():
            return (True, "Honcho stack started and healthy.")
        time.sleep(_HEALTH_POLL_INTERVAL_S)

    return (
        False,
        f"Honcho stack did not become healthy within {timeout_s}s. "
        f"Check 'docker compose logs'.",
    )


__all__ = [
    "DockerStatus",
    "detect_docker",
    "ensure_started",
    "honcho_up",
    "honcho_down",
    "honcho_reset",
    "ps_status",
    "health_probe",
    "status",
]
