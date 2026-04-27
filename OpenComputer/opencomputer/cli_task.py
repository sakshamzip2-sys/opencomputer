"""``opencomputer task`` — manage detached tasks (Tier B item 23).

Subcommands:

    opencomputer task list [--status STATUS] [--limit N]    — list tasks
    opencomputer task show <id>                              — show one task
    opencomputer task cancel <id>                            — cancel queued/running task
    opencomputer task run-once                               — drain queue once + exit

The ``run-once`` subcommand is for testing / cron — it ticks the runner
once and exits. Production use puts the runner inside the gateway
daemon, where it polls continuously.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config import default_config
from opencomputer.tasks import TaskNotFound, TaskRunner, TaskStore

task_app = typer.Typer(
    name="task",
    help="Manage detached agent tasks (long-running fire-and-forget jobs).",
    no_args_is_help=True,
)
_console = Console()


def _store() -> TaskStore:
    return TaskStore(default_config().home / "sessions.db")


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(start: float | None, end: float | None) -> str:
    if start is None:
        return "—"
    end_ts = end if end is not None else _dt.datetime.now().timestamp()
    delta = max(0.0, end_ts - start)
    if delta < 60:
        return f"{delta:.1f}s"
    if delta < 3600:
        return f"{delta / 60:.1f}m"
    return f"{delta / 3600:.1f}h"


@task_app.command("list")
def task_list(
    status: Annotated[
        str | None,
        typer.Option("--status", "-s", help="Filter to one status."),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", "-l", help="Max rows to show.")
    ] = 25,
) -> None:
    """List detached tasks (most-recent-first)."""
    rows = _store().list_(status=status, limit=limit)  # type: ignore[arg-type]
    if not rows:
        _console.print("[dim]No detached tasks recorded.[/dim]")
        _console.print(
            "[dim]Spawn one from a chat: ask the agent to use "
            "`SpawnDetachedTask` with a prompt.[/dim]"
        )
        return
    table = Table(title=f"Detached tasks ({len(rows)} shown)")
    table.add_column("ID", style="cyan")
    table.add_column("Status")
    table.add_column("Created")
    table.add_column("Duration")
    table.add_column("Prompt", overflow="fold")
    for t in rows:
        prompt_preview = (t.prompt[:60] + "…") if len(t.prompt) > 60 else t.prompt
        table.add_row(
            t.id,
            t.status,
            _fmt_ts(t.created_at),
            _fmt_duration(t.started_at, t.completed_at),
            prompt_preview,
        )
    _console.print(table)


@task_app.command("show")
def task_show(
    task_id: Annotated[str, typer.Argument(help="The task id (or prefix).")],
) -> None:
    """Show the full prompt + output of a detached task."""
    try:
        task = _store().get(task_id)
    except TaskNotFound:
        _console.print(f"[red]Task {task_id!r} not found.[/red]")
        raise typer.Exit(1) from None

    _console.print(f"[bold cyan]Task {task.id}[/bold cyan]  [bold]{task.status}[/bold]")
    _console.print(f"[dim]Created:[/dim] {_fmt_ts(task.created_at)}")
    _console.print(f"[dim]Started:[/dim] {_fmt_ts(task.started_at)}")
    _console.print(f"[dim]Completed:[/dim] {_fmt_ts(task.completed_at)}")
    _console.print(f"[dim]Notify policy:[/dim] {task.notify_policy}")
    _console.print(f"[dim]Delivery:[/dim] {task.delivery_status}")
    _console.print()
    _console.print("[bold]Prompt:[/bold]")
    _console.print(task.prompt)
    if task.progress:
        _console.print()
        _console.print("[bold]Progress:[/bold]")
        _console.print(task.progress)
    if task.output:
        _console.print()
        _console.print("[bold]Output:[/bold]")
        _console.print(task.output)
    if task.error:
        _console.print()
        _console.print("[bold red]Error:[/bold red]")
        _console.print(task.error)


@task_app.command("cancel")
def task_cancel(
    task_id: Annotated[str, typer.Argument(help="The task id to cancel.")],
) -> None:
    """Cancel a queued or running task. Already-terminal tasks are no-ops."""
    changed = _store().cancel(task_id)
    if changed:
        _console.print(f"[yellow]Cancelled task {task_id}.[/yellow]")
    else:
        _console.print(
            f"[dim]Task {task_id} was already terminal or didn't exist; no change.[/dim]"
        )


@task_app.command("run-once")
def task_run_once() -> None:
    """Drain the queue once + exit (testing / cron mode).

    Production use: the gateway daemon hosts a long-lived ``TaskRunner``
    that polls continuously. ``run-once`` is for one-shot test/CI use.
    """
    store = _store()
    runner = TaskRunner(store)

    async def _drain() -> None:
        await runner.recover_orphaned()
        queued = store.list_queued(limit=100)
        if not queued:
            _console.print("[dim]No queued tasks.[/dim]")
            return
        _console.print(f"Draining {len(queued)} queued task(s)…")
        for task in queued:
            store.mark_running(task.id)
            try:
                output = await runner._default_executor(task)  # noqa: SLF001 — internal
                store.complete(task.id, output)
                _console.print(f"  [green]✓[/green] {task.id} done")
            except Exception as e:  # noqa: BLE001 — surface to console
                store.fail(task.id, f"{type(e).__name__}: {e}")
                _console.print(f"  [red]✗[/red] {task.id} failed: {e}")

    asyncio.run(_drain())


__all__ = ["task_app"]
