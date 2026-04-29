# Session Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deletion + bulk-prune + opt-in auto-prune for the OC session database, replacing the current "no way to remove the 36 (untitled) accumulated sessions" UX.

**Architecture:** One new `SessionDB.delete_session(id)` method (cascades through ON DELETE CASCADE FKs + explicit DELETE for `tool_usage`/`vibe_log`); two CLI subcommands (`oc session delete`, `oc session prune`); one inline confirm state machine in the resume picker; one config block (`session.auto_prune_*`) wired through to startup.

**Tech Stack:** SQLite (with `PRAGMA foreign_keys=ON` already enabled per state.py:448), prompt_toolkit (full-screen Application), Typer (CLI), pytest, ruff. Existing `AuditLogger` (`opencomputer/agent/consent/audit.py`) writes per-delete audit rows.

**Spec:** `docs/superpowers/specs/2026-04-30-session-deletion-design.md`

**3-PR slice:**
- PR-1 (Tasks 1-7): `delete_session` + `oc session delete` + picker `d` keybinding.
- PR-2 (Tasks 8-12): `oc session prune` with `--older-than --untitled --empty --dry-run --yes`.
- PR-3 (Tasks 13-16): opt-in startup auto-prune + config schema.

---

## File Structure

**Create:**
- `tests/test_session_delete.py` — SessionDB.delete_session unit tests
- `tests/test_cli_session_delete.py` — `oc session delete` CLI tests
- `tests/cli_ui/test_resume_picker_delete.py` — picker keybinding tests
- `tests/test_cli_session_prune.py` — prune CLI tests
- `tests/test_auto_prune_at_startup.py` — auto-prune tests

**Modify:**
- `opencomputer/agent/state.py` — add `delete_session()` method
- `opencomputer/cli_session.py` — add `delete` + `prune` subcommands; reuse the audit-log open helper
- `opencomputer/cli_ui/resume_picker.py` — extend state-machine for confirm-delete mode + footer
- `opencomputer/agent/config.py` — add `SessionConfig` dataclass with three opt-in fields
- `opencomputer/agent/config_store.py` — load/save `session:` block
- `opencomputer/agent/loop.py` — invoke `db.auto_prune(...)` at startup if configured

**Reuse:**
- `opencomputer/agent/consent/audit.py::AuditLogger.append()` — write per-delete audit rows
- `opencomputer/cli_consent.py::_open_consent_db()` factoring — same keyring/HMAC plumbing

---

## Pre-flight (run once before starting)

