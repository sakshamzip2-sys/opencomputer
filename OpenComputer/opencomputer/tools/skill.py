"""Skill (invocable) tool — reads the body of a named skill so the model can
follow it on demand.

`SkillManage` already covers create / update / delete of skills. This tool is
the *read* counterpart: the model invokes it with `name=<skill-id>` and gets
back the full SKILL.md body (frontmatter stripped). The agent then follows
the steps inside without the user having to paste them in.

Source: claude-code's `Skill` tool. Pairs naturally with the SkillManage
write-side already in core.

v1.1 plan-2 M4.3 + M4.4 (2026-05-09): SKILL.md frontmatter now drives
delegation behaviour. Supported fields:

* ``context: fork|inline`` — ``inline`` (default) returns the body
  for the parent agent to follow inline. ``fork`` synthesises a
  ``delegate(task=body, ...)`` call so the steps run in a fresh
  subagent and only a final summary returns to the parent.
* ``agent: <template-name>`` — for ``fork``, picks a registered
  agent template (system prompt + tool allowlist). Ignored for
  ``inline`` (parent's prompt + tools already apply).
* ``tools: [name, ...]`` — for ``fork``, becomes the subagent's
  ``allowed_tools``. For ``inline``, the body is annotated with a
  ``[Skill Tools Constraint]`` directive (model is asked to honor
  it; not enforced — the only safe enforcement is ``context: fork``).
* ``model: <id>`` — only honored under ``fork``. ``context: inline``
  + ``model`` raises :class:`SkillModelOverrideRequiresForkError`
  at execution time so the divergence doesn't silently slip past.
* ``isolation: none|worktree|copy`` — for ``fork``, threaded into
  the synthesised delegate call (M4.1/M4.2). Defaults to ``none``.
"""

from __future__ import annotations

import uuid
from typing import Any

from opencomputer.agent.config import default_config
from opencomputer.agent.memory import MemoryManager
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class SkillModelOverrideRequiresForkError(RuntimeError):
    """Raised when SKILL.md sets ``model:`` without ``context: fork``.

    Inline skills run inside the parent agent's loop; switching the
    provider mid-turn is invasive and not supported. The opt-in path
    is to set ``context: fork`` so the model override applies to a
    fresh subagent loop.
    """


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

        # Parse frontmatter once. Both inline and fork paths read it.
        try:
            import frontmatter

            post = frontmatter.load(meta.path)
            body = post.content
            metadata = dict(post.metadata) if post.metadata else {}
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error reading skill {name!r}: {type(e).__name__}: {e}",
                is_error=True,
            )

        # M4.3: dispatch on ``context`` field. ``fork`` ⇒ delegate.
        context_mode = str(metadata.get("context", "inline")).strip().lower()
        if context_mode not in ("inline", "fork"):
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: skill {name!r} declares unknown "
                    f"context={context_mode!r}; expected 'inline' or 'fork'."
                ),
                is_error=True,
            )

        if context_mode == "fork":
            return await self._dispatch_fork(call.id, meta.name, body, metadata)

        # ``inline`` path — current behavior + optional advisory tools constraint.
        # M4.4 (degraded): the parent agent's loop has no per-skill enforcement
        # surface, so an explicit ``tools:`` list is rendered as a directive
        # the model is asked to follow rather than a hard allowlist. The hard
        # path is ``context: fork`` where the constraint flows to delegate.
        if metadata.get("model"):
            raise SkillModelOverrideRequiresForkError(
                f"skill {name!r} sets model={metadata['model']!r} but "
                f"context is 'inline'. model overrides require context: fork "
                f"so the new provider applies to a fresh subagent loop."
            )

        body_with_constraints = self._render_inline_body(meta.name, body, metadata)
        return ToolResult(
            tool_call_id=call.id,
            content=body_with_constraints,
        )

    @staticmethod
    def _render_inline_body(
        skill_name: str, body: str, metadata: dict[str, Any]
    ) -> str:
        """Wrap the skill body with optional advisory directives.

        For ``inline`` mode the parent agent will execute the steps
        itself, so we can only ASK the model to honor a tool allowlist.
        For hard enforcement use ``context: fork``.
        """
        lines = [f"# Skill: {skill_name}"]
        tools_list = metadata.get("tools")
        if isinstance(tools_list, list) and tools_list:
            tools_str = ", ".join(str(t) for t in tools_list)
            lines.append("")
            lines.append(
                f"[Skill Tools Constraint] While following this skill, prefer "
                f"using only these tools: {tools_str}. (Advisory — not enforced; "
                f"set context: fork in the SKILL.md frontmatter for hard "
                f"enforcement via subagent allowlist.)"
            )
        lines.append("")
        lines.append(body)
        return "\n".join(lines)

    async def _dispatch_fork(
        self,
        call_id: str,
        skill_name: str,
        body: str,
        metadata: dict[str, Any],
    ) -> ToolResult:
        """Synthesise a ``delegate`` call from SKILL.md frontmatter.

        M4.3 — the skill body becomes the subagent's task, frontmatter
        ``agent``/``tools``/``model``/``isolation`` flow through as
        delegate's parameters. Subagent's final-message content
        becomes this skill call's return value.
        """
        from opencomputer.tools.delegate import DelegateTool

        delegate_args: dict[str, Any] = {"task": body}
        if metadata.get("agent"):
            delegate_args["agent"] = str(metadata["agent"]).strip()
        tools = metadata.get("tools")
        if isinstance(tools, list):
            delegate_args["allowed_tools"] = [str(t) for t in tools]
        if metadata.get("isolation"):
            iso = str(metadata["isolation"]).strip().lower()
            if iso not in ("none", "worktree", "copy"):
                return ToolResult(
                    tool_call_id=call_id,
                    content=(
                        f"Error: skill {skill_name!r} declares unknown "
                        f"isolation={iso!r}; expected none/worktree/copy."
                    ),
                    is_error=True,
                )
            delegate_args["isolation"] = iso
        # ``model`` is honoured under fork only. We don't yet plumb a
        # per-call provider override into delegate (the whole subagent
        # inherits its parent's provider). Document the metadata
        # acceptance so the field is reserved for the future plumbing.
        # For now: ignore silently if set under fork (no error since
        # the user did the right thing by using fork).
        delegate_call = ToolCall(
            id=f"{call_id}-skill-fork-{uuid.uuid4().hex[:8]}",
            name="delegate",
            arguments=delegate_args,
        )
        delegate_result = await DelegateTool().execute(delegate_call)
        # Wrap so the caller can tell this came from a skill.
        prefix = f"# Skill: {skill_name} (forked)\n\n"
        content = (delegate_result.content or "") if delegate_result else ""
        return ToolResult(
            tool_call_id=call_id,
            content=prefix + content,
            is_error=delegate_result.is_error if delegate_result else False,
        )


__all__ = ["SkillTool", "SkillModelOverrideRequiresForkError"]
