"""``oc computer-use …`` Typer subcommands.

Registered via :meth:`PluginAPI.register_cli_command` so the verbs surface
as ``oc computer-use install`` / ``oc computer-use status``. The manifest's
``cli_commands: ["computer-use"]`` array tells the loader to wire the
dispatch before plugin import, so ``oc computer-use --help`` works without
loading the heavy MCP machinery.
"""

from __future__ import annotations

import platform
import shutil

import typer

# Plugin loader puts the plugin root on sys.path[0] — flat sibling import.
from installer import (  # type: ignore[import-not-found]  # noqa: E402
    cua_driver_version,
    install_cua_driver,
)

app = typer.Typer(
    name="computer-use",
    help="Manage the cua-driver binary that powers macOS background computer-use.",
    no_args_is_help=True,
)


@app.command("install")
def install(
    upgrade: bool = typer.Option(
        False,
        "--upgrade",
        help="Re-run the upstream installer even if cua-driver is already present.",
    ),
) -> None:
    """Install (or upgrade) the cua-driver binary. macOS only."""
    if platform.system() != "Darwin":
        typer.echo("computer-use is macOS only — cua-driver cannot be installed here.")
        raise typer.Exit(code=1)
    ok = install_cua_driver(upgrade=upgrade)
    if not ok:
        raise typer.Exit(code=1)


@app.command("status")
def status() -> None:
    """Show whether cua-driver is installed and on PATH."""
    if platform.system() != "Darwin":
        typer.echo("computer-use is macOS only — unavailable on this platform.")
        raise typer.Exit(code=1)
    binary = shutil.which("cua-driver")
    if not binary:
        typer.echo("cua-driver: NOT installed. Run `oc computer-use install`.")
        raise typer.Exit(code=1)
    version = cua_driver_version() or "unknown version"
    typer.echo(f"cua-driver: installed at {binary} ({version})")


__all__ = ["app"]
