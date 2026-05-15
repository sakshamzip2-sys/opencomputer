"""``opencomputer host`` — inspect the startup host-environment fingerprint.

Surfaces :class:`plugin_sdk.HostProfile` — the same fingerprint threaded
into the agent's system prompt and consumed by plugins (e.g. a Linux
computer-use backend choosing ``xdotool`` vs ``ydotool``, or detecting a
headless box).

Subcommands:

  - ``oc host show``     — full host profile as a table
  - ``oc host summary``  — the compact one-line fingerprint

The probe (:func:`plugin_sdk.detect_host`) is process-cached and
failure-isolated; any field that could not be detected shows its safe
default ("unknown" / 0) rather than crashing the command.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from plugin_sdk import detect_host

host_app = typer.Typer(
    name="host",
    help="Inspect the startup host-environment fingerprint.",
    no_args_is_help=True,
)
_console = Console()


@host_app.command("show")
def host_show() -> None:
    """Render the full :class:`HostProfile` as a table."""
    host = detect_host()

    table = Table(title="Host profile", expand=False, show_header=True)
    table.add_column("Field", style="bold cyan", no_wrap=True)
    table.add_column("Value", overflow="fold")

    rows: list[tuple[str, str]] = [
        ("OS", host.os_pretty),
        ("OS name", host.os_name),
        ("OS version", host.os_version),
        ("Architecture", host.arch),
        ("Python", host.python_version),
        ("Hostname", host.hostname),
        ("Logical CPUs", str(host.cpu_logical)),
        ("Physical CPUs", str(host.cpu_physical)),
        ("Total RAM", f"{host.total_ram_gb:g} GiB"),
        ("Display server", host.display_server),
        ("Headless", "yes" if host.is_headless else "no"),
        ("Container", "yes" if host.is_container else "no"),
        ("WSL", "yes" if host.is_wsl else "no"),
    ]
    for field_name, value in rows:
        table.add_row(field_name, value)

    _console.print(table)


@host_app.command("summary")
def host_summary() -> None:
    """Print the compact one-line host fingerprint.

    Same string the agent's system prompt and logs use — handy for
    quick diffs / pasting into bug reports.
    """
    _console.print(detect_host().summary_line())


__all__ = ["host_app"]
