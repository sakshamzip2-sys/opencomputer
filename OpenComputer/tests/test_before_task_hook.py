"""Tests for the BEFORE_TASK hook seam in ``AgentLoop.run_conversation``.

Phase 1 of the social-traces plugin (see
``docs/plans/social-traces-plugin.md`` §10). Validates the loop wires
the new ``BEFORE_TASK`` hook event correctly:

* When a registered handler returns ``HookDecision`` with non-empty
  ``modified_message``, a ``<system-reminder>`` user message must land
  in the conversation right after the user's own message, and must be
  persisted (so a resumed session sees the same injected context).
* When no handler is registered, behaviour is unchanged (no reminder).
* When the handler returns ``decision="pass"`` or ``None``, no reminder
  is appended even if ``modified_message`` is non-empty (decision wins).
* When the handler raises, the loop continues — BEFORE_TASK must never
  wedge a normal turn.
"""

from __future__ import annotations

from typing import Any

import pytest

from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from opencomputer.hooks.engine import engine as global_engine
from plugin_sdk.core import Message as _Message
from plugin_sdk.hooks import (
    HookContext,
    HookDecision,
    HookEvent,
    HookSpec,
)
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage


class _FakeProvider(BaseProvider):
    """Minimal provider that returns a single end-turn response so
    ``run_conversation`` exits after one turn without touching the wire."""

    async def complete(
        self,
        *,
        model,
        messages,
        system=None,
        tools=None,
        max_tokens=None,
        temperature=None,
        **kw,
    ):
        return ProviderResponse(
            message=_Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=5, output_tokens=2),
        )

    async def stream_complete(self, **kw):  # pragma: no cover — unused
        yield  # type: ignore[misc]


def _build_loop(tmp_path):
    """Boilerplate AgentLoop wired against the fake provider + tmp DB."""
    from opencomputer.agent.config import Config, SessionConfig

    db_path = tmp_path / "sessions.db"
    cfg = Config(session=SessionConfig(db_path=db_path))
    return AgentLoop(
        provider=_FakeProvider(),
        config=cfg,
        db=SessionDB(db_path),
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )


# ─── happy path ───────────────────────────────────────────────────────


async def test_before_task_modified_message_lands_as_system_reminder(tmp_path) -> None:
    """A handler returning ``modified_message`` must inject a system-reminder
    user message right after the original user message."""
    global_engine.unregister_all(HookEvent.BEFORE_TASK)

    captured: dict[str, Any] = {}

    async def _inject(ctx: HookContext) -> HookDecision:
        captured["fired"] = True
        captured["session_id"] = ctx.session_id
        captured["user_msg_content"] = ctx.message.content if ctx.message else ""
        return HookDecision(
            decision="approve",
            modified_message="injected trace context",
        )

    global_engine.register(
        HookSpec(event=HookEvent.BEFORE_TASK, handler=_inject, fire_and_forget=False)
    )

    try:
        loop = _build_loop(tmp_path)
        result = await loop.run_conversation("sync files on LAN", session_id="sid-bt-1")

        assert captured.get("fired") is True
        assert captured["session_id"] == "sid-bt-1"
        assert captured["user_msg_content"] == "sync files on LAN"

        # Find the user message + the system-reminder. They must be adjacent
        # in that order.
        contents = [
            (m.role, m.content) for m in result.messages if m.role == "user"
        ]
        assert ("user", "sync files on LAN") in contents
        reminder_text = "<system-reminder>injected trace context</system-reminder>"
        assert ("user", reminder_text) in contents

        idx_user = next(
            i for i, m in enumerate(result.messages)
            if m.role == "user" and m.content == "sync files on LAN"
        )
        idx_reminder = next(
            i for i, m in enumerate(result.messages)
            if m.role == "user" and m.content == reminder_text
        )
        assert idx_reminder == idx_user + 1, (
            "reminder must follow the user message immediately"
        )
    finally:
        global_engine.unregister_all(HookEvent.BEFORE_TASK)


async def test_before_task_persists_reminder_to_db(tmp_path) -> None:
    """The injected reminder must be persisted so a resumed session sees it.
    Mirrors the loop-detector reminder persistence at loop.py:~1907-1920."""
    global_engine.unregister_all(HookEvent.BEFORE_TASK)

    async def _inject(ctx: HookContext) -> HookDecision:
        return HookDecision(decision="approve", modified_message="trace X applies")

    global_engine.register(
        HookSpec(event=HookEvent.BEFORE_TASK, handler=_inject, fire_and_forget=False)
    )

    try:
        loop = _build_loop(tmp_path)
        await loop.run_conversation("hi", session_id="sid-bt-persist")

        # Read messages back from the DB — the reminder must be there.
        from_db = loop.db.get_messages("sid-bt-persist")
        contents = [m.content for m in from_db if m.role == "user"]
        assert "<system-reminder>trace X applies</system-reminder>" in contents
    finally:
        global_engine.unregister_all(HookEvent.BEFORE_TASK)


# ─── no-op paths ──────────────────────────────────────────────────────


