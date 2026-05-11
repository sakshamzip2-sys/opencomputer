"""Full-screen session picker for ``oc resume``.

Provides:

- :class:`SessionRow` — minimal dataclass shape the picker renders
  (decoupled from :class:`SessionDB`'s wider row schema)
- :func:`filter_rows` — case-insensitive substring search over title +
  id-prefix, used by the live search box
- :func:`format_time_ago` — humanize Unix epoch seconds as ``"12 minutes ago"``
- :func:`run_resume_picker` — builds and runs the full-screen prompt_toolkit
  Application; returns the selected session id, or ``None`` if the user
  cancels (Esc / Ctrl+C / empty list)

The picker uses *alternate-screen mode* which (a) gives a clean overlay
that disappears on exit, restoring the user's terminal state, and (b)
sidesteps Cursor-Position-Report entirely — making it work in editor
terminals (VS Code, JetBrains) that don't reliably respond to CPR.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionRow:
    """Minimal data the picker needs to display one session."""

    id: str
    title: str
    started_at: float  # Unix epoch seconds (matches SessionDB schema: REAL)
    message_count: int
    # Empty when the session has no recorded cwd (very old rows). Used by
    # :func:`format_session_label` as the headline when ``title`` is also
    # empty — gives the picker a useful label even before the auto-titler
    # has fired.
    cwd: str = ""
    # First user-role message captured when listing sessions for the
    # picker. Used by :func:`format_session_label` as the preferred
    # fallback when no manual or auto-generated title exists — mirrors
    # Claude Code's resume picker, which always shows a short preview of
    # what the conversation was about. Empty when no user message has
    # been recorded yet (e.g., session opened but never sent).
    first_user_message: str = ""
    # v19 (2026-05-11) — active git branch at session-create time, sourced
    # from :func:`opencomputer.worktree.current_git_branch`. Empty when the
    # session started outside a git repo, on a detached HEAD, or for
    # pre-v19 rows (legacy NULLs). Rendered as an extra meta-strip segment
    # only when present, so older rows degrade to the prior layout.
    git_branch: str = ""


def _clean_label(text: str, *, max_len: int = 80) -> str:
    """Single-line, length-capped label from possibly-multiline text.

    Replaces ``\\r``/``\\n``/``\\t`` with spaces, collapses whitespace
    runs, and truncates with a single ``…`` suffix if longer than
    ``max_len``. Empty input passes through unchanged.

    This is shared between :func:`format_session_label` (cleans titles
    that contain newlines — the legacy auto-titler shipped many) and the
    first-user-message fallback (cleans pasted multi-paragraph prompts).
    """
    if not text:
        return ""
    cleaned = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def filter_rows(rows: list[SessionRow], query: str) -> list[SessionRow]:
    """Case-insensitive substring filter over title + preview + id prefix.

    Empty query returns all rows unchanged. Matches on any of:
    - ``query`` is a substring of ``row.title`` (case-insensitive), OR
    - ``query`` is a substring of ``row.first_user_message`` (case-insensitive), OR
    - ``row.id`` starts with ``query`` (case-insensitive — for paste-friendly
      partial-UUID lookups from log scrolls)
    """
    if not query:
        return list(rows)
    q = query.lower()
    return [
        r
        for r in rows
        if q in r.title.lower()
        or q in r.first_user_message.lower()
        or r.id.lower().startswith(q)
    ]


def format_time_ago(ts: float, *, now: float | None = None) -> str:
    """Humanize a Unix epoch timestamp as ``"X seconds/minutes/hours/days ago"``.

    ``ts`` matches :class:`SessionDB`'s schema — column ``started_at`` is
    ``REAL`` storing ``time.time()`` (seconds since epoch as float).

    Returns ``"just now"`` for deltas under 1 second and ``"unknown"`` if
    ``ts`` is not a number.
    """
    if not isinstance(ts, (int, float)):
        return "unknown"
    import time as _time

    if now is None:
        now = _time.time()
    delta = now - ts
    if delta < 1:
        return "just now"
    seconds = int(delta)
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''} ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def format_session_label(row: SessionRow, *, now: float | None = None) -> str:
    """Headline label for one row in the picker.

    Resolution order (mirrors Claude Code's ``/resume``):

    1. ``row.title`` when present — set either via ``/rename`` or by
       :func:`opencomputer.agent.title_generator.maybe_auto_title`. Cleaned
       to single-line via :func:`_clean_label` since legacy auto-titler
       output occasionally contained embedded newlines.
    2. ``row.first_user_message`` (truncated, single-line) when no title
       is set — gives every session a meaningful headline instead of a
       useless ``default @ HH:MM`` for sessions started from the profile
       home. This is the key parity with Claude Code's resume picker.
    3. ``<cwd-basename> @ HH:MM`` when there is also no first user
       message recorded — still better than ``(untitled · ID)`` for
       sessions that opened and exited before a turn fired.
    4. ``(untitled · <id-prefix>)`` legacy fallback for very old rows
       that have neither title, message, nor cwd recorded.

    ``started_at`` is rendered in *local time* via :mod:`time.strftime`,
    matching the user's terminal locale. ``now`` is accepted only for
    test determinism (current implementation ignores it; ``started_at``
    is always shown as the absolute time, not a relative-to-now diff).
    """
    del now  # accepted for test-API symmetry; not used for absolute time

    if row.title:
        return _clean_label(row.title)

    if row.first_user_message:
        preview = _clean_label(row.first_user_message)
        if preview:
            return preview

    if row.cwd:
        import os as _os
        import time as _time

        basename = _os.path.basename(row.cwd.rstrip("/")) or row.cwd
        try:
            hhmm = _time.strftime("%H:%M", _time.localtime(row.started_at))
        except (TypeError, ValueError, OverflowError):
            hhmm = "??:??"
        return f"{basename} @ {hhmm}"

    return f"(untitled · {row.id[:8]})"


def format_session_preview(row: SessionRow, *, max_len: int = 80) -> str:
    """Dim "context" line rendered beneath the headline in the 3-line picker.

    Resolution:

    1. If a title is set AND first_user_message exists → show the
       message preview. The title is the *name*; the preview gives the
       reader the *what* without expanding the row.
    2. If the first_user_message is what's already in line 1 (because
       no title was set), fall through to the cwd hint so line 2 is
       additive — never a duplicate of line 1.
    3. Otherwise show the cwd path (truncated). Helps the user
       distinguish "default" / "OpenComputer" sessions started from
       different working directories.
    4. Empty string when nothing useful exists. The picker still
       reserves the line slot so row height stays uniform.

    Mirrors Claude Code's `/resume` two-line entry body (headline +
    context). The third line is the meta strip rendered by the picker
    itself.
    """
    if row.title and row.first_user_message:
        preview = _clean_label(row.first_user_message, max_len=max_len)
        if preview:
            return preview

    if row.cwd:
        cwd = row.cwd
        if len(cwd) > max_len:
            cwd = "…" + cwd[-(max_len - 1) :]
        return cwd

    return ""


def compute_visible_window(
    rows: list[SessionRow],
    *,
    selected_idx: int,
    window_height: int,
) -> tuple[list[SessionRow], int]:
    """Pure scroll-window calculator — picks the visible slice of *rows*.

    Returns ``(visible_rows, scroll_offset)`` where ``scroll_offset`` is
    the index in *rows* that ``visible_rows[0]`` corresponds to. The
    selected row is guaranteed to be inside ``visible_rows`` (when the
    list is non-empty and ``selected_idx`` is valid).

    This is the only "smart" piece of the picker's scroll logic — it
    runs on every render, takes the full ``filtered`` list, the cursor
    position, and the current terminal-derived window height, and
    returns the slice to draw. Pure; no prompt_toolkit; trivially
    testable.

    Edge cases:

    - Empty ``rows`` → ``([], 0)``.
    - ``len(rows) <= window_height`` → all rows visible, offset 0.
    - ``selected_idx`` past the bottom → offset clamps so visible[-1]
      == rows[-1] (the "last page" view).
    - ``selected_idx`` < 0 (no selection) → top window.
    """
    if not rows or window_height <= 0:
        return [], 0

    n = len(rows)

    if n <= window_height:
        return list(rows), 0

    if selected_idx < 0:
        return list(rows[:window_height]), 0

    # Clamp selected_idx into bounds before computing the offset.
    sel = max(0, min(selected_idx, n - 1))

    # Center-ish strategy: keep the selected row in view. We use a
    # straightforward "page" model — the offset is whatever puts ``sel``
    # at the bottom margin of the window, then clamped to [0, n-height].
    # This avoids surprising "jumps" when the cursor crosses a page
    # boundary; the offset stays put unless the cursor is about to leave
    # the visible window.
    max_offset = n - window_height

    # Anchor the offset so sel sits at index (window_height - 1) — i.e.,
    # at the bottom of the visible window. Clamp to [0, max_offset].
    desired_offset = sel - (window_height - 1)
    offset = max(0, min(desired_offset, max_offset))

    return list(rows[offset : offset + window_height]), offset


# ─── Confirm-delete state machine helpers (importable for tests) ──────


def _enter_confirm_delete(state: dict) -> None:
    """Flip the picker into confirm-delete mode for the selected row.

    No-op if the filtered list is empty or no row is selected — there
    is nothing to delete.
    """
    if state["filtered"] and state["selected_idx"] >= 0:
        state["mode"] = "confirm-delete"


def _exit_confirm_delete(state: dict) -> None:
    """Cancel the pending delete and return to navigation mode."""
    state["mode"] = "navigate"


def _commit_confirm_delete(state: dict, db) -> None:  # noqa: ANN001 — db is SessionDB
    """Commit the pending delete: drop row from DB + both lists, clamp cursor."""
    state["mode"] = "navigate"
    if not state["filtered"] or state["selected_idx"] < 0:
        return
    target = state["filtered"][state["selected_idx"]]
    db.delete_session(target.id)
    state["rows"] = [r for r in state["rows"] if r.id != target.id]
    state["filtered"] = [r for r in state["filtered"] if r.id != target.id]
    if state["selected_idx"] >= len(state["filtered"]):
        state["selected_idx"] = max(0, len(state["filtered"]) - 1)


# ─── Rename state-machine helpers (importable for tests) ───────────────


def _enter_rename(state: dict) -> None:
    """Flip the picker into rename mode for the selected row.

    No-op if the filtered list is empty or no row is selected. The
    caller is responsible for seeding any input buffer with the row's
    current title (the picker does this against ``state["rename_seed"]``).
    """
    if state["filtered"] and state["selected_idx"] >= 0:
        target = state["filtered"][state["selected_idx"]]
        state["mode"] = "rename"
        state["rename_seed"] = target.title or ""


def _exit_rename(state: dict) -> None:
    """Cancel the in-progress rename and return to navigate mode."""
    state["mode"] = "navigate"
    state["rename_seed"] = ""


def _commit_rename(
    state: dict, db, *, new_title: str  # noqa: ANN001 — db is SessionDB
) -> None:
    """Commit the new title to DB + replace the row in both lists.

    Empty / whitespace-only ``new_title`` is treated as "clear the
    title" (set to NULL in DB by passing ``""``). The picker's render
    pipeline falls back to the first_user_message / cwd preview chain
    when title is empty — so the user always sees SOMETHING on the row.
    """
    state["mode"] = "navigate"
    state["rename_seed"] = ""
    if not state["filtered"] or state["selected_idx"] < 0:
        return
    target = state["filtered"][state["selected_idx"]]
    cleaned = (new_title or "").strip()
    try:
        db.set_session_title(target.id, cleaned)
    except Exception as exc:  # noqa: BLE001 — UI must never crash on a DB hiccup
        import logging as _logging

        _logging.getLogger("opencomputer.cli_ui.resume_picker").warning(
            "set_session_title(%s, %r) raised %s; row not updated",
            target.id,
            cleaned,
            exc,
        )
        return

    # Replace the row in both backing lists so the UI reflects the
    # change without needing a full refetch. The SessionRow dataclass is
    # frozen, so we reconstruct it from the existing fields.
    def _swap(rows: list[SessionRow]) -> list[SessionRow]:
        return [
            SessionRow(
                id=r.id,
                title=cleaned,
                started_at=r.started_at,
                message_count=r.message_count,
                cwd=r.cwd,
                first_user_message=r.first_user_message,
                git_branch=r.git_branch,
            )
            if r.id == target.id
            else r
            for r in rows
        ]

    state["rows"] = _swap(state["rows"])
    state["filtered"] = _swap(state["filtered"])


#: Scope values understood by :func:`run_resume_picker` and forwarded to
#: :meth:`SessionDB.list_sessions_with_preview` via the refetch callback.
#: Kept as plain string constants (not an Enum) so callers can persist
#: them in settings.yaml without round-tripping through pickled enums.
SCOPE_CWD = "cwd"      # default — current working directory only
SCOPE_REPO = "repo"    # current cwd's repo, all worktrees
SCOPE_ALL = "all"      # every session on this machine


def run_resume_picker(  # noqa: ANN001 — `db` is duck-typed SessionDB
    rows: list[SessionRow],
    db=None,
    *,
    refetch=None,
    initial_scope: str = SCOPE_CWD,
    initial_branch_filter: bool = False,
    current_branch: str | None = None,
) -> str | None:
    """Open a full-screen picker and return the selected session id.

    Returns ``None`` if the user cancels (Esc, Ctrl+C, or empty input).
    Alternate-screen mode is used so the user's terminal state is restored
    cleanly when the picker exits regardless of outcome.

    Args:
        rows: initial list of :class:`SessionRow` to render. The picker
            re-reads via ``refetch`` when the user widens scope (Ctrl+W
            / Ctrl+A) or toggles the branch filter (Ctrl+B).
        db: optional :class:`SessionDB` reference used to commit
            in-picker deletes (Ctrl+D → y). Callers without delete
            support can omit it; Ctrl+D is a no-op when ``db is None``.
        refetch: optional ``Callable[[scope: str, branch_only: bool],
            list[SessionRow]]``. Invoked when the user presses Ctrl+W,
            Ctrl+A, or Ctrl+B. Receives the new ``scope`` (one of
            :data:`SCOPE_CWD`, :data:`SCOPE_REPO`, :data:`SCOPE_ALL`)
            and a boolean indicating whether the branch filter is now
            active. Returns the new row list. If ``None``, the
            scope-toggle shortcuts are disabled (footer hint omitted).
        initial_scope: the scope the caller already used to fetch
            ``rows``. Influences the chrome label only — the picker
            doesn't re-fetch on startup.
        initial_branch_filter: whether ``rows`` was fetched with the
            branch filter already active. Same purpose as
            ``initial_scope`` — chrome state only.
        current_branch: the currently-checked-out branch (used for the
            chrome's "Ctrl+B (current: <name>)" hint). ``None`` when
            we're not inside a git repo.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.styles import Style

    # Allow ``rows == []`` ONLY when the caller wired ``refetch`` — the
    # user might Ctrl+A their way into a populated list from an empty
    # cwd scope. Without refetch, an empty list means "no sessions" and
    # we return None.
    if not rows and refetch is None:
        return None

    # Mutable state captured by the closures below. Plain dict keeps the
    # whole picker in one closure scope without needing a class.
    # ``mode`` switches between "navigate" (cursor + search) and
    # "confirm-delete" (y/n only). ``rows`` is the canonical mutable
    # backing list — `_commit_confirm_delete` removes from it so a
    # subsequent search won't bring the row back.
    state: dict = {
        "query": "",
        "selected_idx": 0 if rows else -1,
        "filtered": list(rows),
        "rows": list(rows),
        "mode": "navigate",
        # Phase B — scope + branch filter live in picker state so the
        # chrome can render them and the shortcuts can toggle them.
        "scope": initial_scope,
        "branch_only": initial_branch_filter,
        # Phase C — Ctrl+R rename. ``rename_seed`` is the title the
        # rename buffer was initialised with; ``mode == "rename"``
        # gates the buffer's visibility + the Enter / Esc handlers.
        "rename_seed": "",
    }

    def _is_navigating() -> bool:
        return state["mode"] == "navigate"

    def _is_confirming() -> bool:
        return state["mode"] == "confirm-delete"

    def _is_renaming() -> bool:
        return state["mode"] == "rename"

    def _refilter() -> None:
        # Don't refilter mid-confirm OR mid-rename — keeps the highlighted
        # row visible while the y/n / rename decision is pending.
        if state["mode"] != "navigate":
            return
        state["filtered"] = filter_rows(state["rows"], state["query"])
        state["selected_idx"] = 0 if state["filtered"] else -1

    search_buffer = Buffer()

    def _on_search_text_changed(_buf):  # noqa: ANN001 — pt fires (sender,)
        state["query"] = search_buffer.text
        _refilter()

    search_buffer.on_text_changed += _on_search_text_changed

    def _scope_label() -> str:
        """Human-readable label for the current scope + branch filter."""
        scope_map = {
            SCOPE_CWD: "current dir",
            SCOPE_REPO: "current repo",
            SCOPE_ALL: "all projects",
        }
        base = scope_map.get(state["scope"], "all projects")
        if state["branch_only"] and current_branch:
            return f"{base} · branch: {current_branch}"
        return base

    def _header_text():
        total = len(state["rows"])
        showing = len(state["filtered"])
        out: list[tuple[str, str]] = [
            ("", "\n  "),
            ("class:header.label", "Resume Session"),
            ("", "  "),
        ]
        if showing == total:
            out.append(("class:header.count", f"({total})"))
        else:
            out.append(("class:header.count", f"({showing} of {total} match)"))
        out.append(("class:header.scope", f"  ·  {_scope_label()}"))
        out.append(("", "\n"))
        return out

    def _divider_text():
        return [("class:divider", "  ─────────────────────────────────────────────  \n")]

    def _footer_text():
        if state["mode"] == "confirm-delete":
            return [
                ("", "  "),
                ("class:footer.key", "y"),
                ("class:footer", " confirm    "),
                ("class:footer.key", "n / esc"),
                ("class:footer", " cancel"),
            ]
        if state["mode"] == "rename":
            return [
                ("", "  "),
                ("class:footer.key", "enter"),
                ("class:footer", " save    "),
                ("class:footer.key", "esc"),
                ("class:footer", " cancel    "),
                ("class:footer", "(leave blank to clear title)"),
            ]
        base = [
            ("", "  "),
            ("class:footer.key", "↑↓"),
            ("class:footer", " navigate    "),
            ("class:footer.key", "enter"),
            ("class:footer", " resume    "),
            ("class:footer.key", "Ctrl+D"),
            ("class:footer", " delete    "),
            ("class:footer.key", "Ctrl+R"),
            ("class:footer", " rename    "),
        ]
        # Scope-toggle hints — only shown when ``refetch`` is wired AND
        # there's somewhere meaningful to widen TO. ``Ctrl+B`` requires
        # an active branch (no point filtering by "no branch").
        if refetch is not None:
            if state["scope"] != SCOPE_ALL:
                base.extend([
                    ("class:footer.key", "Ctrl+W"),
                    ("class:footer", " widen    "),
                    ("class:footer.key", "Ctrl+A"),
                    ("class:footer", " all-projects    "),
                ])
            else:
                # Already at SCOPE_ALL: pressing Ctrl+W/A returns to CWD.
                base.extend([
                    ("class:footer.key", "Ctrl+W"),
                    ("class:footer", " narrow    "),
                ])
            if current_branch:
                base.extend([
                    ("class:footer.key", "Ctrl+B"),
                    (
                        "class:footer",
                        (" branch-off " if state["branch_only"] else " branch    "),
                    ),
                ])
        base.extend([
            ("class:footer.key", "esc"),
            ("class:footer", " cancel"),
        ])
        return base

    def _list_text():
        if not state["filtered"]:
            return [("", "\n"), ("class:empty", "  no sessions match\n")]

        # Compute the visible window from terminal height. Each row in
        # the picker takes 3 lines (title + preview + meta) for full
        # Claude-Code parity, and there are ~9 lines of chrome (header +
        # 4 dividers + search + footer + margin). Floor-divide remaining
        # by 3 to get the visible row count.
        try:
            from prompt_toolkit.application.current import get_app

            term_rows = get_app().output.get_size().rows
        except Exception:  # noqa: BLE001 — picker must render even without app
            term_rows = 24  # safe default
        visible_count = max(1, (term_rows - 9) // 3)

        visible_rows, scroll_offset = compute_visible_window(
            state["filtered"],
            selected_idx=state["selected_idx"],
            window_height=visible_count,
        )
        state["scroll_offset"] = scroll_offset  # surface for tests

        out: list[tuple[str, str]] = [("", "\n")]
        for local_i, row in enumerate(visible_rows):
            absolute_i = scroll_offset + local_i
            is_sel = absolute_i == state["selected_idx"]
            is_confirming = is_sel and state["mode"] == "confirm-delete"
            arrow = "❯ " if is_sel else "  "
            title = format_session_label(row)
            preview = format_session_preview(row)
            # v19 — slot the git branch between "N messages" and the id
            # prefix when present. Pre-v19 rows have ``git_branch == ""``
            # and degrade to the prior 3-segment layout cleanly.
            meta_parts = [
                format_time_ago(row.started_at),
                f"{row.message_count} message{'s' if row.message_count != 1 else ''}",
            ]
            if row.git_branch:
                meta_parts.append(row.git_branch)
            meta_parts.append(row.id[:8])
            meta = "  ·  ".join(meta_parts)
            arrow_cls = "class:row.cursor" if is_sel else "class:row.cursor.dim"
            title_cls = "class:row.title.selected" if is_sel else "class:row.title"
            preview_cls = (
                "class:row.preview.selected" if is_sel else "class:row.preview"
            )
            meta_cls = "class:meta.selected" if is_sel else "class:meta"
            # Line 1: cursor + title (bold) — or confirm-delete prompt
            out.append(("", "  "))  # left padding
            out.append((arrow_cls, arrow))
            if is_confirming:
                out.append(
                    (
                        "class:row.confirm.delete",
                        f"delete '{title[:40]}'? [y / N]\n",
                    )
                )
            else:
                out.append((title_cls, f"{title}\n"))
            # Line 2: preview context (dim) — always present so row
            # height stays uniform. Empty preview becomes a blank line.
            out.append(("", "      "))
            out.append((preview_cls, f"{preview}\n"))
            # Line 3: meta strip (dimmer)
            out.append(("", "      "))
            out.append((meta_cls, f"{meta}\n"))

        # Visual indicator that more rows exist below/above the window —
        # only shown when there's actually overflow, so a small list looks
        # uncluttered.
        total = len(state["filtered"])
        if total > visible_count:
            shown_lo = scroll_offset + 1
            shown_hi = scroll_offset + len(visible_rows)
            out.append(
                ("class:meta", f"\n  showing {shown_lo}-{shown_hi} of {total}\n")
            )
        return out

    kb = KeyBindings()

    # Critical: plain-character bindings ('d', 'y', 'n') would shadow
    # letters typed into the search buffer (focused below). Use Ctrl+D
    # for the delete request (a control sequence, never a typed
    # character) and gate y/n with filter=Condition(_is_confirming) so
    # they only intercept input while the picker is in confirm mode —
    # otherwise they fall through to the search buffer.

    @kb.add(Keys.Up, filter=Condition(_is_navigating))
    def _up(event):  # noqa: ANN001
        if state["filtered"]:
            state["selected_idx"] = max(0, state["selected_idx"] - 1)

    @kb.add(Keys.Down, filter=Condition(_is_navigating))
    def _down(event):  # noqa: ANN001
        if state["filtered"]:
            state["selected_idx"] = min(
                len(state["filtered"]) - 1, state["selected_idx"] + 1
            )

    @kb.add(Keys.Enter, filter=Condition(_is_navigating))
    def _enter(event):  # noqa: ANN001
        if state["filtered"] and 0 <= state["selected_idx"] < len(state["filtered"]):
            sel = state["filtered"][state["selected_idx"]]
            event.app.exit(result=sel.id)
        else:
            event.app.exit(result=None)

    @kb.add(Keys.ControlD, filter=Condition(_is_navigating))
    def _delete_request(event):  # noqa: ANN001
        if state["filtered"]:
            _enter_confirm_delete(state)

    # ─── Phase B — scope-widening + branch-filter shortcuts ──────────
    #
    # All three shortcuts share the same "re-fetch via callback, replace
    # rows, reset search query, clamp cursor" flow. Factor it out so the
    # individual handlers stay one-liners.

    def _refetch_and_replace(*, new_scope: str, branch_only: bool) -> None:
        """Re-pull rows from the callback and swap them into picker state.

        Defensive: if ``refetch`` raises (DB went away, query bug, …),
        we keep the old rows and log to stderr-via-print — the picker
        must NEVER crash mid-session because of a scope toggle.
        """
        if refetch is None:
            return
        try:
            new_rows = refetch(new_scope, branch_only)
        except Exception as exc:  # noqa: BLE001 — UI must not crash
            # Surface the error in the picker's "no sessions" empty
            # state by leaving rows untouched. A more ambitious version
            # would render a transient toast; for now we degrade silently
            # plus log at WARNING via the worktree logger (the only
            # available channel inside an alt-screen Application).
            import logging as _logging

            _logging.getLogger("opencomputer.cli_ui.resume_picker").warning(
                "refetch(scope=%r, branch_only=%r) raised %s; rows unchanged",
                new_scope,
                branch_only,
                exc,
            )
            return
        state["scope"] = new_scope
        state["branch_only"] = branch_only
        state["rows"] = list(new_rows)
        state["query"] = ""
        search_buffer.text = ""  # also clears the search box visually
        state["filtered"] = list(new_rows)
        state["selected_idx"] = 0 if new_rows else -1

    @kb.add(Keys.ControlW, filter=Condition(_is_navigating))
    def _widen_worktree(event):  # noqa: ANN001
        """Toggle between cwd → repo → all → cwd."""
        if refetch is None:
            return
        # CC's contract: Ctrl+W toggles "current repo's worktrees". From
        # cwd we widen to repo; from repo we widen to all; from all we
        # narrow back to cwd. This gives the user a 3-state cycle on a
        # single keystroke without needing a fourth binding.
        next_scope = {
            SCOPE_CWD: SCOPE_REPO,
            SCOPE_REPO: SCOPE_ALL,
            SCOPE_ALL: SCOPE_CWD,
        }.get(state["scope"], SCOPE_CWD)
        _refetch_and_replace(new_scope=next_scope, branch_only=state["branch_only"])

    @kb.add(Keys.ControlA, filter=Condition(_is_navigating))
    def _widen_all(event):  # noqa: ANN001
        """Hard-set scope to ``all`` (Claude Code's ``Ctrl+A``)."""
        if refetch is None:
            return
        # Press twice to return — second press toggles back to CWD,
        # matching Claude Code's "press again to return" contract.
        new_scope = SCOPE_CWD if state["scope"] == SCOPE_ALL else SCOPE_ALL
        _refetch_and_replace(new_scope=new_scope, branch_only=state["branch_only"])

    @kb.add(Keys.ControlB, filter=Condition(_is_navigating))
    def _toggle_branch_filter(event):  # noqa: ANN001
        """Toggle the current-branch filter on / off."""
        if refetch is None or current_branch is None:
            return
        _refetch_and_replace(
            new_scope=state["scope"], branch_only=not state["branch_only"]
        )

    # ─── Phase C — Ctrl+R rename in picker ───────────────────────────

    @kb.add(Keys.ControlR, filter=Condition(_is_navigating))
    def _start_rename(event):  # noqa: ANN001
        """Enter rename mode for the highlighted row.

        Seeds the rename buffer with the row's current title so the
        user can edit (instead of typing from scratch). Refuses to enter
        rename mode when no DB is wired — there's nowhere to commit to.
        """
        if db is None or not state["filtered"] or state["selected_idx"] < 0:
            return
        _enter_rename(state)
        rename_buffer.text = state["rename_seed"]
        # Move cursor to end of seeded text so user can keep typing.
        rename_buffer.cursor_position = len(rename_buffer.text)
        # Focus the rename buffer so keystrokes land there.
        event.app.layout.focus(rename_window)

    @kb.add(Keys.Enter, filter=Condition(_is_renaming))
    def _commit_rename_handler(event):  # noqa: ANN001
        """Commit the new title to DB + return focus to search buffer."""
        new_title = rename_buffer.text
        _commit_rename(state, db, new_title=new_title)
        rename_buffer.text = ""
        event.app.layout.focus(search_window)

    @kb.add(Keys.Escape, eager=True, filter=Condition(_is_renaming))
    def _cancel_rename(event):  # noqa: ANN001
        """Drop the in-progress rename + return to navigate mode."""
        _exit_rename(state)
        rename_buffer.text = ""
        event.app.layout.focus(search_window)

    @kb.add("y", filter=Condition(_is_confirming))
    def _confirm_yes(event):  # noqa: ANN001
        if db is not None:
            _commit_confirm_delete(state, db)

    @kb.add("n", filter=Condition(_is_confirming))
    def _confirm_no(event):  # noqa: ANN001
        _exit_confirm_delete(state)

    @kb.add(
        Keys.Escape,
        eager=True,
        filter=Condition(lambda: state["mode"] != "rename"),
    )
    def _esc(event):  # noqa: ANN001
        # Mid-confirm: Esc cancels the pending delete instead of closing the picker.
        if state["mode"] == "confirm-delete":
            _exit_confirm_delete(state)
            return
        event.app.exit(result=None)

    @kb.add(Keys.ControlC)
    def _ctrl_c(event):  # noqa: ANN001
        event.app.exit(result=None)

    # fzf-inspired aesthetic: no heavy background blocks, bright accent
    # colors only where the eye needs them (cursor + selected title).
    style = Style.from_dict(
        {
            "header.label": "bold #61afef",
            "header.count": "#5f5f5f",
            "header.scope": "#87875f",  # subdued amber — visible but not loud
            "rename.symbol": "bold #d75f87",  # rose — distinct from search amber
            "divider": "#3a3a3a",
            "search.symbol": "bold #61afef",
            "footer": "#5f5f5f",
            "footer.key": "bold #afaf87",
            "row.cursor": "bold #ffaf00",
            "row.cursor.dim": "#3a3a3a",
            "row.title": "#a8a8a8",
            "row.title.selected": "bold #61afef",
            # Preview is the "context" line — dimmer than the title but
            # readable. Selected row brightens to match the title's
            # accent so the eye groups the 3 lines as one entry.
            "row.preview": "#6c6c6c",
            "row.preview.selected": "#a8a8a8",
            "row.confirm.delete": "bold #ff5f5f",
            "meta": "#5f5f5f",
            "meta.selected": "#9e9e9e",
            "empty": "italic #6c6c6c",
        }
    )

    search_control = BufferControl(buffer=search_buffer)
    search_window = Window(
        content=search_control,
        height=1,
    )
    # Search row: a single inline row with a colored magnifier-glass
    # symbol followed by the buffer. Achieved via a VSplit so the symbol
    # has fixed width and the buffer extends.
    from prompt_toolkit.layout import ConditionalContainer, VSplit

    search_label_window = Window(
        content=FormattedTextControl([("class:search.symbol", "  ⌕  ")]),
        height=1,
        dont_extend_width=True,
    )
    search_row = VSplit([search_label_window, search_window])

    # Phase C — rename buffer + row, shown via ConditionalContainer
    # only when mode == "rename". When we enter rename mode we focus
    # the rename buffer; on Enter / Esc we commit-or-cancel and return
    # focus to the search buffer.
    rename_buffer = Buffer()
    rename_label_window = Window(
        content=FormattedTextControl(
            lambda: [
                ("class:rename.symbol", "  ✎  "),
            ]
        ),
        height=1,
        dont_extend_width=True,
    )
    rename_window = Window(content=BufferControl(buffer=rename_buffer), height=1)
    rename_row = VSplit([rename_label_window, rename_window])

    search_row_cond = ConditionalContainer(
        content=search_row,
        filter=Condition(lambda: state["mode"] != "rename"),
    )
    rename_row_cond = ConditionalContainer(
        content=rename_row,
        filter=Condition(_is_renaming),
    )

    layout = Layout(
        HSplit(
            [
                Window(
                    content=FormattedTextControl(_header_text),
                    height=2,  # leading blank line + label
                ),
                Window(
                    content=FormattedTextControl(_divider_text),
                    height=1,
                ),
                search_row_cond,
                rename_row_cond,
                Window(
                    content=FormattedTextControl(_divider_text),
                    height=1,
                ),
                Window(content=FormattedTextControl(_list_text)),
                Window(
                    content=FormattedTextControl(_divider_text),
                    height=1,
                ),
                Window(
                    content=FormattedTextControl(_footer_text),
                    height=1,
                ),
            ]
        ),
        focused_element=search_window,
    )

    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        mouse_support=False,
        style=style,
    )
    return app.run()
