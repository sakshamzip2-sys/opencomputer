"""IV.2 — turn-counting cadence throttle for plan/review mode injections.

Providers are cadence-aware: on turn 1 and every 5th turn afterward
(1, 6, 11, 16, ...), they return the FULL reminder. Other turns get a
SPARSE one-liner — saving ~500 tokens/turn after ~50 turns in long
sessions. Mirrors Kimi CLI's pattern
(sources/kimi-cli/src/kimi_cli/soul/dynamic_injections/plan_mode.py:27-29).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import RuntimeContext

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "extensions" / "coding-harness"


@pytest.fixture(autouse=True)
def _plugin_on_syspath():
    """Same shape the 6d/6f tests use — put coding-harness on sys.path and
    evict any cached sibling modules so each test re-imports fresh."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    for mod_name in list(sys.modules):
        if mod_name.split(".")[0] in {
            "context",
            "rewind",
            "state",
            "tools",
            "hooks",
            "modes",
            "plan_mode",
        }:
            sys.modules.pop(mod_name, None)
    yield
    if str(PLUGIN_ROOT) in sys.path:
        sys.path.remove(str(PLUGIN_ROOT))


# ─── plan_mode cadence ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_mode_full_on_turn_1() -> None:
    from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]

    provider = PlanModeInjectionProvider()
    runtime = RuntimeContext(plan_mode=True)
    ctx = InjectionContext(messages=(), runtime=runtime, turn_index=1)
    out = await provider.collect(ctx)
    assert out is not None
    # Full reminder keeps the existing template payload — > 100 chars easily.
    assert len(out) > 100, f"turn 1 should be FULL reminder, got {len(out)} chars"


@pytest.mark.asyncio
async def test_plan_mode_sparse_on_turn_2() -> None:
    from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]

    provider = PlanModeInjectionProvider()
    runtime = RuntimeContext(plan_mode=True)
    ctx = InjectionContext(messages=(), runtime=runtime, turn_index=2)
    out = await provider.collect(ctx)
    assert out is not None
    assert len(out) < 100, f"non-full turn should be SPARSE, got {len(out)} chars"


@pytest.mark.asyncio
async def test_plan_mode_full_on_turn_6() -> None:
    from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]

    provider = PlanModeInjectionProvider()
    runtime = RuntimeContext(plan_mode=True)
    ctx = InjectionContext(messages=(), runtime=runtime, turn_index=6)
    out = await provider.collect(ctx)
    assert out is not None
    assert len(out) > 100, "turn 6 should be FULL (1 + 5n cadence)"


@pytest.mark.asyncio
async def test_plan_mode_sparse_on_turn_5() -> None:
    from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]

    provider = PlanModeInjectionProvider()
    runtime = RuntimeContext(plan_mode=True)
    ctx = InjectionContext(messages=(), runtime=runtime, turn_index=5)
    out = await provider.collect(ctx)
    assert out is not None
    assert len(out) < 100, "turn 5 should be SPARSE (5 % 5 != 1)"


@pytest.mark.asyncio
async def test_plan_mode_full_on_turn_11() -> None:
    from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]

    provider = PlanModeInjectionProvider()
    runtime = RuntimeContext(plan_mode=True)
    ctx = InjectionContext(messages=(), runtime=runtime, turn_index=11)
    out = await provider.collect(ctx)
    assert out is not None
    assert len(out) > 100, "turn 11 should be FULL (11 % 5 == 1)"


@pytest.mark.asyncio
async def test_plan_mode_disabled_returns_none() -> None:
    from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]

    provider = PlanModeInjectionProvider()
    runtime = RuntimeContext(plan_mode=False)
    ctx = InjectionContext(messages=(), runtime=runtime, turn_index=1)
    out = await provider.collect(ctx)
    assert out is None


@pytest.mark.asyncio
async def test_plan_mode_turn_index_zero_still_full() -> None:
    """turn_index=0 is the neutral default — treat it as "first exposure" and
    still return the FULL reminder so callers that never thread the counter
    don't silently get the sparse version forever."""
    from modes.plan_mode import PlanModeInjectionProvider  # type: ignore[import-not-found]

    provider = PlanModeInjectionProvider()
    runtime = RuntimeContext(plan_mode=True)
    ctx = InjectionContext(messages=(), runtime=runtime, turn_index=0)
    out = await provider.collect(ctx)
    assert out is not None
    assert len(out) > 100, "turn_index=0 (unthreaded) should default to FULL"


# ─── review_mode cadence ────────────────────────────────────────


def _review_runtime() -> RuntimeContext:
    return RuntimeContext(custom={"review_mode": True})


