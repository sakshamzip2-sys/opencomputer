"""GET/PUT /api/v1/dashboard/* — themes + dashboard plugin meta. Populated in PR7."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["dashboard-meta"])
