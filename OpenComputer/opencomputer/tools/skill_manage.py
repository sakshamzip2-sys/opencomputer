"""
skill_manage — the self-improvement tool.

The agent calls this after completing complex tasks to save the approach
as a reusable skill. On the next relevant conversation, the skill's
description auto-activates and its body enters the system prompt.

Inspired by hermes's tools/skill_manager_tool.py. Trimmed: only the
actions we actually need (create/edit/patch/delete/view/list).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import frontmatter

from opencomputer.agent.config import default_config
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


_VALID_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _skills_root() -> Path:
    return default_config().memory.skills_path


def _skill_dir(skill_id: str) -> Path:
    return _skills_root() / skill_id


def _validate_id(skill_id: str) -> str | None:
    if not skill_id:
        return "Error: skill id is required"
    if not _VALID_ID.match(skill_id):
        return f"Error: skill id '{skill_id}' must be kebab-case (lowercase, hyphens only)"
    return None


def _validate_frontmatter(body: str) -> str | None:
    if not body.strip():
        return "Error: body is empty"
    try:
        post = frontmatter.loads(body)
    except Exception as e:  # noqa: BLE001
        return f"Error parsing frontmatter: {e}"
    meta = post.metadata
    if "name" not in meta:
        return "Error: frontmatter must include 'name'"
    if "description" not in meta:
        return "Error: frontmatter must include 'description'"
    if not str(meta.get("description", "")).strip():
        return "Error: description cannot be empty"
    return None


class SkillManageTool(BaseTool):
    parallel_safe = False  # writes to disk

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="skill_manage",
            description=(
                "Manage skills (procedural memory). Call this AFTER completing a complex task "
                "(5+ tool calls, tricky error, non-trivial workflow) to save the approach as a "
                "reusable skill. Skills are auto-activated on relevant future queries.\n\n"
                "Also use to fix outdated/wrong skills — patch them immediately when you "
                "notice something is off.\n\n"
                "Actions:\n"
                "  create — make a new skill with full SKILL.md content (frontmatter + body)\n"
                "  edit   — fully rewrite an existing skill\n"
                "  patch  — targeted find/replace within an existing skill\n"
                "  delete — remove a skill\n"
                "  view   — read a skill's contents\n"
                "  list   — list all installed skills"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "edit", "patch", "delete", "view", "list"],
                        "description": "What to do",
                    },
                    "name": {
                        "type": "string",
                        "description": "Skill id (kebab-case). Not required for list.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Full SKILL.md content for create/edit (must include frontmatter "
                            "with name + description)."
                        ),
                    },
                    "find": {
                        "type": "string",
                        "description": "For patch action: exact text to find.",
                    },
                    "replace": {
                        "type": "string",
                        "description": "For patch action: replacement text.",
                    },
                },
                "required": ["action"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        action = args.get("action", "").lower()
        name = args.get("name", "")

        if action == "list":
            return self._list(call.id)
        if action == "view":
            return self._view(call.id, name)
        if action == "create":
            return self._create(call.id, name, args.get("content", ""))
        if action == "edit":
            return self._edit(call.id, name, args.get("content", ""))
        if action == "patch":
            return self._patch(
                call.id, name, args.get("find", ""), args.get("replace", "")
            )
        if action == "delete":
            return self._delete(call.id, name)
        return ToolResult(
            tool_call_id=call.id,
            content=f"Error: unknown action '{action}'",
            is_error=True,
        )

    # ─── actions ──────────────────────────────────────────────────

    def _list(self, call_id: str) -> ToolResult:
        root = _skills_root()
        if not root.exists():
            return ToolResult(tool_call_id=call_id, content="no skills installed")
        lines: list[str] = []
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                post = frontmatter.load(skill_md)
                desc = post.metadata.get("description", "")
            except Exception:
                desc = "[failed to parse]"
            lines.append(f"- {d.name}: {desc}")
        return ToolResult(
            tool_call_id=call_id,
            content="\n".join(lines) or "no skills installed",
        )

    def _view(self, call_id: str, name: str) -> ToolResult:
        if err := _validate_id(name):
            return ToolResult(tool_call_id=call_id, content=err, is_error=True)
        skill_md = _skill_dir(name) / "SKILL.md"
        if not skill_md.exists():
            return ToolResult(
                tool_call_id=call_id,
                content=f"Error: skill '{name}' not found",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call_id, content=skill_md.read_text(encoding="utf-8")
        )

    def _create(self, call_id: str, name: str, content: str) -> ToolResult:
        if err := _validate_id(name):
            return ToolResult(tool_call_id=call_id, content=err, is_error=True)
        if err := _validate_frontmatter(content):
            return ToolResult(tool_call_id=call_id, content=err, is_error=True)
        skill_dir = _skill_dir(name)
        if skill_dir.exists():
            return ToolResult(
                tool_call_id=call_id,
                content=f"Error: skill '{name}' already exists — use action='edit' or 'patch'",
                is_error=True,
            )
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return ToolResult(
            tool_call_id=call_id,
            content=f"Created skill '{name}' at {skill_dir}",
        )

    def _edit(self, call_id: str, name: str, content: str) -> ToolResult:
        if err := _validate_id(name):
            return ToolResult(tool_call_id=call_id, content=err, is_error=True)
        if err := _validate_frontmatter(content):
            return ToolResult(tool_call_id=call_id, content=err, is_error=True)
        skill_md = _skill_dir(name) / "SKILL.md"
        if not skill_md.exists():
            return ToolResult(
                tool_call_id=call_id,
                content=f"Error: skill '{name}' not found — use action='create' to add it",
                is_error=True,
            )
        skill_md.write_text(content, encoding="utf-8")
        return ToolResult(
            tool_call_id=call_id, content=f"Updated skill '{name}'"
        )

    def _patch(self, call_id: str, name: str, find: str, replace: str) -> ToolResult:
        if err := _validate_id(name):
            return ToolResult(tool_call_id=call_id, content=err, is_error=True)
        if not find:
            return ToolResult(
                tool_call_id=call_id,
                content="Error: patch requires 'find' string",
                is_error=True,
            )
        skill_md = _skill_dir(name) / "SKILL.md"
        if not skill_md.exists():
            return ToolResult(
                tool_call_id=call_id,
                content=f"Error: skill '{name}' not found",
                is_error=True,
            )
        text = skill_md.read_text(encoding="utf-8")
        if find not in text:
            return ToolResult(
                tool_call_id=call_id,
                content=f"Error: 'find' string not present in skill '{name}'",
                is_error=True,
            )
        if text.count(find) > 1:
            return ToolResult(
                tool_call_id=call_id,
                content=(
                    f"Error: 'find' string appears {text.count(find)} times in skill "
                    f"'{name}' — be more specific"
                ),
                is_error=True,
            )
        new_text = text.replace(find, replace)
        skill_md.write_text(new_text, encoding="utf-8")
        return ToolResult(
            tool_call_id=call_id, content=f"Patched skill '{name}' (1 replacement)"
        )

    def _delete(self, call_id: str, name: str) -> ToolResult:
        if err := _validate_id(name):
            return ToolResult(tool_call_id=call_id, content=err, is_error=True)
        skill_dir = _skill_dir(name)
        if not skill_dir.exists():
            return ToolResult(
                tool_call_id=call_id,
                content=f"Error: skill '{name}' not found",
                is_error=True,
            )
        import shutil

        shutil.rmtree(skill_dir)
        return ToolResult(tool_call_id=call_id, content=f"Deleted skill '{name}'")


__all__ = ["SkillManageTool"]
