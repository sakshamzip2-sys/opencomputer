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

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

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
# Session mutations: Delete / Rename / New / Fork buttons in workspace UI.
# All Bearer-gated (mutate profile state). 404 when target session missing.
# ---------------------------------------------------------------------------


class CreateSessionBody(BaseModel):
    """Workspace's createSession() payload — fields are all optional."""

    id: str | None = None
    title: str | None = None
    model: str | None = None


class UpdateSessionBody(BaseModel):
    """Workspace's updateSession() payload — currently just title rename."""

    title: str | None = None


@router.post("/sessions")
async def create_session(
    body: CreateSessionBody,
    _: None = Depends(require_session_token),
) -> dict[str, Any]:
    """Create a fresh empty session. Hermes shape: ``{session: {...}}``.

    Bearer-gated: creating sessions writes to the profile DB. We generate a
    fresh UUID4 hex if the caller didn't pass an explicit id; if they did,
    we reject conflicts with 409 rather than silently overwriting.
    """
    import uuid as _uuid

    from opencomputer.dashboard.routes._common import get_session_db

    sid = (body.id or "").strip() or _uuid.uuid4().hex
    title = (body.title or "").strip() or None
    try:
        with get_session_db() as db:
            if body.id and db.get_session(sid):
                raise HTTPException(
                    status_code=409,
                    detail=f"session id {sid!r} already exists",
                )
            db.create_session(
                session_id=sid,
                platform="webui",
                title=title,
            )
            row = db.get_session(sid) or {"id": sid, "title": title}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: create_session failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"create session failed: {exc}",
        ) from exc
    return {"session": row}


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    body: UpdateSessionBody,
    _: None = Depends(require_session_token),
) -> dict[str, Any]:
    """Rename a session (title-only in v1). Returns ``{session: ...}``."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise HTTPException(status_code=400, detail="empty session id")
    new_title = (body.title or "").strip()
    if not new_title:
        raise HTTPException(
            status_code=400,
            detail="title is required and must be non-empty",
        )

    from opencomputer.dashboard.routes._common import get_session_db

    try:
        with get_session_db() as db:
            if not db.get_session(session_id):
                raise HTTPException(status_code=404, detail="session not found")
            with db._connect() as conn:  # noqa: SLF001
                conn.execute(
                    "UPDATE sessions SET title = ? WHERE id = ?",
                    (new_title, session_id),
                )
                conn.commit()
            row = db.get_session(session_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes_aliases: update_session failed: sid=%s exc=%s",
            session_id, exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"update session failed: {exc}",
        ) from exc
    return {"session": row}


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    response_class=Response,
)
async def delete_session(
    session_id: str,
    _: None = Depends(require_session_token),
) -> Response:
    """Delete a session and its messages. 404 if absent; 204 on success.

    Empty body on 204 — FastAPI requires ``response_class=Response`` to
    satisfy the "204 must not have a response body" assertion.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise HTTPException(status_code=400, detail="empty session id")
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    # OC's delete handler returns a Response(204); pass it through.
    return await _oc_sessions.delete_session(session_id)


@router.post("/sessions/{session_id}/fork")
async def fork_session(
    session_id: str,
    _: None = Depends(require_session_token),
) -> dict[str, Any]:
    """Clone a session's messages into a new session. Returns ``{session}``.

    Uses raw SQL inside the SessionDB connection so timestamps and ordering
    are preserved exactly. The new session id is a fresh UUID4 hex.
    """
    import uuid as _uuid

    from opencomputer.dashboard.routes._common import get_session_db

    if not isinstance(session_id, str) or not session_id.strip():
        raise HTTPException(status_code=400, detail="empty session id")

    new_id = _uuid.uuid4().hex
    try:
        with get_session_db() as db:
            src = db.get_session(session_id)
            if not src:
                raise HTTPException(
                    status_code=404,
                    detail="source session not found",
                )
            src_title = (src.get("title") or "").strip()
            new_title = (
                f"{src_title} (fork)" if src_title else f"Fork of {session_id[:8]}"
            )
            db.create_session(
                session_id=new_id,
                platform=src.get("platform") or "webui",
                title=new_title,
            )
            with db._connect() as conn:  # noqa: SLF001
                rows = conn.execute(
                    "SELECT role, content, tool_call_id, tool_calls, "
                    "name, timestamp FROM messages WHERE session_id = ? "
                    "ORDER BY id",
                    (session_id,),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "INSERT INTO messages (session_id, role, content, "
                        "tool_call_id, tool_calls, name, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_id,
                            r["role"],
                            r["content"],
                            r["tool_call_id"],
                            r["tool_calls"],
                            r["name"],
                            r["timestamp"],
                        ),
                    )
                conn.commit()
            new_row = db.get_session(new_id) or {"id": new_id, "title": new_title}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes_aliases: fork_session failed: sid=%s exc=%s",
            session_id, exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"fork session failed: {exc}",
        ) from exc
    return {"session": new_row}


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


@router.get("/skills/{skill_name:path}")
async def get_skill(skill_name: str) -> dict[str, Any]:
    """Return a single skill's metadata by name. Hermes shape: ``{skill}``.

    Path is matched with ``:path`` so skills with slashes in their name
    (plugin-namespaced ``plugin:skill``) resolve correctly.
    """
    decoded = (skill_name or "").strip()
    if not decoded:
        raise HTTPException(status_code=400, detail="empty skill name")
    try:
        from opencomputer.dashboard.routes import skills as _skills

        oc_payload = await _skills.list_skills()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: skill detail lookup failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"skills lookup failed: {exc}",
        ) from exc
    items = oc_payload.get("skills") or oc_payload.get("items") or []
    for s in items:
        if isinstance(s, dict) and s.get("name") == decoded:
            return {"skill": s}
    raise HTTPException(status_code=404, detail=f"skill {decoded!r} not found")


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
# /api/memory — surface MEMORY.md / USER.md / SOUL.md so workspace's
# Memory tab can browse + display them.
# ---------------------------------------------------------------------------


