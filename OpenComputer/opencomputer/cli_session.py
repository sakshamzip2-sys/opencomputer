"""`opencomputer session` CLI subcommand — list, show, fork, resume.

Sub-project G.33 (Tier 4). Surfaces the existing ``SessionDB``
session/message storage to the user via four subcommands:

* ``list``    — table of recent sessions (id, started, platform, msgs).
* ``show ID`` — print one session's metadata + a head-of-messages
                preview without dumping the entire history.
* ``fork ID`` — clone a session's history into a NEW session id so
                you can branch the conversation without polluting the
                source. Useful for "what would have happened if I'd
                asked Y instead of X?".
* ``resume ID`` — print the exact ``opencomputer chat --resume <id>``
                command to drop into the existing session. We don't
                spawn the chat ourselves because typer-inside-typer is
                fiddly; the user copy-pastes the line.

Storage is the per-profile ``sessions.db`` resolved via
``opencomputer.agent.config._home()``. The CLI never touches profile
state directly — it just reads/writes through ``SessionDB``.

Round 2B P-12 adds three slim filters to ``session list``:

* ``--label`` — case-insensitive substring match on title.
* ``--agent`` — switch the source DB to a named profile's
                ``sessions.db`` (profiles are per-directory, so
                ``meta.profile_name`` maps to which DB we open).
* ``--search`` — FTS5 query across messages; returns sessions whose
                 messages contained matches. User input is wrapped in
                 a quoted phrase so ``:``, ``*``, ``(``, ``)`` etc.
                 stay literal instead of getting parsed as FTS5
                 operators.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config import _home
from opencomputer.agent.state import SessionDB
from opencomputer.profiles import (
    ProfileNameError,
    get_profile_dir,
    validate_profile_name,
)

session_app = typer.Typer(
    name="session", help="Inspect, fork, and resume saved sessions."
)
console = Console()


def _db() -> SessionDB:
    """Construct a SessionDB rooted at the active profile."""
    db_path: Path = _home() / "sessions.db"
    return SessionDB(db_path)


def _db_for_profile(profile: str) -> SessionDB:
    """Construct a SessionDB rooted at *profile*'s directory.

    Used by ``--agent`` to read sessions belonging to a different
    profile than the active one. Sessions in OpenComputer are isolated
    per-profile by directory (each profile has its own
    ``sessions.db``), so "filter by agent" maps directly to "open the
    other profile's DB". Validation is delegated to
    :func:`opencomputer.profiles.validate_profile_name` so the same
    rules apply that ``profiles.py`` enforces elsewhere.
    """
    validate_profile_name(profile)
    db_path: Path = get_profile_dir(profile) / "sessions.db"
    return SessionDB(db_path)


def _escape_fts5(query: str) -> str:
    """Wrap *query* as a quoted FTS5 phrase so special chars are literal.

    FTS5 reserves a handful of characters that have meaning in its
    mini query language: ``:`` (column qualifier), ``*`` (prefix
    operator), ``(``/``)`` (grouping), ``"`` (phrase delimiter), and
    operators like ``AND``/``OR``/``NOT``. User-supplied search text
    almost never wants those semantics — a query like ``a:b`` should
    look for the literal string ``a:b``, not "match column a against
    b".

    Wrapping in double quotes turns the whole thing into a single
    phrase token where the only metacharacter that survives is ``"``
    itself, which FTS5 escapes by doubling (``""``). That matches the
    pattern :meth:`SessionDB.search_episodic` already uses for its
    own FTS5 escaping.
    """
    safe = query.replace('"', '""')
    return f'"{safe}"'


def _format_started(started_at: float | int | None) -> str:
    """Render the started_at unix timestamp as YYYY-MM-DD HH:MM."""
    if not started_at:
        return "-"
    import datetime as _dt

    return _dt.datetime.fromtimestamp(float(started_at)).strftime(
        "%Y-%m-%d %H:%M"
    )


def _parse_age(spec: str) -> int:
    """Parse '30d' / '6w' / '3mo' / '1y' into seconds.

    Suffix is required; suffix-less or non-positive values raise
    ``ValueError``. Months are approximated as 30 days; years as 365.
    """
    if not spec:
        raise ValueError("empty age spec")
    s = spec.strip().lower()
    if s.endswith("mo"):
        n_str, mult = s[:-2], 30 * 86400
    elif s.endswith("d"):
        n_str, mult = s[:-1], 86400
    elif s.endswith("w"):
        n_str, mult = s[:-1], 7 * 86400
    elif s.endswith("y"):
        n_str, mult = s[:-1], 365 * 86400
    else:
        raise ValueError(f"missing suffix in {spec!r} (use 30d / 6w / 3mo / 1y)")
    try:
        n = int(n_str)
    except ValueError as e:
        raise ValueError(f"non-integer count in {spec!r}") from e
    if n <= 0:
        raise ValueError(f"age must be positive: {spec!r}")
    return n * mult


@session_app.command("list")
def session_list(
    limit: int = typer.Option(
        20, "--limit", "-n", help="Max sessions to show (1-200).", min=1, max=200,
    ),
    label: str | None = typer.Option(
        None,
        "--label",
        help=(
            "Case-insensitive substring match on session title. "
            "Combine with --agent / --search."
        ),
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        help=(
            "Profile (agent) name whose sessions.db to read. Defaults to "
            "the active profile. Profiles are per-directory in OpenComputer "
            "so this switches which DB we open."
        ),
    ),
    search: str | None = typer.Option(
        None,
        "--search",
        help=(
            "FTS5 query against message text. Returns sessions whose "
            "messages contained matches. Special chars (`:`, `*`, "
            "parens, etc.) are treated literally."
        ),
    ),
) -> None:
    """List recent sessions in the active profile.

    Round 2B P-12 added ``--label``, ``--agent``, and ``--search``
    filters. They compose: ``--label foo --search bar`` returns
    sessions whose title contains "foo" AND whose messages contain
    "bar". When ``--search`` is set we ask FTS5 for matching messages,
    map those back to session ids, then post-filter with
    title/agent locally — keeping a single SessionDB query path
    (avoids forking a parallel SQL builder).
    """
    if agent is not None:
        try:
            db = _db_for_profile(agent)
        except ProfileNameError as e:
            console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(1) from None
    else:
        db = _db()

    if search is not None and search.strip():
        # Walk FTS5 results → distinct session ids in match order, then
        # re-fetch each session's metadata so the table shape stays
        # identical to the no-filter path. Pull a generous batch from
        # FTS5 (limit * 5) so post-filtering by --label still has room
        # to reach `limit` sessions even when many messages match the
        # same session.
        #
        # ``phrase=True`` opt-in: SessionDB.search() defaults to legacy
        # behaviour (caller responsible for FTS5 syntax) so existing
        # callers (mcp/server.py, tools/recall.py) keep working. P-12
        # is the user-facing CLI entry point — we want raw user input
        # treated literally, so the FTS5 reserved characters in
        # ``_escape_fts5``'s docstring (``:``, ``*``, ``(``, ``)``,
        # ``"``) cannot be interpreted as operators.
        try:
            matches = db.search(
                search, limit=max(limit * 5, limit), phrase=True
            )
        except Exception as e:  # noqa: BLE001 — surface FTS5 errors
            console.print(f"[red]error:[/red] search failed: {e}")
            raise typer.Exit(1) from None
        seen_ids: list[str] = []
        seen_set: set[str] = set()
        for m in matches:
            sid = m.get("session_id") or ""
            if sid and sid not in seen_set:
                seen_set.add(sid)
                seen_ids.append(sid)
        rows: list[dict] = []
        for sid in seen_ids:
            meta = db.get_session(sid)
            if meta is not None:
                rows.append(meta)
    else:
        # Pull more than `limit` so post-filtering by --label can still
        # reach the cap. 200 is the SessionDB list_sessions max.
        fetch_limit = limit if label is None else max(limit, 200)
        rows = db.list_sessions(limit=fetch_limit)

    if label is not None:
        needle = label.lower()
        rows = [r for r in rows if needle in (r.get("title", "") or "").lower()]

    rows = rows[:limit]

    if not rows:
        if search is not None or label is not None or agent is not None:
            console.print("[dim]no sessions match the filters.[/dim]")
        else:
            console.print(
                "[dim]no sessions yet — run `opencomputer chat` first.[/dim]"
            )
        return
    t = Table(show_lines=False)
    t.add_column("id", style="cyan", overflow="fold")
    t.add_column("started", style="dim")
    t.add_column("platform")
    t.add_column("model", style="dim")
    t.add_column("msgs", justify="right")
    t.add_column("title")
    for r in rows:
        t.add_row(
            r.get("id", ""),
            _format_started(r.get("started_at")),
            r.get("platform", "") or "",
            (r.get("model", "") or "")[:40],
            str(r.get("message_count", 0)),
            (r.get("title", "") or "")[:60],
        )
    console.print(t)


@session_app.command("show")
def session_show(
    session_id: str = typer.Argument(..., help="Session id (full UUID hex)."),
    head: int = typer.Option(
        5,
        "--head",
        help="Show the first N messages (default 5; 0 = metadata only).",
        min=0,
        max=200,
    ),
) -> None:
    """Print one session's metadata + an optional head preview."""
    db = _db()
    meta = db.get_session(session_id)
    if meta is None:
        console.print(f"[red]error:[/red] session {session_id!r} not found.")
        raise typer.Exit(1)
    console.print(f"[cyan]id[/cyan]:        {meta.get('id', '')}")
    console.print(f"[cyan]started[/cyan]:   {_format_started(meta.get('started_at'))}")
    if meta.get("ended_at"):
        console.print(
            f"[cyan]ended[/cyan]:     {_format_started(meta.get('ended_at'))}"
        )
    console.print(f"[cyan]platform[/cyan]:  {meta.get('platform', '') or '-'}")
    console.print(f"[cyan]model[/cyan]:     {meta.get('model', '') or '-'}")
    console.print(f"[cyan]title[/cyan]:     {meta.get('title', '') or '-'}")
    console.print(f"[cyan]messages[/cyan]:  {meta.get('message_count', 0)}")
    if head <= 0:
        return
    msgs = db.get_messages(session_id)[:head]
    if not msgs:
        return
    console.print()
    console.print(f"[bold]first {len(msgs)} message(s):[/bold]")
    for i, m in enumerate(msgs, 1):
        preview = (m.content or "")[:200].replace("\n", " ")
        if len(m.content or "") > 200:
            preview += " […]"
        console.print(f"  [dim]{i}.[/dim] [cyan]{m.role}[/cyan]: {preview}")


