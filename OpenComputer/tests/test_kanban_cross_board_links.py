"""Tests for cross-board kanban dependencies (Wave 6.E.10)."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from opencomputer.kanban import db


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("OC_KANBAN_WORKSPACES_ROOT", raising=False)
    db.init_db()
    return tmp_path


def _make_board(home: Path, slug: str) -> Path:
    """Create a named board + initialize its DB."""
    target = db.board_db_path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(db_path=target)
    return target


# ---- schema migration ----


def test_task_links_has_new_columns(kanban_home: Path):
    with db.connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_links)")}
    assert "parent_board" in cols
    assert "child_board" in cols


# ---- same-board path (back-compat) ----


def test_same_board_link_still_works(kanban_home: Path):
    with db.connect() as conn:
        a = db.create_task(conn, title="A", body=None, assignee="x")
        b = db.create_task(conn, title="B", body=None, assignee="x")
        db.link_tasks(conn, a, b)
        # b should be 'todo' since a isn't done
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (b,)).fetchone()
        assert row["status"] == "todo"


def test_self_link_rejected(kanban_home: Path):
    with db.connect() as conn:
        a = db.create_task(conn, title="A", body=None, assignee="x")
        with pytest.raises(ValueError):
            db.link_tasks(conn, a, a)


def test_cycle_rejected_within_board(kanban_home: Path):
    with db.connect() as conn:
        a = db.create_task(conn, title="A", body=None, assignee="x")
        b = db.create_task(conn, title="B", body=None, assignee="x")
        db.link_tasks(conn, a, b)
        with pytest.raises(ValueError, match="cycle"):
            db.link_tasks(conn, b, a)


# ---- cross-board path ----


def test_cross_board_link_writes_columns(kanban_home: Path):
    other_db = _make_board(kanban_home, "other")
    # Create parent in 'other'
    with db.connect(other_db) as conn_other:
        parent_id = db.create_task(
            conn_other, title="parent", body=None, assignee="x",
        )
    # Create child in default board
    with db.connect() as conn:
        child_id = db.create_task(
            conn, title="child", body=None, assignee="x",
        )
        db.link_tasks(
            conn, parent_id, child_id,
            parent_board="other",
        )
        row = conn.execute(
            "SELECT parent_id, child_id, parent_board "
            "FROM task_links WHERE child_id = ?",
            (child_id,),
        ).fetchone()
    assert row["parent_id"] == parent_id
    assert row["parent_board"] == "other"


def test_cross_board_unknown_parent_rejected(kanban_home: Path):
    _make_board(kanban_home, "other")
    with db.connect() as conn:
        child = db.create_task(conn, title="C", body=None, assignee="x")
        with pytest.raises(ValueError, match="unknown parent task"):
            db.link_tasks(
                conn, "missing-id", child,
                parent_board="other",
            )


def test_cross_board_missing_db_rejected(kanban_home: Path):
    with db.connect() as conn:
        child = db.create_task(conn, title="C", body=None, assignee="x")
        with pytest.raises(ValueError, match="has no kanban.db"):
            db.link_tasks(
                conn, "any", child,
                parent_board="totally-not-a-real-board",
            )


# ---- recompute_ready cross-board ----


def test_cross_board_holds_in_todo_when_parent_not_done(kanban_home: Path):
    other_db = _make_board(kanban_home, "other")
    with db.connect(other_db) as oc:
        parent_id = db.create_task(oc, title="P", body=None, assignee="x")
        # Leave parent in 'todo'
    with db.connect() as conn:
        child = db.create_task(conn, title="C", body=None, assignee="x")
        db.link_tasks(conn, parent_id, child, parent_board="other")
        # Recompute — child should remain in 'todo'
        promoted = db.recompute_ready(conn)
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child,),
        ).fetchone()
    assert promoted == 0
    assert row["status"] == "todo"


def test_cross_board_promotes_when_parent_done(kanban_home: Path):
    other_db = _make_board(kanban_home, "other")
    with db.connect(other_db) as oc:
        parent_id = db.create_task(oc, title="P", body=None, assignee="x")
        # Mark parent done in remote board
        oc.execute(
            "UPDATE tasks SET status = 'done', completed_at = strftime('%s', 'now') "
            "WHERE id = ?",
            (parent_id,),
        )
        oc.commit()
    with db.connect() as conn:
        child = db.create_task(conn, title="C", body=None, assignee="x")
        db.link_tasks(conn, parent_id, child, parent_board="other")
        promoted = db.recompute_ready(conn)
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child,),
        ).fetchone()
    assert promoted == 1
    assert row["status"] == "ready"


def test_cross_board_holds_when_parent_db_disappears(kanban_home: Path):
    """If the parent board's DB file is deleted, hold the child in todo
    (fail-closed)."""
    other_db = _make_board(kanban_home, "ephemeral")
    with db.connect(other_db) as oc:
        parent_id = db.create_task(oc, title="P", body=None, assignee="x")
    with db.connect() as conn:
        child = db.create_task(conn, title="C", body=None, assignee="x")
        db.link_tasks(conn, parent_id, child, parent_board="ephemeral")
    # Nuke the parent DB
    other_db.unlink()
    with db.connect() as conn:
        promoted = db.recompute_ready(conn)
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child,),
        ).fetchone()
    assert promoted == 0
    assert row["status"] == "todo"


# ---- CLI ----


def _run_cli(verb: str, *argv: str) -> tuple[int, str]:
    from opencomputer.kanban import cli as kbcli
    parser = argparse.ArgumentParser(prog="oc", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
    kbcli.build_parser(sub)
    parsed = parser.parse_args(["kanban", verb, *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = kbcli.kanban_command(parsed) or 0
    return rc, buf.getvalue()


def test_cli_link_with_parent_board(kanban_home: Path):
    other_db = _make_board(kanban_home, "remote")
    with db.connect(other_db) as oc:
        parent_id = db.create_task(oc, title="P", body=None, assignee="x")
    with db.connect() as conn:
        child_id = db.create_task(conn, title="C", body=None, assignee="x")
    rc, out = _run_cli(
        "link", parent_id, child_id, "--parent-board", "remote",
    )
    assert rc == 0
    assert "remote" in out
