"""Models-analytics dashboard plugin — backend API routes (Wave 6.D + 6.D-α).

Mounted at ``/api/plugins/models/``. Combines the read endpoints
(``/usage``, ``/health``) with the Wave 6.D-α mutation endpoints
``POST /main`` and ``POST /auxiliary`` that update the active profile's
``config.yaml`` ``model.model`` (main) and ``model.cheap_model``
(auxiliary) fields.

Hermes ref: ``e6b05eaf6 feat: add Models dashboard tab with rich
per-model analytics`` + ``3c27efbb9 feat(dashboard): configure main +
auxiliary models from Models page``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from opencomputer.agent.profile_yaml import modify_yaml_locked
from opencomputer.dashboard._auth import require_session_token

log = logging.getLogger(__name__)

router = APIRouter()


class _SetModelBody(BaseModel):
    model: str


def _session_db_path() -> str:
    """Where the active profile's sessions.db lives."""
    from opencomputer.agent.config import _home

    return str(_home() / "sessions.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_session_db_path())
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/usage")
async def model_usage(
    days: int = Query(default=30, ge=1, le=365, description="window in days"),
) -> dict[str, Any]:
    """Return per-model usage stats for the trailing ``days`` window.

    Response shape::

        {
          "since_ts": 1714867200.0,
          "models": [
            {
              "model": "claude-opus-4-7",
              "session_count": 12,
              "input_tokens": 124500,
              "output_tokens": 38000,
              "cache_read_tokens": 9100,
              "cache_write_tokens": 1500,
              "tool_calls": 87,
              "tool_errors": 3,
              "tool_duration_ms_p50": 412.0,
              "tool_duration_ms_p95": 2103.0,
              "last_used_at": 1714953600.0
            },
            ...
          ]
        }
    """
    since = time.time() - (days * 86400)

    try:
        conn = _connect()
    except sqlite3.OperationalError as exc:
        log.warning("models dashboard could not open sessions.db: %s", exc)
        return {"since_ts": since, "models": []}

    try:
        # Sessions aggregate — token totals + last-used. Treat a missing
        # ``sessions`` table as "no rows yet" rather than 500 — the user
        # may be opening the dashboard on a brand-new install.
        try:
            sess_rows = conn.execute(
                """
                SELECT model,
                       COUNT(*)                       AS session_count,
                       COALESCE(SUM(input_tokens),0)  AS input_tokens,
                       COALESCE(SUM(output_tokens),0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens),0)  AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,
                       MAX(COALESCE(ended_at, started_at)) AS last_used_at
                FROM sessions
                WHERE started_at >= ?
                  AND model IS NOT NULL
                  AND model != ''
                GROUP BY model
                """,
                (since,),
            ).fetchall()
        except sqlite3.OperationalError:
            sess_rows = []

        # tool_usage may not exist on older DBs; treat as empty.
        try:
            tool_rows = conn.execute(
                """
                SELECT model,
                       COUNT(*) AS tool_calls,
                       SUM(error) AS tool_errors
                FROM tool_usage
                WHERE ts >= ? AND model IS NOT NULL AND model != ''
                GROUP BY model
                """,
                (since,),
            ).fetchall()
            durations: dict[str, list[float]] = {}
            for r in conn.execute(
                """
                SELECT model, duration_ms
                FROM tool_usage
                WHERE ts >= ? AND model IS NOT NULL AND duration_ms IS NOT NULL
                """,
                (since,),
            ):
                durations.setdefault(r["model"], []).append(float(r["duration_ms"]))
        except sqlite3.OperationalError:
            tool_rows = []
            durations = {}
    finally:
        conn.close()

    tool_by_model: dict[str, dict[str, int]] = {
        row["model"]: {
            "tool_calls": int(row["tool_calls"] or 0),
            "tool_errors": int(row["tool_errors"] or 0),
        }
        for row in tool_rows
    }

    out: list[dict[str, Any]] = []
    for r in sess_rows:
        model = r["model"]
        durs = sorted(durations.get(model, []))
        p50 = durs[len(durs) // 2] if durs else None
        p95 = durs[int(len(durs) * 0.95)] if durs else None
        tu = tool_by_model.get(model, {"tool_calls": 0, "tool_errors": 0})
        out.append({
            "model": model,
            "session_count": int(r["session_count"]),
            "input_tokens": int(r["input_tokens"]),
            "output_tokens": int(r["output_tokens"]),
            "cache_read_tokens": int(r["cache_read_tokens"]),
            "cache_write_tokens": int(r["cache_write_tokens"]),
            "tool_calls": tu["tool_calls"],
            "tool_errors": tu["tool_errors"],
            "tool_duration_ms_p50": p50,
            "tool_duration_ms_p95": p95,
            "last_used_at": r["last_used_at"],
        })

    out.sort(
        key=lambda x: (x["session_count"], x["input_tokens"] + x["output_tokens"]),
        reverse=True,
    )
    return {"since_ts": since, "models": out}


# ---------------------------------------------------------------------------
# Wave 6.D-α — mutation endpoints (token-gated). Both write to
# ~/.opencomputer/<profile>/config.yaml under model.* keys, mirroring the
# load_config + save_config layout. Atomic + filelock-guarded.
# ---------------------------------------------------------------------------


def _config_yaml_path():
    from opencomputer.agent.config import _home

    return _home() / "config.yaml"


@router.post("/main", dependencies=[Depends(require_session_token)])
async def set_main_model(body: _SetModelBody) -> dict[str, Any]:
    """Set the main model in the active profile's config.yaml.

    Equivalent to editing ``model.model`` and re-running ``oc``. The
    next session reads the new value via ``load_config()``; running
    sessions are unaffected (they hold their own copy).
    """
    path = _config_yaml_path()

    def _mutate(data: dict[str, Any]) -> None:
        data.setdefault("model", {})["model"] = body.model

    modify_yaml_locked(path, _mutate)
    return {"ok": True, "model": body.model, "field": "model.model"}


@router.post("/auxiliary", dependencies=[Depends(require_session_token)])
async def set_auxiliary_model(body: _SetModelBody) -> dict[str, Any]:
    """Set the auxiliary (cheap-route) model in config.yaml.

    Writes to ``model.cheap_model``. ``ModelConfig.cheap_model`` is the
    model used for short/simple prompts via the cheap-route — passing
    an empty string here disables cheap-route until next change.
    """
    path = _config_yaml_path()

    def _mutate(data: dict[str, Any]) -> None:
        data.setdefault("model", {})["cheap_model"] = body.model or None

    modify_yaml_locked(path, _mutate)
    return {"ok": True, "model": body.model, "field": "model.cheap_model"}


@router.get("/health")
async def health() -> dict[str, Any]:
    """Quick status — sessions.db reachable + has any rows."""
    try:
        conn = _connect()
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE model IS NOT NULL"
        ).fetchone()[0]
    except sqlite3.OperationalError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()
    return {"ok": True, "sessions_with_model": int(n)}
