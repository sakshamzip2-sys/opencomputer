"""``oc dashboard`` — DEPRECATED alias for ``oc workspace backend``.

The dashboard backend is the FastAPI server (``DashboardServer``) that
``oc workspace`` runs internally. It now lives under
``oc workspace backend``; this module is kept for one release as a
forwarding alias so existing scripts and service units keep working. It
prints a deprecation notice and forwards to the new command. Scheduled
for removal in a future release.
"""

from __future__ import annotations

from typing import Annotated

import typer

dashboard_app = typer.Typer(
    name="dashboard",
    help=(
        "DEPRECATED — use `oc workspace backend`. Kept as a forwarding "
        "alias; will be removed in a future release."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)


@dashboard_app.callback(invoke_without_command=True)
def run(
    ctx: typer.Context,
    host: Annotated[
        str,
        typer.Option(
            help="Bind address. Non-localhost requires "
            "`dashboard.bind_external` consent."
        ),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 9119,
    wire_url: Annotated[
        str, typer.Option(help="WebSocket URL of the wire server.")
    ] = "ws://127.0.0.1:18789",
    detach: Annotated[
        bool,
        typer.Option(
            "--detach", "-d", help="Run in background; pid + logs under profile home."
        ),
    ] = False,
) -> None:
    """DEPRECATED — forwards to ``oc workspace backend``.

    Prints a deprecation notice, then runs the backend exactly as
    ``oc workspace backend`` would. Use that command directly instead.
    """
    if ctx.invoked_subcommand is not None:
        return

    typer.echo(
        "warning: `oc dashboard` is deprecated and will be removed in a "
        "future release — use `oc workspace backend` instead.",
        err=True,
    )

    # Single source of truth: the backend runner lives in cli_workspace.
    from opencomputer.cli_workspace import _run_backend

    _run_backend(host=host, port=port, wire_url=wire_url, detach=detach)
