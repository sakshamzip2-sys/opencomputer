"""Recall tool — agent-facing search + write into the three-pillar memory.

Phase 11d added the episodic schema (`SessionDB.search_episodic`, the per-turn
event log) and the `opencomputer recall` CLI. Phase 12a closes the loop: the
**model itself** can now query and write memory mid-turn via this tool.

Companion to the declarative `MemoryTool` (10f.D) — that one does CRUD on
MEMORY.md/USER.md; this one does FTS search over episodic + message history
plus `note` (long-form declarative appends) and `recall_session` (fetch a
prior session's messages by id prefix).

Three actions:

- `search` — full-text query across BOTH episodic events (per-turn summaries)
  and raw message contents from prior sessions. Uses the existing FTS5 indexes.
- `note` — append a free-form note to MEMORY.md (declarative pillar). Used by
  the agent when something is worth remembering across sessions long-term.
- `recall_session` — fetch the message history of a specific past session, so
  the agent can quote / continue from it.

Source: hermes-agent's memory_tool pattern + tools/skill_manage.py shape.
Reuses SessionDB.search / search_episodic / get_messages and
MemoryManager.append_declarative — does not reimplement any of them.
"""

from __future__ import annotations

from typing import Any

from opencomputer.agent.config import default_config
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

DEFAULT_SEARCH_LIMIT = 10
DEFAULT_RECALL_LIMIT = 30
MAX_NOTE_CHARS = 2_000  # MEMORY.md hygiene; longer notes belong in skills


def _format_episodic_hit(h: dict[str, Any]) -> str:
    sid_short = h["session_id"][:8]
    extras: list[str] = []
    if h.get("tools_used"):
        extras.append(f"tools={h['tools_used']}")
    if h.get("file_paths"):
        extras.append(f"files={h['file_paths']}")
    suffix = f"  ({', '.join(extras)})" if extras else ""
    return f"[episodic {sid_short}…/turn-{h['turn_index']}] {h['summary']}{suffix}"


def _format_message_hit(h: dict[str, Any]) -> str:
    sid_short = h["session_id"][:8]
    return f"[message {sid_short}… {h['role']}] {h['snippet']}"