@session_app.command("fork")
def session_fork(
    session_id: str = typer.Argument(..., help="Source session id to clone."),
    title: str = typer.Option(
        "",
        "--title",
        "-t",
        help="Title for the new session (defaults to '<source title> (fork)').",
    ),
) -> None:
    """Clone a session's history into a new session id.

    Useful for branching a conversation without polluting the source —
    e.g. "what would the agent have said if I'd asked Y instead?".
    The new session inherits the source's platform / model / message
    history; only the id (and title) differ.
    """
    db = _db()
    src = db.get_session(session_id)
    if src is None:
        console.print(f"[red]error:[/red] source session {session_id!r} not found.")
        raise typer.Exit(1)
    msgs = db.get_messages(session_id)
    new_id = uuid.uuid4().hex
    new_title = title or f"{(src.get('title') or '').strip()} (fork)".strip()
    db.create_session(
        new_id,
        platform=src.get("platform", "") or "cli",
        model=src.get("model", "") or "",
        title=new_title,
    )
    if msgs:
        db.append_messages_batch(new_id, msgs)
    console.print(
        f"[green]forked[/green] {session_id} → [cyan]{new_id}[/cyan] "
        f"({len(msgs)} message(s) copied)"
    )
    console.print(f"[dim]continue with: opencomputer chat --resume {new_id}[/dim]")


