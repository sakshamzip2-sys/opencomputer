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


def filter_rows(rows: list[SessionRow], query: str) -> list[SessionRow]:
    """Case-insensitive substring filter over title + id prefix.

    Empty query returns all rows unchanged. Matches on either:
    - ``query`` is a substring of ``row.title`` (case-insensitive), OR
    - ``row.id`` starts with ``query`` (case-insensitive — for paste-friendly
      partial-UUID lookups from log scrolls)
    """
    if not query:
        return list(rows)
    q = query.lower()
    return [
        r
        for r in rows
        if q in r.title.lower() or r.id.lower().startswith(q)
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


def run_resume_picker(rows: list[SessionRow], db=None) -> str | None:  # noqa: ANN001
    """Open a full-screen picker and return the selected session id.

    Returns ``None`` if the user cancels (Esc, Ctrl+C, or empty input).
    Alternate-screen mode is used so the user's terminal state is restored
    cleanly when the picker exits regardless of outcome.

    ``db`` is an optional :class:`SessionDB` reference used to commit
    in-picker deletes (Ctrl+D → y). Callers without delete support can
    omit it; pressing Ctrl+D is a no-op when ``db is None``.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.styles import Style

    if not rows:
        return None

    # Mutable state captured by the closures below. Plain dict keeps the
    # whole picker in one closure scope without needing a class.
    # ``mode`` switches between "navigate" (cursor + search) and
    # "confirm-delete" (y/n only). ``rows`` is the canonical mutable
    # backing list — `_commit_confirm_delete` removes from it so a
    # subsequent search won't bring the row back.
    state = {
        "query": "",
        "selected_idx": 0,
        "filtered": list(rows),
        "rows": list(rows),
        "mode": "navigate",
    }

    def _is_navigating() -> bool:
        return state["mode"] == "navigate"

    def _is_confirming() -> bool:
        return state["mode"] == "confirm-delete"

    def _refilter() -> None:
        # Don't refilter mid-confirm — keeps the highlighted row visible
        # while the y/n decision is pending.
        if state["mode"] != "navigate":
            return
        state["filtered"] = filter_rows(state["rows"], state["query"])
        state["selected_idx"] = 0 if state["filtered"] else -1

    search_buffer = Buffer()

    def _on_search_text_changed(_buf):  # noqa: ANN001 — pt fires (sender,)
        state["query"] = search_buffer.text
        _refilter()

    search_buffer.on_text_changed += _on_search_text_changed

    def _header_text():
        total = len(rows)
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
        return [
            ("", "  "),
            ("class:footer.key", "↑↓"),
            ("class:footer", " navigate    "),
            ("class:footer.key", "enter"),
            ("class:footer", " resume    "),
            ("class:footer.key", "Ctrl+D"),
            ("class:footer", " delete    "),
            ("class:footer.key", "esc"),
            ("class:footer", " cancel"),
        ]

    def _list_text():
        if not state["filtered"]:
            return [("", "\n"), ("class:empty", "  no sessions match\n")]
        out: list[tuple[str, str]] = [("", "\n")]
        for i, row in enumerate(state["filtered"]):
            is_sel = i == state["selected_idx"]
            is_confirming = is_sel and state["mode"] == "confirm-delete"
            arrow = "❯ " if is_sel else "  "
            title = row.title or f"(untitled · {row.id[:8]})"
            meta = (
                f"{format_time_ago(row.started_at)}  ·  "
                f"{row.message_count} message{'s' if row.message_count != 1 else ''}  ·  "
                f"{row.id[:8]}"
            )
            arrow_cls = "class:row.cursor" if is_sel else "class:row.cursor.dim"
            title_cls = "class:row.title.selected" if is_sel else "class:row.title"
            meta_cls = "class:meta.selected" if is_sel else "class:meta"
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
            out.append(("", "      "))  # meta indent
            out.append((meta_cls, f"{meta}\n"))
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

    @kb.add("y", filter=Condition(_is_confirming))
    def _confirm_yes(event):  # noqa: ANN001
        if db is not None:
            _commit_confirm_delete(state, db)

    @kb.add("n", filter=Condition(_is_confirming))
    def _confirm_no(event):  # noqa: ANN001
        _exit_confirm_delete(state)

    @kb.add(Keys.Escape, eager=True)
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
            "divider": "#3a3a3a",
            "search.symbol": "bold #61afef",
            "footer": "#5f5f5f",
            "footer.key": "bold #afaf87",
            "row.cursor": "bold #ffaf00",
            "row.cursor.dim": "#3a3a3a",
            "row.title": "#a8a8a8",
            "row.title.selected": "bold #61afef",
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
    from prompt_toolkit.layout import VSplit

    search_label_window = Window(
        content=FormattedTextControl([("class:search.symbol", "  ⌕  ")]),
        height=1,
        dont_extend_width=True,
    )
    search_row = VSplit([search_label_window, search_window])

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
                search_row,
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
