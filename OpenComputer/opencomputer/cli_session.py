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


__all__ = ["session_app"]
