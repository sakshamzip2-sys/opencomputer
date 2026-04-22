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
from opencomputer.agent.config_store import (
    config_file_path,
    get_value,
    load_config,
    save_config,
    set_value,
)
from opencomputer.agent.loop import AgentLoop
from opencomputer.plugins.registry import registry as plugin_registry
from opencomputer.tools.bash import BashTool
from opencomputer.tools.delegate import DelegateTool
from opencomputer.tools.glob import GlobTool
from opencomputer.tools.grep import GrepTool
from opencomputer.tools.read import ReadTool
from opencomputer.tools.registry import registry
from opencomputer.tools.skill_manage import SkillManageTool
from opencomputer.tools.web_fetch import WebFetchTool
from opencomputer.tools.web_search import WebSearchTool
from opencomputer.tools.write import WriteTool
from plugin_sdk.runtime_context import RuntimeContext

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
    registry.register(WebFetchTool())
    registry.register(WebSearchTool())


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
    """Resolve a provider by name from the plugin registry.

    Providers are plugins — discovered via plugin.json + activated on demand.
    There is no in-tree fallback: if a provider isn't registered, the user
    needs to install (or enable) the corresponding plugin.
    """
    registered = plugin_registry.providers.get(provider_name)
    if registered is None:
        raise RuntimeError(
            f"Provider '{provider_name}' is not available. "
            f"Installed providers: {list(plugin_registry.providers.keys()) or 'none'}. "
            f"Ensure the relevant plugin is in extensions/ or ~/.opencomputer/plugins/."
        )
    # Plugins register the CLASS — instantiate with defaults (reads env vars)
    return registered() if isinstance(registered, type) else registered


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
    plan: bool = typer.Option(
        False, "--plan", help="Plan mode — agent describes actions, refuses destructive tools."
    ),
    no_compact: bool = typer.Option(
        False, "--no-compact", help="Disable automatic context compaction (debugging)."
    ),
) -> None:
    """Start an interactive chat session."""
    cfg = load_config()
    _check_provider_key(cfg.model.provider)

    from opencomputer.mcp.client import MCPManager

    _register_builtin_tools()
    n_plugins = _discover_plugins()
    provider = _resolve_provider(cfg.model.provider)
    runtime = RuntimeContext(plan_mode=plan)
    loop = AgentLoop(provider=provider, config=cfg, compaction_disabled=no_compact)
    mcp_mgr = MCPManager(tool_registry=registry)

    # Wire the delegate factory so the model can spawn subagents
    DelegateTool.set_factory(
        lambda: AgentLoop(provider=provider, config=cfg, compaction_disabled=no_compact)
    )
    DelegateTool.set_runtime(runtime)

    # Connect MCP servers synchronously in chat mode (simpler — no event loop yet)
    n_mcp_tools = 0
    if cfg.mcp.servers:
        n_mcp_tools = asyncio.run(mcp_mgr.connect_all(list(cfg.mcp.servers)))

    session_id = resume or str(uuid.uuid4())
    console.print(f"[bold cyan]OpenComputer v{__version__}[/bold cyan]")
    console.print(f"[dim]session: {session_id}[/dim]")
    console.print(f"[dim]model:   {cfg.model.model} ({cfg.model.provider})[/dim]")
    console.print(f"[dim]tools:   {', '.join(sorted(registry.names()))}[/dim]")
    console.print(f"[dim]plugins: {n_plugins} loaded[/dim]")
    if plan:
        console.print("[bold yellow]plan mode ON[/bold yellow] — destructive tools will be refused")
    if no_compact:
        console.print("[dim]compaction disabled[/dim]")
    if cfg.mcp.servers:
        console.print(f"[dim]mcp:     {n_mcp_tools} tool(s) from {len(cfg.mcp.servers)} server(s)[/dim]")
    console.print("[dim]Type 'exit' to quit. Ctrl+C to interrupt.[/dim]\n")

    async def _run_turn(user_input: str) -> None:
        # Stream tokens to the terminal as they arrive
        printed_header = {"val": False}

        def on_chunk(text: str) -> None:
            if not printed_header["val"]:
                console.print("[bold magenta]oc ›[/bold magenta] ", end="")
                printed_header["val"] = True
            # Print raw text (not markdown) so streaming is smooth;
            # final full message is re-rendered as Markdown below.
            console.print(text, end="", markup=False, highlight=False)

        result = await loop.run_conversation(
            user_message=user_input,
            session_id=session_id,
            runtime=runtime,
            stream_callback=on_chunk,
        )
        # Newline after streaming content (if any)
        if printed_header["val"]:
            console.print()
        # Re-render as Markdown for code fences / lists if content is present
        # and wasn't already streamed as text (prevents double output).
        if result.final_message.content.strip() and not printed_header["val"]:
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
def wire(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(18789, "--port"),
) -> None:
    """Run the wire server — JSON-over-WebSocket API for TUI / IDE / web clients."""
    from opencomputer.gateway.wire_server import WireServer

    cfg = load_config()
    _check_provider_key(cfg.model.provider)

    _register_builtin_tools()
    _discover_plugins()

    provider = _resolve_provider(cfg.model.provider)
    loop = AgentLoop(provider=provider, config=cfg)
    DelegateTool.set_factory(lambda: AgentLoop(provider=provider, config=cfg))

    server = WireServer(loop=loop, host=host, port=port)
    console.print(
        f"[bold cyan]OpenComputer wire server[/bold cyan] — ws://{host}:{port}"
    )
    console.print(f"[dim]model: {cfg.model.model} ({cfg.model.provider})[/dim]")
    console.print("[dim]ctrl+c to stop[/dim]\n")

    async def _run():
        await server.start()
        try:
            await asyncio.Future()  # run forever
        finally:
            await server.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]wire server stopped[/dim]")


