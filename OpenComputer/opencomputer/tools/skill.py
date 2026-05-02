"""Skill (invocable) tool — reads the body of a named skill so the model can
follow it on demand.

`SkillManage` already covers create / update / delete of skills. This tool is
the *read* counterpart: the model invokes it with `name=<skill-id>` and gets
back the full SKILL.md body (frontmatter stripped). The agent then follows
the steps inside without the user having to paste them in.

Source: claude-code's `Skill` tool. Pairs naturally with the SkillManage
write-side already in core.
"""

from __future__ import annotations

from opencomputer.agent.config import default_config
from opencomputer.agent.memory import MemoryManager
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class SkillTool(BaseTool):
    parallel_safe = True
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True

    def __init__(self, memory_manager: MemoryManager | None = None) -> None:
        # Default to the user's configured paths when no override supplied so
        # tests can pass an isolated MemoryManager without touching real disk.
        if memory_manager is None:
            cfg = default_config()
            memory_manager = MemoryManager(
                cfg.memory.declarative_path, cfg.memory.skills_path
            )
        self._mem = memory_manager

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Skill",
            description=(
                "Load a saved skill by id and follow its steps. Returns the full "
                "SKILL.md body (frontmatter stripped). Use this when a previously-"
                "captured procedural memory matches the current task — the skill body "
                "tells you HOW. Discover names via `opencomputer skills` or skim your "
                "skill index. Prefer Skill over re-deriving a workflow from scratch when "
                "a relevant skill exists. To create or amend a skill, use SkillManage "
                "instead. If the skill turns out to be wrong/stale, patch it via "
                "SkillManage immediately rather than working around it silently."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill id (the directory name under skills/).",
                    },
                },
                "required": ["name"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        name = str(call.arguments.get("name", "")).strip()
        if not name:
            return ToolResult(
                tool_call_id=call.id, content="Error: name is required", is_error=True
            )

        # First, scan all skill roots so user + bundled skills both resolve.
        available = {s.id: s for s in self._mem.list_skills()}
        meta = available.get(name)
        if meta is None:
            ids = ", ".join(sorted(available.keys())) or "(none)"
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: skill {name!r} not found. Available: {ids}",
                is_error=True,
            )

        # `load_skill_body` only checks the user skills_path. For bundled skills
        # we need to read directly from the resolved meta.path so this works
        # for both bundled and user skills.
        try:
            import frontmatter

            post = frontmatter.load(meta.path)
            body = post.content
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error reading skill {name!r}: {type(e).__name__}: {e}",
                is_error=True,
            )

        return ToolResult(
            tool_call_id=call.id,
            content=f"# Skill: {meta.name}\n\n{body}",
        )


__all__ = ["SkillTool"]
