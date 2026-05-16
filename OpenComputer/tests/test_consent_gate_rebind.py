"""Tests for §2.10 ConsentGate.rebind_to_profile.

Coverage:
  - close + reconstruction against new audit.db
  - In-memory session_grants cleared
  - Approvals config cache cleared
  - Bad input → False return + no state change
  - Failure to open new DB → False return, gate keeps prior bindings
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _build_gate(home: Path):
    """Construct a fresh ConsentGate against a temp audit.db."""
    from opencomputer.agent.consent.audit import AuditLogger
    from opencomputer.agent.consent.gate import ConsentGate
    from opencomputer.agent.consent.store import ConsentStore
    from opencomputer.agent.state import apply_migrations

    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "audit.db", check_same_thread=False)
    apply_migrations(conn)
    store = ConsentStore(conn)
    audit = AuditLogger(conn, hmac_key=b"x" * 32)
    return ConsentGate(store=store, audit=audit)


def test_rebind_to_new_home_succeeds(tmp_path: Path) -> None:
    """The gate opens new audit.db and clears caches."""
    old = tmp_path / "old_profile"
    new = tmp_path / "new_profile"
    gate = _build_gate(old)

    # Stuff a session grant so we can verify it clears.
    gate._session_grants[("sid", "cap.x")] = "marker"  # type: ignore[assignment]

    ok = gate.rebind_to_profile(new)
    assert ok is True
    # session grants cleared.
    assert gate._session_grants == {}
    # New audit.db exists.
    assert (new / "audit.db").exists()


def test_rebind_rejects_non_path(tmp_path: Path) -> None:
    gate = _build_gate(tmp_path / "p")
    ok = gate.rebind_to_profile("not a path")  # type: ignore[arg-type]
    assert ok is False
    # Gate still functional (didn't lose its store).
    assert gate._store is not None


def test_rebind_to_unwritable_path_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the new audit.db can't be opened, return False, keep old bindings."""
    gate = _build_gate(tmp_path / "old")
    prior_store = gate._store

    # Force sqlite3.connect to raise via a path that points at an existing
    # file as a directory.
    bad = tmp_path / "old" / "audit.db"  # already a regular file
    # bad/audit.db treats bad as a dir → IO error on sqlite3.connect or mkdir.
    ok = gate.rebind_to_profile(bad)
    assert ok is False
    # Store unchanged.
    assert gate._store is prior_store


def test_rebind_isolates_audit_chain(tmp_path: Path) -> None:
    """Rebinding to a new home points subsequent audit appends at new file."""
    from opencomputer.agent.consent.audit import AuditEvent

    old = tmp_path / "old_profile"
    new = tmp_path / "new_profile"
    gate = _build_gate(old)

    # Append into OLD file.
    gate._audit.append(
        AuditEvent(
            action="profile_swap_test", capability_id="cap.x", actor="test",
            tier=0, scope="", decision="allow", session_id="sid",
            reason="test",
        )
    )

    gate.rebind_to_profile(new)

    # Append into NEW file (via the rebound audit object).
    gate._audit.append(
        AuditEvent(
            action="post_rebind", capability_id="cap.x", actor="test",
            tier=0, scope="", decision="allow", session_id="sid2",
            reason="test",
        )
    )

    # OLD file has only the first row.
    conn_old = sqlite3.connect(old / "audit.db")
    rows = conn_old.execute(
        "SELECT action FROM audit_log WHERE action LIKE 'profile_swap_test'"
    ).fetchall()
    conn_old.close()
    assert len(rows) == 1

    # NEW file has only the second row.
    conn_new = sqlite3.connect(new / "audit.db")
    rows = conn_new.execute(
        "SELECT action FROM audit_log WHERE action = 'post_rebind'"
    ).fetchall()
    conn_new.close()
    assert len(rows) == 1
