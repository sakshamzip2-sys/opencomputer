"""OpenComputer CLI entry point — an actual working chat loop."""

from __future__ import annotations

import asyncio
import os
import uuid

import typer
from rich.console import Console
from rich.markdown import Markdown

from opencomputer import __version__
from opencomputer.agent.config import default_config
from opencomputer.agent.loop import AgentLoop
from opencomputer.providers.anthropic_provider import AnthropicProvider
from opencomputer.tools.bash import BashTool
from opencomputer.tools.read import ReadTool
from opencomputer.tools.registry import registry
from opencomputer.tools.write import WriteTool

app = typer.Typer(
    name="opencomputer",
    help="Personal AI agent framework — plugin-first, self-improving, multi-channel.",
    no_args_is_help=False,
)
console = Console()


def _register_builtin_tools() -> None:
    """Register the core bundled tools. Only runs once per process."""
    if "Read" in registry.names():
        return
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(BashTool())


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
def chat(
    resume: str = typer.Option(
        "", "--resume", "-r", help="Resume a session by id (latest if empty)."
    ),
) -> None:
    """Start an interactive chat session."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[bold red]error:[/bold red] ANTHROPIC_API_KEY not set.\n"
            "[dim]export ANTHROPIC_API_KEY=your-key to continue.[/dim]"
        )
        raise typer.Exit(1)

    _register_builtin_tools()
    cfg = default_config()
    provider = AnthropicProvider()
    loop = AgentLoop(provider=provider, config=cfg)

    session_id = resume or str(uuid.uuid4())
    console.print(f"[bold cyan]OpenComputer v{__version__}[/bold cyan]")
    console.print(f"[dim]session: {session_id}[/dim]")
    console.print(f"[dim]model:   {cfg.model.model} ({cfg.model.provider})[/dim]")
    console.print(f"[dim]tools:   {', '.join(sorted(registry.names()))}[/dim]")
    console.print("[dim]Type 'exit' to quit. Ctrl+C to interrupt.[/dim]\n")

    async def _run_turn(user_input: str) -> None:
        result = await loop.run_conversation(
            user_message=user_input, session_id=session_id
        )
        if result.final_message.content.strip():
            console.print("[bold magenta]oc ›[/bold magenta]")
            console.print(Markdown(result.final_message.content))
        console.print(
            f"[dim]({result.iterations} iterations · "
            f"{result.input_tokens} in / {result.output_tokens} out)[/dim]\n"
        )

    while True:
        try:
            user_input = console.input("[bold green]you ›[/bold green] ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]bye.[/dim]")
            return
        if user_input.strip().lower() in {"exit", "quit", ":q"}:
            console.print("[dim]bye.[/dim]")
            return
        if not user_input.strip():
            continue
        try:
            asyncio.run(_run_turn(user_input))
        except Exception as e:
            console.print(f"[bold red]error:[/bold red] {type(e).__name__}: {e}")


@app.command()
def search(
    query: str = typer.Argument(..., help="Query to search across past sessions."),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Full-text search across saved sessions (FTS5)."""
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    db = SessionDB(cfg.session.db_path)
    hits = db.search(query, limit=limit)
    if not hits:
        console.print("[dim]no matches[/dim]")
        return
    for h in hits:
        console.print(
            f"[cyan]{h['role']}[/cyan] [dim]({h['session_id'][:8]}…)[/dim]  {h['snippet']}"
        )


@app.command()
def sessions(limit: int = typer.Option(10, "--limit", "-n")) -> None:
    """List recent sessions."""
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    db = SessionDB(cfg.session.db_path)
    rows = db.list_sessions(limit=limit)
    for r in rows:
        title = r.get("title") or "[untitled]"
        console.print(
            f"[dim]{r['id'][:8]}…[/dim] "
            f"msgs={r['message_count']:<3} {title}"
        )


@app.command()
def plugins() -> None:
    """List available plugins (Phase 1)."""
    console.print("[dim]Plugin discovery is not yet implemented (Phase 1 late).[/dim]")


@app.command()
def skills() -> None:
    """List available skills."""
    from opencomputer.agent.memory import MemoryManager

    cfg = default_config()
    mem = MemoryManager(cfg.memory.declarative_path, cfg.memory.skills_path)
    found = mem.list_skills()
    if not found:
        console.print("[dim]no skills found at[/dim] " + str(cfg.memory.skills_path))
        return
    for s in found:
        console.print(f"[cyan]{s.name}[/cyan] — {s.description}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