@app.command()
def gateway() -> None:
    """Run the gateway daemon — connects all configured channel adapters.

    Requires provider API key + at least one channel token (TELEGRAM_BOT_TOKEN,
    DISCORD_BOT_TOKEN, etc.) in the environment. The same agent loop runs,
    but input comes from channels instead of the terminal.
    """
    from opencomputer.gateway.server import Gateway
    from opencomputer.mcp.client import MCPManager

    cfg = load_config()
    _check_provider_key(cfg.model.provider)

    _register_builtin_tools()
    n_plugins = _discover_plugins()

    provider = _resolve_provider(cfg.model.provider)
    loop = AgentLoop(provider=provider, config=cfg)
    DelegateTool.set_factory(lambda: AgentLoop(provider=provider, config=cfg))

    # Connect to MCP servers in the background (kimi-cli deferred pattern)
    mcp_mgr = MCPManager(tool_registry=registry)
    if cfg.mcp.servers:
        console.print(
            f"[dim]mcp: deferring connection to {len(cfg.mcp.servers)} server(s)[/dim]"
        )

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

    async def _run():
        if cfg.mcp.servers:
            asyncio.create_task(
                mcp_mgr.connect_all(list(cfg.mcp.servers))
            )
        try:
            await gw.serve_forever()
        finally:
            await mcp_mgr.shutdown()

    try:
        asyncio.run(_run())
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
def setup() -> None:
    """Interactive first-run wizard — pick provider, enter key, test."""
    from opencomputer.setup_wizard import run_setup

    run_setup()


@app.command()
def doctor(
    fix: bool = typer.Option(
        False, "--fix", help="Invoke plugin-contributed repairs in place."
    ),
) -> None:
    """Diagnose common config/env issues.

    With --fix, every plugin-registered HealthContribution is invoked with
    fix=True and is expected to repair state (e.g. migrate a legacy config
    shape, rewrite broken skill frontmatter) rather than merely report.
    """
    from opencomputer.doctor import run_doctor

    failures = run_doctor(fix=fix)
    if failures:
        raise typer.Exit(1)


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


config_app = typer.Typer(
    name="config", help="Manage OpenComputer config (~/.opencomputer/config.yaml)"
)
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show() -> None:
    """Print current effective config (defaults + overrides from disk)."""
    import yaml

    from opencomputer.agent.config_store import _to_yaml_dict

    cfg = load_config()
    console.print(yaml.safe_dump(_to_yaml_dict(cfg), default_flow_style=False, sort_keys=False))


@config_app.command("get")
def config_get(key: str = typer.Argument(..., help="Dotted key, e.g. model.provider")) -> None:
    """Get a single config value by dotted key."""
    cfg = load_config()
    try:
        value = get_value(cfg, key)
    except KeyError as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        raise typer.Exit(1) from None
    console.print(str(value))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Dotted key, e.g. model.provider"),
    value: str = typer.Argument(..., help="New value"),
) -> None:
    """Set a config value and persist to ~/.opencomputer/config.yaml."""
    cfg = load_config()
    # Attempt to coerce numeric / bool / path values sensibly
    coerced: object = value
    if value.lower() in {"true", "false"}:
        coerced = value.lower() == "true"
    else:
        try:
            coerced = int(value)
        except ValueError:
            try:
                coerced = float(value)
            except ValueError:
                coerced = value
    try:
        new_cfg = set_value(cfg, key, coerced)
    except KeyError as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        raise typer.Exit(1) from None
    save_config(new_cfg)
    console.print(f"[green]✓[/green] {key} = {coerced!r}")
    console.print(f"[dim]saved to {config_file_path()}[/dim]")


@config_app.command("path")
def config_path() -> None:
    """Print the path to the config file."""
    console.print(str(config_file_path()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
