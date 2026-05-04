"""Tests for HookEvent.USER_PROMPT_SUBMIT firing in run_conversation (E7-T1)."""

from __future__ import annotations

import pytest

from opencomputer.hooks.engine import engine as _hook_engine
from plugin_sdk.hooks import HookContext, HookEvent, HookSpec


@pytest.fixture(autouse=True)
def _reset_hook_engine():
    """Snapshot + restore the global hook engine state per test.

    HookEngine stores hooks as a defaultdict[HookEvent, list[tuple[...]]];
    a shallow copy of the dict + each value list is enough to round-trip.
    """
    if not hasattr(_hook_engine, "_hooks"):
        yield
        return
    saved = {ev: list(specs) for ev, specs in _hook_engine._hooks.items()}
    saved_seq = _hook_engine._next_seq
    yield
    _hook_engine._hooks.clear()
    for ev, specs in saved.items():
        _hook_engine._hooks[ev] = specs
    _hook_engine._next_seq = saved_seq


def test_user_prompt_submit_event_is_defined():
    """The enum value exists and matches the SDK convention."""
    assert HookEvent.USER_PROMPT_SUBMIT.value == "UserPromptSubmit"


def test_hookcontext_carries_message_field():
    """HookContext.message is the field user-prompt subscribers read."""
    from plugin_sdk.core import Message
    ctx = HookContext(
        event=HookEvent.USER_PROMPT_SUBMIT,
        session_id="abc",
        message=Message(role="user", content="hello world"),
    )
    assert ctx.message is not None
    assert ctx.message.content == "hello world"
    assert ctx.message.role == "user"


def test_hook_spec_can_subscribe_to_user_prompt_submit():
    """Round-trip: register a subscriber, hook engine recognizes it."""
    received: list[str] = []

    async def _subscriber(ctx: HookContext):
        if ctx.message and isinstance(ctx.message.content, str):
            received.append(ctx.message.content)

    spec = HookSpec(event=HookEvent.USER_PROMPT_SUBMIT, handler=_subscriber)
    _hook_engine.register(spec)

    # Verify the spec is in the engine's registry
    matching = _hook_engine._ordered_specs(HookEvent.USER_PROMPT_SUBMIT)
    assert any(s is spec for s in matching)
