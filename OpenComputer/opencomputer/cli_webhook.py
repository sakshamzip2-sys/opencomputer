"""``opencomputer webhook`` CLI — manage webhook tokens.

Subcommands:

    opencomputer webhook list                                 — show tokens
    opencomputer webhook create --name N [--scope S] [--notify telegram]  — issue
    opencomputer webhook revoke <token_id>                    — soft-disable
    opencomputer webhook remove <token_id>                    — hard-delete
    opencomputer webhook info <token_id>                      — full metadata

Tokens live at ``<profile_home>/webhook_tokens.json`` (mode 0600).

Note: the actual webhook HTTP listener is provided by the
``extensions/webhook/`` plugin. Enable it in your active profile to start
serving inbound POSTs.
"""

from __future__ import annotations

# Import directly from the plugin path so the CLI doesn't require the plugin
# to be enabled in the profile (token management should work regardless).
import importlib.util
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

_TOKENS_PATH = (
    Path(__file__).resolve().parent.parent / "extensions" / "webhook" / "tokens.py"
)


def _load_tokens_module():
    spec = importlib.util.spec_from_file_location("webhook_tokens_cli", _TOKENS_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


webhook_app = typer.Typer(
    name="webhook",
    help="Manage webhook tokens (HTTP triggers from external services).",
    no_args_is_help=True,
)
_console = Console()


@webhook_app.command("list")
def webhook_list(
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Include revoked tokens.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List webhook tokens (secrets are NEVER shown — use --info on a single token to see metadata)."""
    tokens_mod = _load_tokens_module()
    tokens = tokens_mod.list_tokens(include_revoked=show_all)

    if json_output:
        typer.echo(json.dumps(tokens, default=str, indent=2))
        return

    if not tokens:
        _console.print("[dim]No webhook tokens.[/dim]")
        _console.print("[dim]Create one with `opencomputer webhook create --name <name>`[/dim]")
        return

    table = Table(title=f"Webhook Tokens ({len(tokens)})")
    table.add_column("Token ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Scopes")
    table.add_column("Notify")
    table.add_column("Last Used", style="yellow")
    table.add_column("State")
    for t in tokens:
        scopes = ", ".join(t.get("scopes") or []) or "—"
        last = t.get("last_used_at")
        last_str = "—" if not last else f"{last:.0f}"
        state = "revoked" if t.get("revoked") else "active"
        table.add_row(
            t["token_id"][:8],
            t.get("name", "")[:30],
            scopes[:40],
            t.get("notify") or "—",
            last_str,
            state,
        )
    _console.print(table)


@webhook_app.command("create")
def webhook_create(
    name: Annotated[str, typer.Option("--name", "-n", help="Friendly name (e.g., 'tradingview-alerts').")],
    scope: Annotated[list[str] | None, typer.Option("--scope", "-s", help="Repeatable. Examples: 'skill:stock-market-analysis'.")] = None,
    notify: Annotated[str | None, typer.Option("--notify", help="Default channel for output (telegram/discord).")] = None,
) -> None:
    """Create a new webhook token. Secret is shown ONCE."""
    tokens_mod = _load_tokens_module()
    token_id, secret = tokens_mod.create_token(name=name, scopes=scope or [], notify=notify)

    typer.secho(f"Created webhook token '{name}'", fg="green")
    typer.echo("")
    typer.secho("  token_id:", fg="cyan", nl=False)
    typer.echo(f" {token_id}")
    typer.secho("  secret:  ", fg="red", nl=False)
    typer.echo(f" {secret}")
    typer.echo("")
    typer.secho("⚠️  Save the secret now — it will NOT be shown again.", fg="yellow")
    typer.echo("")
    typer.echo("To send a test webhook:")
    typer.echo(
        f'  curl -X POST http://127.0.0.1:18790/webhook/{token_id} \\\n'
        f'    -H "Content-Type: application/json" \\\n'
        f'    -H "X-Webhook-Signature: sha256=$(echo -n \'{{"text":"hello"}}\' | openssl dgst -sha256 -hmac \'{secret}\' | sed \'s/^.* //\')" \\\n'
        f'    -d \'{{"text":"hello"}}\''
    )


@webhook_app.command("revoke")
def webhook_revoke(token_id: Annotated[str, typer.Argument(help="Token id (full or first 8 chars).")]) -> None:
    """Mark a token as revoked. Future requests with this token return 401."""
    tokens_mod = _load_tokens_module()
    full_id = _resolve_token_id(tokens_mod, token_id)
    if not full_id:
        typer.secho(f"token_id={token_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)
    if not tokens_mod.revoke_token(full_id):
        typer.secho(f"failed to revoke {full_id}", fg="red", err=True)
        raise typer.Exit(2)
    typer.secho(f"Revoked webhook token {full_id}", fg="yellow")


@webhook_app.command("remove")
def webhook_remove(
    token_id: Annotated[str, typer.Argument(help="Token id.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Permanently delete a token from the registry."""
    tokens_mod = _load_tokens_module()
    full_id = _resolve_token_id(tokens_mod, token_id)
    if not full_id:
        typer.secho(f"token_id={token_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)
    if not yes and not typer.confirm(f"Permanently delete token {full_id}?"):
        typer.echo("Cancelled.")
        raise typer.Exit(0)
    if not tokens_mod.remove_token(full_id):
        typer.secho(f"failed to remove {full_id}", fg="red", err=True)
        raise typer.Exit(2)
    typer.secho(f"Removed webhook token {full_id}", fg="green")


@webhook_app.command("info")
def webhook_info(token_id: Annotated[str, typer.Argument(help="Token id.")]) -> None:
    """Show one token's full metadata (secret is redacted)."""
    tokens_mod = _load_tokens_module()
    full_id = _resolve_token_id(tokens_mod, token_id)
    if not full_id:
        typer.secho(f"token_id={token_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)
    meta = tokens_mod.get_token(full_id)
    view = dict(meta)
    if "secret" in view:
        view["secret"] = "<redacted — only shown on create>"
    view["token_id"] = full_id
    typer.echo(json.dumps(view, default=str, indent=2))


def _resolve_token_id(tokens_mod, partial: str) -> str | None:
    """Accept a full token_id or its first-8-char prefix. Returns full id or None."""
    if len(partial) >= 32 and tokens_mod.get_token(partial):
        return partial
    matches = [t["token_id"] for t in tokens_mod.list_tokens(include_revoked=True) if t["token_id"].startswith(partial)]
    if len(matches) == 1:
        return matches[0]
    return None


__all__ = ["webhook_app"]
