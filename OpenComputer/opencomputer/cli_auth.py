"""T8 — `oc auth` CLI subcommand group.

Hermes-doc parity. Subcommands:

* ``list [provider]``      — show pool entries (masked, table format)
* ``add <provider>``       — append key (--key INLINE | --key-env ENV)
* ``remove <provider> <i>``— remove by 0-based index
* ``reset <provider>``     — write a force-reset marker so the running
                              pool clears all cooldowns on next refresh
* ``login <provider>``     — run an OAuth login flow for an OAuth provider
                              (currently ``graph`` — Microsoft Graph
                              device-code flow). Net-new in Milestone 3:
                              no ``oc auth login`` subcommand existed before.
* ``logout <provider>``    — delete a stored OAuth token.

Credential-pool state lives in ``<OPENCOMPUTER_HOME>/config.yaml`` under the
existing ``credential_pools:`` key (reset markers under
``credential_pool_reset_at:``). OAuth tokens are a separate concern — they
live in ``auth_tokens.json`` via :mod:`opencomputer.auth.token_store`.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config import _home

console = Console()
auth_app = typer.Typer(
    name="auth",
    help=(
        "Show provider credential status (no args) or manage credential "
        "pools (Hermes-doc parity)."
    ),
    invoke_without_command=True,
)


@auth_app.callback()
def _auth_callback(ctx: typer.Context) -> None:
    """Bare ``oc auth`` shows provider env-var status (Hermes parity)."""
    if ctx.invoked_subcommand is not None:
        return
    # Lazy import to avoid a cli ↔ cli_auth import cycle.
    from opencomputer.cli import run_auth_status

    run_auth_status()


@auth_app.command("status")
def status_cmd() -> None:
    """Explicit alias for the no-args invocation — show provider env-var status."""
    from opencomputer.cli import run_auth_status

    run_auth_status()


def _config_path() -> Path:
    return _home() / "config.yaml"


def _load_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        console.print(f"[red]Could not parse {path}.[/red]")
        raise typer.Exit(code=1) from None


def _save_config(data: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _safe_id(key: str, idx: int) -> str:
    """Stable masked id (matches credential_pool._safe_id digest length)."""
    if not key:
        return f"[{idx}]:empty"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"[{idx}]:{digest}"


def _is_env_indirection(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("${") and value.endswith("}")


@auth_app.command("list")
def list_keys(
    provider: str | None = typer.Argument(None, help="Filter by provider"),
) -> None:
    """List credential pool entries (masked)."""
    cfg = _load_config()
    pools = (cfg.get("credential_pools") or {}) if isinstance(cfg, dict) else {}
    if provider is not None:
        pools = {provider: pools.get(provider, [])} if provider in pools else {}
    if not pools or not any(pools.values()):
        console.print("[dim]No credential pools configured (empty).[/dim]")
        return
    table = Table(title="Credential pools")
    table.add_column("Provider")
    table.add_column("Index")
    table.add_column("Masked key")
    for prov_name, keys in pools.items():
        for idx, k in enumerate(keys or []):
            display = k if _is_env_indirection(k) else _safe_id(str(k), idx)
            table.add_row(prov_name, str(idx), str(display))
    console.print(table)


@auth_app.command("add")
def add(
    provider: str = typer.Argument(..., help="Provider name (e.g. openrouter)"),
    key: str | None = typer.Option(None, "--key", help="Inline API key"),
    key_env: str | None = typer.Option(
        None, "--key-env", help="Env var name holding the key"
    ),
) -> None:
    """Append a credential to ``credential_pools[provider]``."""
    if not key and not key_env:
        console.print("[red]Either --key or --key-env is required.[/red]")
        raise typer.Exit(code=2)
    if key and key_env:
        console.print("[red]Pass --key OR --key-env, not both.[/red]")
        raise typer.Exit(code=2)
    stored = key if key else f"${{{key_env}}}"
    cfg = _load_config()
    cfg.setdefault("credential_pools", {})
    cfg["credential_pools"].setdefault(provider, [])
    cfg["credential_pools"][provider].append(stored)
    _save_config(cfg)
    new_idx = len(cfg["credential_pools"][provider]) - 1
    console.print(
        f"[green]Added[/green] credential to {provider} pool (index {new_idx})."
    )


@auth_app.command("remove")
def remove(
    provider: str = typer.Argument(..., help="Provider name"),
    index: int = typer.Argument(..., help="Pool index to remove (0-based)"),
) -> None:
    """Remove a credential by 0-based index."""
    cfg = _load_config()
    pools = cfg.get("credential_pools") or {}
    if provider not in pools:
        console.print(f"[red]Unknown provider '{provider}'.[/red]")
        raise typer.Exit(code=2)
    keys = pools[provider]
    if not 0 <= index < len(keys):
        console.print(
            f"[red]Index {index} out of range for {provider} (size {len(keys)}).[/red]"
        )
        raise typer.Exit(code=2)
    removed = keys.pop(index)
    cfg["credential_pools"][provider] = keys
    _save_config(cfg)
    masked = removed if _is_env_indirection(removed) else _safe_id(str(removed), index)
    console.print(f"[yellow]Removed[/yellow] {masked} from {provider}.")


@auth_app.command("reset")
def reset(
    provider: str = typer.Argument(..., help="Provider name"),
) -> None:
    """Clear all cooldowns for ``provider``.

    Writes a ``credential_pool_reset_at[provider] = <timestamp>``
    marker that the running pool reads on next refresh.
    """
    cfg = _load_config()
    cfg.setdefault("credential_pool_reset_at", {})
    cfg["credential_pool_reset_at"][provider] = time.time()
    _save_config(cfg)
    console.print(
        f"[green]Reset[/green] cooldowns for {provider}. "
        "Running processes pick up on next refresh."
    )


# =============================================================================
# OAuth login / logout
# =============================================================================
#
# These are net-new in Milestone 3: before M3 there was no ``oc auth login``
# subcommand anywhere (``oc auth`` was a credential-pool manager only, and
# ``oc login`` is a separate API-key-into-.env tool). The first — and so far
# only — OAuth provider wired here is ``graph`` (Microsoft Graph), which uses
# the device-code flow from :mod:`opencomputer.auth.graph_oauth`.

#: OAuth providers ``oc auth login`` / ``oc auth logout`` understand.
_OAUTH_PROVIDERS = ("graph",)


def _login_graph() -> None:
    """Run the Microsoft Graph device-code login and persist the token."""
    from opencomputer.auth.graph_oauth import (
        GraphOAuthError,
        begin_device_login,
        complete_device_login,
        stored_account_summary,
    )

    try:
        prompt = begin_device_login()
    except GraphOAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    # Microsoft does not pre-fill the code into the URL — show the code on
    # its own line so the user knows to type it.
    console.print("\n[bold]Sign in to Microsoft Graph[/bold]")
    console.print(
        f"  1. Open [cyan]{prompt.verification_uri}[/cyan] in a browser."
    )
    console.print(f"  2. Enter the code: [bold yellow]{prompt.user_code}[/bold yellow]")
    console.print(
        f"  3. Approve the requested permissions "
        f"(code expires in ~{prompt.expires_in // 60} min).\n"
    )
    console.print("[dim]Waiting for you to finish signing in…[/dim]")

    try:
        complete_device_login(prompt)
    except GraphOAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    summary = stored_account_summary()
    detail = f" ({summary})" if summary else ""
    console.print(f"[green]Signed in to Microsoft Graph.[/green]{detail}")


@auth_app.command("login")
def login(
    provider: str = typer.Argument(
        ..., help="OAuth provider to sign in to (e.g. graph)"
    ),
) -> None:
    """Run an OAuth login flow for an OAuth-based provider.

    ``oc auth login graph`` runs the Microsoft Graph device-code flow: it
    prints a verification URL + one-time code, polls until you finish signing
    in, then stores the resulting token in ``auth_tokens.json``.
    """
    key = provider.strip().lower()
    if key == "graph":
        _login_graph()
        return
    console.print(
        f"[red]Unknown OAuth provider '{provider}'.[/red] "
        f"Supported: {', '.join(_OAUTH_PROVIDERS)}."
    )
    raise typer.Exit(code=2)


@auth_app.command("logout")
def logout(
    provider: str = typer.Argument(
        ..., help="OAuth provider to sign out of (e.g. graph)"
    ),
) -> None:
    """Delete a stored OAuth token (sign out of an OAuth provider)."""
    key = provider.strip().lower()
    if key == "graph":
        from opencomputer.auth.graph_oauth import logout as graph_logout

        if graph_logout():
            console.print("[green]Signed out of Microsoft Graph.[/green]")
        else:
            console.print("[dim]Not signed in to Microsoft Graph — nothing to do.[/dim]")
        return
    console.print(
        f"[red]Unknown OAuth provider '{provider}'.[/red] "
        f"Supported: {', '.join(_OAUTH_PROVIDERS)}."
    )
    raise typer.Exit(code=2)
