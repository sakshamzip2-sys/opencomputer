"""GET /api/v1/analytics/* — usage analytics. Populated in PR6."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["analytics"])
