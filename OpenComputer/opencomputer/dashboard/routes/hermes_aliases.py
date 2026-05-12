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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
    """List sessions in a UNION shape that satisfies both callers.

    Workspace has TWO listSessions paths:
    * Gateway path (``getCapabilities().dashboard.available === false``):
      reads ``resp.items``.
    * Dashboard path (``available === true``): reads ``resp.sessions``.

    OC always reports ``dashboard.available=true`` once ``/api/status``
    is wired, so the dashboard path activates and reading ``resp.items``
    no longer triggers. We return BOTH keys pointing at the same list
    so a probe or future workspace version that flips between the two
    paths keeps working without further OC changes. ``offset`` is also
    included in the response per the dashboard path's signature.
    """
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    # OC's /api/v1/sessions doesn't natively page by offset; it always
    # returns the newest N. To honour ``offset`` we ask for limit+offset
    # rows and slice client-side — fine at our scale (capped at 200).
    fetch = clamp_limit(limit) + offset
    raw = await _oc_sessions.list_sessions(limit=fetch, channel=channel)
    items = raw.get("items", []) or []
    paged = items[offset : offset + clamp_limit(limit)]
    return {
        "items": paged,
        "sessions": paged,
        "total": len(items),
        "limit": clamp_limit(limit),
        "offset": offset,
    }


@router.get("/sessions/search")
async def search_sessions(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Search sessions. Returns BOTH gateway-shape (``{items, count}``)
    AND dashboard-shape (``{results: [{session_id, snippet, role,
    source, model, session_started}]}``).

    OC's underlying search yields message rows; we re-shape into the
    dashboard's per-result entry while keeping the original ``items``
    array intact for the gateway-shape caller.
    """
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    raw = await _oc_sessions.search_sessions(q=q, limit=clamp_limit(limit))
    items = raw.get("items", []) or []
    # Re-shape into the dashboard-search result entries. Best-effort:
    # OC's message rows include session_id, role, content, timestamp;
    # source/model are looked up from the parent session if available
    # (cheap because clamp_limit caps at 200).
    results: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        results.append(
            {
                "session_id": row.get("session_id") or row.get("id") or "",
                "snippet": str(row.get("content") or row.get("snippet") or "")[:300],
                "role": row.get("role"),
                "source": row.get("source") or row.get("platform"),
                "model": row.get("model"),
                "session_started": row.get("started_at") or row.get("timestamp"),
            }
        )
    return {
        "query": q,
        "count": len(results),
        "results": results,
        "items": items,
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    """Return a session in a UNION shape.

    Gateway path expects ``{session: {...}}``; dashboard path expects
    the flat session object directly. We return the flat object PLUS
    a ``session`` key wrapping itself, so both callers work.
    """
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    row = await _oc_sessions.get_session(session_id)
    if not isinstance(row, dict):
        return {"session": row}
    # Flat fields + nested ``session`` mirror — both callers happy.
    return {**row, "session": row}


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Messages in a UNION shape.

    Gateway path expects ``{items, total, limit, offset}``.
    Dashboard path expects ``{messages, session_started, model}``.

    We return both — the same list under ``items`` AND ``messages``,
    plus ``session_started`` and ``model`` looked up from the session
    row for the dashboard path's UX.
    """
    from opencomputer.dashboard.routes import sessions as _oc_sessions

    payload = await _oc_sessions.get_messages(
        session_id, limit=limit, offset=offset,
    )
    items = payload.get("items", []) or []
    # Best-effort session metadata for the dashboard-shape consumer.
    started_at: Any = None
    model: Any = None
    try:
        sess = await _oc_sessions.get_session(session_id)
        if isinstance(sess, dict):
            started_at = sess.get("started_at")
            model = sess.get("model")
    except Exception:  # noqa: BLE001 — metadata is opportunistic, never break
        pass
    return {
        **payload,
        "messages": items,
        "session_started": started_at,
        "model": model,
    }


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


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    _: None = Depends(require_session_token),
) -> dict[str, Any]:
    """Delete a session and its messages. 404 if absent; 200 ``{ok: true}``
    on success.

    Workspace's dashboard-shape ``deleteSession`` parses the response
    body and expects ``{ok: boolean}``. A 204 with empty body would
    break ``dashboardJson()`` since it always tries to parse JSON.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise HTTPException(status_code=400, detail="empty session id")
    from opencomputer.dashboard.routes._common import get_session_db

    with get_session_db() as db:
        existed = db.delete_session(session_id)
    if not existed:
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "deleted": session_id}


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
    # Dashboard-shape ``forkSession`` expects ``{session, forked_from}``.
    return {"session": new_row, "forked_from": session_id}


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
    """Return the active OC config in hermes-agent-compatible shape.

    Bearer-gated because config can include sensitive paths and selected
    provider state.

    Critically: we always include a ``mcp_servers`` key (built from the
    enumerated MCP manager when available, empty array otherwise). The
    workspace's ``probeMcpConfigKey()`` checks for this exact key to flip
    the ``mcpFallback`` capability to "available" — without it, the
    Workspace MCP tab degrades to "Not Available" even when the rest of
    the MCP probe succeeds.
    """
    try:
        from opencomputer.dashboard.routes import config as _config

        oc_cfg = await _config.get_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes_aliases: config lookup failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"config unavailable: {exc}",
        ) from exc

    if not isinstance(oc_cfg, dict):
        oc_cfg = {"raw": oc_cfg}

    # Build the mcp_servers list defensively — failures here MUST NOT
    # break /api/config (the user's settings UI depends on it).
    mcp_servers: list[dict[str, Any]] = []
    try:
        mcp_payload = await list_mcp_servers()
        for entry in mcp_payload.get("servers") or []:
            if isinstance(entry, dict) and entry.get("name"):
                mcp_servers.append(
                    {
                        "name": str(entry["name"]),
                        "type": str(entry.get("type") or "stdio"),
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes_aliases: mcp_servers enumeration failed: %s", exc,
        )

    # Don't override an existing mcp_servers if OC's config ever surfaces
    # one — but if absent (today's case), we synthesise it.
    if "mcp_servers" not in oc_cfg:
        oc_cfg = {**oc_cfg, "mcp_servers": mcp_servers}
    return oc_cfg


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
# /api/status — hermes-agent dashboard liveness probe. Workspace's
# probeDashboard() hits this and looks for ``body.version``; success here
# is what flips workspace's ``dashboard`` capability to "available", which
# in turn unlocks ``mcpFallback`` and the Conductor/Kanban tabs.
# ---------------------------------------------------------------------------


@router.get("/status")
async def hermes_status() -> dict[str, Any]:
    """Dashboard liveness probe in hermes-agent shape.

    Workspace looks for a non-empty ``version`` field; provide OC's own
    version so a downstream consumer can tell apart different builds.
    """
    try:
        from opencomputer import __version__ as _version
    except Exception:  # noqa: BLE001
        _version = "unknown"
    return {
        "status": "ok",
        "version": _version,
        "service": "opencomputer-dashboard",
    }


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
# /api/sessions/{id}/chat/stream — SSE streaming session chat. Workspace's
# preferred path when ``enhancedChat`` capability is detected.
# ---------------------------------------------------------------------------


class SessionStreamBody(BaseModel):
    """Workspace's streamChat payload."""

    message: str | None = None
    model: str | None = None
    system_message: str | None = None
    attachments: list[dict[str, Any]] | None = None


