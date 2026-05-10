"""``opencomputer context`` — context-window inspection per session.

CC §4 visibility surface — closes the "no historical session inspection"
gap. Mirrors the in-chat ``/context`` slash command but operates on
arbitrary session ids stored in :class:`SessionDB`, not the in-flight
``runtime.custom`` keys.

Subcommands:

  - ``oc context show <session-id>``   — render panel for one session
  - ``oc context show --current``      — render for the most-recent session

Storage: reads from ``<profile_home>/sessions.db`` via
:meth:`SessionDB.session_usage_summary`. The session row carries
cumulative input/output tokens, cache reads/writes, and the v18
``compactions_count`` column. The compaction trigger threshold and
context-window resolution share semantics with the slash command for
consistency.

Spec:
    docs/superpowers/specs/2026-05-10-cc-usage-context-visibility-design.md §4.7.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer
from rich.console import Console

_LOG = logging.getLogger(__name__)

context_app = typer.Typer(
    name="context",
    help="Context-window inspection per session.",
    no_args_is_help=True,
)
_console = Console()

#: Compaction trigger threshold — same value as the in-chat
#: ``/context`` command so users see consistent numbers across surfaces.
_COMPACTION_TRIGGER_PCT: float = 0.98


def _resolve_sessions_db_path() -> Path:
    """Locate the active profile's ``sessions.db``.

    Mirrors ``cli_usage._resolve_sessions_db_path`` — env var first,
    then ``opencomputer.agent.config._home`` for the active profile.
    Returns a path even if the file doesn't exist; SessionDB will
    create an empty schema on first connect.
    """
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if env:
        return Path(env) / "sessions.db"
    from opencomputer.agent.config import _home  # lazy: avoid cycles

    return _home() / "sessions.db"


def _render_session_panel(session_id: str) -> int:
    """Render the panel for ``session_id``. Returns shell exit code."""
    from opencomputer.agent.state import SessionDB

    db_path = _resolve_sessions_db_path()
    db = SessionDB(db_path)

    summary = db.session_usage_summary(session_id)
    if summary is None:
        from opencomputer.cli_ui.empty_state import render_empty_state

        render_empty_state(
            console=_console,
            title="Session not found",
            when_populated=(
                "a context-window panel: model, used / max tokens, "
                "compactions this session, and the trigger threshold."
            ),
            why_empty=(
                f"No session row for id {session_id!r}. The id may have "
                "been deleted, mistyped, or belong to a different OC "
                "profile."
            ),
            next_steps=[
                "[bold]oc usage sessions[/bold] — list every session id in this profile",
                "[bold]oc context show --current[/bold] — render for the most-recent session instead",
            ],
        )
        return 0

    from opencomputer.agent.compaction import resolve_window_safe

    max_ctx = resolve_window_safe(summary.model or "")
    used = summary.input_tokens
    pct = (used / max_ctx * 100.0) if max_ctx > 0 else 0.0
    remaining = max_ctx - used

    # Plain-text rendering keeps the test asserts simple AND mirrors the
    # in-chat ``/context`` slash output. Rich panels are pretty but
    # introduce wrap / truncation issues in narrow CliRunner terminals.
    sid_short = summary.session_id[:8]
    lines = ["## Context window"]
    lines.append(f"  session: {sid_short}… ({summary.session_id})")
    lines.append(f"  model: {summary.model or '(unknown)'}")
    lines.append(f"  used: {used:,} / {max_ctx:,} ({pct:.1f}%)")
    lines.append(f"  remaining: {remaining:,} tokens")
    lines.append(
        f"  compaction triggers at: {_COMPACTION_TRIGGER_PCT * 100:.0f}%"
    )
    lines.append(f"  compactions this session: {summary.compactions_count}")
    lines.append(f"  output tokens: {summary.output_tokens:,}")
    lines.append(
        f"  cache: {summary.cache_read_tokens:,} read / "
        f"{summary.cache_write_tokens:,} written"
    )
    if summary.cost_usd is not None:
        lines.append(f"  cost: ${summary.cost_usd:.4f}")
    else:
        lines.append("  cost: — (no priced llm_calls rows)")

    _console.print("\n".join(lines))
    return 0


def _resolve_current_session_id() -> str | None:
    """Return the most-recently-started session id, or ``None`` if
    the DB is empty."""
    from opencomputer.agent.state import SessionDB

    db = SessionDB(_resolve_sessions_db_path())
    rows = db.list_sessions(limit=1)
    if not rows:
        return None
    return str(rows[0]["id"])


@context_app.command("show")
def context_show(
    session_id: str | None = typer.Argument(
        None,
        metavar="SESSION_ID",
        help="Session id to inspect. Omit when using --current.",
    ),
    current: bool = typer.Option(
        False, "--current", "-c",
        help="Pick the most-recently-started session in the active profile.",
    ),
) -> None:
    """Render the context-window panel for a session.

    Either pass ``SESSION_ID`` positionally or use ``--current`` to
    pick the most-recent session.
    """
    if not session_id and not current:
        raise typer.BadParameter(
            "Provide a SESSION_ID or pass --current. "
            "Run ``oc usage sessions`` to list ids."
        )
    if session_id and current:
        raise typer.BadParameter(
            "Pass either SESSION_ID or --current, not both."
        )

    if current:
        sid = _resolve_current_session_id()
        if sid is None:
            from opencomputer.cli_ui.empty_state import render_empty_state

            render_empty_state(
                console=_console,
                title="No sessions yet",
                when_populated=(
                    "the most-recent session's context-window panel."
                ),
                why_empty=(
                    "This profile has no sessions in its DB. Run "
                    "``opencomputer chat`` to start one, then re-run "
                    "this command."
                ),
                next_steps=[
                    "[bold]opencomputer chat[/bold] — start a chat session",
                    "[bold]opencomputer profile list[/bold] — make sure you're in the right profile",
                ],
            )
            return
    else:
        sid = session_id

    assert sid is not None  # narrowed by branches above
    raise typer.Exit(code=_render_session_panel(sid))


@context_app.command("list")
def context_list(
    limit: int = typer.Option(
        20, "--limit", "-n",
        help="Max sessions to list. Clamped to [1, 1000].",
    ),
) -> None:
    """List recent sessions with context-window % used per row.

    Companion to ``oc context show`` — gives a quick overview of every
    session's context pressure so you can pick which to drill into.
    """
    from rich.table import Table

    from opencomputer.agent.state import SessionDB

    db = SessionDB(_resolve_sessions_db_path())
    rows = db.usage_summary_aggregate(limit=limit)
    if not rows:
        from opencomputer.cli_ui.empty_state import render_empty_state

        render_empty_state(
            console=_console,
            title="No sessions",
            when_populated=(
                "a table of every session with its context-window % "
                "and compaction count."
            ),
            why_empty=(
                "No sessions yet in this profile. Start one with "
                "``opencomputer chat``."
            ),
            next_steps=[
                "[bold]opencomputer chat[/bold] — start a new session",
            ],
        )
        return

    from opencomputer.agent.compaction import resolve_window_safe

    table = Table(title=f"Sessions ({len(rows)})", expand=False)
    table.add_column("Session", overflow="fold")
    table.add_column("Model", overflow="fold")
    table.add_column("Used / Max", justify="right")
    table.add_column("%", justify="right")
    table.add_column("Compactions", justify="right")
    for r in rows:
        max_ctx = resolve_window_safe(r.model or "")
        pct = (r.input_tokens / max_ctx * 100.0) if max_ctx > 0 else 0.0
        table.add_row(
            (r.session_id[:8] + "…") if len(r.session_id) > 9 else r.session_id,
            r.model or "—",
            f"{r.input_tokens:,} / {max_ctx:,}",
            f"{pct:.1f}%",
            str(r.compactions_count),
        )

    # Wide console keeps long model names from wrapping in narrow CI.
    Console(width=240, soft_wrap=True).print(table)


__all__ = ["context_app"]