@session_app.command("resume")
def session_resume(
    session_id: str = typer.Argument(..., help="Session id to continue."),
) -> None:
    """Print the chat-resume command for a session.

    Doesn't spawn ``chat`` itself — typer-inside-typer is fiddly and
    invoking interactive flows from a subcommand has rough edges. The
    caller copy-pastes the printed line.
    """
    src = _db().get_session(session_id)
    if src is None:
        console.print(f"[red]error:[/red] session {session_id!r} not found.")
        raise typer.Exit(1)
    console.print(
        f"[green]ready to resume[/green] [cyan]{session_id}[/cyan] "
        f"({src.get('message_count', 0)} message(s))"
    )
    console.print(f"  [bold]opencomputer chat --resume {session_id}[/bold]")


@session_app.command("delete")
def session_delete(
    session_id: str = typer.Argument(
        ..., help="Session id to delete (full UUID hex or 8-char prefix)."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt. Required for non-interactive use.",
    ),
) -> None:
    """Delete a session and all its messages.

    Cascades through messages / episodic_events / vibe_log / tool_usage
    via FOREIGN KEY ... ON DELETE CASCADE. The F1 audit_log is preserved
    (append-only by trigger).
    """
    db = _db()
    src = db.get_session(session_id)
    if src is None:
        console.print(f"[red]error:[/red] session {session_id!r} not found.")
        raise typer.Exit(1)
    title = (src.get("title") or f"(untitled · {session_id[:8]})").strip()
    msg_count = src.get("message_count", 0)
    if not yes:
        console.print(
            f"Delete session [cyan]{session_id[:8]}[/cyan] "
            f"({title}, {msg_count} message(s))? [y/N] ",
            end="",
        )
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            console.print("[dim]aborted.[/dim]")
            raise typer.Exit(1)
    db.delete_session(session_id)
    console.print(
        f"[green]deleted[/green] {session_id[:8]} "
        f"({msg_count} message(s) removed)"
    )


