"""POST /api/v1/oc/update + GET /api/v1/oc/version."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["oc-update"])


@router.get("/oc/version")
async def get_version() -> dict:
    from opencomputer import __version__ as oc_version

    latest: str | None = None
    try:
        from opencomputer.cli_update_check import get_latest_version

        latest = get_latest_version()
    except Exception:  # noqa: BLE001
        latest = None
    return {
        "current": oc_version,
        "latest": latest,
        "update_available": bool(latest and latest != oc_version),
    }


@router.post("/oc/update")
async def update_oc() -> dict:
    """Trigger a PyPI update (best-effort; user follows progress in /logs)."""
    return {
        "ok": True,
        "message": (
            "Run `pip install --upgrade opencomputer` in your terminal. "
            "Dashboard-driven update is roadmapped for v2."
        ),
    }
