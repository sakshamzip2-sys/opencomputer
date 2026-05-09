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
    skill: Annotated[list[str] | None, typer.Option("--skill", help="Skill to invoke at run-time. Repeat for multiple skills (Hermes parity).")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt", "-p", help="Free-text prompt (threat-scanned).")] = None,
    repeat: Annotated[int | None, typer.Option("--repeat", help="Run N times then auto-remove. Omit for infinite.")] = None,
    notify: Annotated[str | None, typer.Option("--notify", help="Where to deliver: 'telegram', 'discord', 'origin', 'slack:#chan', etc.")] = None,
    auto: Annotated[bool, typer.Option("--auto", help="Disable plan_mode (USE WITH CAUTION — destructive tools run unguarded).")] = False,
    yolo: Annotated[bool, typer.Option("--yolo", help="[deprecated] Alias for --auto.")] = False,
    no_agent: Annotated[bool, typer.Option("--no-agent", help="Run a script instead of invoking the agent (Hermes parity).")] = False,
    script: Annotated[str | None, typer.Option("--script", help="Script name (relative to ~/.opencomputer/<profile>/scripts/). Required with --no-agent.")] = None,
    script_timeout: Annotated[int | None, typer.Option("--script-timeout", help="Per-job override of cron.script_timeout_seconds (default 120).")] = None,
) -> None:
    """Create a new scheduled job.

    At least one of --skill or --prompt is required (or --no-agent --script).
    --skill is preferred because skills are vetted code; --prompt requires a
    stricter threat scan.

    Hermes parity: --no-agent --script <name> runs a shell script under
    ~/.opencomputer/<profile>/scripts/ with no LLM invocation. Empty stdout
    = silent tick (watchdog pattern); non-zero exit = error notification.

    Repeatable --skill creates a multi-skill cron job (the agent invokes
    each skill in turn and combines the results into one report).
    """
    skills = list(skill) if skill else None

    if no_agent:
        if not script:
            typer.secho("Error: --no-agent requires --script <name>", fg="red", err=True)
            raise typer.Exit(2)
        if skills or prompt:
            typer.secho("Error: --no-agent is exclusive with --skill/--prompt", fg="red", err=True)
            raise typer.Exit(2)
    elif not skills and not prompt:
        typer.secho("Error: must supply --skill or --prompt (or --no-agent --script)", fg="red", err=True)
        raise typer.Exit(2)

    if yolo:
        from opencomputer.cli import _emit_yolo_deprecation
        _emit_yolo_deprecation()
        auto = True

    # Pass skills as singular when length 1, plural when >1 — matches
    # create_job's normalization.
    create_kwargs: dict = {}
    if skills and len(skills) == 1:
        create_kwargs["skill"] = skills[0]
    elif skills:
        create_kwargs["skills"] = skills

    try:
        job = create_job(
            schedule=schedule,
            name=name,
            prompt=prompt,
            repeat=repeat,
            notify=notify,
            plan_mode=not auto,
            no_agent=no_agent,
            script=script,
            script_timeout_seconds=script_timeout,
            **create_kwargs,
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
    if job.get("skills"):
        typer.echo(f"  skills:      {job['skills']}")
    elif job.get("skill"):
        typer.echo(f"  skill:       {job['skill']}")
    if job.get("no_agent"):
        typer.echo(f"  script:      {job.get('script')}")
        typer.echo("  no_agent:    True")


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


def _render_schedule(sched) -> str:  # noqa: ANN001
    """Render a possibly-dict schedule into a short human display string."""
    if isinstance(sched, dict):
        return str(sched.get("display") or sched.get("kind", ""))[:20]
    return str(sched or "")[:20]


@cron_app.command("prune")
def cron_prune(
    noise: Annotated[bool, typer.Option("--noise", help="Flag short-named (<4 chars) and exact-duplicate jobs.")] = False,
    apply_changes: Annotated[bool, typer.Option("--apply", help="Actually delete flagged jobs (default is dry-run).")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip interactive confirmation prompt.")] = False,
) -> None:
    """Identify and (optionally) remove cron-job noise.

    Heuristic for "noise":
    - Job name length < 4 characters (test/garbage names like "a", "x", "T", "b").
    - Exact duplicate of a previously-seen (name, schedule, prompt) tuple.

    Default is DRY RUN — flagged jobs are listed only.
    Pair ``--apply`` with ``--yes`` to delete non-interactively.
    """
    if not noise:
        typer.echo("No filter selected. Use --noise to flag short-named + duplicate jobs.")
        return

    from opencomputer.cron.jobs import load_jobs

    jobs = load_jobs()
    flagged: list[dict] = []
    seen: dict[tuple[str, str, str], dict] = {}
    for j in jobs:
        name = (j.get("name") or "").strip()
        sched = str(j.get("schedule", ""))
        # Prompt may be a dict (skill-based jobs) — stringify for dedup key.
        prompt = json.dumps(j.get("prompt"), sort_keys=True, default=str)
        key = (name, sched, prompt)
        if len(name) < 4:
            flagged.append(j)
            continue
        if key in seen:
            flagged.append(j)
            continue
        seen[key] = j

    if not flagged:
        typer.echo("No noise jobs found.")
        return

    typer.echo(f"{len(flagged)} noise job(s) flagged:")
    for j in flagged:
        # Prefer the rendered schedule_display when present (cron_list pattern);
        # raw `schedule` may be a dict for interval/cron-kind jobs.
        sched_display = j.get("schedule_display") or _render_schedule(j.get("schedule"))
        typer.echo(
            f"  {j.get('id', '?')[:8]:<10} {(j.get('name') or '')[:20]:<20} {sched_display:<20}"
        )

    if not apply_changes:
        typer.echo("\n(dry run — pass --apply to delete)")
        return

    if not yes:
        if not typer.confirm("Delete these jobs?", default=False):
            typer.echo("Cancelled.")
            return

    deleted = 0
    for j in flagged:
        if remove_job(j["id"]):
            deleted += 1
    remaining = len(load_jobs())
    typer.echo(f"deleted {deleted} noise job(s); {remaining} remain.")


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


# ---------------------------------------------------------------------------
# edit (Hermes parity, 2026-05-08)
# ---------------------------------------------------------------------------


@cron_app.command("edit")
def cron_edit(
    job_id: Annotated[str, typer.Argument(help="Job id.")],
    schedule: Annotated[str | None, typer.Option("--schedule", "-s", help="New schedule expression.")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt", "-p", help="New prompt (re-scanned for threats).")] = None,
    skill: Annotated[list[str] | None, typer.Option("--skill", help="REPLACE the skill list with these. Repeat for multiple.")] = None,
    add_skill: Annotated[list[str] | None, typer.Option("--add-skill", help="Append a skill to the list.")] = None,
    remove_skill: Annotated[list[str] | None, typer.Option("--remove-skill", help="Remove a skill from the list.")] = None,
    clear_skills: Annotated[bool, typer.Option("--clear-skills", help="Remove all skills.")] = False,
    notify: Annotated[str | None, typer.Option("--notify", help="New delivery target. Pass empty string to clear.")] = None,
    workdir: Annotated[str | None, typer.Option("--workdir", help="New working directory. Empty string clears.")] = None,
    repeat: Annotated[int | None, typer.Option("--repeat", help="New repeat count (>0 = N runs, ≤0 = infinite).")] = None,
) -> None:
    """Edit an existing cron job (Hermes parity).

    Skill mutation order: --clear-skills → --skill (replace) → --add-skill → --remove-skill.
    When the resulting list is empty the singular skill field is also cleared.
    """
    from opencomputer.cron.jobs import update_job
    from opencomputer.cron.threats import assert_cron_prompt_safe

    job = get_job(job_id)
    if not job:
        typer.secho(f"job_id={job_id!r} not found", fg="red", err=True)
        raise typer.Exit(2)

    updates: dict[str, object] = {}
    if schedule is not None:
        updates["schedule"] = schedule  # update_job re-parses the string
    if prompt is not None:
        try:
            assert_cron_prompt_safe(prompt)
        except CronThreatBlocked as exc:
            typer.secho(f"Blocked by threat scan: {exc}", fg="red", err=True)
            raise typer.Exit(2) from exc
        updates["prompt"] = prompt
    if notify is not None:
        updates["notify"] = notify or None
    if workdir is not None:
        updates["workdir"] = workdir or None
    if repeat is not None:
        rep = job.get("repeat") or {"times": None, "completed": 0}
        rep["times"] = repeat if repeat > 0 else None
        updates["repeat"] = rep

    # Skill mutation. Order: clear → set → add → remove.
    new_skills = list(
        job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    )
    skill_touched = False
    if clear_skills:
        new_skills = []
        skill_touched = True
    if skill:
        new_skills = list(skill)
        skill_touched = True
    if add_skill:
        for s in add_skill:
            if s not in new_skills:
                new_skills.append(s)
        skill_touched = True
    if remove_skill:
        new_skills = [s for s in new_skills if s not in set(remove_skill)]
        skill_touched = True

    if skill_touched:
        # Always store as plural list when ≥1 entry; clear singular so the
        # back-compat shim in _build_run_prompt doesn't double-emit.
        if not new_skills:
            updates["skills"] = None
            updates["skill"] = None
        else:
            updates["skills"] = new_skills
            updates["skill"] = None
        # Production-grade (2026-05-09): when skills become active, clear
        # any stale prompt so the job's behavior is unambiguous (skills
        # take precedence in _build_run_prompt; leaving the old prompt
        # around is confusing for operators reading jobs.json).
        if new_skills and "prompt" not in updates:
            updates["prompt"] = None

    # Production-grade (2026-05-09): switching to prompt-only ⇒ clear
    # stale skills so _build_run_prompt actually emits the new prompt.
    # Otherwise the prompt edit silently no-ops because skills win.
    if prompt is not None and not skill_touched:
        if job.get("skills") or job.get("skill"):
            updates["skills"] = None
            updates["skill"] = None

    if not updates:
        typer.secho(
            "Nothing to update. Pass at least one of --schedule/--prompt/--skill/etc.",
            fg="yellow",
            err=True,
        )
        raise typer.Exit(0)

    try:
        updated = update_job(job_id, updates)
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc

    if updated is None:
        typer.secho(f"job_id={job_id!r} not found (race?)", fg="red", err=True)
        raise typer.Exit(2)

    typer.secho(f"Updated cron job {updated['id']} '{updated['name']}'", fg="green")
    typer.echo(f"  schedule:    {updated['schedule_display']}")
    if updated.get("skills"):
        typer.echo(f"  skills:      {updated['skills']}")
    elif updated.get("skill"):
        typer.echo(f"  skill:       {updated['skill']}")
    if updated.get("notify"):
        typer.echo(f"  notify:      {updated['notify']}")


__all__ = ["cron_app"]
