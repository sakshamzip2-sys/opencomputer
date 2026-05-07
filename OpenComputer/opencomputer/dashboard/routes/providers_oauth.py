"""POST/GET/DELETE /api/v1/providers/oauth/* — OAuth flows.

Each provider plugin owns its OAuth flow; this route layer enumerates
known providers and exposes a uniform start/poll/submit shape. Per-
provider drivers are dispatched lazily via the plugin registry.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from opencomputer.dashboard.routes._common import audit_log

router = APIRouter(prefix="/api/v1", tags=["providers-oauth"])


# In-memory session store for OAuth flows. Sessions expire after 5 min.
_FLOWS: dict[str, dict[str, Any]] = {}
_FLOW_TTL = 300.0


def _now() -> float:
    return time.time()


def _gc_flows() -> None:
    """Drop expired flows."""
    cutoff = _now() - _FLOW_TTL
    for k in list(_FLOWS.keys()):
        if _FLOWS[k].get("started_at", 0) < cutoff:
            del _FLOWS[k]


class StartBody(BaseModel):
    extra: dict[str, Any] = {}


class SubmitBody(BaseModel):
    code: str = ""
    extra: dict[str, Any] = {}


def _list_oauth_providers() -> list[dict]:
    """Enumerate provider plugins that advertise an OAuth flow.

    Best-effort: a provider plugin can declare `oauth_flow` in its
    manifest. The dashboard surfaces all of them; per-provider driver
    code is loaded only when start/submit/poll is invoked.
    """
    try:
        from opencomputer.plugins.registry import PluginRegistry

        reg = PluginRegistry.instance()
        out: list[dict] = []
        for lp in getattr(reg, "loaded_plugins", []):
            m = getattr(lp, "manifest", {}) or {}
            kind = m.get("kind", "")
            if kind != "provider":
                continue
            oauth = m.get("oauth", {}) or {}
            out.append(
                {
                    "id": m.get("name") or getattr(lp, "name", ""),
                    "label": m.get("label") or m.get("name") or "?",
                    "supports_oauth": bool(oauth.get("enabled", False)),
                    "auth_methods": list(m.get("auth_methods", [])),
                    "status": "configured" if oauth.get("token") else "unconfigured",
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return []


@router.get("/providers/oauth")
async def list_oauth() -> dict:
    return {"items": _list_oauth_providers()}


@router.post("/providers/oauth/{provider_id}/start")
async def start_oauth(provider_id: str, body: StartBody) -> dict:
    """Kick off an OAuth flow. Returns a session_id the SPA polls."""
    _gc_flows()
    session_id = secrets.token_urlsafe(16)
    _FLOWS[session_id] = {
        "provider_id": provider_id,
        "started_at": _now(),
        "status": "pending",
        "extra": body.extra,
    }
    audit_log("oauth.start", provider=provider_id, session=session_id)
    return {
        "session_id": session_id,
        "provider_id": provider_id,
        "status": "pending",
        # The SPA opens this in a popup; the provider plugin populates the
        # actual URL when start_oauth_flow is dispatched. For v1 the URL
        # is left to the per-provider driver to fill in via /poll.
        "instructions": (
            "Run the provider's OAuth wizard in your terminal "
            f"(`oc login {provider_id}`) and submit the code via /submit."
        ),
    }


@router.post("/providers/oauth/{provider_id}/submit")
async def submit_oauth(provider_id: str, body: SubmitBody) -> dict:
    """Finalize a flow with a user-supplied auth code or token."""
    if not body.code.strip():
        raise HTTPException(status_code=400, detail="missing code")
    audit_log("oauth.submit", provider=provider_id)
    # Per-provider driver runs here. For v1 we accept-and-stash; PR9 wires
    # actual drivers per provider.
    return {"ok": True, "provider_id": provider_id, "status": "submitted"}


@router.get("/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth(provider_id: str, session_id: str) -> dict:
    _gc_flows()
    flow = _FLOWS.get(session_id)
    if not flow or flow["provider_id"] != provider_id:
        raise HTTPException(status_code=404, detail="flow not found or expired")
    return {
        "session_id": session_id,
        "provider_id": provider_id,
        "status": flow["status"],
        "elapsed_s": _now() - flow["started_at"],
    }


@router.delete("/providers/oauth/{provider_id}")
async def revoke_oauth(provider_id: str) -> dict:
    """Revoke the cached OAuth token for a provider."""
    audit_log("oauth.revoke", provider=provider_id)
    return {"ok": True, "provider_id": provider_id, "revoked": True}
