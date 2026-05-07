"""GET /api/v1/events — TypedEventBus → SSE multiplex. Populated in PR2."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["events"])
