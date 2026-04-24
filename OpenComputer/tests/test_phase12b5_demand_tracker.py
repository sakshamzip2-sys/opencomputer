"""Phase 12b.5 — Sub-project E, Task E2.

Tests for ``opencomputer.plugins.demand_tracker.PluginDemandTracker``.

The tracker is a core-only module (plugins MUST NOT import from it) that
records "tool not found" events against the active session DB's
``plugin_demand`` table. It uses E1's ``PluginManifest.tool_names`` to
resolve a tool name to installed-but-disabled plugin candidates without
loading any of them.

All 8 tests are tmp_path-isolated — each gets its own SQLite file and
in-memory fake discover function. No real extensions are loaded here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from opencomputer.plugins.demand_tracker import PluginDemandTracker
from opencomputer.plugins.discovery import PluginCandidate
from plugin_sdk.core import PluginManifest

# ─── fixtures / helpers ───────────────────────────────────────────────


def _mk_candidate(
    plugin_id: str,
    tool_names: tuple[str, ...],
    *,
    root: Path | None = None,
) -> PluginCandidate:
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.1.0",
        entry="plugin",
        kind="tool",
        tool_names=tool_names,
    )
    root_dir = root if root is not None else Path(f"/tmp/fake-{plugin_id}")
    return PluginCandidate(
        manifest=manifest,
        root_dir=root_dir,
        manifest_path=root_dir / "plugin.json",
    )


def _fake_discover(
    candidates: list[PluginCandidate],
) -> Callable[[], list[PluginCandidate]]:
    """Return a callable that yields the given candidates — used in place of
    the real discover() so tests don't touch the filesystem."""

    def _call() -> list[PluginCandidate]:
        return list(candidates)

    return _call


def _count_rows(db_path: Path, where: str = "", params: tuple = ()) -> int:
    sql = "SELECT COUNT(*) FROM plugin_demand"
    if where:
        sql += " WHERE " + where
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def _all_rows(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM plugin_demand").fetchall()]


# ─── 1. table auto-create ─────────────────────────────────────────────


def test_table_auto_created_on_first_use(tmp_path: Path) -> None:
    db = tmp_path / "session.sqlite"
    tracker = PluginDemandTracker(
        db_path=db,
        discover_fn=_fake_discover([]),
    )
    tracker._ensure_table()
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='plugin_demand'"
        ).fetchone()
    assert row is not None, "plugin_demand table should exist after _ensure_table"


# ─── 2. record inserts one row per matching candidate ────────────────


def test_record_tool_not_found_inserts_per_matching_candidate(
    tmp_path: Path,
) -> None:
    db = tmp_path / "session.sqlite"
    candidates = [
        _mk_candidate("alpha", ("Edit",)),
        _mk_candidate("beta", ("Edit", "Write")),
        _mk_candidate("gamma", ("Write",)),  # does NOT declare Edit
    ]
    tracker = PluginDemandTracker(
        db_path=db,
        discover_fn=_fake_discover(candidates),
    )
    tracker.record_tool_not_found("Edit", "s1", 0)

    assert _count_rows(db) == 2
    rows = _all_rows(db)
    ids = {r["plugin_id"] for r in rows}
    assert ids == {"alpha", "beta"}
    assert all(r["tool_name"] == "Edit" for r in rows)
    assert all(r["session_id"] == "s1" for r in rows)
    assert all(r["turn_index"] == 0 for r in rows)


# ─── 3. skip plugins enabled for the active profile ───────────────────


def test_record_tool_not_found_skips_enabled_plugins(tmp_path: Path) -> None:
    db = tmp_path / "session.sqlite"
    candidates = [
        _mk_candidate("alpha", ("Edit",)),
        _mk_candidate("beta", ("Edit",)),
    ]
    tracker = PluginDemandTracker(
        db_path=db,
        discover_fn=_fake_discover(candidates),
        active_profile_plugins=frozenset({"alpha"}),
    )
    tracker.record_tool_not_found("Edit", "s1", 0)

    assert _count_rows(db) == 1
    rows = _all_rows(db)
    assert rows[0]["plugin_id"] == "beta"


