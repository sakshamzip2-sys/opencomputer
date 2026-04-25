"""Phase 6a tests: InjectionEngine, CompactionEngine, RuntimeContext threading, SDK linter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin_sdk.core import Message, ToolCall
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext

# ─── InjectionEngine ────────────────────────────────────────────


class _PlanProvider(DynamicInjectionProvider):
    priority = 10

    @property
    def provider_id(self) -> str:
        return "plan"

    async def collect(self, ctx: InjectionContext) -> str | None:
        return "PLAN MODE" if ctx.runtime.plan_mode else None


class _ClockProvider(DynamicInjectionProvider):
    priority = 90

    @property
    def provider_id(self) -> str:
        return "clock"

    async def collect(self, ctx: InjectionContext) -> str | None:
        return "current time"


async def test_injection_engine_registers_and_collects() -> None:
    from opencomputer.agent.injection import InjectionEngine

    eng = InjectionEngine()
    eng.register(_PlanProvider())
    eng.register(_ClockProvider())

    ctx_plan = InjectionContext(
        messages=(),
        runtime=RuntimeContext(plan_mode=True),
    )
    out = await eng.collect_all(ctx_plan)
    # Priority ordering: plan (10) before clock (90)
    assert out == ["PLAN MODE", "current time"]

    ctx_no_plan = InjectionContext(messages=(), runtime=DEFAULT_RUNTIME_CONTEXT)
    out = await eng.collect_all(ctx_no_plan)
    # plan returns None when flag false — only clock appears
    assert out == ["current time"]


def test_injection_engine_dedup_provider_id() -> None:
    from opencomputer.agent.injection import InjectionEngine

    eng = InjectionEngine()
    eng.register(_PlanProvider())
    with pytest.raises(ValueError, match="already registered"):
        eng.register(_PlanProvider())


async def test_injection_engine_ordering_is_deterministic() -> None:
    """Same inputs → same output (cache stability)."""
    from opencomputer.agent.injection import InjectionEngine

    eng = InjectionEngine()
    eng.register(_ClockProvider())  # priority 90
    eng.register(_PlanProvider())  # priority 10
    ctx = InjectionContext(messages=(), runtime=RuntimeContext(plan_mode=True))
    out1 = await eng.collect_all(ctx)
    out2 = await eng.collect_all(ctx)
    assert out1 == out2
    # Priority asc means plan (10) comes first
    assert out1[0] == "PLAN MODE"


async def test_injection_engine_provider_exception_is_swallowed() -> None:
    """A broken provider must not break other providers or the loop."""
    from opencomputer.agent.injection import InjectionEngine

    class Broken(DynamicInjectionProvider):
        priority = 5

        @property
        def provider_id(self) -> str:
            return "broken"

        async def collect(self, ctx: InjectionContext) -> str | None:
            raise RuntimeError("plugin broken")

    eng = InjectionEngine()
    eng.register(Broken())
    eng.register(_ClockProvider())
    ctx = InjectionContext(messages=(), runtime=DEFAULT_RUNTIME_CONTEXT)
    out = await eng.collect_all(ctx)
    assert out == ["current time"]  # broken skipped, clock intact


# ─── CompactionEngine ───────────────────────────────────────────


def _mk_msgs(n: int) -> list[Message]:
    return [
        Message(role="user" if i % 2 == 0 else "assistant", content=f"msg {i}")
        for i in range(n)
    ]


def test_compaction_does_not_fire_below_threshold() -> None:
    from opencomputer.agent.compaction import CompactionEngine

    eng = CompactionEngine(provider=MagicMock(), model="claude-opus-4-7")
    assert eng.should_compact(last_input_tokens=100) is False
    # 80% of 200k = 160k
    assert eng.should_compact(last_input_tokens=159_000) is False
    assert eng.should_compact(last_input_tokens=170_000) is True


def test_compaction_disabled_never_fires() -> None:
    from opencomputer.agent.compaction import CompactionEngine

    eng = CompactionEngine(provider=MagicMock(), model="claude-opus-4-7", disabled=True)
    assert eng.should_compact(last_input_tokens=999_999) is False


def test_compaction_preserves_tool_use_tool_result_pair() -> None:
    """Split index must NEVER put a tool_result first in the recent_block
    (which would orphan it from its tool_use in the compacted block)."""
    from opencomputer.agent.compaction import CompactionEngine

    # Case 1: preserve_recent targets a tool_result message → engine must move back.
    msgs = [
        Message(role="user", content="do X"),
        Message(
            role="assistant",
            content="ok",
            tool_calls=[ToolCall(id="t1", name="Read", arguments={"file_path": "/x"})],
        ),
        Message(role="tool", content="result", tool_call_id="t1"),
        Message(role="assistant", content="done"),
        Message(role="user", content="next"),
    ]
    eng = CompactionEngine(provider=MagicMock(), model="claude-opus-4-7")

    # preserve_recent=3 → naive target=2 (which is tool_result — orphan!)
    # Engine must step back to a safe boundary.
    idx = eng._safe_split_index(msgs, preserve_recent=3)
    # The message right AFTER the split (first of recent_block) must NOT be a tool result
    if idx < len(msgs):
        assert msgs[idx].role != "tool", (
            f"split at {idx} would orphan a tool_result into recent_block"
        )


def test_compaction_fallback_on_aux_failure() -> None:
    """If the summary LLM call raises, fall back to truncate-and-drop."""
    from opencomputer.agent.compaction import CompactionEngine

    bad_provider = MagicMock()
    bad_provider.complete = AsyncMock(side_effect=RuntimeError("aux down"))
    eng = CompactionEngine(provider=bad_provider, model="claude-opus-4-7")
    msgs = _mk_msgs(50)
    # Force should_compact to True by stubbing the threshold check
    result = asyncio.run(eng.maybe_run(msgs, last_input_tokens=9_999_999))
    assert result.did_compact
    assert result.degraded
    assert result.reason == "aux-failed-truncated"
    # Synthetic header + some preserved tail
    assert any("[compacted-truncated]" in m.content for m in result.messages)


def test_compaction_in_progress_flag() -> None:
    """The in_progress flag must be off outside of an active summarization."""
    from opencomputer.agent.compaction import CompactionEngine

    eng = CompactionEngine(provider=MagicMock(), model="claude-opus-4-7")
    assert eng.in_progress is False


# ─── RuntimeContext threading ──────────────────────────────────


def test_runtime_context_defaults_are_all_false() -> None:
    ctx = DEFAULT_RUNTIME_CONTEXT
    assert ctx.plan_mode is False
    assert ctx.yolo_mode is False
    assert ctx.custom == {}


def test_runtime_context_is_frozen() -> None:
    ctx = RuntimeContext(plan_mode=True)
    with pytest.raises(Exception):  # FrozenInstanceError
        ctx.plan_mode = False  # type: ignore[misc]


def test_runtime_context_reaches_hooks_via_hookcontext() -> None:
    """HookContext now accepts runtime — make sure the field exists."""
    from plugin_sdk.hooks import HookContext, HookEvent

    ctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        runtime=RuntimeContext(plan_mode=True),
    )
    assert ctx.runtime is not None
    assert ctx.runtime.plan_mode is True


def test_runtime_context_backward_compat_default_none() -> None:
    """Pre-6a hooks that don't pass runtime still construct — default None."""
    from plugin_sdk.hooks import HookContext, HookEvent

    ctx = HookContext(event=HookEvent.STOP, session_id="s")
    assert ctx.runtime is None


