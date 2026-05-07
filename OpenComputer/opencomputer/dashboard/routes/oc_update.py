"""POST /api/v1/oc/update + GET /api/v1/oc/version. Populated in PR7."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["oc-update"])
