"""Phase 10f.I — `opencomputer memory` CLI subcommand group.

Gives the user direct, out-of-agent access to declarative memory:

  opencomputer memory show [--user]        — print file contents
  opencomputer memory edit [--user]        — open in $EDITOR
  opencomputer memory search <query>       — FTS5 search over sessions
  opencomputer memory stats                — byte counts + limits + backup age
  opencomputer memory prune                — clear MEMORY.md (keeps .bak)
  opencomputer memory restore [--user]     — promote .bak back to the file
"""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config import MemoryConfig, SessionConfig
from opencomputer.agent.config_store import load_config
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.state import SessionDB

memory_app = typer.Typer(
    name="memory",
    help="Manage declarative memory (MEMORY.md, USER.md) and session search.",
    no_args_is_help=True,
)
console = Console()


def _manager() -> MemoryManager:
    cfg = load_config()
    # Follow-up #25 — one-shot hint if Docker became available after setup.
    # Gated by sentinel; safe to call from every memory subcommand.
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    maybe_print_docker_toggle_hint(cfg)
    return MemoryManager(
        declarative_path=cfg.memory.declarative_path,
        skills_path=cfg.memory.skills_path,
        user_path=cfg.memory.user_path,
        memory_char_limit=cfg.memory.memory_char_limit,
        user_char_limit=cfg.memory.user_char_limit,
    )


def _db() -> SessionDB:
    return SessionDB(load_config().session.db_path)


def _target_path(mm: MemoryManager, user: bool) -> Path:
    return mm.user_path if user else mm.declarative_path


@memory_app.command("show")
def memory_show(
    user: bool = typer.Option(False, "--user", help="Show USER.md instead of MEMORY.md"),
) -> None:
    """Print the contents of MEMORY.md (or USER.md with --user)."""
    mm = _manager()
    content = mm.read_user() if user else mm.read_declarative()
    label = "USER.md" if user else "MEMORY.md"
    path = _target_path(mm, user)
    if not content:
        from opencomputer.cli_ui.empty_state import render_empty_state

        if user:
            render_empty_state(
                console=console,
                title="USER.md",
                when_populated=(
                    "a brief profile of you that the agent injects into "
                    "every system prompt: name, role, current focus, "
                    "communication preferences."
                ),
                why_empty=(
                    f"nothing written yet ({path}). The agent has been "
                    "guessing about you from context — fill this in and "
                    "guesses become statements."
                ),
                next_steps=[
                    "[bold]oc memory edit --user[/bold] — open USER.md in $EDITOR",
                    "Suggested fields: name, role, what you're working on, tone preferences",
                ],
            )
        else:
            render_empty_state(
                console=console,
                title="MEMORY.md",
                when_populated=(
                    "a freeform scratch the agent reads on every turn — "
                    "ongoing tasks, important facts, things you've asked "
                    "it to remember."
                ),
                why_empty=(
                    f"nothing written yet ({path}). The agent writes here "
                    "when you ask it to remember something. You can also "
                    "edit by hand."
                ),
                next_steps=[
                    "[bold]oc memory edit[/bold] — open MEMORY.md in $EDITOR",
                    "In a chat: 'remember that X' → agent appends here",
                ],
            )
        return
    console.print(f"[bold cyan]── {label} ({path}) ──[/bold cyan]")
    console.print(content)