@session_app.command("prune")
def session_prune(
    older_than: str | None = typer.Option(
        None,
        "--older-than",
        help="Drop sessions older than this. Examples: 30d, 6w, 3mo, 1y.",
    ),
    untitled: bool = typer.Option(
        False, "--untitled", help="Drop sessions whose title is empty."
    ),
    empty: bool = typer.Option(
        False,
        "--empty",
        help="Drop sessions with message_count <= 1 (system-only / aborted).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Show what would be deleted, change nothing.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt before deleting.",
    ),
) -> None:
    """Bulk-delete sessions matching the given filters (AND-composed).

    Refuses to run with no filters — preventing accidental whole-DB
    nukes. Filters compose with AND: ``--untitled --older-than 30d``
    only deletes untitled sessions older than 30 days.
    """
    if not (older_than or untitled or empty):
        console.print(
            "[red]error:[/red] specify at least one filter "
            "(--older-than / --untitled / --empty). "
            "Refusing to prune everything."
        )
        raise typer.Exit(1)

    cutoff_ts: float | None = None
    if older_than:
        try:
            cutoff_ts = time.time() - _parse_age(older_than)
        except ValueError as e:
            console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(1) from None

    db = _db()
    rows = db.list_sessions(limit=200)
    candidates = []
    for r in rows:
        if cutoff_ts is not None and (r.get("started_at") or 0) >= cutoff_ts:
            continue
        if untitled and (r.get("title") or "").strip():
            continue
        if empty and (r.get("message_count") or 0) > 1:
            continue
        candidates.append(r)

    if not candidates:
        console.print("[dim]nothing to prune.[/dim]")
        return

    t = Table(show_lines=False)
    t.add_column("id", style="cyan")
    t.add_column("started", style="dim")
    t.add_column("msgs", justify="right")
    t.add_column("title")
    for r in candidates:
        t.add_row(
            (r.get("id", "") or "")[:8],
            _format_started(r.get("started_at")),
            str(r.get("message_count", 0)),
            (r.get("title", "") or "")[:50],
        )
    console.print(t)
    if dry_run:
        console.print(
            f"[yellow]dry-run:[/yellow] would delete {len(candidates)} session(s)"
        )
        return

    if not yes:
        console.print(f"delete {len(candidates)} session(s)? [y/N] ", end="")
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            console.print("[dim]aborted.[/dim]")
            raise typer.Exit(1)

    deleted = 0
    for r in candidates:
        if db.delete_session(r["id"]):
            deleted += 1
    console.print(f"[green]pruned[/green] {deleted} session(s)")


# ---------------------------------------------------------------------------
# Hermes-CLI parity C2-C4 — `oc sessions {stats,export,rename}` subcommands.
# These ride on the same session_app so they're reachable as both
# `oc session …` (singular) and `oc sessions …` (plural alias added in cli.py).
# ---------------------------------------------------------------------------


