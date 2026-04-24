"""Evolution storage layer — SQLite-backed CRUD for trajectory records.

# TODO(F1): replace this self-contained migration runner with the F1 framework
# once Sub-project F lands. See OpenComputer/docs/evolution/design.md §5.1 for
# the refactor path.

Design reference: OpenComputer/docs/evolution/design.md §4.4, §5, §5.2, §5.3.
Pattern reference: opencomputer/agent/state.py (WAL mode, retry-jitter).
"""

from __future__ import annotations

import dataclasses
import json
import random
import re
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path

from opencomputer.agent.config import _home
from opencomputer.evolution.trajectory import (
    TrajectoryEvent,
    TrajectoryRecord,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def evolution_home() -> Path:
    """Return the per-profile evolution home dir, creating if missing."""
    p = _home() / "evolution"
    p.mkdir(parents=True, exist_ok=True)
    return p


def trajectory_db_path() -> Path:
    """Return the path to the trajectory SQLite file (per profile)."""
    return evolution_home() / "trajectory.sqlite"


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")


@dataclasses.dataclass(frozen=True, slots=True)
class Migration:
    """A single discovered SQL migration file."""

    version: int
    path: Path
    sql: str


def _discover_migrations() -> list[Migration]:
    """Scan opencomputer/evolution/migrations/*.sql, return sorted by numeric prefix."""
    migrations: list[Migration] = []
    for sql_path in _MIGRATIONS_DIR.glob("*.sql"):
        m = _MIGRATION_RE.match(sql_path.name)
        if m is None:
            continue
        version = int(m.group(1))
        sql = sql_path.read_text(encoding="utf-8")
        migrations.append(Migration(version=version, path=sql_path, sql=sql))
    return sorted(migrations, key=lambda x: x.version)


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS schema_version (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER NOT NULL,
            applied_at REAL NOT NULL
        )
        """
    )


def _max_applied(conn: sqlite3.Connection) -> int:
    """Return MAX(version) from schema_version, or 0 if empty/missing table."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])
    except sqlite3.OperationalError:
        # table doesn't exist yet
        return 0


def apply_pending(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations in version order. Returns versions newly applied.

    Idempotent: re-running on an up-to-date DB returns []. Each migration runs in
    its own transaction (``with conn:``).
    """
    _ensure_schema_version_table(conn)
    current = _max_applied(conn)
    pending = [m for m in _discover_migrations() if m.version > current]
    applied: list[int] = []
    for m in sorted(pending, key=lambda x: x.version):
        with conn:
            conn.executescript(m.sql)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (m.version, time.time()),
            )
        applied.append(m.version)
    return applied


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection in WAL mode with sane defaults.

    Pattern matches opencomputer/agent/state.py.
    """
    if path is None:
        path = trajectory_db_path()
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ---------------------------------------------------------------------------
# Internal retry helper
# ---------------------------------------------------------------------------


def _with_retry(conn: sqlite3.Connection, fn):  # type: ignore[no-untyped-def]
    """Run *fn(conn)* with application-level retry on SQLITE_BUSY / locked.

    ~3 retries, 10-50 ms jitter — matches agent/state.py pattern.
    """
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            return fn(conn)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if ("locked" not in msg and "busy" not in msg) or attempt >= max_attempts - 1:
                raise
            time.sleep(random.uniform(0.01, 0.05))


# ---------------------------------------------------------------------------
# Public CRUD API
# ---------------------------------------------------------------------------


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """Open connection, run apply_pending, return the connection."""
    if conn is None:
        conn = _connect()
    apply_pending(conn)
    return conn


def insert_record(
    record: TrajectoryRecord,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Insert a TrajectoryRecord (and its events). Returns the new record id.

    Each TrajectoryEvent's metadata is serialised as JSON via json.dumps.
    The record's events are inserted with seq = 0..len-1 in order.
    """
    _own_conn = conn is None
    if _own_conn:
        conn = _connect()

    assert conn is not None

    def _do(c: sqlite3.Connection) -> int:
        with c:
            cur = c.execute(
                """
                INSERT INTO trajectory_records
                    (session_id, record_schema_version, started_at, ended_at,
                     completion_flag, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_id,
                    record.schema_version,
                    record.started_at,
                    record.ended_at,
                    1 if record.completion_flag else 0,
                    time.time(),
                ),
            )
            record_id = int(cur.lastrowid or 0)
            for seq, event in enumerate(record.events):
                metadata_json: str | None = None
                if event.metadata:
                    metadata_json = json.dumps(dict(event.metadata))
                c.execute(
                    """
                    INSERT INTO trajectory_events
                        (record_id, seq, message_id, action_type, tool_name,
                         outcome, timestamp, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        seq,
                        event.message_id,
                        event.action_type,
                        event.tool_name,
                        event.outcome,
                        event.timestamp,
                        metadata_json,
                    ),
                )
            return record_id

    try:
        return _with_retry(conn, _do)
    finally:
        if _own_conn:
            conn.close()


def _build_record(row: sqlite3.Row, event_rows: list[sqlite3.Row]) -> TrajectoryRecord:
    """Reconstruct a TrajectoryRecord from DB rows."""
    events: list[TrajectoryEvent] = []
    for ev in event_rows:
        metadata: Mapping = {}
        if ev["metadata_json"]:
            try:
                metadata = json.loads(ev["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        events.append(
            TrajectoryEvent(
                session_id=row["session_id"],
                message_id=ev["message_id"],
                action_type=ev["action_type"],
                tool_name=ev["tool_name"],
                outcome=ev["outcome"],
                timestamp=ev["timestamp"],
                metadata=metadata,
            )
        )
    return TrajectoryRecord(
        id=row["id"],
        session_id=row["session_id"],
        schema_version=row["record_schema_version"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        events=tuple(events),
        completion_flag=bool(row["completion_flag"]),
    )


def get_record(
    record_id: int,
    conn: sqlite3.Connection | None = None,
) -> TrajectoryRecord | None:
    """Fetch a record + its events. Returns None if not found.

    Reconstructs the TrajectoryRecord with full events tuple, parsing
    metadata_json back.
    """
    _own_conn = conn is None
    if _own_conn:
        conn = _connect()

    assert conn is not None

    try:
        row = conn.execute(
            "SELECT * FROM trajectory_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        event_rows = conn.execute(
            "SELECT * FROM trajectory_events WHERE record_id = ? ORDER BY seq",
            (record_id,),
        ).fetchall()
        return _build_record(row, event_rows)
    finally:
        if _own_conn:
            conn.close()


def list_recent(
    limit: int = 30,
    conn: sqlite3.Connection | None = None,
) -> list[TrajectoryRecord]:
    """Return up to ``limit`` most recent records (ordered by created_at DESC).
    Each record includes its events.
    """
    _own_conn = conn is None
    if _own_conn:
        conn = _connect()

    assert conn is not None

    try:
        rows = conn.execute(
            "SELECT * FROM trajectory_records ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result: list[TrajectoryRecord] = []
        for row in rows:
            event_rows = conn.execute(
                "SELECT * FROM trajectory_events WHERE record_id = ? ORDER BY seq",
                (row["id"],),
            ).fetchall()
            result.append(_build_record(row, event_rows))
        return result
    finally:
        if _own_conn:
            conn.close()


def count_records(conn: sqlite3.Connection | None = None) -> int:
    """Total number of records in the DB."""
    _own_conn = conn is None
    if _own_conn:
        conn = _connect()

    assert conn is not None

    try:
        row = conn.execute("SELECT COUNT(*) FROM trajectory_records").fetchone()
        return int(row[0]) if row else 0
    finally:
        if _own_conn:
            conn.close()


def purge_older_than(
    epoch_seconds: float,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Delete records with ended_at < epoch_seconds (or created_at if ended_at is NULL).
    Returns the number of records deleted.
    """
    _own_conn = conn is None
    if _own_conn:
        conn = _connect()

    assert conn is not None

    def _do(c: sqlite3.Connection) -> int:
        with c:
            cur = c.execute(
                """
                DELETE FROM trajectory_records
                WHERE (ended_at IS NOT NULL AND ended_at < ?)
                   OR (ended_at IS NULL AND created_at < ?)
                """,
                (epoch_seconds, epoch_seconds),
            )
            return cur.rowcount

    try:
        return _with_retry(conn, _do)
    finally:
        if _own_conn:
            conn.close()


def update_reward(
    record_id: int,
    reward: float,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Set reward_score on a record."""
    _own_conn = conn is None
    if _own_conn:
        conn = _connect()

    assert conn is not None

    def _do(c: sqlite3.Connection) -> None:
        with c:
            c.execute(
                "UPDATE trajectory_records SET reward_score = ? WHERE id = ?",
                (reward, record_id),
            )

    try:
        _with_retry(conn, _do)
    finally:
        if _own_conn:
            conn.close()


__all__ = [
    "evolution_home",
    "trajectory_db_path",
    "Migration",
    "apply_pending",
    "init_db",
    "insert_record",
    "get_record",
    "list_recent",
    "count_records",
    "purge_older_than",
    "update_reward",
]
