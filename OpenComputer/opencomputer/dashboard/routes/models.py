"""GET/POST /api/v1/models/* — list providers, set default. Populated in PR2."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["models"])
