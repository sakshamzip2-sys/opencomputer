"""Phase H — fork grouping in the resume picker.

Covers:

    1. ``_build_fork_groups`` correctly identifies children-of-visible-parents
       and treats parents-not-in-the-visible-set as if they were roots.
    2. ``SessionRow.parent_session_id`` defaults to ``""`` and is wired
       through cli.py's row construction (verified via integration with
       the DB row dict shape).
    3. The picker's expand/collapse state machine: render_rows is
       rebuilt from filtered + expanded_parents, child indent levels
       are correct, hidden children are absent when their parent is
       collapsed.

The arrow-key handlers (Right / Left) themselves live inside
``run_resume_picker``'s closure and need a tty to drive. We verify
the underlying _build_fork_groups + the rebuild logic that the
arrows trigger, then prove via integration test that the cli.py
row-construction wires ``parent_session_id`` through from the DB.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from opencomputer.agent.state import SessionDB
from opencomputer.cli_ui.resume_picker import (
    SessionRow,
    _build_fork_groups,
)


def _row(rid: str, *, parent: str = "", title: str = "") -> SessionRow:
    return SessionRow(
        id=rid,
        title=title,
        started_at=time.time(),
        message_count=1,
        parent_session_id=parent,
    )


# ─── _build_fork_groups ───────────────────────────────────────────────


def test_no_parents_means_no_groups() -> None:
    rows = [_row("a"), _row("b"), _row("c")]
    by_parent, child_ids = _build_fork_groups(rows)
    assert by_parent == {}
    assert child_ids == set()


def test_groups_children_under_visible_parent() -> None:
    rows = [_row("root"), _row("c1", parent="root"), _row("c2", parent="root")]
    by_parent, child_ids = _build_fork_groups(rows)
    assert set(by_parent.keys()) == {"root"}
    assert [c.id for c in by_parent["root"]] == ["c1", "c2"]
    assert child_ids == {"c1", "c2"}


def test_orphan_child_with_missing_parent_treated_as_root() -> None:
    """A row whose parent isn't in the visible list is NOT grouped under it."""
    rows = [_row("c1", parent="not-visible")]
    by_parent, child_ids = _build_fork_groups(rows)
    # ``not-visible`` doesn't appear in rows, so c1 is rendered at the top level.
    assert by_parent == {}
    assert child_ids == set()


def test_multiple_parents_in_visible_set() -> None:
    rows = [
        _row("r1"),
        _row("r2"),
        _row("c1a", parent="r1"),
        _row("c1b", parent="r1"),
        _row("c2a", parent="r2"),
    ]
    by_parent, child_ids = _build_fork_groups(rows)
    assert set(by_parent.keys()) == {"r1", "r2"}
    assert [c.id for c in by_parent["r1"]] == ["c1a", "c1b"]
    assert [c.id for c in by_parent["r2"]] == ["c2a"]
    assert child_ids == {"c1a", "c1b", "c2a"}


def test_preserves_iteration_order_of_children() -> None:
    """Children should appear in the order they were in the input
    (matches SessionDB's ``ORDER BY started_at DESC`` so most recent
    fork appears first)."""
    rows = [
        _row("root"),
        _row("c-late", parent="root"),
        _row("c-mid", parent="root"),
        _row("c-early", parent="root"),
    ]
    by_parent, _ = _build_fork_groups(rows)
    assert [c.id for c in by_parent["root"]] == ["c-late", "c-mid", "c-early"]


def test_empty_input_returns_empty_dicts() -> None:
    by_parent, child_ids = _build_fork_groups([])
    assert by_parent == {}
    assert child_ids == set()


def test_three_level_chain_only_one_level_of_grouping() -> None:
    """A → B → C: B is grouped under A; C is grouped under B.

    Phase H ships ONE level of nesting. C ends up under B, B ends up
    under A. The renderer indents children one level deep — three-deep
    chains render as A (root) with B (level 1), and C as a child of
    B at level 1 if B is expanded. (Phase H doesn't try to nest 2+
    levels visually — that's deferred until forks-of-forks are common.)
    """
    rows = [_row("a"), _row("b", parent="a"), _row("c", parent="b")]
    by_parent, child_ids = _build_fork_groups(rows)
    assert by_parent["a"] == [rows[1]]
    assert by_parent["b"] == [rows[2]]
    assert child_ids == {"b", "c"}


# ─── SessionRow.parent_session_id ────────────────────────────────────


def test_sessionrow_defaults_parent_to_empty_string() -> None:
    row = SessionRow(id="x", title="", started_at=0.0, message_count=0)
    assert row.parent_session_id == ""


