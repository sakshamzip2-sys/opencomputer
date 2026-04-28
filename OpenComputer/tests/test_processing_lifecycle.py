"""Tests for reaction-lifecycle hooks (Hermes PR 2 Task 2.2 + amendment §A.7).

Coverage:
* on_processing_start adds 👀 when REACTIONS capability is set.
* on_processing_complete maps SUCCESS → ✅, FAILURE → ❌, CANCELLED → no-op.
* No-cap adapters silently no-op.
* Reaction-send failures are swallowed.
* Dispatch wires the hooks around run_conversation.
* Amendment §A.7: integration test that verifies ordering during a
  ConsentGate prompt — start-reaction → consent prompt → user click →
  complete-reaction → reply, with ConsentGate.resolve_pending called
  exactly once.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, ProcessingOutcome, SendResult


class _ReactiveAdapter(BaseChannelAdapter):
    platform = Platform.TELEGRAM
    capabilities = ChannelCapabilities.REACTIONS

    def __init__(self, config) -> None:
        super().__init__(config)
        self.reactions: list[tuple[str, str, str]] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, text, **kwargs):
        return SendResult(success=True)

    async def send_reaction(self, chat_id, message_id, emoji, **kwargs):
        self.reactions.append((chat_id, message_id, emoji))
        return SendResult(success=True)


# ─── Default lifecycle behaviour ─────────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_start_adds_eyes_reaction_when_capable() -> None:
    adapter = _ReactiveAdapter({})
    await adapter.on_processing_start("chat-1", "msg-42")
    assert ("chat-1", "msg-42", "👀") in adapter.reactions


@pytest.mark.asyncio
async def test_on_processing_complete_success_replaces_with_check() -> None:
    adapter = _ReactiveAdapter({})
    await adapter.on_processing_start("chat", "1")
    await adapter.on_processing_complete("chat", "1", ProcessingOutcome.SUCCESS)
    assert ("chat", "1", "✅") in adapter.reactions


@pytest.mark.asyncio
async def test_on_processing_complete_failure_uses_cross() -> None:
    adapter = _ReactiveAdapter({})
    await adapter.on_processing_complete("chat", "1", ProcessingOutcome.FAILURE)
    assert ("chat", "1", "❌") in adapter.reactions


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_is_noop() -> None:
    """CANCELLED leaves the 👀 reaction in place — no replace, no clear."""
    adapter = _ReactiveAdapter({})
    await adapter.on_processing_start("chat", "1")
    starting = list(adapter.reactions)
    await adapter.on_processing_complete("chat", "1", ProcessingOutcome.CANCELLED)
    # No new reaction added (and existing eye stays).
    assert adapter.reactions == starting


@pytest.mark.asyncio
async def test_no_reactions_capability_is_noop() -> None:
    class _PlainAdapter(_ReactiveAdapter):
        capabilities = ChannelCapabilities.NONE

    adapter = _PlainAdapter({})
    await adapter.on_processing_start("chat", "1")
    await adapter.on_processing_complete("chat", "1", ProcessingOutcome.SUCCESS)
    assert adapter.reactions == []


@pytest.mark.asyncio
async def test_no_message_id_skips_reaction() -> None:
    """No message_id → can't react (most adapters key reactions on a msg id)."""
    adapter = _ReactiveAdapter({})
    await adapter.on_processing_start("chat", None)
    await adapter.on_processing_complete("chat", None, ProcessingOutcome.SUCCESS)
    assert adapter.reactions == []


@pytest.mark.asyncio
async def test_reaction_send_failure_swallowed() -> None:
    class _BrokenAdapter(_ReactiveAdapter):
        async def send_reaction(self, *a, **kw):
            raise RuntimeError("api down")

    adapter = _BrokenAdapter({})
    # Should NOT raise.
    await adapter.on_processing_start("chat", "1")
    await adapter.on_processing_complete("chat", "1", ProcessingOutcome.SUCCESS)


# ─── Dispatch wiring (Step 2.2.3) ────────────────────────────────────


def _conversation_result(text: str):
    """Build a real-shaped ConversationResult for AsyncMock returns."""
    from opencomputer.agent.loop import ConversationResult
    from plugin_sdk.core import Message

    final = Message(role="assistant", content=text)
    return ConversationResult(
        final_message=final,
        messages=[final],
        session_id="s",
        iterations=1,
        input_tokens=0,
        output_tokens=0,
    )


@pytest.mark.asyncio
async def test_dispatch_fires_lifecycle_hooks_around_agent_loop() -> None:
    """Dispatch.handle_message fires start before, complete after, run_conversation."""
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import MessageEvent

    loop_mock = MagicMock()
    loop_mock.run_conversation = AsyncMock(return_value=_conversation_result("hi back"))

    d = Dispatch(loop_mock)
    adapter = _ReactiveAdapter({})
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="c1",
        user_id="u1",
        text="hello",
        timestamp=1.0,
        metadata={"message_id": "42"},
    )
    out = await d.handle_message(event)
    assert out == "hi back"

    # Allow fire-and-forget hook tasks to run.
    for _ in range(5):
        await asyncio.sleep(0)

    eyes = [r for r in adapter.reactions if r[2] == "👀"]
    check = [r for r in adapter.reactions if r[2] == "✅"]
    assert eyes, f"expected 👀 reaction, got {adapter.reactions!r}"
    assert check, f"expected ✅ reaction, got {adapter.reactions!r}"