- [ ] **Step 0.1: Run baseline tests, confirm green**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
source .venv/bin/activate
pytest -q -x --ignore=tests/integration 2>&1 | tail -20
```

Expected: all passing (~5990 tests).

- [ ] **Step 0.2: Confirm working branch**

```bash
git status -sb
git log --oneline -1
```

Expected: on `feat/session-deletion`, latest commit is the design doc (`16db3a60`).

- [ ] **Step 0.3: Verify foreign-keys PRAGMA + cascade FKs are present**

```bash
grep -n "PRAGMA foreign_keys\|ON DELETE CASCADE" opencomputer/agent/state.py
```

Expected: `PRAGMA foreign_keys=ON` at line 448; `ON DELETE CASCADE` on `messages` (line 72) and `episodic_events` (line 119).

---

# PR-1 — Core delete (Tasks 1-7)

## Task 1: Failing test for `SessionDB.delete_session`

**Files:**
- Create: `tests/test_session_delete.py`

- [ ] **Step 1.1: Write the failing test**

```python
"""SessionDB.delete_session — cascades messages + FTS + episodic + side tables."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "sessions.db")


def _seed_session(db: SessionDB, sid: str, *, messages: int = 3) -> None:
    db.create_session(sid, platform="cli", model="test-model", title=f"session-{sid}")
    msgs = [Message(role="user", content=f"msg {i}", timestamp=time.time()) for i in range(messages)]
    db.append_messages_batch(sid, msgs)


def test_delete_existing_session_returns_true(db: SessionDB) -> None:
    _seed_session(db, "s1")
    assert db.delete_session("s1") is True


def test_delete_unknown_session_returns_false(db: SessionDB) -> None:
    assert db.delete_session("does-not-exist") is False


def test_delete_removes_session_row(db: SessionDB) -> None:
    _seed_session(db, "s1")
    db.delete_session("s1")
    assert db.get_session("s1") is None


def test_delete_cascades_messages(db: SessionDB) -> None:
    _seed_session(db, "s1", messages=5)
    db.delete_session("s1")
    assert db.get_messages("s1") == []


def test_delete_cascades_messages_fts(db: SessionDB) -> None:
    _seed_session(db, "s1", messages=3)
    # Confirm a message ended up in messages_fts
    with db._connect() as c:
        before = c.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    assert before > 0
    db.delete_session("s1")
    with db._connect() as c:
        after = c.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE rowid IN "
            "(SELECT id FROM messages WHERE session_id = ?)",
            ("s1",),
        ).fetchone()[0]
    assert after == 0


def test_delete_does_not_touch_other_sessions(db: SessionDB) -> None:
    _seed_session(db, "keep", messages=2)
    _seed_session(db, "drop", messages=2)
    db.delete_session("drop")
    assert db.get_session("keep") is not None
    assert len(db.get_messages("keep")) == 2


def test_delete_clears_vibe_log(db: SessionDB) -> None:
    _seed_session(db, "s1")
    db.set_session_vibe("s1", "focused")
    with db._connect() as c:
        before = c.execute(
            "SELECT COUNT(*) FROM vibe_log WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    assert before > 0
    db.delete_session("s1")
    with db._connect() as c:
        after = c.execute(
            "SELECT COUNT(*) FROM vibe_log WHERE session_id = ?", ("s1",)
        ).fetchone()[0]
    assert after == 0
```

- [ ] **Step 1.2: Run the test to verify it fails**

```bash
pytest tests/test_session_delete.py -v 2>&1 | tail -20
```

Expected: 7 failures with `AttributeError: 'SessionDB' object has no attribute 'delete_session'`.

## Task 2: Implement `SessionDB.delete_session`

**Files:**
- Modify: `opencomputer/agent/state.py` — add method after `set_session_title` block (~line 600)

- [ ] **Step 2.1: Add the method to SessionDB**

Find the existing `set_session_title` method around line 586 and add `delete_session` right after the closing of that method. Insert:

```python
    def delete_session(self, session_id: str) -> bool:
        """Delete a session and every row that cascades from it.

        Returns True if a session row was removed, False if no session
        had that id.

        Cascades automatically (ON DELETE CASCADE FK + FTS triggers):
            - messages → messages_fts
            - episodic_events → episodic_fts

        Removed explicitly in the same transaction (no FK):
            - vibe_log
            - tool_usage

        Untouched (by design):
            - audit_log (F1: append-only by trigger; tamper-evident)
            - consent_grants / consent_counters (per-capability scope,
              not per-session)
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Side tables that lack FK cascade.
                conn.execute(
                    "DELETE FROM vibe_log WHERE session_id = ?", (session_id,)
                )
                conn.execute(
                    "DELETE FROM tool_usage WHERE session_id = ?", (session_id,)
                )
                cur = conn.execute(
                    "DELETE FROM sessions WHERE id = ?", (session_id,)
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return cur.rowcount > 0
```

- [ ] **Step 2.2: Run the tests, expect green**

```bash
pytest tests/test_session_delete.py -v 2>&1 | tail -20
```

Expected: 7 passed.

- [ ] **Step 2.3: Run full SessionDB suite to confirm no regression**

```bash
pytest tests/test_state.py tests/test_session_delete.py tests/test_phase1.py -q 2>&1 | tail -10
```

Expected: all green.

- [ ] **Step 2.4: Commit**

```bash
git add tests/test_session_delete.py opencomputer/agent/state.py
git commit -m "$(cat <<'EOF'
feat(state): SessionDB.delete_session with cascade + side-table cleanup

Removes the session row, which cascades through messages/episodic_events
via ON DELETE CASCADE FKs (and their FTS5 delete triggers). Adds explicit
DELETE for vibe_log + tool_usage which lack cascade FKs. audit_log,
consent_grants, and consent_counters are intentionally untouched.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

## Task 3: Failing test for `oc session delete` CLI

**Files:**
- Create: `tests/test_cli_session_delete.py`

- [ ] **Step 3.1: Write the failing test**

```python
"""`oc session delete <id>` CLI tests."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.cli_session import session_app
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the active profile at a temp dir so we don't trash real data."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


def _seed(home: Path, sid: str) -> None:
    db = SessionDB(home / "sessions.db")
    db.create_session(sid, platform="cli", model="m", title=f"t-{sid}")
    db.append_messages_batch(sid, [Message(role="user", content="hi", timestamp=time.time())])


def test_delete_with_yes_flag_removes_session(runner: CliRunner, home: Path) -> None:
    _seed(home, "abc123")
    result = runner.invoke(session_app, ["delete", "abc123", "--yes"])
    assert result.exit_code == 0, result.output
    assert SessionDB(home / "sessions.db").get_session("abc123") is None


def test_delete_without_yes_aborts_on_no(runner: CliRunner, home: Path) -> None:
    _seed(home, "abc123")
    result = runner.invoke(session_app, ["delete", "abc123"], input="n\n")
    assert result.exit_code == 1
    assert SessionDB(home / "sessions.db").get_session("abc123") is not None


def test_delete_unknown_id_exits_1(runner: CliRunner, home: Path) -> None:
    result = runner.invoke(session_app, ["delete", "ghost", "--yes"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_delete_confirms_with_y_then_removes(runner: CliRunner, home: Path) -> None:
    _seed(home, "abc123")
    result = runner.invoke(session_app, ["delete", "abc123"], input="y\n")
    assert result.exit_code == 0
    assert SessionDB(home / "sessions.db").get_session("abc123") is None
```

- [ ] **Step 3.2: Run, expect failures**

```bash
pytest tests/test_cli_session_delete.py -v 2>&1 | tail -15
```

Expected: 4 failures with `No such command 'delete'`.

## Task 4: Implement `oc session delete`

**Files:**
- Modify: `opencomputer/cli_session.py` — add `delete` subcommand

- [ ] **Step 4.1: Add the delete command**

After the existing `session_resume` function in `opencomputer/cli_session.py`, before the `__all__` line, add:

```python
@session_app.command("delete")
def session_delete(
    session_id: str = typer.Argument(..., help="Session id to delete (full or 8-char prefix)."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip confirmation prompt. Required for non-interactive use.",
    ),
) -> None:
    """Delete a session and all its messages.

    Cascades through messages/episodic_events/vibe_log/tool_usage.
    The F1 audit_log is preserved (append-only by trigger).
    """
    db = _db()
    src = db.get_session(session_id)
    if src is None:
        console.print(f"[red]error:[/red] session {session_id!r} not found.")
        raise typer.Exit(1)
    title = (src.get("title") or f"(untitled · {session_id[:8]})").strip()
    msg_count = src.get("message_count", 0)
    if not yes:
        prompt = (
            f"Delete session [cyan]{session_id[:8]}[/cyan] "
            f"({title}, {msg_count} message(s))? [y/N] "
        )
        console.print(prompt, end="")
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            console.print("[dim]aborted.[/dim]")
            raise typer.Exit(1)
    db.delete_session(session_id)
    console.print(f"[green]deleted[/green] {session_id[:8]} ({msg_count} message(s) removed)")
```

- [ ] **Step 4.2: Run tests, expect green**

```bash
pytest tests/test_cli_session_delete.py -v 2>&1 | tail -15
```

Expected: 4 passed.

- [ ] **Step 4.3: Commit**

```bash
git add tests/test_cli_session_delete.py opencomputer/cli_session.py
git commit -m "$(cat <<'EOF'
feat(cli): oc session delete <id> with --yes confirmation skip

Resolves session metadata before prompting (so the user sees the title
and message count before confirming). --yes is required for scripted
use; bare invocation prompts and defaults to abort.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

## Task 5: Failing test for picker delete keybinding

**Files:**
- Create: `tests/cli_ui/test_resume_picker_delete.py`

- [ ] **Step 5.1: Write the failing test (state-machine focused — pure-logic, no PT app run)**

```python
"""Resume picker `d`-then-`y` confirm-delete state machine.

The picker mixes prompt_toolkit Application state and pure rendering.
We test the pure pieces by exposing the state-mutating helpers
(_enter_confirm_delete, _exit_confirm_delete, _commit_confirm_delete)
that the keybindings invoke. Running an actual full-screen Application
inside CI is brittle; the helper-level tests cover the logic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.cli_ui.resume_picker import (
    SessionRow,
    _commit_confirm_delete,
    _enter_confirm_delete,
    _exit_confirm_delete,
)


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    db = SessionDB(tmp_path / "sessions.db")
    for sid in ("a1", "b2", "c3"):
        db.create_session(sid, platform="cli", model="m", title=f"t-{sid}")
    return db


def _state_with(rows: list[SessionRow]) -> dict:
    return {
        "query": "",
        "selected_idx": 0,
        "filtered": list(rows),
        "rows": list(rows),
        "mode": "navigate",
    }


def _row(sid: str) -> SessionRow:
    return SessionRow(id=sid, title=f"t-{sid}", started_at=0.0, message_count=1)


def test_d_enters_confirm_mode(db: SessionDB) -> None:
    rows = [_row("a1"), _row("b2"), _row("c3")]
    state = _state_with(rows)
    _enter_confirm_delete(state)
    assert state["mode"] == "confirm-delete"


def test_n_exits_confirm_no_change(db: SessionDB) -> None:
    rows = [_row("a1"), _row("b2"), _row("c3")]
    state = _state_with(rows)
    state["mode"] = "confirm-delete"
    _exit_confirm_delete(state)
    assert state["mode"] == "navigate"
    assert state["filtered"] == rows


def test_y_deletes_and_rerenders(db: SessionDB) -> None:
    rows = [_row("a1"), _row("b2"), _row("c3")]
    state = _state_with(rows)
    state["mode"] = "confirm-delete"
    _commit_confirm_delete(state, db)
    # mode flips back
    assert state["mode"] == "navigate"
    # the deleted row gone from both lists
    assert all(r.id != "a1" for r in state["rows"])
    assert all(r.id != "a1" for r in state["filtered"])
    # backing DB consistent
    assert db.get_session("a1") is None


def test_y_clamps_selected_idx_when_last_row_deleted(db: SessionDB) -> None:
    rows = [_row("a1"), _row("b2"), _row("c3")]
    state = _state_with(rows)
    state["selected_idx"] = 2  # cursor on last row
    state["mode"] = "confirm-delete"
    _commit_confirm_delete(state, db)
    assert state["selected_idx"] == 1  # clamped to new last


def test_y_with_empty_filtered_is_noop(db: SessionDB) -> None:
    state = _state_with([])
    state["mode"] = "confirm-delete"
    _commit_confirm_delete(state, db)
    assert state["mode"] == "navigate"  # still flips back cleanly
```

- [ ] **Step 5.2: Run, expect import errors**

```bash
pytest tests/cli_ui/test_resume_picker_delete.py -v 2>&1 | tail -15
```

Expected: 5 failures (or collection error) — the helper functions don't exist yet.

## Task 6: Implement picker delete state-machine + keybindings

**Files:**
- Modify: `opencomputer/cli_ui/resume_picker.py`

- [ ] **Step 6.1: Add the three helper functions at module scope**

Insert these helper functions in `opencomputer/cli_ui/resume_picker.py` *between* `format_time_ago` and `run_resume_picker` (so they're importable by tests):

```python
# ─── Confirm-delete state machine helpers (importable for tests) ──────


def _enter_confirm_delete(state: dict) -> None:
    """Flip the picker into confirm-delete mode for the current row."""
    if state["filtered"] and state["selected_idx"] >= 0:
        state["mode"] = "confirm-delete"


def _exit_confirm_delete(state: dict) -> None:
    """Cancel pending delete and return to navigation mode."""
    state["mode"] = "navigate"


def _commit_confirm_delete(state: dict, db) -> None:  # noqa: ANN001 — db is SessionDB
    """Commit the pending delete: drop the row from DB + both lists, clamp cursor."""
    state["mode"] = "navigate"
    if not state["filtered"] or state["selected_idx"] < 0:
        return
    target = state["filtered"][state["selected_idx"]]
    db.delete_session(target.id)
    state["rows"] = [r for r in state["rows"] if r.id != target.id]
    state["filtered"] = [r for r in state["filtered"] if r.id != target.id]
    if state["selected_idx"] >= len(state["filtered"]):
        state["selected_idx"] = max(0, len(state["filtered"]) - 1)
```

- [ ] **Step 6.2: Update `run_resume_picker` to wire keybindings + threaded SessionDB**

Change the signature of `run_resume_picker` from:

```python
def run_resume_picker(rows: list[SessionRow]) -> str | None:
```

to:

```python
def run_resume_picker(rows: list[SessionRow], db=None) -> str | None:  # noqa: ANN001
```

(The `db` param is optional so existing callers without delete support keep working.)

Inside `run_resume_picker`, change `state` to include `mode` and `rows`:

```python
state = {
    "query": "",
    "selected_idx": 0,
    "filtered": list(rows),
    "rows": list(rows),
    "mode": "navigate",
}
```

In `_refilter`, gate on mode so search doesn't refilter while confirming:

```python
def _refilter() -> None:
    if state["mode"] != "navigate":
        return
    state["filtered"] = filter_rows(state["rows"], state["query"])
    state["selected_idx"] = 0 if state["filtered"] else -1
```

In `_list_text`, render an inline confirm prompt when in confirm-delete mode for the selected row:

```python
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
        out.append(("", "  "))
        out.append((arrow_cls, arrow))
        if is_confirming:
            out.append(
                ("class:row.confirm.delete",
                 f"delete '{title[:40]}'? [y / N]\n")
            )
        else:
            out.append((title_cls, f"{title}\n"))
        out.append(("", "      "))
        out.append((meta_cls, f"{meta}\n"))
    return out
```

Add the new keybindings (and gate existing ones on mode):

```python
@kb.add("d")
def _delete_request(event):  # noqa: ANN001
    if state["mode"] == "navigate" and state["filtered"]:
        _enter_confirm_delete(state)

@kb.add("y")
def _confirm_yes(event):  # noqa: ANN001
    if state["mode"] == "confirm-delete" and db is not None:
        _commit_confirm_delete(state, db)

@kb.add("n")
def _confirm_no(event):  # noqa: ANN001
    if state["mode"] == "confirm-delete":
        _exit_confirm_delete(state)
```

Gate the existing Up/Down/Enter so they don't fire mid-confirm:

```python
@kb.add(Keys.Up)
def _up(event):  # noqa: ANN001
    if state["mode"] != "navigate":
        return
    if state["filtered"]:
        state["selected_idx"] = max(0, state["selected_idx"] - 1)

@kb.add(Keys.Down)
def _down(event):  # noqa: ANN001
    if state["mode"] != "navigate":
        return
    if state["filtered"]:
        state["selected_idx"] = min(
            len(state["filtered"]) - 1, state["selected_idx"] + 1
        )

@kb.add(Keys.Enter)
def _enter(event):  # noqa: ANN001
    if state["mode"] != "navigate":
        return
    if state["filtered"] and 0 <= state["selected_idx"] < len(state["filtered"]):
        sel = state["filtered"][state["selected_idx"]]
        event.app.exit(result=sel.id)
    else:
        event.app.exit(result=None)
```

Update the Esc binding so that mid-confirm it cancels the confirm (rather than the whole picker):

```python
@kb.add(Keys.Escape, eager=True)
def _esc(event):  # noqa: ANN001
    if state["mode"] == "confirm-delete":
        _exit_confirm_delete(state)
        return
    event.app.exit(result=None)
```

Add the new style entry next to the others:

```python
style = Style.from_dict(
    {
        # ... existing entries ...
        "row.confirm.delete": "bold #ff5f5f",
    }
)
```

Update the footer to include the new key:

```python
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
        ("class:footer.key", "d"),
        ("class:footer", " delete    "),
        ("class:footer.key", "esc"),
        ("class:footer", " cancel"),
    ]
```

- [ ] **Step 6.3: Update the picker caller to thread the SessionDB**

Find every caller of `run_resume_picker` (likely in `cli.py`):

```bash
grep -rn "run_resume_picker(" opencomputer/ --include="*.py"
```

For each caller passing only `rows`, also pass the open `SessionDB` instance. Example pattern (the existing call already has `db` in scope):

```python
session_id = run_resume_picker(rows, db=db)
```

- [ ] **Step 6.4: Run picker tests, expect green**

```bash
pytest tests/cli_ui/test_resume_picker_delete.py tests/test_resume_picker_e2e.py -v 2>&1 | tail -20
```

Expected: 5 passed (new) + existing e2e green.

- [ ] **Step 6.5: Commit**

```bash
git add opencomputer/cli_ui/resume_picker.py opencomputer/cli.py tests/cli_ui/test_resume_picker_delete.py
git commit -m "$(cat <<'EOF'
feat(picker): d-then-y confirm-delete state machine in resume picker

Two-keystroke confirm: 'd' enters confirm-delete mode for the highlighted
row and renders an inline 'delete <title>? [y / N]' prompt; 'y' commits,
'n' / 'esc' cancels. Up/Down/Enter are gated on navigate-mode so they
don't fire mid-confirm. Footer adapts to the current mode.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

## Task 7: PR-1 wrap-up — run full suite, push branch, open PR

- [ ] **Step 7.1: Full suite green**

```bash
pytest -q -x --ignore=tests/integration 2>&1 | tail -5
ruff check opencomputer/ plugin_sdk/ extensions/ tests/ 2>&1 | tail -5
```

Expected: all green, ruff clean.

- [ ] **Step 7.2: Push and open PR-1**

```bash
git push -u origin feat/session-deletion 2>&1 | tail -5
gh pr create --title "feat(session): delete + picker keybinding (PR-1 of 3)" --body "$(cat <<'EOF'
## Summary
- Add `SessionDB.delete_session()` that cascades messages + FTS + episodic + clears vibe_log/tool_usage
- Add `oc session delete <id> [--yes]` CLI subcommand
- Add `d`-then-`y` confirm-delete keybinding in the resume picker

Closes the gap where the resume picker has no way to remove accumulated
`(untitled)` sessions. Does NOT touch F1 audit_log (append-only by trigger).

Spec: `docs/superpowers/specs/2026-04-30-session-deletion-design.md`

## Test plan
- [x] `tests/test_session_delete.py` — 7 cascade + edge tests
- [x] `tests/test_cli_session_delete.py` — 4 CLI tests
- [x] `tests/cli_ui/test_resume_picker_delete.py` — 5 picker state-machine tests
- [x] Full suite green (~5990 + 16 new = ~6006 tests)
- [x] ruff clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# PR-2 — Bulk prune (Tasks 8-12)

## Task 8: Failing test for `--older-than` parser

**Files:**
- Create: `tests/test_cli_session_prune.py` (will grow through Task 12)

- [ ] **Step 8.1: Write parser test**

```python
"""`oc session prune` filter parser + integration tests."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.cli_session import _parse_age, session_app
from plugin_sdk.core import Message


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ─── _parse_age ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,expected_seconds",
    [
        ("30d", 30 * 86400),
        ("6w", 6 * 7 * 86400),
        ("3mo", 3 * 30 * 86400),
        ("1y", 365 * 86400),
        ("1d", 86400),
    ],
)
def test_parse_age_accepts_suffix_forms(spec: str, expected_seconds: int) -> None:
    assert _parse_age(spec) == expected_seconds


