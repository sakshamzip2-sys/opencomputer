"""CheckpointManager — per-prompt message-history snapshots.

v1.1 plan-2 M5.2 (2026-05-09). The agent loop fires
:meth:`CheckpointManager.create` before each ``tool_use`` block so a
later ``oc session rewind --mode conv_only`` can restore the message
state at any chosen prior point.

Storage lives in ``sessions.db.prompt_checkpoints`` (schema v15).
Each row carries:

* ``id`` — sha256 prefix of ``(session_id, prompt_index, ts)``.
* ``session_id`` — FK to ``sessions.id``.
* ``prompt_index`` — monotonic per-session counter (start at 0).
* ``messages_snapshot_json`` — JSON-serialised list of message dicts
  at the moment of capture (just before the tool dispatch).
* ``files_snapshot_json`` — opt-in via ``checkpoints.snapshot_files``
  config (NULL by default; the existing ``auto_checkpoint`` PreToolUse
  hook already covers file rollback).
* ``label`` — auto-generated like ``"before tool_use #N"``.
* ``created_at`` — epoch seconds.

The manager is intentionally NOT global — each :class:`SessionDB`
gets its own instance. Tests can construct one against a tmp DB
without touching real state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from opencomputer.agent.state import SessionDB

logger = logging.getLogger("opencomputer.agent.checkpoint_manager")


@dataclass(frozen=True, slots=True)
class MessageCheckpoint:
    """One row of ``prompt_checkpoints``."""

    id: str
    session_id: str
    prompt_index: int
    messages_snapshot_json: str
    label: str
    created_at: float
    files_snapshot_json: str | None = None

    def messages(self) -> list[dict[str, Any]]:
        """Decode ``messages_snapshot_json`` into a list of dicts."""
        try:
            return list(json.loads(self.messages_snapshot_json))
        except json.JSONDecodeError:
            return []


def _checkpoint_id(session_id: str, prompt_index: int, ts: float) -> str:
    """Stable 16-char hex id from (session, index, ts)."""
    raw = f"{session_id}|{prompt_index}|{ts:.6f}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class CheckpointManager:
    """Per-session message-history checkpoint creator + reader.

    Construct with a :class:`SessionDB`; call :meth:`create` before
    each tool_use block in the agent loop. The DB layer handles
    concurrency (SQLite WAL); we don't need a process-wide lock.
    """

    def __init__(self, db: SessionDB) -> None:
        self._db = db

    # ─── create ──────────────────────────────────────────────────

    def create(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        files: dict[str, bytes] | None = None,
        label: str | None = None,
    ) -> MessageCheckpoint:
        """Snapshot the message history (and optionally files) for a session.

        ``label`` defaults to ``"before tool_use #N"`` where N is the
        per-session prompt_index (count of existing checkpoints + 1).

        Returns the persisted :class:`MessageCheckpoint`.
        """
        ts = time.time()
        # Decide prompt_index by counting existing checkpoints for this session.
        # Cheap (indexed COUNT). If a future high-volume profile needs O(1),
        # cache the counter on the session row.
        with self._db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM prompt_checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            existing = int(row[0]) if row else 0
        prompt_index = existing
        cp_id = _checkpoint_id(session_id, prompt_index, ts)
        cp_label = label or f"before tool_use #{prompt_index + 1}"

        msgs_json = json.dumps(messages, default=str)
        files_json: str | None = None
        if files is not None:
            # Files dict[path → bytes]. Encode bytes as latin-1 for JSON
            # safety — round-trips losslessly because latin-1 is the
            # 1-to-1 byte→char mapping. Restore decodes back.
            files_json = json.dumps(
                {p: data.decode("latin-1") for p, data in files.items()},
                default=str,
            )

        cp = MessageCheckpoint(
            id=cp_id,
            session_id=session_id,
            prompt_index=prompt_index,
            messages_snapshot_json=msgs_json,
            files_snapshot_json=files_json,
            label=cp_label,
            created_at=ts,
        )
        try:
            with self._db._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO prompt_checkpoints (
                        id, session_id, prompt_index,
                        messages_snapshot_json, files_snapshot_json,
                        label, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cp.id,
                        cp.session_id,
                        cp.prompt_index,
                        cp.messages_snapshot_json,
                        cp.files_snapshot_json,
                        cp.label,
                        cp.created_at,
                    ),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 — never break the loop
            logger.warning(
                "M5.2: checkpoint create failed for session %s: %s",
                session_id,
                exc,
            )
        return cp

    # ─── list ────────────────────────────────────────────────────

    def list(self, session_id: str, *, limit: int = 50) -> list[MessageCheckpoint]:
        """Return checkpoints for ``session_id``, newest first."""
        with self._db._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, session_id, prompt_index, messages_snapshot_json,
                       files_snapshot_json, label, created_at
                FROM prompt_checkpoints
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            MessageCheckpoint(
                id=r[0],
                session_id=r[1],
                prompt_index=int(r[2]),
                messages_snapshot_json=r[3],
                files_snapshot_json=r[4],
                label=r[5],
                created_at=float(r[6]),
            )
            for r in rows
        ]

    # ─── restore ─────────────────────────────────────────────────

    def restore_messages(self, checkpoint_id: str) -> list[dict[str, Any]] | None:
        """Return the JSON-decoded message list for ``checkpoint_id``.

        ``None`` when the checkpoint isn't found — caller decides
        whether to surface as an error.
        """
        with self._db._connect() as conn:
            row = conn.execute(
                """
                SELECT messages_snapshot_json
                FROM prompt_checkpoints
                WHERE id = ?
                """,
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return list(json.loads(row[0]))
        except json.JSONDecodeError:
            return None

    def restore_files(self, checkpoint_id: str) -> dict[str, bytes] | None:
        """Return the file snapshot for ``checkpoint_id``, or ``None``.

        Returns ``None`` when the checkpoint had no file snapshot
        (the default — opt-in via ``checkpoints.snapshot_files``).
        """
        with self._db._connect() as conn:
            row = conn.execute(
                """
                SELECT files_snapshot_json
                FROM prompt_checkpoints
                WHERE id = ?
                """,
                (checkpoint_id,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            decoded = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return {p: s.encode("latin-1") for p, s in decoded.items()}


__all__ = ["CheckpointManager", "MessageCheckpoint"]
