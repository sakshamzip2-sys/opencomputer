"""`opencomputer mcp` CLI subcommand — manage MCP servers in config.yaml.

Subcommands:
    add NAME [--transport stdio|sse|http] [--command CMD] [--url URL]
             [--arg V] [--env K=V] [--header K=V] [--enabled]
    list                       — print configured servers
    remove NAME                — drop a server from config
    test NAME                  — connect + list tools, no register
    enable NAME / disable NAME — flip the enabled flag

All mutations write back to ~/.opencomputer/config.yaml. Source: hermes-agent
+ kimi-cli's MCP CLIs. See OpenComputer/docs/mcp-catalog.md for ready-to-paste
`opencomputer mcp add <preset>` snippets.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config import MCPServerConfig
from opencomputer.agent.config_store import (
    load_config,
    save_config,
)

mcp_app = typer.Typer(name="mcp", help="Manage MCP servers in config.yaml.")
console = Console()


def _parse_kv_list(items: list[str], flag_name: str) -> dict[str, str]:
    """Parse a list of K=V strings into a dict. Raises typer.BadParameter on bad shape."""
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(
                f"--{flag_name} {item!r} is not K=V (e.g. --{flag_name} TOKEN=abc123)"
            )
        k, v = item.split("=", 1)
        if not k:
            raise typer.BadParameter(f"--{flag_name} {item!r} has empty key")
        out[k] = v
    return out


def _save_servers(servers: tuple[MCPServerConfig, ...]) -> Path:
    """Replace mcp.servers in config and persist. Returns the path written."""
    cfg = load_config()
    new_mcp = replace(cfg.mcp, servers=servers)
    new_cfg = replace(cfg, mcp=new_mcp)
    return save_config(new_cfg)


@mcp_app.command("list")
def list_servers() -> None:
    """List every configured MCP server."""
    cfg = load_config()
    if not cfg.mcp.servers:
        console.print("[dim]no MCP servers configured.[/dim]")
        console.print(
            "[dim]add one with: opencomputer mcp add NAME --transport stdio --command CMD ...[/dim]"
        )
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("name")
    table.add_column("transport")
    table.add_column("target")
    table.add_column("enabled")
    for s in cfg.mcp.servers:
        target = f"{s.command} {' '.join(s.args)}".strip() if s.transport == "stdio" else s.url
        table.add_row(
            s.name,
            s.transport,
            target or "[dim](unset)[/dim]",
            "[green]yes[/green]" if s.enabled else "[red]no[/red]",
        )
    console.print(table)


@mcp_app.command("add")
def add_server(
    name: str = typer.Argument(..., help="Server name (used as tool prefix)."),
    transport: str = typer.Option("stdio", "--transport", "-t", help="stdio | sse | http"),
    command: str = typer.Option(
        "", "--command", "-c", help="(stdio) Executable, e.g. python3 or npx."
    ),
    arg: list[str] = typer.Option([], "--arg", "-a", help="(stdio) One argv item per flag."),
    env: list[str] = typer.Option([], "--env", "-e", help="(stdio) K=V env var. Repeatable."),
    url: str = typer.Option("", "--url", "-u", help="(sse/http) Endpoint URL."),
    header: list[str] = typer.Option(
        [], "--header", "-H", help="(sse/http) K=V HTTP header. Repeatable."
    ),
    disabled: bool = typer.Option(
        False, "--disabled", help="Add but leave disabled until you `enable` it."
    ),
) -> None:
    """Add a new MCP server to config.yaml.

    For stdio: --command + --arg(s) + --env(s).
    For sse/http: --url + optional --header(s) for auth.
    """
    if transport not in ("stdio", "sse", "http"):
        raise typer.BadParameter(f"transport must be stdio | sse | http (got {transport!r})")
    if transport == "stdio" and not command:
        raise typer.BadParameter("--command is required for stdio transport")
    if transport in ("sse", "http") and not url:
        raise typer.BadParameter(f"--url is required for {transport} transport")

    new_server = MCPServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=tuple(arg),
        url=url,
        env=_parse_kv_list(env, "env"),
        headers=_parse_kv_list(header, "header"),
        enabled=not disabled,
    )

    cfg = load_config()
    if any(s.name == name for s in cfg.mcp.servers):
        console.print(
            f"[red]error:[/red] server {name!r} already exists. "
            f"Remove first or pick a different name."
        )
        raise typer.Exit(1)

    new_servers = (*cfg.mcp.servers, new_server)
    path = _save_servers(new_servers)
    console.print(
        f"[green]added[/green] {name} ({transport}) → {path}"
        f"  [dim]{'enabled' if new_server.enabled else 'disabled'}[/dim]"
    )


@mcp_app.command("remove")
def remove_server(
    name: str = typer.Argument(..., help="Server name to drop."),
) -> None:
    """Remove an MCP server from config.yaml."""
    cfg = load_config()
    remaining = tuple(s for s in cfg.mcp.servers if s.name != name)
    if len(remaining) == len(cfg.mcp.servers):
        console.print(f"[yellow]not found:[/yellow] {name}")
        raise typer.Exit(1)
    path = _save_servers(remaining)
    console.print(f"[green]removed[/green] {name} → {path}")


def _set_enabled(name: str, enabled: bool) -> None:
    cfg = load_config()
    found = False
    new_servers: list[MCPServerConfig] = []
    for s in cfg.mcp.servers:
        if s.name == name:
            new_servers.append(replace(s, enabled=enabled))
            found = True
        else:
            new_servers.append(s)
    if not found:
        console.print(f"[yellow]not found:[/yellow] {name}")
        raise typer.Exit(1)
    path = _save_servers(tuple(new_servers))
    state = "enabled" if enabled else "disabled"
    console.print(f"[green]{state}[/green] {name} → {path}")


@mcp_app.command("enable")
def enable_server(name: str = typer.Argument(...)) -> None:
    """Mark a server enabled (it will be connected on next gateway/chat run)."""
    _set_enabled(name, True)


@mcp_app.command("disable")
def disable_server(name: str = typer.Argument(...)) -> None:
    """Mark a server disabled (it will be skipped on next run)."""
    _set_enabled(name, False)


@mcp_app.command("status")
def status_servers() -> None:
    """Print a rich status snapshot of every enabled MCP server (IV.4).

    Connects to each enabled server, calls
    :meth:`opencomputer.mcp.client.MCPManager.status_snapshot`, then
    renders a Rich table with name / transport / state / tools /
    version / uptime / last error.

    This is a diagnostic command — it spins up + tears down a fresh
    ``MCPManager`` each call so it never touches the live agent's
    registry. Mirrors Kimi CLI's per-server diagnostics view
    (``sources/kimi-cli/src/kimi_cli/ui/shell/mcp_status.py``).
    """
    cfg = load_config()
    enabled = [s for s in cfg.mcp.servers if s.enabled]
    if not enabled:
        console.print("[dim]no MCP servers configured (or all disabled).[/dim]")
        console.print(
            "[dim]add one with: opencomputer mcp add NAME --transport stdio --command CMD ...[/dim]"
        )
        return

    from opencomputer.mcp.client import MCPManager
    from opencomputer.tools.registry import ToolRegistry

    async def _run() -> list[dict[str, object]]:
        # Fresh registry so this doesn't collide with anything; we just
        # want the snapshot, not to register tools.
        mgr = MCPManager(tool_registry=ToolRegistry())
        try:
            await mgr.connect_all(enabled)
            return mgr.status_snapshot()
        finally:
            await mgr.shutdown()

    snapshot = asyncio.run(_run())

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("name")
    table.add_column("state")
    table.add_column("tools")
    table.add_column("version")
    table.add_column("uptime")
    table.add_column("target")
    table.add_column("last error")

    state_colors = {
        "connected": "[green]connected[/green]",
        "disconnected": "[yellow]disconnected[/yellow]",
        "error": "[red]error[/red]",
    }
    for row in snapshot:
        state = str(row["connection_state"])
        uptime_sec = row["uptime_sec"]
        uptime_str = f"{float(uptime_sec):.1f}s" if uptime_sec is not None else "—"
        table.add_row(
            str(row["name"]),
            state_colors.get(state, state),
            str(row["tool_count"]),
            str(row["version"] or "—"),
            uptime_str,
            str(row["url"] or "—"),
            str(row["last_error"] or ""),
        )
    console.print(table)


@mcp_app.command("test")
def test_server(name: str = typer.Argument(..., help="Server name to test.")) -> None:
    """Connect to one server, list its tools, then disconnect. No registration.

    Useful as a smoke test after `add` — confirms the transport is reachable
    and the server speaks MCP correctly without affecting the live registry.
    """
    cfg = load_config()
    target = next((s for s in cfg.mcp.servers if s.name == name), None)
    if target is None:
        console.print(f"[red]error:[/red] {name!r} is not configured.")
        console.print("[dim]run `opencomputer mcp list` to see configured servers.[/dim]")
        raise typer.Exit(1)

    from opencomputer.mcp.client import MCPConnection

    async def _run():
        conn = MCPConnection(config=target)
        ok = await conn.connect()
        if not ok:
            console.print(f"[red]✗[/red] {name} ({target.transport}) — connect failed (see logs)")
            return 1
        try:
            console.print(
                f"[green]✓[/green] {name} ({target.transport}) — {len(conn.tools)} tool(s):"
            )
            for tool in conn.tools:
                console.print(f"  [cyan]{tool.tool_name}[/cyan] — {tool.description}")
            return 0
        finally:
            await conn.disconnect()

    rc = asyncio.run(_run())
    if rc:
        raise typer.Exit(rc)


__all__ = ["mcp_app"]
