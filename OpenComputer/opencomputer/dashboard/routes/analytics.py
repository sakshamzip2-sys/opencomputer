"""GET /api/v1/analytics/* — usage analytics from sessions.db.

Reads CostGuard / llm_calls + tool_usage tables. Returns aggregate dicts
suitable for plotting client-side via @observablehq/plot.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/v1", tags=["analytics"])


def _db():
    from opencomputer.agent.config import default_config
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    p = cfg.home / "sessions.db"
    if not p.exists():
        raise HTTPException(status_code=503, detail="sessions.db not initialized")
    return SessionDB(p)


@router.get("/analytics/usage")
async def usage(days: int = Query(30, ge=1, le=365)) -> dict:
    """Token + cost usage by day."""
    db = _db()
    since = time.time() - days * 86400
    try:
        with db._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                """
                SELECT
                  CAST(ts AS INTEGER) / 86400 AS day_bucket,
                  SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cost_usd) AS cost_usd,
                  COUNT(*) AS calls
                FROM llm_calls
                WHERE ts >= ?
                GROUP BY day_bucket
                ORDER BY day_bucket DESC
                """,
                (since,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"analytics unavailable: {exc}")
    return {
        "items": [
            {
                "day": int(r["day_bucket"]) * 86400,
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cost_usd": float(r["cost_usd"] or 0.0),
                "calls": int(r["calls"]),
            }
            for r in rows
        ],
        "days": days,
    }


@router.get("/analytics/models")
async def models_usage(days: int = Query(30, ge=1, le=365)) -> dict:
    """Per-model token + cost breakdown."""
    db = _db()
    since = time.time() - days * 86400
    try:
        with db._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                """
                SELECT
                  provider, model,
                  SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cost_usd) AS cost_usd,
                  COUNT(*) AS calls
                FROM llm_calls
                WHERE ts >= ?
                GROUP BY provider, model
                ORDER BY cost_usd DESC
                """,
                (since,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"analytics unavailable: {exc}")
    return {
        "items": [
            {
                "provider": r["provider"],
                "model": r["model"],
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cost_usd": float(r["cost_usd"] or 0.0),
                "calls": int(r["calls"]),
            }
            for r in rows
        ],
        "days": days,
    }


@router.get("/analytics/tools")
async def tools_usage(days: int = Query(30, ge=1, le=365)) -> dict:
    """Per-tool call count + duration / error breakdown.

    Reads from tool_usage if the table exists (schema v5+); falls back to
    empty list otherwise.
    """
    db = _db()
    since = time.time() - days * 86400
    try:
        with db._connect() as conn:  # noqa: SLF001
            rows = conn.execute(
                """
                SELECT
                  tool,
                  COUNT(*) AS calls,
                  SUM(CASE WHEN error THEN 1 ELSE 0 END) AS errors,
                  AVG(duration_ms) AS avg_duration_ms
                FROM tool_usage
                WHERE ts >= ?
                GROUP BY tool
                ORDER BY calls DESC
                """,
                (since,),
            ).fetchall()
    except Exception:
        return {"items": [], "days": days, "note": "tool_usage table not present"}
    return {
        "items": [
            {
                "tool": r["tool"],
                "calls": int(r["calls"]),
                "errors": int(r["errors"] or 0),
                "avg_duration_ms": float(r["avg_duration_ms"] or 0.0),
            }
            for r in rows
        ],
        "days": days,
    }