@router.get("/memory")
async def get_memory() -> dict[str, Any]:
    """Return the active profile's memory documents.

    Hermes shape (matching the workspace's Memory tab expectation):

        {
            "memory_md": "...",   # MEMORY.md content
            "user_md": "...",     # USER.md content
            "soul_md": "...",     # SOUL.md content (optional)
            "status": {           # plus the existing /api/v1/memory/status
                "memory_md": {...},
                "user_md": {...},
                ...
            }
        }

    Public — no Bearer gate. The files are local to the profile dir;
    workspace's UI shows them read-only by default and any future
    edit-write surface should land as a separate PATCH endpoint with
    auth gating.
    """
    try:
        from opencomputer.agent.config import _home as _profile_home_fn
        from opencomputer.dashboard.routes import memory as _memory

        status = await _memory.memory_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: memory status lookup failed: %s", exc)
        status = {"error": str(exc)}

    payload: dict[str, Any] = {"status": status}

    try:
        profile_home = _profile_home_fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: profile home lookup failed: %s", exc)
        return {**payload, "memory_md": "", "user_md": "", "soul_md": "", "error": str(exc)}

    for key, fname in (
        ("memory_md", "MEMORY.md"),
        ("user_md", "USER.md"),
        ("soul_md", "SOUL.md"),
    ):
        p = profile_home / fname
        try:
            payload[key] = p.read_text(encoding="utf-8") if p.is_file() else ""
        except OSError as exc:
            logger.warning(
                "hermes_aliases: memory read failed for %s: %s", p, exc,
            )
            payload[key] = ""
    return payload


# ---------------------------------------------------------------------------
# /api/config — mirror of OC's /api/v1/config (GET + PATCH)
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_config(_: None = Depends(require_session_token)) -> dict[str, Any]:
    """Return the active OC config.

    Bearer-gated because config can include sensitive paths and selected
    provider state. Workspace's config tab shows the raw blob for
    inspection; we forward what OC's own ``/api/v1/config`` handler
    returns unmodified.
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


@router.patch("/config")
async def patch_config(
    body: dict[str, Any],
    _: None = Depends(require_session_token),
) -> dict[str, Any]:
    """Deep-merge ``body`` into the active config and persist.

    Bearer-gated. Empty body is rejected (400). Unknown keys are accepted
    but only those that match OC's config schema take effect — OC's
    ``merge_put_config`` normalizes and validates them.
    """
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not body:
        raise HTTPException(status_code=400, detail="empty config patch")
    try:
        from opencomputer.dashboard.routes import config as _config

        return await _config.merge_put_config(body)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: config patch failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"config patch failed: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# /api/sessions/{id}/chat — non-streaming session-bound chat (workspace's
# fallback when streaming isn't engaged). Delegates to /v1/chat/completions
# under the hood so the same AgentLoop path drives every request.
# ---------------------------------------------------------------------------


class SessionChatBody(BaseModel):
    """Workspace's session-chat payload — flexible to fit two shapes
    upstream uses interchangeably."""

    message: str | None = None
    messages: list[dict[str, Any]] | None = None
    model: str | None = None
    stream: bool = False


@router.post("/sessions/{session_id}/chat")
async def session_chat(
    session_id: str,
    body: SessionChatBody,
    _: None = Depends(require_session_token),
) -> dict[str, Any]:
    """Run one assistant turn on a session and return the assistant text.

    We delegate to OC's OpenAI-compat handler so the same AgentLoop
    construction + provider routing + tool dispatch applies. The
    request's ``message`` (or last entry of ``messages``) seeds the
    next turn; prior session history is loaded by AgentLoop from
    SessionDB via the ``oc_session_id`` extension field.
    """
    if not session_id or not session_id.strip():
        raise HTTPException(status_code=400, detail="empty session id")

    text = (body.message or "").strip()
    if not text and body.messages:
        last = body.messages[-1] if body.messages else None
        if isinstance(last, dict):
            content = last.get("content")
            if isinstance(content, str):
                text = content.strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail="message text is required (set `message` or send a non-empty `messages`)",
        )

    # Resolve model: explicit > session metadata > profile default.
    model = (body.model or "").strip()
    if not model:
        try:
            from opencomputer.agent.config import default_config

            cfg = default_config()
            model = getattr(getattr(cfg, "model", None), "model", "") or ""
        except Exception:  # noqa: BLE001
            model = ""
    if not model:
        raise HTTPException(
            status_code=400,
            detail="no model resolvable — pass `model` explicitly",
        )

    try:
        from opencomputer.dashboard.routes import openai_compat as _oai

        final_text = await _oai._run_agent_completion(  # noqa: SLF001
            user_message=text,
            history=[],
            system_prompt=None,
            model=model,
            oc_session_id=session_id,
            stream_callback=None,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes_aliases: session_chat failed sid=%s model=%s exc=%s",
            session_id, model, exc,
        )
        raise HTTPException(
            status_code=500,
            detail=f"session chat failed: {exc}",
        ) from exc

    return {
        "session_id": session_id,
        "model": model,
        "message": {"role": "assistant", "content": final_text},
    }


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
