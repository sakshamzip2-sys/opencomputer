"""v0.5 Task C: read-only engine quality metrics.

For a given recommendation_engine_version (e.g. ``MostCitedBelowMedian/1``),
counts how many of its recommendations:
  - are still pending (pending_approval, pending_evaluation)
  - reached active state
  - decayed naturally (expired_decayed)
  - got reverted

The ``unrevert_rate`` = (active + expired_decayed) / (recommendations - pending)
is the headline quality signal: high values mean the engine is making
recommendations that hold up under scrutiny. Useful for:
  - Comparing two engine versions side-by-side (cohort A/B)
  - Deciding whether to promote a new engine from staging to default
  - Spotting an engine that's silently regressing
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EngineQuality:
    engine_version: str
    n_recommendations: int
    n_active: int
    n_expired_decayed: int
    n_reverted: int
    n_pending: int
    unrevert_rate: float
    revert_rate: float
    days_window: int


def compute_engine_quality(
    db, *, engine_version: str | None = None, days: int = 30,
) -> list[EngineQuality]:
    """Compute quality stats per engine_version.

    If ``engine_version`` is None, returns one row per distinct engine
    that produced recommendations in the window. Otherwise filtered.
    """
    cutoff = time.time() - days * 86400
    with db._connect() as conn:
        params: list = [cutoff]
        sql = (
            "SELECT recommendation_engine_version AS engine, status, "
            "COUNT(*) AS n FROM policy_changes WHERE ts_drafted >= ?"
        )
        if engine_version:
            sql += " AND recommendation_engine_version = ?"
            params.append(engine_version)
        sql += " GROUP BY engine, status"
        rows = conn.execute(sql, params).fetchall()

    by_engine: dict[str, dict[str, int]] = {}
    for r in rows:
        by_engine.setdefault(r["engine"], {})[r["status"]] = r["n"]

    out: list[EngineQuality] = []
    for ev, counts in sorted(by_engine.items()):
        active = counts.get("active", 0)
        expired = counts.get("expired_decayed", 0)
        reverted = counts.get("reverted", 0)
        pending = (
            counts.get("pending_approval", 0)
            + counts.get("pending_evaluation", 0)
            + counts.get("drafted", 0)
        )
        total = active + expired + reverted + pending
        evaluated = total - pending
        unrevert = (active + expired) / evaluated if evaluated else 0.0
        revert = reverted / evaluated if evaluated else 0.0
        out.append(EngineQuality(
            engine_version=ev,
            n_recommendations=total,
            n_active=active,
            n_expired_decayed=expired,
            n_reverted=reverted,
            n_pending=pending,
            unrevert_rate=unrevert,
            revert_rate=revert,
            days_window=days,
        ))
    return out
