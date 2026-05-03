"""Phase 0 recall-citations writer.

Records (turn, episodic_event) citation pairs whenever the recall tool
surfaces a memory hit. Phase 2 v0's recommendation engine joins this
table with ``turn_outcomes`` to compute mean downstream ``turn_score``
per memory.

Without this linkage the recommendation engine cannot tell
"memory M was surfaced in turn T" from "memory M shares a session with
turn T." (See BLOCKER #2 in plan self-audit.)
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.agent.state import SessionDB


@dataclass(frozen=True, slots=True)
class CitationWrite:
    session_id: str
    turn_index: int
    episodic_event_id: str | None        # NULL for message-kind hits
    candidate_kind: str                  # 'episodic' | 'message'
    candidate_text_id: str | None        # e.g. 'session@ts' for messages
    bm25_score: float | None             # raw FTS5 rank
    adjusted_score: float | None         # after recall_penalty * decay


class RecallCitationsWriter:
    def __init__(self, db: SessionDB) -> None:
        self._db = db

    def record(self, c: CitationWrite, *, now: float | None = None) -> str:
        """Insert one row. Returns the row's UUID."""
        cid = str(uuid.uuid4())
        ts = time.time() if now is None else now
        with self._db._connect() as conn:
            conn.execute(
                """
                INSERT INTO recall_citations (
                    id, session_id, turn_index, episodic_event_id,
                    candidate_kind, candidate_text_id, bm25_score,
                    adjusted_score, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid, c.session_id, c.turn_index, c.episodic_event_id,
                    c.candidate_kind, c.candidate_text_id, c.bm25_score,
                    c.adjusted_score, ts,
                ),
            )
        return cid
