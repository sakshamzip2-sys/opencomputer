"""v0.5 Item: tool-risk dashboard.

Read-only computation of per-tool risk signals from existing
``tool_usage`` + ``turn_outcomes`` data. Surfaces:

  - error_rate: fraction of calls that errored (from tool_usage.error)
  - self_cancel_rate: fraction of calls associated with a turn that
    had self_cancel_count > 0
  - mean_duration_ms

This is read-only / advisory. Auto-tightening tool consent based on
risk is explicitly out of scope (UX-sensitive). Users see the
dashboard via ``oc policy tool-risk`` and decide for themselves.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolRisk:
    tool: str
    n_calls: int
    error_rate: float
    self_cancel_rate: float
    mean_duration_ms: float
    days_window: int


def compute_tool_risk(db, *, days: int = 7) -> list[ToolRisk]:
    """Return per-tool risk stats for the last ``days``. Sorted by
    self_cancel_rate DESC then error_rate DESC."""
    cutoff = time.time() - days * 86400
    with db._connect() as conn:
        rows = conn.execute(
            """
            SELECT
              tu.tool AS tool,
              COUNT(*) AS n_calls,
              AVG(tu.error) AS error_rate,
              AVG(CASE
                  WHEN to_row.self_cancel_count > 0 THEN 1.0
                  ELSE 0.0
              END) AS self_cancel_rate,
              AVG(tu.duration_ms) AS mean_duration_ms
            FROM tool_usage tu
            LEFT JOIN turn_outcomes to_row
              ON to_row.session_id = tu.session_id
              AND ABS(to_row.created_at - tu.ts) <= 60.0
            WHERE tu.ts >= ?
            GROUP BY tu.tool
            ORDER BY self_cancel_rate DESC, error_rate DESC
            """,
            (cutoff,),
        ).fetchall()

    return [
        ToolRisk(
            tool=r["tool"],
            n_calls=int(r["n_calls"] or 0),
            error_rate=float(r["error_rate"] or 0.0),
            self_cancel_rate=float(r["self_cancel_rate"] or 0.0),
            mean_duration_ms=float(r["mean_duration_ms"] or 0.0),
            days_window=days,
        )
        for r in rows
    ]
