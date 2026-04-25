"""``opencomputer cron`` CLI subcommand group.

Subcommands:

    opencomputer cron list [--all]                  — list jobs
    opencomputer cron create <args>                 — schedule a new job
    opencomputer cron get <job_id>                  — show one job in detail
    opencomputer cron pause <job_id> [--reason X]   — pause without deleting
    opencomputer cron resume <job_id>               — resume a paused job
    opencomputer cron run <job_id>                  — trigger immediately
    opencomputer cron remove <job_id>               — delete a job
    opencomputer cron tick                          — single-shot tick now
    opencomputer cron daemon [--interval 60]        — run scheduler in foreground
    opencomputer cron status                        — daemon health summary

Storage is profile-isolated under ``<profile_home>/cron/``. The daemon takes
a file lock so multiple processes (gateway + standalone) coordinate without
double-execution.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.cron import (
    DEFAULT_TICK_INTERVAL_S,
    create_job,
    cron_dir,
    get_job,
    list_jobs,
    pause_job,
    remove_job,
    resume_job,
    run_scheduler_loop,
    tick,
    trigger_job,
)
from opencomputer.cron.threats import CronThreatBlocked

cron_app = typer.Typer(
    name="cron",
    help="Schedule and manage agent cron jobs.",
    no_args_is_help=True,
)

_console = Console()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@cron_app.command("list")
def cron_list(
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Include disabled / completed jobs.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")] = False,
) -> None:
    """List scheduled jobs."""
    jobs = list_jobs(include_disabled=show_all)

    if json_output:
        typer.echo(json.dumps(jobs, default=str, indent=2))
        return

    if not jobs:
        _console.print("[dim]No scheduled jobs.[/dim]")
        _console.print("[dim]Create one with `opencomputer cron create ...`[/dim]")
        return

    table = Table(title=f"Cron Jobs ({len(jobs)})")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Next Run", style="yellow")
    table.add_column("Last Status")
    table.add_column("State")
    table.add_column("Skill / Prompt")
    table.add_column("Notify")
    for j in jobs:
        target = j.get("skill") or (j.get("prompt") or "")[:40]
        table.add_row(
            j["id"][:8],
            j.get("name", "")[:30],
            j.get("schedule_display", "")[:20],
            (j.get("next_run_at") or "—")[:19],
            j.get("last_status") or "—",
            j.get("state") or "—",
            target,
            j.get("notify") or "local",
        )
    _console.print(table)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@cron_app.command("create")
def cron_create(
    schedule: Annotated[str, typer.Option("--schedule", "-s", help="Schedule expression (e.g. '0 9 * * *', 'every 30m', '2h').")],
    name: Annotated[str | None, typer.Option("--name", "-n", help="Friendly name.")] = None,
    skill: Annotated[str | None, typer.Option("--skill", help="Skill to invoke at run-time. Preferred over --prompt.")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt", "-p", help="Free-text prompt (threat-scanned).")] = None,
    repeat: Annotated[int | None, typer.Option("--repeat", help="Run N times then auto-remove. Omit for infinite.")] = None,
    notify: Annotated[str | None, typer.Option("--notify", help="Where to deliver: 'telegram', 'discord', 'telegram:<chat_id>', or omit.")] = None,
    yolo: Annotated[bool, typer.Option("--yolo", help="Disable plan_mode (USE WITH CAUTION — destructive tools run unguarded).")] = False,
) -> None:
    """Create a new scheduled job.

    At least one of --skill or --prompt is required. --skill is preferred
    because skills are vetted code; --prompt requires a stricter threat scan.
    """
    if not skill and not prompt:
        typer.secho("Error: must supply --skill or --prompt", fg="red", err=True)
        raise typer.Exit(2)

    try:
        job = create_job(
            schedule=schedule,
            name=name,
            skill=skill,
            prompt=prompt,
            repeat=repeat,
            notify=notify,
            plan_mode=not yolo,
        )
    except CronThreatBlocked as exc:
        typer.secho(f"Blocked by threat scan: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc

    typer.secho(f"Created cron job {job['id']} '{job['name']}'", fg="green")
    typer.echo(f"  schedule:    {job['schedule_display']}")
    typer.echo(f"  next_run_at: {job.get('next_run_at') or 'n/a'}")
    typer.echo(f"  notify:      {job.get('notify') or 'local'}")
    typer.echo(f"  plan_mode:   {job.get('plan_mode')}")


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@cron_app.command("get")
def cron_get(job_id: Annotated[str, typer.Argument(help="Job id.")]) -> None:
    """Show full details for one job."""
    job = get_job(job_id)
    if not job:
        typer.secho(f"job_id={job_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(job, default=str, indent=2))


# ---------------------------------------------------------------------------
# pause / resume / run / remove
# ---------------------------------------------------------------------------


@cron_app.command("pause")
def cron_pause(
    job_id: Annotated[str, typer.Argument(help="Job id.")],
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Optional reason for the pause.")] = None,
) -> None:
    """Pause a job without deleting it."""
    job = pause_job(job_id, reason=reason)
    if not job:
        typer.secho(f"job_id={job_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)
    typer.secho(f"Paused {job['id']} '{job['name']}'", fg="yellow")


@cron_app.command("resume")
def cron_resume(job_id: Annotated[str, typer.Argument(help="Job id.")]) -> None:
    """Resume a paused job. Computes the next future run from now."""
    job = resume_job(job_id)
    if not job:
        typer.secho(f"job_id={job_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)
    typer.secho(f"Resumed {job['id']} '{job['name']}' — next: {job['next_run_at']}", fg="green")


@cron_app.command("run")
def cron_run(job_id: Annotated[str, typer.Argument(help="Job id.")]) -> None:
    """Trigger a job to run on the next tick (regardless of its schedule)."""
    job = trigger_job(job_id)
    if not job:
        typer.secho(f"job_id={job_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)
    typer.secho(f"Triggered {job['id']} '{job['name']}'", fg="green")
    typer.echo("Run will fire on next scheduler tick. Run `opencomputer cron tick` to fire now.")


@cron_app.command("remove")
def cron_remove(
    job_id: Annotated[str, typer.Argument(help="Job id.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt.")] = False,
) -> None:
    """Remove a job permanently."""
    job = get_job(job_id)
    if not job:
        typer.secho(f"job_id={job_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)
    if not yes:
        typer.echo(f"About to remove cron job: {job['name']} ({job_id})")
        if not typer.confirm("Continue?"):
            typer.echo("Cancelled.")
            raise typer.Exit(0)
    if not remove_job(job_id):
        typer.secho(f"Failed to remove {job_id}", fg="red", err=True)
        raise typer.Exit(2)
    typer.secho(f"Removed cron job {job_id}", fg="green")


# ---------------------------------------------------------------------------
# tick / daemon / status
# ---------------------------------------------------------------------------


@cron_app.command("tick")
def cron_tick() -> None:
    """Run a single tick — execute all due jobs and exit.

    Useful for testing. The full scheduler runs via ``cron daemon`` or
    inside the gateway daemon.
    """
    n = asyncio.run(tick(verbose=True))
    typer.echo(f"Cron tick: ran {n} job(s).")


@cron_app.command("daemon")
def cron_daemon(
    interval: Annotated[int, typer.Option("--interval", "-i", help="Tick interval in seconds.")] = DEFAULT_TICK_INTERVAL_S,
) -> None:
    """Run the cron scheduler as a foreground daemon.

    The daemon ticks every --interval seconds. Process is cancellable
    with Ctrl-C. To run as a system service, wrap this command with
    launchd / systemd / nohup.
    """
    typer.secho(f"Cron daemon started (interval={interval}s). Ctrl-C to stop.", fg="cyan")
    try:
        asyncio.run(run_scheduler_loop(interval_s=interval))
    except KeyboardInterrupt:
        typer.echo("\nCron daemon stopped.")


@cron_app.command("status")
def cron_status() -> None:
    """Show cron storage location + recent activity summary."""
    cdir = cron_dir()
    jobs = list_jobs(include_disabled=True)
    enabled = [j for j in jobs if j.get("enabled", True)]
    paused = [j for j in jobs if j.get("state") == "paused"]
    completed = [j for j in jobs if j.get("state") == "completed"]
    last_runs = sorted(
        (j for j in jobs if j.get("last_run_at")),
        key=lambda j: j["last_run_at"],
        reverse=True,
    )[:5]

    typer.secho("Cron status", fg="cyan", bold=True)
    typer.echo(f"  Storage: {cdir}")
    typer.echo(f"  Total jobs: {len(jobs)}  (enabled={len(enabled)}, paused={len(paused)}, completed={len(completed)})")

    if last_runs:
        typer.echo("\n  Last 5 runs:")
        for j in last_runs:
            status_icon = "✓" if j.get("last_status") == "ok" else "✗"
            typer.echo(
                f"    {status_icon} {j['id'][:8]} {j['name']:<30} {j['last_run_at']}  "
                f"({j.get('last_status') or 'n/a'})"
            )
    else:
        typer.echo("\n  No runs yet.")


__all__ = ["cron_app"]
