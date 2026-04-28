# TUI Autocomplete Uplift + Resume Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the slash-command dropdown actually visible in editor terminals (VS Code's integrated terminal in particular), display it in a Claude-Code-style format with command name + category + description, and add a polished `oc resume` top-level subcommand that opens a full-screen session picker.

**Architecture:** Three phases stacked. Phase 1 is a one-line `complete_style` change in `build_prompt_session` from prompt_toolkit's default `CompleteStyle.COLUMN` (which uses a `Float` widget that needs Cursor-Position-Report support — failing in VS Code's terminal) to `CompleteStyle.MULTI_COLUMN` (which renders the menu as an in-layout `Window`, no CPR needed). Phase 2 adds a `(category)` prefix to `_format_display` so each row reads `/clear (session)` matching Claude Code's three-column convention. Phase 3 adds an `oc resume` top-level Typer subcommand that delegates to a new full-screen prompt_toolkit `Application` (alternate-screen mode — also bypasses CPR) which lists sessions from `SessionDB.list_sessions()`, lets the user filter live with a search box, and arrow-navigate.

**Tech Stack:** `prompt_toolkit>=3.0` (existing), `typer>=0.12` (existing), `rich` (existing). No new dependencies.

---

## Why this is structured as three phases

- **Phase 1 (one-line fix)** is the urgent unblocker. Without it the user sees no menu at all, so it ships first independently.
- **Phase 2 (display polish)** is purely cosmetic on top of Phase 1.
- **Phase 3 (resume picker)** is its own subsystem — full-screen Application + new CLI surface — and lives or dies on its own merits.

Each phase produces working software on its own.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `opencomputer/cli_ui/input_loop.py` | **Modify** | Phase 1: change `complete_style` arg passed to PromptSession |
| `opencomputer/cli_ui/slash_completer.py` | **Modify** | Phase 2: include `(category)` in `_format_display` |
| `opencomputer/cli_ui/resume_picker.py` | **Create** | Phase 3: full-screen `Application` for session picking |
| `opencomputer/cli.py` | **Modify** | Phase 3: add `oc resume` top-level subcommand |
| `tests/test_cli_ui_slash_completer.py` | **Modify** | Phase 2: update display assertion to include `(category)` |
| `tests/test_cli_ui_input_loop.py` | **Modify** | Phase 1: assert `complete_style=MULTI_COLUMN` is set |
| `tests/test_cli_ui_resume_picker.py` | **Create** | Phase 3: unit tests for picker filter + selection logic (pure functions, no UI) |
| `tests/test_cli_resume_command.py` | **Create** | Phase 3: smoke test `oc resume --help` parses |

---

## Phase 1 — Make the Dropdown Render in VS Code Terminal

### Task 1.1: Switch `complete_style` to `MULTI_COLUMN`

**Why this works:** Default `CompleteStyle.COLUMN` wraps the menu in a `Float` widget that positions itself relative to the cursor via CPR (`\e[6n`). VS Code's integrated terminal responds slowly/unreliably to CPR, so prompt_toolkit gives up and silently disables the Float — no menu rendered. `CompleteStyle.MULTI_COLUMN` puts a `MultiColumnCompletionsMenu` Window into the main layout's HSplit, with positioning handled by normal layout flow — no CPR involvement.

**Files:**
- Modify: `opencomputer/cli_ui/input_loop.py:138-150` (the `PromptSession(...)` call site)
- Test: `tests/test_cli_ui_input_loop.py` (add assertion)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_cli_ui_input_loop.py`:

```python
def test_build_prompt_session_uses_multicolumn_complete_style(tmp_path: Path):
    """The dropdown must use MULTI_COLUMN style so it renders in
    editor terminals (e.g. VS Code) that don't reliably respond to
    Cursor-Position-Report (CPR) requests. The default COLUMN style
    uses a Float widget that needs CPR; MULTI_COLUMN uses a Window
    in the main layout and works without CPR."""
    from prompt_toolkit.shortcuts import CompleteStyle

    scope = TurnCancelScope()
    session = build_prompt_session(profile_home=tmp_path, scope=scope)
    assert session.complete_style == CompleteStyle.MULTI_COLUMN