# ─── 4. unmatched tool name is a silent no-op ─────────────────────────


def test_record_tool_not_found_is_noop_for_unmatched_tool_name(
    tmp_path: Path,
) -> None:
    db = tmp_path / "session.sqlite"
    candidates = [
        _mk_candidate("alpha", ("Edit",)),
        _mk_candidate("beta", ("Write",)),
    ]
    tracker = PluginDemandTracker(
        db_path=db,
        discover_fn=_fake_discover(candidates),
    )
    tracker.record_tool_not_found("MadeUpTool", "s1", 0)

    assert _count_rows(db) == 0


# ─── 5. threshold gating ──────────────────────────────────────────────


def test_recommended_plugins_respects_threshold(tmp_path: Path) -> None:
    db = tmp_path / "session.sqlite"
    cand_a = _mk_candidate("A", ("ToolA",))
    cand_b = _mk_candidate("B", ("ToolB",))
    tracker = PluginDemandTracker(
        db_path=db,
        discover_fn=_fake_discover([cand_a, cand_b]),
    )
    for turn in range(3):
        tracker.record_tool_not_found("ToolA", "s1", turn)
    tracker.record_tool_not_found("ToolB", "s1", 0)

    recs = tracker.recommended_plugins(threshold=3)
    assert recs == [("A", 3)]


# ─── 6. session scoping ───────────────────────────────────────────────


def test_recommended_plugins_scopes_by_session_id(tmp_path: Path) -> None:
    db = tmp_path / "session.sqlite"
    cand_a = _mk_candidate("A", ("ToolA",))
    tracker = PluginDemandTracker(
        db_path=db,
        discover_fn=_fake_discover([cand_a]),
    )
    for turn in range(4):
        tracker.record_tool_not_found("ToolA", "s1", turn)
    for turn in range(2):
        tracker.record_tool_not_found("ToolA", "s2", turn)

    assert tracker.recommended_plugins(session_id="s1", threshold=3) == [("A", 4)]
    assert tracker.recommended_plugins(session_id="s2", threshold=3) == []
    # Global count combines both sessions.
    assert tracker.recommended_plugins(threshold=3) == [("A", 6)]


# ─── 7. since_turns window ────────────────────────────────────────────


def test_recommended_plugins_scopes_by_since_turns_window(tmp_path: Path) -> None:
    db = tmp_path / "session.sqlite"
    cand_a = _mk_candidate("A", ("ToolA",))
    tracker = PluginDemandTracker(
        db_path=db,
        discover_fn=_fake_discover([cand_a]),
    )
    tracker.record_tool_not_found("ToolA", "s1", 0)
    tracker.record_tool_not_found("ToolA", "s1", 5)
    tracker.record_tool_not_found("ToolA", "s1", 10)

    # max_turn=10, since_turns=6 → keep rows with turn_index >= 4, i.e. turns 5 + 10.
    recs = tracker.recommended_plugins(since_turns=6, threshold=1)
    assert recs == [("A", 2)]


# ─── 8. clear(plugin_id) only affects that plugin ─────────────────────


def test_clear_removes_only_specified_plugin(tmp_path: Path) -> None:
    db = tmp_path / "session.sqlite"
    cand_a = _mk_candidate("A", ("ToolA",))
    cand_b = _mk_candidate("B", ("ToolB",))
    tracker = PluginDemandTracker(
        db_path=db,
        discover_fn=_fake_discover([cand_a, cand_b]),
    )
    for turn in range(3):
        tracker.record_tool_not_found("ToolA", "s1", turn)
    for turn in range(2):
        tracker.record_tool_not_found("ToolB", "s1", turn)

    deleted = tracker.clear("A")
    assert deleted == 3
    assert _count_rows(db, "plugin_id = ?", ("A",)) == 0
    assert _count_rows(db, "plugin_id = ?", ("B",)) == 2
