"""v0.5 Task B: append-only HMAC chain over status transitions."""
from __future__ import annotations

from opencomputer.agent.policy_audit_log import GENESIS_HMAC, PolicyAuditLog
from opencomputer.agent.state import SessionDB


def _hmac():
    return b"k" * 32


def _seed_change(db, change_id="c1"):
    """Need a policy_changes row for FK validity."""
    import time
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO policy_changes ("
            "id, ts_drafted, ts_applied, knob_kind, target_id, prev_value, "
            "new_value, reason, expected_effect, rollback_hook, "
            "recommendation_engine_version, approval_mode, hmac_prev, "
            "hmac_self, status) VALUES (?, ?, ?, 'recall_penalty', '1', "
            "'{}', '{}', 'r', 'e', '{}', 'MostCitedBelowMedian/1', "
            "'auto_ttl', '0', 'h', 'drafted')",
            (change_id, time.time(), time.time()),
        )


def test_append_transition_writes_row(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    _seed_change(db)
    with db._connect() as conn:
        log = PolicyAuditLog(conn, _hmac())
        rid = log.append_transition(
            change_id="c1", status="pending_approval",
            actor="cron.engine_tick", reason="initial draft",
        )
        rows = conn.execute(
            "SELECT change_id, status, actor, reason, hmac_prev, hmac_self "
            "FROM policy_audit_log WHERE id = ?", (rid,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "c1"
    assert rows[0][1] == "pending_approval"
    assert rows[0][2] == "cron.engine_tick"
    assert rows[0][3] == "initial draft"
    assert rows[0][4] == GENESIS_HMAC  # first row
    assert rows[0][5] != GENESIS_HMAC  # has actual HMAC


def test_chain_validates_after_multiple_transitions(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    _seed_change(db, "c1")
    _seed_change(db, "c2")

    with db._connect() as conn:
        log = PolicyAuditLog(conn, _hmac())
        log.append_transition(change_id="c1", status="pending_approval")
        log.append_transition(change_id="c1", status="pending_evaluation",
                              actor="user", reason="manual approve")
        log.append_transition(change_id="c2", status="pending_approval")
        log.append_transition(change_id="c1", status="active")
        log.append_transition(change_id="c2", status="reverted",
                              reason="statistical")
        assert log.verify_chain() is True


def test_chain_detects_status_tamper(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    _seed_change(db, "c1")
    with db._connect() as conn:
        log = PolicyAuditLog(conn, _hmac())
        log.append_transition(change_id="c1", status="active")
        # Tamper: rewrite status field
        conn.execute("UPDATE policy_audit_log SET status = 'reverted'")
    with db._connect() as conn:
        log = PolicyAuditLog(conn, _hmac())
        assert log.verify_chain() is False


def test_chain_detects_row_deletion(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    _seed_change(db, "c1")
    with db._connect() as conn:
        log = PolicyAuditLog(conn, _hmac())
        log.append_transition(change_id="c1", status="pending_approval")
        log.append_transition(change_id="c1", status="active")
        log.append_transition(change_id="c1", status="reverted")
        # Delete the middle transition
        conn.execute(
            "DELETE FROM policy_audit_log WHERE status = 'active'"
        )
    with db._connect() as conn:
        log = PolicyAuditLog(conn, _hmac())
        assert log.verify_chain() is False


def test_transitions_for_returns_history(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    _seed_change(db, "c1")
    with db._connect() as conn:
        log = PolicyAuditLog(conn, _hmac())
        log.append_transition(change_id="c1", status="pending_approval")
        log.append_transition(change_id="c1", status="pending_evaluation",
                              actor="user")
        log.append_transition(change_id="c1", status="active")
        history = log.transitions_for("c1")

    statuses = [h["status"] for h in history]
    assert statuses == ["pending_approval", "pending_evaluation", "active"]
    assert history[1]["actor"] == "user"


def test_fk_cascade_on_policy_changes_delete(tmp_path):
    """Deleting the parent policy_changes row removes its audit-log entries."""
    db = SessionDB(tmp_path / "s.db")
    _seed_change(db, "c1")
    with db._connect() as conn:
        log = PolicyAuditLog(conn, _hmac())
        log.append_transition(change_id="c1", status="active")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM policy_changes WHERE id = 'c1'")
        n = conn.execute(
            "SELECT COUNT(*) FROM policy_audit_log WHERE change_id = 'c1'"
        ).fetchone()[0]
    assert n == 0