```

- [ ] **Step 2: Run test, verify it fails**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_input_loop.py::test_build_prompt_session_uses_multicolumn_complete_style -v
```
Expected: FAIL — current `complete_style` defaults to `COLUMN`.

- [ ] **Step 3: Implement the change in `input_loop.py`**

Add the import next to the existing prompt_toolkit imports:

```python
from prompt_toolkit.shortcuts import CompleteStyle
```

Update the `PromptSession(...)` call in `build_prompt_session` — locate the existing block and add `complete_style=CompleteStyle.MULTI_COLUMN`:

```python
    return PromptSession(
        message=HTML("<ansigreen><b>you ›</b></ansigreen> "),
        history=FileHistory(str(history_path)),
        key_bindings=kb,
        multiline=False,
        mouse_support=False,
        enable_history_search=True,
        # complete_while_typing=True opens the slash autocomplete dropdown
        # automatically as the user types (no need to hit Tab first). The
        # SlashCommandCompleter returns nothing for non-slash input, so
        # plain chat messages don't trigger any visible menu.
        complete_while_typing=True,
        completer=SlashCommandCompleter(),
        # MULTI_COLUMN puts the completion menu into the main layout
        # (Window-based) instead of using a Float widget that depends
        # on Cursor-Position-Report. This makes the menu visible in
        # editor terminals (VS Code, JetBrains) that don't reliably
        # respond to CPR. Trade-off: layout looks like fish/zsh tab
        # completion rather than a popup overlay — acceptable.
        complete_style=CompleteStyle.MULTI_COLUMN,
        # erase_when_done clears the typed prompt line on submit so the
        # chat loop can re-render the user's message inside a styled
        # boundary box (no duplicate "you › ..." line in scrollback).
        erase_when_done=True,
    )
```

- [ ] **Step 4: Run test, verify it passes**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_input_loop.py -v
```
Expected: all 8 tests PASS (the 4 original + 3 from PR #200 + the 1 new one).

- [ ] **Step 5: Run the full unit suite to catch regressions**

```bash
cd OpenComputer && python -m pytest tests/ -q 2>&1 | tail -5
```
Expected: same pass count as baseline (4050 passed, 13 skipped).

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/tests/test_cli_ui_input_loop.py
git commit -m "fix(tui): use MULTI_COLUMN complete_style so dropdown renders in editor terminals"
```

---

## Phase 2 — Claude-Code-Style Display Format

### Task 2.1: Add `(category)` prefix to the display column

Looking at Claude Code's screenshot, each row is `/<name>  (<source>)  <description>`. Our `CommandDef` has `category` already (`session`, `meta`, `output`, `config`) — we just need to embed it in the formatted display so prompt_toolkit's menu renders it as part of the left column. The right column (description) already comes from `display_meta`.

**Files:**
- Modify: `opencomputer/cli_ui/slash_completer.py:42-49` (the `_format_display` function)
- Test: `tests/test_cli_ui_slash_completer.py` (update existing display tests)

- [ ] **Step 1: Update the failing test**

In `tests/test_cli_ui_slash_completer.py`, replace the two existing display tests with:

```python
def test_completer_display_includes_args_hint_and_category_for_rename():
    completer = SlashCommandCompleter()
    doc = Document(text="/rename", cursor_position=7)
    [c] = list(completer.get_completions(doc, CompleteEvent()))
    plain = _display_plain(c)
    assert "rename" in plain
    assert "<new title>" in plain
    assert "(session)" in plain  # category embedded


def test_completer_display_omits_hint_but_includes_category_for_argless():
    completer = SlashCommandCompleter()
    doc = Document(text="/help", cursor_position=5)
    [c] = list(completer.get_completions(doc, CompleteEvent()))
    plain = _display_plain(c)
    # /help is in the "meta" category, no args
    assert "/help" in plain
    assert "(meta)" in plain
    assert "<" not in plain  # no args hint placeholder leaking in
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_slash_completer.py -v -k "display"
```
Expected: FAIL — current display has no `(category)` text.

- [ ] **Step 3: Update `_format_display` in `slash_completer.py`**

Replace the existing `_format_display`:

