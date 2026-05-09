"""Hermes parity: SESSION_FINALIZE clears session-scoped grants in production wiring.

Two layers of test:
1. ``register_session_finalize_handler`` actually subscribes to the
   real hook engine.
2. Firing SESSION_FINALIZE through the engine clears matching grants.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from opencomputer.agent.consent.audit import AuditLogger
from opencomputer.agent.consent.gate import ConsentGate
from opencomputer.agent.consent.store import ConsentStore
from opencomputer.agent.state import apply_migrations
from opencomputer.hooks.engine import HookEngine
from plugin_sdk import ConsentGrant, ConsentTier
from plugin_sdk.hooks import HookContext, HookEvent


def _gate():
    tmp = Path(tempfile.mkdtemp())
    c = sqlite3.connect(tmp / "t.db", check_same_thread=False)
    apply_migrations(c)
    store = ConsentStore(c)
    log = AuditLogger(c, hmac_key=b"k" * 16)
    return ConsentGate(store=store, audit=log)


def test_register_session_finalize_handler_subscribes_to_engine():
    """Calling the helper actually adds a HookSpec at SESSION_FINALIZE."""
    from opencomputer.hooks import engine as engine_module

    real_engine = engine_module.engine
    fresh = HookEngine()
    engine_module.engine = fresh
    try:
        gate = _gate()
        # Engine starts with zero hooks at SESSION_FINALIZE.
        assert fresh._ordered_specs(HookEvent.SESSION_FINALIZE) == []
        gate.register_session_finalize_handler()
        specs = fresh._ordered_specs(HookEvent.SESSION_FINALIZE)
        assert len(specs) == 1
        assert specs[0].event == HookEvent.SESSION_FINALIZE
    finally:
        engine_module.engine = real_engine


@pytest.mark.asyncio
async def test_firing_session_finalize_clears_session_grants():
    """End-to-end: register the handler, fire SESSION_FINALIZE, verify
    matching grants are popped while non-matching survive."""
    from opencomputer.hooks import engine as engine_module

    real_engine = engine_module.engine
    fresh = HookEngine()
    engine_module.engine = fresh
    try:
        gate = _gate()
        gate.register_session_finalize_handler()

        g = ConsentGrant(
            "execute_code.run", ConsentTier.PER_ACTION, None,
            time.time(), None, "user",
        )
        gate._session_grants[("s-keep", "execute_code.run")] = g
        gate._session_grants[("s-end", "execute_code.run")] = g
        gate._session_grants[("s-end", "Bash.execute")] = g

        await fresh.fire_blocking(HookContext(
            event=HookEvent.SESSION_FINALIZE,
            session_id="s-end",
        ))

        # All s-end grants gone, s-keep survives.
        assert ("s-end", "execute_code.run") not in gate._session_grants
        assert ("s-end", "Bash.execute") not in gate._session_grants
        assert ("s-keep", "execute_code.run") in gate._session_grants
    finally:
        engine_module.engine = real_engine


def test_register_helper_is_idempotent_safe_under_double_call():
    """Calling twice doesn't crash — a second registration is harmless
    (handler just fires twice, both invocations are idempotent on the
    same session_id)."""
    from opencomputer.hooks import engine as engine_module

    real_engine = engine_module.engine
    fresh = HookEngine()
    engine_module.engine = fresh
    try:
        gate = _gate()
        gate.register_session_finalize_handler()
        gate.register_session_finalize_handler()
        specs = fresh._ordered_specs(HookEvent.SESSION_FINALIZE)
        # Both registered — but the handler is idempotent (clearing an
        # already-empty session is a no-op).
        assert len(specs) == 2
    finally:
        engine_module.engine = real_engine
