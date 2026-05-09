"""ExitPlanMode — signal that the agent has finished formulating a plan.

Claude Code parity. When the agent is in plan mode and has produced a
plan, it calls ``ExitPlanMode(plan="...")``. The tool returns the plan
wrapped in a user-visible "Plan ready for review / Awaiting user approval"
header so the host surface can render it prominently.

This is a SIGNAL, not a state change. The tool does NOT mutate
``RuntimeContext`` (which is frozen anyway). The user exits plan mode
out-of-band — either via the ``/exit-plan`` slash command (D8) or by
re-running without ``--plan``.

v1.1 plan-2 M5.4 (2026-05-09): the tool now accepts an optional
``next_mode`` suggestion (``auto`` / ``acceptEdits`` / ``manual`` /
``keep``) which gets stored in a process-wide proposal slot so the
consuming surface (CLI prompt, wire `plan.exit_proposal` event) can
act on it. Loop mutation of ``RuntimeContext.permission_mode`` is a
separate concern — the exit-plan slash command (D8) remains the
canonical apply path.

Safe to run in parallel — no side effects beyond producing output.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

#: Allowed values for the ``next_mode`` parameter. Subset of canonical
#: PermissionMode enum + a "keep" sentinel meaning "stay in plan mode
#: and continue iterating". Mirrors the plan's 4-route flow.
PROPOSED_EXIT_MODES: tuple[str, ...] = ("auto", "acceptEdits", "manual", "keep")
ProposedExitMode = Literal["auto", "acceptEdits", "manual", "keep"]


@dataclass(frozen=True, slots=True)
class ExitPlanProposal:
    """Most-recent plan + suggested next_mode emitted by ExitPlanMode."""

    plan: str
    next_mode: str  # one of PROPOSED_EXIT_MODES


_PROPOSAL_LOCK = threading.Lock()
_LAST_PROPOSAL: ExitPlanProposal | None = None


def get_last_proposal() -> ExitPlanProposal | None:
    """Return the most recent proposal without consuming it. Read-only."""
    with _PROPOSAL_LOCK:
        return _LAST_PROPOSAL


def pop_last_proposal() -> ExitPlanProposal | None:
    """Return + clear the most recent proposal. Use after the consuming
    surface routes the decision back to the runtime."""
    global _LAST_PROPOSAL  # noqa: PLW0603
    with _PROPOSAL_LOCK:
        out = _LAST_PROPOSAL
        _LAST_PROPOSAL = None
        return out


def _record_proposal(plan: str, next_mode: str) -> None:
    global _LAST_PROPOSAL  # noqa: PLW0603
    with _PROPOSAL_LOCK:
        _LAST_PROPOSAL = ExitPlanProposal(plan=plan, next_mode=next_mode)


class ExitPlanModeTool(BaseTool):
    """Signal the end of plan-mode planning and surface the plan for review.

    M5.4 — when ``next_mode`` is set, the tool stores the proposal in a
    process-wide slot so consuming surfaces (CLI prompt, wire-server
    `plan.exit_proposal` event) can act on it.
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
                "judge whether to proceed. Optionally pass ``next_mode`` to suggest "
                "what permission mode to switch to after approval (auto = "
                "fully-automated execution, acceptEdits = file edits OK but Bash + "
                "network still prompt, manual = ask per action, keep = stay in plan "
                "mode and iterate). The tool wraps the plan in a 'Plan ready for "
                "review' header so the host surface can render it prominently. "
                "Does NOT auto-exit plan mode — the user decides; the runtime "
                "applies the chosen mode out-of-band."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": "Markdown plan to show the user.",
                    },
                    "next_mode": {
                        "type": "string",
                        "enum": list(PROPOSED_EXIT_MODES),
                        "description": (
                            "Optional. Suggest the post-approval permission mode. "
                            "'auto' = no per-action confirms (use when the plan is "
                            "low-risk and you want the agent to rip through it). "
                            "'acceptEdits' = file edits auto-approved but Bash/network "
                            "still prompt. 'manual' = ask per action (Tier-2 default). "
                            "'keep' = stay in plan mode and continue iterating."
                        ),
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
        next_mode_raw = call.arguments.get("next_mode")
        next_mode: str | None = None
        if next_mode_raw is not None:
            if not isinstance(next_mode_raw, str) or next_mode_raw not in PROPOSED_EXIT_MODES:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"Error: next_mode must be one of "
                        f"{', '.join(PROPOSED_EXIT_MODES)} (got {next_mode_raw!r})."
                    ),
                    is_error=True,
                )
            next_mode = next_mode_raw

        if next_mode:
            _record_proposal(plan=plan, next_mode=next_mode)
            mode_line = (
                f"\n**Suggested next_mode:** `{next_mode}` "
                f"(consume via `oc /exit-plan {next_mode}` or wire "
                f"`plan.exit_decision` RPC).\n"
            )
        else:
            mode_line = ""

        wrapped = (
            "## Plan ready for review\n"
            "\n"
            f"{plan}\n"
            f"{mode_line}"
            "\n---\n"
            "Awaiting user approval. Exit plan mode to proceed with these edits."
        )
        return ToolResult(tool_call_id=call.id, content=wrapped)


__all__ = [
    "PROPOSED_EXIT_MODES",
    "ExitPlanModeTool",
    "ExitPlanProposal",
    "ProposedExitMode",
    "get_last_proposal",
    "pop_last_proposal",
]