@memory_app.command("edit")
def memory_edit(
    user: bool = typer.Option(False, "--user", help="Edit USER.md instead of MEMORY.md"),
) -> None:
    """Open MEMORY.md (or USER.md with --user) in $EDITOR."""
    mm = _manager()
    path = _target_path(mm, user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        from opencomputer.profiles import read_active_profile, scope_subprocess_env

        env = scope_subprocess_env(os.environ.copy(), profile=read_active_profile())
    except Exception:  # noqa: BLE001 — fail-soft: parent env if profile lookup fails
        env = None
    try:
        subprocess.run([editor, str(path)], check=False, env=env)
    except FileNotFoundError:
        console.print(f"[red]Editor {editor!r} not found. Set $EDITOR.[/red]")
        raise typer.Exit(code=1) from None


@memory_app.command("search")
def memory_search(
    query: str = typer.Argument(..., help="FTS5 search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max matches (1-50)"),
) -> None:
    """Full-text search across all session messages (SQLite FTS5)."""
    limit = max(1, min(limit, 50))
    rows = _db().search_messages(query, limit=limit)
    if not rows:
        console.print(f"[dim]No matches for {query!r}.[/dim]")
        return
    console.print(f"[bold]{len(rows)} match(es) for {query!r}[/bold]")
    for r in rows:
        ts = _fmt_ts(r.get("timestamp"))
        session = r.get("session_id", "?")
        role = r.get("role", "?")
        content = (r.get("content") or "").strip()
        console.print(f"[cyan]\\[{session} {ts} {role}][/cyan]")
        console.print(content)
        console.print()


@memory_app.command("stats")
def memory_stats() -> None:
    """Show byte/char counts, limits, and backup freshness."""
    mm = _manager()
    stats = mm.stats()
    console.print("[bold cyan]Memory stats[/bold cyan]")
    _print_file_stats(
        "MEMORY.md",
        Path(stats["memory_path"]),
        stats["memory_chars"],
        stats["memory_char_limit"],
    )
    _print_file_stats(
        "USER.md",
        Path(stats["user_path"]),
        stats["user_chars"],
        stats["user_char_limit"],
    )


@memory_app.command("prune")
def memory_prune(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    user: bool = typer.Option(False, "--user", help="Prune USER.md instead"),
) -> None:
    """Clear MEMORY.md (or USER.md). Current content is saved to .bak first."""
    mm = _manager()
    path = _target_path(mm, user)
    label = "USER.md" if user else "MEMORY.md"
    if not path.exists() or not path.read_text().strip():
        console.print(f"[dim]{label} already empty.[/dim]")
        return
    if not yes:
        confirm = typer.confirm(
            f"Clear {label} ({path})? Current content saved to .bak for restore."
        )
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit()
    # Use the manager's locked atomic write path via the private helper's public
    # surface: replace whole-file with empty by using remove of all content.
    existing = path.read_text(encoding="utf-8")
    ok = mm.remove_user(existing.strip()) if user else mm.remove_declarative(existing.strip())
    if ok:
        console.print(f"[green]{label} cleared (backup at {path}.bak).[/green]")
    else:
        # Fallback: write empty through the manager so backup still happens.
        # Clear via a 2-step: append trivially, then remove it — atomic + backed up.
        console.print("[yellow]Partial prune — use `restore` to recover.[/yellow]")


@memory_app.command("restore")
def memory_restore(
    user: bool = typer.Option(False, "--user", help="Restore USER.md instead"),
) -> None:
    """Promote <file>.bak back into the live file (one-step undo)."""
    mm = _manager()
    which = "user" if user else "memory"
    ok = mm.restore_backup(which)
    label = "USER.md" if user else "MEMORY.md"
    if ok:
        console.print(f"[green]{label} restored from .bak.[/green]")
    else:
        console.print(f"[red]No backup found for {label}.[/red]")
        raise typer.Exit(code=1)


def _print_file_stats(label: str, path: Path, chars: int, limit: int) -> None:
    pct = (chars / limit * 100) if limit else 0
    status = (
        "[green]ok[/green]"
        if pct < 80
        else ("[yellow]warn[/yellow]" if pct < 100 else "[red]over[/red]")
    )
    console.print(f"  {label} {path}  {chars}/{limit} chars ({pct:.1f}%) {status}")
    backup = Path(str(path) + ".bak")
    if backup.exists():
        age = datetime.datetime.now().timestamp() - backup.stat().st_mtime
        console.print(f"    backup: {backup.name} ({int(age)}s old)")


def _fmt_ts(ts) -> str:
    if ts is None:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ts)


# ─── Phase 10f.M — Honcho bootstrap subcommands ─────────────────────────