class RecallTool(BaseTool):
    """The agent's window into past sessions and long-term notes."""

    parallel_safe = True

    def __init__(
        self,
        db: SessionDB | None = None,
        memory: MemoryManager | None = None,
    ) -> None:
        # Default to the user's configured paths so the CLI can register this
        # tool without wiring; tests override both for isolation.
        if db is None or memory is None:
            cfg = default_config()
            if db is None:
                db = SessionDB(cfg.session.db_path)
            if memory is None:
                memory = MemoryManager(
                    declarative_path=cfg.memory.declarative_path,
                    skills_path=cfg.memory.skills_path,
                )
        self._db = db
        self._memory = memory

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Recall",
            description=(
                "Search and write into the agent's long-term memory across all past sessions.\n"
                "\n"
                "Use this when:\n"
                "  - The user asks 'what did I say about X' (semantic / FTS search)\n"
                "  - You want to find related past turns by topic, not exact text\n"
                "  - You're recording a fact worth carrying across all future sessions\n"
                "  - You need the FULL transcript of a known prior session by id\n"
                "\n"
                "Do NOT use this for:\n"
                "  - Reading the current session's own messages — already in context\n"
                "  - Listing sessions by recency — use SessionsList (faster, no FTS)\n"
                "  - Reading a known recent session — use SessionsHistory (faster)\n"
                "  - Direct CRUD on MEMORY.md — use Memory (faster, no embedding cost)\n"
                "\n"
                "Three actions:\n"
                "- search: find past turns/messages by text. Returns a mix of episodic "
                "summaries and raw message hits across all prior sessions.\n"
                "- note: append a fact / decision / preference to MEMORY.md so it "
                "carries across all future sessions. Use sparingly for things truly "
                "worth remembering — not for every interaction.\n"
                "- recall_session: fetch the history of a specific session by its id "
                "(returned by search). Use when you need the full context of a past "
                "conversation, not just the snippet."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "note", "recall_session"],
                        "description": "Which operation to perform.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search text (action=search).",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Note body to append to MEMORY.md (action=note). "
                            f"Capped at {MAX_NOTE_CHARS} chars."
                        ),
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Target session id (action=recall_session). Use the "
                            "8-char prefix shown in search results — full id is "
                            "looked up by prefix."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Result cap. Default {DEFAULT_SEARCH_LIMIT} for "
                            f"search, {DEFAULT_RECALL_LIMIT} for recall_session."
                        ),
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        action = str(call.arguments.get("action", "")).strip()
        if action == "search":
            return self._do_search(call)
        if action == "note":
            return self._do_note(call)
        if action == "recall_session":
            return self._do_recall(call)
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Error: unknown action {action!r}. Use one of: search, note, recall_session."
            ),
            is_error=True,
        )

    # ─── per-action handlers ───────────────────────────────────────────

    def _do_search(self, call: ToolCall) -> ToolResult:
        query = str(call.arguments.get("query", "")).strip()
        if not query:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: action=search requires query.",
                is_error=True,
            )
        limit = int(call.arguments.get("limit", DEFAULT_SEARCH_LIMIT))
        # Round 4 Item 1 — opt-out per call. Default is None which lets
        # the synthesizer respect OPENCOMPUTER_RECALL_SYNTHESIS env var.
        synthesize_arg = call.arguments.get("synthesize")
        synthesize: bool | None = (
            None if synthesize_arg is None else bool(synthesize_arg)
        )

        # Prefer episodic hits — they're denser per-row than raw messages.
        episodic = self._db.search_episodic(query, limit=limit)
        remaining = max(0, limit - len(episodic))
        message_hits = self._db.search(query, limit=remaining) if remaining else []

        if not episodic and not message_hits:
            return ToolResult(
                tool_call_id=call.id,
                content=f"No memory matches for {query!r}.",
            )

        lines: list[str] = []
        if episodic:
            lines.append(f"## Episodic ({len(episodic)})")
            lines.extend(_format_episodic_hit(h) for h in episodic)
        if message_hits:
            if lines:
                lines.append("")
            lines.append(f"## Messages ({len(message_hits)})")
            lines.extend(_format_message_hit(h) for h in message_hits)
        raw_content = "\n".join(lines)

        # Synthesis pass — converts the raw FTS5 dump into a focused
        # 1-3 sentence answer with citations. Returns None when:
        # disabled, <3 candidates, LLM unreachable, or citations
        # don't validate. None means "show raw" — never silent loss.
        synthesis = self._maybe_synthesize(query, episodic, message_hits, synthesize)
        content = (
            "## Synthesis\n" + synthesis + "\n\n" + raw_content
            if synthesis
            else raw_content
        )
        return ToolResult(tool_call_id=call.id, content=content)

    def _maybe_synthesize(
        self,
        query: str,
        episodic: list[dict],
        message_hits: list[dict],
        synthesize: bool | None,
    ) -> str | None:
        """Build candidate list + run the synthesizer. Returns the
        synthesised string or None on any skip/failure path. Never
        raises — the recall tool keeps working even if the synthesizer
        module isn't importable (e.g. broken install)."""
        try:
            from opencomputer.agent.recall_synthesizer import (
                RecallCandidate,
                synthesize_recall,
            )
        except Exception:  # noqa: BLE001 — defence-in-depth
            return None

        candidates: list = []
        for h in episodic:
            candidates.append(
                RecallCandidate(
                    kind="episodic",
                    id=str(h.get("id", "")),
                    session_id=str(h.get("session_id", "")),
                    turn_index=(
                        int(h["turn_index"]) if h.get("turn_index") is not None else None
                    ),
                    text=str(h.get("summary", "")),
                )
            )
        for h in message_hits:
            sid = str(h.get("session_id", ""))
            ts = h.get("timestamp", 0)
            candidates.append(
                RecallCandidate(
                    kind="message",
                    id=f"{sid[:8]}@{ts}",
                    session_id=sid,
                    turn_index=None,
                    text=str(h.get("snippet", "")),
                )
            )
        try:
            return synthesize_recall(query, candidates, synthesize=synthesize)
        except Exception:  # noqa: BLE001
            return None

    def _do_note(self, call: ToolCall) -> ToolResult:
        text = str(call.arguments.get("text", "")).strip()
        if not text:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: action=note requires text.",
                is_error=True,
            )
        if len(text) > MAX_NOTE_CHARS:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: note exceeds {MAX_NOTE_CHARS} chars "
                    f"(got {len(text)}). Split into multiple notes or write a skill."
                ),
                is_error=True,
            )
        self._memory.append_declarative(text)
        return ToolResult(
            tool_call_id=call.id,
            content=f"Noted to {self._memory.declarative_path}: {text[:80]}…"
            if len(text) > 80
            else f"Noted to {self._memory.declarative_path}: {text}",
        )

    def _do_recall(self, call: ToolCall) -> ToolResult:
        sid_input = str(call.arguments.get("session_id", "")).strip()
        if not sid_input:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: action=recall_session requires session_id.",
                is_error=True,
            )
        limit = int(call.arguments.get("limit", DEFAULT_RECALL_LIMIT))

        # Allow an 8-char prefix; resolve to the full id via list_sessions.
        target_sid = self._resolve_session_prefix(sid_input)
        if target_sid is None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: no session found with id starting {sid_input!r}. "
                    "Use Recall(action=search, ...) first."
                ),
                is_error=True,
            )

        messages = self._db.get_messages(target_sid)
        if not messages:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Session {target_sid[:8]}… has no recorded messages.",
            )

        # Truncate from the END (most recent kept).
        truncated = messages[-limit:]
        prefix = (
            f"[truncated — showing last {limit} of {len(messages)}]\n"
            if len(messages) > limit
            else ""
        )
        lines = [prefix.rstrip(), f"# Session {target_sid[:8]}…", ""]
        for m in truncated:
            body = m.content or ""
            if len(body) > 400:
                body = body[:400] + "…"
            lines.append(f"## {m.role}")
            lines.append(body)
            lines.append("")
        return ToolResult(tool_call_id=call.id, content="\n".join(lines).strip())

    def _resolve_session_prefix(self, prefix: str) -> str | None:
        """Find a session whose id starts with `prefix`. None if no match or ambiguous."""
        # Exact id wins.
        if self._db.get_session(prefix) is not None:
            return prefix
        # Prefix scan over recent sessions only (cap 200 — searched by prefix
        # so we don't pull every session ever).
        candidates = [
            row["id"] for row in self._db.list_sessions(limit=200) if row["id"].startswith(prefix)
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None


__all__ = ["MAX_NOTE_CHARS", "RecallTool"]