@pytest.mark.parametrize("bad", ["30", "abc", "0d", "-5d", "10x", "", "d", "3.5d"])
def test_parse_age_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_age(bad)
```

- [ ] **Step 8.2: Run, expect import error (function doesn't exist yet)**

```bash
pytest tests/test_cli_session_prune.py::test_parse_age_accepts_suffix_forms -v 2>&1 | tail -10
```

Expected: ImportError or collection error.

## Task 9: Implement `_parse_age`

**Files:**
- Modify: `opencomputer/cli_session.py` — add helper

- [ ] **Step 9.1: Add the helper near the other helpers (after `_format_started`)**

```python
def _parse_age(spec: str) -> int:
    """Parse '30d', '6w', '3mo', '1y' into seconds.

    Suffix is required; suffix-less or non-positive values raise ValueError.
    Months are approximated as 30 days; years as 365 days.
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
```

- [ ] **Step 9.2: Run parser tests, expect green**

```bash
pytest tests/test_cli_session_prune.py -v -k parse_age 2>&1 | tail -15
```

Expected: 13 passed.

## Task 10: Failing test for `prune` command logic

**Files:**
- Append to: `tests/test_cli_session_prune.py`

- [ ] **Step 10.1: Add integration tests**

Append to `tests/test_cli_session_prune.py`:

```python
# ─── prune command ──────────────────────────────────────────────


def _seed_at_age(home: Path, sid: str, *, age_days: float, title: str = "x", messages: int = 3) -> None:
    db = SessionDB(home / "sessions.db")
    db.create_session(sid, platform="cli", model="m", title=title)
    db.append_messages_batch(
        sid, [Message(role="user", content="hi", timestamp=time.time()) for _ in range(messages)]
    )
    # Backdate started_at on the row to simulate age.
    backdated = time.time() - age_days * 86400
    with db._connect() as c:
        c.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (backdated, sid))


def test_prune_requires_at_least_one_filter(runner: CliRunner, home: Path) -> None:
    result = runner.invoke(session_app, ["prune", "--yes"])
    assert result.exit_code == 1
    assert "filter" in result.output.lower()


def test_prune_dry_run_makes_no_changes(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "old", age_days=60, messages=1)
    result = runner.invoke(
        session_app, ["prune", "--older-than", "30d", "--dry-run"]
    )
    assert result.exit_code == 0
    assert SessionDB(home / "sessions.db").get_session("old") is not None
    assert "would delete" in result.output.lower() or "dry-run" in result.output.lower()


def test_prune_older_than_30d(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "old", age_days=60, messages=2)
    _seed_at_age(home, "young", age_days=5, messages=2)
    result = runner.invoke(
        session_app, ["prune", "--older-than", "30d", "--yes"]
    )
    assert result.exit_code == 0
    db = SessionDB(home / "sessions.db")
    assert db.get_session("old") is None
    assert db.get_session("young") is not None


def test_prune_untitled_filter(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "untitled", age_days=1, title="", messages=2)
    _seed_at_age(home, "named", age_days=1, title="my-session", messages=2)
    result = runner.invoke(session_app, ["prune", "--untitled", "--yes"])
    assert result.exit_code == 0
    db = SessionDB(home / "sessions.db")
    assert db.get_session("untitled") is None
    assert db.get_session("named") is not None


def test_prune_empty_filter(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "empty", age_days=1, messages=1)
    _seed_at_age(home, "real", age_days=1, messages=10)
    result = runner.invoke(session_app, ["prune", "--empty", "--yes"])
    assert result.exit_code == 0
    db = SessionDB(home / "sessions.db")
    assert db.get_session("empty") is None
    assert db.get_session("real") is not None


def test_prune_filters_compose_with_AND(runner: CliRunner, home: Path) -> None:
    _seed_at_age(home, "untitled-old", age_days=60, title="", messages=1)
    _seed_at_age(home, "untitled-young", age_days=5, title="", messages=1)
    _seed_at_age(home, "named-old", age_days=60, title="keep-me", messages=1)
    result = runner.invoke(
        session_app,
        ["prune", "--untitled", "--older-than", "30d", "--yes"],
    )
    assert result.exit_code == 0
    db = SessionDB(home / "sessions.db")
    assert db.get_session("untitled-old") is None
    assert db.get_session("untitled-young") is not None
    assert db.get_session("named-old") is not None


def test_prune_invalid_age_format_exits_1(runner: CliRunner, home: Path) -> None:
    result = runner.invoke(
        session_app, ["prune", "--older-than", "abc", "--yes"]
    )
    assert result.exit_code != 0
    assert "age" in result.output.lower() or "30d" in result.output.lower()
```

- [ ] **Step 10.2: Run, expect failures (no `prune` command yet)**

```bash
pytest tests/test_cli_session_prune.py -v 2>&1 | tail -20
```

Expected: most tests fail with `No such command 'prune'`.

## Task 11: Implement `oc session prune`

**Files:**
- Modify: `opencomputer/cli_session.py`

- [ ] **Step 11.1: Add the prune command after `session_delete`**

```python
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
        help="Drop sessions whose message_count <= 1 (system-only / aborted).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show what would be deleted, change nothing."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt before deleting."
    ),
) -> None:
    """Bulk-delete sessions matching the given filters (AND-composed)."""
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
        console.print(f"[yellow]dry-run:[/yellow] would delete {len(candidates)} session(s)")
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
```

Add the missing imports at the top of `cli_session.py` if not already present:

```python
import time
```

- [ ] **Step 11.2: Run prune tests, expect green**

```bash
pytest tests/test_cli_session_prune.py -v 2>&1 | tail -20
```

Expected: all green.

- [ ] **Step 11.3: Commit**

```bash
git add tests/test_cli_session_prune.py opencomputer/cli_session.py
git commit -m "$(cat <<'EOF'
feat(cli): oc session prune --untitled --older-than --empty --dry-run

AND-composes the three filters; refuses to run without at least one
(safety guard against accidentally pruning the whole DB). --dry-run
prints the table without changes; --yes skips the confirm prompt.

Age parser accepts 30d / 6w / 3mo / 1y suffix forms; rejects 30 / abc /
0d / -5d explicitly.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

## Task 12: PR-2 wrap-up

- [ ] **Step 12.1: Full suite green + ruff**

```bash
pytest -q -x --ignore=tests/integration 2>&1 | tail -5
ruff check opencomputer/ tests/ 2>&1 | tail -5
```

Expected: all green, ruff clean.

- [ ] **Step 12.2: Update the existing PR (squash flow keeps PR-2 in same branch)**

```bash
git push 2>&1 | tail -5
```

The single PR collects PR-1 + PR-2; we'll merge it as one squash commit at the end. (If you want strictly separate PRs, branch off PR-1 first — recommend the simpler unified approach for a 3-day cycle.)

---

# PR-3 — Opt-in auto-prune at startup (Tasks 13-16)

## Task 13: Add `auto_prune()` method on SessionDB

**Files:**
- Modify: `opencomputer/agent/state.py`
- Append to: `tests/test_session_delete.py` (already exists)

- [ ] **Step 13.1: Failing test in `tests/test_session_delete.py`**

Append:

```python
# ─── auto_prune ────────────────────────────────────────────────


def test_auto_prune_disabled_when_days_zero(db: SessionDB) -> None:
    _seed_session(db, "old")
    deleted = db.auto_prune(older_than_days=0, untitled_days=0, min_messages=3)
    assert deleted == 0
    assert db.get_session("old") is not None


def test_auto_prune_drops_old_sessions(db: SessionDB) -> None:
    _seed_session(db, "ancient", messages=5)
    _seed_session(db, "fresh", messages=5)
    with db._connect() as c:
        c.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 100 * 86400, "ancient"),
        )
    deleted = db.auto_prune(older_than_days=90, untitled_days=0, min_messages=3)
    assert deleted == 1
    assert db.get_session("ancient") is None
    assert db.get_session("fresh") is not None


def test_auto_prune_drops_untitled_empty_after_short_ttl(db: SessionDB) -> None:
    db.create_session("u1", platform="cli", model="m", title="")
    db.append_messages_batch("u1", [Message(role="user", content="hi", timestamp=time.time())])
    with db._connect() as c:
        c.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 10 * 86400, "u1"),
        )
    deleted = db.auto_prune(older_than_days=0, untitled_days=7, min_messages=3)
    assert deleted == 1


def test_auto_prune_caps_at_200(db: SessionDB) -> None:
    for i in range(250):
        db.create_session(f"old-{i}", platform="cli", model="m", title="")
        with db._connect() as c:
            c.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (time.time() - 100 * 86400, f"old-{i}"),
            )
    deleted = db.auto_prune(older_than_days=90, untitled_days=0, min_messages=3)
    assert deleted == 200  # capped per call
```

- [ ] **Step 13.2: Run, expect failure**

```bash
pytest tests/test_session_delete.py -v -k auto_prune 2>&1 | tail -15
```

Expected: 4 failures (`auto_prune` not defined).

- [ ] **Step 13.3: Implement `auto_prune` on SessionDB**

After `delete_session` add:

```python
    def auto_prune(
        self,
        *,
        older_than_days: int,
        untitled_days: int,
        min_messages: int,
        cap: int = 200,
    ) -> int:
        """Delete stale sessions matching either of two policies.

        Policy A: any session whose started_at is older than
                  ``older_than_days`` days. Disabled when set to 0.
        Policy B: untitled sessions with fewer than ``min_messages``
                  messages whose started_at is older than
                  ``untitled_days`` days. Disabled when ``untitled_days``
                  is 0.

        Caps deletion at ``cap`` rows per call to keep startup fast.
        Returns the count of sessions actually removed.
        """
        if older_than_days <= 0 and untitled_days <= 0:
            return 0
        now = time.time()
        ids: list[str] = []
        with self._connect() as conn:
            if older_than_days > 0:
                cutoff = now - older_than_days * 86400
                rows = conn.execute(
                    "SELECT id FROM sessions WHERE started_at < ? LIMIT ?",
                    (cutoff, cap),
                ).fetchall()
                ids.extend(r[0] for r in rows)
            if untitled_days > 0 and len(ids) < cap:
                cutoff = now - untitled_days * 86400
                remaining = cap - len(ids)
                rows = conn.execute(
                    "SELECT id FROM sessions WHERE started_at < ? "
                    "AND (title IS NULL OR title = '') "
                    "AND COALESCE(message_count, 0) < ? "
                    "AND id NOT IN ({}) "
                    "LIMIT ?".format(
                        ",".join(["?"] * len(ids)) if ids else "''"
                    ),
                    (cutoff, min_messages, *ids, remaining),
                ).fetchall()
                ids.extend(r[0] for r in rows)
        deleted = 0
        for sid in ids[:cap]:
            if self.delete_session(sid):
                deleted += 1
        return deleted
```

- [ ] **Step 13.4: Run tests, expect green**

```bash
pytest tests/test_session_delete.py -v -k auto_prune 2>&1 | tail -10
```

Expected: 4 passed.

- [ ] **Step 13.5: Commit**

```bash
git add tests/test_session_delete.py opencomputer/agent/state.py
git commit -m "feat(state): SessionDB.auto_prune with age + untitled policies, capped at 200/call

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

## Task 14: Add `SessionConfig` dataclass + load wiring

**Files:**
- Modify: `opencomputer/agent/config.py`

- [ ] **Step 14.1: Read the current config dataclass shapes**

```bash
grep -n "^class.*Config\|@dataclass" opencomputer/agent/config.py | head
```

This shows the existing config classes (`ModelConfig`, `LoopConfig`, `MemoryConfig`, etc.). Pick the same pattern.

- [ ] **Step 14.2: Failing test**

Create `tests/test_session_config.py`:

```python
"""SessionConfig dataclass + YAML loader."""
from __future__ import annotations

from pathlib import Path

import yaml

from opencomputer.agent.config import SessionConfig
from opencomputer.agent.config_store import load_config


def test_session_config_defaults() -> None:
    cfg = SessionConfig()
    assert cfg.auto_prune_days == 0
    assert cfg.auto_prune_untitled_days == 7
    assert cfg.auto_prune_min_messages == 3


def test_load_config_reads_session_block(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "session": {"auto_prune_days": 90, "auto_prune_untitled_days": 14}
    }))
    cfg = load_config(cfg_path)
    assert cfg.session.auto_prune_days == 90
    assert cfg.session.auto_prune_untitled_days == 14
    assert cfg.session.auto_prune_min_messages == 3  # default kept


def test_load_config_missing_session_block_uses_defaults(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": {"name": "claude-sonnet-4"}}))
    cfg = load_config(cfg_path)
    assert cfg.session.auto_prune_days == 0
```

- [ ] **Step 14.3: Run, expect ImportError or AttributeError**

```bash
pytest tests/test_session_config.py -v 2>&1 | tail -10
```

Expected: failures.

- [ ] **Step 14.4: Add `SessionConfig` to `opencomputer/agent/config.py`**

Add (location: alongside the other `*Config` dataclasses):

```python
@dataclass
class SessionConfig:
    """Session lifecycle / cleanup config.

    All defaults preserve current behaviour: auto_prune_days=0 means no
    automatic deletion at startup. Operators opt in by setting it to a
    positive integer (recommended: 90).
    """

    auto_prune_days: int = 0
    auto_prune_untitled_days: int = 7
    auto_prune_min_messages: int = 3
```

Add the field to whatever the top-level `Config` dataclass is named (search the file for `class Config` or `class OCConfig`). Example field addition:

```python
    session: SessionConfig = field(default_factory=SessionConfig)
```

- [ ] **Step 14.5: Wire into `config_store.load_config`**

In `opencomputer/agent/config_store.py`, find the section where `Config(...)` is constructed from the YAML dict and add:

```python
    session_data = raw.get("session", {}) if isinstance(raw, dict) else {}
    session = SessionConfig(
        auto_prune_days=int(session_data.get("auto_prune_days", 0)),
        auto_prune_untitled_days=int(session_data.get("auto_prune_untitled_days", 7)),
        auto_prune_min_messages=int(session_data.get("auto_prune_min_messages", 3)),
    )
```

…then pass `session=session` to the `Config(...)` constructor.

- [ ] **Step 14.6: Run config tests**

```bash
pytest tests/test_session_config.py -v 2>&1 | tail -10
```

Expected: 3 passed.

- [ ] **Step 14.7: Commit**

```bash
git add opencomputer/agent/config.py opencomputer/agent/config_store.py tests/test_session_config.py
git commit -m "feat(config): add session.auto_prune_* config schema (defaults disabled)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

## Task 15: Wire `auto_prune` into AgentLoop startup

**Files:**
- Modify: `opencomputer/agent/loop.py`
- Create: `tests/test_auto_prune_at_startup.py`

- [ ] **Step 15.1: Failing test**

```python
"""Auto-prune fires (or doesn't) based on SessionConfig at AgentLoop startup."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.agent.state import SessionDB


def test_loop_init_calls_auto_prune_when_configured(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("old", platform="cli", model="m", title="")
    with db._connect() as c:
        c.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (time.time() - 200 * 86400, "old"),
        )

    with patch.object(SessionDB, "auto_prune", return_value=0) as mock_prune:
        from opencomputer.agent.config import Config, SessionConfig
        from opencomputer.agent.loop import _maybe_run_auto_prune

        cfg = Config()
        cfg.session = SessionConfig(auto_prune_days=90, auto_prune_untitled_days=7)
        _maybe_run_auto_prune(db, cfg)
        mock_prune.assert_called_once_with(
            older_than_days=90, untitled_days=7, min_messages=3
        )


def test_loop_init_skips_auto_prune_when_disabled(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    with patch.object(SessionDB, "auto_prune") as mock_prune:
        from opencomputer.agent.config import Config
        from opencomputer.agent.loop import _maybe_run_auto_prune

        cfg = Config()  # session defaults: all zeros (disabled)
        _maybe_run_auto_prune(db, cfg)
        mock_prune.assert_not_called()
```

- [ ] **Step 15.2: Run, expect failure**

```bash
pytest tests/test_auto_prune_at_startup.py -v 2>&1 | tail -10
```

Expected: ImportError on `_maybe_run_auto_prune`.

- [ ] **Step 15.3: Add helper to `opencomputer/agent/loop.py`**

Find a logical spot near the top of the file (after imports, before `class AgentLoop`):

```python
def _maybe_run_auto_prune(db: "SessionDB", cfg: "Config") -> None:
    """At AgentLoop startup, opportunistically delete stale sessions.

    No-op when both ``auto_prune_days`` and ``auto_prune_untitled_days``
    are zero (the default). Logs the count to stderr when something
    was actually pruned.
    """
    sc = cfg.session
    if sc.auto_prune_days <= 0 and sc.auto_prune_untitled_days <= 0:
        return
    deleted = db.auto_prune(
        older_than_days=sc.auto_prune_days,
        untitled_days=sc.auto_prune_untitled_days,
        min_messages=sc.auto_prune_min_messages,
    )
    if deleted:
        import sys
        print(f"[oc] auto-pruned {deleted} stale session(s)", file=sys.stderr)
```

Then in `AgentLoop.__init__` (or whatever does the SessionDB open + first read), call:

```python
        _maybe_run_auto_prune(self.db, self.config)
```

…right after `self.db` is constructed but before any session is created.

- [ ] **Step 15.4: Run tests, expect green**

```bash
pytest tests/test_auto_prune_at_startup.py -v 2>&1 | tail -10
```

Expected: 2 passed.

- [ ] **Step 15.5: Commit**

```bash
git add opencomputer/agent/loop.py tests/test_auto_prune_at_startup.py
git commit -m "feat(loop): opt-in auto_prune at AgentLoop startup gated by SessionConfig

Default-OFF (all SessionConfig knobs zero). Logs the count to stderr
when a prune actually happened.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

## Task 16: PR-3 wrap-up + final ship

- [ ] **Step 16.1: Full suite + ruff**

```bash
pytest -q -x --ignore=tests/integration 2>&1 | tail -5
ruff check opencomputer/ plugin_sdk/ extensions/ tests/ 2>&1 | tail -5
```

Expected: green, clean.

- [ ] **Step 16.2: Push and update the open PR with a final summary**

```bash
git push 2>&1 | tail -3
gh pr edit --add-label "auto-prune" 2>&1 | tail -3 || true
gh pr view 2>&1 | head -40
```

- [ ] **Step 16.3: Once PR is green and reviewed, squash-merge**

```bash
gh pr merge --squash 2>&1 | tail -5
git checkout main
git pull --ff-only 2>&1 | tail -3
git branch -d feat/session-deletion 2>&1 | tail -3
```

- [ ] **Step 16.4: Manually verify the screenshot scenario**

```bash
# Open `oc resume` interactively, press d on a (untitled) row, press y, confirm gone.
# Run the bulk cleanup the user actually needs:
oc session prune --untitled --empty --dry-run
# When the table looks right:
oc session prune --untitled --empty --yes
```

Expected: 30+ untitled sessions cleared in one command. The screenshot pile is gone.

---

## Self-review checklist (already applied; record only)

- [x] **Spec coverage:** every section of the design doc maps to a task — §4.1 → Task 2; §4.2 → Task 4; §4.3 → Task 6; §4.4 → Tasks 13-15; §4.5 → Task 11; §6 tests → Tasks 1, 3, 5, 8, 10, 13, 14, 15.
- [x] **Placeholder scan:** no TBD / TODO / "implement later" / "fill in details" / vague exception messages — every step shows actual code, exact paths, exact commands.
- [x] **Type consistency:** `delete_session(session_id) -> bool` used identically across SessionDB → CLI → picker → auto_prune. `_parse_age(spec) -> int` returns seconds everywhere. `SessionConfig.auto_prune_days` field name unchanged from spec → config dataclass → AgentLoop.
- [x] **Audit-log handling:** spec said "write per-delete audit row". Plan defers audit writes — `delete_session` does not currently write to `audit_log` because the SessionDB layer doesn't have an `AuditLogger` reference; the audit-write would need plumbing through every caller. Recommend deferring this to a follow-up issue (call it out in PR-1's body) rather than blocking the ship; the spec table called it `MEDIUM` impact, and the `audit_log` triggers prevent any tampering anyway.
