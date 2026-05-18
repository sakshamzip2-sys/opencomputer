"""A8 — /handoff completes a profile swap on the gateway.

The gateway's slash-command runtime is ephemeral, so /handoff records
its target as a *persistent* profile override in the runtime-state
store; the dispatcher applies that override on every subsequent turn
(mirroring the CLI persisting the active profile to disk).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.slash_commands_impl.handoff_cmd import HandoffCommand
from opencomputer.gateway.dispatch import Dispatch
from opencomputer.gateway.runtime_state import GatewayRuntimeState
from plugin_sdk.core import MessageEvent, Platform
from plugin_sdk.runtime_context import RuntimeContext

# ─── store: persistent profile override ─────────────────────────────────


def test_profile_override_defaults_none() -> None:
    assert GatewayRuntimeState(path=None).get_profile_override("s1") is None


def test_profile_override_set_get_clear() -> None:
    state = GatewayRuntimeState(path=None)
    state.set_profile_override("s1", "stocks")
    assert state.get_profile_override("s1") == "stocks"
    state.clear_profile_override("s1")
    assert state.get_profile_override("s1") is None


def test_profile_override_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "gateway" / "runtime_state.json"
    state = GatewayRuntimeState(path=path)
    state.set_profile_override("sess-A", "research")
    # plan_mode + override coexist on the same session entry.
    state.set_plan_mode("sess-A", True)
    reloaded = GatewayRuntimeState(path=path)
    assert reloaded.get_profile_override("sess-A") == "research"
    assert reloaded.get_plan_mode("sess-A") is True


# ─── /handoff command on the gateway ────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    from opencomputer.gateway import runtime_state as rs

    monkeypatch.setattr(rs, "_active", GatewayRuntimeState(path=None))


@pytest.mark.asyncio
async def test_handoff_on_gateway_queues_docless_swap(tmp_path, monkeypatch):
    """No provider adapter (the typical gateway bypass case) → /handoff
    queues a doc-less swap and writes the persistent override."""
    import opencomputer.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "list_profiles", lambda: ["stocks"])
    monkeypatch.setattr(
        profiles_mod, "get_profile_dir", lambda name: tmp_path / (name or "x"),
    )
    from opencomputer.gateway.runtime_state import get_runtime_state

    rt = RuntimeContext(
        custom={"session_id": "sess-X", "active_profile_id": "default"},
    )
    result = await HandoffCommand().execute("stocks", rt)

    assert "Swap queued" in result.output
    assert get_runtime_state().get_profile_override("sess-X") == "stocks"


@pytest.mark.asyncio
async def test_handoff_rejects_unknown_profile(tmp_path, monkeypatch):
    import opencomputer.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "list_profiles", lambda: ["stocks"])
    monkeypatch.setattr(
        profiles_mod, "get_profile_dir", lambda name: tmp_path / (name or "x"),
    )
    from opencomputer.gateway.runtime_state import get_runtime_state

    rt = RuntimeContext(
        custom={"session_id": "sess-X", "active_profile_id": "default"},
    )
    result = await HandoffCommand().execute("nonesuch", rt)

    assert "not found" in result.output
    # No swap recorded for a rejected target.
    assert get_runtime_state().get_profile_override("sess-X") is None


def test_handoff_command_is_gateway_safe() -> None:
    assert HandoffCommand.gateway_safe is True
    assert HandoffCommand.bypass_running_guard is True


# ─── dispatcher applies the override ─────────────────────────────────────


def _sid(platform: str, chat_id: str) -> str:
    return hashlib.sha256(f"{platform}:{chat_id}".encode()).hexdigest()[:32]


def _make_loop():
    fake_loop = MagicMock()

    async def fake_run(user_message: str, session_id: str, **kw):
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        return result

    fake_loop.run_conversation = fake_run
    return fake_loop


def _evt() -> MessageEvent:
    return MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="77",
        user_id="u",
        text="hello",
        timestamp=0.0,
    )


@pytest.mark.asyncio
async def test_dispatch_routes_to_overridden_profile(tmp_path, monkeypatch):
    """With a /handoff override recorded, the dispatcher loads the loop
    for the swapped-to profile, not the binding-resolved one."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.gateway.agent_router import AgentRouter

    requested: list[str] = []
    loop = _make_loop()

    def _factory(pid: str, home):
        requested.append(pid)
        return loop

    router = AgentRouter(
        loop_factory=_factory,
        profile_home_resolver=lambda pid: tmp_path / "home",
    )
    dispatch = Dispatch(router=router)
    sid = _sid("telegram", "77")
    dispatch._runtime_state.set_profile_override(sid, "stocks")

    await dispatch.handle_message(_evt())

    # The override (stocks) drove loop resolution, not the default.
    assert "stocks" in requested


@pytest.mark.asyncio
async def test_dispatch_no_override_uses_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    from opencomputer.gateway.agent_router import AgentRouter

    requested: list[str] = []
    loop = _make_loop()
    router = AgentRouter(
        loop_factory=lambda pid, home: (requested.append(pid), loop)[1],
        profile_home_resolver=lambda pid: tmp_path / "home",
    )
    dispatch = Dispatch(router=router)

    await dispatch.handle_message(_evt())

    assert requested == ["default"]
