"""Dispatch routes through AgentRouter with profile_id='default' in Phase 2.

Phase 3 will add the BindingResolver and per-event profile_id resolution.
For now this verifies:
  - Dispatch.__init__ accepts router= or loop= (not both, not neither).
  - Dispatch._do_dispatch resolves to "default" and runs the router-cached loop.
  - Per-(profile, session) lock keys are tuples now.
  - Consent prompt handler is registered on each per-profile loop's gate.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.gateway.agent_router import AgentRouter
from opencomputer.gateway.dispatch import Dispatch
from plugin_sdk.core import MessageEvent, Platform


@pytest.mark.asyncio
async def test_dispatch_routes_via_router_default(tmp_path: Path) -> None:
    """End-to-end: Dispatch accepts router=, resolves profile_id='default',
    fetches loop via get_or_load, runs run_conversation."""
    fake_loop = MagicMock()

    async def fake_run(user_message: str, session_id: str, **kw):
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        return result

    fake_loop.run_conversation = fake_run

    router = AgentRouter(
        loop_factory=lambda pid, home: fake_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )

    dispatch = Dispatch(router=router)
    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="123",
        user_id="u",
        text="hi",
        timestamp=0.0,
        attachments=[],
        metadata={},
    )
    out = await dispatch.handle_message(event)
    assert out == "ok"


def test_dispatch_init_rejects_both_loop_and_router() -> None:
    """Can't pass both loop and router."""
    fake_loop = MagicMock()
    fake_router = MagicMock(spec=AgentRouter)
    with pytest.raises(ValueError, match="not both"):
        Dispatch(loop=fake_loop, router=fake_router)


def test_dispatch_init_rejects_neither_loop_nor_router() -> None:
    """Must pass exactly one of loop or router."""
    with pytest.raises(ValueError, match="either"):
        Dispatch()


def test_dispatch_init_legacy_loop_works() -> None:
    """Backwards-compat: passing loop= still works (wrapped into one-entry router)."""
    fake_loop = MagicMock()
    dispatch = Dispatch(loop=fake_loop)
    assert dispatch.loop is fake_loop  # legacy attribute access
    assert "default" in dispatch._router._loops


@pytest.mark.asyncio
async def test_dispatch_lock_key_is_tuple(tmp_path: Path) -> None:
    """Per-(profile_id, session_id) lock keys are tuples now."""
    fake_loop = MagicMock()

    async def fake_run(user_message: str, session_id: str, **kw):
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        return result

    fake_loop.run_conversation = fake_run

    router = AgentRouter(
        loop_factory=lambda pid, home: fake_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    dispatch = Dispatch(router=router)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="u",
        text="hi",
        timestamp=0.0,
        attachments=[],
        metadata={},
    )
    await dispatch.handle_message(event)

    # After one dispatch, the lock map should hold a tuple key.
    assert len(dispatch._locks) == 1
    only_key = next(iter(dispatch._locks))
    assert isinstance(only_key, tuple)
    assert only_key[0] == "default"
    # session id is a 32-char hex (sha256 truncated)
    assert isinstance(only_key[1], str) and len(only_key[1]) == 32


def test_consent_prompt_fires_on_per_profile_gate() -> None:
    """Pass-2 F7: when the Gateway wraps the factory to register the
    consent prompt handler, each per-profile loop's gate gets it."""
    from opencomputer.gateway.server import Gateway

    # Build a minimal loop with a consent gate that records prompt_handler set calls.
    handlers_set: list = []

    class _FakeGate:
        def set_prompt_handler(self, handler) -> None:
            handlers_set.append(handler)

    fake_loop = MagicMock()
    fake_loop._consent_gate = _FakeGate()

    Gateway(loop=fake_loop)
    # The seeded "default" loop's gate should have the handler registered.
    assert len(handlers_set) == 1, (
        f"expected 1 handler registration on default loop's gate; got {len(handlers_set)}"
    )
