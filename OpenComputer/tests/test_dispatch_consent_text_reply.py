"""Integration tests for gateway dispatch text-based consent replies.

P3.3 — closes the dead-code gap where classify_reply existed but
nothing called it. Now `Dispatch._maybe_resolve_consent_text_reply`
intercepts inbound text events on sessions with a pending consent
prompt, resolves the gate, and short-circuits the agent loop.
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk import CapabilityClaim, ConsentTier


def _gate() -> ConsentGate:
    tmp = Path(tempfile.mkdtemp())
    conn = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(conn)
    return ConsentGate(
        store=ConsentStore(conn),
        audit=AuditLogger(conn, hmac_key=b"k" * 16),
    )


def _dispatch_with_gate(gate: ConsentGate) -> Dispatch:
    """Build a minimal Dispatch with the gate wired into a fake router."""
    fake_loop = SimpleNamespace(_consent_gate=gate)
    fake_router = SimpleNamespace(_loops={"default": fake_loop})
    d = Dispatch.__new__(Dispatch)
    d._router = fake_router
    d._session_profiles = {}
    return d


def test_resolve_returns_false_when_no_pending() -> None:
    gate = _gate()
    d = _dispatch_with_gate(gate)
    consumed = asyncio.run(d._maybe_resolve_consent_text_reply(
        session_id="s1", text="yes",
    ))
    assert consumed is False


def test_resolve_returns_false_when_text_unclassifiable() -> None:
    gate = _gate()
    # Add a pending request manually.
    gate._pending_requests[("s1", "cap.x")] = asyncio.Event()
    d = _dispatch_with_gate(gate)
    consumed = asyncio.run(d._maybe_resolve_consent_text_reply(
        session_id="s1", text="hello there friend",
    ))
    assert consumed is False
    # Pending entry still alive.
    assert ("s1", "cap.x") in gate._pending_requests


def test_resolve_approve_consumes_yes() -> None:
    gate = _gate()
    gate._pending_requests[("s1", "cap.x")] = asyncio.Event()
    d = _dispatch_with_gate(gate)
    consumed = asyncio.run(d._maybe_resolve_consent_text_reply(
        session_id="s1", text="yes",
    ))
    assert consumed is True
    # Decision recorded as approve / once.
    assert gate._pending_decisions[("s1", "cap.x")] == (True, False)
    # Event triggered so a waiting request_approval would unblock.
    assert gate._pending_requests[("s1", "cap.x")].is_set()


def test_resolve_deny_consumes_no() -> None:
    gate = _gate()
    gate._pending_requests[("s1", "cap.x")] = asyncio.Event()
    d = _dispatch_with_gate(gate)
    consumed = asyncio.run(d._maybe_resolve_consent_text_reply(
        session_id="s1", text="no",
    ))
    assert consumed is True
    assert gate._pending_decisions[("s1", "cap.x")] == (False, False)


def test_resolve_handles_multiple_pending() -> None:
    """Multiple capabilities pending for one session → all resolved by one reply."""
    gate = _gate()
    gate._pending_requests[("s1", "cap.x")] = asyncio.Event()
    gate._pending_requests[("s1", "cap.y")] = asyncio.Event()
    d = _dispatch_with_gate(gate)
    consumed = asyncio.run(d._maybe_resolve_consent_text_reply(
        session_id="s1", text="approve",
    ))
    assert consumed is True
    assert gate._pending_decisions[("s1", "cap.x")] == (True, False)
    assert gate._pending_decisions[("s1", "cap.y")] == (True, False)


def test_resolve_does_not_match_other_session() -> None:
    """Reply on session A does not resolve pending on session B."""
    gate = _gate()
    gate._pending_requests[("sB", "cap.x")] = asyncio.Event()
    d = _dispatch_with_gate(gate)
    consumed = asyncio.run(d._maybe_resolve_consent_text_reply(
        session_id="sA", text="yes",
    ))
    assert consumed is False
    assert ("sB", "cap.x") in gate._pending_requests


def test_resolve_returns_false_when_no_loop_or_gate() -> None:
    d = Dispatch.__new__(Dispatch)
    d._router = SimpleNamespace(_loops={})
    d._session_profiles = {}
    consumed = asyncio.run(d._maybe_resolve_consent_text_reply(
        session_id="s1", text="yes",
    ))
    assert consumed is False
