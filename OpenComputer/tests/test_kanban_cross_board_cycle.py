"""Tests for cross-board cycle detection (Wave 6.E.12).

Closes the deferral documented in PR #456.
"""

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


# ---- regressions: same-board still works ----


def test_same_board_cycle_still_rejected(kanban_home: Path):
    with db.connect() as conn:
        a = db.create_task(conn, title="A", body=None, assignee="x")
        b = db.create_task(conn, title="B", body=None, assignee="x")
        db.link_tasks(conn, a, b)
        with pytest.raises(ValueError, match="cycle"):
            db.link_tasks(conn, b, a)


# ---- direct cross-board cycle ----


def test_direct_cross_board_cycle_rejected(kanban_home: Path):
    """A in board-a depends on B in board-b; reverse edge creates cycle.

    Production-grade users name all their boards (the legacy unnamed
    default doesn't participate in cross-board cycle detection because
    it has no addressable slug).
    """
    board_a = _make_board("board-a")
    board_b = _make_board("board-b")
    with db.connect(board_a) as ac:
        a = db.create_task(ac, title="A", body=None, assignee="x")
    with db.connect(board_b) as bc:
        b = db.create_task(bc, title="B", body=None, assignee="x")
    # B (board-b) → A (board-a) — link row stored in board-a
    with db.connect(board_a) as ac:
        db.link_tasks(
            ac, b, a,
            parent_board="board-b", child_board="board-a",
        )
    # Now try the reverse: A → B (cycle across the two boards).
    # Walker starts from B, finds the existing B → A edge, lands on
    # A which equals our proposed parent → cycle.
    with db.connect(board_b) as bc:
        with pytest.raises(ValueError, match="cycle"):
            db.link_tasks(
                bc, a, b,
                parent_board="board-a", child_board="board-b",
            )


def test_indirect_cross_board_cycle_rejected(kanban_home: Path):
    """A → B → C → A across 3 named boards."""
    board_x = _make_board("board-x")
    board_y = _make_board("board-y")
    board_z = _make_board("board-z")
    with db.connect(board_x) as xc:
        a = db.create_task(xc, title="A", body=None, assignee="x")
    with db.connect(board_y) as yc:
        b = db.create_task(yc, title="B", body=None, assignee="x")
    with db.connect(board_z) as zc:
        c = db.create_task(zc, title="C", body=None, assignee="x")
    # A → B (board-x → board-y) — link stored in board-x
    with db.connect(board_x) as xc:
        db.link_tasks(
            xc, a, b,
            parent_board="board-x", child_board="board-y",
        )
    # B → C (board-y → board-z) — link stored in board-y
    with db.connect(board_y) as yc:
        db.link_tasks(
            yc, b, c,
            parent_board="board-y", child_board="board-z",
        )
    # C → A would close the cycle.
    with db.connect(board_z) as zc:
        with pytest.raises(ValueError, match="cycle"):
            db.link_tasks(
                zc, c, a,
                parent_board="board-z", child_board="board-x",
            )


def test_cross_board_no_cycle_works(kanban_home: Path):
    """A → B across boards (no cycle) should succeed."""
    board_a = _make_board("board-a")
    board_b = _make_board("board-b")
    with db.connect(board_b) as bc:
        b = db.create_task(bc, title="B", body=None, assignee="x")
    with db.connect(board_a) as ac:
        a = db.create_task(ac, title="A", body=None, assignee="x")
        db.link_tasks(
            ac, b, a,
            parent_board="board-b", child_board="board-a",
        )


# ---- helper unit tests ----


def test_would_cycle_global_returns_false_for_empty_graph(kanban_home: Path):
    with db.connect() as conn:
        a = db.create_task(conn, title="A", body=None, assignee="x")
        b = db.create_task(conn, title="B", body=None, assignee="x")
        result = db._would_cycle_global(
            conn,
            parent_id=a, child_id=b,
            parent_board=None, child_board=None,
        )
        assert result is False


def test_would_cycle_global_caps_at_max_hops(kanban_home: Path, monkeypatch):
    """Pathological data with > MAX_HOPS distinct edges fails closed."""
    monkeypatch.setattr(db, "MAX_CROSS_BOARD_HOPS", 3)
    with db.connect() as conn:
        ids = [
            db.create_task(conn, title=f"t{i}", body=None, assignee="x")
            for i in range(8)
        ]
        # Build chain t0 → t1 → t2 → t3 → t4 → t5 → t6 → t7
        for i in range(len(ids) - 1):
            db.link_tasks(conn, ids[i], ids[i + 1])
        # Now query the global walker for a non-cycle that exceeds depth.
        result = db._would_cycle_global(
            conn,
            parent_id="totally-fake",
            child_id=ids[0],
            parent_board=None, child_board=None,
        )
    # We should fail-closed at the cap (return True).
    assert result is True


def test_would_cycle_global_unreachable_board_treated_as_leaf(kanban_home: Path):
    """A link references a missing board's slug — that path contributes no
    descendants; the walker doesn't crash."""
    with db.connect() as conn:
        a = db.create_task(conn, title="A", body=None, assignee="x")
        b = db.create_task(conn, title="B", body=None, assignee="x")
        # Plant a synthetic link with a non-existent board for b
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id, parent_board, child_board) "
            "VALUES (?, ?, ?, ?)",
            (a, b, None, "ghost-board"),
        )
        result = db._would_cycle_global(
            conn,
            parent_id="anything",
            child_id=a,
            parent_board=None, child_board=None,
        )
    assert result is False
