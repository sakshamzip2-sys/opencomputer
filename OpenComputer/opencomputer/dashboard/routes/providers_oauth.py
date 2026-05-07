"""POST/GET/DELETE /api/v1/providers/oauth/* — OAuth flows. Populated in PR4."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["providers-oauth"])
