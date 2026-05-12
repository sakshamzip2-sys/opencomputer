"""Implementations of ``opencomputer evolution …`` subcommands.

Provider resolution note (B2):
  ``_resolve_provider()`` queries the module-level ``registry`` singleton from
  ``opencomputer.plugins.registry``, which exposes a ``providers`` dict keyed
  by provider name.  This mirrors the pattern used by ``cli._resolve_provider``
  (which also calls ``plugin_registry.providers.get(provider_name)``).  We do
  NOT import from ``opencomputer.cli`` because that file is Session A's
  reserved file; instead we look up the same global registry object directly.
  If the registry has no providers loaded (typical in a freshly spawned CLI
  with no provider plugin enabled), we raise a ``RuntimeError`` with an
  actionable message.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.evolution.entrypoint import evolution_app
from opencomputer.evolution.reflect import ReflectionEngine
from opencomputer.evolution.storage import (
    evolution_home,
    init_db,
    list_recent,
    record_reflection,
    record_skill_invocation,
)
from opencomputer.evolution.synthesize import SkillSynthesizer

logger = logging.getLogger("opencomputer.evolution.cli")
console = Console()


# ---------------------------------------------------------------------------
# skills sub-group
# ---------------------------------------------------------------------------

skills_app = typer.Typer(
    name="skills",
    help="Manage synthesized skills (the evolution quarantine namespace).",
    no_args_is_help=True,
)
evolution_app.add_typer(skills_app, name="skills")


@skills_app.command("list")
def skills_list() -> None:
    """Show synthesized skills currently in the evolution quarantine."""
    skills_dir = evolution_home() / "skills"
    if not skills_dir.exists():
        console.print("[dim]No synthesized skills yet.[/dim]")
        return
    rows = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        # Read description from frontmatter (first lines, name: / description:)
        description = ""
        for line in skill_md.read_text(encoding="utf-8").splitlines():
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
                break
            if line.startswith("---") and rows:  # second --- = end of frontmatter
                break
        rows.append((child.name, description))
    if not rows:
        console.print("[dim]No synthesized skills yet.[/dim]")
        return
    table = Table(title="Synthesized skills (evolution quarantine)")
    table.add_column("slug", style="cyan")
    table.add_column("description")
    for slug, desc in rows:
        table.add_row(slug, desc)
    console.print(table)


@skills_app.command("promote")
def skills_promote(
    slug: str = typer.Argument(..., help="Slug of synthesized skill to promote"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing main-skills entry"),
) -> None:
    """Copy a synthesized skill from the evolution quarantine to the user's main skills dir."""
    src = evolution_home() / "skills" / slug
    if not src.exists():
        console.print(f"[red]Synthesized skill not found:[/red] {src}")
        raise typer.Exit(code=1)
    # Main skills dir per existing convention (_home() / "skills") — see agent/config.py
    from opencomputer.agent.config import (
        _home as _profile_home,  # local import to avoid load order issues
    )

    main_dir = _profile_home() / "skills" / slug
    if main_dir.exists() and not force:
        console.print(
            f"[red]Main skill already exists:[/red] {main_dir}\n"
            "[dim]Use --force to overwrite.[/dim]"
        )
        raise typer.Exit(code=1)
    if main_dir.exists() and force:
        shutil.rmtree(main_dir)
    main_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, main_dir)
    # Record an invocation so the promoted skill starts with non-atrophied state.
    # init_db() ensures migrations (including B4 tables) are applied before writing.
    conn = init_db()
    try:
        record_skill_invocation(slug, source="cli_promote", conn=conn)
    finally:
        conn.close()
    console.print(f"[green]Promoted[/green] {slug} → {main_dir}")


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@evolution_app.command("reflect")
def reflect(
    window: int = typer.Option(
        30,
        "--window",
        help="Number of recent trajectories to reflect on (default 30)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Render the prompt + show counts without calling the LLM or synthesizing",
    ),
    model: str = typer.Option(
        "claude-opus-4-7",
        "--model",
        help="Model to use for reflection (provider must be configured)",
    ),
) -> None:
    """Manually trigger a reflection pass on recent trajectories."""
    conn = init_db()
    records = list_recent(limit=window, conn=conn)
    if not records:
        console.print("[dim]No trajectories to reflect on. Auto-collection lands in B3.[/dim]")
        return
    console.print(
        f"Reflecting on {len(records)} trajectories (window={window}, model={model})..."
    )
    if dry_run:
        # Show summary; do NOT call provider
        table = Table(title="Trajectories to reflect on")
        table.add_column("id")
        table.add_column("session_id")
        table.add_column("events")
        table.add_column("completion")
        for r in records:
            table.add_row(
                str(r.id),
                r.session_id,
                str(len(r.events)),
                "✓" if r.completion_flag else "✗",
            )
        console.print(table)
        console.print("[yellow]Dry-run: no LLM call made.[/yellow]")
        return
    # Real reflection requires a provider. For B2, raise an actionable error if none.
    try:
        provider = _resolve_provider()
    except RuntimeError as exc:
        console.print(f"[red]Cannot resolve provider:[/red] {exc}")
        raise typer.Exit(code=2)
    engine = ReflectionEngine(provider=provider, model=model, window=window)
    insights = engine.reflect(records)
    console.print(f"[green]Got {len(insights)} insights.[/green]")

    # Persist a reflection row for dashboard / audit trail.
    # cache_hit detection requires engine introspection out of scope for B4 —
    # always False here; a future pass can wire through engine._cache state.
    import hashlib as _hashlib
    _ids_str = ",".join(str(r.id) for r in records if r.id is not None)
    _records_hash = _hashlib.sha256(_ids_str.encode()).hexdigest()
    record_reflection(
        window_size=window,
        records_count=len(records),
        insights_count=len(insights),
        records_hash=_records_hash,
        cache_hit=False,
    )

    synth = SkillSynthesizer()
    created = []
    for ins in insights:
        if ins.action_type == "create_skill":
            try:
                path = synth.synthesize(ins)
                created.append(path)
                console.print(f"  [cyan]synthesized[/cyan] {path}")
            except (ValueError, FileExistsError) as exc:
                kind = type(exc).__name__
                console.print(f"  [yellow]skipped insight ({kind}):[/yellow] {exc}")
    console.print(f"[bold]Synthesized {len(created)} skills.[/bold]")


