"""Task II.3 — ``opencomputer channels`` CLI subcommand group.

A thin Rich-table viewer over ``opencomputer/gateway/channel_directory.py``.
Today the only subcommand is ``list``; additional operations (clear, rename,
export) can land later without restructuring.

Commands:

  opencomputer channels list
    Print the directory at ``~/.opencomputer/channel_directory.json`` as a
    Rich table sorted by most-recent ``last_seen``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

channels_app = typer.Typer(
    name="channels",
    help="Inspect the cached channel directory (platform/chat_id → friendly name).",
    no_args_is_help=True,
)
_console = Console()


@channels_app.command("list")
def channels_list() -> None:
    """Print all known channels, sorted by most-recent activity first."""
    from opencomputer.gateway.channel_directory import ChannelDirectory

    directory = ChannelDirectory()
    entries = directory.list_all()

    if not entries:
        _console.print(
            f"[dim]no channels recorded yet at[/dim] {directory.path}"
        )
        return

    table = Table(title="OpenComputer channel directory", title_style="bold")
    table.add_column("Platform", style="cyan", no_wrap=True)
    table.add_column("Chat ID", style="magenta", no_wrap=True)
    table.add_column("Display name", style="white")
    table.add_column("Last seen (UTC)", style="dim", no_wrap=True)

    for entry in entries:
        seen = (
            datetime.fromtimestamp(entry.last_seen, tz=UTC)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        table.add_row(
            entry.platform,
            entry.chat_id,
            entry.display_name or "[dim](none)[/dim]",
            seen,
        )

    _console.print(table)
    _console.print(f"[dim]source: {directory.path}[/dim]")


__all__ = ["channels_app"]
