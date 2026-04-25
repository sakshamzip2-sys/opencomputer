"""
SQLite-backed archive for :class:`plugin_sdk.decay.DriftReport` records.

Schema lives at ``<profile_home>/user_model/drift_reports.sqlite``.
Pattern mirrors :mod:`opencomputer.user_model.store` and
:mod:`opencomputer.inference.storage`:

* WAL mode for concurrent readers during writes.
* Application-level retry+jitter on ``SQLITE_BUSY``.
* Idempotent migrations via a ``schema_version`` row.

Phase 3.D stashes every :class:`DriftReport` emitted by
:class:`DriftDetector.detect` when a store is attached. Retention is
opt-in via :meth:`DriftStore.delete_older_than`.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from opencomputer.agent.config import _home
from plugin_sdk.decay import DriftReport

#: Incremented when the SQLite schema is extended. Migrations advance
#: the DB from its stored version to :data:`SCHEMA_VERSION` via
#: :func:`apply_migrations`. v1 = baseline.
SCHEMA_VERSION = 1


DDL_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS drift_reports (
    report_id              TEXT PRIMARY KEY,
    created_at             REAL NOT NULL,
    window_seconds         REAL NOT NULL,
    total_kl_divergence    REAL NOT NULL,
    per_kind_drift         TEXT NOT NULL,    -- JSON
    recent_distribution    TEXT NOT NULL,    -- JSON
    lifetime_distribution  TEXT NOT NULL,    -- JSON
    top_changes            TEXT NOT NULL,    -- JSON array
    significant            INTEGER NOT NULL  -- 0/1
);

CREATE INDEX IF NOT EXISTS idx_drift_reports_created_at_desc
    ON drift_reports(created_at DESC);
"""


# ---------------------------------------------------------------------------
# Migration framework — mirror opencomputer/user_model/store.py::apply_migrations
# ---------------------------------------------------------------------------


MIGRATIONS: dict[tuple[int, int], str] = {
    (0, 1): "_migrate_v0_to_v1",
}


def _read_schema_version(conn: sqlite3.Connection) -> int:
    """Return stored schema version. Returns 0 on fresh DBs (no table yet)."""
    try:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def _bump_schema_version(conn: sqlite3.Connection, v: int) -> None:
    """Replace the single schema_version row."""
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (v,))


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """Apply the v1 baseline DDL — drift_reports table + index. Idempotent."""
    conn.executescript(DDL_V1)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Advance DB from stored schema_version to SCHEMA_VERSION. Idempotent."""
    current = _read_schema_version(conn)
    while current < SCHEMA_VERSION:
        fn_name = MIGRATIONS[(current, current + 1)]
        globals()[fn_name](conn)
        _bump_schema_version(conn, current + 1)
        current += 1
    conn.commit()


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def _default_db_path() -> Path:
    """Return ``<profile_home>/user_model/drift_reports.sqlite``, creating dirs."""
    p = _home() / "user_model"
    p.mkdir(parents=True, exist_ok=True)
    return p / "drift_reports.sqlite"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class DriftStore:
    """Thin SQLite wrapper for :class:`DriftReport` CRUD.

    Parameters
    ----------
    db_path:
        Override the default location. ``None`` (the production default)
        uses ``<profile_home>/user_model/drift_reports.sqlite``.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            apply_migrations(conn)

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """Open a transaction with retry+jitter on SQLITE_BUSY.

        Mirrors :mod:`opencomputer.user_model.store`'s ``_txn`` —
        up to 5 retries with 20–150 ms jitter.
        """
        conn = self._connect()
        attempts = 0
        max_attempts = 5
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                attempts += 1
                if attempts >= max_attempts:
                    raise
                time.sleep(random.uniform(0.02, 0.15))
            finally:
                conn.close()

    # ─── CRUD ─────────────────────────────────────────────────────────

    _INSERT_SQL = (
        "INSERT OR REPLACE INTO drift_reports "
        "(report_id, created_at, window_seconds, total_kl_divergence, "
        "per_kind_drift, recent_distribution, lifetime_distribution, "
        "top_changes, significant) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def insert(self, report: DriftReport) -> None:
        """Insert / replace one report by ``report_id``."""
        row = (
            report.report_id,
            float(report.created_at),
            float(report.window_seconds),
            float(report.total_kl_divergence),
            json.dumps(dict(report.per_kind_drift)),
            json.dumps(dict(report.recent_distribution)),
            json.dumps(dict(report.lifetime_distribution)),
            json.dumps([dict(c) for c in report.top_changes]),
            1 if report.significant else 0,
        )
        with self._txn() as conn:
            conn.execute(self._INSERT_SQL, row)

    def get(self, report_id: str) -> DriftReport | None:
        """Fetch by id. Returns ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM drift_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return self._row_to_report(row) if row else None

    def list(  # noqa: A003 — `list` is the natural domain verb here
        self,
        *,
        since: float | None = None,
        significant_only: bool = False,
        limit: int = 20,
    ) -> list[DriftReport]:
        """Return reports matching the filters, newest first.

        Parameters
        ----------
        since:
            Unix epoch seconds — only return reports with
            ``created_at >= since``. ``None`` (the default) means
            "no lower bound".
        significant_only:
            When ``True`` skip reports whose ``significant`` flag is 0.
        limit:
            Cap on rows returned. Default 20.
        """
        clauses: list[str] = []
        args: list[object] = []
        if since is not None:
            clauses.append("created_at >= ?")
            args.append(float(since))
        if significant_only:
            clauses.append("significant = 1")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT * FROM drift_reports {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        args.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [self._row_to_report(r) for r in rows]

    def delete_older_than(self, age_seconds: float) -> int:
        """Delete reports with ``created_at < now - age_seconds``.

        Returns the number of rows deleted.
        """
        cutoff = time.time() - float(age_seconds)
        with self._txn() as conn:
            cur = conn.execute(
                "DELETE FROM drift_reports WHERE created_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    # ─── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_report(row: sqlite3.Row) -> DriftReport:
        """Reconstruct a :class:`DriftReport` from a SQLite row.

        Bad JSON in any of the blob columns is tolerated — corrupt rows
        surface with empty distributions rather than breaking ``list()``.
        """
        def _safe_loads(text: str, fallback: object) -> object:
            try:
                return json.loads(text) if text else fallback
            except (json.JSONDecodeError, TypeError):
                return fallback

        per_kind = _safe_loads(row["per_kind_drift"], {})
        recent = _safe_loads(row["recent_distribution"], {})
        lifetime = _safe_loads(row["lifetime_distribution"], {})
        top = _safe_loads(row["top_changes"], [])
        # Re-cast ints / floats coming back from JSON in case they
        # serialised as strings on some future schema migration.
        return DriftReport(
            report_id=row["report_id"],
            created_at=float(row["created_at"]),
            window_seconds=float(row["window_seconds"]),
            total_kl_divergence=float(row["total_kl_divergence"]),
            per_kind_drift=dict(per_kind) if isinstance(per_kind, dict) else {},
            recent_distribution=(
                dict(recent) if isinstance(recent, dict) else {}
            ),
            lifetime_distribution=(
                dict(lifetime) if isinstance(lifetime, dict) else {}
            ),
            top_changes=tuple(top) if isinstance(top, list) else (),
            significant=bool(row["significant"]),
        )


__all__ = [
    "DriftStore",
    "SCHEMA_VERSION",
    "apply_migrations",
]
