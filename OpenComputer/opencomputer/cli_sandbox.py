"""``opencomputer sandbox`` Typer subapp (Phase 3.E).

Subcommands::

    opencomputer sandbox status           # which strategies are available
    opencomputer sandbox run -- <argv>    # run argv through auto strategy
    opencomputer sandbox explain -- <argv>  # print wrapped command, dry-run

Useful for visibility (debugging which strategy got picked on this host),
quick smoke tests of the containment layer, and audit-ready dry-run
inspection of the actual wrapped invocation.

Argv is passed AFTER ``--`` so flags meant for the sandboxed command
aren't parsed by Typer. ``argv`` is a list-of-strings — every token
becomes a single argv element (no shell parsing).
"""

from __future__ import annotations

import asyncio
import platform
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.sandbox.auto import auto_strategy
from opencomputer.sandbox.docker import DockerStrategy
from opencomputer.sandbox.linux import LinuxBwrapStrategy
from opencomputer.sandbox.macos import MacOSSandboxExecStrategy
from opencomputer.sandbox.none_strategy import NoneSandboxStrategy
from opencomputer.sandbox.runner import run_sandboxed
from plugin_sdk.sandbox import SandboxConfig, SandboxUnavailable

sandbox_app = typer.Typer(
    name="sandbox",
    help="Pluggable sandbox strategies (Phase 3.E).",
    no_args_is_help=True,
)
console = Console()


def _all_strategies() -> list[tuple[str, object]]:
    """Return ``(name, instance)`` for every concrete strategy.

    Used by the ``status`` command to render a table of availabilities.
    Ordered to match the auto-selection preference (host-native first,
    then Docker, then ``none``).
    """
    return [
        ("macos_sandbox_exec", MacOSSandboxExecStrategy()),
        ("linux_bwrap", LinuxBwrapStrategy()),
        ("docker", DockerStrategy()),
        ("none", NoneSandboxStrategy()),
    ]


@sandbox_app.command("status")
def sandbox_status() -> None:
    """Show which strategies are available and which one ``auto`` picks."""
    table = Table(title=f"Sandbox strategies ({platform.system()})")
    table.add_column("Strategy", style="cyan")
    table.add_column("Available", justify="center")
    for name, s in _all_strategies():
        marker = "[green]yes[/green]" if s.is_available() else "[dim]no[/dim]"
        table.add_row(name, marker)
    console.print(table)

    try:
        picked = auto_strategy()
        console.print(f"[bold]auto[/bold] would pick: [cyan]{picked.name}[/cyan]")
    except SandboxUnavailable as e:
        console.print(f"[yellow]auto unavailable:[/yellow] {e}")


@sandbox_app.command("run")
def sandbox_run(
    argv: Annotated[
        list[str],
        typer.Argument(help="Command + args to run inside the sandbox."),
    ],
) -> None:
    """Run ``argv`` through the auto-selected strategy. Exits with the wrapped exit code."""
    if not argv:
        console.print("[bold red]error:[/bold red] no command supplied")
        raise typer.Exit(2)
    try:
        result = asyncio.run(run_sandboxed(argv, config=SandboxConfig()))
    except SandboxUnavailable as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        raise typer.Exit(2) from None
    if result.stdout:
        # Print raw stdout so binary-y content survives; rely on Rich's
        # default UTF-8 handling for safety.
        console.out(result.stdout, end="")
    if result.stderr:
        console.print(result.stderr, end="", style="red")
    console.print(
        f"\n[dim]strategy={result.strategy_name} "
        f"exit={result.exit_code} duration={result.duration_seconds:.2f}s[/dim]"
    )
    raise typer.Exit(result.exit_code if result.exit_code >= 0 else 1)


@sandbox_app.command("explain")
def sandbox_explain(
    argv: Annotated[
        list[str],
        typer.Argument(help="Command + args to wrap (without running it)."),
    ],
) -> None:
    """Print the wrapped command without running it. Useful for ``--dry-run``."""
    if not argv:
        console.print("[bold red]error:[/bold red] no command supplied")
        raise typer.Exit(2)
    try:
        strategy = auto_strategy()
    except SandboxUnavailable as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        raise typer.Exit(2) from None
    wrapped = strategy.explain(argv, config=SandboxConfig())
    console.print(f"[dim]strategy:[/dim] [cyan]{strategy.name}[/cyan]")
    # One token per line so the output is easy to eyeball + diff.
    for tok in wrapped:
        console.out(tok)
