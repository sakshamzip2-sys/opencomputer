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
from opencomputer.agent.memory import (
    MemoryManager,
    _segment_paragraphs,
    _strip_prior_compaction_header,
)
from opencomputer.agent.memory_cap import cap_status
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


def _flag_paragraph(text: str) -> list[str]:
    """Return human-readable drift flags for a single paragraph.

    Deterministic checks only (M3 scope; LLM-driven drift detection is
    explicitly out per the 2026-05-10 spec). Each flag is a short string
    rendered next to the paragraph in the audit table.
    """
    flags: list[str] = []
    upper = text.upper()
    if "TODO" in upper or "TBD" in upper or "FIXME" in upper:
        flags.append("[TODO]")
    if len(text) > 400:
        flags.append("[long]")
    if len(text) < 20:
        flags.append("[short]")
    return flags


def _audit_one_file(label: str, path: Path, body: str, limit: int) -> None:
    """Render the audit view for one memory file."""
    if not body.strip():
        console.print(f"[dim]{label} is empty (0 chars / {limit} cap).[/dim]")
        return

    status = cap_status(body, limit=limit, file_name=label)
    pct_int = int(round(status.pct * 100))
    pct_color = "green" if pct_int < 80 else ("yellow" if pct_int < 100 else "red")
    console.print(
        f"[bold cyan]── {label}[/bold cyan]  "
        f"[{pct_color}]{status.bytes_used}/{status.bytes_limit} chars "
        f"({pct_int}%)[/{pct_color}]  "
        f"[dim]{path}[/dim]"
    )

    cleaned = _strip_prior_compaction_header(body).strip()
    paragraphs = _segment_paragraphs(cleaned)
    if not paragraphs:
        console.print("[dim](no paragraphs to audit)[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("Chars", justify="right", no_wrap=True)
    table.add_column("Flags", no_wrap=True)
    table.add_column("Preview", overflow="fold")

    for idx, para in enumerate(paragraphs, start=1):
        flags = _flag_paragraph(para)
        # First-line snippet, truncated for table sanity.
        first_line = para.split("\n", 1)[0].strip()
        preview = first_line if len(first_line) <= 100 else first_line[:97] + "..."
        flags_text = " ".join(flags) if flags else "[dim]—[/dim]"
        table.add_row(str(idx), str(len(para)), flags_text, preview)

    console.print(table)
    flagged_count = sum(1 for p in paragraphs if _flag_paragraph(p))
    if flagged_count:
        console.print(
            f"[dim]flagged: {flagged_count} of {len(paragraphs)} paragraph(s)[/dim]"
        )


@memory_app.command("audit")
def memory_audit(
    user: bool = typer.Option(False, "--user", help="Audit USER.md instead of MEMORY.md"),
    all_files: bool = typer.Option(False, "--all", help="Audit BOTH MEMORY.md and USER.md"),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="Walk paragraphs and prompt keep/delete/replace/skip per entry",
    ),
) -> None:
    """Per-paragraph audit of MEMORY.md / USER.md.

    Read-only by default; ``--interactive`` enables write mode. Distinct from
    ``oc memory doctor`` (multi-layer health) — this command inspects the
    paragraphs of the agent's declarative memory files.
    """
    mm = _manager()
    targets: list[tuple[str, Path, str, int]] = []  # (label, path, body, limit)

    if all_files:
        targets.append(("MEMORY.md", mm.declarative_path, mm.read_declarative(), mm.memory_char_limit))
        targets.append(("USER.md", mm.user_path, mm.read_user(), mm.user_char_limit))
    elif user:
        targets.append(("USER.md", mm.user_path, mm.read_user(), mm.user_char_limit))
    else:
        targets.append(("MEMORY.md", mm.declarative_path, mm.read_declarative(), mm.memory_char_limit))

    for label, path, body, limit in targets:
        _audit_one_file(label, path, body, limit)
        if interactive:
            _audit_interactive_walk(mm, label, path, body)


