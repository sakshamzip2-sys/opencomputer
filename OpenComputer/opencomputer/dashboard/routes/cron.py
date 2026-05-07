"""GET/POST/PUT/DELETE /api/v1/cron/jobs/* — cron CRUD.

Wraps :mod:`opencomputer.cron` low-level helpers (Typer commands print to
stdout/exit; we use the underlying state-store directly for clean dicts).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from opencomputer.dashboard.routes._common import audit_log

router = APIRouter(prefix="/api/v1", tags=["cron"])


class CreateJobBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    schedule: str = Field(..., description="Cron expression, e.g. '0 9 * * *'")
    command: str = Field(..., min_length=1)
    enabled: bool = True


class UpdateJobBody(BaseModel):
    name: str | None = None
    schedule: str | None = None
    command: str | None = None
    enabled: bool | None = None


def _store():
    """Get the cron job store. Lazy import; tolerate missing module."""
    try:
        from opencomputer.cron import store as cron_store

        return cron_store
    except ImportError:
        try:
            from opencomputer import cron as cron_store

            return cron_store
        except ImportError:
            raise HTTPException(status_code=503, detail="cron subsystem unavailable")


def _job_to_dict(job) -> dict:
    return {
        "id": getattr(job, "id", str(job)),
        "name": getattr(job, "name", ""),
        "schedule": getattr(job, "schedule", ""),
        "command": getattr(job, "command", ""),
        "enabled": getattr(job, "enabled", True),
        "last_run": getattr(job, "last_run", None),
        "next_run": getattr(job, "next_run", None),
    }


@router.get("/cron/jobs")
async def list_jobs() -> dict:
    s = _store()
    try:
        jobs = list(s.list_jobs()) if hasattr(s, "list_jobs") else []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"cron list failed: {exc}")
    return {"items": [_job_to_dict(j) for j in jobs]}


@router.get("/cron/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    s = _store()
    try:
        job = s.get_job(job_id) if hasattr(s, "get_job") else None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_dict(job)


@router.post("/cron/jobs", status_code=201)
async def create_job(body: CreateJobBody) -> dict:
    s = _store()
    if not hasattr(s, "create_job"):
        raise HTTPException(status_code=503, detail="cron.create_job unavailable")
    try:
        job = s.create_job(
            name=body.name,
            schedule=body.schedule,
            command=body.command,
            enabled=body.enabled,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    audit_log("cron.create", name=body.name, schedule=body.schedule)
    return _job_to_dict(job)


@router.put("/cron/jobs/{job_id}")
async def update_job(job_id: str, body: UpdateJobBody) -> dict:
    s = _store()
    if not hasattr(s, "update_job"):
        raise HTTPException(status_code=503, detail="cron.update_job unavailable")
    try:
        job = s.update_job(job_id, **body.model_dump(exclude_none=True))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    audit_log("cron.update", id=job_id)
    return _job_to_dict(job)


@router.post("/cron/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict:
    s = _store()
    fn = getattr(s, "pause_job", None) or getattr(s, "update_job", None)
    if fn is None:
        raise HTTPException(status_code=503, detail="cron pause unavailable")
    try:
        if fn is getattr(s, "pause_job", None):
            fn(job_id)
        else:
            fn(job_id, enabled=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": job_id, "paused": True}


@router.post("/cron/jobs/{job_id}/resume")
async def resume_job(job_id: str) -> dict:
    s = _store()
    fn = getattr(s, "resume_job", None) or getattr(s, "update_job", None)
    if fn is None:
        raise HTTPException(status_code=503, detail="cron resume unavailable")
    try:
        if fn is getattr(s, "resume_job", None):
            fn(job_id)
        else:
            fn(job_id, enabled=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": job_id, "paused": False}


@router.post("/cron/jobs/{job_id}/trigger")
async def trigger_job(job_id: str) -> dict:
    s = _store()
    fn = getattr(s, "run_job", None) or getattr(s, "trigger_job", None)
    if fn is None:
        raise HTTPException(status_code=503, detail="cron trigger unavailable")
    try:
        fn(job_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": job_id}


@router.delete("/cron/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    s = _store()
    fn = getattr(s, "delete_job", None) or getattr(s, "remove_job", None)
    if fn is None:
        raise HTTPException(status_code=503, detail="cron delete unavailable")
    try:
        result = fn(job_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    if result is False:
        raise HTTPException(status_code=404, detail="job not found")
    audit_log("cron.delete", id=job_id)
    from fastapi import Response

    return Response(status_code=204)
