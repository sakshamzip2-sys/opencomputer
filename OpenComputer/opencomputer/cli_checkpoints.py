"""``oc checkpoints`` Typer subapp — status / prune / clear.

Backs the production-grade RewindStore hygiene UX. Reads defaults from
the live ``Config.checkpoints`` section; explicit flags override.
"""
from __future__ import annotations

import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.checkpoint_admin import (
    PrunePolicy,
    aggregate_status,
    clear_all,
    prune_all,
)

checkpoints_app = typer.Typer(
    name="checkpoints",
    help="Manage RewindStore checkpoints (the /rollback + auto_checkpoint backing store).",
    no_args_is_help=True,
)
console = Console()


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / (1024 ** 2):.1f} MB"
    return f"{n / (1024 ** 3):.2f} GB"


@checkpoints_app.command("status")
def status_cmd() -> None:
    """Print per-session and aggregate checkpoint store stats."""
    rep = aggregate_status()
    if not rep.stores:
        console.print("[dim]no checkpoint stores yet — nothing to report.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", title="checkpoint stores")
    table.add_column("session_id", style="cyan", overflow="fold")
    table.add_column("count", justify="right")
    table.add_column("size", justify="right")
    table.add_column("oldest")
    table.add_column("newest")
    table.add_column("subagents", justify="right")
    table.add_column("last_prune")

    for s in rep.stores:
        table.add_row(
            s.session_id,
            str(s.count),
            _format_size(s.size_bytes),
            (s.oldest_iso or "—")[:19],
            (s.newest_iso or "—")[:19],
            str(s.subagent_count),
            (s.last_prune_iso or "—")[:19],
        )
    console.print(table)
    console.print(
        f"\n[bold]total:[/bold] {rep.total_count} checkpoints across "
        f"{len(rep.stores)} sessions = {_format_size(rep.total_size_bytes)}"
    )


@checkpoints_app.command("prune")
def prune_cmd(
    older_than: Annotated[
        int | None,
        typer.Option("--older-than", help="Drop checkpoints older than N days."),
    ] = None,
    max_size: Annotated[
        int | None,
        typer.Option(
            "--max-size",
            help="Cap aggregate size to N MB (oldest-first eviction).",
        ),
    ] = None,
    max_count: Annotated[
        int | None,
        typer.Option("--max-count", help="Per-session cap; oldest above are dropped."),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Only operate on the given session_id."),
    ] = None,
    no_orphans: Annotated[
        bool,
        typer.Option(
            "--no-delete-orphans",
            help="Keep dirs with missing/corrupt meta.json.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print policy effect; do not delete."),
    ] = False,
) -> None:
    """Apply prune policy to one or all session stores.

    Explicit flags override the corresponding fields in
    ``Config.checkpoints``. With no flags, the configured defaults
    apply.
    """
    try:
        from opencomputer.agent.config import default_config

        cfg = default_config().checkpoints
    except Exception:  # noqa: BLE001
        cfg = None

    policy = PrunePolicy(
        older_than_days=older_than
        if older_than is not None
        else (cfg.retention_days if cfg else None),
        max_total_bytes=(max_size * 1024 * 1024)
        if max_size is not None
        else (cfg.max_total_size_mb * 1024 * 1024 if cfg else None),
        max_count=max_count if max_count is not None else (cfg.max_snapshots if cfg else None),
        delete_orphans=not no_orphans,
        dry_run=dry_run,
    )

    out = prune_all(policy=policy, session_filter=session)
    if not out:
        console.print("[dim]no stores matched.[/dim]")
        return

    table = Table(
        show_header=True,
        header_style="bold cyan",
        title=("dry-run " if dry_run else "") + "prune report",
    )
    table.add_column("session_id", style="cyan", overflow="fold")
    table.add_column("dropped", justify="right")
    table.add_column("orphans", justify="right")
    table.add_column("freed", justify="right")
    table.add_column("kept", justify="right")
    for sid, rep in out.items():
        verb = "would-drop" if dry_run else "dropped"
        table.add_row(
            sid,
            f"{verb} {len(rep.dropped)}",
            str(len(rep.orphans_removed)),
            _format_size(rep.bytes_freed),
            str(rep.kept),
        )
    console.print(table)


@checkpoints_app.command("clear")
def clear_cmd(
    session: Annotated[
        str | None,
        typer.Option("--session", help="Only wipe the named session."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Skip the safety confirmation."),
    ] = False,
) -> None:
    """Wipe checkpoint stores. Refuses without --yes when stdin is non-interactive."""
    if not yes:
        if not sys.stdin.isatty():
            console.print(
                "[bold red]error:[/bold red] refusing to clear without --yes "
                "in a non-interactive environment."
            )
            raise typer.Exit(2)
        confirm = typer.confirm("really wipe all checkpoint stores?")
        if not confirm:
            raise typer.Exit(0)

    n = clear_all(session_filter=session)
    console.print(f"[green]cleared {n} checkpoints.[/green]")


__all__ = ["checkpoints_app"]
