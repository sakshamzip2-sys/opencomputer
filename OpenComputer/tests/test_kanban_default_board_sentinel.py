"""Tests for Wave 6.E.16 — _default_ sentinel slug for legacy default board."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.kanban import db


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    db.init_db()
    return tmp_path


def _make_board(slug: str) -> Path:
    target = db.board_db_path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(db_path=target)
    return target


# ---- validate_slug accepts the sentinel only ----


def test_sentinel_passes_validation():
    db.validate_slug(db.DEFAULT_BOARD_SENTINEL)
    db.validate_slug("_default_")


@pytest.mark.parametrize("bad", [
    "_other",          # leading underscore not allowed for non-sentinel
    "_default__",      # extra trailing underscore — not the sentinel
    "_DEFAULT_",       # uppercase not allowed
    "_",               # bare underscore alone
    "_default",        # missing trailing underscore
])
def test_underscore_prefix_still_rejected(bad: str):
    with pytest.raises(db.InvalidBoardSlugError):
        db.validate_slug(bad)


# ---- board_db_path maps sentinel to legacy path ----


def test_board_db_path_sentinel_returns_legacy(kanban_home: Path):
    assert db.board_db_path(db.DEFAULT_BOARD_SENTINEL) == kanban_home / "kanban.db"
    assert db.board_db_path(None) == kanban_home / "kanban.db"


def test_board_db_path_named_still_under_boards(kanban_home: Path):
    assert db.board_db_path("foo") == kanban_home / "kanban" / "boards" / "foo" / "kanban.db"


# ---- set_active_board sentinel == None ----


def test_set_active_board_sentinel_clears_state(kanban_home: Path):
    _make_board("named")
    db.set_active_board("named")
    assert db.active_board() == "named"
    db.set_active_board(db.DEFAULT_BOARD_SENTINEL)
    assert db.active_board() is None


# ---- cross-board cycle detection includes the legacy default ----


def test_cycle_default_to_named_to_default(kanban_home: Path):
    """A → B (default → named) followed by B → A (named → default) cycles."""
    other_db = _make_board("other")
    with db.connect() as conn:
        a = db.create_task(conn, title="A-default", body=None, assignee="x")
    with db.connect(other_db) as oc:
        b = db.create_task(oc, title="B-named", body=None, assignee="x")
    # A (default) → B (other). Link stored on default.
    with db.connect() as conn:
        db.link_tasks(
            conn, a, b,
            parent_board=db.DEFAULT_BOARD_SENTINEL, child_board="other",
        )
    # B (other) → A (default) would close the cycle.
    with db.connect(other_db) as oc:
        with pytest.raises(ValueError, match="cycle"):
            db.link_tasks(
                oc, b, a,
                parent_board="other", child_board=db.DEFAULT_BOARD_SENTINEL,
            )


def test_named_to_default_no_cycle_succeeds(kanban_home: Path):
    """Single edge from named → default with no return path should succeed."""
    other_db = _make_board("other")
    with db.connect() as conn:
        a = db.create_task(conn, title="A-default", body=None, assignee="x")
    with db.connect(other_db) as oc:
        b = db.create_task(oc, title="B-named", body=None, assignee="x")
        db.link_tasks(
            oc, b, a,
            parent_board="other", child_board=db.DEFAULT_BOARD_SENTINEL,
        )
