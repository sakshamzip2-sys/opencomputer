"""``oc hooks`` — list / test / clear / revoke for debug observability.

The hook system has 17 lifecycle events declared in
plugin_sdk.hooks.HookEvent. Settings, plugins, and config.yaml can
register handlers. With no CLI surface, "why didn't my hook fire"
required reading source. This module adds the missing observability
layer.

Subcommands:
    oc hooks list [--json]
    oc hooks test EVENT [--payload JSON] [--execute]
    oc hooks clear
    oc hooks revoke PLUGIN_ID
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.hook_history import (
    all_events,
    clear_history,
    iter_history,
)

hooks_app = typer.Typer(help="Inspect and manage agent hooks.")
_console = Console()


def _profile_dir() -> Path:
    raw = os.environ.get("OC_PROFILE_DIR") or str(
        Path.home() / ".opencomputer" / "default"
    )
    return Path(raw).expanduser()


def _known_events() -> list[str]:
    """Return all declared HookEvent values, falling back to history keys."""
    try:
        from plugin_sdk.hooks import HookEvent

        return sorted(e.value for e in HookEvent)
    except Exception:  # noqa: BLE001
        return sorted(all_events())


def _last_fire(event: str) -> dict | None:
    records = list(iter_history(event))
    if not records:
        return None
    rec = records[-1]
    return {
        "ts_utc": rec.ts_utc,
        "source": rec.source_id,
        "ok": rec.ok,
        "summary": rec.summary,
    }


@hooks_app.command("list")
def cmd_list(
    json_out: bool = typer.Option(
        False, "--json", help="Machine-readable output."
    ),
) -> None:
    """List all known hook events with last-fire metadata."""
    rows: list[dict] = []
    for event in _known_events():
        last = _last_fire(event)
        rows.append(
            {
                "event": event,
                "last_fired_utc": (
                    datetime.fromtimestamp(
                        last["ts_utc"], tz=timezone.utc
                    ).isoformat()
                    if last
                    else None
                ),
                "last_source": last["source"] if last else None,
                "last_result": (
                    ("ok" if last["ok"] else "err") if last else None
                ),
                "last_summary": last["summary"] if last else None,
            }
        )

    if json_out:
        typer.echo(json.dumps(rows))
        return

    table = Table(title="Hook events")
    table.add_column("Event", style="cyan")
    table.add_column("Last fired (UTC)", style="dim")
    table.add_column("Source")
    table.add_column("Result")
    table.add_column("Summary")
    for row in rows:
        table.add_row(
            row["event"],
            row["last_fired_utc"] or "—",
            row["last_source"] or "—",
            row["last_result"] or "—",
            (row["last_summary"] or "")[:40],
        )
    _console.print(table)


@hooks_app.command("test")
def cmd_test(
    event: str = typer.Argument(
        ..., help="Hook event name (e.g. UserPromptSubmit)."
    ),
    payload: str = typer.Option(
        "{}", "--payload", help="JSON-encoded synthetic payload."
    ),
    execute: bool = typer.Option(
        False, "--execute", help="Actually dispatch (default: dry-run)."
    ),
) -> None:
    """Fire a synthetic hook event. Default is dry-run."""
    try:
        payload_obj = json.loads(payload)
    except json.JSONDecodeError as exc:
        _console.print(f"[red]Invalid --payload JSON:[/red] {exc}")
        raise typer.Exit(1)

    if not execute:
        _console.print(
            f"[yellow]dry-run:[/yellow] would fire {event} with {payload_obj!r}"
        )
        # Best-effort: surface registered handlers from the engine without
        # invoking them.
        try:
            from opencomputer.hooks.engine import engine
            from plugin_sdk.hooks import HookEvent

            try:
                event_enum = HookEvent(event)
            except ValueError:
                _console.print(
                    f"  [dim](unknown event {event!r}; "
                    f"known events: {[e.value for e in HookEvent]})[/dim]"
                )
                return
            specs = engine._ordered_specs(event_enum)  # noqa: SLF001
            if not specs:
                _console.print(
                    "  [dim](no handlers registered for this event)[/dim]"
                )
            for spec in specs:
                handler_id = getattr(
                    spec.handler, "__qualname__", repr(spec.handler)
                )
                _console.print(f"  would invoke: {handler_id}")
        except Exception as exc:  # noqa: BLE001
            _console.print(
                f"  [dim](handler enumeration unavailable: {exc})[/dim]"
            )
        return

    _console.print(
        "[red]--execute is not yet implemented;[/red] use dry-run for now."
    )
    raise typer.Exit(2)


@hooks_app.command("clear")
def cmd_clear() -> None:
    """Clear in-memory hook fire history."""
    n = clear_history()
    _console.print(f"[green]Cleared {n} fire records.[/green]")


@hooks_app.command("revoke")
def cmd_revoke(
    plugin_id: str = typer.Argument(..., help="Plugin id to disable hooks for."),
) -> None:
    """Disable a plugin's hooks via settings.local.json."""
    target = _profile_dir() / "settings.local.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            data = json.loads(target.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    revoked = list(data.get("disabled_hooks", []))
    if plugin_id not in revoked:
        revoked.append(plugin_id)
    data["disabled_hooks"] = revoked
    target.write_text(json.dumps(data, indent=2))
    _console.print(f"[green]Revoked hooks for[/green] {plugin_id}")
    _console.print(f"  written to: {target}")
