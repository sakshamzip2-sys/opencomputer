"""GET/POST/PUT/DELETE /api/v1/cron/jobs/* — cron CRUD. Populated in PR4."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["cron"])
