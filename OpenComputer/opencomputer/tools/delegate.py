"""
delegate — spawn a fresh subagent in an isolated context.

Used when the main agent wants to offload a big exploration task without
polluting its own context. The subagent gets a fresh system prompt +
whatever briefing the main agent writes, runs its own while-loop, and
returns a single text summary.

Phase 1.5 stub: uses a simple approach where the subagent shares the
provider + tool registry, but keeps its own conversation messages.
Later phases can add context isolation, tool restrictions, etc.
"""

from __future__ import annotations

import dataclasses

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class DelegateTool(BaseTool):
    parallel_safe = True  # each delegate gets its own loop instance

    # Lazy-import a factory the CLI can inject; until then raise a clear error
    _factory = None
    #: Class-level "current runtime" set by the parent loop before dispatching
    #: tool calls. Ensures subagent loops inherit plan_mode / yolo_mode, etc.
    _current_runtime: RuntimeContext = DEFAULT_RUNTIME_CONTEXT

    @classmethod
    def set_factory(cls, factory) -> None:
        """Inject a callable that returns a fresh AgentLoop. Called once at CLI startup."""
        # staticmethod wrap prevents Python from binding `self` when we later do
        # `self._factory()` on an instance — lambdas and plain functions would
        # otherwise get `self` auto-injected.
        cls._factory = staticmethod(factory)

    @classmethod
    def set_runtime(cls, runtime: RuntimeContext) -> None:
        """Set the runtime context to propagate into subagents. Called by AgentLoop."""
        cls._current_runtime = runtime

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="delegate",
            description=(
                "Spawn a fresh subagent with isolated context to handle a specific task. "
                "Use this when you need to do heavy exploration (reading many files, searching "
                "code) and only want a summary back instead of polluting the main conversation. "
                "The subagent runs until it produces a final answer, then returns its output."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Describe the task for the subagent completely. The subagent has "
                            "no memory of the main conversation — include all context it needs."
                        ),
                    },
                },
                "required": ["task"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        task = call.arguments.get("task", "").strip()
        if not task:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: task description required",
                is_error=True,
            )
        if self._factory is None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Error: delegate is not initialized. "
                    "CLI bootstrapping must call DelegateTool.set_factory(...)."
                ),
                is_error=True,
            )
        subagent_loop = self._factory()
        # II.1: cap the subagent's iteration budget at the parent's
        # ``delegation_max_iterations`` (default 50) instead of letting it
        # inherit the full ``max_iterations``. Mirrors Hermes's pattern
        # (sources/hermes-agent/run_agent.py:IterationBudget lines 185-196).
        # Config/LoopConfig are frozen dataclasses — use ``dataclasses.replace``
        # to build a new LoopConfig with the override, then swap it onto the
        # child. ``dataclasses.is_dataclass`` guards against fake/mocked
        # subagents in tests that don't carry a real Config.
        child_cfg = getattr(subagent_loop, "config", None)
        if child_cfg is not None and dataclasses.is_dataclass(child_cfg):
            new_loop_cfg = dataclasses.replace(
                child_cfg.loop,
                max_iterations=child_cfg.loop.delegation_max_iterations,
            )
            subagent_loop.config = dataclasses.replace(child_cfg, loop=new_loop_cfg)
        # Propagate the parent's runtime context — plan mode, yolo mode, etc.
        # must apply to subagents too, otherwise delegating becomes an escape hatch.
        result = await subagent_loop.run_conversation(
            user_message=task,
            runtime=self._current_runtime,
        )
        # D7: emit SubagentStop hook when the delegated subagent finishes
        # so plugins can log / summarize / react. Fire-and-forget.
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookContext, HookEvent

            _hook_engine.fire_and_forget(
                HookContext(
                    event=HookEvent.SUBAGENT_STOP,
                    session_id=result.session_id,
                    runtime=self._current_runtime,
                )
            )
        except Exception:
            # Hook emission must never break the main delegate flow.
            pass
        return ToolResult(
            tool_call_id=call.id,
            content=result.final_message.content,
        )


__all__ = ["DelegateTool"]
