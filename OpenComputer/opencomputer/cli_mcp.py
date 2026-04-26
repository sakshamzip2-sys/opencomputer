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
            await mgr.connect_all(
                enabled,
                osv_check_enabled=cfg.mcp.osv_check_enabled,
                osv_check_fail_closed=cfg.mcp.osv_check_fail_closed,
            )
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


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Start an MCP server exposing OpenComputer's session history over stdio.

    Run this from another MCP client (Claude Code, Cursor, …) to query OC's
    sessions, search across past conversations, and read F1 consent audit
    entries.

    Tools exposed: ``sessions_list``, ``session_get``, ``messages_read``,
    ``recall_search``, ``consent_history``.

    Saksham use case: Claude Code while coding can call ``recall_search``
    to surface past Telegram discussions about a stock or codebase decision.

    The server runs until stdin/stdout closes (SIGINT also exits cleanly).
    """
    from opencomputer.mcp.server import main as serve_main

    serve_main()


@mcp_app.command("presets")
def mcp_presets() -> None:
    """List bundled MCP presets — vetted one-line installs for common MCPs.

    Use ``opencomputer mcp install <preset>`` to add one to config.yaml.
    """
    from opencomputer.mcp.presets import PRESETS

    if not PRESETS:
        console.print("[dim]No presets bundled.[/dim]")
        return

    table = Table(title=f"MCP Presets ({len(PRESETS)})")
    table.add_column("Slug", style="cyan", no_wrap=True)
    table.add_column("Description")
    table.add_column("Required env", style="yellow")
    for slug, p in PRESETS.items():
        env_str = ", ".join(p.required_env) or "—"
        table.add_row(slug, p.description, env_str)
    console.print(table)
    console.print(
        "\n[dim]Install: [bold]opencomputer mcp install <slug>[/bold] — "
        "adds the server to config.yaml.[/dim]"
    )


@mcp_app.command("catalog")
def mcp_catalog() -> None:
    """List bundled MCP catalog entries (alias for ``mcp presets``).

    ``catalog`` is the friendlier name for users coming from package
    managers (pip, npm, brew). Functionally identical to ``presets``;
    both stay so existing scripts keep working.
    """
    mcp_presets()


@mcp_app.command("oauth-paste")
def mcp_oauth_paste(
    provider: str = typer.Argument(..., help="Provider slug (github / google / notion / etc.)"),
    token: str = typer.Option(
        "",
        "--token",
        "-t",
        help="Token value. If omitted, prompts securely on stdin (hidden input).",
    ),
    token_type: str = typer.Option(
        "Personal Access Token",
        "--type",
        help="Bearer / Personal Access Token / etc.",
    ),
    scope: str = typer.Option(
        "", "--scope", "-s", help="Space-separated scope string (optional)."
    ),
) -> None:
    """Paste an OAuth token / PAT for an MCP provider into the secure store.

    For GitHub: create a PAT at github.com/settings/tokens, then::

        opencomputer mcp oauth-paste github

    The token is read from stdin (hidden) and written to
    ``<profile_home>/mcp_oauth/github.json`` (mode 0600). Subsequent
    MCP launches that reference ``${GITHUB_PERSONAL_ACCESS_TOKEN}`` will
    fall back to this stored token when the env var is unset.
    """
    from opencomputer.mcp.oauth import paste_token

    if not token:
        token = typer.prompt("token", hide_input=True)
    if not token.strip():
        console.print("[red]error:[/red] token must be non-empty")
        raise typer.Exit(1)

    try:
        path = paste_token(
            provider=provider,
            access_token=token,
            token_type=token_type,
            scope=scope or None,
        )
    except ValueError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(f"[green]stored[/green] token for {provider!r} → {path}")
    console.print("[dim]Token file is mode 0600 (owner-only).[/dim]")


@mcp_app.command("oauth-login")
def mcp_oauth_login(
    provider: str = typer.Argument(
        ..., help="Provider slug (e.g. github / notion / google)."
    ),
    authorization_url: str = typer.Option(
        ..., "--authorization-url", "-A", help="OAuth /authorize endpoint URL."
    ),
    token_url: str = typer.Option(
        ..., "--token-url", "-T", help="OAuth /token endpoint URL."
    ),
    client_id: str = typer.Option(
        ..., "--client-id", "-i", help="OAuth client id registered with the provider."
    ),
    scope: str = typer.Option(
        "", "--scope", "-s", help="Space-separated scope string (provider-specific)."
    ),
    timeout_s: int = typer.Option(
        300, "--timeout", help="Seconds to wait for the browser callback."
    ),
) -> None:
    """Run the OAuth 2.1 PKCE flow against an MCP provider's OAuth endpoints.

    Opens the user's browser to the authorization URL, hosts an ephemeral
    HTTP callback server on ``127.0.0.1`` (kernel-picked port), and on
    success exchanges the authorization code for a token via the token
    endpoint, then persists the result via :class:`OAuthTokenStore`.

    ``--authorization-url`` and ``--token-url`` are required because the
    MCP server-config schema does not yet carry OAuth manifest fields;
    callers paste them directly from the provider's docs.

    Security defenses (every one is intentional):
        - PKCE verifier is 256-bit (``secrets.token_urlsafe(64)``).
        - ``state`` (CSRF) compared in constant time.
        - Callback bound to ``127.0.0.1`` ONLY (never ``0.0.0.0``).
        - 5-minute default timeout with clean shutdown.
        - Token persisted to mode-0600 file via the existing store.
    """
    import time

    from opencomputer.mcp.oauth import OAuthToken, OAuthTokenStore
    from opencomputer.mcp.oauth_pkce import (
        OAuthCallbackError,
        OAuthFlowTimeout,
        OAuthStateMismatch,
        run_pkce_flow,
    )

    console.print(
        f"[cyan]Starting OAuth 2.1 PKCE flow for {provider!r}…[/cyan]"
    )
    try:
        token_response = run_pkce_flow(
            authorization_url=authorization_url,
            token_url=token_url,
            client_id=client_id,
            scope=scope,
            timeout_s=timeout_s,
        )
    except OAuthFlowTimeout as exc:
        console.print(f"[red]timeout:[/red] {exc}")
        raise typer.Exit(2) from exc
    except OAuthStateMismatch as exc:
        console.print(f"[red]CSRF / state mismatch:[/red] {exc}")
        raise typer.Exit(3) from exc
    except OAuthCallbackError as exc:
        console.print(f"[red]callback error:[/red] {exc}")
        raise typer.Exit(4) from exc

    access_token = token_response.get("access_token")
    if not access_token:
        console.print(
            "[red]error:[/red] token endpoint did not return an 'access_token' field. "
            f"Got keys: {sorted(token_response.keys())!r}"
        )
        raise typer.Exit(5)

    expires_in = token_response.get("expires_in")
    expires_at: float | None
    expires_at = time.time() + float(expires_in) if isinstance(expires_in, int | float) else None

    stored = OAuthToken(
        provider=provider.strip().lower(),
        access_token=str(access_token),
        token_type=str(token_response.get("token_type") or "Bearer"),
        expires_at=expires_at,
        scope=token_response.get("scope") or (scope or None),
        refresh_token=token_response.get("refresh_token"),
        created_at=time.time(),
    )
    path = OAuthTokenStore().put(stored)
    console.print(f"[green]stored[/green] token for {provider!r} → {path}")
    console.print("[dim]Token file is mode 0600 (owner-only).[/dim]")


@mcp_app.command("oauth-list")
def mcp_oauth_list() -> None:
    """Show all OAuth/PAT tokens currently stored. Token values are NEVER printed."""
    from opencomputer.mcp.oauth import OAuthTokenStore

    tokens = OAuthTokenStore().list()
    if not tokens:
        console.print("[dim]No OAuth tokens stored.[/dim]")
        console.print(
            "[dim]Add one with `opencomputer mcp oauth-paste <provider>`.[/dim]"
        )
        return

    from rich.table import Table

    table = Table(title=f"OAuth Tokens ({len(tokens)})")
    table.add_column("Provider", style="cyan")
    table.add_column("Type")
    table.add_column("Scope")
    table.add_column("Expires")
    table.add_column("Created")
    for t in tokens:
        expires = (
            "never"
            if t.expires_at is None
            else f"{(t.expires_at - __import__('time').time()) / 86400:.0f}d"
        )
        created = "—" if not t.created_at else f"{t.created_at:.0f}"
        table.add_row(t.provider, t.token_type, t.scope or "—", expires, created)
    console.print(table)


@mcp_app.command("oauth-revoke")
def mcp_oauth_revoke(
    provider: str = typer.Argument(..., help="Provider slug to delete the token for."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a stored OAuth/PAT token. Doesn't revoke at the provider — caller does that."""
    from opencomputer.mcp.oauth import OAuthTokenStore

    if not yes and not typer.confirm(f"Delete stored token for {provider!r}?"):
        typer.echo("Cancelled.")
        raise typer.Exit(0)
    if not OAuthTokenStore().revoke(provider):
        console.print(f"[yellow]not found:[/yellow] {provider}")
        raise typer.Exit(1)
    console.print(f"[green]revoked[/green] {provider}")
    console.print(
        "[dim]Note: this only deletes the local file. Revoke at the provider too "
        "(github.com/settings/tokens for GitHub PATs).[/dim]"
    )


