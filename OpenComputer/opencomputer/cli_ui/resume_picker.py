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


def run_resume_picker(rows: list[SessionRow]) -> str | None:
    """Open a full-screen picker and return the selected session id.

    Returns ``None`` if the user cancels (Esc, Ctrl+C, or empty input).
    Alternate-screen mode is used so the user's terminal state is restored
    cleanly when the picker exits regardless of outcome.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.styles import Style

    if not rows:
        return None

    # Mutable state captured by the closures below. Plain dict keeps the
    # whole picker in one closure scope without needing a class.
    state = {"query": "", "selected_idx": 0, "filtered": list(rows)}

    def _refilter() -> None:
        state["filtered"] = filter_rows(rows, state["query"])
        state["selected_idx"] = 0 if state["filtered"] else -1

    search_buffer = Buffer()

    def _on_search_text_changed(_buf):  # noqa: ANN001 — pt fires (sender,)
        state["query"] = search_buffer.text
        _refilter()

    search_buffer.on_text_changed += _on_search_text_changed

    def _header_text():
        total = len(rows)
        showing = len(state["filtered"])
        if showing == total:
            return [("class:header", f"  Resume Session ({total})  ")]
        return [("class:header", f"  Resume Session ({showing} of {total} match)  ")]

    def _footer_text():
        return [
            (
                "class:footer",
                "  ↑↓  navigate     enter  resume     esc  cancel  ",
            )
        ]

    def _list_text():
        if not state["filtered"]:
            return [("class:empty", "\n  no sessions match\n")]
        out: list[tuple[str, str]] = [("", "\n")]
        for i, row in enumerate(state["filtered"]):
            is_sel = i == state["selected_idx"]
            arrow = "❯ " if is_sel else "  "
            title = row.title or f"(untitled · {row.id[:8]})"
            meta = (
                f"{format_time_ago(row.started_at)}  ·  "
                f"{row.message_count} message{'s' if row.message_count != 1 else ''}  ·  "
                f"{row.id[:8]}"
            )
            row_style = "class:row.selected" if is_sel else "class:row"
            meta_style = "class:meta.selected" if is_sel else "class:meta"
            out.append((row_style, f"{arrow}{title}\n"))
            out.append((meta_style, f"    {meta}\n"))
        return out

    kb = KeyBindings()

    @kb.add(Keys.Up)
    def _up(event):  # noqa: ANN001
        if state["filtered"]:
            state["selected_idx"] = max(0, state["selected_idx"] - 1)

    @kb.add(Keys.Down)
    def _down(event):  # noqa: ANN001
        if state["filtered"]:
            state["selected_idx"] = min(
                len(state["filtered"]) - 1, state["selected_idx"] + 1
            )

    @kb.add(Keys.Enter)
    def _enter(event):  # noqa: ANN001
        if state["filtered"] and 0 <= state["selected_idx"] < len(state["filtered"]):
            sel = state["filtered"][state["selected_idx"]]
            event.app.exit(result=sel.id)
        else:
            event.app.exit(result=None)

    @kb.add(Keys.Escape, eager=True)
    def _esc(event):  # noqa: ANN001
        event.app.exit(result=None)

    @kb.add(Keys.ControlC)
    def _ctrl_c(event):  # noqa: ANN001
        event.app.exit(result=None)

    style = Style.from_dict(
        {
            "header": "bold bg:#005f87 #ffffff",
            "footer": "bg:#262626 #808080",
            "search": "bg:#262626",
            "search.label": "#5fafd7 bold",
            "row": "#d0d0d0",
            "row.selected": "bold #ffffff bg:#005f87",
            "meta": "#6c6c6c",
            "meta.selected": "#bcbcbc bg:#005f87",
            "empty": "#808080 italic",
        }
    )

    search_control = BufferControl(buffer=search_buffer)
    search_window = Window(
        content=search_control,
        height=1,
        style="class:search",
    )
    layout = Layout(
        HSplit(
            [
                Window(
                    content=FormattedTextControl(_header_text),
                    height=1,
                ),
                Window(
                    content=FormattedTextControl(
                        [("class:search.label", "  ⌕  search:  ")]
                    ),
                    height=1,
                    style="class:search",
                ),
                search_window,
                Window(content=FormattedTextControl(_list_text)),
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
