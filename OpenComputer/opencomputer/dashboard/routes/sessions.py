"""GET/DELETE /api/v1/sessions/* — read-mostly session surface.

Wraps SessionDB. Populated in PR2 (Sessions+Logs+Models phase).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["sessions"])