@mcp_app.command("install")
def mcp_install(
    preset: str = typer.Argument(..., help="Preset slug (e.g. filesystem, github, fetch)."),
    name: str = typer.Option(
        "",
        "--name",
        "-n",
        help="Override the registered server name (defaults to the preset slug).",
    ),
    disabled: bool = typer.Option(
        False, "--disabled", help="Add but leave disabled until you `enable` it."
    ),
) -> None:
    """Install a bundled MCP preset into config.yaml.

    Examples::

        opencomputer mcp install filesystem    # local file access
        opencomputer mcp install github        # needs GITHUB_PERSONAL_ACCESS_TOKEN
        opencomputer mcp install fetch         # web fetcher

    See ``opencomputer mcp presets`` for the full list.
    """
    import os

    from opencomputer.mcp.presets import get_preset, list_preset_slugs

    p = get_preset(preset)
    if p is None:
        console.print(
            f"[red]error:[/red] unknown preset {preset!r}. "
            f"Available: {', '.join(list_preset_slugs())}"
        )
        raise typer.Exit(1)

    cfg = load_config()
    server_name = name or p.config.name
    if any(s.name == server_name for s in cfg.mcp.servers):
        console.print(
            f"[red]error:[/red] server {server_name!r} already exists. "
            f"Remove first with `opencomputer mcp remove {server_name}` "
            "or pick a different --name."
        )
        raise typer.Exit(1)

    new_server = MCPServerConfig(
        name=server_name,
        transport=p.config.transport,
        command=p.config.command,
        args=p.config.args,
        url=p.config.url,
        env=dict(p.config.env),
        headers=dict(p.config.headers),
        enabled=not disabled,
    )

    new_servers = (*cfg.mcp.servers, new_server)
    path = _save_servers(new_servers)
    console.print(
        f"[green]installed[/green] preset {preset!r} as {server_name!r} → {path}"
    )

    # Surface env-var prerequisites with a clear status icon.
    if p.required_env:
        console.print("\n[bold]Required environment variables:[/bold]")
        for var in p.required_env:
            present = bool(os.environ.get(var))
            icon = "[green]✓[/green]" if present else "[yellow]✗ unset[/yellow]"
            console.print(f"  {icon}  {var}")
        if not all(os.environ.get(v) for v in p.required_env):
            console.print(
                "\n[yellow]Set the missing vars before the next agent run, "
                "or the server will fail to start.[/yellow]"
            )

    if p.homepage:
        console.print(f"\n[dim]docs: {p.homepage}[/dim]")


