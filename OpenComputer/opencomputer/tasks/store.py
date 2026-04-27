"""SQLite-backed CRUD for detached tasks.

The ``tasks`` table lives in the same ``sessions.db`` as everything else
(schema v5+). One row per task; status field follows the lifecycle:

    queued ──▶ running ──┬──▶ done
                         ├──▶ failed
                         └──▶ cancelled

Plus an ``orphaned`` terminal state used by ``TaskRunner.recover_orphaned``
on startup — if a task was ``running`` when the gateway crashed, it gets
marked ``orphaned`` rather than re-run blindly (re-running a 30-minute LLM
session unattended would burn budget without consent).

Storage decisions:

- **Same DB as sessions** — keeps everything in one file per profile so
  ``opencomputer profile delete`` cleans up cleanly. No second DB to
  worry about.
- **WAL mode + retry-on-busy** — inherited from :class:`SessionDB`'s
  pragmas, so multiple writers (gateway + ``opencomputer task cancel``
  CLI) coexist without locking each other out for long.
- **No FTS5 on tasks** — task corpora stay small (dozens-to-hundreds per
  user). FTS adds storage + trigger overhead with no obvious read-side
  win at this scale.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("opencomputer.tasks.store")


TaskStatus = Literal["queued", "running", "done", "failed", "cancelled", "orphaned"]

TASK_STATUSES: tuple[TaskStatus, ...] = (
    "queued",
    "running",
    "done",
    "failed",
    "cancelled",
    "orphaned",
)


# ──────────────────────────────────────────────────────────────────────
# DDL — applied lazily by TaskStore.__init__ via CREATE IF NOT EXISTS so
# we don't depend on a particular SCHEMA_VERSION number landing first.
# Coordinates safely with archit-2's parallel schema work.
# ──────────────────────────────────────────────────────────────────────


_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    session_id      TEXT,
    user_id         TEXT,
    platform        TEXT,
    chat_id         TEXT,
    status          TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    progress        TEXT,
    output          TEXT,
    error           TEXT,
    created_at      REAL NOT NULL,
    started_at      REAL,
    completed_at    REAL,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    notify_policy   TEXT NOT NULL DEFAULT 'done_only',
    metadata        TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
"""


# ──────────────────────────────────────────────────────────────────────
# Dataclass
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Task:
    id: str
    prompt: str
    status: TaskStatus
    created_at: float
    session_id: str | None = None
    user_id: str | None = None
    platform: str | None = None
    chat_id: str | None = None
    progress: str | None = None
    output: str | None = None
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    delivery_status: str = "pending"
    notify_policy: str = "done_only"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        meta_raw = row["metadata"]
        meta = json.loads(meta_raw) if meta_raw else {}
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            platform=row["platform"],
            chat_id=row["chat_id"],
            status=row["status"],
            prompt=row["prompt"],
            progress=row["progress"],
            output=row["output"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            delivery_status=row["delivery_status"],
            notify_policy=row["notify_policy"],
            metadata=meta,
        )


class TaskNotFound(LookupError):  # noqa: N818 — domain term, not "*Error"
    """Task id doesn't exist in the store."""


# ──────────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────────


class TaskStore:
    """SQLite CRUD for detached tasks.

    The DB path is the same as :class:`SessionDB`'s
    (``<profile_home>/sessions.db``). On first construction the
    ``tasks`` table + indexes are created idempotently.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_TASKS_DDL)

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except sqlite3.OperationalError:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ─── CRUD ─────────────────────────────────────────────────────

    def create(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        user_id: str | None = None,
        platform: str | None = None,
        chat_id: str | None = None,
        notify_policy: str = "done_only",
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        """Create a new task in ``queued`` status. Returns the row."""
        task_id = uuid.uuid4().hex[:12]
        now = time.time()
        meta_json = json.dumps(metadata or {})
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id, session_id, user_id, platform, chat_id, status, "
                "prompt, created_at, delivery_status, notify_policy, metadata) "
                "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, 'pending', ?, ?)",
                (
                    task_id,
                    session_id,
                    user_id,
                    platform,
                    chat_id,
                    prompt,
                    now,
                    notify_policy,
                    meta_json,
                ),
            )
        return Task(
            id=task_id,
            session_id=session_id,
            user_id=user_id,
            platform=platform,
            chat_id=chat_id,
            status="queued",
            prompt=prompt,
            created_at=now,
            notify_policy=notify_policy,
            metadata=metadata or {},
        )

    def get(self, task_id: str) -> Task:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return Task.from_row(row)

    def list_(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 100,
    ) -> list[Task]:
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Task.from_row(r) for r in rows]

    def list_queued(self, limit: int = 16) -> list[Task]:
        """Oldest-first — TaskRunner pulls in FIFO order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'queued' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Task.from_row(r) for r in rows]

    def list_running(self) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'running'"
            ).fetchall()
        return [Task.from_row(r) for r in rows]

    # ─── status transitions ───────────────────────────────────────

    def mark_running(self, task_id: str) -> None:
        now = time.time()
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='running', started_at=? "
                "WHERE id=? AND status='queued'",
                (now, task_id),
            )
            if cur.rowcount == 0:
                raise TaskNotFound(
                    f"task {task_id!r} not in queued status (cannot start)"
                )

    def record_progress(self, task_id: str, progress: str) -> None:
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET progress=? WHERE id=?",
                (progress, task_id),
            )
            if cur.rowcount == 0:
                raise TaskNotFound(task_id)

    def complete(self, task_id: str, output: str) -> None:
        now = time.time()
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='done', output=?, completed_at=? "
                "WHERE id=? AND status='running'",
                (output, now, task_id),
            )
            if cur.rowcount == 0:
                raise TaskNotFound(
                    f"task {task_id!r} not in running status (cannot complete)"
                )

    def fail(self, task_id: str, error: str) -> None:
        now = time.time()
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='failed', error=?, completed_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (error, now, task_id),
            )
            if cur.rowcount == 0:
                raise TaskNotFound(task_id)

    def cancel(self, task_id: str) -> bool:
        """Best-effort cancel. Only ``queued`` and ``running`` may be cancelled.

        Returns True if the row's status changed; False if the task was
        already terminal or didn't exist. Caller decides what to do.

        For ``running`` tasks this only marks the row — the runner will
        observe the new status on its next progress check and abandon
        the in-flight LLM call (it doesn't kill the underlying process,
        the loop just stops).
        """
        now = time.time()
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='cancelled', completed_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (now, task_id),
            )
            return cur.rowcount > 0

    def mark_orphaned_running(self) -> int:
        """Recovery on startup — mark long-running tasks as orphaned.

        Called by :meth:`TaskRunner.recover_orphaned` on gateway start.
        Any ``running`` row at this point was abandoned mid-execution by
        the previous gateway crash; we mark it ``orphaned`` rather than
        re-run blindly (a 30-min LLM session shouldn't replay without
        explicit consent).

        Returns the number of rows marked.
        """
        now = time.time()
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='orphaned', completed_at=?, "
                "error='gateway crashed during run; not auto-resumed' "
                "WHERE status='running'",
                (now,),
            )
            return int(cur.rowcount)

    def mark_delivered(self, task_id: str) -> None:
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET delivery_status='delivered' WHERE id=?",
                (task_id,),
            )
            if cur.rowcount == 0:
                raise TaskNotFound(task_id)
