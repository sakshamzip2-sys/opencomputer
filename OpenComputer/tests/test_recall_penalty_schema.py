"""P2-1: schema migration v9 — recall_penalty + policy_changes."""
from __future__ import annotations

from opencomputer.agent.state import SCHEMA_VERSION, SessionDB


def _cols(db, table):
    with db._connect() as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_schema_version_at_or_above_9():
    assert SCHEMA_VERSION >= 9


def test_episodic_events_has_recall_penalty(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    cols = _cols(db, "episodic_events")
    assert "recall_penalty" in cols
    assert "recall_penalty_updated_at" in cols


def test_policy_changes_table_exists_with_full_shape(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    cols = _cols(db, "policy_changes")
    expected = {
        "id", "ts_drafted", "ts_applied", "knob_kind", "target_id",
        "prev_value", "new_value", "reason", "expected_effect",
        "revert_after", "rollback_hook", "recommendation_engine_version",
        "approval_mode", "approved_by", "approved_at",
        "hmac_prev", "hmac_self", "status",
        "eligible_turn_count", "pre_change_baseline_mean",
        "pre_change_baseline_std", "post_change_mean",
        "reverted_at", "reverted_reason",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_policy_changes_indices(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    with db._connect() as conn:
        indices = {
            r[1] for r in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index' "
                "AND tbl_name='policy_changes'"
            ).fetchall()
        }
    assert "idx_policy_changes_status" in indices
    assert "idx_policy_changes_target" in indices
    assert "idx_policy_changes_engine" in indices


def test_recall_penalty_default_is_zero(tmp_path):
    """New episodic_events rows must default to recall_penalty = 0.0."""
    import time

    db = SessionDB(tmp_path / "s.db")
    db.create_session("s1", platform="cli", model="m")
    rid = db.record_episodic(session_id="s1", turn_index=0, summary="hi")
    with db._connect() as conn:
        row = conn.execute(
            "SELECT recall_penalty, recall_penalty_updated_at "
            "FROM episodic_events WHERE id = ?",
            (rid,),
        ).fetchone()
    assert row[0] == 0.0
    assert row[1] is None