@pytest.mark.asyncio
async def test_review_mode_full_on_turn_1() -> None:
    from modes.review_mode import ReviewModeInjectionProvider  # type: ignore[import-not-found]

    provider = ReviewModeInjectionProvider()
    ctx = InjectionContext(messages=(), runtime=_review_runtime(), turn_index=1)
    out = await provider.collect(ctx)
    assert out is not None
    assert len(out) > 100, f"turn 1 should be FULL reminder, got {len(out)} chars"


@pytest.mark.asyncio
async def test_review_mode_sparse_on_turn_2() -> None:
    from modes.review_mode import ReviewModeInjectionProvider  # type: ignore[import-not-found]

    provider = ReviewModeInjectionProvider()
    ctx = InjectionContext(messages=(), runtime=_review_runtime(), turn_index=2)
    out = await provider.collect(ctx)
    assert out is not None
    assert len(out) < 100, f"non-full turn should be SPARSE, got {len(out)} chars"


@pytest.mark.asyncio
async def test_review_mode_full_on_turn_6() -> None:
    from modes.review_mode import ReviewModeInjectionProvider  # type: ignore[import-not-found]

    provider = ReviewModeInjectionProvider()
    ctx = InjectionContext(messages=(), runtime=_review_runtime(), turn_index=6)
    out = await provider.collect(ctx)
    assert out is not None
    assert len(out) > 100, "turn 6 should be FULL (1 + 5n cadence)"


@pytest.mark.asyncio
async def test_review_mode_disabled_returns_none() -> None:
    from modes.review_mode import ReviewModeInjectionProvider  # type: ignore[import-not-found]

    provider = ReviewModeInjectionProvider()
    runtime = RuntimeContext()  # no review_mode flag in custom
    ctx = InjectionContext(messages=(), runtime=runtime, turn_index=1)
    out = await provider.collect(ctx)
    assert out is None


# ─── AgentLoop threads turn_index into both call sites ──────────


@pytest.mark.asyncio
async def test_agent_loop_threads_turn_index_to_providers(tmp_path) -> None:
    """AgentLoop must pass a positive turn_index to providers, and both call
    sites (turn-start + post-compaction re-collect) must agree."""
    # Import here so the sys.path fixture doesn't tangle with opencomputer/.
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.core import Message
    from plugin_sdk.injection import DynamicInjectionProvider
    from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage

    # Recording injection provider — captures every InjectionContext it sees.
    observed: list[InjectionContext] = []

    class _RecordingProvider(DynamicInjectionProvider):
        priority = 999

        @property
        def provider_id(self) -> str:
            return "test:recording-turn-index"

        async def collect(self, ctx: InjectionContext) -> str | None:
            observed.append(ctx)
            return None

    # Stub provider that returns END_TURN immediately so the loop exits after
    # one LLM call (no tool use, no compaction needed for turn 1).
    class _StubProvider(BaseProvider):
        async def complete(self, **kwargs):
            return ProviderResponse(
                message=Message(role="assistant", content="hi"),
                stop_reason="end_turn",
                usage=Usage(input_tokens=10, output_tokens=2),
            )

        async def stream_complete(self, **kwargs):
            raise NotImplementedError

    from opencomputer.agent.injection import engine as global_engine

    global_engine.register(_RecordingProvider())
    try:
        cfg = Config(
            model=ModelConfig(model="stub", max_tokens=1, temperature=0.0),
            loop=LoopConfig(max_iterations=3, parallel_tools=False),
            session=SessionConfig(db_path=tmp_path / "db.sqlite"),
            memory=MemoryConfig(
                declarative_path=tmp_path / "MEM.md",
                skills_path=tmp_path / "skills",
                user_path=tmp_path / "USER.md",
                soul_path=tmp_path / "SOUL.md",
            ),
        )
        loop = AgentLoop(
            provider=_StubProvider(),
            config=cfg,
            compaction_disabled=True,
            episodic_disabled=True,
            reviewer_disabled=True,
        )
        # First turn on a fresh session ⇒ turn_index should be 1.
        result = await loop.run_conversation("first user message")
        assert observed, "injection provider should have been invoked"
        assert observed[0].turn_index == 1, (
            f"first turn should have turn_index=1, got {observed[0].turn_index}"
        )
        # Second turn on the same session ⇒ turn_index should be 2.
        observed.clear()
        await loop.run_conversation(
            "second user message", session_id=result.session_id
        )
        assert observed, "injection provider should have been invoked on turn 2"
        assert observed[0].turn_index == 2, (
            f"second turn should have turn_index=2, got {observed[0].turn_index}"
        )
    finally:
        global_engine.unregister("test:recording-turn-index")
