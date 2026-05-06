"""``oc langfuse`` — bring up / down a self-hosted langfuse stack.

Subcommands:

    oc langfuse up           # docker compose up -d using the bundled template
    oc langfuse down         # graceful stop (data preserved)
    oc langfuse status       # health probe of langfuse-server :3000/api/health
    oc langfuse logs         # tail compose logs
    oc langfuse keys         # print URL + first-run setup pointer

The compose template lives at
``opencomputer/integrations/langfuse/docker-compose.yaml`` and persists
state under ``~/.opencomputer/langfuse/``. Configuration env vars come
from ``~/.opencomputer/langfuse/.env`` (auto-created from the template
on first ``up``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import typer

langfuse_app = typer.Typer(
    name="langfuse",
    help="Self-host a langfuse stack via docker compose.",
    no_args_is_help=True,
)


def _bundle_dir() -> Path:
    return Path(__file__).resolve().parent / "integrations" / "langfuse"


def _data_dir() -> Path:
    home = Path.home() / ".opencomputer" / "langfuse"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _env_path() -> Path:
    return _data_dir() / ".env"


def _ensure_env() -> Path:
    env_path = _env_path()
    if env_path.is_file():
        return env_path
    template = _bundle_dir() / ".env.template"
    if not template.is_file():
        raise typer.BadParameter(
            f"missing langfuse env template at {template}; reinstall opencomputer"
        )
    env_path.write_text(template.read_text())
    typer.echo(f"created {env_path} from template — review + rotate secrets before exposing langfuse to a network")
    return env_path


def _docker_or_die() -> str:
    docker = shutil.which("docker")
    if docker is None:
        typer.echo(
            "docker not found on PATH — install Docker Desktop / Engine first.",
            err=True,
        )
        raise typer.Exit(code=2)
    return docker


def _compose_cmd(*args: str) -> list[str]:
    docker = _docker_or_die()
    compose_file = _bundle_dir() / "docker-compose.yaml"
    env_file = _ensure_env()
    return [
        docker,
        "compose",
        "-f",
        str(compose_file),
        "--env-file",
        str(env_file),
        "-p",
        "oc-langfuse",
        *args,
    ]


@langfuse_app.command("up")
def up_command(
    detach: bool = typer.Option(
        True,
        "--detach/--foreground",
        "-d/-f",
        help="Detach (default) — return after starting. --foreground to stream logs.",
    ),
    pull: bool = typer.Option(
        False, "--pull", help="Pull latest images first."
    ),
) -> None:
    """Bring the langfuse stack up."""
    if pull:
        subprocess.run(_compose_cmd("pull"), check=False)
    args = ["up"]
    if detach:
        args.append("-d")
    rc = subprocess.run(_compose_cmd(*args), check=False).returncode
    if rc != 0:
        raise typer.Exit(code=rc)
    if detach:
        port = os.environ.get("OC_LANGFUSE_PORT", "3000")
        typer.echo(
            "\nlangfuse is starting. Once healthy, visit:\n"
            f"  http://localhost:{port}\n"
            "First-run flow creates the admin user. Then go to:\n"
            "  Settings → API keys → Create new pair\n"
            "And export them so the OC plugin picks them up:\n"
            "  export LANGFUSE_PUBLIC_KEY=pk-lf-...\n"
            "  export LANGFUSE_SECRET_KEY=sk-lf-...\n"
            f"  export LANGFUSE_BASE_URL=http://localhost:{port}\n"
        )


@langfuse_app.command("down")
def down_command(
    volumes: bool = typer.Option(
        False,
        "--volumes",
        help="Also remove the bind-mounted data directory. DESTRUCTIVE.",
    ),
) -> None:
    """Stop the langfuse stack. Data is preserved unless --volumes."""
    args = ["down"]
    if volumes:
        if not typer.confirm(
            "This will delete ~/.opencomputer/langfuse/{postgres,clickhouse-data,clickhouse-logs,redis}. Continue?"
        ):
            typer.echo("cancelled")
            return
    rc = subprocess.run(_compose_cmd(*args), check=False).returncode
    if volumes:
        # docker compose's --volumes only nukes named volumes, not bind
        # mounts. We managed the data dir ourselves; remove it now.
        import shutil as _sh

        for sub in ("postgres", "clickhouse-data", "clickhouse-logs", "redis"):
            _sh.rmtree(_data_dir() / sub, ignore_errors=True)
    if rc != 0:
        raise typer.Exit(code=rc)


@langfuse_app.command("status")
def status_command() -> None:
    """Print health status of langfuse-server."""
    port = os.environ.get("OC_LANGFUSE_PORT", "3000")
    url = f"http://localhost:{port}/api/public/health"
    typer.echo(f"GET {url}")
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 — local URL
            typer.echo(f"  HTTP {resp.status}")
            body = resp.read().decode("utf-8", errors="replace").strip()
            if body:
                typer.echo(f"  body: {body[:300]}")
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"  unreachable: {exc}", err=True)
        raise typer.Exit(code=1) from None


@langfuse_app.command("logs")
def logs_command(
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream logs."),
    tail: int = typer.Option(100, "--tail", help="Last N lines per service."),
    service: str | None = typer.Option(
        None, "--service", "-s", help="One of: server | worker | postgres | clickhouse | redis"
    ),
) -> None:
    """Tail compose logs."""
    args = ["logs", f"--tail={tail}"]
    if follow:
        args.append("-f")
    if service:
        args.append(f"langfuse-{service}")
    rc = subprocess.run(_compose_cmd(*args), check=False).returncode
    raise typer.Exit(code=rc)


@langfuse_app.command("keys")
def keys_command() -> None:
    """Print pointers to the API-key setup flow + active env vars (if set)."""
    port = os.environ.get("OC_LANGFUSE_PORT", "3000")
    typer.echo(
        "Generate langfuse keys via the web UI:\n"
        f"  http://localhost:{port}/  →  Settings → API Keys\n"
    )
    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "")
    base = os.environ.get("LANGFUSE_BASE_URL", "")
    typer.echo("Currently set:")
    typer.echo(f"  LANGFUSE_PUBLIC_KEY = {pub[:10]+'…' if pub else '(unset)'}")
    typer.echo(f"  LANGFUSE_SECRET_KEY = {'(set)' if sec else '(unset)'}")
    typer.echo(f"  LANGFUSE_BASE_URL   = {base or '(unset → cloud.langfuse.com)'}")


__all__ = ["langfuse_app"]