@pytest.mark.asyncio
async def test_dispatch_fires_failure_hook_on_exception() -> None:
    """Exception in run_conversation → on_processing_complete(FAILURE)."""
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import MessageEvent

    loop_mock = MagicMock()
    loop_mock.run_conversation = AsyncMock(side_effect=RuntimeError("kaboom"))

    d = Dispatch(loop_mock)
    adapter = _ReactiveAdapter({})
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="c1",
        user_id="u1",
        text="hello",
        timestamp=1.0,
        metadata={"message_id": "99"},
    )
    # Exception is swallowed; user-facing error returned.
    out = await d.handle_message(event)
    assert isinstance(out, str)

    for _ in range(5):
        await asyncio.sleep(0)

    cross = [r for r in adapter.reactions if r[2] == "❌"]
    assert cross, f"expected ❌ reaction on failure, got {adapter.reactions!r}"


# ─── Amendment §A.7 — consent × lifecycle integration ────────────────


@pytest.mark.asyncio
async def test_processing_lifecycle_during_consent_prompt() -> None:
    """Verify ordering: start-reaction → consent prompt → user click →
    complete-reaction → reply, when a fake ConsentGate prompts mid-conversation.

    Per amendment §A.7: assert ConsentGate.resolve_pending is called
    exactly once and no parallel approval state is created.
    """
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import MessageEvent

    # ─── 1. Build a fake ConsentGate that records calls + prompts mid-flight
    call_log: list[str] = []

    class _FakeGate:
        def __init__(self) -> None:
            self.resolve_count = 0
            self._prompt_handler = None
            self._pending_event: asyncio.Event | None = None

        def set_prompt_handler(self, handler) -> None:
            self._prompt_handler = handler

        @staticmethod
        def render_prompt(claim, scope) -> str:
            return f"Allow {claim} on {scope}?"

        def resolve_pending(self, *, session_id, capability_id, decision, persist):
            self.resolve_count += 1
            call_log.append(f"resolve_pending#{self.resolve_count}")
            if self._pending_event is not None:
                self._pending_event.set()
            return True

        async def trigger_prompt_during_run(self, session_id) -> None:
            """Simulate the consent gate firing a prompt mid-conversation."""
            call_log.append("consent_prompt_dispatched")
            assert self._prompt_handler is not None, "dispatch should bind handler"
            self._pending_event = asyncio.Event()

            class _Claim:
                capability_id = "files.read"

                def __repr__(self) -> str:  # readable in render_prompt
                    return "files.read"

            await self._prompt_handler(session_id, _Claim(), "/tmp/x")
            # Event will be set by resolve_pending below.

    gate = _FakeGate()

    # ─── 2. Build an adapter that supports approval prompts + reactions
    class _ApprovalAdapter(_ReactiveAdapter):
        def __init__(self, config) -> None:
            super().__init__(config)
            self.approval_callback = None
            self.events: list[str] = []

        def set_approval_callback(self, cb) -> None:
            self.approval_callback = cb

        async def send_reaction(self, chat_id, message_id, emoji, **kwargs):
            self.events.append(f"react:{emoji}")
            self.reactions.append((chat_id, message_id, emoji))
            return SendResult(success=True)

        async def send_approval_request(self, *, chat_id, prompt_text, request_token):
            self.events.append(f"prompt:{request_token[:6]}")
            # Stash token so the test can simulate the user click below.
            self.last_token = request_token
            return SendResult(success=True)

        async def send(self, chat_id, text, **kwargs):
            self.events.append(f"send:{text}")
            return SendResult(success=True)

    adapter = _ApprovalAdapter({})

    # ─── 3. Mock loop.run_conversation: triggers the consent prompt mid-flight
    async def fake_run_conversation(*, user_message, session_id):
        # 3a. Mid-conversation, the agent hits a Tier-2 claim → gate prompts.
        prompt_task = asyncio.create_task(gate.trigger_prompt_during_run(session_id))
        # 3b. Yield once to let prompt-send fire.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # 3c. Simulate the user clicking "allow once" → adapter callback.
        assert hasattr(adapter, "last_token"), "prompt should have been sent"
        assert adapter.approval_callback is not None
        await adapter.approval_callback("once", adapter.last_token)
        # 3d. Wait for prompt task to complete.
        await prompt_task
        return _conversation_result("approved + done")

    loop_mock = MagicMock()
    loop_mock._consent_gate = gate
    loop_mock.run_conversation = AsyncMock(side_effect=fake_run_conversation)

    d = Dispatch(loop_mock)
    d.register_adapter(Platform.TELEGRAM.value, adapter)

    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="c1",
        user_id="u1",
        text="please read /tmp/x",
        timestamp=1.0,
        metadata={"message_id": "501"},
    )
    out = await d.handle_message(event)
    assert out == "approved + done"

    # Allow fire-and-forget hooks to run.
    for _ in range(10):
        await asyncio.sleep(0)

    # ─── 4. Assert ordering: 👀 → consent prompt → click → ✅
    eye_idx = next(
        (i for i, e in enumerate(adapter.events) if e == "react:👀"), None
    )
    prompt_idx = next(
        (i for i, e in enumerate(adapter.events) if e.startswith("prompt:")), None
    )
    check_idx = next(
        (i for i, e in enumerate(adapter.events) if e == "react:✅"), None
    )
    assert eye_idx is not None, f"expected 👀 reaction, events={adapter.events!r}"
    assert prompt_idx is not None, (
        f"expected approval prompt, events={adapter.events!r}"
    )
    assert check_idx is not None, f"expected ✅ reaction, events={adapter.events!r}"
    # 👀 must precede the prompt; ✅ must come after the click.
    assert eye_idx < prompt_idx < check_idx, (
        f"order violated: events={adapter.events!r}"
    )

    # ─── 5. Assert resolve_pending called exactly once
    assert gate.resolve_count == 1, (
        f"resolve_pending called {gate.resolve_count}× — "
        "expected exactly 1 (no parallel approval state)"
    )
