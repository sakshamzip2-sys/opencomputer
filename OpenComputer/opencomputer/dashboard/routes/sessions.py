"""GET/DELETE /api/v1/sessions/* — read-mostly session surface.

Wraps SessionDB. Routes:
- GET    /api/v1/sessions               — list summary (paginated)
- GET    /api/v1/sessions/search        — FTS5 search across messages
- GET    /api/v1/sessions/{id}          — single session metadata
- GET    /api/v1/sessions/{id}/messages — messages (paginated client-side)
- DELETE /api/v1/sessions/{id}          — delete (consent-gated; loopback-public)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from opencomputer.dashboard.routes._common import clamp_limit, get_session_db

router = APIRouter(prefix="/api/v1", tags=["sessions"])


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    channel: str | None = Query(None, description="Filter by platform/channel name."),
) -> dict:
    """List recent sessions, newest-first. Optional `channel` filters by `platform`."""
    with get_session_db() as db:
        rows = db.list_sessions(limit=clamp_limit(limit))
    if channel:
        rows = [r for r in rows if r.get("platform") == channel]
    return {"items": rows, "limit": clamp_limit(limit)}


@router.get("/sessions/search")
async def search_sessions(
    q: str = Query(..., min_length=1, description="FTS5 search query."),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """FTS5 search across message contents."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="empty query")
    with get_session_db() as db:
        rows = db.search_messages(q.strip(), limit=clamp_limit(limit))
    return {"items": rows, "limit": clamp_limit(limit), "query": q}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    with get_session_db() as db:
        row = db.get_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    return row


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    """Return messages for a session, paginated.

    Reads the messages table directly so we can include timestamps
    (which `SessionDB.get_messages()` strips when reconstructing Message
    dataclasses). Mirrors the read-only direct-query pattern already used
    by /api/llm-calls/recent.
    """
    capped = clamp_limit(limit, default=200, maximum=500)
    with get_session_db() as db:
        if not db.get_session(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        with db._connect() as conn:  # noqa: SLF001 — internal helper, dashboard read-only
            total_row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            total = int(total_row["c"]) if total_row else 0
            rows = conn.execute(
                "SELECT id, role, content, tool_call_id, tool_calls, name, "
                "timestamp FROM messages WHERE session_id = ? "
                "ORDER BY id LIMIT ? OFFSET ?",
                (session_id, capped, offset),
            ).fetchall()
    items = [
        {
            "seq": offset + i,
            "id": int(r["id"]),
            "role": r["role"],
            "content": r["content"],
            "tool_call_id": r["tool_call_id"],
            "tool_calls": r["tool_calls"],  # JSON string or None — client parses
            "name": r["name"],
            "timestamp": float(r["timestamp"]) if r["timestamp"] is not None else None,
        }
        for i, r in enumerate(rows)
    ]
    return {"items": items, "limit": capped, "offset": offset, "total": total}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str) -> Response:
    """Delete a session and its messages. 404 if not found."""
    with get_session_db() as db:
        existed = db.delete_session(session_id)
    if not existed:
        raise HTTPException(status_code=404, detail="session not found")
    return Response(status_code=204)


def _message_to_dict(msg: object, seq: int) -> dict:
    """Coerce a plugin_sdk.core.Message into a wire-friendly dict.

    Uses getattr because Message is a frozen dataclass and we want the
    JSON shape to match the dashboard's TypeScript MessageRow contract
    even if Message gains fields later.
    """
    return {
        "seq": seq,
        "role": getattr(msg, "role", ""),
        "content": getattr(msg, "content", ""),
        "tool_calls": getattr(msg, "tool_calls", None),
        "tool_call_id": getattr(msg, "tool_call_id", None),
        "name": getattr(msg, "name", None),
        "timestamp": getattr(msg, "timestamp", None),
    }