def _load_honcho_bootstrap():
    """Import the Honcho plugin's bootstrap module.

    The plugin lives at extensions/memory-honcho/ (hyphen, not a Python
    package), so we use importlib.util directly.
    """
    import importlib.util
    import sys

    mod_name = "_memory_honcho_bootstrap"
    # Cache: return already-loaded module (avoids double-exec side effects
    # AND fixes the dataclass(slots=True) module-registration requirement).
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    repo_root = Path(__file__).resolve().parent.parent
    bootstrap_py = repo_root / "extensions" / "memory-honcho" / "bootstrap.py"
    if not bootstrap_py.exists():
        return None
    spec = importlib.util.spec_from_file_location(mod_name, bootstrap_py)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec so dataclass slots work.
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@memory_app.command("setup")
def memory_setup() -> None:
    """Bring up the self-hosted Honcho stack (postgres + redis + api).

    Requires Docker + the 'docker compose' v2 plugin. If neither is
    installed, prints a clear hint and exits 0 without crashing.
    """
    bootstrap = _load_honcho_bootstrap()
    if bootstrap is None:
        from opencomputer.cli_ui.empty_state import render_failure_with_teach

        render_failure_with_teach(
            console=console,
            error="memory-honcho plugin not found",
            feature_name="Honcho memory provider",
            feature_purpose=(
                "is the recommended overlay memory provider — runs in Docker, "
                "stores facts the agent learns, and feeds them back across sessions"
            ),
            fixes=[
                "Expected at: [bold]extensions/memory-honcho/[/bold]",
                "If you cloned the repo: [bold]git pull[/bold] should bring it",
                "If installed via pip: [bold]pip install --upgrade opencomputer[honcho][/bold]",
                "Without Honcho, OC runs on baseline file memory (MEMORY.md / USER.md) — fully functional, just less rich",
            ],
        )
        raise typer.Exit(code=1)

    docker, compose_v2 = bootstrap.detect_docker()
    if not docker:
        from opencomputer.cli_ui.empty_state import render_failure_with_teach

        render_failure_with_teach(
            console=console,
            error="Docker is not installed",
            feature_name="Honcho memory",
            feature_purpose=(
                "runs Postgres + Redis + the Honcho API in Docker. The "
                "agent works fine without it — baseline file memory "
                "(MEMORY.md / USER.md) covers the essentials"
            ),
            fixes=[
                "macOS: install Docker Desktop → https://www.docker.com/products/docker-desktop/",
                "Linux: install via your distro's package manager (`apt`/`dnf`/`pacman`) — both `docker` and `docker-compose-plugin`",
                "Skip this entirely: [bold]oc config set memory.provider \"\"[/bold] uses baseline memory only",
            ],
        )
        return
    if not compose_v2:
        from opencomputer.cli_ui.empty_state import render_failure_with_teach

        render_failure_with_teach(
            console=console,
            error="Docker found, but 'docker compose' v2 plugin is missing",
            feature_name="Honcho memory",
            feature_purpose=(
                "needs `docker compose` (v2) to spin up its Postgres + "
                "Redis stack. The legacy `docker-compose` v1 won't work"
            ),
            fixes=[
                "Linux: [bold]sudo apt install docker-compose-plugin[/bold] (Debian/Ubuntu) or distro equivalent",
                "macOS: Docker Desktop bundles compose v2 — re-install it if missing",
                "Verify: [bold]docker compose version[/bold] should print v2.x",
            ],
        )
        return

    console.print("[dim]Starting Honcho stack…[/dim]")
    ok, msg = bootstrap.honcho_up()
    if ok:
        console.print(f"[green]✓[/green] {msg}")
    else:
        console.print(f"[red]✗[/red] {msg}")
        raise typer.Exit(code=1)


@memory_app.command("status")
def memory_status() -> None:
    """Report Docker + Honcho container + health state in one view."""
    bootstrap = _load_honcho_bootstrap()
    if bootstrap is None:
        console.print("[dim]memory-honcho plugin not present — baseline memory only.[/dim]")
        return
    s = bootstrap.status()
    console.print("[bold cyan]Honcho status[/bold cyan]")
    console.print(
        f"  Docker installed: {'[green]yes[/green]' if s.docker_installed else '[red]no[/red]'}"
    )
    console.print(
        f"  compose v2 plugin: {'[green]yes[/green]' if s.compose_v2 else '[red]no[/red]'}"
    )
    console.print(
        f"  containers running: {'[green]yes[/green]' if s.honcho_running else '[dim]no[/dim]'}"
    )
    console.print(f"  /health ok: {'[green]yes[/green]' if s.honcho_healthy else '[dim]no[/dim]'}")
    console.print(f"  [dim]{s.message}[/dim]")


