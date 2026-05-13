"""Real-AgentLoop E2E test for the handoff auto-swap pipeline.

Constructs an actual :class:`AgentLoop` (no mocks of the loop itself) with
a stub provider + tmp-path SessionDB + real MemoryManager, and drives the
loop's per-turn handoff hook directly. Validates:

  * ``_run_handoff_auto_swap`` is reachable from a constructed loop
  * The cached :class:`AutoSwapTrigger` is built lazily
  * Audit logger rebinds when the active profile changes mid-session
  * Successful FIRE queues ``pending_profile_id`` on the runtime
  * Handoff file appears at the target profile's inbox path

The classifier itself is monkey-patched at the orchestrator boundary
(``orchestrator.classify``) since the real Bayesian classifier's
verdict on synthetic test data is too sensitive to its full input
signal (foreground app, file paths, etc.) for stable assertions. The
pipeline + loop integration is what's under test, not classifier
accuracy.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencomputer.agent.config import (
    Config,
    LoopConfig,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
)
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    Usage,
)
from plugin_sdk.runtime_context import RuntimeContext


class _HandoffProvider(BaseProvider):
    """Stub provider — returns a handoff body that passes Step 0."""

    name = "stub-handoff-provider"
    default_model = "stub-handoff"

    def __init__(self) -> None:
        self.complete_calls = 0
        self._next_response: str = (
            "**Collaboration:** Saksham is researching stocks.\n"
            "**State:** Mid-thread on NVDA earnings.\n"
            "**Next:** Continue the earnings analysis."
        )

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        self.complete_calls += 1
        # The handoff generator calls .complete() — return a content
        # string that parses as a YES handoff.
        return ProviderResponse(
            message=Message(role="assistant", content=self._next_response),
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=20),
        )

    async def stream_complete(self, **kwargs: Any):
        if False:
            yield

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


def _make_loop(tmp_path: Path) -> AgentLoop:
    """Construct a real AgentLoop with tmp-path-rooted state."""
    db_path = tmp_path / "rt.db"
    db = SessionDB(db_path)
    cfg = Config(
        model=ModelConfig(model="stub-handoff", max_tokens=4096),
        loop=LoopConfig(),
        session=SessionConfig(db_path=db_path),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            soul_path=tmp_path / "SOUL.md",
            skills_path=tmp_path / "skills",
        ),
    )
    provider = _HandoffProvider()
    loop = AgentLoop(config=cfg, provider=provider, db=db)
    return loop


@pytest.mark.asyncio
async def test_real_loop_run_handoff_auto_swap_method_reachable(
    tmp_path: Path,
) -> None:
    """Sanity: the loop method exists and runs without raising on a
    fresh loop with an empty message list."""
    loop = _make_loop(tmp_path)
    loop._runtime = RuntimeContext(custom={"active_profile_id": "default"})
    # Run with empty messages — must not raise; will short-circuit
    # because no user-message signal feeds the classifier.
    await loop._run_handoff_auto_swap(sid="sess-A", messages=[])


@pytest.mark.asyncio
async def test_real_loop_trigger_lazily_constructed(tmp_path: Path) -> None:
    """First call constructs the trigger + adapter + caches on self."""
    loop = _make_loop(tmp_path)
    loop._runtime = RuntimeContext(custom={"active_profile_id": "default"})
    assert not hasattr(loop, "_handoff_trigger")
    await loop._run_handoff_auto_swap(sid="sess-B", messages=[])
    assert hasattr(loop, "_handoff_trigger")
    # Second call reuses the same instance
    first_trigger = loop._handoff_trigger
    await loop._run_handoff_auto_swap(sid="sess-B", messages=[])
    assert loop._handoff_trigger is first_trigger


@pytest.mark.asyncio
async def test_real_loop_fires_swap_after_three_sustained_turns(
    tmp_path: Path, monkeypatch,
) -> None:
    """Three sustained-high-confidence turns → pending_profile_id queued."""
    loop = _make_loop(tmp_path)
    loop._runtime = RuntimeContext(custom={"active_profile_id": "default"})

    # Stub the orchestrator's classify call so the verdict is stable
    # across all three turns.
    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    monkeypatch.setattr(
        orch, "classify",
        lambda _ctx: ClassificationResult(
            persona_id="trading", confidence=0.9, reason="stub",
        ),
    )

    # Stub profile listing so the trigger sees a matching target.
    import opencomputer.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "list_profiles", lambda: ["stocks"])
    monkeypatch.setattr(
        profiles_mod, "get_profile_dir",
        lambda name: tmp_path / "profiles" / (name or "default"),
    )
    monkeypatch.setattr(
        profiles_mod, "read_active_profile", lambda: "default",
    )

    # Drive three turns
    fake_messages: list[Any] = [
        Message(role="user", content="what's NVDA at?"),
        Message(role="assistant", content="up 2%"),
    ]
    for _ in range(3):
        await loop._run_handoff_auto_swap(sid="sess-fire", messages=fake_messages)

    # Pending swap is queued
    assert loop._runtime.custom.get("pending_profile_id") == "stocks"
    # Provider was called exactly once (only on the FIRED turn)
    assert loop.provider.complete_calls == 1
    # Handoff file is in the stocks profile's inbox
    stocks_inbox = tmp_path / "profiles" / "stocks" / "home" / "inbox"
    assert stocks_inbox.exists()
    handoffs = [
        p for p in stocks_inbox.iterdir() if p.name.startswith("handoff_")
    ]
    assert len(handoffs) == 1
    # Notification dict surfaced for any UI surface to read
    note = loop._runtime.custom.get("profile_swap_notification")
    assert note is not None
    assert note["from_profile"] == "default"
    assert note["to_profile"] == "stocks"
    assert note["trigger"] == "auto"


@pytest.mark.asyncio
async def test_real_loop_audit_logger_rebinds_on_profile_change(
    tmp_path: Path, monkeypatch,
) -> None:
    """When active_profile_id changes between turns, the audit logger
    re-binds to the new profile's audit DB."""
    loop = _make_loop(tmp_path)
    loop._runtime = RuntimeContext(custom={"active_profile_id": "default"})

    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    monkeypatch.setattr(
        orch, "classify",
        lambda _ctx: ClassificationResult("default", 0.5, "x"),
    )
    import opencomputer.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "list_profiles", lambda: [])
    monkeypatch.setattr(
        profiles_mod, "get_profile_dir",
        lambda name: tmp_path / "profiles" / (name or "default"),
    )
    monkeypatch.setattr(
        profiles_mod, "read_active_profile", lambda: "default",
    )

    # Turn 1: in "default" profile
    await loop._run_handoff_auto_swap(sid="rt", messages=[])
    profile_at_t1 = loop._handoff_audit_logger_profile
    logger_at_t1 = loop._handoff_audit_logger

    # Mid-session, runtime swaps to "stocks"
    loop._runtime.custom["active_profile_id"] = "stocks"

    # Turn 2: same session, but now active is "stocks"
    await loop._run_handoff_auto_swap(sid="rt", messages=[])
    profile_at_t2 = loop._handoff_audit_logger_profile
    logger_at_t2 = loop._handoff_audit_logger

    assert profile_at_t1 == "default"
    assert profile_at_t2 == "stocks"
    # The logger object was replaced (not the same instance)
    assert logger_at_t1 is not logger_at_t2 or logger_at_t1 is None


