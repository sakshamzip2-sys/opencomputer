"""GET/POST /api/v1/plugins/* — list + enable/disable + install. Populated in PR3."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["plugins"])
