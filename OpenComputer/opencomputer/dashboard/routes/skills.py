"""GET/PUT /api/v1/skills/* — list + toggle. Populated in PR3."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["skills"])
