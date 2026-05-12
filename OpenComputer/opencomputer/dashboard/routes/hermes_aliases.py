"""Hermes-shape aliases: ``/api/*`` routes mirroring ``/api/v1/*``.

Hermes-workspace queries paths under the ``/api/`` prefix (no ``v1``)
because that's where the upstream Hermes Agent dashboard exposes its
sessions/skills/jobs/config/mcp surface. OC exposes the same data under
``/api/v1/`` (the long-standing namespace for OC's own dashboard SPA).

This module bridges the gap by registering thin alias routes at the
``/api/*`` paths that the workspace's gateway-capabilities probe checks
AND that the workspace's UI fetches from when the user clicks the
Sessions / Skills / Jobs / MCP tabs.

Why not have both prefixes share one router? Because we want to keep the
two surfaces independent — a future change to OC's native shape under
``/api/v1/`` shouldn't accidentally break the workspace alias. So the
aliases delegate to the same underlying data layer (SessionDB,
SkillsHub, etc.) but build their responses in the Hermes-Agent shape
the workspace expects.

Probe-pass rules from the workspace's ``gateway-capabilities.ts``::

    status === 404 || status === 403  → "missing"
    anything else                     → "available"

So even a 401 (Bearer required) counts as "available", which keeps the
auth-gated routes from being mis-classified as missing.

Response shapes are documented per-route below. Where the workspace's
shape exactly matches OC's, we pass the data through unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from opencomputer.dashboard._auth import require_session_token
from opencomputer.dashboard.routes._common import clamp_limit

logger = logging.getLogger("opencomputer.dashboard.hermes_aliases")

router = APIRouter(prefix="/api", tags=["hermes-compat"])


# ---------------------------------------------------------------------------
# /api/sessions — delegates to OC's existing /api/v1/sessions handlers.
# We delegate (not re-implement) so any timestamp / count / shape edge
# the OC handler already gets right is shared. Then we re-shape the
# return value to match what the workspace expects.
# ---------------------------------------------------------------------------


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    channel: str | None = Query(None),
) -> dict[str, Any]:
    """List sessions in the hermes-agent shape: ``{items, total, limit}``."""
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    # OC's /api/v1/sessions doesn't natively page by offset; it always
    # returns the newest N. To honour ``offset`` we ask for limit+offset
    # rows and slice client-side — fine at our scale (capped at 200).
    fetch = clamp_limit(limit) + offset
    raw = await _oc_sessions.list_sessions(limit=fetch, channel=channel)
    items = raw.get("items", [])
    paged = items[offset : offset + clamp_limit(limit)]
    return {"items": paged, "total": len(items), "limit": clamp_limit(limit)}


@router.get("/sessions/search")
async def search_sessions(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Hermes shape: ``{query, count, results}``."""
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    raw = await _oc_sessions.search_sessions(q=q, limit=clamp_limit(limit))
    items = raw.get("items", [])
    return {"query": q, "count": len(items), "results": items}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    """Hermes shape: ``{session: {...}}``."""
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    row = await _oc_sessions.get_session(session_id)
    return {"session": row}


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Hermes shape mirrors OC's ``{items, limit, offset, total}``."""
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    return await _oc_sessions.get_messages(
        session_id, limit=limit, offset=offset,
    )


# ---------------------------------------------------------------------------
# /api/skills — mirror of OC's /api/v1/skills
# ---------------------------------------------------------------------------


@router.get("/skills")
async def list_skills() -> dict[str, Any]:
    """Return skills in hermes-agent shape.

    Workspace expects ``{skills: [{name, description, source, enabled,
    ...}, ...]}``. OC's CLI surface (``cli_skills_hub``) enumerates
    skills with a similar shape; we re-shape lightly.

    On failure: ``{skills: [], error: <str>}``. The workspace UI shows
    an empty list either way; the ``error`` key gives an operator
    looking at the response in DevTools a clear signal of WHY.
    """
    try:
        from opencomputer.dashboard.routes import skills as _skills

        oc_payload = await _skills.list_skills()
    except Exception as exc:  # noqa: BLE001 — surface every failure
        logger.warning("hermes_aliases: skills lookup failed: %s", exc)
        return {"skills": [], "error": str(exc)}
    items = oc_payload.get("skills") or oc_payload.get("items") or []
    return {"skills": items}


