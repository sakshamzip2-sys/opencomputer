"""ExitPlanMode — signal that the agent has finished formulating a plan.

Claude Code parity. When the agent is in plan mode and has produced a
plan, it calls ``ExitPlanMode(plan="...")``. The tool returns the plan
wrapped in a user-visible "Plan ready for review / Awaiting user approval"
header so the host surface can render it prominently.

This is a SIGNAL, not a state change. The tool does NOT mutate
``RuntimeContext`` (which is frozen anyway). The user exits plan mode
out-of-band — either via the ``/exit-plan`` slash command (D8) or by
re-running without ``--plan``.

Safe to run in parallel — no side effects beyond producing output.
"""

from __future__ import annotations

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class ExitPlanModeTool(BaseTool):
    """Signal the end of plan-mode planning and surface the plan for review.

    The agent passes the full plan markdown as ``plan``. We wrap it with
    a header + footer so downstream UIs (CLI, Telegram, wire clients)
    can render it distinctly.
    """

    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ExitPlanMode",
            description=(
                "Signal that plan-mode planning is COMPLETE and surface the plan for "
                "user review. Use this only when you are in plan mode and have "
                "produced a concrete, actionable plan you want the user to approve "
                "before any edits begin. Pass the full plan as markdown — bullet "
                "points, code touched, success criteria, anything the user needs to "
                "judge whether to proceed. The tool wraps the plan in a 'Plan ready "
                "for review' header so the host surface can render it prominently. "
                "Does NOT auto-exit plan mode — the user decides whether to approve "
                "and re-run without --plan. Don't call this just to share a sketch; "
                "the model is in plan mode for a reason."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": "Markdown plan to show the user.",
                    },
                },
                "required": ["plan"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        plan = call.arguments.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            return ToolResult(
                tool_call_id=call.id,
                content="Error: plan must be a non-empty string",
                is_error=True,
            )
        wrapped = (
            "## Plan ready for review\n"
            "\n"
            f"{plan}\n"
            "\n"
            "---\n"
            "Awaiting user approval. Exit plan mode to proceed with these edits."
        )
        return ToolResult(tool_call_id=call.id, content=wrapped)


__all__ = ["ExitPlanModeTool"]
