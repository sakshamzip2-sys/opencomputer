"""OpenComputer CLI entry point."""

from __future__ import annotations

import typer
from rich.console import Console

from opencomputer import __version__

app = typer.Typer(
    name="opencomputer",
    help="Personal AI agent framework — plugin-first, self-improving, multi-channel.",
    no_args_is_help=False,
)
console = Console()


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
) -> None:
    if version:
        console.print(f"opencomputer {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        chat()


@app.command()
def chat() -> None:
    """Start an interactive chat session (Phase 1 will wire this to the agent loop)."""
    console.print(f"[bold cyan]OpenComputer v{__version__}[/bold cyan]")
    console.print("[dim]Phase 0 scaffold — agent loop not wired yet.[/dim]")
    console.print("[dim]Type 'exit' to quit.[/dim]\n")
    while True:
        try:
            user_input = console.input("[bold green]you ›[/bold green] ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]bye.[/dim]")
            return
        if user_input.strip().lower() in {"exit", "quit", ":q"}:
            console.print("[dim]bye.[/dim]")
            return
        console.print(
            "[bold magenta]oc ›[/bold magenta] "
            "[dim](stub) the agent loop is not yet implemented. "
            "This will route to opencomputer/agent/loop.py in Phase 1.[/dim]\n"
        )


@app.command()
def plugins() -> None:
    """List available plugins (Phase 1)."""
    console.print("[dim]Plugin discovery is not yet implemented (Phase 1).[/dim]")


@app.command()
def skills() -> None:
    """List available skills (Phase 1)."""
    console.print("[dim]Skill discovery is not yet implemented (Phase 1).[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
