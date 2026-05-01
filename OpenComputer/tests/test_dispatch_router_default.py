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


@pytest.mark.asyncio
async def test_send_approval_prompt_works_with_router_construction(tmp_path: Path) -> None:
    """Critical fix: Dispatch(router=...) (no loop=) construction must
    NOT silently auto-deny approval prompts because self.loop is None.

    The fix resolves the per-profile gate via the router rather than
    self.loop. With this fix, an approval prompt routed to the default
    profile finds the gate and proceeds.
    """
    # Build a fake loop with a fake gate.
    fake_gate = MagicMock()
    fake_gate.render_prompt = MagicMock(return_value="Allow X?")

    fake_loop = MagicMock()
    fake_loop._consent_gate = fake_gate

    # Construct Dispatch via router= (NOT loop=).
    router = AgentRouter(
        loop_factory=lambda pid, home: fake_loop,
        profile_home_resolver=lambda pid: tmp_path / pid,
    )
    # Pre-seed the default loop so get_or_load returns immediately.
    router._loops["default"] = fake_loop

    dispatch = Dispatch(router=router)

    # Verify that even though dispatch.loop may be set from the pre-seeded
    # router entry, the _send_approval_prompt code NOW goes through the
    # router path (not self.loop directly). We prove correctness by using
    # a SEPARATE loop for self.loop vs. the router, so the old code
    # (self.loop._consent_gate) would find the wrong gate.
    different_gate = MagicMock()
    different_gate.render_prompt = MagicMock(return_value="wrong gate")
    different_loop = MagicMock()
    different_loop._consent_gate = different_gate
    # Patch dispatch.loop to point at a different loop/gate than the router.
    dispatch.loop = different_loop

    # Now exercise the approval-prompt path. Build a fake CapabilityClaim,
    # bind a session to a fake adapter, and verify the gate's render_prompt
    # is called (i.e., the function did NOT silently return False).
    fake_adapter = MagicMock()
    fake_adapter.send_approval_request = MagicMock(return_value=MagicMock(success=True))
    dispatch._session_channels["test_session"] = (fake_adapter, "chat_123")
    # Also populate _session_profiles so the router lookup finds "default".
    dispatch._session_profiles["test_session"] = "default"

    fake_claim = MagicMock()
    fake_claim.capability_id = "test_capability"

    # Direct call to the prompt-handler.
    result = await dispatch._send_approval_prompt(
        session_id="test_session",
        claim=fake_claim,
        scope=None,
    )

    # The function should have proceeded (not silently returned False).
    # The router-resolved gate (fake_gate) should have been consulted,
    # NOT dispatch.loop's gate (different_gate). This proves the fix works:
    # the code goes through router._loops[profile_id], not self.loop.
    assert fake_gate.render_prompt.called, (
        "_send_approval_prompt did not consult the router-resolved per-profile "
        "gate — Critical regression: must use router._loops[pid]._consent_gate, "
        "not self.loop._consent_gate."
    )
    assert not different_gate.render_prompt.called, (
        "_send_approval_prompt used self.loop._consent_gate instead of the "
        "router-resolved gate — the fix was not applied correctly."
    )


def test_dispatch_legacy_loop_path_uses_real_home(tmp_path: Path, monkeypatch) -> None:
    """Minor fix: legacy Dispatch(loop=...) path's _profile_home_resolver
    should return _home() (the actual default home), not Path() (CWD).
    """
    fake_loop = MagicMock()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "fakehome"))

    dispatch = Dispatch(loop=fake_loop)
    resolved = dispatch._router._profile_home_resolver("default")

    # Should resolve to the env-var home, not CWD
    assert resolved == tmp_path / "fakehome", (
        f"Expected {tmp_path / 'fakehome'}, got {resolved} "
        f"— legacy loop= path is using Path() (CWD) instead of _home()"
    )