@mcp_app.command("scaffold")
def mcp_scaffold(
    name: str = typer.Argument(
        ..., help="MCP server name (folder + module name; lowercase, hyphens or underscores).",
    ),
    target_dir: Path = typer.Option(
        Path.cwd(),
        "--dir",
        "-d",
        help="Parent directory where <name>/ will be created. Defaults to CWD.",
    ),
    transport: str = typer.Option(
        "stdio",
        "--transport",
        "-t",
        help="MCP transport for the generated server (stdio | sse | http).",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite an existing folder at <dir>/<name>."
    ),
) -> None:
    """Generate a minimal Python MCP server scaffold.

    Creates ``<dir>/<name>/`` with:

    * ``server.py``     — FastMCP app with one demo tool (``echo``).
    * ``pyproject.toml`` — runnable via ``python -m <pkg>`` or ``uvx``.
    * ``README.md``     — install/run instructions.

    Sub-project G.30 (Tier 4 OpenClaw port). Mirrors the upstream
    FastMCP "create new server" flow but writes the files locally so
    Saksham can hack on them. Once the server runs, register it with
    ``opencomputer mcp add <name> --transport stdio --command 'python
    -m <name>.server'``.
    """
    # Normalise the name: lowercase, hyphens → underscores for the
    # Python package name. Keep the folder name as the user typed it.
    folder_name = name.strip()
    if not folder_name or "/" in folder_name or "\\" in folder_name:
        console.print(f"[red]error:[/red] invalid name {name!r}")
        raise typer.Exit(1)
    pkg_name = folder_name.replace("-", "_").lower()
    if not pkg_name.isidentifier():
        console.print(
            f"[red]error:[/red] {name!r} doesn't yield a valid Python package "
            f"name (got {pkg_name!r}). Use lowercase letters, digits, hyphens, "
            "or underscores."
        )
        raise typer.Exit(1)
    if transport not in ("stdio", "sse", "http"):
        console.print(
            f"[red]error:[/red] --transport must be one of stdio|sse|http; got {transport!r}"
        )
        raise typer.Exit(1)

    target = target_dir / folder_name
    if target.exists() and not force:
        console.print(
            f"[red]error:[/red] {target} already exists. Pass --force to overwrite, "
            "or pick a different --dir/--name."
        )
        raise typer.Exit(1)

    target.mkdir(parents=True, exist_ok=True)
    (target / pkg_name).mkdir(exist_ok=True)

    # __init__.py — keeps it importable as a package.
    (target / pkg_name / "__init__.py").write_text(
        f'"""MCP server package: {pkg_name}."""\n', encoding="utf-8"
    )

    # server.py — FastMCP app with one demo tool.
    server_py = f'''"""FastMCP server: {pkg_name}.

Run with::

    python -m {pkg_name}.server

Or register into OpenComputer::

    opencomputer mcp add {folder_name} \\
        --transport {transport} \\
        --command 'python -m {pkg_name}.server'
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


server = FastMCP(name="{pkg_name}")


@server.tool()
def echo(text: str) -> str:
    """Return ``text`` unchanged. Replace this with your own tools."""
    return text


def main() -> None:
    """Run the server on the chosen transport (default stdio)."""
    server.run(transport="{transport}")


if __name__ == "__main__":
    main()
'''
    (target / pkg_name / "server.py").write_text(server_py, encoding="utf-8")

    # pyproject.toml — minimal runnable package.
    pyproject = f'''[project]
name = "{folder_name}"
version = "0.1.0"
description = "MCP server scaffold generated by `opencomputer mcp scaffold`."
requires-python = ">=3.11"
dependencies = ["mcp>=1.0"]

[project.scripts]
{folder_name} = "{pkg_name}.server:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["{pkg_name}"]
'''
    (target / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    # README.md — quickstart.
    readme = f"""# {folder_name}

Minimal MCP server scaffold generated by `opencomputer mcp scaffold {folder_name}`.

## Quickstart

```bash
pip install -e .

# Run the server (stdio transport):
python -m {pkg_name}.server

# Or invoke the project script:
{folder_name}
```

## Register with OpenComputer

```bash
opencomputer mcp add {folder_name} \\
  --transport {transport} \\
  --command 'python -m {pkg_name}.server'
```

The starter server exposes a single tool `echo(text: str) -> str`. Replace it
with your own tools in `{pkg_name}/server.py`. Each tool needs the
`@server.tool()` decorator and a typed signature.

## Resources

- FastMCP docs: https://github.com/modelcontextprotocol/python-sdk
- MCP spec: https://spec.modelcontextprotocol.io/
"""
    (target / "README.md").write_text(readme, encoding="utf-8")

    console.print(
        f"[green]scaffolded[/green] MCP server {folder_name!r} → "
        f"[dim]{target}[/dim]"
    )
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print(f"  cd {target}")
    console.print("  pip install -e .")
    console.print(f"  python -m {pkg_name}.server")
    console.print()
    console.print("[bold]Register with OpenComputer:[/bold]")
    console.print(
        f"  opencomputer mcp add {folder_name} \\\n"
        f"    --transport {transport} \\\n"
        f"    --command 'python -m {pkg_name}.server'"
    )


__all__ = ["mcp_app"]