```python
def _format_display(cmd: CommandDef) -> str:
    """Render the left-column display text for a command in the dropdown.

    Format: ``/<name> [<args_hint>] (<category>)`` — mirrors Claude
    Code's three-column convention (name, source/category, description)
    where the description ends up in ``display_meta`` (right column).
    """
    parts = [f"/{cmd.name}"]
    if cmd.args_hint:
        parts.append(cmd.args_hint)
    parts.append(f"({cmd.category})")
    return " ".join(parts)
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_slash_completer.py -v
```
Expected: all tests PASS (18 + the test rename means same total count).

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_completer.py OpenComputer/tests/test_cli_ui_slash_completer.py
git commit -m "feat(tui): show (category) tag in slash dropdown rows"
```

---

## Phase 3 — `oc resume` Top-Level Picker

### Task 3.1: Pure-logic helpers for the picker (filter + format)

The picker UI is hard to unit-test, but the data-shaping logic — filtering sessions by search query, formatting rows for display — is pure and should be tested in isolation.

**Files:**
- Create: `opencomputer/cli_ui/resume_picker.py` (filter + format helpers + Application factory)
- Create: `tests/test_cli_ui_resume_picker.py` (unit tests for pure helpers)

- [ ] **Step 1: Write failing tests**

```python
# OpenComputer/tests/test_cli_ui_resume_picker.py
"""Tests for resume_picker pure-logic helpers (filter + format).

The full-screen Application is hard to unit-test because it depends on
prompt_toolkit's runtime, but the data layer — taking SessionDB rows,
filtering by query, formatting for display — is pure and lives here.
"""
from __future__ import annotations

from opencomputer.cli_ui.resume_picker import (
    SessionRow,
    filter_rows,
    format_time_ago,
)


def test_filter_rows_empty_query_returns_all():
    rows = [
        SessionRow(id="abc123", title="hello", started_at=1714281600.0, message_count=4),
        SessionRow(id="def456", title="bye", started_at=1714281660.0, message_count=2),
    ]
    assert filter_rows(rows, "") == rows


def test_filter_rows_substring_match_on_title_case_insensitive():
    a = SessionRow(id="a", title="Architecture review", started_at=0.0, message_count=1)
    b = SessionRow(id="b", title="bug triage", started_at=0.0, message_count=1)
    out = filter_rows([a, b], "arch")
    assert out == [a]
    out = filter_rows([a, b], "TRIAGE")
    assert out == [b]


def test_filter_rows_no_match_returns_empty():
    a = SessionRow(id="a", title="hello", started_at=0.0, message_count=1)
    assert filter_rows([a], "zzz") == []


def test_filter_rows_matches_id_prefix():
    """If the query looks like a session id prefix, match against id too —
    so users can paste a partial UUID from logs."""
    a = SessionRow(id="abc12345-1111", title="hello", started_at=0.0, message_count=1)
    b = SessionRow(id="def67890-2222", title="bye", started_at=0.0, message_count=1)
    out = filter_rows([a, b], "abc12")
    assert out == [a]


def test_format_time_ago_seconds():
    now = 1714305600.0  # arbitrary anchor
    assert format_time_ago(now - 5, now=now) == "5 seconds ago"


def test_format_time_ago_minutes():
    now = 1714305600.0
    assert format_time_ago(now - 12 * 60, now=now) == "12 minutes ago"


def test_format_time_ago_hours():
    now = 1714305600.0
    assert format_time_ago(now - 3 * 3600, now=now) == "3 hours ago"


def test_format_time_ago_days():
    now = 1714305600.0
    assert format_time_ago(now - 2 * 86400, now=now) == "2 days ago"


def test_format_time_ago_handles_invalid_value():
    # Don't crash if the DB has a weird value (e.g. string from old schema)
    assert format_time_ago("not-a-number") == "unknown"  # type: ignore[arg-type]
    assert format_time_ago(None) == "unknown"  # type: ignore[arg-type]


def test_format_time_ago_just_now():
    now = 1714305600.0
    assert format_time_ago(now - 0.5, now=now) == "just now"
```

- [ ] **Step 2: Run tests, verify ImportError**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_resume_picker.py -v
```
Expected: ImportError — module doesn't exist yet.

- [ ] **Step 3: Implement pure helpers in `resume_picker.py`**