def _hermes_sse_event(event_name: str, payload: dict[str, Any]) -> bytes:
    """Encode one hermes-shape SSE event (named event + JSON data)."""
    import json as _json

    return (
        f"event: {event_name}\n"
        f"data: {_json.dumps(payload, default=str)}\n\n"
    ).encode()


@router.post("/sessions/{session_id}/chat/stream")
async def session_chat_stream(
    session_id: str,
    body: SessionStreamBody,
    request: Request,
    _: None = Depends(require_session_token),
) -> Any:
    """Streaming session chat in hermes-agent SSE shape.

    Events emitted (one per ``event: <name>`` block):

    * ``message_start`` — once at stream open; ``{session_id, model}``
    * ``content_delta``  — per text-delta from AgentLoop; ``{text}``
    * ``message_complete`` — once after stream end; ``{text, stop_reason}``
    * ``error`` — emitted in-band if AgentLoop raises; ``{message}``
    * sentinel ``data: [DONE]`` line as the last frame

    The probe POSTs to ``/api/sessions/__probe__/chat/stream`` with body
    ``{}``; that triggers our validation 400 (no ``message``) which the
    workspace's probe treats as "available" (status is not 404/403/405).
    Real callers with valid bodies get the live stream.
    """
    import asyncio as _asyncio

    from fastapi.responses import StreamingResponse

    if not session_id or not session_id.strip():
        raise HTTPException(status_code=400, detail="empty session id")
    message = (body.message or "").strip()
    if not message:
        # Probe path: body is `{}`. Reject with 400 — workspace's probe
        # interprets that as "endpoint exists" (not 404/403/405) so the
        # `enhancedChat` capability flips to available. Real callers see
        # this as a useful validation error.
        raise HTTPException(
            status_code=400,
            detail="message text is required",
        )

    # Resolve model: explicit > profile default. (Unlike /v1/chat/completions
    # we don't accept `messages[]` here — the workspace's streamChat passes
    # exactly one message; prior history comes from SessionDB via
    # ``oc_session_id``.)
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

    pump: _asyncio.Queue[bytes | None] = _asyncio.Queue(maxsize=2048)

    def _on_delta(text: str) -> None:
        if not isinstance(text, str) or not text:
            return
        try:
            pump.put_nowait(_hermes_sse_event("content_delta", {"text": text}))
        except _asyncio.QueueFull:
            logger.warning(
                "hermes_aliases.session_chat_stream: SSE pump full; "
                "dropping delta",
            )

    async def _runner() -> str | BaseException:
        try:
            from opencomputer.dashboard.routes import openai_compat as _oai

            text = await _oai._run_agent_completion(  # noqa: SLF001
                user_message=message,
                history=[],
                system_prompt=(body.system_message or None),
                model=model,
                oc_session_id=session_id,
                stream_callback=_on_delta,
            )
            return text
        except BaseException as exc:  # noqa: BLE001
            return exc
        finally:
            await pump.put(None)  # sentinel

    async def _gen() -> Any:
        yield _hermes_sse_event(
            "message_start",
            {"session_id": session_id, "model": model},
        )
        task = _asyncio.create_task(_runner())
        try:
            while True:
                try:
                    if await request.is_disconnected():
                        break
                except Exception:  # noqa: BLE001
                    # request.is_disconnected can raise during teardown;
                    # treat as disconnected and exit cleanly.
                    break
                try:
                    item = await _asyncio.wait_for(pump.get(), timeout=15.0)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if item is None:
                    break
                yield item

            outcome = await task
            if isinstance(outcome, BaseException):
                yield _hermes_sse_event(
                    "error",
                    {"message": str(outcome) or outcome.__class__.__name__},
                )
                logger.error(
                    "hermes_aliases.session_chat_stream failed sid=%s exc=%s",
                    session_id, outcome,
                )
            else:
                yield _hermes_sse_event(
                    "message_complete",
                    {"text": outcome, "stop_reason": "stop"},
                )
        finally:
            yield b"data: [DONE]\n\n"
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001
                    pass

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
