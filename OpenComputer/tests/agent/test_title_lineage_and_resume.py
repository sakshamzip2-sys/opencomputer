"""Tests for C5/C6/C7 — resume by name/title with lineage + helper."""

from pathlib import Path

from opencomputer.agent.state import SessionDB
from opencomputer.agent.title_generator import next_title_in_lineage


def _make_db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "s.db")


def test_find_session_by_title_returns_unique(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_session(session_id="abc", title="hello")
    row = db.find_session_by_title("hello")
    assert row is not None
    assert row["id"] == "abc"


def test_find_session_by_title_missing_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    assert db.find_session_by_title("nope") is None


def test_lineage_query_orders_by_started_desc(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    # create_session sets started_at = time.time() — to control ordering for
    # this test, write a, then b, then c with widening sleeps OR overwrite
    # started_at directly. Direct UPDATE is simpler.
    db.create_session(session_id="a", title="proj")
    db.create_session(session_id="b", title="proj #2")
    db.create_session(session_id="c", title="proj #3")
    # Force deterministic ordering on started_at — DB only has second-resolution
    # timestamps so back-to-back inserts may tie. Re-stamp with explicit values.
    with db._txn() as conn:  # type: ignore[attr-defined]
        conn.execute("UPDATE sessions SET started_at = 1.0 WHERE id = ?", ("a",))
        conn.execute("UPDATE sessions SET started_at = 2.0 WHERE id = ?", ("b",))
        conn.execute("UPDATE sessions SET started_at = 3.0 WHERE id = ?", ("c",))

    rows = db.find_sessions_by_title_lineage("proj")
    assert [r["id"] for r in rows] == ["c", "b", "a"]


def test_lineage_helper_first_returns_base(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    assert next_title_in_lineage(db, "fresh") == "fresh"


def test_lineage_helper_existing_base_bumps_to_2(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_session(session_id="a", title="proj")
    assert next_title_in_lineage(db, "proj") == "proj #2"


def test_lineage_helper_picks_next_n(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.create_session(session_id="a", title="proj")
    db.create_session(session_id="b", title="proj #2")
    db.create_session(session_id="c", title="proj #5")
    assert next_title_in_lineage(db, "proj") == "proj #6"


def test_lineage_helper_handles_db_error_gracefully() -> None:
    class _FlakyDB:
        def find_sessions_by_title_lineage(self, base: str):
            raise RuntimeError("boom")

    assert next_title_in_lineage(_FlakyDB(), "anything") == "anything"


def test_lineage_helper_ignores_non_lineage_titles(tmp_path: Path) -> None:
    """Titles like 'proj #2 extra' must NOT count as lineage members."""
    db = _make_db(tmp_path)
    db.create_session(session_id="a", title="proj")
    # GLOB 'proj #*' will catch 'proj #2 extra' too, but the regex inside
    # next_title_in_lineage should reject it (only `^(.+?)\s+#(\d+)$`).
    db.create_session(session_id="b", title="proj #2 extra")
    # Helper should return "proj #2" because no valid lineage row exists
    # beyond bare 'proj'.
    assert next_title_in_lineage(db, "proj") == "proj #2"