@pytest.mark.asyncio
async def test_real_loop_handoff_disabled_when_config_off(
    tmp_path: Path, monkeypatch,
) -> None:
    """`config.auto_swap_handoff = "off"` → trigger never fires no matter
    how confident the classifier is."""
    db_path = tmp_path / "rt.db"
    db = SessionDB(db_path)
    cfg = Config(
        model=ModelConfig(model="stub-handoff", max_tokens=4096),
        loop=LoopConfig(),
        session=SessionConfig(db_path=db_path),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            soul_path=tmp_path / "SOUL.md",
            skills_path=tmp_path / "skills",
        ),
        auto_swap_handoff="off",
    )
    loop = AgentLoop(config=cfg, provider=_HandoffProvider(), db=db)
    loop._runtime = RuntimeContext(custom={"active_profile_id": "default"})

    from opencomputer.agent.handoff import orchestrator as orch
    from opencomputer.awareness.personas.classifier import ClassificationResult

    monkeypatch.setattr(
        orch, "classify",
        lambda _ctx: ClassificationResult("trading", 0.99, "stub"),
    )
    import opencomputer.profiles as profiles_mod
    monkeypatch.setattr(profiles_mod, "list_profiles", lambda: ["stocks"])
    monkeypatch.setattr(
        profiles_mod, "get_profile_dir",
        lambda name: tmp_path / "profiles" / (name or "default"),
    )
    monkeypatch.setattr(
        profiles_mod, "read_active_profile", lambda: "default",
    )

    for _ in range(5):
        await loop._run_handoff_auto_swap(sid="off", messages=[])
    # No swap was queued
    assert "pending_profile_id" not in loop._runtime.custom
    # Provider not called — gate was upstream of LLM cost
    assert loop.provider.complete_calls == 0
