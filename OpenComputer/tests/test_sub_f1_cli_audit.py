"""F1 2.B.4 — `opencomputer audit show / verify` CLI."""
from __future__ import annotations

import json
import sqlite3
import time

from typer.testing import CliRunner

from opencomputer.agent.config import _home
from opencomputer.agent.consent import AuditEvent
from opencomputer.agent.state import apply_migrations
from opencomputer.cli import app

runner = CliRunner()


def _seed(events: list[AuditEvent], tmp_path, monkeypatch) -> None:
    """Write events to the profile DB using the CLI's logger so the
    HMAC chain stays valid and seeded rows match what the CLI later reads.
    """
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli_consent import _open_consent_db

    _, _, logger = _open_consent_db()
    for evt in events:
        logger.append(evt)


def test_audit_show_default_lists_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed([
        AuditEvent(None, "user", "grant", "read_files", 1, None, "allow", "r1"),
        AuditEvent(None, "user", "grant", "shell_exec", 2, "/x", "allow", "r2"),
        AuditEvent(None, "user", "revoke", "read_files", 0, None, "n/a", "r3"),
    ], tmp_path, monkeypatch)
    r = runner.invoke(app, ["audit", "show"])
    assert r.exit_code == 0, r.output
    assert "read_files" in r.output
    assert "shell_exec" in r.output
    assert "r1" in r.output or "r2" in r.output


def test_audit_show_filters_by_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed([
        AuditEvent(None, "user", "grant", "read_files", 1, None, "allow", "rfg"),
        AuditEvent(None, "user", "grant", "shell_exec", 2, "/x", "allow", "shg"),
    ], tmp_path, monkeypatch)
    r = runner.invoke(app, ["audit", "show", "--tool", "shell_exec"])
    assert r.exit_code == 0, r.output
    assert "shell_exec" in r.output
    assert "read_files" not in r.output


def test_audit_show_filters_by_since(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Pre-seed an OLD row by writing directly with a timestamp far in the past;
    # then add a current row through the chain helper.
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.cli_consent import _open_consent_db

    _, _, logger = _open_consent_db()
    old_evt = AuditEvent(None, "user", "grant", "old_cap", 1, None, "allow", "old")
    new_evt = AuditEvent(None, "user", "grant", "new_cap", 1, None, "allow", "new")
    logger.append(old_evt, now=time.time() - 7200)  # 2h ago
    logger.append(new_evt)  # right now
    r = runner.invoke(app, ["audit", "show", "--since", "1h"])
    assert r.exit_code == 0, r.output
    assert "new_cap" in r.output
    assert "old_cap" not in r.output


def test_audit_show_filters_by_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed([
        AuditEvent(None, "hook", "check", "cap_a", 1, None, "allow", "ok"),
        AuditEvent(None, "hook", "check", "cap_b", 1, None, "deny", "no grant"),
    ], tmp_path, monkeypatch)
    r = runner.invoke(app, ["audit", "show", "--decision", "deny"])
    assert r.exit_code == 0, r.output
    assert "cap_b" in r.output
    assert "cap_a" not in r.output


def test_audit_show_json_output(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed([
        AuditEvent(None, "user", "grant", "read_files", 1, None, "allow", "r1"),
    ], tmp_path, monkeypatch)
    r = runner.invoke(app, ["audit", "show", "--json"])
    assert r.exit_code == 0, r.output
    parsed = json.loads(r.output.strip())
    assert isinstance(parsed, list)
    assert len(parsed) >= 1
    assert any(row["capability_id"] == "read_files" for row in parsed)


def test_audit_verify_prints_ok_on_intact_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed([
        AuditEvent(None, "user", "grant", "x", 1, None, "allow", "first"),
        AuditEvent(None, "user", "grant", "y", 1, None, "allow", "second"),
    ], tmp_path, monkeypatch)
    r = runner.invoke(app, ["audit", "verify"])
    assert r.exit_code == 0, r.output
    assert "Chain intact" in r.output
    assert "2 rows" in r.output


def test_audit_verify_exits_nonzero_on_broken_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _seed([
        AuditEvent(None, "user", "grant", "x", 1, None, "allow", "first"),
        AuditEvent(None, "user", "grant", "y", 1, None, "allow", "second"),
        AuditEvent(None, "user", "grant", "z", 1, None, "allow", "third"),
    ], tmp_path, monkeypatch)
    # Snip a middle row so chain links break (the audit_log table has BEFORE
    # DELETE/UPDATE triggers, so we must drop them first to perform a tamper).
    db = _home() / "sessions.db"
    conn = sqlite3.connect(db, check_same_thread=False)
    apply_migrations(conn)
    conn.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
    conn.execute("DELETE FROM audit_log WHERE reason='second'")
    conn.commit()
    conn.close()

    r = runner.invoke(app, ["audit", "verify"])
    assert r.exit_code != 0, r.output
    # stderr is captured into r.output by CliRunner
    combined = (r.output or "") + (r.stderr if r.stderr else "")
    assert "Chain broken" in combined
