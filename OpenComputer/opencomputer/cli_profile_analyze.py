"""Plan 3 — ``oc profile analyze`` Typer subapp.

Subcommands:
  run        — manual one-shot analysis (writes cache)
  install    — install OS-level cron (launchd on macOS, systemd on Linux)
  uninstall  — remove OS-level cron
  status     — show install state + last-run timestamp from cache

V1 limitation: only the default profile's SessionDB is analyzed.
A user who has been on a named profile for months won't get
suggestions like "you should split your work history" — the
analysis is anchored on default. Acceptable for V1; revisit if
real users on named profiles complain.
"""
from __future__ import annotations

import datetime as _dt
import shutil as _shutil
import sys as _sys

import typer
from rich.console import Console

profile_analyze_app = typer.Typer(
    name="analyze",
    help="Analyze usage patterns and suggest profiles.",
    invoke_without_command=False,
)
_console = Console()


@profile_analyze_app.command("run")
def analyze_run() -> None:
    """One-shot: read SessionDB, compute suggestions, write cache."""
    from opencomputer.agent.state import SessionDB
    from opencomputer.profile_analysis_daily import (
        compute_daily_suggestions,
        load_cache,
        save_cache,
    )
    from opencomputer.profiles import get_default_root, list_profiles

    db_path = get_default_root() / "default" / "sessions.db"
    if not db_path.exists():
        _console.print(
            f"[yellow]No session history found at {db_path} — "
            "nothing to analyze.[/yellow]"
        )
        return

    db = SessionDB(db_path)
    rows = db.list_sessions(limit=30)
    available = tuple(list_profiles())
    suggestions = compute_daily_suggestions(rows, available_profiles=available)

    # Preserve dismissals across runs.
    prev = load_cache() or {}
    dismissed = prev.get("dismissed", [])

    save_cache(suggestions=suggestions, dismissed=dismissed)

    if not suggestions:
        _console.print(
            f"[dim]Analyzed {len(rows)} sessions — no clear patterns yet "
            "(need ≥10 sessions and a strong cluster). Cache updated.[/dim]"
        )
        return

    _console.print(
        f"[green]Analyzed {len(rows)} sessions — "
        f"{len(suggestions)} suggestion(s):[/green]"
    )
    for s in suggestions:
        _console.print(f"  • [bold]{s.name}[/bold] — {s.rationale}")
        _console.print(f"    Accept: [cyan]{s.command}[/cyan]")


@profile_analyze_app.command("install")
def analyze_install() -> None:
    """Install the daily background analyzer cron."""
    exe = _shutil.which("opencomputer") or f"{_sys.executable} -m opencomputer"
    if _sys.platform == "darwin":
        from opencomputer.service.launchd import install_launchd_plist
        path = install_launchd_plist(executable=exe)
        _console.print(f"[green]launchd plist installed:[/green] {path}")
        _console.print(
            "Daily run at 9am local. View logs: "
            "~/.opencomputer/profile-analyze.log"
        )
    elif _sys.platform.startswith("linux"):
        from opencomputer.service import install_profile_analyze_timer
        timer, service = install_profile_analyze_timer(executable=exe)
        _console.print(f"[green]systemd timer installed:[/green] {timer}")
        _console.print(f"[green]systemd service installed:[/green] {service}")
        _console.print("Daily via OnCalendar=daily.")
    else:
        _console.print(
            f"[yellow]No background scheduler for {_sys.platform!r}.[/yellow] "
            "Run `oc profile analyze run` manually."
        )


@profile_analyze_app.command("uninstall")
def analyze_uninstall() -> None:
    """Remove the cron."""
    if _sys.platform == "darwin":
        from opencomputer.service.launchd import uninstall_launchd_plist
        path = uninstall_launchd_plist()
        if path:
            _console.print(f"[green]launchd plist removed:[/green] {path}")
        else:
            _console.print("[dim]launchd plist was not installed.[/dim]")
    elif _sys.platform.startswith("linux"):
        from opencomputer.service import uninstall_profile_analyze_timer
        timer, service = uninstall_profile_analyze_timer()
        if timer:
            _console.print(f"[green]systemd timer removed:[/green] {timer}")
        else:
            _console.print("[dim]systemd timer was not installed.[/dim]")
    else:
        _console.print(
            f"[dim]Nothing to uninstall on {_sys.platform!r}.[/dim]"
        )


@profile_analyze_app.command("status")
def analyze_status() -> None:
    """Show install state + last-run timestamp."""
    from opencomputer.profile_analysis_daily import load_cache

    if _sys.platform == "darwin":
        from opencomputer.service.launchd import is_loaded
        installed = is_loaded()
    elif _sys.platform.startswith("linux"):
        from opencomputer.service import is_profile_analyze_timer_active
        installed = is_profile_analyze_timer_active()
    else:
        installed = False

    label = "[green]yes[/green]" if installed else "[red]no[/red]"
    _console.print(f"Installed: {label}")

    cache = load_cache()
    if cache and "last_run" in cache:
        ts = _dt.datetime.fromtimestamp(cache["last_run"])
        _console.print(f"Last run: {ts.isoformat()}")
        _console.print(f"Suggestions in cache: {len(cache.get('suggestions', []))}")
    else:
        _console.print("[dim]Last run: never (cache absent).[/dim]")


__all__ = ["profile_analyze_app"]
