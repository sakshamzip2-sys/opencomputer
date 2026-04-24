"""AuditLogger — HMAC-chained append-only log."""
import sqlite3
import tempfile
from pathlib import Path

import pytest

from opencomputer.agent.consent.audit import AuditEvent, AuditLogger
from opencomputer.agent.state import apply_migrations


def _conn():
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    c = sqlite3.connect(tmp, check_same_thread=False)
    apply_migrations(c)
    return c


def test_append_writes_row_with_hmac():
    c = _conn()
    log = AuditLogger(c, hmac_key=b"testkey" * 4)
    evt = AuditEvent(
        session_id="s1", actor="user", action="grant",
        capability_id="read_files", tier=1, scope=None,
        decision="allow", reason="",
    )
    row_id = log.append(evt)
    assert row_id >= 1
    row = c.execute(
        "SELECT row_hmac, prev_hmac FROM audit_log WHERE id=?", (row_id,)
    ).fetchone()
    assert len(row[0]) == 64  # sha256 hex
    assert row[1] == "0" * 64   # first row's prev is genesis


def test_chain_extends_correctly():
    c = _conn()
    log = AuditLogger(c, hmac_key=b"k" * 16)
    log.append(AuditEvent("s1", "user", "grant", "x", 1, None, "allow", ""))
    log.append(AuditEvent("s1", "user", "check", "x", 1, None, "allow", ""))
    rows = c.execute(
        "SELECT id, prev_hmac, row_hmac FROM audit_log ORDER BY id"
    ).fetchall()
    assert rows[1][1] == rows[0][2]  # second row's prev_hmac == first row's row_hmac


def test_verify_chain_ok_on_untouched_log():
    c = _conn()
    log = AuditLogger(c, hmac_key=b"k" * 16)
    log.append(AuditEvent("s1", "user", "grant", "x", 1, None, "allow", ""))
    log.append(AuditEvent("s1", "user", "check", "x", 1, None, "allow", ""))
    assert log.verify_chain() is True


def test_verify_chain_fails_after_direct_row_edit():
    c = _conn()
    log = AuditLogger(c, hmac_key=b"k" * 16)
    log.append(AuditEvent("s1", "user", "grant", "x", 1, None, "allow", ""))
    log.append(AuditEvent("s1", "user", "check", "x", 1, None, "allow", ""))
    # Simulate FS-level tamper — triggers won't block if we drop them first.
    # This mimics what a user with write access to the DB file can do.
    c.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
    c.commit()
    c.execute("UPDATE audit_log SET reason='tampered' WHERE id=1")
    c.commit()
    assert log.verify_chain() is False


def test_update_and_delete_blocked_at_db_level():
    c = _conn()
    log = AuditLogger(c, hmac_key=b"k" * 16)
    log.append(AuditEvent("s1", "user", "grant", "x", 1, None, "allow", ""))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE audit_log SET reason='x'")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM audit_log")


def test_verify_chain_empty_log_is_ok():
    c = _conn()
    log = AuditLogger(c, hmac_key=b"k" * 16)
    assert log.verify_chain() is True


def test_different_keys_produce_different_hmacs():
    c = _conn()
    log1 = AuditLogger(c, hmac_key=b"key1" * 8)
    log1.append(AuditEvent("s1", "user", "grant", "x", 1, None, "allow", ""))
    h1 = c.execute("SELECT row_hmac FROM audit_log WHERE id=1").fetchone()[0]

    c2 = _conn()
    log2 = AuditLogger(c2, hmac_key=b"key2" * 8)
    log2.append(AuditEvent("s1", "user", "grant", "x", 1, None, "allow", ""))
    h2 = c2.execute("SELECT row_hmac FROM audit_log WHERE id=1").fetchone()[0]

    assert h1 != h2
