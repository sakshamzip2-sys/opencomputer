"""Episodic memory — the third pillar.

Hermes pattern: after each completed turn, record a short summary of what
happened (decision + tools used + file paths). Stored in SessionDB's new
episodic_events table with FTS5 indexing for cross-session retrieval.

v1 design: summaries are *template-generated* (no LLM call) so this is
zero-latency and zero-cost. Future v2 can add LLM-summarised events as a
config flag — the schema doesn't change.

Reads come back via:
    SessionDB.search_episodic(query)  — FTS5 across all sessions
    SessionDB.list_episodic(session_id) — newest events for one session
    `opencomputer recall QUERY`        — CLI wrapper around search_episodic

Source: hermes-agent's three-pillar memory (declarative + procedural +
episodic). Plan: phase-11-commit-list item 11d.1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message

#: Cap for the per-event summary so the FTS5 index stays useful (long
#: paragraphs dilute term frequency).
SUMMARY_MAX_CHARS = 240

#: Cap on how much assistant text we paste into the summary template.
ASSISTANT_GIST_CHARS = 80

#: Path-like substring detector — lifts file paths out of free text so
#: episodic queries like "the auth.py refactor" can find the right turn.
#: Two patterns merged with alternation so we catch:
#:   - absolute or dot-relative: /Users/x/auth.py, ./README.md, ../foo/bar.txt
#:   - bare-relative with at least one slash: tests/test_auth.py, src/foo.py
#: Bare filenames with no slash (e.g. "auth.py") are skipped — too noisy.
_PATH_RE = re.compile(
    r"(?:(?:/|\.{1,2}/)[\w./-]+\.[a-zA-Z0-9]{1,8}"
    r"|\b[\w-]+(?:/[\w-]+)+\.[a-zA-Z0-9]{1,8})"
    r"\b"
)


def _extract_paths(text: str, limit: int = 6) -> list[str]:
    """Pull up to `limit` path-shaped tokens out of a string. Order-preserving + deduped."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _PATH_RE.finditer(text):
        path = m.group(0)
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def render_template_summary(
    *, user_message: str, assistant_message: Message, tools_used: list[str]
) -> str:
    """Build a one-line summary of a turn from its inputs/outputs.

    Format: `[tools: A, B] Q: <user>... → A: <assistant gist>`
    Length-capped to SUMMARY_MAX_CHARS.
    """
    user_short = user_message.strip().splitlines()[0] if user_message.strip() else ""
    if len(user_short) > 60:
        user_short = user_short[:57] + "..."
    gist = (assistant_message.content or "").strip()
    if len(gist) > ASSISTANT_GIST_CHARS:
        gist = gist[: ASSISTANT_GIST_CHARS - 3] + "..."

    tools_part = f"[tools: {', '.join(tools_used)}] " if tools_used else ""
    summary = f"{tools_part}Q: {user_short} → A: {gist}"
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 1] + "…"
    return summary


@dataclass(slots=True)
class EpisodicMemory:
    """Thin façade over SessionDB's episodic_events for the agent loop.

    The agent loop calls `record_turn(...)` after each successful FinalResponse;
    everything else (queries, listings) goes through SessionDB directly.
    """

    db: SessionDB

    def record_turn(
        self,
        *,
        session_id: str,
        turn_index: int,
        user_message: str,
        assistant_message: Message,
        tool_messages: list[Message] | None = None,
    ) -> int:
        """Record one turn. Returns the event rowid."""
        tools_used: list[str] = []
        path_corpus_parts: list[str] = []

        if assistant_message.tool_calls:
            for tc in assistant_message.tool_calls:
                if tc.name not in tools_used:
                    tools_used.append(tc.name)
        # Earlier-turn tool dispatches in the same turn appear as role=tool.
        for tm in tool_messages or []:
            if tm.role == "tool" and tm.name and tm.name not in tools_used:
                tools_used.append(tm.name)
            path_corpus_parts.append(tm.content or "")

        path_corpus = "\n".join([assistant_message.content or "", *path_corpus_parts])
        file_paths = _extract_paths(path_corpus)

        summary = render_template_summary(
            user_message=user_message,
            assistant_message=assistant_message,
            tools_used=tools_used,
        )
        return self.db.record_episodic(
            session_id=session_id,
            turn_index=turn_index,
            summary=summary,
            tools_used=tools_used or None,
            file_paths=file_paths or None,
        )


__all__ = [
    "ASSISTANT_GIST_CHARS",
    "SUMMARY_MAX_CHARS",
    "EpisodicMemory",
    "render_template_summary",
]
