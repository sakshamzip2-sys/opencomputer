"""GET /api/v1/status — SPA's first call on mount.

Returns the active profile name, the wire-server URL the SPA should
connect to for live chat, and the OC version string. Loopback-public
(no token check) so the SPA can render the StatusBar before the user
has done anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1", tags=["status"])


@router.get("/status")
async def status(request: Request) -> dict:
    from opencomputer import __version__ as oc_version
    from opencomputer.agent.config import default_config

    cfg = default_config()
    profile_name = getattr(cfg, "profile_name", None) or "default"
    return {
        "profile": profile_name,
        "wire_url": getattr(request.app.state, "wire_url", ""),
        "version": oc_version,
    }
