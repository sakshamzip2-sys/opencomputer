"""GET/PUT /api/v1/config/* — config edit. Populated in PR5."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["config"])