def _audit_interactive_walk(mm: MemoryManager, label: str, path: Path, body: str) -> None:
    """Walk each paragraph and prompt keep/delete/replace/skip per entry.

    Delegates writes to the existing ``MemoryManager.remove_*`` /
    ``replace_*`` paths so locking, atomic writes, ``.bak`` backup, and
    ``MemoryWriteEvent`` publication continue to work — same write path
    the Memory tool uses.

    Known limitation: ``remove_*`` is implemented via ``str.replace(block,
    "")`` (memory.py:922) which is a global substring match. If two
    paragraphs share identical text both are removed. In practice memory
    paragraphs are unique. Documented in the M4 spec section.
    """
    is_user = label == "USER.md"

    # Re-read just-in-time so we walk against the current state (the M3
    # caller passed in a snapshot which is fine for the read-only table,
    # but the interactive walk needs to track edits).
    current_body = mm.read_user() if is_user else mm.read_declarative()
    cleaned = _strip_prior_compaction_header(current_body).strip()
    paragraphs = _segment_paragraphs(cleaned)
    if not paragraphs:
        console.print(f"[dim]{label} has no paragraphs to walk.[/dim]")
        return

    console.print(
        f"[bold]Interactive walk over {label}[/bold] — "
        "[k]eep / [d]elete / [r]eplace / [s]kip per paragraph "
        "(unknown input = skip)."
    )

    total = len(paragraphs)
    for idx, para in enumerate(paragraphs, start=1):
        first_line = para.split("\n", 1)[0].strip()
        preview = first_line if len(first_line) <= 80 else first_line[:77] + "..."
        console.print(
            f"\n[cyan]\\[{idx}/{total}] ({len(para)} chars)[/cyan] {preview}"
        )
        try:
            action = typer.prompt("Action", default="s", show_default=False).strip().lower()
        except typer.Abort:
            console.print("[yellow]Aborted by user.[/yellow]")
            return

        if action in {"d", "delete"}:
            ok = mm.remove_user(para) if is_user else mm.remove_declarative(para)
            console.print(
                "[green]deleted.[/green]" if ok else "[yellow]not found (skipped).[/yellow]"
            )
        elif action in {"r", "replace"}:
            try:
                new_text = typer.prompt("Replacement text").strip()
            except typer.Abort:
                console.print("[yellow]Replacement aborted; paragraph kept.[/yellow]")
                continue
            if not new_text:
                console.print("[yellow]Empty replacement; paragraph kept.[/yellow]")
                continue
            ok = (
                mm.replace_user(para, new_text)
                if is_user
                else mm.replace_declarative(para, new_text)
            )
            console.print(
                "[green]replaced.[/green]" if ok else "[yellow]substring not found; skipped.[/yellow]"
            )
        elif action in {"k", "keep", "s", "skip", ""}:
            continue
        else:
            console.print(f"[dim]unknown action {action!r} — skipped.[/dim]")


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


def _doctor_active_memory_row() -> tuple[str, str]:
    """(status, detail) for OpenClaw 1.B-alt Active Memory pre-loop injection.

    Distinct from MEMORY.md hybrid retrieval — this layer queries SessionDB
    FTS5 + episodic on every turn and prepends a `<relevant-memories>`
    block. Off by default; useful when the user has rich session history.
    """
    try:
        cfg = load_config().memory
    except Exception as e:  # noqa: BLE001
        return ("disabled", f"config read error: {e}")
    if cfg.active_memory_enabled:
        return (
            "enabled",
            f"top_n={cfg.active_memory_top_n} (FTS5 + episodic prepend per turn)",
        )
    return (
        "disabled",
        "opt-in via memory.active_memory_enabled=true — recalls relevant "
        "past turns each request",
    )


