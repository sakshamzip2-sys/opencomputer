"""Phase C — Ctrl+R rename in the resume picker.

The actual key binding lives inside ``run_resume_picker``'s closure and
needs a tty to exercise. The state-machine helpers
(``_enter_rename``, ``_exit_rename``, ``_commit_rename``) are factored
out as module-level functions specifically so tests can drive them
without an Application. Coverage:

    1. ``_enter_rename`` flips mode + seeds the buffer payload.
    2. ``_enter_rename`` is a no-op when nothing's selected.
    3. ``_exit_rename`` returns to navigate cleanly.
    4. ``_commit_rename`` writes via ``db.set_session_title`` AND
       updates the in-memory rows so the picker re-renders the new
       title without a refetch.
    5. ``_commit_rename`` strips whitespace.
    6. ``_commit_rename`` survives DB errors without crashing.
    7. ``_commit_rename`` falls through cleanly when nothing's selected.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.cli_ui.resume_picker import (
    SessionRow,
    _commit_rename,
    _enter_rename,
    _exit_rename,
)


def _row(rid: str, title: str = "") -> SessionRow:
    return SessionRow(
        id=rid, title=title, started_at=time.time(), message_count=1
    )


def _state(rows: list[SessionRow], selected_idx: int = 0) -> dict:
    # Phase H — render_rows is now the selection target. For tests
    # without fork groups, it's just a 1:1 mirror of ``rows`` with
    # indent level 0. (See _build_fork_groups in resume_picker.py for
    # the production rebuild logic.)
    render_rows = [(r, 0) for r in rows]
    return {
        "rows": list(rows),
        "filtered": list(rows),
        "render_rows": render_rows,
        "children_by_parent": {},
        "expanded_parents": set(),
        "selected_idx": selected_idx,
        "mode": "navigate",
        "rename_seed": "",
    }


# ─── _enter_rename ───────────────────────────────────────────────────


def test_enter_rename_flips_mode_and_seeds_buffer_with_current_title() -> None:
    rows = [_row("a", title="Existing Title")]
    state = _state(rows, selected_idx=0)

    _enter_rename(state)

    assert state["mode"] == "rename"
    assert state["rename_seed"] == "Existing Title"


def test_enter_rename_seeds_empty_string_when_no_title() -> None:
    """A row with no title still enters rename mode — seed is just ''."""
    rows = [_row("a", title="")]
    state = _state(rows, selected_idx=0)

    _enter_rename(state)

    assert state["mode"] == "rename"
    assert state["rename_seed"] == ""


def test_enter_rename_is_noop_when_filtered_empty() -> None:
    state = _state([], selected_idx=-1)

    _enter_rename(state)

    assert state["mode"] == "navigate"
    assert state["rename_seed"] == ""


def test_enter_rename_is_noop_when_selected_idx_negative() -> None:
    rows = [_row("a")]
    state = _state(rows, selected_idx=-1)

    _enter_rename(state)

    assert state["mode"] == "navigate"


# ─── _exit_rename ────────────────────────────────────────────────────


def test_exit_rename_returns_to_navigate_and_clears_seed() -> None:
    state = _state([_row("a", "T")], selected_idx=0)
    state["mode"] = "rename"
    state["rename_seed"] = "in-progress"

    _exit_rename(state)

    assert state["mode"] == "navigate"
    assert state["rename_seed"] == ""


# ─── _commit_rename ──────────────────────────────────────────────────


def test_commit_rename_writes_db_and_updates_rows(tmp_path: Path) -> None:
    """Happy path: DB upsert + in-memory row swap."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, title="Original")

    rows = [_row(sid, title="Original")]
    state = _state(rows, selected_idx=0)
    state["mode"] = "rename"

    _commit_rename(state, db, new_title="Renamed")

    assert state["mode"] == "navigate"
    assert state["rename_seed"] == ""
    # DB row reflects the new title.
    persisted = db.get_session(sid)
    assert persisted is not None
    assert persisted["title"] == "Renamed"
    # In-memory rows reflect the new title.
    assert state["rows"][0].title == "Renamed"
    assert state["filtered"][0].title == "Renamed"