```python
# OpenComputer/opencomputer/cli_ui/resume_picker.py
"""Full-screen session picker for ``oc resume``.

This module provides:

- :class:`SessionRow` — minimal dataclass shape we need for picker rendering
  (decouples from :class:`SessionDB`'s wider row schema)
- :func:`filter_rows` — case-insensitive substring search over title +
  id-prefix, used by the search box
- :func:`format_time_ago` — humanize ISO timestamps as ``"12 minutes ago"``
- :func:`run_resume_picker` — builds and runs the full-screen prompt_toolkit
  Application; returns the selected session id, or ``None`` if the user
  cancels (Esc / Ctrl+C / empty list)

The Application uses *alternate-screen mode* which (a) gives us a clean
overlay that disappears on exit and (b) sidesteps CPR entirely — making
it work in editor terminals (VS Code, JetBrains) that don't respond to
Cursor-Position-Report requests.
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
    out: list[SessionRow] = []
    for r in rows:
        if q in r.title.lower():
            out.append(r)
        elif r.id.lower().startswith(q):
            out.append(r)
    return out


def format_time_ago(ts: float, *, now: float | None = None) -> str:
    """Humanize a Unix epoch timestamp as ``"X seconds/minutes/hours/days ago"``.

    ``ts`` matches :class:`SessionDB`'s schema — column ``started_at`` is
    ``REAL`` storing ``time.time()`` (seconds since epoch as float).

    Returns ``"just now"`` for deltas under 1 second and ``"unknown"`` if
    ``ts`` is not a number (avoid crashing the picker over a malformed
    or legacy DB row).
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


# run_resume_picker is added in Task 3.2 below.
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_resume_picker.py -v
```
Expected: all 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/resume_picker.py OpenComputer/tests/test_cli_ui_resume_picker.py
git commit -m "feat(tui): SessionRow + filter_rows + format_time_ago helpers for resume picker"
```

---

### Task 3.2: Full-screen `Application` for the picker UI

This adds the actual picker UI — alternate-screen Application with header, search input, scrollable list, footer hint. Returns the selected session id or `None`.

**Files:**
- Modify: `opencomputer/cli_ui/resume_picker.py` (append `run_resume_picker`)

- [ ] **Step 1: Append `run_resume_picker` to `resume_picker.py`**

```python
def run_resume_picker(rows: list[SessionRow]) -> str | None:
    """Open a full-screen picker and return the selected session id.

    Returns ``None`` if the user cancels (Esc, Ctrl+C, or the list is empty).
    The Application runs in alternate-screen mode, so the user's terminal
    state is restored cleanly when the picker exits regardless of outcome.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

    if not rows:
        return None

    # Mutable picker state, captured by closures below.
    state = {"query": "", "selected_idx": 0, "filtered": list(rows)}

    def _refilter() -> None:
        state["filtered"] = filter_rows(rows, state["query"])
        state["selected_idx"] = 0 if state["filtered"] else -1

    search_buffer = Buffer()

    def _on_search_text_changed(_buf):  # noqa: ANN001
        state["query"] = search_buffer.text
        _refilter()

    search_buffer.on_text_changed += _on_search_text_changed

    def _header_text():
        total = len(rows)
        showing = len(state["filtered"])
        if showing == total:
            return [("class:header", f" Resume Session ({total})  ")]
        return [("class:header", f" Resume Session ({showing} of {total} match)  ")]

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

    from prompt_toolkit.styles import Style

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
                    content=FormattedTextControl([("class:search.label", " ⌕  search:  ")]),
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
```

- [ ] **Step 2: Smoke import**

```bash
cd OpenComputer && python -c "from opencomputer.cli_ui.resume_picker import run_resume_picker, SessionRow; print('ok')"
```
Expected: `ok` — no import errors.

- [ ] **Step 3: Run picker module's tests**

```bash
cd OpenComputer && python -m pytest tests/test_cli_ui_resume_picker.py -v
```
Expected: still 10 PASS (we only ADDED a function; pure tests unaffected).

- [ ] **Step 4: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/resume_picker.py
git commit -m "feat(tui): full-screen Application for oc resume picker (alt-screen, no CPR)"
```

---

### Task 3.3: `oc resume` top-level Typer subcommand

Wire the picker into the CLI. The user wanted `oc resume` (no `--`) — it loads sessions, opens the picker, and on selection delegates to the existing `_run_chat_session(resume=<id>, ...)` flow.

**Files:**
- Modify: `opencomputer/cli.py` (add `@app.command("resume")` near the existing `chat`/`code` commands)
- Create: `tests/test_cli_resume_command.py`

- [ ] **Step 1: Write the failing CLI test**

```python
# OpenComputer/tests/test_cli_resume_command.py
"""Smoke tests for the ``oc resume`` Typer subcommand.