def _doctor_vector_retrieval_row() -> tuple[str, str]:
    """(status, detail) for MEMORY.md hybrid retrieval (M6.3 BM25+vector RRF).

    Status mapping:
      * disabled  — memory_md_retrieval_enabled=false in config
      * active    — enabled AND active provider supports embeddings
      * bm25-only — enabled BUT provider raises EmbeddingsUnsupportedError
                    at probe-time; falls back gracefully but the user
                    isn't getting vector recall. Includes the provider's
                    own message so the fix hint (e.g. set VOYAGE_API_KEY)
                    surfaces here instead of buried in DEBUG logs.
    """
    try:
        full_cfg = load_config()
    except Exception as e:  # noqa: BLE001
        return ("disabled", f"config read error: {e}")

    if not getattr(full_cfg.memory, "memory_md_retrieval_enabled", False):
        return (
            "disabled",
            "opt-in via memory.memory_md_retrieval_enabled=true",
        )

    # Probe the active provider's embed() with an empty list. By contract
    # this is cheap (most providers short-circuit) and raises
    # EmbeddingsUnsupportedError when the provider can't embed.
    try:
        from opencomputer.cli import _resolve_provider
        from plugin_sdk.embeddings import EmbeddingsUnsupportedError
    except Exception as e:  # noqa: BLE001
        return ("active", f"could not resolve embedding probe: {e}")

    try:
        provider = _resolve_provider(full_cfg.model.provider)
    except Exception as e:  # noqa: BLE001
        return (
            "bm25-only",
            f"active provider {full_cfg.model.provider!r} not resolvable: {e}",
        )

    if provider is None or not hasattr(provider, "embed"):
        return (
            "bm25-only",
            f"active provider {full_cfg.model.provider!r} has no embed() method",
        )

    import asyncio

    async def _probe() -> str:
        try:
            await provider.embed([])
            return ""
        except EmbeddingsUnsupportedError as exc:
            return f"unsupported: {exc}"
        except Exception as exc:  # noqa: BLE001 — surface the real error
            return f"probe failed: {type(exc).__name__}: {exc}"

    try:
        msg = asyncio.run(_probe())
    except RuntimeError:
        # Already in an event loop (rare for `oc memory doctor`); fall
        # back to "active" with a hint.
        return (
            "active",
            "probe skipped (already in event loop) — assume provider supports embed",
        )

    if msg:
        return ("bm25-only", msg)
    return (
        "active",
        f"hybrid BM25+vector via {full_cfg.model.provider!r}.embed()",
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

    # Active Memory (OpenClaw 1.B-alt) — opt-in pre-loop FTS5 + episodic
    # injection. Surface even when disabled so users know the layer
    # exists and can opt in.
    try:
        am_status, am_detail = _doctor_active_memory_row()
    except Exception as e:  # noqa: BLE001
        am_status, am_detail = "disabled", f"error: {e}"
    rows.append(("active_memory", am_status, am_detail))

    # Vector retrieval (M6.3 hybrid BM25+vector RRF) — surfaces silent
    # bm25-only fallback caused by unconfigured embedding providers
    # (e.g., Anthropic without VOYAGE_API_KEY). Prevents the user from
    # thinking hybrid retrieval is active when it has degraded silently.
    try:
        vr_status, vr_detail = _doctor_vector_retrieval_row()
    except Exception as e:  # noqa: BLE001
        vr_status, vr_detail = "bm25-only", f"probe error: {e}"
    rows.append(("vector_retrieval", vr_status, vr_detail))

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
        "bm25-only": "yellow",  # vector retrieval degraded
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


# ─── 2026-05-09 — dream-v2 enable/disable ───────────────────────────


@memory_app.command("dream-v2-on")
def memory_dream_v2_on() -> None:
    """EXPERIMENTAL — enable Dreaming v2 (pure local episodic→MEMORY.md).

    Sets ``memory.dreaming_v2_enabled = True``. Unlike the v1
    ``dream-on`` flow, v2 does NOT register a separate cron job —
    instead it fires inside the system cron tick (see
    ``opencomputer.cron.system_jobs.run_system_tick``) which is
    invoked on every ``oc cron daemon`` tick alongside the four other
    system jobs. So flipping the flag is sufficient: as soon as the
    cron daemon ticks again, v2 will run.

    For the cron daemon itself, see ``oc cron daemon`` (foreground)
    or the LaunchAgent setup at ``oc cron install``.

    Idempotent: re-running with the flag already True is a no-op.
    Distinct from v1 ``dream-on`` — both can coexist (v1 reads
    ``dreaming_enabled``, v2 reads ``dreaming_v2_enabled``); typical
    usage is one or the other.
    """
    from opencomputer.agent.config_store import (
        config_file_path,
        load_config,
        save_config,
        set_value,
    )

    cfg = load_config()
    cfg = set_value(cfg, "memory.dreaming_v2_enabled", True)
    save_config(cfg)

    console.print(
        "[green]✓[/green] dreaming_v2 enabled\n"
        f"[dim]saved to {config_file_path()}[/dim]\n"
        "[dim]v2 fires from the system cron tick — make sure "
        "[cyan]oc cron daemon[/cyan] is running.[/dim]"
    )


@memory_app.command("dream-v2-off")
def memory_dream_v2_off() -> None:
    """EXPERIMENTAL — disable Dreaming v2.

    Only writes the config flag; existing MEMORY.md / DREAMS.md rows
    are left intact. Pairs with :func:`memory_dream_v2_on`.
    """
    from opencomputer.agent.config_store import (
        config_file_path,
        load_config,
        save_config,
        set_value,
    )

    cfg = load_config()
    cfg = set_value(cfg, "memory.dreaming_v2_enabled", False)
    save_config(cfg)

    console.print(
        "[green]✓[/green] dreaming_v2 disabled\n"
        f"[dim]saved to {config_file_path()}[/dim]"
    )


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


# ─── v1.1 plan-3 M6.4 — Dreaming v2 (three-gate consolidation INTO MEMORY.md) ──


@memory_app.command("dream-v2")
def memory_dream_v2(
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Max recent un-dreamed episodic events to score (1-500).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Run even when dreaming_v2 is disabled in config.yaml.",
    ),
    output: str = typer.Option(
        "text", "--output", "-o", help="text|json"
    ),
) -> None:
    """Run one Dreaming v2 pass NOW.

    Three gates (score / recall-count / diversity) decide whether each
    candidate episodic event is promoted into MEMORY.md, held in
    DREAMS.md, or dropped. Default cron cadence runs the same engine
    via the system tick (gated by ``cfg.memory.dreaming_v2_enabled``).
    """
    import asyncio
    import json as _json

    from opencomputer.cron.dreaming_v2_tick import (
        build_production_dependencies,
        run_dreaming_v2_async,
    )

    limit = max(1, min(int(limit), 500))
    deps = build_production_dependencies()

    if not deps.config.enabled and not force:
        console.print(
            "[yellow]dreaming_v2 is disabled[/yellow] "
            "(cfg.memory.dreaming_v2_enabled = false). "
            "Re-run with [cyan]--force[/cyan] to override, or enable in "
            "[cyan]~/.opencomputer/<profile>/config.yaml[/cyan]."
        )
        raise typer.Exit(code=0)

    if force and not deps.config.enabled:
        # Build a one-shot deps with config.enabled forced True so the
        # engine actually runs (engine's own enabled-check otherwise
        # short-circuits).
        from opencomputer.agent.dreaming_v2 import DreamingV2Config

        deps = type(deps)(
            profile_home=deps.profile_home,
            memory=deps.memory,
            db=deps.db,
            provider=deps.provider,
            model=deps.model,
            config=DreamingV2Config(
                enabled=True,
                score_threshold=deps.config.score_threshold,
                min_recall_count=deps.config.min_recall_count,
                diversity_threshold=deps.config.diversity_threshold,
                max_promotions_per_run=deps.config.max_promotions_per_run,
                dreams_md_max_bytes=deps.config.dreams_md_max_bytes,
            ),
        )

    summary = asyncio.run(
        run_dreaming_v2_async(deps=deps, candidate_limit=limit)
    )

    payload = {
        "promoted": [
            {
                "event_id": r.candidate.event_id,
                "score": r.score,
                "recall_count": r.recall_count,
                "diversity": r.diversity_score,
                "rationale": r.rationale,
                "preview": r.candidate.raw_text[:120],
            }
            for r in summary.promoted
        ],
        "held": [
            {
                "event_id": r.candidate.event_id,
                "score": r.score,
                "recall_count": r.recall_count,
                "diversity": r.diversity_score,
                "rationale": r.rationale,
            }
            for r in summary.held
        ],
        "dropped": [
            {
                "event_id": r.candidate.event_id,
                "rationale": r.rationale,
            }
            for r in summary.dropped
        ],
        "skipped_already_processed": summary.skipped_already_processed,
        "total_evaluated": summary.total_evaluated,
        "catch_up_run": summary.catch_up_run,
    }

    if output == "json":
        typer.echo(_json.dumps(payload, indent=2))
        return

    console.print(
        f"[green]✓[/green] dream-v2 finished: "
        f"promoted={len(summary.promoted)}, "
        f"held={len(summary.held)}, "
        f"dropped={len(summary.dropped)}, "
        f"skipped_already_processed={summary.skipped_already_processed}, "
        f"total_evaluated={summary.total_evaluated}"
        + (" (catch-up run)" if summary.catch_up_run else "")
    )
    if summary.promoted:
        console.print("\n[bold green]Promoted to MEMORY.md:[/bold green]")
        for r in summary.promoted:
            console.print(
                f"  • [dim]score={r.score:.2f} recall={r.recall_count} "
                f"div={r.diversity_score:.2f}[/dim] "
                f"{r.candidate.raw_text[:80]}"
            )
    if summary.held:
        console.print("\n[bold yellow]Held in DREAMS.md:[/bold yellow]")
        for r in summary.held:
            console.print(f"  • [dim]{r.rationale}[/dim]")


