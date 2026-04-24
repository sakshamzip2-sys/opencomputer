"""Phase 12b.5 — Sub-project E, Task E3.

Tests for wiring ``PluginDemandTracker`` into ``ToolRegistry.dispatch``.

When the LLM calls a tool name the active registry can't dispatch, the
demand tracker should record a signal for each installed-but-disabled
candidate plugin that would provide the tool. The caller contract of
``dispatch`` is unchanged — we still return ``ToolResult(is_error=True,
...)`` so the rest of the loop behaves exactly as before.

Positional-only callers of ``dispatch`` must keep working (kwargs are
optional). The ``AgentLoop.__init__`` now instantiates a tracker that
the loop threads through each dispatch call.
"""

from __future__ import annotations

from collections.abc import Callable
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
from opencomputer.plugins.demand_tracker import PluginDemandTracker
from opencomputer.plugins.discovery import PluginCandidate
from opencomputer.tools.registry import ToolRegistry
from plugin_sdk.core import PluginManifest, ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# ─── helpers ───────────────────────────────────────────────────────────


class _StubTool(BaseTool):
    """Minimal BaseTool that records the call and returns a plain result."""

    parallel_safe = True

    def __init__(self, name: str = "Known") -> None:
        self._name = name

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self._name,
            description="stub",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content="ok")


def _mk_candidate(
    plugin_id: str,
    tool_names: tuple[str, ...],
) -> PluginCandidate:
    manifest = PluginManifest(
        id=plugin_id,
        name=plugin_id,
        version="0.1.0",
        entry="plugin",
        kind="tool",
        tool_names=tool_names,
    )
    root = Path(f"/tmp/fake-{plugin_id}")
    return PluginCandidate(
        manifest=manifest,
        root_dir=root,
        manifest_path=root / "plugin.json",
    )


def _fake_discover(
    candidates: list[PluginCandidate],
) -> Callable[[], list[PluginCandidate]]:
    def _call() -> list[PluginCandidate]:
        return list(candidates)

    return _call


def _mk_tracker(
    tmp_path: Path,
    candidates: list[PluginCandidate],
    *,
    active: frozenset[str] | None = None,
) -> PluginDemandTracker:
    return PluginDemandTracker(
        db_path=tmp_path / "sessions.db",
        discover_fn=_fake_discover(candidates),
        active_profile_plugins=active,
    )


def _config(tmp: Path) -> Config:
    return Config(
        model=ModelConfig(provider="mock", model="mock-model", max_tokens=1024, temperature=0.0),
        loop=LoopConfig(max_iterations=3, parallel_tools=False),
        session=SessionConfig(db_path=tmp / "sessions.db"),
        memory=MemoryConfig(
            declarative_path=tmp / "MEMORY.md",
            skills_path=tmp / "skills",
        ),
    )


# ─── 1. tool-found path: no signals recorded ──────────────────────────


@pytest.mark.asyncio
async def test_dispatch_tool_found_does_not_touch_tracker(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_StubTool("Known"))
    tracker = _mk_tracker(
        tmp_path,
        [_mk_candidate("demo", ("Known",))],
    )

    call = ToolCall(id="tc1", name="Known", arguments={})
    result = await registry.dispatch(
        call,
        session_id="s1",
        turn_index=0,
        demand_tracker=tracker,
    )
    assert result.is_error is False
    assert result.content == "ok"
    # No signal rows: the tool was dispatched, not missed.
    assert tracker.signals_by_plugin(session_id="s1") == {}


# ─── 2. tool-not-found path: signal recorded ──────────────────────────


@pytest.mark.asyncio
async def test_dispatch_tool_not_found_records_to_tracker(tmp_path: Path) -> None:
    registry = ToolRegistry()  # no tools registered
    tracker = _mk_tracker(
        tmp_path,
        [_mk_candidate("demo-editor", ("Edit",))],
    )

    call = ToolCall(id="tc1", name="Edit", arguments={"file_path": "/tmp/x"})
    result = await registry.dispatch(
        call,
        session_id="s1",
        turn_index=0,
        demand_tracker=tracker,
    )
    assert result.is_error is True
    assert "not found" in (result.content or "").lower()
    # Tracker recorded one signal for the candidate plugin.
    assert tracker.recommended_plugins(threshold=1) == [("demo-editor", 1)]


# ─── 3. no-candidate tool: still a silent no-op ───────────────────────


@pytest.mark.asyncio
async def test_dispatch_tool_not_found_noop_when_no_candidate(tmp_path: Path) -> None:
    registry = ToolRegistry()
    # Candidate exists but doesn't declare any tool names.
    tracker = _mk_tracker(
        tmp_path,
        [_mk_candidate("demo-editor", ())],
    )

    call = ToolCall(id="tc1", name="Edit", arguments={})
    result = await registry.dispatch(
        call,
        session_id="s1",
        turn_index=0,
        demand_tracker=tracker,
    )
    assert result.is_error is True
    # No candidate matched → zero rows inserted.
    assert tracker.signals_by_plugin(session_id="s1") == {}
    assert tracker.recommended_plugins(threshold=1) == []


# ─── 4. positional-only dispatch keeps working ────────────────────────


@pytest.mark.asyncio
async def test_dispatch_without_tracker_kwarg_still_works(tmp_path: Path) -> None:
    """Regression guard: existing callers using only the positional ``call``
    arg must keep getting the legacy behaviour (ToolResult with is_error=True
    on a missing tool, no tracker side-effects)."""
    registry = ToolRegistry()
    call = ToolCall(id="tc1", name="Edit", arguments={})
    result = await registry.dispatch(call)  # no kwargs at all
    assert result.is_error is True
    assert "not found" in (result.content or "").lower()


# ─── 5. AgentLoop instantiates a tracker ──────────────────────────────


def test_agent_loop_instantiates_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``AgentLoop.__init__`` should set ``self.demand_tracker`` to a
    ``PluginDemandTracker`` (or a no-op compatible shim — either is fine).

    The tracker must support ``record_tool_not_found`` (duck-type OK).
    """
    # Isolate home so profile/preset lookups don't touch the real ~/.opencomputer
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "home"))

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)

    from unittest.mock import MagicMock

    provider: Any = MagicMock()

    loop = AgentLoop(provider=provider, config=cfg, compaction_disabled=True)
    assert hasattr(loop, "demand_tracker")
    assert loop.demand_tracker is not None
    # Duck-type: must expose record_tool_not_found(tool_name, session_id, turn_index)
    assert callable(getattr(loop.demand_tracker, "record_tool_not_found", None))