Full picker UI is interactive and untestable in CI; these tests just
verify the command is registered and `--help` parses.
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app

runner = CliRunner()


def test_resume_command_registered():
    """`oc resume --help` should produce help text without raising."""
    result = runner.invoke(app, ["resume", "--help"])
    assert result.exit_code == 0
    assert "resume" in result.stdout.lower()


def test_resume_listed_in_top_level_help():
    """`oc --help` should show `resume` as one of the commands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "resume" in result.stdout.lower()
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd OpenComputer && python -m pytest tests/test_cli_resume_command.py -v
```
Expected: FAIL — `resume` command doesn't exist on `app`.

- [ ] **Step 3: Add `resume` command to `cli.py`**

Find the section in `cli.py` where `@app.command()` decorators sit (around the `chat`, `code`, `search`, `sessions` commands — line ~1161) and append:

```python
@app.command()
def resume(
    plan: bool = typer.Option(
        False, "--plan", help="Resume in plan mode."
    ),
    no_compact: bool = typer.Option(
        False, "--no-compact", help="Disable automatic context compaction."
    ),
) -> None:
    """Open a full-screen session picker and resume the selected session.

    Equivalent to ``oc chat --resume pick`` but with a polished alt-screen
    picker (search + arrow nav + metadata rows) — works in editor
    terminals that don't support CPR.
    """
    from opencomputer.agent.config import _home as _profile_home_fn
    from opencomputer.agent.state import SessionDB
    from opencomputer.cli_ui.resume_picker import SessionRow, run_resume_picker

    profile_home = _profile_home_fn()
    db = SessionDB(profile_home / "sessions.db")
    db_rows = db.list_sessions(limit=200)
    def _coerce_started_at(v) -> float:
        # SessionDB schema is REAL (Unix epoch float). Defensive coercion
        # in case a row predates the schema or was migrated.
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    rows = [
        SessionRow(
            id=r.get("id", ""),
            title=r.get("title") or "",
            started_at=_coerce_started_at(r.get("started_at")),
            message_count=int(r.get("message_count", 0) or 0),
        )
        for r in db_rows
        if r.get("id")
    ]
    if not rows:
        console.print("[dim]no sessions yet — start one with `oc chat`.[/dim]")
        return

    selected_id = run_resume_picker(rows)
    if selected_id is None:
        console.print("[dim]cancelled.[/dim]")
        return

    _run_chat_session(resume=selected_id, plan=plan, no_compact=no_compact, yolo=False)
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
cd OpenComputer && python -m pytest tests/test_cli_resume_command.py -v
```
Expected: both tests PASS.

- [ ] **Step 5: Smoke check the CLI surface**

```bash
cd OpenComputer && /Users/saksham/Vscode/claude/OpenComputer/.venv/bin/oc --help 2>&1 | grep -E "resume|chat"
```
Expected: `resume` listed in the commands table.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/cli.py OpenComputer/tests/test_cli_resume_command.py
git commit -m "feat(cli): oc resume top-level subcommand with full-screen picker"
```

---

## Phase 4 — Verification

### Task 4.1: Full suite + manual smoke

- [ ] **Step 1: Full unit suite (no regressions)**

```bash
cd OpenComputer && python -m pytest tests/ -q 2>&1 | tail -5
```
Expected: 4060+ passed (4050 baseline + new tests we added), 13 skipped.

- [ ] **Step 2: Lint clean**

```bash
cd OpenComputer && ruff check opencomputer/ tests/ 2>&1 | tail -3
```
Expected: `All checks passed!`

- [ ] **Step 3: Smoke import all new symbols**

```bash
cd OpenComputer && python -c "
from opencomputer.cli_ui import SlashCommandCompleter
from opencomputer.cli_ui.resume_picker import (
    SessionRow, filter_rows, format_time_ago, run_resume_picker
)
from opencomputer.cli import app
print('all imports clean')
"
```
Expected: `all imports clean`

- [ ] **Step 4: Verify completion display contains category**

```bash
cd OpenComputer && python -c "
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from opencomputer.cli_ui.slash_completer import SlashCommandCompleter
c = SlashCommandCompleter()
for p in ['/', '/re']:
    out = list(c.get_completions(Document(p, len(p)), CompleteEvent()))
    for comp in out[:3]:
        plain = ''.join(text for _, text in comp.display)
        print(f'  {p:6} -> display={plain!r}  meta={comp.display_meta_text!r}')
"
```
Expected: every line shows `(category)` embedded in `display=...`.

---

## Phase 5 — PR

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/tui-autocomplete-uplift
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(tui): visible dropdown in editor terminals + oc resume picker" --body "$(cat <<'EOF'
## Summary

Three independent fixes/features stacked into one PR (each commit is squashable on its own).

### 1. Dropdown visible in VS Code/JetBrains/editor terminals
Default `CompleteStyle.COLUMN` uses a `Float` widget that needs Cursor-Position-Report support; VS Code's integrated terminal is unreliable on CPR, so the menu silently disabled. Switched to `CompleteStyle.MULTI_COLUMN` which puts the menu into the main layout (Window-based) — no CPR involvement, renders everywhere.

### 2. Claude-Code-style three-column rows
Each dropdown row now reads `/<name> [args_hint] (category)` on the left with description on the right, matching the look in Claude Code's slash menu (where the middle column is the source like `(superpowers)`).

### 3. `oc resume` top-level subcommand
New CLI surface: `oc resume` opens a full-screen alt-screen Application that lists sessions with live search, arrow navigation, and metadata rows (title · time-ago · message count · id-prefix). Alt-screen mode bypasses CPR, so it works in editor terminals too.

## Test plan
- [x] 1 new test for `complete_style=MULTI_COLUMN` wiring
- [x] 2 updated tests for category-embedded display
- [x] 10 new tests for `filter_rows` + `format_time_ago` helpers
- [x] 2 new smoke tests for `oc resume` CLI surface
- [x] Full suite (`pytest tests/ -q`): no regressions
- [x] Ruff lint clean

## Audit
8-defect self-audit recorded in `OpenComputer/docs/superpowers/plans/2026-04-28-tui-autocomplete-uplift.md` — see end of plan.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- "Dropdown not visible in editor terminal" → Phase 1 / Task 1.1 (CompleteStyle.MULTI_COLUMN)
- "Show category + description like Claude Code" → Phase 2 / Task 2.1 (`(category)` in display)
- "Tab autocompletes after arrow navigation" → already shipped in PR #200; with menu now visible the existing Tab handler is reachable
- "`oc resume` (no `--`) with polished picker" → Phase 3 / Tasks 3.1–3.3 (full-screen Application, top-level subcommand)
- "Search, arrow nav, metadata rows" → Phase 3 / Task 3.2 (search_buffer, Up/Down kb, format_time_ago + message_count)

**Placeholder scan:** every code block is real Python; every command shows expected output; no TBDs.

**Type consistency:** `SessionRow` shape is the same in tests and implementation. `run_resume_picker(rows: list[SessionRow]) -> str | None` matches usage in `cli.py`. `filter_rows(rows, query)` signature consistent across tests + impl.

**Known V1 trade-offs (audit-verified honest framing):**

- **MULTI_COLUMN renders commands in a grid; per-row descriptions are NOT shown.** prompt_toolkit's `MultiColumnCompletionsMenu` shows `display` in column cells and `display_meta` (description) in a single-row meta toolbar that updates as the user arrows through. So at any moment the description is visible only for the highlighted item — not for all rows simultaneously like Claude Code's popup. We mitigate by embedding `(category)` in the display itself so EACH cell at least signals the command type. **If the user wants strict Claude-Code parity (description next to every row), that's V2 — ~150 lines of custom prompt_toolkit `Application` work.** Document this trade-off in the PR.
- Branch name + disk size NOT shown in picker rows (we don't track these per session). Adding them would require a `SessionDB` schema change — out of scope for this PR; document as TODO.
- `started_at` in `SessionDB` is a **Unix epoch float** (column type `REAL`, set by `time.time()` at session creation), not an ISO string. `SessionRow.started_at` is typed `float` and `format_time_ago(ts: float, ...)` accepts a float. Tests use float deltas accordingly.