@evolution_app.command("reset")
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete all evolution data: DB + synthesized skills + (future) prompt proposals.

    Your sessions DB and main skills are NOT touched.
    """
    # Compute path WITHOUT calling evolution_home() so we don't create the dir
    # just to check if it exists (evolution_home() has a mkdir side-effect).
    from opencomputer.agent.config import _home as _profile_home

    eh = _profile_home() / "evolution"
    if not eh.exists():
        console.print("[dim]No evolution data to delete.[/dim]")
        return
    if not yes:
        confirm = typer.confirm(f"Delete entire evolution dir at {eh}?")
        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(code=0)
    shutil.rmtree(eh)
    console.print(f"[green]Deleted[/green] {eh}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_provider():
    """Return a BaseProvider instance, or raise RuntimeError with actionable message.

    Provider resolution strategy (B2 MVP):
      We query the module-level ``registry`` singleton from
      ``opencomputer.plugins.registry``, which holds a ``providers`` dict
      keyed by provider-name strings (populated by ``registry.load_all()``
      at CLI startup).  We return the first registered provider.

      This mirrors the exact approach used by ``opencomputer.cli._resolve_provider``
      — which also calls ``plugin_registry.providers.get(provider_name)`` — but
      adapted for the evolution CLI which (a) doesn't know the configured
      provider name at import time, and (b) must not import from cli.py.

      If the registry is empty (provider plugins not loaded), we raise a clear
      error so the user knows what to do.
    """
    # Local import — keeps the CLI surface independent of plugin-registry load order.
    try:
        from opencomputer.plugins.registry import registry  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "plugin registry not importable — ensure opencomputer is installed correctly"
        ) from exc

    providers = registry.providers  # dict[str, BaseProvider | type[BaseProvider]]
    if not providers:
        raise RuntimeError(
            "No provider plugin enabled. "
            "Run `opencomputer plugin enable anthropic-provider` "
            "(or another provider) first."
        )
    # First provider wins for B2 MVP — user can configure preference later.
    _first = next(iter(providers.values()))
    # Plugins may register the class OR an instance; handle both.
    return _first() if isinstance(_first, type) else _first


# ---------------------------------------------------------------------------
# prompts sub-group
# ---------------------------------------------------------------------------

prompts_app = typer.Typer(
    name="prompts",
    help="Review and decide on prompt-evolution proposals (never auto-applied).",
    no_args_is_help=True,
)
evolution_app.add_typer(prompts_app, name="prompts")


@prompts_app.command("list")
def prompts_list(
    status: str = typer.Option(
        "pending",
        "--status",
        help="Filter by status: pending|applied|rejected|all",
    ),
) -> None:
    """List prompt proposals (default: pending)."""
    from opencomputer.evolution.prompt_evolution import PromptEvolver

    pe = PromptEvolver()
    proposals = (
        pe.list_all()
        if status == "all"
        else [p for p in pe.list_all() if p.status == status]
    )
    if not proposals:
        console.print(f"[dim]No prompt proposals with status={status}.[/dim]")
        return
    table = Table(title=f"Prompt proposals ({status})")
    table.add_column("id", style="cyan")
    table.add_column("target")
    table.add_column("status")
    table.add_column("cache?")
    table.add_column("diff_hint", overflow="fold")
    for p in proposals:
        cache_cell = "[red]CACHE INVALIDATES[/red]" if p.cache_invalidation_warning else ""
        table.add_row(str(p.id), p.target, p.status, cache_cell, p.diff_hint[:120])
    console.print(table)


@prompts_app.command("apply")
def prompts_apply(
    proposal_id: int = typer.Argument(...),
    reason: str = typer.Option("", "--reason"),
    force_cache_invalidation: bool = typer.Option(
        False,
        "--force-cache-invalidation",
        help="Apply even if the proposal flagged a cache-invalidation warning",
    ),
) -> None:
    """Mark a prompt proposal as applied. The actual prompt-file edit is your responsibility —
    this command persists the decision only.
    """
    from opencomputer.evolution.prompt_evolution import PromptEvolver

    pe = PromptEvolver()
    try:
        proposal = pe.get(proposal_id)
    except KeyError:
        console.print(f"[red]No proposal with id={proposal_id}[/red]")
        raise typer.Exit(code=1)
    if proposal.cache_invalidation_warning and not force_cache_invalidation:
        confirm = typer.confirm(
            "Proposal flagged: applying it mid-session will invalidate the "
            "Anthropic prompt cache (≈3x cost spike for the rest of the session). "
            "Apply anyway?",
        )
        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(code=0)
    p = pe.apply(proposal_id, reason=reason)
    console.print(f"[green]Marked proposal {p.id} as applied.[/green]")


@prompts_app.command("reject")
def prompts_reject(
    proposal_id: int = typer.Argument(...),
    reason: str = typer.Option("", "--reason"),
) -> None:
    """Mark a prompt proposal as rejected."""
    from opencomputer.evolution.prompt_evolution import PromptEvolver

    pe = PromptEvolver()
    try:
        p = pe.reject(proposal_id, reason=reason)
    except KeyError:
        console.print(f"[red]No proposal with id={proposal_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[yellow]Rejected proposal {p.id}.[/yellow]")


# ---------------------------------------------------------------------------
# dashboard command
# ---------------------------------------------------------------------------


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "[dim]never[/dim]"
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_float(v: float | None) -> str:
    return "[dim]n/a[/dim]" if v is None else f"{v:.3f}"


@evolution_app.command("dashboard")
def dashboard(
    atrophy_days: int = typer.Option(
        60,
        "--atrophy-days",
        help="Days of inactivity before a skill counts as atrophied",
    ),
) -> None:
    """Show the evolution monitoring dashboard."""
    from opencomputer.evolution.monitor import MonitorDashboard

    snap = MonitorDashboard(atrophy_days=atrophy_days).snapshot()
    summary = Table(title="Evolution dashboard")
    summary.add_column("metric")
    summary.add_column("value")
    summary.add_row("total reflections", str(snap.total_reflections))
    summary.add_row("last reflection", _fmt_ts(snap.last_reflection_at))
    summary.add_row(
        "synthesized skills",
        f"{len(snap.synthesized_skills)} ({snap.atrophied_count} atrophied)",
    )
    summary.add_row("avg reward (30d)", _fmt_float(snap.avg_reward_last_30))
    summary.add_row("avg reward (lifetime)", _fmt_float(snap.avg_reward_lifetime))
    console.print(summary)

    if snap.synthesized_skills:
        skills_table = Table(title="Skills")
        skills_table.add_column("slug")
        skills_table.add_column("invocations")
        skills_table.add_column("last")
        skills_table.add_column("status")
        for s in snap.synthesized_skills:
            skills_table.add_row(
                s.slug,
                str(s.invocation_count),
                _fmt_ts(s.last_invoked_at),
                "[red]atrophied[/red]" if s.is_atrophied else "[green]active[/green]",
            )
        console.print(skills_table)

    # ── Operational section ─────────────────────────────────────────
    # Aggregates four cheap on-disk signals into one table so the
    # operator can see "what's actually firing" without grepping the
    # codebase. Every row degrades gracefully — a missing file shows
    # "[dim]—[/dim]" not a stack trace. This is the M2 delivery for the
    # senior-engineer-workflow pass on self-evolution-comparison.md.
    op_rows = _collect_operational_rows()
    op_table = Table(title="Operational")
    op_table.add_column("signal")
    op_table.add_column("value")
    for label, value in op_rows:
        op_table.add_row(label, value)
    console.print(op_table)


# ── Operational dashboard helpers ───────────────────────────────────
#
# Read-only diagnostics surfaced by ``oc evolution dashboard``. Each
# helper:
#   1. Validates input shape before consuming it (state files can be
#      adversarial — corrupted, list-instead-of-dict, type-shifted).
#   2. Logs at WARNING with stack info on unexpected failures so
#      ``oc doctor`` and the journal capture them.
#   3. Returns a renderable string; NEVER raises into the dashboard
#      command. Three-tier swallow per CLAUDE.md §7 #10.
#   4. Treats ``_profile_home()`` as the only privileged operation
#      (it owns env-var resolution + workspace overlay). All other
#      paths derive from it.
#
# The trust boundary is the filesystem: any file under ``_profile_home()``
# may be malformed, missing, a directory where a file is expected, a
# symlink loop, or unreadable due to permissions. Each helper is hardened
# against all of those.

_DASHBOARD_FALLBACK = "[dim]—[/dim]"
_HEARTBEAT_ACTIVE_SECS = 3600  # 1h — fresh subscriber
_HEARTBEAT_IDLE_SECS = 86400  # 24h — still alive but quiet


def _collect_operational_rows() -> list[tuple[str, str]]:
    """Build the Operational table rows. Each helper is fail-soft."""
    return [
        ("skill-evolution", _skill_evolution_status()),
        ("proposed candidates", _proposed_count()),
        ("dreaming-v2 last run", _dreaming_v2_status()),
        ("DREAMS.md", _dreams_md_status()),
    ]


def _profile_home() -> Path:
    """Resolve the active profile home.

    Mirrors the cron / memory-tick resolution path so the dashboard
    points at the same disk surface the engine writes to. May raise
    ``RuntimeError`` if env / config resolution fails entirely; callers
    must handle that case.
    """
    from opencomputer.agent.config import _home

    return _home()


def _try_profile_home() -> Path | None:
    """Variant that swallows resolution errors with a WARNING log.

    Returns ``None`` so callers can render a fallback row instead of
    crashing the whole table.
    """
    try:
        return _profile_home()
    except Exception:  # noqa: BLE001 — last-line defence; log+null
        logger.warning(
            "evolution dashboard: failed to resolve profile_home",
            exc_info=True,
        )
        return None


def _skill_evolution_status() -> str:
    """Heartbeat freshness.

    States:
      - file absent → "—" (subscriber never fired)
      - mtime in the future → "—" (clock skew or tampered timestamp)
      - <1h old → green "active"
      - <24h old → yellow "idle"
      - otherwise → red "stale"

    Adversarial-input handling: if the heartbeat path is a directory
    or symlink loop, ``stat`` raises ``OSError``; we log + fall back.
    """
    import time

    home = _try_profile_home()
    if home is None:
        return f"{_DASHBOARD_FALLBACK} (profile unresolved)"
    try:
        hb = home / "skills" / "evolution_heartbeat"
        if not hb.exists():
            return f"{_DASHBOARD_FALLBACK} (no heartbeat)"
        st = hb.stat()  # follows symlinks; raises on loop
        age_s = time.time() - st.st_mtime
        if age_s < 0:
            logger.warning(
                "evolution dashboard: heartbeat mtime is in the future "
                "(skew=%.1fs); reporting as stale",
                -age_s,
            )
            return f"{_DASHBOARD_FALLBACK} (clock skew)"
        if age_s < _HEARTBEAT_ACTIVE_SECS:
            return f"[green]active[/green] ({int(age_s) // 60}m ago)"
        if age_s < _HEARTBEAT_IDLE_SECS:
            return f"[yellow]idle[/yellow] ({int(age_s // 3600)}h ago)"
        return f"[red]stale[/red] ({int(age_s // 86400)}d ago)"
    except OSError:
        logger.warning(
            "evolution dashboard: heartbeat stat() failed", exc_info=True
        )
        return f"{_DASHBOARD_FALLBACK} (unreadable)"


def _proposed_count() -> str:
    """Count candidate dirs in <profile>/skills/_proposed/.

    Hardened against:
      - dir absent (fresh install) → "0 (no candidates staged)"
      - _proposed/ is a regular file (someone touched the wrong path)
        → "—" with WARNING log; do not iter on a file
      - permission denied → "—" with WARNING; do not crash
      - very large dir (10k+ entries) → cap at 999 to bound render size
    """
    home = _try_profile_home()
    if home is None:
        return f"{_DASHBOARD_FALLBACK} (profile unresolved)"
    try:
        proposed = home / "skills" / "_proposed"
        if not proposed.exists():
            return "0 [dim](no candidates staged)[/dim]"
        if not proposed.is_dir():
            logger.warning(
                "evolution dashboard: _proposed path exists but is not "
                "a directory: %s",
                proposed,
            )
            return f"{_DASHBOARD_FALLBACK} (path collision — _proposed is not a dir)"
        n = 0
        for p in proposed.iterdir():
            try:
                if p.is_dir():
                    n += 1
            except OSError:
                continue  # broken symlink — skip silently per-entry
            if n >= 999:
                break
        if n == 0:
            return "0 [dim](no candidates staged)[/dim]"
        return f"{n} [yellow](run `oc skills review`)[/yellow]"
    except OSError:
        logger.warning(
            "evolution dashboard: failed to list _proposed dir",
            exc_info=True,
        )
        return f"{_DASHBOARD_FALLBACK} (unreadable)"


def _dreaming_v2_status() -> str:
    """Read last_summary from dreaming-v2 state.json (M3 audit fallback).

    Disjoint HELD-bucket counts (``score_only`` / ``recall_only`` /
    ``both_gates``) satisfy ``held == score_only + recall_only +
    both_gates`` so the rendered breakdown is sum-consistent. Falls
    back to the legacy non-disjoint keys (``score_fail`` / ``recall_fail``)
    when reading a state file written by an older build.

    Adversarial-input handling:
      - state file absent → "(never run — `oc memory dream-v2-now` to seed)"
      - state file is unreadable → "—" with WARN
      - state file is valid JSON but a list (not dict) → "—" with WARN
      - last_summary is a string / number / list → fall back to "ran"
      - integer fields are strings/floats → coerced via int() with try
      - missing keys → defaulted to 0
    """
    import json

    home = _try_profile_home()
    if home is None:
        return f"{_DASHBOARD_FALLBACK} (profile unresolved)"
    state_path = home / "cron" / "dreaming_v2_state.json"
    try:
        if not state_path.exists():
            return f"{_DASHBOARD_FALLBACK} (never run — run `oc memory dream-v2-now` to seed)"
        raw = state_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning(
            "evolution dashboard: state file unreadable at %s",
            state_path,
            exc_info=True,
        )
        return f"{_DASHBOARD_FALLBACK} (state unreadable)"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "evolution dashboard: state.json has invalid JSON at %s",
            state_path,
        )
        return f"{_DASHBOARD_FALLBACK} (state malformed)"

    if not isinstance(data, dict):
        logger.warning(
            "evolution dashboard: state.json is %s, expected dict",
            type(data).__name__,
        )
        return f"{_DASHBOARD_FALLBACK} (state shape unexpected: {type(data).__name__})"

    ls = data.get("last_summary")
    if ls is None:
        return "[dim]ran[/dim] (no per-run summary — run `oc memory dream-v2-now` to populate)"
    if not isinstance(ls, dict):
        logger.warning(
            "evolution dashboard: last_summary is %s, expected dict",
            type(ls).__name__,
        )
        return f"{_DASHBOARD_FALLBACK} (last_summary shape unexpected)"

    promoted = _safe_int(ls.get("promoted"))
    held = _safe_int(ls.get("held"))
    dropped = _safe_int(ls.get("dropped"))
    if "score_only" in ls or "recall_only" in ls or "both_gates" in ls:
        so = _safe_int(ls.get("score_only"))
        ro = _safe_int(ls.get("recall_only"))
        bg = _safe_int(ls.get("both_gates"))
        breakdown = f"score-only={so}, recall-only={ro}, both={bg}"
    else:
        sf = _safe_int(ls.get("score_fail"))
        rf = _safe_int(ls.get("recall_fail"))
        breakdown = f"score-fail={sf}, recall-fail={rf}"
    df = _safe_int(ls.get("diversity_fail"), default=dropped)
    return (
        f"promoted={promoted}, held={held}, dropped={dropped} "
        f"[dim]({breakdown}, diversity-fail={df})[/dim]"
    )


def _safe_int(value: object, *, default: int = 0) -> int:
    """Best-effort int coercion that never raises.

    Accepts None, numeric strings, floats; falls through to ``default``
    for any other shape. Used to render counts from state files that
    may have been hand-edited or written by an older schema.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(float(value))
            except ValueError:
                return default
    return default


