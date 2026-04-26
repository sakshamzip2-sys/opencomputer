"""``opencomputer dashboard`` CLI command (Phase 8.A of catch-up plan)."""

from __future__ import annotations

import logging
import time
from typing import Annotated

import typer

from opencomputer.dashboard.server import DashboardServer

dashboard_app = typer.Typer(
    name="dashboard",
    help="Run the local web UI dashboard (Phase 8.A — minimal MVP).",
    no_args_is_help=False,
    invoke_without_command=True,
)

_log = logging.getLogger("opencomputer.dashboard")


@dashboard_app.callback(invoke_without_command=True)
def run(
    ctx: typer.Context,
    host: Annotated[
        str, typer.Option(help="Bind address. Non-localhost requires "
                          "`dashboard.bind_external` consent.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 9119,
    wire_url: Annotated[
        str, typer.Option(help="WebSocket URL of the wire server.")
    ] = "ws://127.0.0.1:18789",
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    if host != "127.0.0.1":
        _check_external_bind_consent_or_exit(host)

    server = DashboardServer(host=host, port=port, wire_url=wire_url)
    try:
        server.start()
    except OSError as e:
        typer.echo(f"Dashboard failed to bind {host}:{port}: {e}", err=True)
        raise typer.Exit(2) from e

    typer.echo(f"Dashboard: {server.url}")
    typer.echo(f"Wire URL:  {wire_url}")
    typer.echo("Ctrl-C to stop.")
    try:
        # Block main thread until Ctrl-C; the http server runs in a
        # daemon thread.
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        typer.echo("\nstopping…")
    finally:
        server.stop()


def _check_external_bind_consent_or_exit(host: str) -> None:
    """Refuse non-localhost binds unless ``dashboard.bind_external`` granted."""
    try:
        import sqlite3

        from opencomputer.agent.config import _home
        from opencomputer.agent.consent.store import ConsentStore
        from opencomputer.agent.state import apply_migrations

        db_path = _home() / "sessions.db"
        conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            apply_migrations(conn)
            store = ConsentStore(conn)
            grant = store.get("dashboard.bind_external", None)
        finally:
            conn.close()

        if grant is None:
            typer.echo(
                f"Refusing to bind {host}: capability "
                "'dashboard.bind_external' not granted. Run "
                "`opencomputer consent grant dashboard.bind_external` "
                "first if you really mean it.",
                err=True,
            )
            raise typer.Exit(2)
    except typer.Exit:
        raise
    except Exception as e:  # noqa: BLE001
        # If we can't check consent, fail closed.
        _log.exception("dashboard: external bind consent check failed: %s", e)
        typer.echo(
            f"Refusing to bind {host}: consent system unavailable.",
            err=True,
        )
        raise typer.Exit(2) from e
