"""Heartbeat lane — A2 from 2026-05-06 OpenClaw deep-comparison.

Heartbeat = always-on agent tick. Distinct from cron (calendared jobs)
in spirit but implemented on top of the existing cron infra so we don't
duplicate scheduler/storage. A heartbeat is a single recurring job
tagged with ``lane="heartbeat"`` whose body asks the agent
"anything pending?" at a configured interval.

Why a separate lane (vs. just a cron job): the brief argues heartbeat
and cron should not share an execution queue conceptually — cron is
"the agent's calendar", heartbeat is "the agent's pulse". Today that
distinction lives in metadata (the ``lane`` tag on the job) and CLI
ergonomics (``oc heartbeat enable`` is faster than constructing the
equivalent ``oc cron create`` invocation). Future work can split
schedulers if a real load-isolation need appears.
"""

from __future__ import annotations

import json
from typing import Any

import filelock

from opencomputer.cron import (
    create_job,
    list_jobs,
    pause_job,
    remove_job,
    resume_job,
)
from opencomputer.cron.jobs import jobs_file

DEFAULT_HEARTBEAT_INTERVAL_MIN: int = 30
HEARTBEAT_LANE: str = "heartbeat"
DEFAULT_HEARTBEAT_PROMPT: str = (
    "Heartbeat tick — review recent activity, surface anything pending: "
    "stale TODO items, missed reminders, follow-up questions, "
    "or background goals that need attention. If nothing's pending, "
    "say 'all clear' and stop."
)


def _heartbeat_jobs() -> list[dict[str, Any]]:
    """Return all jobs tagged as heartbeat (including paused/disabled)."""
    return [
        j for j in list_jobs(include_disabled=True)
        if j.get("lane") == HEARTBEAT_LANE
    ]


def is_heartbeat_enabled() -> bool:
    return any(_heartbeat_jobs())


def heartbeat_status() -> dict[str, Any]:
    """Summarize heartbeat state — present? interval? last run? next run?.

    Reads ``state == "paused"`` (the cron module's actual paused signal)
    rather than a non-existent ``paused`` boolean, and pulls
    ``interval_minutes`` from the parsed schedule dict.
    """
    jobs = _heartbeat_jobs()
    if not jobs:
        return {
            "enabled": False,
            "lane": HEARTBEAT_LANE,
        }
    j = jobs[0]
    sched = j.get("schedule") or {}
    paused = j.get("state") == "paused" or j.get("enabled") is False
    return {
        "enabled": not paused,
        "paused": paused,
        "lane": HEARTBEAT_LANE,
        "job_id": j.get("id"),
        "interval_minutes": sched.get("minutes"),
        "schedule_display": sched.get("display") or j.get("schedule_display"),
        "last_run_at": j.get("last_run_at"),
        "next_run_at": j.get("next_run_at"),
        "run_count": j.get("repeat", {}).get("completed", 0),
    }


def enable_heartbeat(
    *,
    interval_minutes: int = DEFAULT_HEARTBEAT_INTERVAL_MIN,
    prompt: str = DEFAULT_HEARTBEAT_PROMPT,
    notify: str | None = None,
    plan_mode: bool = True,
) -> dict[str, Any]:
    """Create a heartbeat job (or no-op if one already exists).

    Returns the created/existing job dict for symmetry with
    :func:`opencomputer.cron.create_job`.
    """
    existing = _heartbeat_jobs()
    if existing:
        # Already enabled — return first match without mutating.
        return existing[0]

    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be > 0")

    # parse_schedule treats a bare "15m" as a one-shot delay; "every 15m"
    # is the interval form we want for an always-on heartbeat.
    job = create_job(
        schedule=f"every {interval_minutes}m",
        name=f"heartbeat ({interval_minutes}m)",
        prompt=prompt,
        notify=notify,
        plan_mode=plan_mode,
    )
    # Tag the job as heartbeat-lane so list/status filters can find it.
    # ``create_job`` doesn't expose this kwarg; we mutate the persisted
    # store via filelock-protected read-modify-write.
    jf = jobs_file()
    lock_path = jf.with_suffix(jf.suffix + ".lock")
    with filelock.FileLock(str(lock_path)):
        data = (
            json.loads(jf.read_text(encoding="utf-8"))
            if jf.exists()
            else {"jobs": []}
        )
        for j in data.get("jobs", []):
            if j.get("id") == job.get("id"):
                j["lane"] = HEARTBEAT_LANE
                job["lane"] = HEARTBEAT_LANE
                break
        jf.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return job


def disable_heartbeat() -> int:
    """Remove ALL heartbeat-lane jobs. Returns count removed."""
    jobs = _heartbeat_jobs()
    n = 0
    for j in jobs:
        if remove_job(j["id"]):
            n += 1
    return n


def pause_heartbeat() -> bool:
    jobs = _heartbeat_jobs()
    if not jobs:
        return False
    pause_job(jobs[0]["id"], reason="paused via oc heartbeat pause")
    return True


def resume_heartbeat() -> bool:
    jobs = _heartbeat_jobs()
    if not jobs:
        return False
    resume_job(jobs[0]["id"])
    return True


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_MIN",
    "DEFAULT_HEARTBEAT_PROMPT",
    "HEARTBEAT_LANE",
    "disable_heartbeat",
    "enable_heartbeat",
    "heartbeat_status",
    "is_heartbeat_enabled",
    "pause_heartbeat",
    "resume_heartbeat",
]
