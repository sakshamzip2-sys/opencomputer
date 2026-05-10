"""GET /api/v1/actions/{name}/status — long-running action poll.

Each action that runs in the background (oc/update, plugin/install)
posts progress here. v1 ships a no-op success — actual long-running
infra lands in PR7.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["actions"])


@router.get("/actions/{name}/status")
async def action_status(name: str) -> dict:
    return {"name": name, "status": "idle", "message": "no active action"}
