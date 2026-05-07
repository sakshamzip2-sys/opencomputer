"""GET/PUT/DELETE/POST /api/v1/env/* — env vars (consent-gated reveal). Populated in PR5."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["env"])
