"""
SQLite-backed CRUD for :class:`plugin_sdk.inference.Motif` records.

Schema lives at ``<profile_home>/inference/motifs.sqlite``. Pattern
mirrors :mod:`opencomputer.agent.state` — WAL mode, application-level
retry+jitter on SQLITE_BUSY, idempotent migrations.

Phase 3.C user-model graph reads from :meth:`MotifStore.list` keyed
by ``kind`` — that's the public consumption path.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opencomputer.agent.config import _home
from plugin_sdk.inference import Motif, MotifKind

#: Incremented when the SQLite schema is extended. Migrations advance
#: the DB from its stored version to :data:`SCHEMA_VERSION` via
#: :func:`apply_migrations`. v1 = baseline (motifs table + index).
SCHEMA_VERSION = 1

DDL_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS motifs (
    motif_id            TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    confidence          REAL NOT NULL,
    support             INTEGER NOT NULL,
    summary             TEXT NOT NULL,
    payload             TEXT NOT NULL,    -- JSON
    evidence_event_ids  TEXT NOT NULL,    -- JSON array
    created_at          REAL NOT NULL,
    session_id          TEXT
);

CREATE INDEX IF NOT EXISTS idx_motifs_kind_created
    ON motifs(kind, created_at DESC);
"""


# ---------------------------------------------------------------------------
# Migration framework — mirror opencomputer/agent/state.py::apply_migrations
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
    """Apply the v1 baseline DDL — motifs table + index. Idempotent."""
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
    """Return ``<profile_home>/inference/motifs.sqlite``, creating dirs."""
    p = _home() / "inference"
    p.mkdir(parents=True, exist_ok=True)
    return p / "motifs.sqlite"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class MotifStore:
    """Thin SQLite wrapper for :class:`Motif` CRUD.

    Parameters
    ----------
    db_path:
        Override the default location. ``None`` (the production
        default) uses ``<profile_home>/inference/motifs.sqlite``.
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

        Pattern adapted from :mod:`opencomputer.agent.state`. ~5 retries
        with 20–150 ms jitter — friendly to short-lived contention from
        multiple writers (CLI + engine subscriber).
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

    @staticmethod
    def _motif_row(m: Motif) -> tuple[Any, ...]:
        return (
            m.motif_id,
            m.kind,
            float(m.confidence),
            int(m.support),
            m.summary,
            json.dumps(dict(m.payload)),
            json.dumps(list(m.evidence_event_ids)),
            float(m.created_at),
            m.session_id,
        )

    _INSERT_SQL = (
        "INSERT OR REPLACE INTO motifs "
        "(motif_id, kind, confidence, support, summary, payload, "
        "evidence_event_ids, created_at, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def insert(self, motif: Motif) -> None:
        """Insert one motif. Idempotent via PK conflict (REPLACE)."""
        with self._txn() as conn:
            conn.execute(self._INSERT_SQL, self._motif_row(motif))

    def insert_many(self, motifs: list[Motif]) -> int:
        """Insert a batch atomically. Returns the number written."""
        if not motifs:
            return 0
        with self._txn() as conn:
            for m in motifs:
                conn.execute(self._INSERT_SQL, self._motif_row(m))
        return len(motifs)

    def get(self, motif_id: str) -> Motif | None:
        """Fetch by id. Returns ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM motifs WHERE motif_id = ?",
                (motif_id,),
            ).fetchone()
        return self._row_to_motif(row) if row else None

    def list(  # noqa: A003 — `list` is the natural domain verb here
        self,
        *,
        kind: MotifKind | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[Motif]:
        """Return motifs matching the filters, newest first.

        Parameters
        ----------
        kind:
            Restrict to one motif kind. ``None`` returns all kinds.
        since:
            Unix epoch seconds — only return motifs with
            ``created_at >= since``. Phase 3.C uses this to fetch
            "what's new since last sync".
        limit:
            Cap on rows returned; defaults to 100. Higher values are
            allowed but the index covers ``(kind, created_at DESC)``
            so unbounded scans should pin to ``kind``.
        """
        clauses: list[str] = []
        args: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            args.append(kind)
        if since is not None:
            clauses.append("created_at >= ?")
            args.append(float(since))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT * FROM motifs {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        args.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [self._row_to_motif(r) for r in rows]

    def count(self, *, kind: MotifKind | None = None) -> int:
        """Return the number of motifs (optionally filtered by kind)."""
        with self._connect() as conn:
            if kind is None:
                row = conn.execute("SELECT COUNT(*) FROM motifs").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM motifs WHERE kind = ?",
                    (kind,),
                ).fetchone()
        return int(row[0]) if row else 0

    def delete_older_than(self, age_seconds: float) -> int:
        """Delete motifs with ``created_at < now - age_seconds``.

        Returns the number of rows deleted. Used by the
        ``opencomputer inference motifs prune`` CLI for retention.
        """
        cutoff = time.time() - float(age_seconds)
        with self._txn() as conn:
            cur = conn.execute(
                "DELETE FROM motifs WHERE created_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    # ─── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_motif(row: sqlite3.Row) -> Motif:
        """Reconstruct a :class:`Motif` from a SQLite row.

        Defensive against bad JSON in ``payload`` / ``evidence_event_ids``
        — a corrupt row should not break ``list()``. Bad rows surface
        as motifs with empty payloads; the original error is swallowed
        because store consumers don't have a meaningful recovery path.
        """
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        try:
            evidence = (
                tuple(json.loads(row["evidence_event_ids"]))
                if row["evidence_event_ids"]
                else ()
            )
        except (json.JSONDecodeError, TypeError):
            evidence = ()
        return Motif(
            motif_id=row["motif_id"],
            kind=row["kind"],
            confidence=float(row["confidence"]),
            support=int(row["support"]),
            summary=row["summary"],
            payload=payload,
            evidence_event_ids=evidence,
            created_at=float(row["created_at"]),
            session_id=row["session_id"],
        )


__all__ = [
    "MotifStore",
    "SCHEMA_VERSION",
    "apply_migrations",
]
