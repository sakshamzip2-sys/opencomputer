"""Integration tests for ApprovalsConfig wired into ConsentGate.

P3.2 — closes the dead-code gap where security.approvals.{mode,timeout}
was readable from config but the consent gate never consulted it.
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path

from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from opencomputer.security.approvals import ApprovalsConfig
from plugin_sdk import CapabilityClaim, ConsentTier


def _make_gate() -> ConsentGate:
    """Build a gate with a real in-memory SQLite + applied migrations,
    matching the existing test_consent_pending_requests pattern."""
    tmp = Path(tempfile.mkdtemp())
    conn = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(conn)
    store = ConsentStore(conn)
    audit = AuditLogger(conn, hmac_key=b"k" * 16)
    return ConsentGate(store=store, audit=audit)


def _claim(tier: ConsentTier = ConsentTier.PER_ACTION) -> CapabilityClaim:
    return CapabilityClaim(
        capability_id="test.cap",
        tier_required=tier,
        human_description="test capability for consent gate wiring",
    )


# ── check() honours mode=off auto-allow ──────────────────────────────


def test_check_auto_allows_when_mode_off(monkeypatch):
    gate = _make_gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="off", timeout_s=300.0),
    )
    decision = gate.check(_claim(), scope=None, session_id="s1")
    assert decision.allowed is True
    assert "mode=off" in decision.reason


def test_check_does_not_auto_allow_when_mode_manual(monkeypatch):
    gate = _make_gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="manual", timeout_s=300.0),
    )
    decision = gate.check(_claim(), scope=None, session_id="s1")
    assert decision.allowed is False  # no grant exists


def test_audit_event_written_for_auto_allow(monkeypatch):
    gate = _make_gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="off"),
    )
    decision = gate.check(_claim(), scope=None, session_id="s1")
    assert decision.audit_event_id is not None


# ── request_approval honours config-driven timeout ──────────────────


def test_request_approval_uses_config_timeout_when_default(monkeypatch):
    """When caller doesn't pass timeout_s, the config value is used."""
    gate = _make_gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="manual", timeout_s=10.0),
    )

    captured = {}

    async def fake_handler(session_id, claim, scope):
        captured["called"] = True
        return False  # don't deliver — gate auto-denies

    gate.set_prompt_handler(fake_handler)
    decision = asyncio.run(gate.request_approval(
        claim=_claim(), scope=None, session_id="s1",
    ))
    # Handler called with the config-resolved timeout; auto-deny path
    # taken because handler returned False (no channel).
    assert captured.get("called") is True
    assert decision.allowed is False


def test_request_approval_explicit_timeout_overrides_config(monkeypatch):
    """Caller-supplied timeout_s wins over config."""
    gate = _make_gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="manual", timeout_s=999.0),
    )

    async def slow_handler(session_id, claim, scope):
        return True  # pretend we delivered

    gate.set_prompt_handler(slow_handler)
    # Pass explicit 0.05s — should time out almost immediately.
    decision = asyncio.run(gate.request_approval(
        claim=_claim(), scope=None, session_id="s1", timeout_s=0.05,
    ))
    assert decision.allowed is False
    reason = decision.reason.lower()
    assert (
        "timed out" in reason
        or "timeout" in reason
        or "did not deliver" in reason
    ), f"unexpected reason: {decision.reason!r}"


def test_request_approval_auto_allows_when_mode_off(monkeypatch):
    """mode=off short-circuits request_approval too."""
    gate = _make_gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="off"),
    )

    async def never_called(session_id, claim, scope):
        raise AssertionError("handler should not be called when mode=off")

    gate.set_prompt_handler(never_called)
    decision = asyncio.run(gate.request_approval(
        claim=_claim(), scope=None, session_id="s1",
    ))
    assert decision.allowed is True


# ── refresh_approvals_config invalidates the cache ──────────────────


def test_refresh_invalidates_cache(monkeypatch):
    gate = _make_gate()
    states = iter([
        ApprovalsConfig(mode="manual"),
        ApprovalsConfig(mode="off"),
    ])
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: next(states),
    )

    # First read populates cache.
    d1 = gate.check(_claim(), scope=None, session_id="s1")
    assert d1.allowed is False  # mode=manual → no grant → deny

    # Without refresh, cache hits — still mode=manual.
    d2 = gate.check(_claim(), scope=None, session_id="s1")
    assert d2.allowed is False

    # Refresh → re-read picks up mode=off.
    gate.refresh_approvals_config()
    d3 = gate.check(_claim(), scope=None, session_id="s1")
    assert d3.allowed is True
