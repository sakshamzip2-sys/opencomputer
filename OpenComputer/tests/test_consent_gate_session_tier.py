"""Hermes parity: 4th approval verb 'session' grants for the rest of the session only.

Mirrors the canonical fixture pattern from
``tests/test_sub_f1_consent_gate.py:_setup`` — real ConsentStore +
real AuditLogger backed by an in-memory SQLite via apply_migrations.
"""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path

from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from plugin_sdk import CapabilityClaim, ConsentGrant, ConsentTier


def _setup_gate():
    tmp = Path(tempfile.mkdtemp())
    c = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(c)
    store = ConsentStore(c)
    log = AuditLogger(c, hmac_key=b"k" * 16)
    return ConsentGate(store=store, audit=log), store


def _claim() -> CapabilityClaim:
    return CapabilityClaim(
        "execute_code.run", ConsentTier.PER_ACTION, "run user code",
    )


def test_session_grant_short_circuits_check_within_same_session():
    gate, _ = _setup_gate()
    gate._session_grants[("s1", "execute_code.run")] = ConsentGrant(
        "execute_code.run",
        ConsentTier.PER_ACTION,
        None,
        time.time(),
        None,
        "user",
    )

    decision = gate.check(_claim(), scope=None, session_id="s1")
    assert decision.allowed is True
    assert "session" in decision.reason


def test_session_grant_does_not_leak_to_other_session():
    gate, _ = _setup_gate()
    gate._session_grants[("s1", "execute_code.run")] = ConsentGrant(
        "execute_code.run",
        ConsentTier.PER_ACTION,
        None,
        time.time(),
        None,
        "user",
    )
    decision = gate.check(_claim(), scope=None, session_id="s2")
    assert decision.allowed is False


def test_on_session_finalize_clears_only_matching_session():
    gate, _ = _setup_gate()
    g = ConsentGrant(
        "execute_code.run",
        ConsentTier.PER_ACTION,
        None,
        time.time(),
        None,
        "user",
    )
    gate._session_grants[("s1", "execute_code.run")] = g
    gate._session_grants[("s2", "execute_code.run")] = g

    gate.on_session_finalize(session_id="s1")

    assert ("s1", "execute_code.run") not in gate._session_grants
    assert ("s2", "execute_code.run") in gate._session_grants


def test_resolve_pending_session_scoped_writes_3_tuple():
    gate, store = _setup_gate()
    key = ("s1", "execute_code.run")
    gate._pending_requests[key] = asyncio.Event()

    resolved = gate.resolve_pending(
        session_id="s1",
        capability_id="execute_code.run",
        decision=True,
        persist=False,
        session_scoped=True,
    )
    assert resolved is True
    assert gate._pending_decisions[key] == (True, False, True)
    # Persistent store untouched — session grants live in-memory only.
    assert store.get("execute_code.run", None) is None


def test_resolve_pending_legacy_2_arg_call_still_works():
    """Backward-compat: existing dispatch calls without session_scoped kwarg."""
    gate, _ = _setup_gate()
    key = ("s1", "execute_code.run")
    gate._pending_requests[key] = asyncio.Event()

    resolved = gate.resolve_pending(
        session_id="s1",
        capability_id="execute_code.run",
        decision=True,
        persist=True,
    )
    assert resolved is True
    # 3-tuple stored — session_scoped defaulted to False.
    assert gate._pending_decisions[key] == (True, True, False)
