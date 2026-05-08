"""GET/POST/PUT/DELETE /api/v1/cron/jobs/* — cron CRUD.

Wraps :mod:`opencomputer.cron.jobs` directly. The previous version
routed through an abstract ``_store()`` that passed kwargs
(``name``/``command``/``enabled``) which ``cron.jobs.create_job`` does
not accept — POST + PUT were silently broken whenever the generic-store
fallback resolved to the real ``cron`` package. Production-grade
(2026-05-09) shipping accepts the full Hermes-parity field set
(``prompt``/``skill``/``skills``/``notify``/``plan_mode``/``enabled_toolsets``/
``context_from``/``workdir``/``no_agent``/``script``/``script_timeout_seconds``/
``repeat``) and keeps ``command`` as a back-compat alias for ``prompt``.

Read endpoints (``GET /cron/jobs``, ``GET /cron/jobs/{id}``) surface
every field via :func:`_job_to_dict`. Write endpoints validate on entry
and return the full canonical job dict on success.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field, model_validator

from opencomputer.dashboard.routes._common import audit_log

router = APIRouter(prefix="/api/v1", tags=["cron"])


# ---------------------------------------------------------------------------
# Request models — extended 2026-05-09 to mirror the cron.jobs surface.
# ---------------------------------------------------------------------------


class CreateJobBody(BaseModel):
    """Request body for ``POST /cron/jobs``.

    Hermes-parity fields (2026-05-09):
        Goal:           ``prompt`` (free-text) OR ``skill`` (singular)
                        OR ``skills`` (plural list) OR
                        ``no_agent=True`` + ``script``.
        Delivery:       ``notify`` ∈ ``{None, "local", "origin",
                        "<platform>:<chat_id>[:<thread_id>]"}``.
        Runtime:        ``plan_mode``, ``enabled_toolsets``,
                        ``context_from``, ``workdir``.
        Script-only:    ``no_agent``, ``script``, ``script_timeout_seconds``.
        Repeat:         ``repeat`` (int; 0/None = infinite for recurring).

    Back-compat:
        ``command`` is accepted as an alias for ``prompt`` (the old
        route's only goal-field). When both are supplied ``prompt``
        wins; when only ``command`` is supplied it is mapped to
        ``prompt`` transparently.

    The legacy ``enabled`` field is accepted for back-compat but only
    affects whether the job is left running (``True``, default) or
    immediately paused after creation (``False``).
    """

    name: str | None = Field(
        default=None,
        max_length=120,
        description="Friendly name. Auto-generated from prompt/skill/script when omitted.",
    )
    schedule: str = Field(..., description="Schedule expression — see parse_schedule docstring.")

    # Goal fields — at least one must be set (or no_agent + script).
    prompt: str | None = Field(default=None, description="Free-text prompt (threat-scanned).")
    command: str | None = Field(
        default=None,
        description="Back-compat alias for ``prompt`` (legacy field). Use ``prompt`` in new clients.",
    )
    skill: str | None = Field(default=None, description="Singular skill name.")
    skills: list[str] | None = Field(default=None, description="Plural skill list (Hermes parity).")

    # Delivery + runtime
    notify: str | None = Field(
        default=None,
        description="Delivery target: 'local'/'origin'/'<platform>'/'<platform>:<chat_id>[:<thread_id>]'.",
    )
    plan_mode: bool | None = Field(default=None, description="If False, destructive tools run unguarded.")
    enabled_toolsets: list[str] | None = Field(
        default=None,
        description="Tool allowlist for the cron run. None=full set, []=no tools.",
    )
    context_from: list[str] | None = Field(
        default=None,
        description="Upstream cron job IDs whose ``last_response`` is prepended.",
    )
    workdir: str | None = Field(
        default=None,
        description="Absolute working directory for the agent run (must exist).",
    )

    # Script-only mode
    no_agent: bool = Field(default=False, description="Skip LLM, run a shell script instead.")
    script: str | None = Field(
        default=None,
        description="Script name (relative to ~/.opencomputer/<profile>/scripts/).",
    )
    script_timeout_seconds: int | None = Field(
        default=None, description="Per-job override of cron.script_timeout_seconds."
    )

    # Repeat + legacy enabled
    repeat: int | None = Field(default=None, description="Run N times then auto-remove (None = infinite).")
    enabled: bool = Field(default=True, description="Legacy: pause after create when False.")

    @model_validator(mode="after")
    def _check_goal(self) -> CreateJobBody:
        # Map command → prompt when prompt is empty (back-compat).
        if not self.prompt and self.command:
            self.prompt = self.command
        # Exactly one goal-mode must be active.
        if self.no_agent:
            if not self.script:
                raise ValueError("no_agent=True requires script=<name>")
            if self.prompt or self.skill or self.skills:
                raise ValueError("no_agent is exclusive with prompt/skill/skills")
        elif not (self.prompt or self.skill or self.skills):
            raise ValueError(
                "must supply one of: prompt, skill, skills, or no_agent+script"
            )
        return self


class UpdateJobBody(BaseModel):
    """Request body for ``PUT /cron/jobs/{id}``.

    All fields optional. Only fields explicitly set in the request body
    are forwarded to ``cron.jobs.update_job``. Skill mutation matches
    the CLI semantics: pass ``skill`` to replace, ``skills`` to set the
    plural list, or send an empty list to clear.

    ``command`` is a back-compat alias for ``prompt``. ``enabled`` is
    forwarded as a paused/scheduled state-flip.
    """

    name: str | None = None
    schedule: str | None = None
    prompt: str | None = None
    command: str | None = Field(
        default=None,
        description="Back-compat alias for ``prompt``. ``prompt`` wins when both supplied.",
    )
    skill: str | None = None
    skills: list[str] | None = None
    notify: str | None = None
    plan_mode: bool | None = None
    enabled_toolsets: list[str] | None = None
    context_from: list[str] | None = None
    workdir: str | None = None
    repeat: int | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _job_to_dict(job: Any) -> dict:
    """Serialize a cron job to a dict for the dashboard API.

    Supports two job shapes:
      - Dict (current ``opencomputer.cron.jobs`` shape — what the system
        actually uses today).
      - Object with attributes (legacy CronStore fallback, kept for
        backward compatibility).

    Hermes parity (2026-05-09): exposes ``skill``, ``skills``, ``notify``,
    ``plan_mode``, ``enabled_toolsets``, ``context_from``, ``workdir``,
    ``no_agent``, ``script``, ``origin_platform``, ``origin_chat_id``,
    ``origin_thread_id``, ``repeat``, ``state``, ``last_status``,
    ``last_error`` so dashboard UIs can render and edit them.
    """
    if isinstance(job, dict):
        sched = job.get("schedule")
        sched_display = job.get("schedule_display") or (
            sched.get("display") if isinstance(sched, dict) else sched
        )
        return {
            "id": job.get("id", ""),
            "name": job.get("name", ""),
            "schedule": sched_display or "",
            "command": job.get("prompt") or "",
            "prompt": job.get("prompt"),
            "skill": job.get("skill"),
            "skills": job.get("skills"),
            "enabled": job.get("enabled", True),
            "state": job.get("state"),
            "last_run": job.get("last_run_at"),
            "next_run": job.get("next_run_at"),
            "last_status": job.get("last_status"),
            "last_error": job.get("last_error"),
            "notify": job.get("notify"),
            "plan_mode": job.get("plan_mode", True),
            "enabled_toolsets": job.get("enabled_toolsets"),
            "context_from": job.get("context_from"),
            "workdir": job.get("workdir"),
            "no_agent": job.get("no_agent", False),
            "script": job.get("script"),
            "script_timeout_seconds": job.get("script_timeout_seconds"),
            "origin_platform": job.get("origin_platform"),
            "origin_chat_id": job.get("origin_chat_id"),
            "origin_thread_id": job.get("origin_thread_id"),
            "repeat": job.get("repeat"),
        }
    # Fallback: legacy object shape.
    return {
        "id": getattr(job, "id", str(job)),
        "name": getattr(job, "name", ""),
        "schedule": getattr(job, "schedule", ""),
        "command": getattr(job, "command", ""),
        "enabled": getattr(job, "enabled", True),
        "last_run": getattr(job, "last_run", None),
        "next_run": getattr(job, "next_run", None),
    }


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@router.get("/cron/jobs")
async def list_jobs() -> dict:
    try:
        from opencomputer.cron.jobs import list_jobs as _list_jobs
        jobs = _list_jobs(include_disabled=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"cron list failed: {exc}") from exc
    return {"items": [_job_to_dict(j) for j in jobs]}


@router.get("/cron/jobs/{job_id}")
async def get_job_route(job_id: str) -> dict:
    try:
        from opencomputer.cron.jobs import get_job
        job = get_job(job_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_dict(job)


# ---------------------------------------------------------------------------
# Write endpoints — call cron.jobs directly (the abstract _store layer
# was always mismatched with cron.jobs's actual signature).
# ---------------------------------------------------------------------------


@router.post("/cron/jobs", status_code=201)
async def create_job_route(body: CreateJobBody) -> dict:
    """Create a new cron job through the canonical ``cron.jobs.create_job``."""
    from opencomputer.cron.jobs import create_job, pause_job
    from opencomputer.cron.threats import CronThreatBlocked

    kwargs: dict[str, Any] = {
        "schedule": body.schedule,
        "name": body.name,
    }
    if body.prompt:
        kwargs["prompt"] = body.prompt
    if body.skill:
        kwargs["skill"] = body.skill
    if body.skills:
        kwargs["skills"] = body.skills
    if body.notify is not None:
        kwargs["notify"] = body.notify or None
    if body.plan_mode is not None:
        kwargs["plan_mode"] = body.plan_mode
    if body.enabled_toolsets is not None:
        kwargs["enabled_toolsets"] = body.enabled_toolsets
    if body.context_from is not None:
        kwargs["context_from"] = body.context_from
    if body.workdir is not None:
        kwargs["workdir"] = body.workdir or None
    if body.no_agent:
        kwargs["no_agent"] = True
        kwargs["script"] = body.script
        if body.script_timeout_seconds is not None:
            kwargs["script_timeout_seconds"] = body.script_timeout_seconds
    if body.repeat is not None:
        kwargs["repeat"] = body.repeat

    try:
        job = create_job(**kwargs)
    except CronThreatBlocked as exc:
        raise HTTPException(status_code=400, detail=f"threat scan blocked: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"create failed: {exc}") from exc

    # Legacy `enabled=False` → immediately pause (matches the old route's
    # interpretation; cron.jobs.create_job doesn't take an `enabled` arg).
    if not body.enabled:
        pause_job(job["id"], reason="created paused via API")
        # Re-read so the response reflects the paused state.
        from opencomputer.cron.jobs import get_job as _get
        job = _get(job["id"]) or job

    audit_log(
        "cron.create",
        source="dashboard",
        job_id=job["id"],
        schedule=body.schedule,
        has_prompt=bool(body.prompt),
        skill_count=len(body.skills or []) if body.skills else (1 if body.skill else 0),
        no_agent=body.no_agent,
        notify=body.notify,
    )
    return _job_to_dict(job)


@router.put("/cron/jobs/{job_id}")
async def update_job_route(job_id: str, body: UpdateJobBody) -> dict:
    """Update an existing cron job.

    Map ``command`` → ``prompt`` for back-compat. Skill mutation: pass
    ``skill`` (singular) or ``skills`` (plural list) to replace; the
    matching opposite field is auto-cleared. ``enabled=False`` pauses;
    ``enabled=True`` resumes.
    """
    from opencomputer.cron.jobs import (
        get_job,
        pause_job,
        resume_job,
        update_job,
    )
    from opencomputer.cron.threats import CronThreatBlocked

    existing = get_job(job_id)
    if not existing:
        raise HTTPException(status_code=404, detail="job not found")

    # Build the updates dict from explicitly-set body fields only.
    fields = body.model_dump(exclude_unset=True)
    updates: dict[str, Any] = {}

    if "name" in fields:
        updates["name"] = fields["name"]
    if "schedule" in fields:
        updates["schedule"] = fields["schedule"]
    # command → prompt back-compat. prompt wins when both supplied.
    if "prompt" in fields:
        updates["prompt"] = fields["prompt"]
    elif "command" in fields:
        updates["prompt"] = fields["command"]

    # Skill mutation with mutual exclusion (mirrors CLI semantics).
    skill_touched = "skill" in fields or "skills" in fields
    if "skills" in fields:
        new_skills = fields["skills"] or []
        updates["skills"] = new_skills if new_skills else None
        updates["skill"] = None
    elif "skill" in fields:
        sk = fields["skill"]
        updates["skill"] = sk
        updates["skills"] = None

    # Mutual exclusion: prompt active ⇒ clear stale skills.
    if "prompt" in updates and updates["prompt"] and not skill_touched:
        if existing.get("skill") or existing.get("skills"):
            updates["skill"] = None
            updates["skills"] = None
    # Mirror: skills active ⇒ clear stale prompt.
    if skill_touched and (updates.get("skill") or updates.get("skills")):
        if "prompt" not in updates and existing.get("prompt"):
            updates["prompt"] = None

    if "notify" in fields:
        updates["notify"] = fields["notify"] or None
    if "plan_mode" in fields:
        updates["plan_mode"] = fields["plan_mode"]
    if "enabled_toolsets" in fields:
        updates["enabled_toolsets"] = fields["enabled_toolsets"]
    if "context_from" in fields:
        updates["context_from"] = fields["context_from"]
    if "workdir" in fields:
        updates["workdir"] = fields["workdir"] or None
    if "repeat" in fields:
        rep = existing.get("repeat") or {"times": None, "completed": 0}
        rep = dict(rep)
        rep["times"] = fields["repeat"] if (fields["repeat"] or 0) > 0 else None
        updates["repeat"] = rep

    enabled_change = fields.get("enabled") if "enabled" in fields else None

    if not updates and enabled_change is None:
        raise HTTPException(status_code=400, detail="no updatable fields supplied")

    try:
        updated = update_job(job_id, updates) if updates else existing
    except CronThreatBlocked as exc:
        raise HTTPException(status_code=400, detail=f"threat scan blocked: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"update failed: {exc}") from exc

    if not updated:
        raise HTTPException(status_code=404, detail="job not found")

    # enabled flip — kept after update_job so the field-update + state
    # flip happens atomically in the user's mental model.
    if enabled_change is False:
        updated = pause_job(job_id, reason="paused via API") or updated
    elif enabled_change is True and existing.get("state") == "paused":
        updated = resume_job(job_id) or updated

    audit_log("cron.update", source="dashboard", job_id=job_id, fields=sorted(updates))
    return _job_to_dict(updated)


@router.post("/cron/jobs/{job_id}/pause")
async def pause_job_route(job_id: str) -> dict:
    from opencomputer.cron.jobs import pause_job
    job = pause_job(job_id, reason="paused via API")
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    audit_log("cron.pause", source="dashboard", job_id=job_id)
    return {"ok": True, "id": job_id, "paused": True}


@router.post("/cron/jobs/{job_id}/resume")
async def resume_job_route(job_id: str) -> dict:
    from opencomputer.cron.jobs import resume_job
    job = resume_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    audit_log("cron.resume", source="dashboard", job_id=job_id)
    return {"ok": True, "id": job_id, "paused": False}


@router.post("/cron/jobs/{job_id}/trigger")
async def trigger_job_route(job_id: str) -> dict:
    from opencomputer.cron.jobs import trigger_job
    job = trigger_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    audit_log("cron.trigger", source="dashboard", job_id=job_id)
    return {"ok": True, "id": job_id}


@router.delete("/cron/jobs/{job_id}", status_code=204)
async def delete_job_route(job_id: str):
    from opencomputer.cron.jobs import remove_job
    ok = remove_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    audit_log("cron.delete", source="dashboard", job_id=job_id)
    return Response(status_code=204)
