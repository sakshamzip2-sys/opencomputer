"""A9 — a binding's ``queue_mode`` seeds the session's queue mode.

The binding is the *default*: it is applied exactly once, the first time
the gateway dispatches for the session. A later ``/queue-mode`` from the
user is never clobbered.
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from opencomputer.agent.bindings_config import (
    Binding,
    BindingMatch,
    BindingsConfig,
)
from opencomputer.gateway.binding_resolver import BindingResolver
from opencomputer.gateway.dispatch import Dispatch
from opencomputer.gateway.queue_manager import QueueManager
from plugin_sdk.core import MessageEvent, Platform


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


def _evt(text: str = "hi") -> MessageEvent:
    return MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="42",
        user_id="u",
        text=text,
        timestamp=0.0,
    )


def _resolver(queue_mode: str | None) -> BindingResolver:
    return BindingResolver(
        BindingsConfig(
            default_profile="default",
            bindings=(
                Binding(
                    match=BindingMatch(platform="telegram"),
                    profile="default",
                    queue_mode=queue_mode,
                ),
            ),
        )
    )


def test_queue_manager_has_session_mode() -> None:
    qm = QueueManager()
    assert qm.has_session_mode("s1") is False
    qm.set_session_mode("s1", "collect")
    assert qm.has_session_mode("s1") is True


@pytest.mark.asyncio
async def test_binding_queue_mode_seeds_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    dispatch = Dispatch(loop=_make_loop(), resolver=_resolver("interrupt"))
    sid = _sid("telegram", "42")
    assert not dispatch._queue_manager.has_session_mode(sid)

    await dispatch.handle_message(_evt())

    assert dispatch._queue_manager.get_session_mode(sid) == "interrupt"


@pytest.mark.asyncio
async def test_seed_does_not_clobber_user_choice(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    dispatch = Dispatch(loop=_make_loop(), resolver=_resolver("interrupt"))
    sid = _sid("telegram", "42")
    # User flipped the mode via /queue-mode before the first dispatch.
    dispatch._queue_manager.set_session_mode(sid, "followup")

    await dispatch.handle_message(_evt())

    # The binding default must NOT override the user's explicit choice.
    assert dispatch._queue_manager.get_session_mode(sid) == "followup"


@pytest.mark.asyncio
async def test_no_binding_queue_mode_leaves_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))
    dispatch = Dispatch(loop=_make_loop(), resolver=_resolver(None))
    sid = _sid("telegram", "42")

    await dispatch.handle_message(_evt())

    # No binding queue_mode → no seeding → the process default stands.
    assert not dispatch._queue_manager.has_session_mode(sid)
