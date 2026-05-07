"""GET /api/v1/actions/{name}/status — long-running action poll. Populated in PR7."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["actions"])
