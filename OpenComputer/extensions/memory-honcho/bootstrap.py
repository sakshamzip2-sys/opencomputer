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

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = PLUGIN_DIR / "docker-compose.yml"
IMAGE_VERSION_FILE = PLUGIN_DIR / "IMAGE_VERSION"
HEALTH_URL = "http://127.0.0.1:8000/health"


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


def _compose(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``docker compose -f <plugin>/docker-compose.yml <args>``."""
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
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
        r = _compose("up", "-d", env=_compose_env())
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


__all__ = [
    "DockerStatus",
    "detect_docker",
    "honcho_up",
    "honcho_down",
    "honcho_reset",
    "ps_status",
    "health_probe",
    "status",
]
