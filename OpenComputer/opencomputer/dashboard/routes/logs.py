"""GET /api/v1/logs — Server-Sent log feed. Populated in PR2."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["logs"])
