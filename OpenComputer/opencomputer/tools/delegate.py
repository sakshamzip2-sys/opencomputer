"""
delegate — spawn a fresh subagent in an isolated context.

Used when the main agent wants to offload a big exploration task without
polluting its own context. The subagent gets a fresh system prompt +
whatever briefing the main agent writes, runs its own while-loop, and
returns a single text summary.

Phase 1.5 stub: uses a simple approach where the subagent shares the
provider + tool registry, but keeps its own conversation messages.
Later phases can add context isolation, tool restrictions, etc.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext
from plugin_sdk.tool_contract import BaseTool, ToolSchema

if TYPE_CHECKING:
    from opencomputer.agent.agent_templates import AgentTemplate


DELEGATE_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "delegate",          # no recursive delegation (depth check is the second line of defense)
    "AskUserQuestion",   # subagent has no user
    "ExitPlanMode",      # subagent doesn't own plan mode
})
"""Tools the parent must NEVER pass to a subagent. Caller-supplied
`allowed_tools` containing any of these is a hard error; implicit-inherit
strips them. Mirrors Hermes `DELEGATE_BLOCKED_TOOLS`."""


class DelegateTool(BaseTool):
    parallel_safe = True  # each delegate gets its own loop instance

    # Lazy-import a factory the CLI can inject; until then raise a clear error
    _factory = None
    #: Class-level "current runtime" set by the parent loop before dispatching
    #: tool calls. Ensures subagent loops inherit plan_mode / yolo_mode, etc.
    _current_runtime: RuntimeContext = DEFAULT_RUNTIME_CONTEXT
    #: III.5 — registered subagent templates. Populated at CLI startup via
    #: ``set_templates(discover_agents(...))``. Empty until then, so a bare
    #: ``agent`` argument without prior registration surfaces a clean error
    #: listing available names (of which there are none, yet). Mirrors the
    #: Claude Code concept of pre-registered named subagents from
    #: ``sources/claude-code/plugins/<plugin>/agents/*.md``.
    _templates: dict[str, AgentTemplate] = {}

    @classmethod
    def set_factory(cls, factory) -> None:
        """Inject a callable that returns a fresh AgentLoop. Called once at CLI startup."""
        # staticmethod wrap prevents Python from binding `self` when we later do
        # `self._factory()` on an instance — lambdas and plain functions would
        # otherwise get `self` auto-injected.
        cls._factory = staticmethod(factory)

    @classmethod
    def set_runtime(cls, runtime: RuntimeContext) -> None:
        """Set the runtime context to propagate into subagents. Called by AgentLoop."""
        cls._current_runtime = runtime

    @classmethod
    def set_templates(cls, templates: dict[str, AgentTemplate]) -> None:
        """Register the discovered agent templates.

        III.5 — called once at CLI startup after
        :func:`opencomputer.agent.agent_templates.discover_agents` runs.
        A second call REPLACES the registry (so per-profile CLI invocations
        don't leak templates from a previous process state in long-lived
        test harnesses). Passing an empty dict clears the registry.
        """
        cls._templates = dict(templates)

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="delegate",
            description=(
                "Spawn a fresh subagent with isolated context to handle a specific task. "
                "Use this when you need to do heavy exploration (reading many files, searching "
                "code) and only want a summary back instead of polluting the main conversation. "
                "The subagent runs until it produces a final answer, then returns its output."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Describe the task for the subagent completely. The subagent has "
                            "no memory of the main conversation — include all context it needs."
                        ),
                    },
                    # III.1 tool allowlist. Mirrors Claude Code's
                    # ``allowed-tools:`` command frontmatter
                    # (sources/claude-code/plugins/code-review/commands/
                    # code-review.md) applied to OpenComputer's actual
                    # tool-dispatching surface (subagent spawn).
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional. Restrict the subagent to these tool names. "
                            "Omit or pass null for the parent's full tool set "
                            "(existing behavior). An empty list means no tools — "
                            "use for pure-reasoning delegations with zero side effects."
                        ),
                    },
                    # III.5: pre-registered agent templates. Mirrors Claude
                    # Code's subagent-definition pattern from
                    # ``sources/claude-code/plugins/<plugin>/agents/*.md``.
                    "agent": {
                        "type": "string",
                        "description": (
                            "Optional. Name of a registered agent template "
                            "(from `opencomputer agents list`). Applies its "
                            "system-prompt + tool allowlist + model override. "
                            "Omit for default delegation."
                        ),
                    },
                },
                "required": ["task"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        task = call.arguments.get("task", "").strip()
        if not task:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: task description required",
                is_error=True,
            )
        if self._factory is None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Error: delegate is not initialized. "
                    "CLI bootstrapping must call DelegateTool.set_factory(...)."
                ),
                is_error=True,
            )
        # T1.1: enforce max delegation depth
        parent_loop = getattr(self._factory, "__self__", None)
        max_depth = 2  # default mirrors Hermes
        if parent_loop is not None and hasattr(parent_loop, "config"):
            max_depth = getattr(parent_loop.config.loop, "max_delegation_depth", 2)
        if self._current_runtime.delegation_depth >= max_depth:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: max delegation depth ({max_depth}) reached. "
                    f"Refusing recursive delegation to avoid fork-bombing the agent. "
                    f"Tune via LoopConfig.max_delegation_depth."
                ),
                is_error=True,
            )
        # III.1: parse the allowlist input. ``None`` / missing → unrestricted
        # (parent's full registry); explicit ``[]`` → no tools at all; list
        # of strings → exactly those tool names.
        raw_allowed = call.arguments.get("allowed_tools")
        allowed: frozenset[str] | None
        explicit_allowed = False
        if raw_allowed is None:
            allowed = None
        elif isinstance(raw_allowed, (list, tuple, set, frozenset)):
            allowed = frozenset(str(x) for x in raw_allowed)
            explicit_allowed = True
        else:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Error: 'allowed_tools' must be a list of tool names "
                    f"(got {type(raw_allowed).__name__})."
                ),
                is_error=True,
            )

        # T1.2: enforce blocklist regardless of allowlist mode
        if allowed is not None:
            overlap = allowed & DELEGATE_BLOCKED_TOOLS
            if overlap:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"Error: 'allowed_tools' includes blocked tools: {sorted(overlap)}. "
                        f"These tools are unsafe in subagents (see DELEGATE_BLOCKED_TOOLS)."
                    ),
                    is_error=True,
                )
        # Note: when `allowed is None` (inherit-everything), blocked tools are
        # stripped at dispatch time inside the child loop's allowlist filter.
        # Push a synthetic allowlist that EXCLUDES blocked tools so the child
        # can't call them even via inherit-everything.
        if allowed is None:
            # Lazy import — registry shape may not be available at module load time
            try:
                from opencomputer.tools.registry import registry as _reg
                all_names = frozenset(_reg.names())
                allowed = all_names - DELEGATE_BLOCKED_TOOLS
            except Exception:
                # If registry isn't loaded (test/edge case), fall back to passing
                # `None` to the child — the child loop's existing allowlist filter
                # is empty/permissive then; not ideal but explicit allowed param
                # remains the recommended path.
                allowed = None

        # III.5: resolve the optional ``agent`` parameter. A registered
        # template overrides the default subagent shape: its ``tools``
        # become the allowlist (unless an explicit ``allowed_tools``
        # argument was supplied — explicit beats template), and its
        # ``system_prompt`` is passed through to the child loop verbatim.
        raw_agent = call.arguments.get("agent")
        template: AgentTemplate | None = None
        if raw_agent is not None and isinstance(raw_agent, str) and raw_agent.strip():
            agent_name = raw_agent.strip()
            template = self._templates.get(agent_name)
            if template is None:
                available = sorted(self._templates.keys())
                available_str = (
                    ", ".join(available) if available else "(no templates registered)"
                )
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        f"Error: unknown agent template {agent_name!r}. "
                        f"Available: {available_str}."
                    ),
                    is_error=True,
                )
            # Template's tool list becomes the allowlist ONLY when the
            # caller didn't supply an explicit ``allowed_tools`` argument.
            # Explicit beats template — matches III.1's "caller intent wins"
            # semantic. An empty template ``tools`` tuple means the author
            # intentionally chose "inherit parent's tool set" (documented
            # on AgentTemplate.tools).
            if not explicit_allowed and template.tools:
                allowed = frozenset(template.tools)

        # T1.1: child runtime gets incremented depth
        child_runtime = dataclasses.replace(
            self._current_runtime,
            delegation_depth=self._current_runtime.delegation_depth + 1,
        )

        subagent_loop = self._factory()
        # II.1: cap the subagent's iteration budget at the parent's
        # ``delegation_max_iterations`` (default 50) instead of letting it
        # inherit the full ``max_iterations``. Mirrors Hermes's pattern
        # (sources/hermes-agent/run_agent.py:IterationBudget lines 185-196).
        # Config/LoopConfig are frozen dataclasses — use ``dataclasses.replace``
        # to build a new LoopConfig with the override, then swap it onto the
        # child. ``dataclasses.is_dataclass`` guards against fake/mocked
        # subagents in tests that don't carry a real Config.
        child_cfg = getattr(subagent_loop, "config", None)
        if child_cfg is not None and dataclasses.is_dataclass(child_cfg):
            new_loop_cfg = dataclasses.replace(
                child_cfg.loop,
                max_iterations=child_cfg.loop.delegation_max_iterations,
            )
            subagent_loop.config = dataclasses.replace(child_cfg, loop=new_loop_cfg)
        # III.1: push the allowlist onto the child BEFORE it runs. ``None``
        # is also explicitly assigned so callers who re-use a loop factory
        # don't inherit a stale allowlist from a prior delegation.
        subagent_loop.allowed_tools = allowed
        # Propagate the parent's runtime context — plan mode, yolo mode, etc.
        # must apply to subagents too, otherwise delegating becomes an escape hatch.
        # III.5: pass the template's system_prompt to the child loop. When
        # ``template is None`` the kwarg is ``None`` and the child builds
        # its usual declarative + skills + memory + SOUL prompt (existing
        # behavior). With a template, the template BODY is the whole
        # prompt — no further injection on top.
        result = await subagent_loop.run_conversation(
            user_message=task,
            runtime=child_runtime,   # ← was self._current_runtime
            system_prompt_override=(
                template.system_prompt if template is not None else None
            ),
        )
        # D7: emit SubagentStop hook when the delegated subagent finishes
        # so plugins can log / summarize / react. Fire-and-forget.
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookContext, HookEvent

            _hook_engine.fire_and_forget(
                HookContext(
                    event=HookEvent.SUBAGENT_STOP,
                    session_id=result.session_id,
                    runtime=child_runtime,
                )
            )
        except Exception:
            # Hook emission must never break the main delegate flow.
            pass
        return ToolResult(
            tool_call_id=call.id,
            content=result.final_message.content,
        )


__all__ = ["DelegateTool", "DELEGATE_BLOCKED_TOOLS"]
