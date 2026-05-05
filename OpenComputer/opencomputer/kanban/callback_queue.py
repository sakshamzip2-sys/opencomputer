"""Outbound callback retry queue for delegated kanban tasks (Wave 6.E.17).

PR #460 documented "if the callback POST to sender fails, the result data
is lost." This module closes that gap with a production-grade retry
queue:

- ``enqueue_callback`` writes a pending row when a peer-side worker
  transitions a delegated task. The peer-side ``proxy_spawn`` endpoint
  also calls :func:`record_delegated_task` so the queue knows which
  sender + callback URL the task belongs to.
- The dispatcher's drainer tick walks :func:`next_due` each iteration,
  POSTs each pending callback (signed with the sender's HMAC secret),
  and either :func:`mark_delivered` on 2xx or :func:`mark_attempted`
  on failure. Backoff is exponential with a cap, then dead-letter at
  ``max_attempts``.

Idempotency on the sender's side is already provided by
``reconcile_callback`` — duplicate deliveries (e.g. attempt 3 succeeded
but our 200 response was dropped) hit ``find_claim_by_remote_id`` →
status=done → no-op. So aggressive retries are safe.

Backoff schedule (per audit lens A6, also in 2026-05-05 design doc):

  attempt 1 fails -> +30s
  attempt 2 fails -> +60s
  attempt 3 fails -> +120s
  attempt 4 fails -> +300s
  attempts 5..9   -> +600s each
  after 10        -> dead-letter (status='dead')

Total time before dead-letter: ~80 minutes — long enough to survive an
overnight peer outage but short enough that an operator can act.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from opencomputer.kanban import db as kdb

# Default cap on retries before dead-lettering. Operators can override
# at call-site; the default is what the design doc audited.
DEFAULT_MAX_ATTEMPTS = 10

# Per-attempt backoff in seconds. Index = attempt_count BEFORE this
# attempt was made (so backoff[0] is what we use after attempt 1
# failed). Saturates at the last entry for attempts beyond the table.
_BACKOFF_SECONDS = (30, 60, 120, 300, 600, 600, 600, 600, 600)


@dataclass(frozen=True, slots=True)
class PendingCallback:
    id: int
    sender_slug: str
    callback_url: str
    payload_json: str
    attempt_count: int
    next_attempt_at: int
    last_error: str | None
    status: str
    created_at: int


# ---------------------------------------------------------------------------
# Delegated-task tracking — peer side
# ---------------------------------------------------------------------------


def record_delegated_task(
    conn: sqlite3.Connection,
    *,
    local_task_id: str,
    sender_slug: str,
    callback_url: str,
) -> None:
    """Note that ``local_task_id`` is a mirror of a delegated task.

    Called by the peer's ``/proxy/spawn`` handler after creating the
    local mirror task. Idempotent: re-calling with the same task_id is
    a no-op (REPLACE semantics — useful if the sender retries spawn
    after a network blip).
    """
    if not local_task_id or not sender_slug or not callback_url:
        raise ValueError(
            "record_delegated_task requires non-empty local_task_id, "
            "sender_slug, and callback_url"
        )
    now = int(time.time())
    with kdb.write_txn(conn):
        conn.execute(
            "INSERT OR REPLACE INTO kanban_delegated_tasks "
            "(local_task_id, sender_slug, callback_url, created_at) "
            "VALUES (?, ?, ?, ?)",
            (local_task_id, sender_slug, callback_url, now),
        )


def find_delegated_task(
    conn: sqlite3.Connection, local_task_id: str,
) -> tuple[str, str] | None:
    """Return ``(sender_slug, callback_url)`` if this task was delegated,
    else None. Used by complete/block/fail handlers to decide whether
    to enqueue a callback."""
    row = conn.execute(
        "SELECT sender_slug, callback_url FROM kanban_delegated_tasks "
        "WHERE local_task_id = ?",
        (local_task_id,),
    ).fetchone()
    if row is None:
        return None
    return (row["sender_slug"], row["callback_url"])


# ---------------------------------------------------------------------------
# Callback queue
# ---------------------------------------------------------------------------


def enqueue_callback(
    conn: sqlite3.Connection,
    *,
    sender_slug: str,
    callback_url: str,
    payload: dict,
) -> int:
    """Insert a pending callback. Returns the row id.

    First attempt fires immediately (next_attempt_at = now); subsequent
    failures push it forward via :func:`mark_attempted`.
    """
    if not sender_slug or not callback_url:
        raise ValueError(
            "enqueue_callback requires non-empty sender_slug and callback_url"
        )
    payload_json = json.dumps(payload, separators=(",", ":"))
    now = int(time.time())
    with kdb.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO kanban_pending_callbacks "
            "(sender_slug, callback_url, payload_json, attempt_count, "
            " next_attempt_at, last_error, status, created_at) "
            "VALUES (?, ?, ?, 0, ?, NULL, 'pending', ?)",
            (sender_slug, callback_url, payload_json, now, now),
        )
        return int(cur.lastrowid or 0)


def next_due(
    conn: sqlite3.Connection, *, now: int | None = None, limit: int = 50,
) -> list[PendingCallback]:
    """Return pending callbacks whose ``next_attempt_at <= now``.

    Ordered by next_attempt_at ASC so the oldest-due fires first.
    ``limit`` caps a runaway tick — the drainer processes at most this
    many per pass.
    """
    if now is None:
        now = int(time.time())
    rows = conn.execute(
        "SELECT * FROM kanban_pending_callbacks "
        "WHERE status = 'pending' AND next_attempt_at <= ? "
        "ORDER BY next_attempt_at ASC, id ASC LIMIT ?",
        (now, int(limit)),
    ).fetchall()
    return [_row_to_pc(r) for r in rows]


def mark_delivered(conn: sqlite3.Connection, row_id: int) -> None:
    """Flip ``status -> 'delivered'``. The row stays for audit; a future
    janitor can prune delivered rows older than N days."""
    with kdb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_pending_callbacks SET status = 'delivered' "
            "WHERE id = ?",
            (row_id,),
        )


def mark_attempted(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    error: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    """Bump attempt_count + reschedule with exponential backoff.

    Returns the new status: ``'pending'`` (still retrying) or
    ``'dead'`` (max_attempts reached).
    """
    now = int(time.time())
    with kdb.write_txn(conn):
        row = conn.execute(
            "SELECT attempt_count FROM kanban_pending_callbacks WHERE id = ?",
            (row_id,),
        ).fetchone()
        if row is None:
            return "missing"
        attempts = int(row["attempt_count"]) + 1
        if attempts >= int(max_attempts):
            conn.execute(
                "UPDATE kanban_pending_callbacks "
                "SET attempt_count = ?, last_error = ?, status = 'dead' "
                "WHERE id = ?",
                (attempts, _truncate_err(error), row_id),
            )
            return "dead"
        backoff = _backoff_for_attempt(attempts)
        conn.execute(
            "UPDATE kanban_pending_callbacks "
            "SET attempt_count = ?, last_error = ?, next_attempt_at = ? "
            "WHERE id = ?",
            (attempts, _truncate_err(error), now + backoff, row_id),
        )
        return "pending"


def list_dead_letters(conn: sqlite3.Connection) -> list[PendingCallback]:
    """Return callbacks that exhausted retries — for ``oc kanban callback list-dead``."""
    rows = conn.execute(
        "SELECT * FROM kanban_pending_callbacks "
        "WHERE status = 'dead' ORDER BY id DESC"
    ).fetchall()
    return [_row_to_pc(r) for r in rows]


def requeue_dead_letter(conn: sqlite3.Connection, row_id: int) -> bool:
    """Reset a dead row back to pending for one more try (operator escape)."""
    now = int(time.time())
    with kdb.write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_pending_callbacks "
            "SET status = 'pending', attempt_count = 0, "
            "    next_attempt_at = ?, last_error = NULL "
            "WHERE id = ? AND status = 'dead'",
            (now, row_id),
        )
        return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _backoff_for_attempt(attempt: int) -> int:
    """Return the seconds-to-wait after ``attempt`` failures.

    ``attempt`` is the 1-based failure count; we look up index
    ``attempt - 1`` (so attempt 1 -> +30s). Saturates at the last entry
    for attempts past the table.
    """
    idx = max(0, min(attempt - 1, len(_BACKOFF_SECONDS) - 1))
    return _BACKOFF_SECONDS[idx]


def _truncate_err(error: str) -> str:
    """Cap stored errors at 500 chars so a noisy traceback doesn't bloat the row."""
    return (error or "")[:500]


def _row_to_pc(row) -> PendingCallback:
    return PendingCallback(
        id=int(row["id"]),
        sender_slug=row["sender_slug"],
        callback_url=row["callback_url"],
        payload_json=row["payload_json"],
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=int(row["next_attempt_at"]),
        last_error=row["last_error"],
        status=row["status"],
        created_at=int(row["created_at"]),
    )


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "PendingCallback",
    "enqueue_callback",
    "find_delegated_task",
    "list_dead_letters",
    "mark_attempted",
    "mark_delivered",
    "next_due",
    "record_delegated_task",
    "requeue_dead_letter",
]
