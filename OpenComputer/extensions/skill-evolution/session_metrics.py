"""SessionDB → SessionMetrics adapter for skill-evolution pattern detection.

The pattern detector needs derived data (turn count, concatenated user
messages, tool-call summaries) that ``SessionDB.get_session()`` doesn't
expose — that method returns the raw ``sessions`` table row. Compute the
derived fields here so the detector stays pure (takes a metrics dataclass)
and the production query logic lives in one place.

This module is the bridge between ``SessionDB`` (raw rows + ``get_messages``)
and ``pattern_detector`` (heuristic + LLM judge).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("opencomputer.skill_evolution.session_metrics")


@dataclass(frozen=True, slots=True)
class ToolCallSummary:
    """Minimal shape the detector needs from each tool call.

    Captures only ``is_error`` (for the recovery heuristic) and
    ``turn_index`` (to determine which call came last). No content,
    no arguments — privacy-safe by construction.
    """

    is_error: bool = False
    turn_index: int = 0


@dataclass(frozen=True, slots=True)
class SessionMetrics:
    """Derived facts about a session that the pattern detector consumes.

    All fields are computed from ``SessionDB.get_messages()`` — no raw
    message content leaves this dataclass except via
    ``user_messages_concat`` which the detector uses for keyword-overlap
    dedup against existing skill descriptions.
    """

    session_id: str
    turn_count: int = 0
    user_messages_total_chars: int = 0
    user_messages_concat: str = ""
    tool_calls: tuple[ToolCallSummary, ...] = field(default_factory=tuple)


def compute_session_metrics(session_db: Any, session_id: str) -> SessionMetrics | None:
    """Read messages for ``session_id`` and derive the metrics the
    detector needs.

    Returns ``None`` if the session has zero messages OR if any DB
    operation raises — the caller treats both as "not a candidate".

    Tolerates missing/corrupt fields on individual messages: a message
    with no ``role`` attribute is skipped, a message with a non-list
    ``tool_calls`` field is skipped, etc. The detector cares about
    aggregate signal not per-message correctness.
    """
    try:
        messages = session_db.get_messages(session_id)
    except Exception:  # noqa: BLE001 — DB hiccup must not crash the detector
        _log.warning(
            "skill-evolution: get_messages(%r) failed",
            session_id,
            exc_info=True,
        )
        return None

    if not messages:
        return None

    user_chunks: list[str] = []
    tool_calls: list[ToolCallSummary] = []

    for turn_index, msg in enumerate(messages):
        role = getattr(msg, "role", None)
        content = getattr(msg, "content", None)

        if role == "user":
            if isinstance(content, str) and content:
                user_chunks.append(content)
            elif isinstance(content, list):
                # Multimodal content blocks — extract text-only parts.
                for block in content:
                    text = _extract_text_from_block(block)
                    if text:
                        user_chunks.append(text)

        elif role == "tool":
            # Tool result. is_error lives on the message itself.
            tool_calls.append(
                ToolCallSummary(
                    is_error=bool(getattr(msg, "is_error", False)),
                    turn_index=turn_index,
                )
            )

        elif role == "assistant":
            # Assistant tool calls. The Message dataclass exposes
            # ``tool_calls`` as a list — convert each to a summary
            # WITHOUT is_error (the result message above carries that).
            raw_tool_calls = getattr(msg, "tool_calls", None) or ()
            for _ in raw_tool_calls:
                tool_calls.append(
                    ToolCallSummary(is_error=False, turn_index=turn_index)
                )

    user_messages_concat = "\n".join(user_chunks)

    return SessionMetrics(
        session_id=session_id,
        turn_count=len(messages),
        user_messages_total_chars=len(user_messages_concat),
        user_messages_concat=user_messages_concat,
        tool_calls=tuple(tool_calls),
    )


def _extract_text_from_block(block: Any) -> str:
    """Extract text from a multimodal content block.

    Handles dict shape ({"type": "text", "text": "..."}) and dataclass
    shape (TextBlock with .text attribute). Returns empty string for
    non-text blocks (images, etc.) — those carry no signal for keyword
    dedup.
    """
    if isinstance(block, dict):
        if block.get("type") == "text":
            text = block.get("text", "")
            return text if isinstance(text, str) else ""
        return ""
    text = getattr(block, "text", None)
    return text if isinstance(text, str) else ""


__all__ = [
    "SessionMetrics",
    "ToolCallSummary",
    "compute_session_metrics",
]
