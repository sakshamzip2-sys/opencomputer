"""SQLite-backed rate limiter for draft synthesis (Phase 5.3).

Two caps:

- ``per_day``: max successful drafts in any rolling 24h window. Default 1.
- ``lifetime``: max total successful drafts since the limiter's DB was
  created. Default 10.

Why so strict: a skill that pollutes the activation matcher hurts every
future turn. We'd rather under-propose than spam. The user can `reset`
when they're confident things are working.

The limiter is consulted *before* the synthesizer runs. Successful
drafts (approved by the user via Phase 5.3 CLI, not the model)
``record_draft()`` to bump the counter.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


class _RateLimitExceeded(RuntimeError):  # noqa: N818 — public name is the load-bearing one (no Error suffix per project style)
    pass


class DraftRateLimiter:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        per_day: int = 1,
        lifetime: int = 10,
    ) -> None:
        if db_path is None:
            # Lazy import — profiles.get_default_root() is HOME-mutation-immune
            # so the rate-limit DB location is stable across profile contexts.
            from opencomputer.profiles import get_default_root
            db_path = get_default_root() / "evolution" / "rate.db"
        self.db_path = db_path
        self.per_day = per_day
        self.lifetime = lifetime
        self._init_schema()

    def _init_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS drafts ("
                "  iso_ts TEXT PRIMARY KEY"
                ")"
            )
            conn.commit()

    def allow(self, *, now: datetime | None = None) -> None:
        """Raise ``_RateLimitExceeded`` if we'd exceed either cap.

        ``now`` is exposed for testability. Default is UTC wall clock.
        """
        n = now or datetime.now(UTC)
        day_ago = (n - timedelta(days=1)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            (recent,) = conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE iso_ts > ?", (day_ago,)
            ).fetchone()
            (total,) = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()
        if recent >= self.per_day:
            raise _RateLimitExceeded(
                f"per-day cap reached: {recent} drafts in last 24h "
                f"(max {self.per_day})"
            )
        if total >= self.lifetime:
            raise _RateLimitExceeded(
                f"lifetime cap reached: {total} total drafts "
                f"(max {self.lifetime}). Use `opencomputer skill reset-limits` "
                "to clear when you're sure things are working."
            )

    def record_draft(self, *, when: datetime | None = None) -> None:
        """Bump the counter. Call AFTER a draft is successfully written."""
        n = when or datetime.now(UTC)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO drafts (iso_ts) VALUES (?)", (n.isoformat(),))
            conn.commit()

    def reset(self) -> None:
        """Wipe the counter (for tests + explicit user reset)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM drafts")
            conn.commit()


# Re-export the exception with a public name (the leading underscore is
# only to avoid showing up in module __init__ accidentally).
RateLimitExceeded = _RateLimitExceeded
