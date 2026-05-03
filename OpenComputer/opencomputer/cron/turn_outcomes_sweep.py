"""Phase 0 cron sweeps: backfill self_cancel_count + conversation_abandoned.

These signals can't be computed at turn-end (we don't yet know if a
follow-up will arrive). Cron does the second-pass enrichment on a delay:

- ``sweep_self_cancels`` — every 5 min: scan recent ``tool_usage`` rows
  for known undo-pair patterns within a session; backfill the
  ``turn_outcomes`` row whose creation time straddles the pair.

- ``sweep_abandonments`` — every 1 hr: mark the LAST ``turn_outcomes``
  row of any session that has had no activity for ``threshold_s`` (24h
  by default).
"""
from __future__ import annotations

import logging
import time

_logger = logging.getLogger(__name__)

#: Tool pairs (original, undo) that count as self-cancels when they
#: occur within ``_SELF_CANCEL_WINDOW_S`` in the same session. The
#: heuristic is intentionally simple in v0; v0.5 may match on tool
#: arguments (same path, same calendar event id, etc.) for stronger
#: precision once we record args.
_SELF_CANCEL_HEURISTICS: list[tuple[str, str]] = [
    ("Write", "Bash"),         # write a file then rm/mv it
    ("MultiEdit", "Bash"),
    ("CronCreate", "CronDelete"),
]

_SELF_CANCEL_WINDOW_S = 60.0


def sweep_self_cancels(db, since_ts: float) -> int:
    """Detect self-cancel patterns in tool_usage since ``since_ts`` and
    backfill self_cancel_count on the corresponding turn_outcomes rows.

    Returns count of (turn_outcomes_row, +1) increments applied.
    """
    increments = 0
    with db._connect() as conn:
        for orig_tool, undo_tool in _SELF_CANCEL_HEURISTICS:
            rows = conn.execute(
                """
                SELECT a.session_id, a.ts AS a_ts, b.ts AS b_ts
                FROM tool_usage a
                JOIN tool_usage b ON b.session_id = a.session_id
                WHERE a.tool = ?
                  AND b.tool = ?
                  AND b.ts > a.ts
                  AND (b.ts - a.ts) <= ?
                  AND a.ts >= ?
                """,
                (orig_tool, undo_tool, _SELF_CANCEL_WINDOW_S, since_ts),
            ).fetchall()
            for row in rows:
                sid = row["session_id"]
                a_ts = row["a_ts"]
                b_ts = row["b_ts"]
                # Find the turn_outcomes row whose creation time straddles a_ts.
                target = conn.execute(
                    """
                    SELECT id FROM turn_outcomes
                    WHERE session_id = ?
                      AND created_at >= ?
                      AND created_at <= ?
                    LIMIT 1
                    """,
                    (sid, a_ts - 30, b_ts + 30),
                ).fetchone()
                if target:
                    conn.execute(
                        "UPDATE turn_outcomes "
                        "SET self_cancel_count = self_cancel_count + 1 "
                        "WHERE id = ?",
                        (target["id"],),
                    )
                    increments += 1

    if increments:
        _logger.info("self_cancels sweep: +%d increments", increments)
    return increments


def sweep_abandonments(db, threshold_s: float = 86400.0) -> int:
    """Mark turn_outcomes rows with conversation_abandoned=1 when no
    follow-up activity has occurred in ``threshold_s`` seconds.

    Only the LAST turn of each session is a candidate — earlier turns
    that were followed by another turn are by definition not abandoned.

    Returns count of rows newly marked abandoned.
    """
    now = time.time()
    cutoff = now - threshold_s
    with db._connect() as conn:
        cur = conn.execute(
            """
            UPDATE turn_outcomes
            SET conversation_abandoned = 1
            WHERE conversation_abandoned = 0
              AND created_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM turn_outcomes t2
                  WHERE t2.session_id = turn_outcomes.session_id
                    AND t2.created_at > turn_outcomes.created_at
              )
            """,
            (cutoff,),
        )
        n = cur.rowcount

    if n:
        _logger.info("abandonment sweep: marked %d rows", n)
    return n
