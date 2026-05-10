"""``opencomputer heartbeat`` CLI subgroup — A2 from 2026-05-06 brief.

A heartbeat is an always-on agent tick — distinct from cron's
calendared jobs in spirit but implemented on top of cron's storage +
scheduler so the daemon doesn't need a second engine.

    oc heartbeat enable [--interval 30m] [--notify telegram]
    oc heartbeat disable
    oc heartbeat status
    oc heartbeat pause
    oc heartbeat resume
"""

from __future__ import annotations

import typer
from rich.console import Console

from opencomputer.heartbeat import (
    DEFAULT_HEARTBEAT_INTERVAL_MIN,
    disable_heartbeat,
    enable_heartbeat,
    heartbeat_status,
    is_heartbeat_enabled,
    pause_heartbeat,
    resume_heartbeat,
)

heartbeat_app = typer.Typer(
    name="heartbeat",
    help="Always-on agent tick (separate lane from cron's calendared jobs).",
    no_args_is_help=True,
)
_console = Console()


@heartbeat_app.command("enable")
def enable(
    interval: int = typer.Option(
        DEFAULT_HEARTBEAT_INTERVAL_MIN,
        "--interval",
        help="Tick interval in minutes (default: 30).",
    ),
    notify: str | None = typer.Option(
        None, "--notify", help="Channel to deliver tick output (e.g. telegram)."
    ),
    yolo: bool = typer.Option(
        False,
        "--yolo",
        help="Run heartbeat without plan mode (allows destructive tools).",
    ),
) -> None:
    """Enable the heartbeat job."""
    if is_heartbeat_enabled():
        _console.print(
            "[yellow]heartbeat already enabled.[/yellow] "
            "Use 'oc heartbeat status' for details."
        )
        raise typer.Exit(code=0)

    job = enable_heartbeat(
        interval_minutes=interval,
        notify=notify,
        plan_mode=not yolo,
    )
    _console.print(
        f"[green]heartbeat enabled[/green]: every {interval}m "
        f"(job_id={job.get('id')[:8]}…)"
    )


@heartbeat_app.command("disable")
def disable() -> None:
    """Disable + remove the heartbeat job."""
    n = disable_heartbeat()
    if n == 0:
        _console.print("[dim]heartbeat was not enabled.[/dim]")
        return
    _console.print(f"[green]heartbeat disabled[/green] (removed {n} job(s))")


@heartbeat_app.command("status")
def status() -> None:
    """Show heartbeat state."""
    s = heartbeat_status()
    if not s.get("enabled") and not s.get("paused"):
        _console.print("[dim]heartbeat: not enabled[/dim]")
        return
    state = "PAUSED" if s.get("paused") else "ENABLED"
    _console.print(f"heartbeat: [bold]{state}[/bold]")
    _console.print(f"  interval: {s.get('schedule_display')}")
    _console.print(f"  job_id:   {s.get('job_id', '?')[:12]}")
    _console.print(f"  last run: {s.get('last_run_at') or 'never'}")
    _console.print(f"  next run: {s.get('next_run_at') or 'unscheduled'}")
    _console.print(f"  ticks:    {s.get('run_count', 0)}")


@heartbeat_app.command("pause")
def pause() -> None:
    """Pause without deleting the heartbeat job."""
    if pause_heartbeat():
        _console.print("[green]heartbeat paused[/green]")
    else:
        _console.print("[dim]heartbeat: not enabled[/dim]")
        raise typer.Exit(code=1)


@heartbeat_app.command("resume")
def resume() -> None:
    """Resume a paused heartbeat job."""
    if resume_heartbeat():
        _console.print("[green]heartbeat resumed[/green]")
    else:
        _console.print("[dim]heartbeat: not enabled[/dim]")
        raise typer.Exit(code=1)


__all__ = ["heartbeat_app"]
