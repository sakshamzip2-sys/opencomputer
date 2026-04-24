"""Tracks clean vs dirty runs per (capability, scope); offers promotion at N.

A "clean run" is a successful tool call under a PER_ACTION grant — the
user approved, the tool ran, nothing went wrong. After N=10 clean runs
on the same (capability, scope), the promoter signals that the user
could now be offered Tier-2 → Tier-1 promotion (auto-approve for that
scope). A "dirty run" (user said no, or the tool failed visibly) resets
the counter. The threshold is configurable per profile via
consent_config.yaml (plan default: 10).
"""
from __future__ import annotations

import sqlite3
import time


class ProgressivePromoter:
    def __init__(self, conn: sqlite3.Connection, *, threshold_n: int = 10) -> None:
        self._conn = conn
        self._n = threshold_n

    def record_clean_run(self, capability_id: str, scope: str | None) -> None:
        # UPSERT pattern: try INSERT; on PK conflict, increment. Again
        # can't rely on plain (cap, scope) PK due to NULL semantics.
        existing = self._conn.execute(
            "SELECT clean_run_count FROM consent_counters "
            "WHERE capability_id=? AND "
            "((scope_filter IS NULL AND ? IS NULL) OR scope_filter = ?)",
            (capability_id, scope, scope),
        ).fetchone()
        now = time.time()
        if existing is None:
            self._conn.execute(
                "INSERT INTO consent_counters "
                "(capability_id, scope_filter, clean_run_count, last_updated) "
                "VALUES (?, ?, 1, ?)",
                (capability_id, scope, now),
            )
        else:
            self._conn.execute(
                "UPDATE consent_counters SET clean_run_count = clean_run_count + 1, "
                "last_updated = ? WHERE capability_id=? AND "
                "((scope_filter IS NULL AND ? IS NULL) OR scope_filter = ?)",
                (now, capability_id, scope, scope),
            )
        self._conn.commit()

    def record_dirty_run(self, capability_id: str, scope: str | None) -> None:
        now = time.time()
        existing = self._conn.execute(
            "SELECT clean_run_count FROM consent_counters "
            "WHERE capability_id=? AND "
            "((scope_filter IS NULL AND ? IS NULL) OR scope_filter = ?)",
            (capability_id, scope, scope),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO consent_counters "
                "(capability_id, scope_filter, clean_run_count, last_updated) "
                "VALUES (?, ?, 0, ?)",
                (capability_id, scope, now),
            )
        else:
            self._conn.execute(
                "UPDATE consent_counters SET clean_run_count = 0, last_updated = ? "
                "WHERE capability_id=? AND "
                "((scope_filter IS NULL AND ? IS NULL) OR scope_filter = ?)",
                (now, capability_id, scope, scope),
            )
        self._conn.commit()

    def counter(self, capability_id: str, scope: str | None) -> int:
        row = self._conn.execute(
            "SELECT clean_run_count FROM consent_counters "
            "WHERE capability_id=? AND "
            "((scope_filter IS NULL AND ? IS NULL) OR scope_filter = ?)",
            (capability_id, scope, scope),
        ).fetchone()
        return int(row[0]) if row else 0

    def should_offer_promotion(self, capability_id: str, scope: str | None) -> bool:
        return self.counter(capability_id, scope) >= self._n
