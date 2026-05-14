"""`oc design …` Typer subcommands.

Registered via :func:`plugin_sdk.PluginAPI.register_cli_command` so the
verbs surface as ``oc design start|stop|status|url|restart``. The plugin
manifest's ``cli_commands: ["design"]`` array tells the loader to wire
the dispatch before plugin import — `oc design --help` works without
spawning the daemon.
"""

from __future__ import annotations

import typer

# Plugin-loader puts the plugin root on sys.path[0]; flat import works.
# We deliberately don't have a "package-mode" fallback — the plugin
# directory is hyphenated (extensions/open-design/) which Python cannot
# import as a package, so any ImportError here is a real bug, not a
# fixture issue. Tests import via importlib + spec_from_file_location.
from lifecycle import (  # noqa: E402 — sys.path[0] populated by loader
    DaemonAlreadyRunningError,
    OpenDesignNotInstalledError,
    PortInUseError,
    resolve_open_design_home,
)
from lifecycle import (
    restart as lifecycle_restart,
)
from lifecycle import (
    start as lifecycle_start,
)
from lifecycle import (
    status as lifecycle_status,
)
from lifecycle import (
    status_json as lifecycle_status_json,
)
from lifecycle import (
    stop as lifecycle_stop,
)

app = typer.Typer(
    name="design",
    help="Manage the Open Design sidecar (https://github.com/nexu-io/open-design).",
    no_args_is_help=True,
    add_completion=False,
)


def _fail(message: str, *, code: int = 1) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


@app.command("status")
def cmd_status(
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show daemon health, PID, port, URL, source path, and SPA readiness."""
    snapshot = lifecycle_status()
    if json_out:
        typer.echo(lifecycle_status_json())
        return
    if snapshot.running and snapshot.web_served:
        state, color = "running", typer.colors.GREEN
    elif snapshot.running:
        state, color = "running (SPA unavailable)", typer.colors.YELLOW
    else:
        state, color = "stopped", typer.colors.YELLOW
    typer.secho(f"open-design: {state}", fg=color)
    typer.echo(f"  url       : {snapshot.url}")
    typer.echo(f"  port      : {snapshot.port}")
    typer.echo(f"  pid       : {snapshot.pid if snapshot.pid is not None else '—'}")
    typer.echo(f"  home      : {snapshot.home if snapshot.home else '(not found — set OPEN_DESIGN_HOME)'}")
    typer.echo(f"  web_served: {'yes' if snapshot.web_served else 'no'}")
    typer.echo(f"  log       : {snapshot.log_path}")
    if snapshot.error:
        typer.secho(f"  error     : {snapshot.error}", fg=typer.colors.RED)


@app.command("start")
def cmd_start(
    port: int | None = typer.Option(None, "--port", help="Override OD_PORT (default 7456)."),
) -> None:
    """Spawn the open-design daemon as a background process."""
    try:
        snapshot = lifecycle_start(port=port)
    except DaemonAlreadyRunningError as exc:
        _fail(str(exc), code=2)
    except OpenDesignNotInstalledError as exc:
        _fail(str(exc), code=3)
    except PortInUseError as exc:
        _fail(str(exc), code=7)
    if snapshot.running:
        typer.secho(
            f"open-design daemon running at {snapshot.url} (pid={snapshot.pid})",
            fg=typer.colors.GREEN,
        )
        if not snapshot.web_served:
            typer.secho(
                f"  warning: web SPA unavailable — {snapshot.error}",
                fg=typer.colors.YELLOW,
            )
    else:
        _fail(
            "daemon did not become healthy within 5s — "
            f"see {snapshot.log_path} for details",
            code=4,
        )


@app.command("stop")
def cmd_stop() -> None:
    """Terminate the daemon (SIGTERM, escalates to SIGKILL after 5s)."""
    snapshot = lifecycle_stop()
    if snapshot.running:
        _fail("failed to stop daemon — process still alive", code=5)
    typer.secho("open-design daemon stopped", fg=typer.colors.GREEN)


@app.command("restart")
def cmd_restart(
    port: int | None = typer.Option(None, "--port", help="Override OD_PORT."),
) -> None:
    """Stop and re-start the daemon."""
    try:
        snapshot = lifecycle_restart(port=port)
    except OpenDesignNotInstalledError as exc:
        _fail(str(exc), code=3)
    except PortInUseError as exc:
        _fail(str(exc), code=7)
    if snapshot.running:
        typer.secho(
            f"open-design daemon restarted at {snapshot.url} (pid={snapshot.pid})",
            fg=typer.colors.GREEN,
        )
        if not snapshot.web_served:
            typer.secho(
                f"  warning: web SPA unavailable — {snapshot.error}",
                fg=typer.colors.YELLOW,
            )
    else:
        _fail(
            "daemon did not become healthy after restart — "
            f"see {snapshot.log_path}",
            code=4,
        )


@app.command("url")
def cmd_url() -> None:
    """Print only the daemon URL (suitable for shell substitution)."""
    snapshot = lifecycle_status()
    typer.echo(snapshot.url)


@app.command("home")
def cmd_home() -> None:
    """Print the resolved OPEN_DESIGN_HOME directory."""
    home = resolve_open_design_home()
    if home is None:
        _fail(
            "OPEN_DESIGN_HOME not set and no default location found. "
            "Set the env var to a directory containing apps/daemon/.",
            code=6,
        )
    typer.echo(str(home))


__all__ = ["app"]
