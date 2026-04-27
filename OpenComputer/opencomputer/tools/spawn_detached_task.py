"""``SpawnDetachedTask`` — agent-callable tool to enqueue a long-running job.

Use case (from Saksham):

> "Forward a chart, ask Claude for a deep 30-minute analysis, get a
> notification when done. The chat shouldn't hang waiting."

The agent calls this tool with a prompt; the store records a queued
task; the gateway's ``TaskRunner`` (already running in the daemon)
picks it up on its next poll and runs it through a fresh ``AgentLoop``.
The tool returns immediately with the task id so the agent can tell
the user "started task abc1234, will ping you when done" within the
same turn.

Failure modes:

- ``TaskStore.create`` raises if the DB is unreachable. We catch and
  return a clear error so the agent can retry.
- The ``TaskRunner`` may not be running (e.g. the agent invoked from
  ``opencomputer chat`` directly, no gateway daemon). The tool still
  succeeds — the task sits ``queued`` until the next gateway boot or
  manual ``opencomputer task run-once`` invocation. We surface this
  as a notice in the result text so the agent knows to set
  user expectations.

Consent: declared as ``IMPLICIT`` — spawning a task is similar in
risk profile to making any other LLM call. The task itself runs with
the same loop budget + cost-guard checks as a normal session, so
runaway costs are still bounded by existing protections.
"""

from __future__ import annotations

from typing import ClassVar

from opencomputer.agent.config import default_config
from opencomputer.tasks import TaskStore
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class SpawnDetachedTaskTool(BaseTool):
    parallel_safe = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="tasks.spawn_detached",
            tier_required=ConsentTier.IMPLICIT,
            human_description=(
                "Spawn a long-running agent task that survives the chat session"
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SpawnDetachedTask",
            description=(
                "Spawn a fire-and-forget background agent task. Use this when "
                "the user asks for analysis that will take more than ~30s "
                "(deep research, long calculations, multi-step file processing). "
                "Returns immediately with a task id; the task runs in the "
                "background and notifies the originating channel when done. "
                "DO NOT use this for short tasks (under 30s) — those should "
                "run inline. DO NOT use it to chain agent turns inside a "
                "single session — those belong in the same loop. The user "
                "checks status via `opencomputer task list` or by waiting "
                "for the notification."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The prompt the detached agent will run. Be "
                            "explicit — it runs in a fresh session with no "
                            "conversation history."
                        ),
                    },
                    "notify_policy": {
                        "type": "string",
                        "enum": ["done_only", "silent"],
                        "description": (
                            "How to notify on completion. 'done_only' "
                            "(default) posts the result to the original "
                            "channel; 'silent' records the result but "
                            "doesn't push (user pulls via "
                            "`opencomputer task list`)."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        prompt = str(call.arguments.get("prompt", "")).strip()
        notify_policy = str(call.arguments.get("notify_policy", "done_only"))
        if not prompt:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: 'prompt' is required",
                is_error=True,
            )
        if notify_policy not in ("done_only", "silent"):
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: notify_policy must be 'done_only' or 'silent' "
                    f"(got {notify_policy!r})"
                ),
                is_error=True,
            )

        cfg = default_config()
        db_path = cfg.home / "sessions.db"
        try:
            store = TaskStore(db_path)
            task = store.create(
                prompt=prompt,
                notify_policy=notify_policy,
            )
        except Exception as e:  # noqa: BLE001 — surface as tool error
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error spawning task: {type(e).__name__}: {e}",
                is_error=True,
            )

        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Detached task started. id: {task.id}\n"
                f"Status: queued — the gateway runner will pick it up "
                f"within {5} seconds.\n"
                "Check progress: `opencomputer task list` or "
                f"`opencomputer task show {task.id}`.\n\n"
                "Note: the task only runs while the gateway daemon is "
                "active. If you ran this from a one-off CLI session, "
                "start the gateway with `opencomputer gateway` to "
                "begin draining queued tasks."
            ),
        )


__all__ = ["SpawnDetachedTaskTool"]
