"""Post-response reviewer — fire-and-forget memory curation after each turn.

Hermes pattern (03-borrowables.md §Post-response reviewer): once the agent
delivers a final answer, spawn a *separate* lightweight pass that reads the
turn's messages and decides whether anything is worth filing into long-term
memory. Recursion is suppressed by the same flag (`_is_reviewer=True`) — the
reviewer never spawns another reviewer.

The reviewer is **never blocking**: it runs as an `asyncio.create_task` after
`run_conversation` returns. If it fails, the user-facing turn is unaffected.

v1 design: rule-based (no LLM call) so this ships zero-latency, zero-cost.
The hooks for an LLM-backed reviewer are documented in the class docstring;
swap `_should_note` and `_extract_note` for an LLM call when v2 lands.

Source: hermes-agent agent/run_agent.py:_spawn_review_agent +
tools/memory_tool.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from opencomputer.agent.memory import MemoryManager
from plugin_sdk.core import Message

logger = logging.getLogger("opencomputer.agent.reviewer")

#: Heuristic phrases that suggest the assistant just affirmed a user preference,
#: a user-shared fact, or a decision worth remembering. v2 LLM-backed reviewer
#: replaces this with model judgement.
_NOTABLE_PHRASES: tuple[str, ...] = (
    "i'll remember",
    "noted",
    "got it, you prefer",
    "thanks for telling me",
    "good to know",
)

#: Don't auto-note the same content twice in a row.
_RECENT_NOTES: list[str] = []
_RECENT_NOTES_MAX = 16


@dataclass(slots=True)
class ReviewResult:
    noted: bool
    note_text: str = ""
    skipped_reason: str = ""


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _should_note(user_message: str, assistant_message: str) -> bool:
    """v1: rule-based gate. v2 swaps for an LLM judgement call."""
    if not user_message.strip() or not assistant_message.strip():
        return False
    lowered = assistant_message.lower()
    return any(phrase in lowered for phrase in _NOTABLE_PHRASES)


def _extract_note(user_message: str, assistant_message: str) -> str:
    """Build the MEMORY.md entry for this turn."""
    user_short = _truncate(user_message.strip().splitlines()[0], 120)
    asst_short = _truncate(assistant_message.strip().splitlines()[0], 200)
    return f"- USER said: {user_short}\n  AGENT noted: {asst_short}"


@dataclass(slots=True)
class PostResponseReviewer:
    """Decide whether a finished turn produced anything worth filing.

    `review(user_message, assistant_message)` returns a ReviewResult. The
    AgentLoop calls `spawn_review(...)` to run it as a background task.
    """

    memory: MemoryManager
    is_reviewer: bool = False  # recursion guard — propagated by spawned tasks

    def review(self, *, user_message: str, assistant_message: str) -> ReviewResult:
        """Synchronously decide + write. Idempotent against duplicate notes."""
        if self.is_reviewer:
            # The reviewer agent never spawns another reviewer.
            return ReviewResult(noted=False, skipped_reason="reviewer-recursion-blocked")

        if not _should_note(user_message, assistant_message):
            return ReviewResult(noted=False, skipped_reason="not-notable")

        note = _extract_note(user_message, assistant_message)
        if note in _RECENT_NOTES:
            return ReviewResult(noted=False, skipped_reason="duplicate")

        try:
            self.memory.append_declarative(note)
        except Exception as e:  # noqa: BLE001
            logger.warning("reviewer append failed: %s", e)
            return ReviewResult(noted=False, skipped_reason=f"write-failed: {e}")

        _RECENT_NOTES.append(note)
        if len(_RECENT_NOTES) > _RECENT_NOTES_MAX:
            del _RECENT_NOTES[0 : len(_RECENT_NOTES) - _RECENT_NOTES_MAX]
        return ReviewResult(noted=True, note_text=note)

    def spawn_review(
        self, *, user_message: str, assistant_message: str
    ) -> asyncio.Task[ReviewResult]:
        """Fire-and-forget: returns the Task so callers MAY await it (tests do)
        but the AgentLoop itself never does."""

        async def _run() -> ReviewResult:
            try:
                return self.review(
                    user_message=user_message,
                    assistant_message=assistant_message,
                )
            except Exception as e:  # noqa: BLE001
                # Reviewer must NEVER raise back into the user-facing flow.
                logger.exception("reviewer crashed: %s", e)
                return ReviewResult(noted=False, skipped_reason=f"crashed: {e}")

        return asyncio.create_task(_run())


def _last_user_and_assistant(messages: Iterable[Message]) -> tuple[str, str]:
    """Walk back through messages → return (last user content, last assistant content).
    Returns empty strings if either is missing."""
    user_text = ""
    asst_text = ""
    for m in reversed(list(messages)):
        if not asst_text and m.role == "assistant" and m.content:
            asst_text = m.content
        elif not user_text and m.role == "user" and m.content:
            user_text = m.content
        if user_text and asst_text:
            break
    return user_text, asst_text


__all__ = [
    "PostResponseReviewer",
    "ReviewResult",
    "_extract_note",
    "_last_user_and_assistant",
    "_should_note",
]
