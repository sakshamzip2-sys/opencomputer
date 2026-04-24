"""F1 2.B.1 — `opencomputer consent suggest-promotions` CLI.

Reads `consent_counters` rows and reports (capability, scope) pairs
with clean_run_count >= 10 whose stored grant is still EXPLICIT.
With ``--auto-accept`` the matching grant is upgraded to IMPLICIT and
a `promote` audit row is appended.
"""
from __future__ import annotations

import sqlite3
import time

from typer.testing import CliRunner

from opencomputer.agent.config import _home
from opencomputer.agent.consent import ConsentStore
from opencomputer.agent.state import apply_migrations
from opencomputer.cli import app
from plugin_sdk import ConsentGrant, ConsentTier

runner = CliRunner()


def _seed_counter(conn: sqlite3.Connection, cap: str, scope: str | None, count: int) -> None:
    conn.execute(
        "INSERT INTO consent_counters "
        "(capability_id, scope_filter, clean_run_count, last_updated) "
        "VALUES (?, ?, ?, ?)",
        (cap, scope, count, time.time()),
    )
    conn.commit()


def _seed_grant(
    conn: sqlite3.Connection,
    cap: str,
    scope: str | None,
    tier: ConsentTier,
) -> None:
    store = ConsentStore(conn)
    store.upsert(ConsentGrant(
        capability_id=cap,
        tier=tier,
        scope_filter=scope,
        granted_at=time.time(),
        expires_at=None,
        granted_by="user",
    ))


def _open_profile_db(tmp_path) -> sqlite3.Connection:
    """Mirror cli_consent._open_consent_db's path resolution for seeding."""
    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "sessions.db", check_same_thread=False)
    apply_migrations(conn)
    return conn


def test_suggest_promotions_lists_eligible(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    conn = _open_profile_db(tmp_path)
    # Eligible — count above threshold + EXPLICIT grant.
    _seed_grant(conn, "read_files", "/a", ConsentTier.EXPLICIT)
    _seed_counter(conn, "read_files", "/a", 15)
    # Ineligible — count below threshold.
    _seed_grant(conn, "shell_exec", None, ConsentTier.EXPLICIT)
    _seed_counter(conn, "shell_exec", None, 3)
    conn.close()

    r = runner.invoke(app, ["consent", "suggest-promotions"])
    assert r.exit_code == 0, r.output
    assert "read_files" in r.output
    assert "/a" in r.output
    assert "15" in r.output
    # Ineligible row must NOT appear.
    assert "shell_exec" not in r.output


def test_suggest_promotions_skips_already_tier1(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    conn = _open_profile_db(tmp_path)
    # Eligible counter, but the active grant is already IMPLICIT (Tier 1):
    # nothing left to promote.
    _seed_grant(conn, "read_files", "/a", ConsentTier.IMPLICIT)
    _seed_counter(conn, "read_files", "/a", 15)
    conn.close()

    r = runner.invoke(app, ["consent", "suggest-promotions"])
    assert r.exit_code == 0, r.output
    assert "No promotion candidates" in r.output
    assert "read_files" not in r.output


def test_suggest_promotions_auto_accept_promotes(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    conn = _open_profile_db(tmp_path)
    _seed_grant(conn, "read_files", "/a", ConsentTier.EXPLICIT)
    _seed_counter(conn, "read_files", "/a", 12)
    conn.close()

    r = runner.invoke(app, ["consent", "suggest-promotions", "--auto-accept"])
    assert r.exit_code == 0, r.output
    assert "Promoted" in r.output
    assert "IMPLICIT" in r.output

    # Re-open the DB and verify the grant tier is now IMPLICIT and an audit
    # row exists with action=promote and actor=progressive_auto_promoter.
    conn = _open_profile_db(tmp_path)
    store = ConsentStore(conn)
    g = store.get("read_files", "/a")
    assert g is not None
    assert g.tier == ConsentTier.IMPLICIT
    assert g.granted_by == "promoted"

    rows = conn.execute(
        "SELECT actor, action, capability_id, scope, decision, reason "
        "FROM audit_log WHERE action='promote'"
    ).fetchall()
    assert len(rows) == 1
    actor, action, cap, scope, decision, reason = rows[0]
    assert actor == "progressive_auto_promoter"
    assert action == "promote"
    assert cap == "read_files"
    assert scope == "/a"
    assert decision == "allow"
    assert reason == "clean_run_count>=10"
    conn.close()
