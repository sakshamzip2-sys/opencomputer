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

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class DelegateTool(BaseTool):
    parallel_safe = True  # each delegate gets its own loop instance

    # Lazy-import a factory the CLI can inject; until then raise a clear error
    _factory = None

    @classmethod
    def set_factory(cls, factory) -> None:
        """Inject a callable that returns a fresh AgentLoop. Called once at CLI startup."""
        cls._factory = factory

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
        result = await subagent_loop.run_conversation(user_message=task)
        return ToolResult(
            tool_call_id=call.id,
            content=result.final_message.content,
        )


__all__ = ["DelegateTool"]
