"""GET /api/v1/tools/* — toolset enumeration. Populated in PR3."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["tools"])