def test_commit_rename_strips_whitespace(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid)

    rows = [_row(sid)]
    state = _state(rows, selected_idx=0)
    state["mode"] = "rename"

    _commit_rename(state, db, new_title="   Trimmed   ")

    assert db.get_session(sid)["title"] == "Trimmed"
    assert state["rows"][0].title == "Trimmed"


def test_commit_rename_with_empty_input_clears_title(tmp_path: Path) -> None:
    """Empty / whitespace-only input → title set to empty string."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, title="Original")

    rows = [_row(sid, title="Original")]
    state = _state(rows, selected_idx=0)
    state["mode"] = "rename"

    _commit_rename(state, db, new_title="   ")

    # title is cleared to empty string — picker falls back to other previews.
    assert db.get_session(sid)["title"] == ""
    assert state["rows"][0].title == ""


def test_commit_rename_is_noop_when_no_row_selected(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "sessions.db")
    state = _state([], selected_idx=-1)
    state["mode"] = "rename"

    _commit_rename(state, db, new_title="anything")

    # Returned to navigate, no rows to update.
    assert state["mode"] == "navigate"


def test_commit_rename_logs_and_returns_when_db_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A DB hiccup must NOT crash the picker — log + keep rows intact."""
    import logging

    class _BrokenDB:
        def set_session_title(self, *_a, **_kw) -> None:  # noqa: ANN201
            raise RuntimeError("disk full")

    rows = [_row("a", title="Before")]
    state = _state(rows, selected_idx=0)
    state["mode"] = "rename"

    with caplog.at_level(
        logging.WARNING, logger="opencomputer.cli_ui.resume_picker"
    ):
        _commit_rename(state, _BrokenDB(), new_title="After")  # type: ignore[arg-type]

    # Mode reset, but row title unchanged because the DB write failed.
    assert state["mode"] == "navigate"
    assert state["rows"][0].title == "Before"
    assert any(
        "set_session_title" in r.message and "disk full" in r.message
        for r in caplog.records
    )


def test_commit_rename_preserves_other_rows(tmp_path: Path) -> None:
    """Only the selected row's title changes; siblings are untouched."""
    db = SessionDB(tmp_path / "sessions.db")
    sid_a = uuid.uuid4().hex
    sid_b = uuid.uuid4().hex
    db.ensure_session(sid_a, title="A")
    db.ensure_session(sid_b, title="B")

    rows = [_row(sid_a, "A"), _row(sid_b, "B")]
    state = _state(rows, selected_idx=0)  # selecting A
    state["mode"] = "rename"

    _commit_rename(state, db, new_title="A-renamed")

    assert state["rows"][0].title == "A-renamed"
    assert state["rows"][1].title == "B"  # unchanged
    assert db.get_session(sid_a)["title"] == "A-renamed"
    assert db.get_session(sid_b)["title"] == "B"


def test_commit_rename_preserves_all_other_fields(tmp_path: Path) -> None:
    """SessionRow.git_branch, cwd, first_user_message, etc. survive the swap."""
    db = SessionDB(tmp_path / "sessions.db")
    sid = uuid.uuid4().hex
    db.ensure_session(sid, title="Before")

    rows = [
        SessionRow(
            id=sid,
            title="Before",
            started_at=42.0,
            message_count=5,
            cwd="/home/work",
            first_user_message="hello",
            git_branch="main",
        )
    ]
    state = _state(rows, selected_idx=0)
    state["mode"] = "rename"

    _commit_rename(state, db, new_title="After")

    updated = state["rows"][0]
    assert updated.title == "After"
    # Every other field survives:
    assert updated.id == sid
    assert updated.started_at == 42.0
    assert updated.message_count == 5
    assert updated.cwd == "/home/work"
    assert updated.first_user_message == "hello"
    assert updated.git_branch == "main"
