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
from opencomputer.plugins.registry import registry as plugin_registry
from opencomputer.providers.anthropic_provider import AnthropicProvider
from opencomputer.tools.bash import BashTool
from opencomputer.tools.delegate import DelegateTool
from opencomputer.tools.glob import GlobTool
from opencomputer.tools.grep import GrepTool
from opencomputer.tools.read import ReadTool
from opencomputer.tools.registry import registry
from opencomputer.tools.skill_manage import SkillManageTool
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
    registry.register(GrepTool())
    registry.register(GlobTool())
    registry.register(SkillManageTool())
    registry.register(DelegateTool())


def _discover_plugins() -> int:
    """Discover + load plugins from known search paths. Returns count loaded."""
    from pathlib import Path

    # In-tree extensions + user plugin dir
    search_paths: list[Path] = []
    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    if ext_dir.exists():
        search_paths.append(ext_dir)
    user_dir = Path.home() / ".opencomputer" / "plugins"
    if user_dir.exists():
        search_paths.append(user_dir)

    loaded = plugin_registry.load_all(search_paths)
    return len(loaded)


def _resolve_provider(provider_name: str):
    """Resolve a provider by name: plugin registry first, then in-tree fallback."""
    # 1. Check plugin registry (e.g. "openai" from openai-provider extension)
    registered = plugin_registry.providers.get(provider_name)
    if registered is not None:
        # Plugins register the CLASS — instantiate with defaults (reads env vars)
        return registered() if isinstance(registered, type) else registered

    # 2. In-tree fallback for anthropic (still bundled for convenience)
    if provider_name == "anthropic":
        return AnthropicProvider()

    raise RuntimeError(
        f"Provider '{provider_name}' is not available. "
        f"Installed plugins: {list(plugin_registry.providers.keys())}. "
        f"Built-in: anthropic."
    )


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


def _check_provider_key(provider_name: str) -> None:
    """Verify the right env var is set for the configured provider."""
    key_env = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider_name)
    if key_env and not os.environ.get(key_env):
        console.print(
            f"[bold red]error:[/bold red] {key_env} not set.\n"
            f"[dim]export {key_env}=your-key to continue.[/dim]"
        )
        raise typer.Exit(1)


@app.command()
def chat(
    resume: str = typer.Option(
        "", "--resume", "-r", help="Resume a session by id (latest if empty)."
    ),
) -> None:
    """Start an interactive chat session."""
    cfg = default_config()
    _check_provider_key(cfg.model.provider)

    _register_builtin_tools()
    n_plugins = _discover_plugins()
    provider = _resolve_provider(cfg.model.provider)
    loop = AgentLoop(provider=provider, config=cfg)

    # Wire the delegate factory so the model can spawn subagents
    DelegateTool.set_factory(lambda: AgentLoop(provider=provider, config=cfg))

    session_id = resume or str(uuid.uuid4())
    console.print(f"[bold cyan]OpenComputer v{__version__}[/bold cyan]")
    console.print(f"[dim]session: {session_id}[/dim]")
    console.print(f"[dim]model:   {cfg.model.model} ({cfg.model.provider})[/dim]")
    console.print(f"[dim]tools:   {', '.join(sorted(registry.names()))}[/dim]")
    console.print(f"[dim]plugins: {n_plugins} loaded[/dim]")
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
def gateway() -> None:
    """Run the gateway daemon — connects all configured channel adapters.

    Requires provider API key + at least one channel token (TELEGRAM_BOT_TOKEN,
    DISCORD_BOT_TOKEN, etc.) in the environment. The same agent loop runs,
    but input comes from channels instead of the terminal.
    """
    from opencomputer.gateway.server import Gateway

    cfg = default_config()
    _check_provider_key(cfg.model.provider)

    _register_builtin_tools()
    n_plugins = _discover_plugins()

    provider = _resolve_provider(cfg.model.provider)
    loop = AgentLoop(provider=provider, config=cfg)
    DelegateTool.set_factory(lambda: AgentLoop(provider=provider, config=cfg))

    gw = Gateway(loop=loop)
    for platform_name, adapter in plugin_registry.channels.items():
        console.print(f"[dim]registering channel:[/dim] [cyan]{platform_name}[/cyan]")
        gw.register_adapter(adapter)

    if not gw.adapters:
        console.print(
            "[bold yellow]warning:[/bold yellow] no channel adapters registered. "
            "Set TELEGRAM_BOT_TOKEN (or another channel token) and ensure the "
            "channel plugin is discovered."
        )
        console.print(f"[dim]plugins loaded: {n_plugins}[/dim]")
        raise typer.Exit(1)

    console.print(
        f"[bold cyan]OpenComputer gateway[/bold cyan] — "
        f"{len(gw.adapters)} channel(s), model={cfg.model.model}"
    )
    console.print("[dim]ctrl+c to stop[/dim]\n")
    try:
        asyncio.run(gw.serve_forever())
    except KeyboardInterrupt:
        console.print("\n[dim]gateway stopped[/dim]")


@app.command()
def plugins() -> None:
    """List discovered plugins (metadata only — no activation)."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    search_paths: list[Path] = []
    ext_dir = repo_root / "extensions"
    if ext_dir.exists():
        search_paths.append(ext_dir)
    user_dir = Path.home() / ".opencomputer" / "plugins"
    if user_dir.exists():
        search_paths.append(user_dir)

    candidates = plugin_registry.list_candidates(search_paths)
    if not candidates:
        console.print("[dim]no plugins found in:[/dim]")
        for p in search_paths:
            console.print(f"[dim]  {p}[/dim]")
        return
    for c in candidates:
        m = c.manifest
        console.print(
            f"[cyan]{m.id}[/cyan] v{m.version} — {m.description or '[no description]'}"
        )
        console.print(f"[dim]  kind: {m.kind}  root: {c.root_dir}[/dim]")


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
