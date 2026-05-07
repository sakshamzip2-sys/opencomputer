"""GET/POST/DELETE /api/v1/profiles/* — profile management. Populated in PR3."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["profiles"])
