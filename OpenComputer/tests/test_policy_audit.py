"""P2-3: HMAC-chained policy_audit log."""
from __future__ import annotations

from opencomputer.agent.policy_audit import (
    PolicyAuditLogger,
    PolicyChangeEvent,
)
from opencomputer.agent.policy_audit_key import get_policy_audit_hmac_key
from opencomputer.agent.state import SessionDB


def _evt(target="ep_x"):
    return PolicyChangeEvent(
        knob_kind="recall_penalty",
        target_id=target,
        prev_value='{"recall_penalty": 0.0}',
        new_value='{"recall_penalty": 0.2}',
        reason="MostCitedBelowMedian/1: cited 5x, mean 0.31 vs median 0.62",
        expected_effect="raise mean turn_score by ~0.1",
        rollback_hook='{"action":"set","field":"recall_penalty","value":0.0}',
        recommendation_engine_version="MostCitedBelowMedian/1",
        approval_mode="explicit",
    )


def test_append_drafted_writes_row(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, hmac_key=b"k" * 32)
        rid = log.append_drafted(_evt())
        rows = conn.execute(
            "SELECT id, status, hmac_prev, hmac_self FROM policy_changes"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == rid
    assert rows[0][1] == "drafted"
    assert rows[0][2] == "0" * 64  # genesis prev
    assert rows[0][3] != "0" * 64  # actual hmac


def test_chain_validates_after_multiple_inserts(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, hmac_key=b"k" * 32)
        for i in range(3):
            log.append_drafted(_evt(target=f"ep_{i}"))
        assert log.verify_chain() is True


def test_chain_detects_tamper(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, hmac_key=b"k" * 32)
        log.append_drafted(_evt())
        # Tamper: rewrite the reason field
        conn.execute("UPDATE policy_changes SET reason = 'TAMPERED'")
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, hmac_key=b"k" * 32)
        assert log.verify_chain() is False


def test_status_transition_logs_status_and_chain_still_validates(tmp_path):
    """Status transitions UPDATE the row's status fields but do NOT
    re-chain (v0 design: chain protects as-drafted content; status flips
    are logged via UPDATE; cryptographic chain of status transitions is
    a v0.5 item)."""
    db = SessionDB(tmp_path / "s.db")
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, hmac_key=b"k" * 32)
        rid = log.append_drafted(_evt())
        first_hmac = conn.execute(
            "SELECT hmac_self FROM policy_changes WHERE id = ?", (rid,)
        ).fetchone()[0]

        log.append_status_transition(
            rid, "pending_approval", approved_by="user",
        )
        second_hmac = conn.execute(
            "SELECT hmac_self FROM policy_changes WHERE id = ?", (rid,)
        ).fetchone()[0]
        status = conn.execute(
            "SELECT status, approved_by FROM policy_changes WHERE id = ?",
            (rid,),
        ).fetchone()

    # hmac unchanged — chain protects as-drafted, not transitions
    assert first_hmac == second_hmac
    assert status[0] == "pending_approval"
    assert status[1] == "user"

    # Chain still validates
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, hmac_key=b"k" * 32)
        assert log.verify_chain() is True


def test_revert_records_reason(tmp_path):
    db = SessionDB(tmp_path / "s.db")
    with db._connect() as conn:
        log = PolicyAuditLogger(conn, hmac_key=b"k" * 32)
        rid = log.append_drafted(_evt())
        log.append_status_transition(
            rid, "reverted",
            reverted_reason="statistical: post_mean below baseline",
        )
        row = conn.execute(
            "SELECT status, reverted_at, reverted_reason "
            "FROM policy_changes WHERE id = ?", (rid,)
        ).fetchone()
    assert row[0] == "reverted"
    assert row[1] is not None
    assert "statistical" in row[2]


def test_get_policy_audit_hmac_key_is_32_bytes(tmp_path):
    key = get_policy_audit_hmac_key(tmp_path)
    assert len(key) == 32


def test_get_policy_audit_hmac_key_is_stable(tmp_path):
    k1 = get_policy_audit_hmac_key(tmp_path)
    k2 = get_policy_audit_hmac_key(tmp_path)
    assert k1 == k2
