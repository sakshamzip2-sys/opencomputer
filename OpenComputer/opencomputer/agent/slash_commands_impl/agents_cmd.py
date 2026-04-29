"""``/agents`` (alias /tasks) — show active detached tasks.

Tier 2.A.14 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Queries TaskStore (which lives in the same sessions.db as the agent
loop's SessionDB) for queued + running tasks and renders them inline.
"""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def _truncate(text: str, n: int = 80) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


class AgentsCommand(SlashCommand):
    name = "agents"
    description = "List active detached tasks (queued + running)"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        db = runtime.custom.get("session_db")
        if db is None:
            return SlashCommandResult(
                output="No active session — /agents only works inside an agent loop turn.",
                handled=True,
            )

        db_path = getattr(db, "db_path", None)
        if db_path is None:
            return SlashCommandResult(
                output="Cannot resolve task store path from session DB.",
                handled=True,
            )

        try:
            from opencomputer.tasks.store import TaskStore
            store = TaskStore(Path(db_path))
            queued = store.list_queued(limit=20)
            running = store.list_running()
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Failed to read task store: {type(e).__name__}: {e}",
                handled=True,
            )

        if not queued and not running:
            return SlashCommandResult(
                output="(no detached tasks active)",
                handled=True,
            )

        lines: list[str] = []
        if running:
            lines.append(f"## Running ({len(running)})")
            for t in running:
                prompt = getattr(t, "prompt", "") or ""
                tid = getattr(t, "id", "?")
                lines.append(f"  • {tid[:8]}  {_truncate(prompt)}")
            lines.append("")
        if queued:
            lines.append(f"## Queued ({len(queued)})")
            for t in queued:
                prompt = getattr(t, "prompt", "") or ""
                tid = getattr(t, "id", "?")
                lines.append(f"  • {tid[:8]}  {_truncate(prompt)}")

        return SlashCommandResult(output="\n".join(lines), handled=True)


__all__ = ["AgentsCommand"]
