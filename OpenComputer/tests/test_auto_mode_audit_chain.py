"""v1.1 plan-3 M9.4 — classifier decisions land in the F1 HMAC-chained audit log.

Pins the contract:

* :func:`audit_classifier_decision` writes one `AuditEvent` per
  classifier verdict (allow/block/ask).
* The actor field is `"classifier"` (distinguishes from
  consent-gate rows: `"consent_gate"`).
* The action field is `"classify"`; `capability_id` is the tool name;
  `decision` carries the verdict; `reason` carries the rationale (with
  `[fail-closed]` suffix when applicable).
* Audit failures NEVER raise — return `None` instead.
* The chain integrity is preserved (`AuditLogger.verify_chain()` returns
  True after appending classifier rows alongside gate rows).
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from opencomputer.agent.consent.audit import AuditEvent, AuditLogger
from opencomputer.agent.tool_call_classifier import (
    ClassifierDecision,
    Decision,
    audit_classifier_decision,
)
from plugin_sdk.core import ToolCall


@pytest.fixture
def audit_logger(tmp_path) -> AuditLogger:
    """A real AuditLogger backed by a temp SQLite DB with the F1 schema."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    # Apply the F1 audit-log migration
    from opencomputer.agent.state import apply_migrations

    apply_migrations(conn)
    return AuditLogger(conn, hmac_key=os.urandom(32))


def _allow() -> ClassifierDecision:
    return ClassifierDecision(decision=Decision.ALLOW, rationale="safe call")


def _block(reason: str = "destructive command") -> ClassifierDecision:
    return ClassifierDecision(decision=Decision.BLOCK, rationale=reason)


def _block_failed_closed() -> ClassifierDecision:
    return ClassifierDecision(
        decision=Decision.BLOCK, rationale="provider timeout", failed_closed=True,
    )


def _call(name: str = "Bash") -> ToolCall:
    return ToolCall(id="t1", name=name, arguments={"command": "ls"})


# ─── happy path ──────────────────────────────────────────────────────────


def test_audit_decision_writes_row(audit_logger: AuditLogger) -> None:
    rid = audit_classifier_decision(audit_logger, "session-X", _call(), _allow())
    assert isinstance(rid, int)
    assert rid > 0


def test_audit_actor_is_classifier(audit_logger: AuditLogger) -> None:
    audit_classifier_decision(audit_logger, "session-Y", _call(), _block())
    row = audit_logger._conn.execute(
        "SELECT actor, action, capability_id, decision, reason FROM audit_log"
    ).fetchone()
    assert row[0] == "classifier"
    assert row[1] == "classify"
    assert row[2] == "Bash"
    assert row[3] == "block"
    assert "destructive command" in row[4]


def test_audit_failed_closed_marker_in_reason(audit_logger: AuditLogger) -> None:
    audit_classifier_decision(
        audit_logger, "session-Z", _call(), _block_failed_closed(),
    )
    row = audit_logger._conn.execute(
        "SELECT reason FROM audit_log"
    ).fetchone()
    assert "[fail-closed]" in row[0]


def test_audit_actor_distinguishable_from_consent_gate(
    audit_logger: AuditLogger,
) -> None:
    """Operators filtering audit rows must be able to tell classifier
    decisions apart from consent gate decisions."""
    audit_logger.append(AuditEvent(
        session_id="s",
        actor="consent_gate",
        action="check",
        capability_id="filesystem.write",
        tier=2,
        scope="/tmp/foo",
        decision="deny",
        reason="user said no",
    ))
    audit_classifier_decision(audit_logger, "s", _call(), _block())

    rows = audit_logger._conn.execute(
        "SELECT actor FROM audit_log ORDER BY id"
    ).fetchall()
    actors = [r[0] for r in rows]
    assert actors == ["consent_gate", "classifier"]


# ─── chain integrity ─────────────────────────────────────────────────────


def test_chain_intact_with_classifier_rows_interleaved(
    audit_logger: AuditLogger,
) -> None:
    """Classifier rows mix in with gate rows without breaking the HMAC chain."""
    # 3 mixed rows
    audit_classifier_decision(audit_logger, "s", _call(), _allow())
    audit_logger.append(AuditEvent(
        session_id="s", actor="consent_gate", action="check",
        capability_id="filesystem.write", tier=2, scope="/tmp/x",
        decision="allow", reason="auto-grant",
    ))
    audit_classifier_decision(audit_logger, "s", _call(), _block())

    assert audit_logger.verify_chain() is True


# ─── defensive paths ─────────────────────────────────────────────────────


def test_audit_returns_none_when_logger_is_none() -> None:
    """No logger == no-op. Caller doesn't need to special-case anything."""
    out = audit_classifier_decision(None, "s", _call(), _allow())
    assert out is None


def test_audit_returns_none_when_logger_raises(audit_logger: AuditLogger) -> None:
    """Any audit-write exception is swallowed, returns None."""
    # Wedge the logger by closing its connection
    audit_logger._conn.close()
    out = audit_classifier_decision(audit_logger, "s", _call(), _block())
    assert out is None  # didn't raise, returned None


def test_audit_truncates_rationale_long_enough(audit_logger: AuditLogger) -> None:
    """Very long rationale (e.g. 5KB classifier explanation) truncates
    cleanly to keep audit row size bounded."""
    long_reason = "x" * 5000
    audit_classifier_decision(
        audit_logger, "s", _call(),
        ClassifierDecision(decision=Decision.BLOCK, rationale=long_reason),
    )
    row = audit_logger._conn.execute(
        "SELECT reason FROM audit_log"
    ).fetchone()
    assert len(row[0]) <= 700  # 500 char cap + suffix slack