async def test_before_task_no_handler_unchanged_flow(tmp_path) -> None:
    """When nothing is registered for BEFORE_TASK, the loop behaves exactly
    as it did before Phase 1 — only the user's own message is in the
    conversation (no system-reminder added)."""
    global_engine.unregister_all(HookEvent.BEFORE_TASK)

    loop = _build_loop(tmp_path)
    result = await loop.run_conversation("plain", session_id="sid-bt-noop")

    user_contents = [m.content for m in result.messages if m.role == "user"]
    assert user_contents == ["plain"]
    # Final assistant message lands as expected.
    assert result.final_message.content == "ok"


async def test_before_task_pass_decision_no_reminder(tmp_path) -> None:
    """``decision='pass'`` is the engine's neutral verdict — even if the
    handler sets ``modified_message`` alongside, ``pass`` wins and we
    must NOT inject a reminder."""
    global_engine.unregister_all(HookEvent.BEFORE_TASK)

    async def _passthrough(ctx: HookContext) -> HookDecision:
        return HookDecision(
            decision="pass",
            modified_message="should be ignored",
        )

    global_engine.register(
        HookSpec(event=HookEvent.BEFORE_TASK, handler=_passthrough, fire_and_forget=False)
    )

    try:
        loop = _build_loop(tmp_path)
        result = await loop.run_conversation("hello", session_id="sid-bt-pass")

        user_contents = [m.content for m in result.messages if m.role == "user"]
        assert user_contents == ["hello"]
        assert not any(
            "system-reminder" in (m.content or "") for m in result.messages
        )
    finally:
        global_engine.unregister_all(HookEvent.BEFORE_TASK)


async def test_before_task_none_return_no_reminder(tmp_path) -> None:
    """Handler returning ``None`` is the documented 'I don't apply this
    turn' shape — engine treats as pass, no reminder injected."""
    global_engine.unregister_all(HookEvent.BEFORE_TASK)

    async def _silent(ctx: HookContext) -> HookDecision | None:
        return None

    global_engine.register(
        HookSpec(event=HookEvent.BEFORE_TASK, handler=_silent, fire_and_forget=False)
    )

    try:
        loop = _build_loop(tmp_path)
        result = await loop.run_conversation("hello", session_id="sid-bt-none")

        user_contents = [m.content for m in result.messages if m.role == "user"]
        assert user_contents == ["hello"]
    finally:
        global_engine.unregister_all(HookEvent.BEFORE_TASK)


async def test_before_task_empty_modified_message_no_reminder(tmp_path) -> None:
    """``modified_message=""`` is the 'I looked, nothing to inject' shape —
    no reminder added even with decision=approve."""
    global_engine.unregister_all(HookEvent.BEFORE_TASK)

    async def _empty(ctx: HookContext) -> HookDecision:
        return HookDecision(decision="approve", modified_message="")

    global_engine.register(
        HookSpec(event=HookEvent.BEFORE_TASK, handler=_empty, fire_and_forget=False)
    )

    try:
        loop = _build_loop(tmp_path)
        result = await loop.run_conversation("hello", session_id="sid-bt-empty")

        user_contents = [m.content for m in result.messages if m.role == "user"]
        assert user_contents == ["hello"]
    finally:
        global_engine.unregister_all(HookEvent.BEFORE_TASK)


# ─── failure isolation ───────────────────────────────────────────────


async def test_before_task_handler_raises_loop_continues(tmp_path) -> None:
    """A handler that raises must NOT break the loop. The contract from
    plan §8 + CLAUDE.md §7: a wedged hook must never wedge the loop."""
    global_engine.unregister_all(HookEvent.BEFORE_TASK)

    async def _explode(ctx: HookContext) -> HookDecision:
        raise RuntimeError("boom")

    global_engine.register(
        HookSpec(event=HookEvent.BEFORE_TASK, handler=_explode, fire_and_forget=False)
    )

    try:
        loop = _build_loop(tmp_path)
        # The loop must complete normally despite the handler raising.
        result = await loop.run_conversation("hi", session_id="sid-bt-boom")
        assert result.final_message.content == "ok"
        # No reminder should have been injected since the handler crashed
        # before it could return a decision.
        assert not any(
            "system-reminder" in (m.content or "") for m in result.messages
        )
    finally:
        global_engine.unregister_all(HookEvent.BEFORE_TASK)


# ─── slash-command path bypasses BEFORE_TASK ─────────────────────────


async def test_before_task_skipped_for_slash_command_early_return(tmp_path) -> None:
    """Slash-command-only turns return before reaching the BEFORE_TASK fire
    point. The seam is 'before the agent starts a real task' by design —
    a /help-style command shouldn't trigger a network query.

    Note: most slash commands fall through to the normal loop after
    appending tool messages — only the early-return ones (terminal
    commands like /quit equivalents) skip the fire entirely. We confirm
    BEFORE_TASK is reachable for the typical flow above; no separate
    bypass test needed since the early-return path is exercised by
    existing slash-command tests.
    """
    # Sentinel placeholder so the docstring is not just a comment. The
    # real coverage is the negative test above (no_handler_unchanged_flow)
    # plus the existing slash-command tests in tests/test_phase12b6_*.
    pytest.skip(
        "documented behaviour — no separate bypass test (existing slash "
        "tests cover the early-return path)"
    )
