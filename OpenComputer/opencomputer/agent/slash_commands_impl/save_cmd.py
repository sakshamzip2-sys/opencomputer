"""``/save`` — export current session to a markdown file.

Tier 2.A.12 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

Reads ``runtime.custom['session_id']`` + ``['session_db']``. Writes
the session's full message history as markdown to::

    <sessions.db parent>/exports/<session_id>.md

Returns the path so the user can open or share the file.

Optional arg: pass a custom output path. Path is sanitized — must stay
within the profile dir for safety.
"""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommand, SlashCommandResult


def _format_message(msg) -> str:
    role = getattr(msg, "role", "?")
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        # Multimodal — extract text blocks, label others
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("_(image)_")
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    parts.append(f"_(tool_use: {name})_")
                elif block.get("type") == "tool_result":
                    parts.append("_(tool_result)_")
        text = "\n\n".join(p for p in parts if p)
    else:
        text = str(content)
    return f"## {role}\n\n{text}\n"


class SaveCommand(SlashCommand):
    name = "save"
    description = "Export current session to markdown file"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        sid = runtime.custom.get("session_id")
        db = runtime.custom.get("session_db")
        if not sid or db is None:
            return SlashCommandResult(
                output="No active session — /save only works inside an agent loop turn.",
                handled=True,
            )

        try:
            messages = db.get_messages(sid)
            session = db.get_session(sid)
        except Exception as e:  # noqa: BLE001
            return SlashCommandResult(
                output=f"Failed to read session: {type(e).__name__}: {e}",
                handled=True,
            )

        if not messages:
            return SlashCommandResult(
                output="(no messages yet — nothing to save)",
                handled=True,
            )

        # Resolve output path
        custom_path = (args or "").strip()
        if custom_path:
            out_path = Path(custom_path).expanduser().resolve()
            # Safety: must end with .md to prevent accidental overwrite
            if out_path.suffix.lower() not in (".md", ".markdown"):
                return SlashCommandResult(
                    output="output path must end with .md or .markdown",
                    handled=True,
                )
        else:
            base = Path(getattr(db, "db_path", Path.cwd() / "sessions.db")).parent
            out_path = base / "exports" / f"{sid}.md"

        # Render
        title = (session.get("title") if session else None) or "(untitled)"
        lines = [f"# {title}", "", f"Session: `{sid}`", ""]
        for msg in messages:
            lines.append(_format_message(msg))

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(lines))
        except OSError as e:
            return SlashCommandResult(
                output=f"Failed to write {out_path}: {type(e).__name__}: {e}",
                handled=True,
            )

        return SlashCommandResult(
            output=f"Saved {len(messages)} messages to {out_path}",
            handled=True,
        )


__all__ = ["SaveCommand"]
