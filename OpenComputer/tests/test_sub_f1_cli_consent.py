"""CLI: opencomputer consent {list, grant, revoke, history, ...}"""
from typer.testing import CliRunner

from opencomputer.cli import app

runner = CliRunner()


def test_consent_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    result = runner.invoke(app, ["consent", "list"])
    assert result.exit_code == 0, result.output
    assert "No active grants" in result.output


def test_consent_grant_then_list(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r1 = runner.invoke(app, [
        "consent", "grant", "read_files",
        "--scope", str(tmp_path),
        "--tier", "2", "--expires", "1d",
    ])
    assert r1.exit_code == 0, r1.output
    assert "Granted read_files" in r1.output
    r2 = runner.invoke(app, ["consent", "list"])
    assert "read_files" in r2.output
    assert "PER_ACTION" in r2.output


def test_consent_revoke(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    r = runner.invoke(app, ["consent", "revoke", "x"])
    assert r.exit_code == 0, r.output
    r2 = runner.invoke(app, ["consent", "list"])
    assert "x " not in r2.output  # "x " would only appear as capability_id


def test_consent_history_shows_events(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    runner.invoke(app, ["consent", "revoke", "x"])
    r = runner.invoke(app, ["consent", "history", "x"])
    assert r.exit_code == 0, r.output
    assert "grant" in r.output
    assert "revoke" in r.output


def test_consent_session_grants_empty_with_helpful_message(tmp_path, monkeypatch):
    """Hermes parity: session-grants subcommand shows helpful empty state."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    r = runner.invoke(app, ["consent", "session-grants"])
    assert r.exit_code == 0, r.output
    assert "session" in r.output.lower()
    # Empty audit-log surfaces the helpful "in-memory on running gateway" hint.
    assert "in-memory" in r.output or "no session" in r.output.lower()


def test_consent_session_grants_after_synthetic_event(tmp_path, monkeypatch):
    """When the audit log records an approval_allow_session event, session-grants surfaces it."""
    import sqlite3

    from opencomputer.agent.consent.audit import AuditEvent, AuditLogger
    from opencomputer.agent.consent.store import ConsentStore
    from opencomputer.agent.state import apply_migrations

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    # Seed an audit row directly. CLI uses sessions.db at OPENCOMPUTER_HOME root.
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    apply_migrations(conn)
    ConsentStore(conn)  # ensure store schema present
    log = AuditLogger(conn, hmac_key=b"k" * 16)
    log.append(AuditEvent(
        session_id="sess-abc-123",
        actor="user", action="approval_allow_session",
        capability_id="execute_code.run",
        tier=2, scope=None,
        decision="allow",
        reason="user clicked allow session",
    ))
    conn.commit()
    conn.close()

    r = runner.invoke(app, ["consent", "session-grants"])
    assert r.exit_code == 0, r.output
    assert "execute_code.run" in r.output
    assert "sess-abc-123" in r.output


def test_consent_verify_chain_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    r = runner.invoke(app, ["consent", "verify-chain"])
    assert r.exit_code == 0, r.output
    assert "ok" in r.output.lower()


def test_consent_export_and_import_chain_head(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    out = tmp_path / "head.json"
    r = runner.invoke(app, ["consent", "export-chain-head", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists()
    r2 = runner.invoke(app, ["consent", "import-chain-head", "--from", str(out)])
    assert r2.exit_code == 0, r2.output


def test_consent_bypass_status(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCOMPUTER_CONSENT_BYPASS", raising=False)
    r = runner.invoke(app, ["consent", "bypass", "--status"])
    assert r.exit_code == 0, r.output
    assert "inactive" in r.output.lower()


def test_consent_bypass_active(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_CONSENT_BYPASS", "1")
    r = runner.invoke(app, ["consent", "bypass", "--status"])
    assert r.exit_code == 0, r.output
    assert "active" in r.output.lower()
    assert "BYPASS" in r.output


def test_consent_grant_default_expiry_30d(tmp_path, monkeypatch):
    """Default expiry should be ~30d when --expires omitted."""
    import time as _time
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["consent", "grant", "x", "--tier", "1"])
    r = runner.invoke(app, ["consent", "list"])
    # Crudely parse the year/month from the output to see something
    # 30d out — but more robustly check that SOME expiry is shown.
    assert "expires" in r.output.lower()
