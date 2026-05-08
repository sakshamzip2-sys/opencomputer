"""Tests for the smart-mode auxiliary-LLM risk assessor (P3.6)."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from opencomputer.security.approvals import ApprovalsConfig
from opencomputer.security.smart_mode import (
    RiskAssessment,
    _parse_assessment,
    assess_risk,
)
from plugin_sdk import CapabilityClaim, ConsentTier

# ── _parse_assessment pure-function coverage ────────────────────────


def test_parse_low_risk():
    raw = '{"risk": "low", "reason": "ls is read-only"}'
    a = _parse_assessment(raw)
    assert a.level == "low"
    assert a.auto_allow is True
    assert a.used_fallback is False


def test_parse_high_risk():
    raw = '{"risk": "high", "reason": "rm -rf / wipes filesystem"}'
    a = _parse_assessment(raw)
    assert a.level == "high"
    assert a.auto_deny is True
    assert a.needs_manual is False


def test_parse_uncertain():
    raw = '{"risk": "uncertain", "reason": "obfuscated command"}'
    a = _parse_assessment(raw)
    assert a.level == "uncertain"
    assert a.needs_manual is True


def test_parse_medium_falls_through_to_manual():
    raw = '{"risk": "medium", "reason": "writes inside project"}'
    a = _parse_assessment(raw)
    assert a.level == "medium"
    assert a.needs_manual is True
    assert a.auto_allow is False
    assert a.auto_deny is False


def test_parse_strips_code_fence():
    raw = '```json\n{"risk": "low", "reason": "x"}\n```'
    a = _parse_assessment(raw)
    assert a.level == "low"


def test_parse_extracts_first_json_object():
    """LLM might wrap in prose despite the system prompt — we recover."""
    raw = 'Here is my decision: {"risk": "high", "reason": "danger"}'
    a = _parse_assessment(raw)
    assert a.level == "high"


def test_parse_unknown_label_is_uncertain_fallback():
    raw = '{"risk": "extreme", "reason": "x"}'
    a = _parse_assessment(raw)
    assert a.level == "uncertain"
    assert a.used_fallback is True


def test_parse_malformed_json_is_uncertain_fallback():
    a = _parse_assessment("not json at all")
    assert a.level == "uncertain"
    assert a.used_fallback is True


def test_parse_empty_is_uncertain_fallback():
    a = _parse_assessment("")
    assert a.level == "uncertain"
    assert a.used_fallback is True


def test_parse_non_object_is_uncertain():
    a = _parse_assessment('["low"]')
    assert a.level == "uncertain"


# ── assess_risk async behaviour ─────────────────────────────────────


def test_assess_empty_command_is_uncertain():
    a = asyncio.run(assess_risk(""))
    assert a.level == "uncertain"
    assert a.used_fallback is True


def test_assess_low_risk_via_mocked_llm(monkeypatch):
    async def fake_complete(**kwargs):
        return '{"risk": "low", "reason": "ls is read-only"}'

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", fake_complete
    )
    a = asyncio.run(assess_risk("ls -la"))
    assert a.level == "low"
    assert a.auto_allow is True


def test_assess_high_risk_via_mocked_llm(monkeypatch):
    async def fake_complete(**kwargs):
        return '{"risk": "high", "reason": "writes /etc"}'

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", fake_complete
    )
    a = asyncio.run(assess_risk("echo x > /etc/passwd"))
    assert a.level == "high"


def test_assess_llm_unavailable_returns_uncertain(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("aux provider down")

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", boom
    )
    a = asyncio.run(assess_risk("ls -la"))
    assert a.level == "uncertain"
    assert a.used_fallback is True


def test_assess_timeout_returns_uncertain(monkeypatch):
    async def slow(**kwargs):
        await asyncio.sleep(60.0)
        return ""

    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text", slow
    )
    monkeypatch.setattr(
        "opencomputer.security.smart_mode._TIMEOUT_S", 0.05
    )
    a = asyncio.run(assess_risk("ls -la"))
    assert a.level == "uncertain"
    assert a.used_fallback is True


# ── ConsentGate integration ──────────────────────────────────────────


def _gate() -> ConsentGate:
    tmp = Path(tempfile.mkdtemp())
    conn = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(conn)
    return ConsentGate(
        store=ConsentStore(conn),
        audit=AuditLogger(conn, hmac_key=b"k" * 16),
    )


def _claim() -> CapabilityClaim:
    return CapabilityClaim(
        capability_id="bash.execute",
        tier_required=ConsentTier.PER_ACTION,
        human_description="ls -la",
    )


def test_gate_smart_mode_low_risk_auto_allows(monkeypatch):
    gate = _gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="smart"),
    )
    monkeypatch.setattr(
        "opencomputer.security.smart_mode.assess_risk",
        AsyncMock(return_value=RiskAssessment(
            level="low", reason="read-only",
        )),
    )

    decision = asyncio.run(gate.request_approval(
        claim=_claim(), scope=None, session_id="s1",
    ))
    assert decision.allowed is True
    assert "smart-mode low-risk" in decision.reason


def test_gate_smart_mode_high_risk_auto_denies(monkeypatch):
    gate = _gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="smart"),
    )
    monkeypatch.setattr(
        "opencomputer.security.smart_mode.assess_risk",
        AsyncMock(return_value=RiskAssessment(
            level="high", reason="writes /etc",
        )),
    )

    decision = asyncio.run(gate.request_approval(
        claim=_claim(), scope=None, session_id="s1",
    ))
    assert decision.allowed is False
    assert "smart-mode high-risk" in decision.reason


def test_gate_smart_mode_uncertain_falls_through_to_manual(monkeypatch):
    gate = _gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="smart"),
    )
    monkeypatch.setattr(
        "opencomputer.security.smart_mode.assess_risk",
        AsyncMock(return_value=RiskAssessment(
            level="uncertain", reason="ambiguous",
        )),
    )

    handler_called = []

    async def fake_handler(session_id, claim, scope):
        handler_called.append(True)
        return False  # no channel — gate auto-denies after fallback

    gate.set_prompt_handler(fake_handler)
    decision = asyncio.run(gate.request_approval(
        claim=_claim(), scope=None, session_id="s1",
    ))
    # Smart did NOT auto-decide → manual prompt path was hit. Handler
    # was called (so prompt fired); since it returned False the gate
    # then auto-denied (no channel).
    assert handler_called == [True]
    assert decision.allowed is False


def test_gate_smart_mode_aux_llm_crash_falls_through_to_manual(monkeypatch):
    gate = _gate()
    monkeypatch.setattr(
        "opencomputer.security.approvals.load_approvals_from_active_config",
        lambda: ApprovalsConfig(mode="smart"),
    )

    async def boom(**kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(
        "opencomputer.security.smart_mode.assess_risk", boom
    )

    handler_called = []

    async def fake_handler(session_id, claim, scope):
        handler_called.append(True)
        return False

    gate.set_prompt_handler(fake_handler)
    decision = asyncio.run(gate.request_approval(
        claim=_claim(), scope=None, session_id="s1",
    ))
    # Crash → manual fallback fired.
    assert handler_called == [True]
    assert decision.allowed is False
