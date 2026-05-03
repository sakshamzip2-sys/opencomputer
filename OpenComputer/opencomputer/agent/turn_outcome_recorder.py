"""Phase 0 turn-outcome recorder.

Reads per-turn signals (tool calls, vibe, latency, affirmation/correction)
and writes one row to ``turn_outcomes``. Called from
``gateway/dispatch.py`` AFTER ``run_conversation()`` returns — outside
the loop's critical path so the user reply isn't blocked on telemetry.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.agent.state import SessionDB


@dataclass(frozen=True, slots=True)
class TurnSignals:
    session_id: str
    turn_index: int

    # Tool call signals (sourced from tool_usage)
    tool_call_count: int = 0
    tool_success_count: int = 0
    tool_error_count: int = 0
    tool_blocked_count: int = 0
    self_cancel_count: int = 0
    retry_count: int = 0

    # User signals
    vibe_before: str | None = None
    vibe_after: str | None = None
    reply_latency_s: float | None = None
    affirmation_present: bool = False
    correction_present: bool = False
    conversation_abandoned: bool = False

    # System signals
    standing_order_violations: tuple[str, ...] = field(default_factory=tuple)
    duration_s: float | None = None


class TurnOutcomeRecorder:
    def __init__(self, db: "SessionDB") -> None:
        self._db = db

    def record(self, sig: TurnSignals, *, now: float | None = None) -> str:
        """Insert one row. Returns the new row's UUID."""
        row_id = str(uuid.uuid4())
        ts = time.time() if now is None else now
        violations_json = (
            json.dumps(list(sig.standing_order_violations))
            if sig.standing_order_violations
            else None
        )
        with self._db._connect() as conn:
            conn.execute(
                """
                INSERT INTO turn_outcomes (
                    id, session_id, turn_index, created_at,
                    tool_call_count, tool_success_count, tool_error_count,
                    tool_blocked_count, self_cancel_count, retry_count,
                    vibe_before, vibe_after, reply_latency_s,
                    affirmation_present, correction_present,
                    conversation_abandoned,
                    standing_order_violations, duration_s, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    row_id, sig.session_id, sig.turn_index, ts,
                    sig.tool_call_count, sig.tool_success_count,
                    sig.tool_error_count, sig.tool_blocked_count,
                    sig.self_cancel_count, sig.retry_count,
                    sig.vibe_before, sig.vibe_after, sig.reply_latency_s,
                    int(sig.affirmation_present), int(sig.correction_present),
                    int(sig.conversation_abandoned),
                    violations_json, sig.duration_s,
                ),
            )
        return row_id
