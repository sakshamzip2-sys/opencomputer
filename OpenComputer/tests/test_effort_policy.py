"""Tests for effort_policy.recommended_effort — pure function, no I/O."""

from __future__ import annotations

import dataclasses

import pytest

from opencomputer.agent.effort_policy import recommended_effort
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext


def _runtime(**overrides) -> RuntimeContext:
    """Build a RuntimeContext from defaults with field overrides."""
    return dataclasses.replace(DEFAULT_RUNTIME_CONTEXT, **overrides)


# ─── Subagent override (depth > 0) ────────────────────────────────


def test_subagent_gets_low_regardless_of_model() -> None:
    """Doc 5: subagents are the canonical low-effort use case."""
    rt = _runtime(delegation_depth=1)
    assert recommended_effort(runtime=rt, model="claude-opus-4-7") == "low"
    assert recommended_effort(runtime=rt, model="claude-sonnet-4-6") == "low"
    assert recommended_effort(runtime=rt, model="o1") == "low"
    assert recommended_effort(runtime=rt, model="kimi-k2") == "low"


def test_grandchild_subagent_also_gets_low() -> None:
    """delegation_depth > 0 catches all subagent depths, not just depth=1."""
    rt = _runtime(delegation_depth=3)
    assert recommended_effort(runtime=rt, model="claude-opus-4-7") == "low"


# ─── Voice mode ───────────────────────────────────────────────────


def test_voice_mode_gets_low() -> None:
    """Realtime voice can't afford thinking budget on the critical path."""
    rt = _runtime(custom={"voice_mode": True})
    assert recommended_effort(runtime=rt, model="claude-opus-4-7") == "low"


def test_voice_mode_false_does_not_trigger_low() -> None:
    """Only literal True triggers — not 'true' string, not present-but-falsy."""
    rt = _runtime(custom={"voice_mode": False})
    assert recommended_effort(runtime=rt, model="claude-opus-4-7") == "xhigh"


# ─── Per-model defaults ───────────────────────────────────────────


@pytest.mark.parametrize("model,expected", [
    # Opus 4.7 → xhigh (Doc 5 explicit recommendation)
    ("claude-opus-4-7", "xhigh"),
    ("claude-opus-4-7-20260301", "xhigh"),
    # Sonnet 4.6 → medium (Doc 5 latency warning)
    ("claude-sonnet-4-6", "medium"),
    ("claude-sonnet-4-6-20251101", "medium"),
    ("claude-sonnet-4-5", "medium"),
    # OpenAI reasoning → medium (sensible paid-tier default)
    ("o1", "medium"),
    ("o1-preview", "medium"),
    ("o3-mini", "medium"),
    ("o3", "medium"),
    ("o4-mini", "medium"),
    ("gpt-5-thinking", "medium"),
    # No recommendation for non-reasoning models — provider default applies
    ("claude-haiku-4-5", None),
    ("claude-opus-4-5", None),  # legacy Anthropic, no policy override yet
    ("gpt-4o", None),
    ("gpt-4", None),
    ("kimi-k2", None),
    ("llama-3-70b", None),
    ("deepseek-chat", None),
])
def test_per_model_defaults(model: str, expected: str | None) -> None:
    rt = _runtime()  # no subagent, no voice
    assert recommended_effort(runtime=rt, model=model) == expected


# ─── Edge cases ──────────────────────────────────────────────────


def test_runtime_none_uses_model_default() -> None:
    """When no runtime is provided, fall back to per-model defaults."""
    assert recommended_effort(runtime=None, model="claude-opus-4-7") == "xhigh"
    assert recommended_effort(runtime=None, model="gpt-4o") is None


def test_subagent_override_beats_model_default() -> None:
    """A coding subagent on Opus 4.7 still gets low — narrow scope wins."""
    rt = _runtime(delegation_depth=1)
    assert recommended_effort(runtime=rt, model="claude-opus-4-7") == "low"
    # Without subagent depth, Opus 4.7 → xhigh
    rt_main = _runtime()
    assert recommended_effort(runtime=rt_main, model="claude-opus-4-7") == "xhigh"


# ─── Loop integration: policy applied when reasoning_effort unset ──


@pytest.mark.asyncio
async def test_loop_applies_policy_default_when_reasoning_unset(tmp_path) -> None:
    """End-to-end: AgentLoop calls policy and the result lands in runtime_extras."""
    from pathlib import Path
    from typing import Any

    from opencomputer.agent.config import (
        Config,
        ModelConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import (
        BaseProvider,
        ProviderResponse,
        StreamEvent,
        Usage,
    )

    captured_extras: dict[str, Any] = {}

    class _CaptureProvider(BaseProvider):
        name = "capture"
        default_model = "claude-opus-4-7"

        async def complete(self, **kwargs: Any) -> ProviderResponse:
            captured_extras["runtime_extras"] = kwargs.get("runtime_extras")
            return ProviderResponse(
                message=Message(role="assistant", content="ok"),
                stop_reason="end_turn",
                usage=Usage(input_tokens=1, output_tokens=1),
            )

        async def stream_complete(self, **kwargs: Any):
            resp = await self.complete(**kwargs)
            yield StreamEvent(kind="done", response=resp)

    loop = AgentLoop(
        provider=_CaptureProvider(),
        config=Config(
            model=ModelConfig(provider="capture", model="claude-opus-4-7"),
            session=SessionConfig(db_path=Path(tmp_path) / "s.db"),
        ),
    )

    await loop.run_conversation("Hi", session_id="t-policy")

    # Opus 4.7 model default → xhigh
    assert captured_extras["runtime_extras"]["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_loop_user_set_reasoning_wins_over_policy(tmp_path) -> None:
    """User-set /reasoning value must NOT be overwritten by the policy."""
    from pathlib import Path
    from typing import Any

    from opencomputer.agent.config import (
        Config,
        ModelConfig,
        SessionConfig,
    )
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import (
        BaseProvider,
        ProviderResponse,
        StreamEvent,
        Usage,
    )
    from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT

    captured_extras: dict[str, Any] = {}

    class _CaptureProvider(BaseProvider):
        name = "capture"
        default_model = "claude-opus-4-7"

        async def complete(self, **kwargs: Any) -> ProviderResponse:
            captured_extras["runtime_extras"] = kwargs.get("runtime_extras")
            return ProviderResponse(
                message=Message(role="assistant", content="ok"),
                stop_reason="end_turn",
                usage=Usage(input_tokens=1, output_tokens=1),
            )

        async def stream_complete(self, **kwargs: Any):
            resp = await self.complete(**kwargs)
            yield StreamEvent(kind="done", response=resp)

    # User explicitly set low via /reasoning low.
    user_runtime = dataclasses.replace(
        DEFAULT_RUNTIME_CONTEXT,
        custom={"reasoning_effort": "low"},
    )

    loop = AgentLoop(
        provider=_CaptureProvider(),
        config=Config(
            model=ModelConfig(provider="capture", model="claude-opus-4-7"),
            session=SessionConfig(db_path=Path(tmp_path) / "s.db"),
        ),
    )

    await loop.run_conversation(
        "Hi", session_id="t-user-wins", runtime=user_runtime,
    )

    # User's "low" wins — policy would have set xhigh.
    assert captured_extras["runtime_extras"]["reasoning_effort"] == "low"
