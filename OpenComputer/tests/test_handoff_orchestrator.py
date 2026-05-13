"""Integration tests for the end-to-end handoff auto-swap pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.handoff import (
    AutoSwapTrigger,
    HandoffGenerationError,
    HandoffInbox,
    ProviderAdapter,
    SwapDecisionReason,
    run_auto_swap_pipeline,
)


@dataclass
class FakeRuntime:
    custom: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeMessage:
    role: str
    content: str


class FakeProvider:
    """Minimal provider stub that returns canned text."""

    def __init__(self, *, response: str, should_raise: bool = False) -> None:
        self._response = response
        self._should_raise = should_raise
        self.complete_calls: list[dict[str, Any]] = []

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        self.complete_calls.append({"args": args, "kwargs": kwargs})
        if self._should_raise:
            raise RuntimeError("simulated provider failure")
        return self._response


@pytest.fixture
def fake_persona_resolver():
    def _resolver(persona: str, available: tuple[str, ...]) -> str | None:
        mapping = {"trading": "stocks", "coding": "coder"}
        target = mapping.get(persona)
        return target if target in available else None
    return _resolver


@pytest.fixture
def messages_about_stocks() -> list[FakeMessage]:
    return [
        FakeMessage("user", "what's NVDA doing today?"),
        FakeMessage("assistant", "Let me check — NVDA is up 2% on volume."),
        FakeMessage("user", "what about the options chain on NVDA?"),
        FakeMessage("assistant", "The 200 calls show heavy IV."),
        FakeMessage("user", "and the trading volume on NVDA?"),
    ]


def _profile_home_resolver(target: str, base: Path):
    return lambda profile_id: base / profile_id / "home"


@pytest.mark.asyncio
async def test_full_pipeline_silent_swap(
    tmp_path: Path,
    fake_persona_resolver,
    messages_about_stocks: list[FakeMessage],
) -> None:
    """Three trading-classified turns → swap fires + handoff written."""
    trigger = AutoSwapTrigger(persona_to_profile=fake_persona_resolver)
    rt = FakeRuntime()
    provider = FakeProvider(
        response=(
            "**Collaboration:** Saksham is researching NVDA on stocks profile.\n"
            "**State:** Just asked about volume; assistant in middle of "
            "options analysis.\n"
            "**Next move:** Continue with the options chain breakdown."
        ),
    )
    adapter = ProviderAdapter(provider=provider, model_id="fake")

    home_resolver = _profile_home_resolver("stocks", tmp_path)

    # Simulate 3 sequential turns — but the FakeProvider serves the
    # classifier indirectly: we pass the real classifier signal via
    # manipulating last_user_messages. But the classifier is local and
    # won't reliably output "trading" without the right keyword set.
    # Use the keyword-heavy stocks messages — the classifier in this
    # codebase has _TRADING_APPS but not generic stock keywords; we
    # call the trigger 3 times with crafted classifications via a
    # custom-injected trigger that bypasses classify().
    # For this test, monkey-patch classify so it returns a stable result.
    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    def _stub_classify(ctx):  # noqa: ARG001
        return ClassificationResult(
            persona_id="trading", confidence=0.9,
            reason="stub: stocks content",
        )

    real_classify = orch.classify
    orch.classify = _stub_classify  # type: ignore[assignment]
    try:
        last_three = ("a", "b", "c")
        for _ in range(3):
            result = await run_auto_swap_pipeline(
                trigger=trigger,
                runtime=rt,
                session_id="sess-001",
                current_profile="default",
                available_profiles=("stocks", "coder"),
                last_user_messages=last_three,
                recent_messages=messages_about_stocks,
                plan_mode=False,
                auto_off=False,
                is_gateway_session=False,
                gateway_optin=False,
                target_profile_home_resolver=home_resolver,
                provider_adapter=adapter,
                audit_logger=None,  # audit not under test here
            )
    finally:
        orch.classify = real_classify  # type: ignore[assignment]

    assert result.queued is True
    assert result.handoff_path is not None
    assert result.handoff_path.exists()
    assert rt.custom["pending_profile_id"] == "stocks"
    # Notification surface populated
    assert "profile_swap_notification" in rt.custom
    note = rt.custom["profile_swap_notification"]
    assert note["from_profile"] == "default"
    assert note["to_profile"] == "stocks"
    assert note["trigger"] == "auto"
    # The handoff file is readable + parseable
    inbox = HandoffInbox(tmp_path / "stocks" / "home")
    docs = inbox.read_and_process_all()
    assert len(docs) == 1
    assert docs[0].metadata.source_profile == "default"
    assert docs[0].metadata.trigger == "auto"
    assert "Saksham" in docs[0].body


@pytest.mark.asyncio
async def test_pipeline_aborts_on_provider_failure(
    tmp_path: Path, fake_persona_resolver, messages_about_stocks,
) -> None:
    """Provider raises → swap aborted, no pending_profile_id queued."""
    trigger = AutoSwapTrigger(persona_to_profile=fake_persona_resolver)
    rt = FakeRuntime()
    provider = FakeProvider(response="", should_raise=True)
    adapter = ProviderAdapter(provider=provider, model_id="fake")

    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    orch.classify = lambda _ctx: ClassificationResult(  # type: ignore[assignment]
        persona_id="trading", confidence=0.95,
        reason="stub",
    )

    for _ in range(3):
        result = await run_auto_swap_pipeline(
            trigger=trigger, runtime=rt, session_id="sess-fail",
            current_profile="default", available_profiles=("stocks",),
            last_user_messages=("a", "b", "c"),
            recent_messages=messages_about_stocks,
            plan_mode=False, auto_off=False,
            is_gateway_session=False, gateway_optin=False,
            target_profile_home_resolver=_profile_home_resolver("stocks", tmp_path),
            provider_adapter=adapter, audit_logger=None,
        )

    assert result.queued is False
    assert result.error is not None
    assert "generation failed" in result.error
    assert "pending_profile_id" not in rt.custom


@pytest.mark.asyncio
async def test_pipeline_not_warranted_skips_handoff(
    tmp_path: Path, fake_persona_resolver, messages_about_stocks,
) -> None:
    """Model returns HANDOFF_NOT_WARRANTED → swap still queues but no handoff file."""
    trigger = AutoSwapTrigger(persona_to_profile=fake_persona_resolver)
    rt = FakeRuntime()
    provider = FakeProvider(
        response="HANDOFF_NOT_WARRANTED: trivial chat",
    )
    adapter = ProviderAdapter(provider=provider, model_id="fake")

    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    orch.classify = lambda _ctx: ClassificationResult(  # type: ignore[assignment]
        persona_id="trading", confidence=0.9, reason="stub",
    )

    for _ in range(3):
        result = await run_auto_swap_pipeline(
            trigger=trigger, runtime=rt, session_id="sess-nowarrant",
            current_profile="default", available_profiles=("stocks",),
            last_user_messages=("a", "b", "c"),
            recent_messages=messages_about_stocks,
            plan_mode=False, auto_off=False,
            is_gateway_session=False, gateway_optin=False,
            target_profile_home_resolver=_profile_home_resolver("stocks", tmp_path),
            provider_adapter=adapter, audit_logger=None,
        )

    # Swap queued but no handoff file written
    assert result.queued is True
    assert result.handoff_path is None
    assert rt.custom["pending_profile_id"] == "stocks"
    inbox = HandoffInbox(tmp_path / "stocks" / "home")
    assert inbox.list_pending() == []


@pytest.mark.asyncio
async def test_pipeline_gateway_disabled_by_default(
    tmp_path: Path, fake_persona_resolver, messages_about_stocks,
) -> None:
    """Gateway session without opt-in → swap blocked, audit deferred."""
    trigger = AutoSwapTrigger(persona_to_profile=fake_persona_resolver)
    rt = FakeRuntime()
    provider = FakeProvider(response="body")
    adapter = ProviderAdapter(provider=provider, model_id="fake")

    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    orch.classify = lambda _ctx: ClassificationResult(  # type: ignore[assignment]
        persona_id="trading", confidence=0.95, reason="stub",
    )

    for _ in range(5):
        result = await run_auto_swap_pipeline(
            trigger=trigger, runtime=rt, session_id="sess-gw",
            current_profile="default", available_profiles=("stocks",),
            last_user_messages=("a", "b", "c"),
            recent_messages=messages_about_stocks,
            plan_mode=False, auto_off=False,
            is_gateway_session=True, gateway_optin=False,  # gateway off
            target_profile_home_resolver=_profile_home_resolver("stocks", tmp_path),
            provider_adapter=adapter, audit_logger=None,
        )

    assert result.queued is False
    assert "pending_profile_id" not in rt.custom
    assert result.decision.reason == SwapDecisionReason.GATEWAY_DISABLED
    # Provider was NOT called — gate is upstream of LLM cost
    assert len(provider.complete_calls) == 0


@pytest.mark.asyncio
async def test_pipeline_plan_mode_disabled(
    tmp_path: Path, fake_persona_resolver, messages_about_stocks,
) -> None:
    trigger = AutoSwapTrigger(persona_to_profile=fake_persona_resolver)
    rt = FakeRuntime()
    provider = FakeProvider(response="body")
    adapter = ProviderAdapter(provider=provider, model_id="fake")

    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    orch.classify = lambda _ctx: ClassificationResult(  # type: ignore[assignment]
        persona_id="trading", confidence=0.95, reason="stub",
    )

    result = await run_auto_swap_pipeline(
        trigger=trigger, runtime=rt, session_id="sess-plan",
        current_profile="default", available_profiles=("stocks",),
        last_user_messages=("a", "b", "c"),
        recent_messages=messages_about_stocks,
        plan_mode=True,  # plan mode active
        auto_off=False,
        is_gateway_session=False, gateway_optin=False,
        target_profile_home_resolver=_profile_home_resolver("stocks", tmp_path),
        provider_adapter=adapter, audit_logger=None,
    )

    assert result.decision.reason == SwapDecisionReason.PLAN_MODE
    assert not result.queued


@pytest.mark.asyncio
async def test_pipeline_auto_off_blocks_even_with_high_confidence(
    tmp_path: Path, fake_persona_resolver, messages_about_stocks,
) -> None:
    trigger = AutoSwapTrigger(persona_to_profile=fake_persona_resolver)
    rt = FakeRuntime()
    provider = FakeProvider(response="body")
    adapter = ProviderAdapter(provider=provider, model_id="fake")

    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    orch.classify = lambda _ctx: ClassificationResult(  # type: ignore[assignment]
        persona_id="trading", confidence=0.99, reason="stub",
    )

    for _ in range(5):
        result = await run_auto_swap_pipeline(
            trigger=trigger, runtime=rt, session_id="sess-off",
            current_profile="default", available_profiles=("stocks",),
            last_user_messages=("a", "b", "c"),
            recent_messages=messages_about_stocks,
            plan_mode=False,
            auto_off=True,  # config killed
            is_gateway_session=False, gateway_optin=False,
            target_profile_home_resolver=_profile_home_resolver("stocks", tmp_path),
            provider_adapter=adapter, audit_logger=None,
        )

    assert not result.queued
    assert result.decision.reason == SwapDecisionReason.AUTO_OFF
    assert len(provider.complete_calls) == 0


@pytest.mark.asyncio
async def test_pipeline_audit_logger_called_on_fire(
    tmp_path: Path, fake_persona_resolver, messages_about_stocks,
) -> None:
    trigger = AutoSwapTrigger(persona_to_profile=fake_persona_resolver)
    rt = FakeRuntime()
    provider = FakeProvider(
        response=(
            "**Collaboration:** X\n**State:** Y\n**Next move:** Z"
        ),
    )
    adapter = ProviderAdapter(provider=provider, model_id="fake")

    calls: list[Any] = []

    class FakeAuditLogger:
        def append(self, evt):
            calls.append(evt)
            return 1

    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    orch.classify = lambda _ctx: ClassificationResult(  # type: ignore[assignment]
        persona_id="trading", confidence=0.9, reason="stub",
    )

    for _ in range(3):
        await run_auto_swap_pipeline(
            trigger=trigger, runtime=rt, session_id="sess-audit",
            current_profile="default", available_profiles=("stocks",),
            last_user_messages=("a", "b", "c"),
            recent_messages=messages_about_stocks,
            plan_mode=False, auto_off=False,
            is_gateway_session=False, gateway_optin=False,
            target_profile_home_resolver=_profile_home_resolver("stocks", tmp_path),
            provider_adapter=adapter,
            audit_logger=FakeAuditLogger(),  # type: ignore[arg-type]
        )

    # At least one call with outcome=allow
    allow_calls = [c for c in calls if c.outcome == "allow"]
    assert len(allow_calls) == 1
    assert allow_calls[0].trigger == "auto"
    assert allow_calls[0].source_profile == "default"
    assert allow_calls[0].target_profile == "stocks"
