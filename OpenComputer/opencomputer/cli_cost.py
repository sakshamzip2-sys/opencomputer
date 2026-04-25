"""``opencomputer cost`` CLI — view + tune per-provider budget caps.

Subcommands:

    opencomputer cost show [--provider X]              — current usage table
    opencomputer cost set-limit --provider X [opts]    — set daily / monthly cap
    opencomputer cost reset [--provider X] [--yes]     — clear recorded usage

Storage is profile-isolated at ``<profile_home>/cost_guard.json`` (mode 0600).
Limits persist across sessions; usage entries past 90 days are auto-pruned.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.cost_guard import get_default_guard

cost_app = typer.Typer(
    name="cost",
    help="Track and cap per-provider API spend.",
    no_args_is_help=True,
)
_console = Console()


@cost_app.command("show")
def cost_show(
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Filter to one provider.")
    ] = None,
) -> None:
    """Print current usage + limits for one or all providers."""
    guard = get_default_guard()
    rows = guard.current_usage(provider)
    if not rows:
        _console.print("[dim]No usage recorded and no limits set.[/dim]")
        _console.print(
            "[dim]Set a cap with `opencomputer cost set-limit --provider X --daily Y`.[/dim]"
        )
        return

    table = Table(title=f"Cost Usage ({len(rows)} provider{'s' if len(rows) != 1 else ''})")
    table.add_column("Provider", style="cyan")
    table.add_column("Daily Used", justify="right")
    table.add_column("Daily Limit", justify="right")
    table.add_column("Monthly Used", justify="right")
    table.add_column("Monthly Limit", justify="right")
    table.add_column("Operations Today")
    for r in rows:
        ops = (
            ", ".join(f"{op}=${cost:.4f}" for op, cost in r.operations_today.items())
            if r.operations_today
            else "—"
        )
        table.add_row(
            r.provider,
            f"${r.daily_used:.4f}",
            f"${r.daily_limit:.4f}" if r.daily_limit is not None else "—",
            f"${r.monthly_used:.4f}",
            f"${r.monthly_limit:.4f}" if r.monthly_limit is not None else "—",
            ops,
        )
    _console.print(table)


@cost_app.command("set-limit")
def cost_set_limit(
    provider: Annotated[str, typer.Option("--provider", "-p", help="Provider id (e.g. openai, anthropic).")],
    daily: Annotated[
        float | None,
        typer.Option("--daily", "-d", help="Daily cap in USD. Omit to leave unchanged; 0 to clear."),
    ] = None,
    monthly: Annotated[
        float | None,
        typer.Option("--monthly", "-m", help="Monthly cap in USD. Omit to leave unchanged; 0 to clear."),
    ] = None,
) -> None:
    """Set daily and/or monthly USD caps for a provider.

    Pass ``0`` (or any value <= 0) to clear a limit. Omit a flag to leave
    that bound unchanged.
    """
    if daily is None and monthly is None:
        typer.secho(
            "Error: at least one of --daily / --monthly must be provided",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)

    guard = get_default_guard()
    # Treat <= 0 as "clear this limit" so users can drop a cap without a flag.
    daily_arg: float | None = None if daily is not None and daily <= 0 else daily
    monthly_arg: float | None = None if monthly is not None and monthly <= 0 else monthly

    # If user passed daily=0, daily_arg is None — but they want it cleared,
    # not "leave unchanged". Distinguish via the original args.
    if daily is not None and daily <= 0:
        # Reload-set-save path: pull current limits, drop daily, re-set.
        existing = guard.current_usage(provider)
        existing_monthly = existing[0].monthly_limit if existing else None
        # Bypass: set_limit treats None as "clear" for that field.
        guard.set_limit(provider, daily=None, monthly=existing_monthly if monthly is None else monthly_arg)
    elif monthly is not None and monthly <= 0:
        existing = guard.current_usage(provider)
        existing_daily = existing[0].daily_limit if existing else None
        guard.set_limit(provider, daily=existing_daily if daily is None else daily_arg, monthly=None)
    else:
        guard.set_limit(provider, daily=daily_arg, monthly=monthly_arg)

    after = guard.current_usage(provider)
    if not after:
        typer.secho(f"Cleared all limits for {provider!r}", fg="yellow")
        return
    r = after[0]
    typer.secho(f"Updated limits for {provider!r}", fg="green")
    typer.echo(f"  daily   = {r.daily_limit if r.daily_limit is not None else 'unlimited'}")
    typer.echo(f"  monthly = {r.monthly_limit if r.monthly_limit is not None else 'unlimited'}")


@cost_app.command("reset")
def cost_reset(
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Reset a single provider only.")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Clear recorded usage. Limits are NOT reset.

    Useful for testing or after a billing cycle that shouldn't roll forward.
    """
    target = f"provider {provider!r}" if provider else "ALL providers"
    if not yes and not typer.confirm(f"Clear recorded cost usage for {target}?"):
        typer.echo("Cancelled.")
        raise typer.Exit(0)
    guard = get_default_guard()
    guard.reset(provider)
    typer.secho(f"Reset usage for {target}", fg="green")


__all__ = ["cost_app"]