# ─── Gap 3 from self-evolution-gaps-deep-dive.md ────────────────────────
# DREAMS.md re-scoring with a configurable model — surface entries the
# original Haiku gate undercredited so the operator can decide whether
# the score threshold is correctly conservative, underconfident, or
# miscalibrated.


@memory_app.command("dream-v2-rescore")
def memory_dream_v2_rescore(
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Max DREAMS.md entries to re-score (cost cap; 1-500).",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help=(
            "Override the active provider's model for re-scoring "
            "(e.g. claude-sonnet-4-6). Defaults to the dream-v2 model "
            "configured in config.yaml — pass a stronger model here to "
            "surface miscalibration of the original Haiku gate."
        ),
    ),
    promote_threshold: float = typer.Option(
        0.75,
        "--promote-threshold",
        help=(
            "Rescore ≥ this value flags the entry as a promotion candidate. "
            "Default 0.75 sits clearly above the score gate's default 0.65 "
            "so only meaningful improvements surface."
        ),
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Write promotion candidates into MEMORY.md (atomic batch via "
            "MemoryManager). Default off — first run shows the diff, "
            "second run with --apply persists."
        ),
    ),
    output: str = typer.Option(
        "text", "--output", "-o", help="text|json"
    ),
) -> None:
    """Re-score DREAMS.md entries with a different (typically stronger) model.

    The deep-dive doc's diagnostic move (Gap 3): the score gate is
    Haiku-by-default. If the gate is undercredited on technical content,
    DREAMS.md piles up with entries that "should" have promoted. This
    command re-runs each entry through a configurable model and reports
    where the new score disagrees with the old gate.

    With ``--apply``, entries that clear ``--promote-threshold`` and have
    a recoverable Q/A structure are appended to MEMORY.md in a single
    atomic batch (MemoryManager owns the flock + .bak rotation).

    NOTE: Re-scored entries are NOT removed from DREAMS.md — the file
    is the dreaming-v2 holding pen and the cron tick manages eviction
    via its byte-cap. Removing here would create racing-update hazards
    with the cron tick.
    """
    import asyncio
    import json as _json

    from opencomputer.agent.config_store import load_config
    from opencomputer.agent.dreams_rescore import (
        parse_dreams_md,
        render_promotion_candidates,
        rescore_entries,
    )
    from opencomputer.cron.dreaming_v2_tick import (
        _build_score_fn_from_provider,
        build_production_dependencies,
    )

    limit = max(1, min(int(limit), 500))
    promote_threshold = max(0.0, min(1.0, float(promote_threshold)))

    cfg = load_config()
    dreams_path = Path(cfg.memory.declarative_path).parent / "DREAMS.md"
    if not dreams_path.exists():
        console.print(
            f"[yellow]No DREAMS.md found at[/yellow] [cyan]{dreams_path}[/cyan]. "
            "Run [cyan]oc memory dream-v2[/cyan] first to populate the holding pen."
        )
        raise typer.Exit(code=0)

    raw = dreams_path.read_text(encoding="utf-8", errors="replace")
    entries = parse_dreams_md(raw, max_entries=limit)
    if not entries:
        console.print(
            f"[dim]Parsed 0 entries from DREAMS.md ({dreams_path}) — nothing to re-score.[/dim]"
        )
        raise typer.Exit(code=0)

    deps = build_production_dependencies()
    if deps.provider is None:
        console.print(
            "[red]Cannot rescore:[/red] no provider plugin is installed. "
            "The score function needs an LLM to call.\n\n"
            "  → Run [cyan]oc auth[/cyan] to see provider options, then install one "
            "([cyan]anthropic-provider[/cyan] / [cyan]openai-provider[/cyan] / etc.) "
            "and set [cyan]model.provider[/cyan] in [cyan]~/.opencomputer/<profile>/config.yaml[/cyan]."
        )
        raise typer.Exit(code=2)
    chosen_model = model or deps.model
    score_fn = _build_score_fn_from_provider(deps.provider, model=chosen_model)

    console.print(
        f"[dim]Re-scoring[/dim] [cyan]{len(entries)}[/cyan] "
        f"[dim]entries with model[/dim] [cyan]{chosen_model}[/cyan]"
        f" [dim](promote threshold {promote_threshold:.2f}, "
        f"apply={apply})[/dim]"
    )

    outcomes = asyncio.run(
        rescore_entries(
            entries,
            score_fn=score_fn,
            promote_threshold=promote_threshold,
        )
    )

    if output == "json":
        typer.echo(
            _json.dumps(
                {
                    "model": chosen_model,
                    "promote_threshold": promote_threshold,
                    "applied": False,
                    "outcomes": [
                        {
                            "date": o.entry.date,
                            "tools": list(o.entry.tools),
                            "question": o.entry.question,
                            "answer": o.entry.answer,
                            "new_score": o.new_score,
                            "promoted_candidate": o.promoted_candidate,
                            "error": o.error,
                        }
                        for o in outcomes
                    ],
                },
                indent=2,
            )
        )
        return

    diff_table = Table(title=f"DREAMS.md rescore (model={chosen_model})")
    diff_table.add_column("date", style="dim")
    diff_table.add_column("Q (preview)")
    diff_table.add_column("new score", justify="right")
    diff_table.add_column("flag", style="bold")

    successes = 0
    errors = 0
    promotion_count = 0
    for o in outcomes:
        if o.error:
            errors += 1
            flag = f"[red]ERR[/red] [dim]{o.error[:30]}[/dim]"
        elif o.promoted_candidate:
            successes += 1
            promotion_count += 1
            flag = "[green]promote[/green]"
        else:
            successes += 1
            flag = "[dim]hold[/dim]"
        score_str = f"{o.new_score:.2f}"
        if o.new_score >= promote_threshold and not o.error:
            score_str = f"[green]{score_str}[/green]"
        diff_table.add_row(
            o.entry.date or "—",
            o.display_question,
            score_str,
            flag,
        )

    console.print(diff_table)
    console.print(
        f"\n[bold]Summary:[/bold] {successes}/{len(outcomes)} scored, "
        f"{errors} errors, "
        f"[green]{promotion_count}[/green] promotion candidate(s) at threshold {promote_threshold:.2f}"
    )

    if not apply:
        if promotion_count:
            console.print(
                f"\n[dim]Re-run with[/dim] [cyan]--apply[/cyan] [dim]to write "
                f"{promotion_count} entr{'y' if promotion_count == 1 else 'ies'} "
                f"into MEMORY.md.[/dim]"
            )
        return

    promotion_lines = render_promotion_candidates(outcomes)
    if not promotion_lines:
        console.print(
            "[dim]No promotion candidates after rescore — MEMORY.md not modified.[/dim]"
        )
        return

    # Atomic append via MemoryManager — owns cap-checking + .bak
    # rotation + char-limit enforcement + index invalidation. Single
    # append (not per-line) so the batch lands as one atomic write.
    mgr = _manager()
    block = "\n".join(promotion_lines) + "\n"
    mgr.append_declarative(block)
    console.print(
        f"\n[green]✓[/green] Promoted {len(promotion_lines)} entr"
        f"{'y' if len(promotion_lines) == 1 else 'ies'} into MEMORY.md "
        f"([cyan]{mgr.declarative_path}[/cyan])."
    )