# ─── SDK import linter ─────────────────────────────────────────


def test_plugin_sdk_does_not_import_opencomputer() -> None:
    """plugin_sdk/* must never depend on opencomputer/*. Enforced on every run."""
    sdk_dir = Path(__file__).resolve().parent.parent / "plugin_sdk"
    offenders: list[str] = []
    for py in sdk_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            # Flag any line that imports from opencomputer
            if (
                stripped.startswith("from opencomputer")
                or stripped.startswith("import opencomputer")
            ):
                offenders.append(f"{py.name}: {stripped}")
    assert not offenders, (
        "SDK BOUNDARY VIOLATION — plugin_sdk imports from opencomputer:\n"
        + "\n".join(offenders)
    )


# ─── PluginAPI.register_injection_provider ─────────────────────


def test_plugin_api_can_register_injection_provider() -> None:
    from opencomputer.agent.injection import InjectionEngine
    from opencomputer.plugins.loader import PluginAPI

    inj_eng = InjectionEngine()
    api = PluginAPI(
        tool_registry=MagicMock(),
        hook_engine=MagicMock(),
        provider_registry={},
        channel_registry={},
        injection_engine=inj_eng,
    )
    api.register_injection_provider(_PlanProvider())
    assert len(inj_eng.providers()) == 1


def test_plugin_api_without_injection_engine_errors() -> None:
    """Missing engine → clear SDK-version-mismatch error."""
    from opencomputer.plugins.loader import PluginAPI

    api = PluginAPI(
        tool_registry=MagicMock(),
        hook_engine=MagicMock(),
        provider_registry={},
        channel_registry={},
        # no injection_engine provided
    )
    with pytest.raises(RuntimeError, match="Injection engine unavailable"):
        api.register_injection_provider(_PlanProvider())


# ─── Delegate runtime propagation ──────────────────────────────


def test_delegate_inherits_parent_runtime() -> None:
    """DelegateTool spawns a subagent with the parent's runtime context."""
    import asyncio

    from opencomputer.tools.delegate import DelegateTool

    captured: dict = {}

    class _FakeLoop:
        async def run_conversation(self, user_message, runtime=None, **kw):
            captured["runtime"] = runtime

            class _R:
                class final_message:
                    content = "ok"

            return _R()

    DelegateTool.set_factory(lambda: _FakeLoop())
    parent_runtime = RuntimeContext(plan_mode=True, custom={"token": "abc"})
    DelegateTool.set_runtime(parent_runtime)

    tool = DelegateTool()
    result = asyncio.run(
        tool.execute(ToolCall(id="1", name="delegate", arguments={"task": "go"}))
    )
    assert not result.is_error
    # PR-4: child_runtime is a new object (delegation_depth is incremented),
    # so identity check is intentionally relaxed — verify field propagation instead.
    assert captured["runtime"].plan_mode is True
    assert captured["runtime"].custom == {"token": "abc"}
    assert captured["runtime"].delegation_depth == 1