def _dreams_md_status() -> str:
    """Size vs cap — surfaces the "rotating noise" condition.

    Reads the *active* user config (``load_config()``) rather than the
    ship default, so users who've overridden ``dreaming_v2_dreams_md_max_bytes``
    see their real cap. Falls back to the ship default on any load error
    so the dashboard never blocks on a malformed config.

    Adversarial-input handling:
      - file absent (fresh install) → "—"
      - file is a directory or symlink loop → "—" with WARN
      - config load fails → fall back to ship default cap
      - cap is non-numeric / negative → fall back to ship default
    """
    home = _try_profile_home()
    if home is None:
        return f"{_DASHBOARD_FALLBACK} (profile unresolved)"

    cap = _load_dreams_cap()
    try:
        path = home / "DREAMS.md"
        if not path.exists():
            return f"{_DASHBOARD_FALLBACK} (no file)"
        if not path.is_file():
            logger.warning(
                "evolution dashboard: DREAMS.md exists but is not a file (path=%s)",
                path,
            )
            return f"{_DASHBOARD_FALLBACK} (DREAMS.md is not a file)"
        size = path.stat().st_size
    except OSError:
        logger.warning(
            "evolution dashboard: DREAMS.md stat failed", exc_info=True
        )
        return f"{_DASHBOARD_FALLBACK} (unreadable)"

    pct = (size / cap * 100) if cap > 0 else 0
    bar = "[red]" if pct >= 95 else "[yellow]" if pct >= 75 else "[green]"
    return f"{bar}{size}[/]/{cap} bytes ({pct:.0f}% of cap)"


