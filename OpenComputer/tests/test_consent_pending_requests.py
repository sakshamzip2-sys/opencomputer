"""ConsentGate pending-approval registry — round 2a P-5.

Covers :meth:`ConsentGate.request_approval` /
:meth:`ConsentGate.resolve_pending` round-trips, the 5-minute auto-deny
timeout (sub-second in tests), no-channel fast-path deny, and double-
resolve no-op behaviour. The Telegram surface lives in a sibling test
file — these tests stay adapter-free so a regression in the gate
itself can't be masked by adapter mocks.
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
from plugin_sdk import CapabilityClaim, ConsentTier


def _setup() -> tuple[sqlite3.Connection, ConsentStore, AuditLogger, ConsentGate]:
    tmp = Path(tempfile.mkdtemp())
    conn = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(conn)
    store = ConsentStore(conn)
    audit = AuditLogger(conn, hmac_key=b"k" * 16)
    gate = ConsentGate(store=store, audit=audit)
    return conn, store, audit, gate


def _claim() -> CapabilityClaim:
    return CapabilityClaim(
        capability_id="read_files.metadata",
        tier_required=ConsentTier.PER_ACTION,
        human_description="read file metadata",
    )


async def test_request_approval_no_handler_immediate_deny() -> None:
    """With no prompt handler set, request_approval auto-denies fast."""
    _, _, _, gate = _setup()
    decision = await gate.request_approval(
        claim=_claim(),
        scope="/Users/x/foo.py",
        session_id="s1",
        timeout_s=5.0,
    )
    assert decision.allowed is False
    assert "no approval channel" in decision.reason
    assert decision.audit_event_id is not None


async def test_request_approval_handler_returns_false_immediate_deny() -> None:
    """If the handler reports failure, gate auto-denies without waiting."""
    _, _, _, gate = _setup()

    async def handler(_sid, _claim, _scope) -> bool:
        return False

    gate.set_prompt_handler(handler)
    decision = await gate.request_approval(
        claim=_claim(),
        scope="/Users/x/foo.py",
        session_id="s1",
        timeout_s=5.0,
    )
    assert decision.allowed is False
    assert "did not deliver prompt" in decision.reason


async def test_request_approval_allow_once_round_trip() -> None:
    """Handler dispatches; resolve_pending(decision=True, persist=False) wins."""
    _, store, _, gate = _setup()
    handler_calls: list[tuple] = []

    async def handler(sid, claim, scope) -> bool:
        handler_calls.append((sid, claim.capability_id, scope))
        # Simulate the user clicking after a tiny delay.
        async def _later() -> None:
            await asyncio.sleep(0.01)
            gate.resolve_pending(
                session_id=sid,
                capability_id=claim.capability_id,
                decision=True,
                persist=False,
            )
        asyncio.create_task(_later())
        return True

    gate.set_prompt_handler(handler)
    decision = await gate.request_approval(
        claim=_claim(),
        scope="/tmp/foo.py",
        session_id="s1",
        timeout_s=5.0,
    )
    assert decision.allowed is True
    assert decision.tier_matched == ConsentTier.PER_ACTION
    assert "allow once" in decision.reason
    # allow_once must NOT persist a grant.
    assert store.get("read_files.metadata", "/tmp/foo.py") is None
    assert handler_calls == [("s1", "read_files.metadata", "/tmp/foo.py")]


async def test_request_approval_allow_always_persists_grant() -> None:
    """``allow_always`` writes a non-expiring grant before returning."""
    _, store, _, gate = _setup()

    async def handler(sid, claim, _scope) -> bool:
        async def _later() -> None:
            await asyncio.sleep(0.01)
            gate.resolve_pending(
                session_id=sid,
                capability_id=claim.capability_id,
                decision=True,
                persist=True,
            )
        asyncio.create_task(_later())
        return True

    gate.set_prompt_handler(handler)
    decision = await gate.request_approval(
        claim=_claim(),
        scope="/tmp/foo.py",
        session_id="s1",
        timeout_s=5.0,
    )
    assert decision.allowed is True
    grant = store.get("read_files.metadata", "/tmp/foo.py")
    assert grant is not None
    assert grant.expires_at is None
    assert grant.granted_by == "user"
    assert grant.tier == ConsentTier.PER_ACTION


async def test_request_approval_deny_click_no_grant_written() -> None:
    """Deny click resolves cleanly with no grant persisted."""
    _, store, _, gate = _setup()

    async def handler(sid, claim, _scope) -> bool:
        async def _later() -> None:
            await asyncio.sleep(0.01)
            gate.resolve_pending(
                session_id=sid,
                capability_id=claim.capability_id,
                decision=False,
                persist=False,
            )
        asyncio.create_task(_later())
        return True

    gate.set_prompt_handler(handler)
    decision = await gate.request_approval(
        claim=_claim(),
        scope="/tmp/foo.py",
        session_id="s1",
        timeout_s=5.0,
    )
    assert decision.allowed is False
    assert "deny" in decision.reason.lower()
    assert store.get("read_files.metadata", "/tmp/foo.py") is None


async def test_request_approval_timeout_auto_deny() -> None:
    """Per L3: 5-minute timeout auto-denies. Test uses a short timeout."""
    _, _, audit, gate = _setup()

    async def handler(_sid, _claim, _scope) -> bool:
        # Delivered the prompt but the user never clicks.
        return True

    gate.set_prompt_handler(handler)
    decision = await gate.request_approval(
        claim=_claim(),
        scope="/tmp/foo.py",
        session_id="s1",
        timeout_s=0.1,
    )
    assert decision.allowed is False
    assert "timed out" in decision.reason
    # The pending registry must be cleared so a stale click later
    # finds nothing to resolve.
    assert not gate.has_pending_request(
        session_id="s1", capability_id="read_files.metadata",
    )
    # Audit must have a deny row with the timeout reason.
    rows = audit.query(decision="deny", limit=10)
    assert any("timed out" in r["reason"] for r in rows)


async def test_resolve_pending_returns_false_when_no_request() -> None:
    """Stale callbacks (no pending key) report False; no exception."""
    _, _, _, gate = _setup()
    ok = gate.resolve_pending(
        session_id="s1",
        capability_id="x",
        decision=True,
        persist=False,
    )
    assert ok is False


async def test_resolve_pending_double_call_is_noop() -> None:
    """Second resolve for the same key is a no-op (False return)."""
    _, _, _, gate = _setup()

    seen: list[bool] = []

    async def handler(sid, claim, _scope) -> bool:
        async def _later() -> None:
            await asyncio.sleep(0.005)
            # First click.
            seen.append(gate.resolve_pending(
                session_id=sid, capability_id=claim.capability_id,
                decision=True, persist=False,
            ))
            await asyncio.sleep(0.005)
            # Double-click: pending entry already cleared by the
            # request_approval consumer, so this is False.
            seen.append(gate.resolve_pending(
                session_id=sid, capability_id=claim.capability_id,
                decision=False, persist=False,
            ))
        asyncio.create_task(_later())
        return True

    gate.set_prompt_handler(handler)
    decision = await gate.request_approval(
        claim=_claim(), scope=None, session_id="s1", timeout_s=5.0,
    )
    # Wait briefly for the second resolve attempt to run after
    # request_approval has popped the entry.
    await asyncio.sleep(0.05)
    assert decision.allowed is True
    assert seen == [True, False]


async def test_late_callback_after_timeout_is_noop() -> None:
    """A click that arrives after the timeout finds no pending entry."""
    _, _, _, gate = _setup()

    async def handler(_sid, _claim, _scope) -> bool:
        return True

    gate.set_prompt_handler(handler)
    decision = await gate.request_approval(
        claim=_claim(), scope=None, session_id="s1", timeout_s=0.05,
    )
    assert decision.allowed is False
    # Stale click after the timeout has cleared the entry.
    ok = gate.resolve_pending(
        session_id="s1", capability_id="read_files.metadata",
        decision=True, persist=True,
    )
    assert ok is False


async def test_audit_event_recorded_for_approval() -> None:
    """Each approval (allow / deny / timeout) gets its own audit row."""
    _, _, audit, gate = _setup()

    async def handler(sid, claim, _scope) -> bool:
        asyncio.create_task(asyncio.sleep(0.005))

        async def _later() -> None:
            await asyncio.sleep(0.005)
            gate.resolve_pending(
                session_id=sid, capability_id=claim.capability_id,
                decision=True, persist=True,
            )
        asyncio.create_task(_later())
        return True

    gate.set_prompt_handler(handler)
    await gate.request_approval(
        claim=_claim(), scope="/tmp/x", session_id="s1", timeout_s=5.0,
    )
    rows = audit.query(session_id="s1", limit=10)
    actions = [r["action"] for r in rows]
    assert "approval_allow_always" in actions