def test_sessionrow_accepts_parent_kwarg() -> None:
    row = SessionRow(
        id="child", title="", started_at=0.0, message_count=0,
        parent_session_id="root",
    )
    assert row.parent_session_id == "root"


# ─── End-to-end: DB row → SessionRow with parent_session_id ──────────


def test_session_row_carries_parent_from_db(tmp_path: Path) -> None:
    """SessionDB.list_sessions_with_preview returns parent_session_id;
    cli.py's _rows_from_db reads it; SessionRow ends up populated."""
    db = SessionDB(tmp_path / "sessions.db")
    db.ensure_session("root-1")
    db.ensure_session("fork-1", parent_session_id="root-1")
    db.ensure_session("fork-2", parent_session_id="root-1")

    rows_dicts = db.list_sessions_with_preview(scope="all")
    # We just verify the field appears on the dict; the cli.py wiring
    # then constructs SessionRow with it.
    by_id = {r["id"]: r for r in rows_dicts}
    assert by_id["fork-1"]["parent_session_id"] == "root-1"
    assert by_id["fork-2"]["parent_session_id"] == "root-1"
    assert by_id["root-1"]["parent_session_id"] is None

    # Now construct SessionRows the same way cli.py does and run them
    # through the grouping helper.
    session_rows = [
        SessionRow(
            id=r["id"],
            title=r["title"] or "",
            started_at=float(r["started_at"] or 0),
            message_count=int(r["message_count"] or 0),
            parent_session_id=r.get("parent_session_id") or "",
        )
        for r in rows_dicts
    ]
    by_parent, child_ids = _build_fork_groups(session_rows)
    assert "root-1" in by_parent
    assert {c.id for c in by_parent["root-1"]} == {"fork-1", "fork-2"}
    assert child_ids == {"fork-1", "fork-2"}


# ─── Render-rows rebuild contract (mirrors closure logic) ────────────


def _rebuild_render_rows_for_test(
    filtered: list[SessionRow], expanded: set[str]
) -> list[tuple[SessionRow, int]]:
    """Mirror of the closure's _rebuild_render_rows for testability.

    The closure version mutates picker state; this pure version
    returns the result directly so tests can pin the contract.
    """
    by_parent, child_ids = _build_fork_groups(filtered)
    render: list[tuple[SessionRow, int]] = []
    for row in filtered:
        if row.id in child_ids:
            continue
        render.append((row, 0))
        if row.id in expanded and row.id in by_parent:
            for child in by_parent[row.id]:
                render.append((child, 1))
    return render


def test_collapsed_parent_hides_children() -> None:
    filtered = [_row("root"), _row("c1", parent="root"), _row("c2", parent="root")]
    rendered = _rebuild_render_rows_for_test(filtered, expanded=set())
    assert [r.id for r, _ in rendered] == ["root"]
    assert [lvl for _, lvl in rendered] == [0]


def test_expanded_parent_shows_children_at_level_1() -> None:
    filtered = [_row("root"), _row("c1", parent="root"), _row("c2", parent="root")]
    rendered = _rebuild_render_rows_for_test(filtered, expanded={"root"})
    assert [r.id for r, _ in rendered] == ["root", "c1", "c2"]
    assert [lvl for _, lvl in rendered] == [0, 1, 1]


def test_only_some_parents_expanded() -> None:
    filtered = [
        _row("r1"),
        _row("c1", parent="r1"),
        _row("r2"),
        _row("c2", parent="r2"),
    ]
    rendered = _rebuild_render_rows_for_test(filtered, expanded={"r1"})
    assert [r.id for r, _ in rendered] == ["r1", "c1", "r2"]
    assert [lvl for _, lvl in rendered] == [0, 1, 0]


def test_orphan_child_renders_at_level_0() -> None:
    """A child whose parent isn't visible appears as a top-level row."""
    filtered = [_row("orphan", parent="not-in-list")]
    rendered = _rebuild_render_rows_for_test(filtered, expanded=set())
    assert [r.id for r, _ in rendered] == ["orphan"]
    assert [lvl for _, lvl in rendered] == [0]


def test_rebuild_is_pure_no_filtered_mutation() -> None:
    """Rebuilding render_rows must not mutate the source ``filtered`` list."""
    filtered = [_row("root"), _row("c1", parent="root")]
    snapshot = list(filtered)
    _rebuild_render_rows_for_test(filtered, expanded={"root"})
    assert filtered == snapshot
