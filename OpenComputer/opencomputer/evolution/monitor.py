"""Evolution monitoring dashboard — aggregates reflection history,
synthesized-skills metrics, reward trends, and atrophy flags.

Design reference: OpenComputer/docs/evolution/design.md §11.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from opencomputer.evolution.storage import (
    _connect,
    apply_pending,
    evolution_home,
    list_reflections,
    list_skill_invocations,
    trajectory_db_path,
)

ATROPHY_DAYS_DEFAULT = 60


@dataclass(frozen=True, slots=True)
class SkillStatus:
    slug: str
    last_invoked_at: float | None
    invocation_count: int
    is_atrophied: bool


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    total_reflections: int
    last_reflection_at: float | None
    synthesized_skills: list[SkillStatus]
    atrophied_count: int
    avg_reward_last_30: float | None    # mean reward_score on records ended_at within 30 days
    avg_reward_lifetime: float | None


def _iter_reward_rows(
    window_start_ts: float | None,
    conn: sqlite3.Connection | None = None,
) -> list[float]:
    """Query reward_score values from trajectory_records.

    Uses option (b) from the spec: queries trajectory_records directly rather
    than extending TrajectoryRecord, keeping the dataclass shape stable for
    downstream consumers.

    Args:
        window_start_ts: If not None, only include records with ended_at >=
            window_start_ts. None means lifetime (no time filter).
        conn: Optional existing connection; opens one if None.

    Returns:
        List of reward_score floats (only non-NULL rows with non-NULL ended_at).
    """
    _own_conn = conn is None
    if _own_conn:
        conn = _connect(trajectory_db_path())
        apply_pending(conn)
    assert conn is not None
    try:
        if window_start_ts is None:
            rows = conn.execute(
                "SELECT reward_score FROM trajectory_records "
                "WHERE reward_score IS NOT NULL AND ended_at IS NOT NULL"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT reward_score FROM trajectory_records "
                "WHERE reward_score IS NOT NULL AND ended_at IS NOT NULL "
                "AND ended_at >= ?",
                (window_start_ts,),
            ).fetchall()
        return [float(r[0]) for r in rows]
    finally:
        if _own_conn:
            conn.close()


class MonitorDashboard:
    def __init__(self, *, atrophy_days: int = ATROPHY_DAYS_DEFAULT) -> None:
        self._atrophy_days = atrophy_days

    def snapshot(self, *, now: float | None = None) -> DashboardSnapshot:
        ts_now = now if now is not None else time.time()

        # Initialise DB so tables exist even on a fresh home
        conn = _connect(trajectory_db_path())
        apply_pending(conn)

        try:
            reflections = list_reflections(limit=10_000, conn=conn)
            total_refl = len(reflections)
            last_refl_at = reflections[0]["invoked_at"] if reflections else None  # newest-first

            skills = self._scan_skills(ts_now, conn=conn)
            atrophied = sum(1 for s in skills if s.is_atrophied)

            avg_30 = self._avg_reward_window(
                window_seconds=30 * 24 * 3600, now=ts_now, conn=conn
            )
            avg_life = self._avg_reward_window(window_seconds=None, now=ts_now, conn=conn)
        finally:
            conn.close()

        return DashboardSnapshot(
            total_reflections=total_refl,
            last_reflection_at=last_refl_at,
            synthesized_skills=skills,
            atrophied_count=atrophied,
            avg_reward_last_30=avg_30,
            avg_reward_lifetime=avg_life,
        )

    def _scan_skills(
        self, now: float, *, conn: sqlite3.Connection | None = None
    ) -> list[SkillStatus]:
        skills_dir = evolution_home() / "skills"
        if not skills_dir.exists():
            return []
        cutoff = now - (self._atrophy_days * 24 * 3600)
        out: list[SkillStatus] = []
        for child in sorted(skills_dir.iterdir()):
            if (
                not child.is_dir()
                or child.name.startswith(".")
                or not (child / "SKILL.md").exists()
            ):
                continue
            slug = child.name
            invocations = list_skill_invocations(slug=slug, conn=conn)
            count = len(invocations)
            last = invocations[0]["invoked_at"] if invocations else None  # newest-first per spec
            atrophied = (last is None) or (last < cutoff)
            out.append(SkillStatus(
                slug=slug,
                last_invoked_at=last,
                invocation_count=count,
                is_atrophied=atrophied,
            ))
        return out

    def _avg_reward_window(
        self,
        *,
        window_seconds: int | None,
        now: float,
        conn: sqlite3.Connection | None = None,
    ) -> float | None:
        """Return mean reward_score for records within the given time window.

        Uses _iter_reward_rows (option b from spec) to query directly without
        extending TrajectoryRecord.
        """
        window_start = (now - window_seconds) if window_seconds is not None else None
        scores = _iter_reward_rows(window_start_ts=window_start, conn=conn)
        if not scores:
            return None
        return sum(scores) / len(scores)


__all__ = [
    "ATROPHY_DAYS_DEFAULT",
    "SkillStatus",
    "DashboardSnapshot",
    "MonitorDashboard",
    "_iter_reward_rows",
]