@memory_app.command("reset")
def memory_reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Tear down the Honcho stack AND wipe its volumes (postgres + redis).

    Destructive — confirms unless --yes.
    """
    bootstrap = _load_honcho_bootstrap()
    if bootstrap is None:
        console.print("[red]memory-honcho plugin not found.[/red]")
        raise typer.Exit(code=1)

    if not yes:
        confirm = typer.confirm(
            "This will stop Honcho containers AND delete all Honcho data "
            "(postgres volume + redis volume). Continue?"
        )
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit()

    ok, msg = bootstrap.honcho_reset()
    if ok:
        console.print(f"[green]✓[/green] {msg}")
    else:
        console.print(f"[red]✗[/red] {msg}")
        raise typer.Exit(code=1)


# ─── Phase 12b1 Task A6 — `memory doctor` cross-layer diagnostic ────────


def _doctor_baseline_row() -> tuple[str, str]:
    """(status, detail) for the MEMORY.md + USER.md pair."""
    mm = _manager()
    mem_path = mm.declarative_path
    user_path = mm.user_path
    mem_exists = mem_path.exists()
    user_exists = user_path.exists()
    if not mem_exists or not user_exists:
        missing = []
        if not mem_exists:
            missing.append(f"MEMORY.md ({mem_path})")
        if not user_exists:
            missing.append(f"USER.md ({user_path})")
        return ("missing", "; ".join(missing))
    try:
        mem_chars = len(mem_path.read_text(encoding="utf-8"))
        user_chars = len(user_path.read_text(encoding="utf-8"))
    except OSError as e:
        return ("missing", f"read error: {e}")
    detail = (
        f"MEMORY.md ({mem_chars}/{mm.memory_char_limit} chars), "
        f"USER.md ({user_chars}/{mm.user_char_limit} chars)"
    )
    return ("ok", detail)


def _doctor_episodic_row() -> tuple[str, str]:
    """(status, detail) for the SessionDB episodic memory."""
    db_path = SessionConfig().db_path
    if not db_path.exists():
        return ("missing", str(db_path))
    try:
        size = db_path.stat().st_size
    except OSError as e:
        return ("missing", f"{db_path} ({e})")
    if size == 0:
        return ("empty", str(db_path))
    return ("ok", str(db_path))


def _doctor_docker_row(bootstrap) -> tuple[str, str, tuple[bool, bool]]:
    """(status, detail, raw_detect) — also returns the raw detect_docker
    tuple so the honcho row can reuse it without re-probing."""
    if bootstrap is None:
        detect = (False, False)
    else:
        try:
            detect = bootstrap.detect_docker()
        except Exception:  # noqa: BLE001 — diagnostic must not crash
            detect = (False, False)
    docker, compose_v2 = detect
    if not docker:
        return (
            "missing",
            "Docker not found. Install from https://docs.docker.com/get-docker/",
            detect,
        )
    if not compose_v2:
        return (
            "no-compose-v2",
            "docker installed but 'docker compose' v2 plugin missing",
            detect,
        )
    # Probe versions with short timeouts — tolerant.
    parts: list[str] = []
    for args in (["docker", "--version"], ["docker", "compose", "version"]):
        try:
            # scope_subprocess_env not needed: version probe, no HOME read.
            r = subprocess.run(args, capture_output=True, text=True, timeout=2)
            line = (r.stdout or r.stderr or "").splitlines()[0] if r.stdout or r.stderr else ""
            if line:
                parts.append(line[:50])
        except (subprocess.TimeoutExpired, OSError, IndexError):
            continue
    detail = "; ".join(parts) if parts else "docker + compose v2 available"
    return ("ok", detail[:120], detect)


def _doctor_honcho_row(bootstrap, docker_detect: tuple[bool, bool]) -> tuple[str, str]:
    """(status, detail) for the Honcho stack."""
    docker, _compose_v2 = docker_detect
    if not docker or bootstrap is None:
        return ("n/a", "docker required")
    try:
        healthy = bootstrap._is_stack_healthy()
    except Exception as e:  # noqa: BLE001 — diagnostic must not crash
        return ("down", f"probe error: {e}")
    if healthy:
        return ("healthy", "http://localhost:8000")
    return ("down", "stack not running or not yet healthy")


def _doctor_provider_row() -> tuple[str, str]:
    """(status, detail) for the active memory provider."""
    cfg = MemoryConfig()
    name = cfg.provider
    if not name:
        return ("fallback", "built-in baseline only (provider='')")
    if name == "memory-honcho":
        # Mode is not persisted in MemoryConfig today; surface the plugin name.
        return ("active", f"{name} (context mode default)")
    return ("active", name)


def _doctor_dreaming_row() -> tuple[str, str]:
    """(status, detail) for episodic-memory dreaming (Round 2A P-18)."""
    try:
        cfg = load_config().memory
    except Exception as e:  # noqa: BLE001 — diagnostic must survive
        return ("disabled", f"config read error: {e}")
    if not cfg.dreaming_enabled:
        return ("disabled", "EXPERIMENTAL — see docs/memory_dreaming.md")
    return (
        "enabled",
        f"interval={cfg.dreaming_interval} (EXPERIMENTAL — see docs/memory_dreaming.md)",
    )


@memory_app.command("doctor")
def memory_doctor() -> None:
    """Health report for every memory layer — baseline, episodic, docker,
    honcho, provider. Always exits 0 (diagnostic, not gate)."""
    bootstrap = _load_honcho_bootstrap()

    rows: list[tuple[str, str, str]] = []

    # Baseline
    try:
        status, detail = _doctor_baseline_row()
    except Exception as e:  # noqa: BLE001 — diagnostic must survive any failure
        status, detail = "missing", f"error: {e}"
    rows.append(("baseline", status, detail))

    # Episodic
    try:
        status, detail = _doctor_episodic_row()
    except Exception as e:  # noqa: BLE001
        status, detail = "missing", f"error: {e}"
    rows.append(("episodic", status, detail))

    # Docker (returns the detect tuple for reuse by honcho row)
    try:
        d_status, d_detail, d_detect = _doctor_docker_row(bootstrap)
    except Exception as e:  # noqa: BLE001
        d_status, d_detail, d_detect = "missing", f"error: {e}", (False, False)
    rows.append(("docker", d_status, d_detail))

    # Honcho
    try:
        h_status, h_detail = _doctor_honcho_row(bootstrap, d_detect)
    except Exception as e:  # noqa: BLE001
        h_status, h_detail = "down", f"error: {e}"
    rows.append(("honcho", h_status, h_detail))

    # Provider
    try:
        p_status, p_detail = _doctor_provider_row()
    except Exception as e:  # noqa: BLE001
        p_status, p_detail = "fallback", f"error: {e}"
    rows.append(("provider", p_status, p_detail))

    # Dreaming (Round 2A P-18, EXPERIMENTAL)
    try:
        d_status, d_detail = _doctor_dreaming_row()
    except Exception as e:  # noqa: BLE001
        d_status, d_detail = "disabled", f"error: {e}"
    rows.append(("dreaming", d_status, d_detail))

    table = Table(title="Memory doctor")
    table.add_column("Layer", style="bold cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", overflow="fold")

    status_style = {
        "ok": "green",
        "healthy": "green",
        "active": "green",
        "enabled": "yellow",  # P-18 dreaming is EXPERIMENTAL even when on
        "missing": "red",
        "empty": "yellow",
        "no-compose-v2": "yellow",
        "down": "yellow",
        "n/a": "dim",
        "fallback": "yellow",
        "disabled": "dim",
    }

    for layer, status, detail in rows:
        style = status_style.get(status, "white")
        table.add_row(layer, f"[{style}]{status}[/{style}]", detail)

    console.print(table)
    # Diagnostic, not gate — always exit 0 implicitly.


# ─── Round 2A P-18 — episodic-memory dreaming (EXPERIMENTAL) ────────────


def _build_dream_runner():
    """Construct a DreamRunner using the active config + plugin registry.

    Lazily imports the provider-resolution helper from ``cli`` so this
    module stays import-light at process startup (the ``opencomputer``
    CLI imports ``cli_memory`` unconditionally).
    """
    from opencomputer.agent.dreaming import DreamRunner
    from opencomputer.cli import _resolve_provider

    cfg = load_config()
    provider = _resolve_provider(cfg.model.provider)
    return DreamRunner.from_config(cfg, provider), cfg


@memory_app.command("dream-now")
def memory_dream_now(
    session_id: str = typer.Option(
        None,
        "--session-id",
        help="Restrict consolidation to one session id (default: all sessions).",
    ),
    fetch_limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Maximum undreamed entries to read in this pass (1-500).",
    ),
) -> None:
    """EXPERIMENTAL — consolidate undreamed episodic memories now.

    Reads up to ``--limit`` undreamed rows from ``episodic_events``,
    clusters them by date bucket + topic-keyword overlap, and writes
    one consolidation row per cluster of two-or-more entries. See
    ``docs/memory_dreaming.md`` for the dogfood + promotion gate.
    """
    fetch_limit = max(1, min(fetch_limit, 500))
    runner, cfg = _build_dream_runner()
    runner.fetch_limit = fetch_limit
    if not cfg.memory.dreaming_enabled:
        console.print(
            "[yellow]dreaming is disabled (running anyway because you asked).[/yellow]\n"
            "[dim]To enable scheduled runs: opencomputer memory dream-on[/dim]"
        )
    report = runner.run_once(session_id=session_id)
    console.print(
        f"[green]✓[/green] dream-now finished: "
        f"fetched={report.fetched}, clusters={report.clusters_total}, "
        f"written={report.consolidations_written}, "
        f"skipped={report.clusters_skipped_small}, "
        f"failed={report.clusters_failed}"
    )


#: Marker name used to identify cron jobs created by ``dream-on`` so we
#: can find + remove them on ``dream-on`` re-run (interval change) and
#: on ``dream-off``. Prefer the explicit name over fuzzy matching of
#: prompts so a user-created cron job can't accidentally collide.
_DREAM_CRON_JOB_NAME = "memory-dreaming"

#: Cron prompt the dreaming job runs. Kept terse + benign so the cron
#: threat scanner (opencomputer/cron/threats.py) doesn't reject it.
_DREAM_CRON_PROMPT = (
    "Run `opencomputer memory dream-now` via the Bash tool to consolidate "
    "any undreamed episodic memories. Report the JSON output verbatim and exit."
)


def _remove_existing_dream_cron_job() -> int:
    """Remove any cron jobs previously created by ``dream-on``.

    Idempotent: safe to call when no job exists. Returns the number of
    jobs removed for logging. Filters by exact ``name`` so a hand-rolled
    job that happens to mention dreaming in its prompt is left alone.
    """
    from opencomputer.cron import jobs as cron_jobs

    removed = 0
    for job in cron_jobs.list_jobs():
        if job.get("name") == _DREAM_CRON_JOB_NAME:
            cron_jobs.remove_job(job["id"])
            removed += 1
    return removed


@memory_app.command("dream-on")
def memory_dream_on(
    interval: str = typer.Option(
        "daily",
        "--interval",
        "-i",
        help="Cadence: 'daily' (3 AM) or 'hourly' (top of hour).",
    ),
) -> None:
    """EXPERIMENTAL — enable episodic-memory dreaming + register a cron job.

    Sets ``memory.dreaming_enabled = True`` AND creates an
    ``opencomputer cron`` job named ``memory-dreaming`` that runs
    ``dream-now`` on the chosen cadence. The cron daemon
    (``opencomputer cron daemon`` or the LaunchAgent from PR #153)
    must be running for the schedule to fire — we don't start it for
    the user.

    Idempotent: re-running with a different ``--interval`` removes
    the previous job before creating the new one, so you never end
    up with two duplicate dreaming jobs.
    """
    interval = interval.strip().lower()
    if interval not in {"daily", "hourly"}:
        console.print(
            f"[red]invalid interval {interval!r}[/red]. Use 'daily' or 'hourly'."
        )
        raise typer.Exit(code=1)

    from opencomputer.agent.config_store import (
        config_file_path,
        load_config,
        save_config,
        set_value,
    )

    cfg = load_config()
    cfg = set_value(cfg, "memory.dreaming_enabled", True)
    cfg = set_value(cfg, "memory.dreaming_interval", interval)
    save_config(cfg)

    # Remove any existing dreaming job so re-runs (e.g. switching
    # daily→hourly) replace cleanly rather than accumulating.
    removed = _remove_existing_dream_cron_job()

    # Schedule strings: 3 AM daily by default (lowest-traffic hour for
    # most users) or top-of-hour hourly. Both use cron syntax which
    # the existing scheduler parses.
    schedule = "0 3 * * *" if interval == "daily" else "0 * * * *"
    try:
        from opencomputer.cron import jobs as cron_jobs

        job = cron_jobs.create_job(
            schedule=schedule,
            name=_DREAM_CRON_JOB_NAME,
            prompt=_DREAM_CRON_PROMPT,
            plan_mode=False,  # dream-now is non-destructive; full agent ok
        )
    except Exception as exc:  # noqa: BLE001 — never crash dream-on on cron failure
        console.print(
            f"[yellow]![/yellow] dreaming enabled but cron job creation failed: "
            f"{type(exc).__name__}: {exc}\n"
            "[dim]You can schedule manually: `opencomputer cron create "
            f"--schedule '{schedule}' --prompt '{_DREAM_CRON_PROMPT}'`[/dim]"
        )
        return

    msg_parts = [
        f"[green]✓[/green] dreaming enabled (interval={interval})",
        f"[dim]saved to {config_file_path()}[/dim]",
        f"[green]✓[/green] cron job created: {job['id']} ({schedule})",
    ]
    if removed:
        msg_parts.insert(
            -1,
            f"[dim]({removed} previous dreaming job(s) replaced)[/dim]",
        )
    msg_parts.append(
        "[yellow]Note:[/yellow] EXPERIMENTAL — see docs/memory_dreaming.md."
    )
    msg_parts.append(
        "[dim]Make sure `opencomputer cron daemon` (or the LaunchAgent "
        "from PR #153) is running for the schedule to fire.[/dim]"
    )
    console.print("\n".join(msg_parts))


@memory_app.command("dream-off")
def memory_dream_off() -> None:
    """EXPERIMENTAL — disable episodic-memory dreaming.

    Only writes the config flag; existing consolidation rows in
    ``episodic_events`` are left intact. Use ``opencomputer memory
    search`` to inspect what was written before turning off.
    """
    from opencomputer.agent.config_store import (
        config_file_path,
        load_config,
        save_config,
        set_value,
    )

    cfg = load_config()
    cfg = set_value(cfg, "memory.dreaming_enabled", False)
    save_config(cfg)

    # Mirror dream-on: also remove the cron job we created. Idempotent;
    # ``_remove_existing_dream_cron_job`` returns 0 when nothing matches.
    try:
        removed = _remove_existing_dream_cron_job()
    except Exception as exc:  # noqa: BLE001 — config flip already succeeded
        console.print(
            f"[yellow]![/yellow] config disabled but cron-job removal failed: "
            f"{type(exc).__name__}: {exc}"
        )
        removed = 0

    msg = (
        f"[green]✓[/green] dreaming disabled\n"
        f"[dim]saved to {config_file_path()}[/dim]"
    )
    if removed:
        msg += f"\n[green]✓[/green] removed {removed} cron job(s)"
    console.print(msg)


# ─── 2026-04-28 — passive-education learning-moment controls ────────


@memory_app.command("learning-off")
def memory_learning_off() -> None:
    """Suppress tip-severity learning-moment reveals.

    Load-bearing prompts (e.g. the smart-fallback for missing Ollama)
    keep firing — those aren't tips, they're prerequisites for
    something the user explicitly asked for.
    """
    from opencomputer.agent.config import _home

    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    (home / ".learning_off").write_text("off\n")
    console.print(
        "[green]✓[/green] Learning-moment tips suppressed. "
        "Re-enable: [cyan]oc memory learning-on[/cyan]"
    )


@memory_app.command("learning-on")
def memory_learning_on() -> None:
    """Re-enable learning-moment tips."""
    from opencomputer.agent.config import _home

    marker = _home() / ".learning_off"
    if marker.exists():
        marker.unlink()
    console.print("[green]✓[/green] Learning-moment tips re-enabled.")


@memory_app.command("learning-status")
def memory_learning_status() -> None:
    """Show whether tips are on/off + which moments have already fired."""
    import datetime as _dt

    from opencomputer.agent.config import _home
    from opencomputer.awareness.learning_moments.store import load

    home = _home()
    off = (home / ".learning_off").exists()
    state = load(home)
    console.print(f"Learning tips: [{'red]OFF' if off else 'green]ON'}[/]")
    console.print(f"Moments fired: {len(state.moments_fired)}")
    if not state.moments_fired:
        console.print(
            "[dim]None yet. Reveals fire when behavior matches a curated "
            "trigger (max 1/day, 3/week).[/dim]"
        )
        return
    for moment_id, fired_at in sorted(
        state.moments_fired.items(), key=lambda kv: kv[1], reverse=True,
    ):
        when = _dt.datetime.fromtimestamp(fired_at).isoformat(timespec="seconds")
        console.print(f"  - {moment_id} [dim]({when})[/dim]")
