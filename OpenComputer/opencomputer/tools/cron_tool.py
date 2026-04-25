"""CronTool — agent-callable cron job management.

Lets the agent create, list, pause, resume, trigger, and remove cron jobs
from chat. Every action goes through the F1 ConsentGate via
:attr:`capability_claims`, so the user can grant ``cron.create`` once and
have subsequent calls bypass the prompt.

Capability tiers used:

- ``cron.create`` — EXPLICIT (user-visible: "agent wants to schedule a job")
- ``cron.modify`` — EXPLICIT (pause/resume/trigger/update existing job)
- ``cron.delete`` — EXPLICIT (irreversible)
- ``cron.list``   — IMPLICIT (read-only, no prompt)

The tool is registered in :mod:`opencomputer.tools.registry` and surfaces
as a single ``cron`` tool with an ``action`` parameter to keep schema flat.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

from opencomputer.cron.jobs import (
    create_job as _create_job,
)
from opencomputer.cron.jobs import (
    get_job,
    list_jobs,
    pause_job,
    remove_job,
    resume_job,
    trigger_job,
)
from opencomputer.cron.threats import CronThreatBlocked
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger(__name__)


_VALID_ACTIONS = frozenset(
    {"create", "list", "get", "pause", "resume", "trigger", "remove"}
)


class CronTool(BaseTool):
    """Manage scheduled cron jobs from agent chat.

    Single-tool design: one tool, one ``action`` parameter, action-specific
    fields. Mirrors Hermes's ``cronjob`` tool to avoid schema/context bloat
    from exposing N separate tools.
    """

    parallel_safe = False
    """Cron writes shared state (jobs.json); serialize tool calls."""

    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="cron.create",
            tier_required=ConsentTier.EXPLICIT,
            human_description=(
                "Schedule a new cron job. Each scheduled job runs the agent "
                "in a fresh session at the configured time, so destructive "
                "tools should remain in plan_mode unless explicitly opted out."
            ),
            data_scope="cron",
        ),
        CapabilityClaim(
            capability_id="cron.modify",
            tier_required=ConsentTier.EXPLICIT,
            human_description=(
                "Modify an existing cron job (pause / resume / trigger / update)."
            ),
            data_scope="cron",
        ),
        CapabilityClaim(
            capability_id="cron.delete",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Remove a cron job permanently.",
            data_scope="cron",
        ),
        CapabilityClaim(
            capability_id="cron.list",
            tier_required=ConsentTier.IMPLICIT,
            human_description="List existing cron jobs (read-only).",
            data_scope="cron",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="cron",
            description=(
                "Manage scheduled agent runs (cron jobs). "
                "Use action='list' to see existing jobs, action='create' to add a new one, "
                "and action ∈ {pause,resume,trigger,remove} to manage by id.\n\n"
                "Schedule formats: '30m' / '2h' / '1d' (one-shot), 'every 30m' (recurring), "
                "'0 9 * * *' (cron expression), '2026-04-30T08:30' (timestamp).\n\n"
                "Prefer skill= over prompt= when possible — skills are vetted code; "
                "prompt= triggers a stricter threat scan."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_VALID_ACTIONS),
                        "description": "What to do.",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Existing job id (required for get/pause/resume/trigger/remove).",
                    },
                    "name": {
                        "type": "string",
                        "description": "Friendly name for new job (create only).",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "Schedule expression (create only). See description.",
                    },
                    "skill": {
                        "type": "string",
                        "description": "Skill to invoke at run time (preferred over prompt).",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Free-text prompt for the agent (threat-scanned). Ignored when skill is set.",
                    },
                    "repeat": {
                        "type": "integer",
                        "description": "How many times to run before auto-remove. Omit for infinite (recurring) or 1 (one-shot).",
                    },
                    "notify": {
                        "type": "string",
                        "description": "Where to deliver output: 'telegram', 'discord', 'telegram:<chat_id>', or omit for local-only.",
                    },
                    "plan_mode": {
                        "type": "boolean",
                        "description": "Whether the cron run starts in plan_mode (default: true). Set false only for trusted skills.",
                    },
                    "include_disabled": {
                        "type": "boolean",
                        "description": "When listing, also include paused/completed jobs.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Human reason for pausing (optional, action=pause only).",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        action = (args.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: action must be one of {sorted(_VALID_ACTIONS)}",
                is_error=True,
            )

        try:
            payload = await self._dispatch(action, args)
        except CronThreatBlocked as exc:
            logger.warning("Cron tool blocked unsafe prompt: %s", exc)
            return ToolResult(tool_call_id=call.id, content=str(exc), is_error=True)
        except (ValueError, KeyError) as exc:
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cron tool action %s failed", action)
            return ToolResult(
                tool_call_id=call.id,
                content=f"Internal error in cron tool: {exc}",
                is_error=True,
            )

        return ToolResult(tool_call_id=call.id, content=json.dumps(payload, default=str, indent=2))

    async def _dispatch(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == "list":
            jobs = list_jobs(include_disabled=bool(args.get("include_disabled", False)))
            return {"action": "list", "count": len(jobs), "jobs": [_summarize(j) for j in jobs]}

        if action == "get":
            job = _require_job(args)
            return {"action": "get", "job": job}

        if action == "create":
            schedule = _require(args, "schedule")
            skill = (args.get("skill") or "").strip() or None
            prompt = (args.get("prompt") or "").strip() or None
            if not skill and not prompt:
                raise ValueError("create requires either 'skill' or 'prompt'")
            job = _create_job(
                schedule=schedule,
                name=args.get("name"),
                prompt=prompt,
                skill=skill,
                repeat=args.get("repeat"),
                notify=(args.get("notify") or None),
                plan_mode=bool(args.get("plan_mode", True)),
            )
            return {"action": "create", "job": _summarize(job)}

        if action == "pause":
            job = pause_job(_require(args, "job_id"), args.get("reason"))
            if not job:
                raise KeyError(f"job_id={args.get('job_id')!r} not found")
            return {"action": "pause", "job": _summarize(job)}

        if action == "resume":
            job = resume_job(_require(args, "job_id"))
            if not job:
                raise KeyError(f"job_id={args.get('job_id')!r} not found")
            return {"action": "resume", "job": _summarize(job)}

        if action == "trigger":
            job = trigger_job(_require(args, "job_id"))
            if not job:
                raise KeyError(f"job_id={args.get('job_id')!r} not found")
            return {"action": "trigger", "job": _summarize(job)}

        if action == "remove":
            ok = remove_job(_require(args, "job_id"))
            if not ok:
                raise KeyError(f"job_id={args.get('job_id')!r} not found")
            return {"action": "remove", "job_id": args["job_id"], "removed": True}

        raise ValueError(f"unhandled action {action!r}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require(args: dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not val or not str(val).strip():
        raise ValueError(f"missing required field {key!r}")
    return str(val).strip()


def _require_job(args: dict[str, Any]) -> dict[str, Any]:
    job_id = _require(args, "job_id")
    job = get_job(job_id)
    if not job:
        raise KeyError(f"job_id={job_id!r} not found")
    return job


def _summarize(job: dict[str, Any]) -> dict[str, Any]:
    """Compact representation for list/create/pause/etc. responses."""
    return {
        "id": job["id"],
        "name": job["name"],
        "schedule": job.get("schedule_display") or job["schedule"].get("display"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "state": job.get("state"),
        "enabled": job.get("enabled"),
        "skill": job.get("skill"),
        "notify": job.get("notify"),
        "plan_mode": job.get("plan_mode"),
        "repeat": job.get("repeat"),
    }


__all__ = ["CronTool"]
