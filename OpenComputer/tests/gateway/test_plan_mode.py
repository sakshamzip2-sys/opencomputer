"""A2 — per-chat plan mode (/plan) for the gateway.

Covers the persistent store, the /plan slash command, and the
dispatcher injecting plan_mode onto the per-turn RuntimeContext.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.slash_commands_impl.plan_cmd import PlanCommand
from opencomputer.gateway.dispatch import Dispatch
from opencomputer.gateway.runtime_state import GatewayRuntimeState
from plugin_sdk.core import MessageEvent, Platform
from plugin_sdk.runtime_context import RuntimeContext


# ─── GatewayRuntimeState ────────────────────────────────────────────────


def test_runtime_state_defaults_false() -> None:
    state = GatewayRuntimeState(path=None)
    assert state.get_plan_mode("s1") is False


def test_runtime_state_set_get() -> None:
    state = GatewayRuntimeState(path=None)
    state.set_plan_mode("s1", True)
    assert state.get_plan_mode("s1") is True
    state.set_plan_mode("s1", False)
    assert state.get_plan_mode("s1") is False


def test_runtime_state_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "gateway" / "runtime_state.json"
    state = GatewayRuntimeState(path=path)
    state.set_plan_mode("sess-A", True)
    assert path.exists()
    # A fresh instance reads the toggle back from disk.
    reloaded = GatewayRuntimeState(path=path)
    assert reloaded.get_plan_mode("sess-A") is True
    assert reloaded.get_plan_mode("sess-B") is False


def test_runtime_state_corrupt_file_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "runtime_state.json"
    path.write_text("{not json")
    state = GatewayRuntimeState(path=path)
    assert state.get_plan_mode("anything") is False


# ─── /plan slash command ────────────────────────────────────────────────


def _runtime(session_id: str | None = "sess-1") -> RuntimeContext:
    custom = {"session_id": session_id} if session_id else {}
    return RuntimeContext(custom=custom)


@pytest.fixture(autouse=True)
def _fresh_active_store(monkeypatch):
    """Reset the process-wide active store between tests."""
    from opencomputer.gateway import runtime_state as rs

    monkeypatch.setattr(rs, "_active", GatewayRuntimeState(path=None))


@pytest.mark.asyncio
async def test_plan_on_then_status() -> None:
    cmd = PlanCommand()
    res = await cmd.execute("on", _runtime())
    assert "ON" in res.output
    status = await cmd.execute("status", _runtime())
    assert "ON" in status.output


@pytest.mark.asyncio
async def test_plan_off() -> None:
    cmd = PlanCommand()
    await cmd.execute("on", _runtime())
    res = await cmd.execute("off", _runtime())
    assert "OFF" in res.output
    status = await cmd.execute("", _runtime())
    assert "OFF" in status.output


@pytest.mark.asyncio
async def test_plan_no_session_context() -> None:
    res = await PlanCommand().execute("on", _runtime(session_id=None))
    assert "no session context" in res.output


@pytest.mark.asyncio
async def test_plan_unknown_option() -> None:
    res = await PlanCommand().execute("sideways", _runtime())
    assert "unknown option" in res.output


def test_plan_command_is_gateway_safe() -> None:
    assert PlanCommand.gateway_safe is True
    assert PlanCommand.bypass_running_guard is True


# ─── dispatcher injects plan_mode onto the runtime ──────────────────────


def _sid(platform: str, chat_id: str) -> str:
    return hashlib.sha256(f"{platform}:{chat_id}".encode()).hexdigest()[:32]


def _make_loop():
    calls: list[dict] = []
    fake_loop = MagicMock()

    async def fake_run(user_message: str, session_id: str, **kw):
        calls.append({"text": user_message, "session_id": session_id, **kw})
        result = MagicMock()
        result.final_message = MagicMock(content="ok")
        return result

    fake_loop.run_conversation = fake_run
    fake_loop.calls = calls  # type: ignore[attr-defined]
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
async def test_dispatch_injects_plan_mode_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    loop = _make_loop()
    dispatch = Dispatch(loop=loop)
    sid = _sid("telegram", "77")
    dispatch._runtime_state.set_plan_mode(sid, True)

    await dispatch.handle_message(_evt())

    assert loop.calls, "agent loop was not invoked"
    runtime = loop.calls[0]["runtime"]
    assert runtime.plan_mode is True


@pytest.mark.asyncio
async def test_dispatch_default_no_plan_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    loop = _make_loop()
    dispatch = Dispatch(loop=loop)

    await dispatch.handle_message(_evt())

    assert loop.calls
    assert loop.calls[0]["runtime"].plan_mode is False