@session_app.command("stats")
def stats_(
    profile: str | None = typer.Option(
        None, "--agent", help="Read another profile's sessions.db."
    ),
) -> None:
    """Counts of sessions by source + total messages + DB size on disk.

    Hermes-CLI parity (doc lines 477-486).
    """
    db = _db_for_profile(profile) if profile else _db()
    db_path: Path = (
        get_profile_dir(profile) / "sessions.db" if profile else _home() / "sessions.db"
    )

    rows = db.list_sessions(limit=10_000)
    total = len(rows)
    by_src: dict[str, int] = {}
    n_msg = 0
    for r in rows:
        src = (r.get("platform") or r.get("source") or "cli") or "cli"
        by_src[src] = by_src.get(src, 0) + 1
        n_msg += int(r.get("message_count") or 0)

    size_mb = db_path.stat().st_size / 1_048_576 if db_path.exists() else 0.0
    console.print(f"Total sessions: [bold]{total}[/]")
    console.print(f"Total messages: [bold]{n_msg}[/]")
    for src, n in sorted(by_src.items(), key=lambda kv: -kv[1]):
        console.print(f"  {src}: {n}")
    console.print(f"Database size: [bold]{size_mb:.1f} MB[/]")


@session_app.command("export")
def export_(
    path: str = typer.Argument(..., help="Output JSONL file."),
    source: str | None = typer.Option(
        None, "--source", help="Filter by source/platform."
    ),
    session_id: str | None = typer.Option(
        None, "--session-id", help="Export one session only."
    ),
    include_messages: bool = typer.Option(
        True, "--include-messages/--no-messages",
        help="Inline full messages (default true).",
    ),
) -> None:
    """Dump sessions to JSONL — one JSON object per line.

    Hermes-CLI parity (doc line 472). Messages are inlined under the
    ``messages`` key when --include-messages (default).
    """
    import json

    db = _db()
    if session_id:
        row = db.get_session(session_id)
        rows = [row] if row else []
    else:
        rows = list(db.list_sessions(limit=10_000))
        if source:
            rows = [
                r
                for r in rows
                if (r.get("platform") or r.get("source") or "cli") == source
            ]

    out_p = Path(path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_p.open("w", encoding="utf-8") as fh:
        for row in rows:
            sid = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
            payload: dict = (
                dict(row) if isinstance(row, dict) else dict(vars(row))
            )
            if include_messages and sid:
                try:
                    payload["messages"] = list(db.get_messages(sid) or [])
                except Exception:  # noqa: BLE001
                    payload["messages"] = []
            fh.write(json.dumps(payload, default=str) + "\n")
            n += 1
    console.print(f"exported {n} session(s) to {out_p}")


@session_app.command("rename")
def rename_(
    session_id: str = typer.Argument(..., help="Session id (or unique prefix)."),
    title: list[str] = typer.Argument(
        None, help="New title (no quotes needed for multi-word)."
    ),
) -> None:
    """Set or change the title of a saved session.

    Hermes-CLI parity (doc line 433). Multi-word titles need no quotes:
    ``oc sessions rename ABCD debugging auth flow``.
    """
    new_title = " ".join(title or []).strip()
    if not new_title:
        console.print("[red]title required[/]")
        raise typer.Exit(2)
    db = _db()
    if hasattr(db, "set_session_title"):
        db.set_session_title(session_id, new_title)
    else:  # pragma: no cover — should always exist on the current SessionDB
        with db._txn() as conn:  # type: ignore[attr-defined]
            conn.execute(
                "UPDATE sessions SET title=? WHERE id=?",
                (new_title, session_id),
            )
    console.print(
        f"renamed [cyan]{session_id[:8]}[/] -> [bold]{new_title}[/]"
    )


# ─── checkpoints (v1.1 plan-2 M5.1, 2026-05-09) ──────────────────────────


@session_app.command("checkpoints")
def session_checkpoints(
    session_id: str = typer.Argument(..., help="Session id to inspect."),
    limit: int = typer.Option(
        50, "--limit", help="Maximum number of checkpoints to display."
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of a Rich table.",
    ),
) -> None:
    """List per-prompt checkpoints saved for ``session_id``.

    Reads the existing on-disk layout written by
    ``extensions/coding-harness/rewind/store.py`` —
    ``~/.opencomputer/harness/<session_id>/rewind/<checkpoint_id>/``.
    No new storage; this command surfaces what the rewind/checkpoint
    plumbing already records.

    Use ``oc checkpoints status`` for a cross-session aggregate; this
    command focuses on one session so you can pick a checkpoint to
    pass to ``rewind`` (M5.3, deferred).
    """
    import json as _json

    from rewind.store import RewindStore  # type: ignore[import-not-found]

    # checkpoint_admin already wires extensions/coding-harness onto
    # sys.path at import-time, so importing it first lets us pull
    # RewindStore off the resulting module path without re-doing the
    # sys.path dance here.
    from opencomputer.checkpoint_admin import harness_root  # noqa: F401

    rwd = harness_root() / session_id / "rewind"
    if not rwd.exists():
        if json_out:
            typer.echo(_json.dumps({"session_id": session_id, "checkpoints": []}))
            return
        console.print(
            f"[yellow]no checkpoints[/yellow] for session "
            f"[cyan]{session_id[:8]}[/cyan] (no rewind dir at {rwd})"
        )
        return

    try:
        store = RewindStore(rwd, workspace_root=harness_root() / session_id)
        cps = store.list()
    except (OSError, ValueError) as exc:
        console.print(f"[red]error:[/red] could not read checkpoints: {exc}")
        raise typer.Exit(1) from None

    cps_sorted = sorted(cps, key=lambda c: c.created_at, reverse=True)[:limit]

    if json_out:
        typer.echo(
            _json.dumps(
                {
                    "session_id": session_id,
                    "checkpoints": [
                        {
                            "id": c.id,
                            "label": c.label,
                            "created_at": c.created_at,
                            "file_count": len(c.files),
                            "size_bytes": sum(len(b) for b in c.files.values()),
                            "excluded_files": list(c.excluded_files),
                        }
                        for c in cps_sorted
                    ],
                }
            )
        )
        return

    if not cps_sorted:
        console.print(
            f"[dim]no checkpoints recorded for session "
            f"[cyan]{session_id[:8]}[/cyan][/dim]"
        )
        return

    table = Table(
        title=f"Checkpoints — session {session_id[:8]}",
        show_lines=False,
    )
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("label")
    table.add_column("created_at", style="dim")
    table.add_column("files", justify="right")
    table.add_column("size", justify="right")
    for cp in cps_sorted:
        size_kb = sum(len(b) for b in cp.files.values()) / 1024
        size_str = f"{size_kb:.1f}KB" if size_kb >= 1 else f"{int(size_kb * 1024)}B"
        table.add_row(
            cp.id[:12],
            cp.label,
            cp.created_at,
            str(len(cp.files)),
            size_str,
        )
    console.print(table)
    console.print(
        f"[dim]showing {len(cps_sorted)} of {len(cps)} checkpoint(s)"
        f"{'; pass --limit to see more' if len(cps) > len(cps_sorted) else ''}[/dim]"
    )


# ─── rewind (v1.1 plan-2 M5.3, 2026-05-09) ───────────────────────────────


@session_app.command("rewind")
def session_rewind(
    session_id: str = typer.Argument(..., help="Session id whose files to restore."),
    at: str | None = typer.Option(
        None,
        "--at",
        help=(
            "Specific checkpoint id (or 8-char prefix) to restore. Skips the "
            "interactive picker — required under --headless."
        ),
    ),
    mode: str = typer.Option(
        "files",
        "--mode",
        help=(
            "Restore mode. 'files' (default) restores file contents from the "
            "checkpoint via RewindStore.restore(). 'conv_only' / 'summarize_from' "
            "require the message-history checkpoint surface (v1.1 deferred — "
            "tracked as M5.2 follow-up); requesting them today returns a clear "
            "not-yet-implemented error."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=(
            "Skip the confirmation prompt. Required under --headless or when "
            "--at is supplied non-interactively."
        ),
    ),
) -> None:
    """Rewind a session's working tree to a saved checkpoint.

    Interactive: pick from the session's checkpoints (newest first).
    Non-interactive: pass ``--at <checkpoint_id>``.

    Files are restored from the on-disk RewindStore at
    ``~/.opencomputer/harness/<session_id>/rewind/<checkpoint_id>/``
    written by the `auto_checkpoint` PreToolUse hook.

    The plan's full mode matrix (``conv_only`` / ``files_only`` /
    ``summarize_from``) requires per-prompt message-history snapshots
    (M5.2) which aren't wired yet — this command surfaces only the
    file-restore mode and reports cleanly on the others.
    """
    from rewind.store import RewindStore  # type: ignore[import-not-found]

    from opencomputer.checkpoint_admin import harness_root  # noqa: F401

    if mode not in ("files", "conv_only", "files_only", "summarize_from"):
        console.print(
            f"[red]error:[/red] unknown --mode {mode!r}. Use one of: "
            f"files, conv_only, files_only, summarize_from."
        )
        raise typer.Exit(2)
    if mode in ("conv_only", "summarize_from"):
        console.print(
            f"[yellow]not yet implemented:[/yellow] --mode {mode} needs the "
            f"per-prompt message-history checkpoint surface (v1.1 plan-2 M5.2 "
            f"deferral). Use --mode files for the file-restore path that's "
            f"shipped today."
        )
        raise typer.Exit(2)

    rwd_root = harness_root() / session_id / "rewind"
    if not rwd_root.exists():
        console.print(
            f"[red]error:[/red] no rewind store for session "
            f"[cyan]{session_id[:8]}[/cyan] (no dir at {rwd_root})."
        )
        raise typer.Exit(1)

    try:
        store = RewindStore(rwd_root, workspace_root=harness_root() / session_id)
        checkpoints = store.list()
    except (OSError, ValueError) as exc:
        console.print(f"[red]error:[/red] cannot read checkpoints: {exc}")
        raise typer.Exit(1) from None

    if not checkpoints:
        console.print(
            f"[yellow]no checkpoints[/yellow] recorded for session "
            f"[cyan]{session_id[:8]}[/cyan]."
        )
        raise typer.Exit(1)

    if at:
        target = _resolve_checkpoint(checkpoints, at)
        if target is None:
            console.print(
                f"[red]error:[/red] no checkpoint matches "
                f"[cyan]{at}[/cyan]. Run [bold]oc session checkpoints "
                f"{session_id[:8]}[/bold] to list available ids."
            )
            raise typer.Exit(1)
    else:
        import sys

        if not sys.stdin.isatty():
            console.print(
                "[red]error:[/red] interactive picker requires a TTY. "
                "Pass --at <checkpoint_id> to choose one non-interactively."
            )
            raise typer.Exit(1)
        target = _interactive_pick(checkpoints, session_id)
        if target is None:
            console.print("[dim]cancelled.[/dim]")
            raise typer.Exit(1)

    if not yes:
        console.print(
            f"Restore session [cyan]{session_id[:8]}[/cyan] files to "
            f"checkpoint [bold]{target.id[:12]}[/bold] "
            f"({target.label}, {target.created_at})? [y/N] ",
            end="",
        )
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            console.print("[dim]aborted.[/dim]")
            raise typer.Exit(1)

    try:
        store.restore(target.id)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]error:[/red] restore failed: {exc}")
        raise typer.Exit(1) from None

    console.print(
        f"[green]restored[/green] session [cyan]{session_id[:8]}[/cyan] "
        f"to checkpoint [bold]{target.id[:12]}[/bold] "
        f"({len(target.files)} file(s) written)"
    )


def _resolve_checkpoint(checkpoints, spec: str):
    """Match a checkpoint by exact id or 4+-char prefix."""
    spec = spec.strip().lower()
    exact = [c for c in checkpoints if c.id.lower() == spec]
    if exact:
        return exact[0]
    if len(spec) >= 4:
        prefixed = [c for c in checkpoints if c.id.lower().startswith(spec)]
        if len(prefixed) == 1:
            return prefixed[0]
    return None


def _interactive_pick(checkpoints, session_id: str):
    """Show a numbered list and read a single selection."""
    console.print(
        f"\nCheckpoints for session [cyan]{session_id[:8]}[/cyan] "
        f"(newest first):\n"
    )
    table = Table(show_lines=False)
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("label")
    table.add_column("created_at", style="dim")
    table.add_column("files", justify="right")
    sorted_cps = sorted(checkpoints, key=lambda c: c.created_at, reverse=True)
    for idx, cp in enumerate(sorted_cps, start=1):
        table.add_row(
            str(idx),
            cp.id[:12],
            cp.label,
            cp.created_at,
            str(len(cp.files)),
        )
    console.print(table)
    console.print(
        "\nSelect a number (1-N), an id prefix (4+ chars), or press Enter to cancel:"
    )
    try:
        raw = input("> ").strip()
    except EOFError:
        return None
    if not raw:
        return None
    try:
        idx = int(raw)
        if 1 <= idx <= len(sorted_cps):
            return sorted_cps[idx - 1]
    except ValueError:
        pass
    return _resolve_checkpoint(sorted_cps, raw)


__all__ = ["session_app"]