@router.get("/skills/categories")
async def list_skill_categories() -> dict[str, Any]:
    """Distinct ``category`` values across loaded skills."""
    try:
        from opencomputer.dashboard.routes import skills as _skills

        oc_payload = await _skills.list_skills()
    except Exception as exc:  # noqa: BLE001 — surface every failure
        logger.warning("hermes_aliases: skill categories lookup failed: %s", exc)
        return {"categories": [], "error": str(exc)}
    items = oc_payload.get("skills") or oc_payload.get("items") or []
    cats = sorted(
        {
            (s.get("category") or "").strip()
            for s in items
            if isinstance(s, dict)
        }
        - {""}
    )
    return {"categories": cats}


# ---------------------------------------------------------------------------
# /api/jobs — mirror of OC's /api/v1/cron/jobs (closest analogue)
# ---------------------------------------------------------------------------


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    """Return scheduled jobs in hermes-agent shape.

    Workspace's Jobs tab expects ``{jobs: [...]}``. OC's nearest
    analogue is the cron registry — that's what we return.

    On failure we surface ``{jobs: [], error: <str>}`` rather than
    masking the failure as empty data. The workspace UI renders the
    empty list either way; the ``error`` key is for an operator
    looking at the response in DevTools.
    """
    try:
        from opencomputer.dashboard.routes import cron as _cron

        oc_payload = await _cron.list_jobs()
    except Exception as exc:  # noqa: BLE001 — surface every failure
        logger.warning("hermes_aliases: cron jobs lookup failed: %s", exc)
        return {"jobs": [], "error": str(exc)}
    items = oc_payload.get("jobs") or oc_payload.get("items") or []
    return {"jobs": items}


# ---------------------------------------------------------------------------
# /api/config — mirror of OC's /api/v1/config
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_config(_: None = Depends(require_session_token)) -> dict[str, Any]:
    """Return the active OC config.

    Bearer-gated because config can include sensitive paths and
    selected provider state. Workspace's config tab shows the raw blob
    for inspection; we forward what OC's own ``/api/v1/config``
    handler returns.
    """
    try:
        from opencomputer.dashboard.routes import config as _config

        return await _config.get_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: config lookup failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"config unavailable: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# /api/mcp — mirror of OC's MCP-server inventory
# ---------------------------------------------------------------------------


@router.get("/mcp")
async def list_mcp_servers() -> dict[str, Any]:
    """Return configured MCP servers in hermes-agent shape.

    Workspace expects ``{servers: [{name, type, status, tools: [...]},
    ...]}``. OC tracks MCP servers via the MCP manager (when the
    ``mcp`` plugin/feature is active for the current profile). If
    the manager isn't importable or hasn't been initialised we return
    an empty list — workspace treats that as "no MCPs configured"
    rather than as an error.

    Defensive on every introspection: each server object can be a
    pydantic model, a dataclass, or a dict depending on the OC
    version. We coerce to a stable shape and skip malformed entries.

    On exception we still return 200 (probe-pass) but include an
    ``error`` field so an operator can see what broke.
    """
    try:
        from opencomputer.mcp.client import MCPManager
    except Exception as exc:  # noqa: BLE001
        logger.debug("hermes_aliases: MCP manager not importable: %s", exc)
        return {"servers": []}

    try:
        get_instance = getattr(MCPManager, "get_instance", None)
        manager = get_instance() if callable(get_instance) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: MCP manager init failed: %s", exc)
        return {"servers": [], "error": f"manager init failed: {exc}"}

    if manager is None:
        return {"servers": []}

    servers: list[dict[str, Any]] = []
    try:
        listed = getattr(manager, "list_servers", None)
        raw = listed() if callable(listed) else []
        for srv in raw or []:
            entry = _coerce_mcp_server(srv)
            if entry is not None:
                servers.append(entry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: MCP server enumeration failed: %s", exc)
        return {"servers": servers, "error": str(exc)}

    return {"servers": servers}


def _coerce_mcp_server(srv: Any) -> dict[str, Any] | None:
    """Normalize a server entry (pydantic / dataclass / dict / object).

    Returns ``None`` for objects we can't safely coerce — better than
    surfacing a garbled row to the workspace UI.
    """
    name: Any = None
    transport: Any = None
    if isinstance(srv, dict):
        name = srv.get("name")
        transport = srv.get("transport") or srv.get("type")
    else:
        name = getattr(srv, "name", None)
        transport = getattr(srv, "transport", None) or getattr(srv, "type", None)
    if not isinstance(name, str) or not name:
        return None
    return {
        "name": name,
        "type": str(transport or ""),
        "status": "configured",
        "tools": [],
    }


__all__ = ["router"]