def _load_dreams_cap(default: int = 16384) -> int:
    """Resolve DREAMS.md byte cap from active config; clean fallback."""
    try:
        from opencomputer.agent.config_store import load_config

        cfg = load_config()
        cap = _safe_int(
            getattr(cfg.memory, "dreaming_v2_dreams_md_max_bytes", default),
            default=default,
        )
        if cap > 0:
            return cap
    except Exception:  # noqa: BLE001
        logger.warning(
            "evolution dashboard: load_config failed; falling back to "
            "ship-default DREAMS cap (%d)",
            default,
            exc_info=True,
        )
    try:
        from opencomputer.agent.config import default_config

        return _safe_int(
            getattr(
                default_config().memory,
                "dreaming_v2_dreams_md_max_bytes",
                default,
            ),
            default=default,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "evolution dashboard: default_config failed; using hard-coded %d",
            default,
            exc_info=True,
        )
        return default


# ---------------------------------------------------------------------------
# skills retire + skills record-invocation
# ---------------------------------------------------------------------------


@skills_app.command("retire")
def skills_retire(
    slug: str = typer.Argument(..., help="Slug of synthesized skill to retire"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Move a synthesized skill from quarantine to <evolution_home>/retired/<slug>/.

    Audit trail preserved; skill no longer shows in `skills list`.
    """
    src = evolution_home() / "skills" / slug
    if not src.exists():
        console.print(f"[red]Skill not found:[/red] {src}")
        raise typer.Exit(code=1)
    if not yes and not typer.confirm(f"Retire {slug}? It will be moved to retired/{slug}/."):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit(code=0)
    retired_dir = evolution_home() / "retired"
    retired_dir.mkdir(parents=True, exist_ok=True)
    target = retired_dir / slug
    if target.exists():
        # Already a previous retirement — resolve collision (slug-2, slug-3, …)
        n = 2
        while (retired_dir / f"{slug}-{n}").exists():
            n += 1
        target = retired_dir / f"{slug}-{n}"
    src.rename(target)
    console.print(f"[yellow]Retired[/yellow] {slug} → {target}")


@skills_app.command("record-invocation")
def skills_record_invocation(
    slug: str = typer.Argument(...),
    source: str = typer.Option(
        "manual",
        "--source",
        help="manual | agent_loop | cli_promote",
    ),
) -> None:
    """Manually record that a synthesized skill was invoked (atrophy data).

    Manual analog for B5 auto-recording; lets you inject invocation data
    outside of the agent loop.
    """
    rec_id = record_skill_invocation(slug, source=source)
    console.print(
        f"[dim]recorded invocation {rec_id} for[/dim] [cyan]{slug}[/cyan]"
    )


# --- trajectories group (B3) ---

trajectories_app = typer.Typer(
    name="trajectories",
    help="View captured agent-loop trajectories (B3 auto-collection).",
    no_args_is_help=True,
)
evolution_app.add_typer(trajectories_app, name="trajectories")


@trajectories_app.command("show")
def trajectories_show(
    limit: int = typer.Option(50, "--limit", help="Number of recent trajectories to show"),
) -> None:
    """List recent captured trajectories with their event counts and rewards."""
    conn = init_db()
    records = list_recent(limit=limit, conn=conn)
    if not records:
        console.print(
            "[dim]No trajectories captured yet. Enable collection with `opencomputer evolution enable`.[/dim]"
        )
        return
    table = Table(title=f"Recent trajectories (last {len(records)})")
    table.add_column("id", style="cyan")
    table.add_column("session_id")
    table.add_column("events")
    table.add_column("started")
    table.add_column("completed")
    for r in records:
        table.add_row(
            str(r.id),
            r.session_id[:24] + ("..." if len(r.session_id) > 24 else ""),
            str(len(r.events)),
            _fmt_ts(r.started_at),
            "✓" if r.completion_flag else "✗",
        )
    console.print(table)


@evolution_app.command("enable")
def enable() -> None:
    """Turn on auto-collection of trajectories (subscribes to the F2 bus on next startup)."""
    from opencomputer.evolution.trajectory import register_with_bus, set_collection_enabled

    set_collection_enabled(True)
    # Also register immediately for the current process (so the change is observed without restart)
    register_with_bus()
    console.print(
        "[green]Evolution auto-collection enabled.[/green] "
        "Restart any running agent to pick up the change globally."
    )


@evolution_app.command("disable")
def disable() -> None:
    """Turn off auto-collection. Existing trajectories remain stored."""
    from opencomputer.evolution.trajectory import set_collection_enabled

    set_collection_enabled(False)
    console.print(
        "[yellow]Evolution auto-collection disabled.[/yellow] "
        "Existing trajectories remain. Run `opencomputer evolution reset --yes` to wipe them."
    )


# ---------------------------------------------------------------------------
# export-trajectory (P-14)
# ---------------------------------------------------------------------------


@evolution_app.command("export-trajectory")
def export_trajectory(
    session_id: str = typer.Argument(..., help="Session id to export"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Output ZIP path. Defaults to "
            "<profile_home>/trajectory_exports/<session_id>_<unix_ts>.zip"
        ),
    ),
    max_bundle_size: int = typer.Option(
        50,
        "--max-bundle-size",
        help="Soft cap (MB) per ZIP file; bigger trajectories split into _part2.zip, …",
    ),
) -> None:
    """Bundle a session's trajectory records into a redacted ZIP.

    Bundle contents (per ZIP):
      manifest.json   — session_id, schema_version, per-record summary, part info
      events.jsonl    — one TrajectoryEvent per line, JSON-serialised, redaction-applied
      redaction.json  — per-pattern hit counts (NO raw matches)

    Five regex patterns are applied to short string-valued metadata fields
    (the schema-level 200-char privacy rule already prevents large bodies
    from being stored): API keys, ``/Users/<name>/`` paths, emails, IPs,
    Bearer tokens.
    """
    # Local import — keeps CLI startup cheap when this command isn't used.
    from opencomputer.evolution.export import bundle

    try:
        paths = bundle(
            session_id,
            output_path=output,
            max_bundle_size_mb=max_bundle_size,
        )
    except ValueError as exc:
        console.print(f"[red]Cannot export trajectory:[/red] {exc}")
        raise typer.Exit(code=2)

    if len(paths) == 1:
        console.print(f"[green]Exported[/green] → {paths[0]}")
    else:
        console.print(
            f"[green]Exported {len(paths)} parts (split on "
            f"--max-bundle-size={max_bundle_size}MB):[/green]"
        )
        for p in paths:
            console.print(f"  • {p}")


__all__ = [
    "skills_list",
    "skills_promote",
    "skills_retire",
    "skills_record_invocation",
    "reflect",
    "reset",
    "prompts_list",
    "prompts_apply",
    "prompts_reject",
    "dashboard",
    "trajectories_show",
    "enable",
    "disable",
    "export_trajectory",
]
