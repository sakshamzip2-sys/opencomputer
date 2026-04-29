"""Resume picker Ctrl+D-then-y confirm-delete state machine.

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


def test_enter_confirm_flips_mode(db: SessionDB) -> None:
    rows = [_row("a1"), _row("b2"), _row("c3")]
    state = _state_with(rows)
    _enter_confirm_delete(state)
    assert state["mode"] == "confirm-delete"


def test_enter_confirm_no_op_when_filtered_empty(db: SessionDB) -> None:
    state = _state_with([])
    _enter_confirm_delete(state)
    # Cannot enter confirm with no row selected — guard prevents wedge.
    assert state["mode"] == "navigate"


def test_exit_confirm_clears_mode(db: SessionDB) -> None:
    rows = [_row("a1")]
    state = _state_with(rows)
    state["mode"] = "confirm-delete"
    _exit_confirm_delete(state)
    assert state["mode"] == "navigate"
    assert state["filtered"] == rows


def test_commit_deletes_and_rerenders(db: SessionDB) -> None:
    rows = [_row("a1"), _row("b2"), _row("c3")]
    state = _state_with(rows)
    state["mode"] = "confirm-delete"
    _commit_confirm_delete(state, db)
    assert state["mode"] == "navigate"
    assert all(r.id != "a1" for r in state["rows"])
    assert all(r.id != "a1" for r in state["filtered"])
    assert db.get_session("a1") is None


def test_commit_clamps_selected_idx_when_last_row_removed(db: SessionDB) -> None:
    rows = [_row("a1"), _row("b2"), _row("c3")]
    state = _state_with(rows)
    state["selected_idx"] = 2  # cursor on last row
    state["mode"] = "confirm-delete"
    _commit_confirm_delete(state, db)
    assert state["selected_idx"] == 1  # clamped to new last


def test_commit_with_empty_filtered_is_safe_noop(db: SessionDB) -> None:
    state = _state_with([])
    state["mode"] = "confirm-delete"
    _commit_confirm_delete(state, db)
    assert state["mode"] == "navigate"
