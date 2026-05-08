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
    "Clarify",           # subagent has no user (Sub-project 1.G — same reason as AskUserQuestion)
    "ExitPlanMode",      # subagent doesn't own plan mode
})
"""Tools the parent must NEVER pass to a subagent. Caller-supplied
`allowed_tools` containing any of these is a hard error; implicit-inherit
strips them. Mirrors Hermes `DELEGATE_BLOCKED_TOOLS`."""


class DelegateTool(BaseTool):
    parallel_safe = True  # each delegate gets its own loop instance
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True

    # Class-level fallbacks (legacy CLI bootstrap path). Per-instance state
    # on each DelegateTool instance takes precedence — see __init__ and the
    # _factory / _templates properties. Renamed from _factory / _templates to
    # avoid colliding with the same-named instance properties (audit Pass-2 G3).
    _factory_class_level = None
    #: Class-level "current runtime" set by the parent loop before dispatching
    #: tool calls. Ensures subagent loops inherit plan_mode / yolo_mode, etc.
    _current_runtime: RuntimeContext = DEFAULT_RUNTIME_CONTEXT
    #: III.5 — registered subagent templates. Populated at CLI startup via
    #: ``set_templates(discover_agents(...))``. Empty until then, so a bare
    #: ``agent`` argument without prior registration surfaces a clean error
    #: listing available names (of which there are none, yet). Mirrors the
    #: Claude Code concept of pre-registered named subagents from
    #: ``sources/claude-code/plugins/<plugin>/agents/*.md``.
    _templates_class_level: dict[str, AgentTemplate] = {}

    def __init__(self) -> None:
        super().__init__()
        # Per-instance state; populated by set_factory(instance=self) and
        # set_templates(instance=self) at AgentLoop construction time
        # (audit Pass-2 G3). Falls back to class-level when not explicitly
        # set on the instance — preserves legacy CLI bootstrap path that
        # calls DelegateTool.set_factory(...) once at startup.
        self._instance_factory = None
        self._instance_templates: dict | None = None

    @property
    def _factory(self):
        """Prefer instance-level factory; fall back to class-level for legacy CLI path."""
        if self._instance_factory is not None:
            return self._instance_factory
        return type(self)._factory_class_level

    @property
    def _templates(self) -> dict[str, AgentTemplate]:
        """Prefer instance-level templates; fall back to class-level for legacy CLI path."""
        if self._instance_templates is not None:
            return self._instance_templates
        return type(self)._templates_class_level

    @classmethod
    def set_factory(cls, factory, *, instance: DelegateTool | None = None) -> None:
        """Inject a callable that returns a fresh AgentLoop.

        With an explicit ``instance`` arg, sets only that instance's
        factory (preferred new path — used by the per-profile AgentLoop
        factory in Phase 2). Without an instance, sets the class-level
        fallback (legacy CLI startup path).
        """
        if instance is not None:
            instance._instance_factory = factory
        else:
            # staticmethod wrap prevents Python from binding `self` when we later do
            # `self._factory()` on an instance — lambdas and plain functions would
            # otherwise get `self` auto-injected.
            cls._factory_class_level = staticmethod(factory)

    @classmethod
    def set_runtime(cls, runtime: RuntimeContext) -> None:
        """Set the runtime context to propagate into subagents. Called by AgentLoop."""
        cls._current_runtime = runtime

    @classmethod
    def set_templates(cls, templates: dict[str, AgentTemplate], *, instance: DelegateTool | None = None) -> None:
        """Register the discovered agent templates.

        With an explicit ``instance`` arg, sets only that instance's
        templates. Without, sets the class-level fallback.

        III.5 — called once at CLI startup after
        :func:`opencomputer.agent.agent_templates.discover_agents` runs.
        A second call REPLACES the registry (so per-profile CLI invocations
        don't leak templates from a previous process state in long-lived
        test harnesses). Passing an empty dict clears the registry.
        """
        if instance is not None:
            instance._instance_templates = dict(templates)
        else:
            cls._templates_class_level = dict(templates)

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
                "additionalProperties": False,
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
                    # PR-E: file-coordination for concurrent siblings.
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional. Files this delegate will read/write. "
                            "Used for sibling-coordination — concurrent delegates with overlapping "
                            "paths serialize; non-overlapping paths run in parallel. Pass empty/null "
                            "for fire-and-forget delegations with no filesystem coordination."
                        ),
                    },
                    # Round 2B P-9: optional context fork for the child loop.
                    "forked_context": {
                        "type": "boolean",
                        "description": (
                            "If true, child receives a snapshot of the parent's "
                            "recent messages (last 5 by default). Tool_use and "
                            "tool_result pairs are preserved atomically. "
                            "Defaults to false when omitted."
                        ),
                    },
                    # Hermes parity (2026-05-08): parallel batch shape.
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "goal": {"type": "string"},
                                "context": {"type": "string"},
                                "toolsets": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["goal"],
                        },
                        "description": (
                            "Hermes parity: parallel batch. Mutually "
                            "exclusive with `task`. Each entry runs in its "
                            "own subagent with isolated context. Concurrency "
                            "capped by LoopConfig.max_concurrent_children "
                            "(default 3); batches larger than that cap return "
                            "a tool error rather than silently truncating."
                        ),
                    },
                    # Hermes parity (2026-05-08): role-based nested delegation.
                    "role": {
                        "type": "string",
                        "enum": ["leaf", "orchestrator"],
                        "description": (
                            "Hermes parity. 'leaf' (default) — subagent "
                            "cannot delegate. 'orchestrator' — subagent "
                            "retains the delegate tool, allowing nested "
                            "delegation up to LoopConfig.max_delegation_depth. "
                            "Requires LoopConfig.orchestrator_enabled=True. "
                            "Cost warning: depth=3 + concurrency=3 yields up "
                            "to 27 leaves; default depth=4 / concurrency=3 = "
                            "up to 81. Use orchestrators sparingly."
                        ),
                    },
                },
                # `task` is no longer required when `tasks` is supplied — the
                # execute() handler enforces mutual exclusion at runtime.
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # Hermes parity (2026-05-08): tasks=[...] parallel batch.
        # Mutually exclusive with single-task `task` arg.
        raw_tasks = call.arguments.get("tasks")
        raw_task = (call.arguments.get("task") or "").strip()
        if raw_tasks is not None and raw_task:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: supply either `task` OR `tasks`, not both.",
                is_error=True,
            )
        if raw_tasks is not None:
            return await self._execute_batch(call.id, raw_tasks)

        task = raw_task
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

        # Hermes parity (2026-05-08): role-based nested delegation.
        # role="orchestrator" + orchestrator_enabled=True keeps `delegate` in
        # the child's allowlist so the child can spawn its own leaves. At
        # max_depth, promote orchestrator → leaf with a warning (no point
        # giving an orchestrator role to a child that can't spawn anyway).
        raw_role = (call.arguments.get("role") or "leaf").strip().lower()
        if raw_role not in ("leaf", "orchestrator"):
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Error: 'role' must be 'leaf' or 'orchestrator' "
                    f"(got {raw_role!r})."
                ),
                is_error=True,
            )
        orchestrator_enabled = True
        if parent_loop is not None and hasattr(parent_loop, "config"):
            orchestrator_enabled = getattr(
                parent_loop.config.loop, "orchestrator_enabled", True
            )
        is_orchestrator = (raw_role == "orchestrator") and orchestrator_enabled
        if is_orchestrator and self._current_runtime.delegation_depth + 1 >= max_depth:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "delegate role=orchestrator promoted to leaf "
                "(child would be at max_depth=%d with no room to delegate)",
                max_depth,
            )
            is_orchestrator = False

        # III.1: parse the allowlist input. ``None`` / missing → unrestricted
        # (parent's full registry); explicit ``[]`` → no tools at all; list
        # of strings → exactly those tool names.
        raw_allowed = call.arguments.get("allowed_tools")
        allowed: frozenset[str] | None
        explicit_allowed = False
        if raw_allowed is None:
            allowed = None
        elif isinstance(raw_allowed, list | tuple | set | frozenset):
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

        # Hermes parity: orchestrators keep `delegate` so they can spawn
        # leaves of their own; leaves still strip it. The other entries in
        # DELEGATE_BLOCKED_TOOLS (AskUserQuestion, Clarify, ExitPlanMode)
        # remain unsafe regardless of role.
        effective_blocked = (
            DELEGATE_BLOCKED_TOOLS - {"delegate"}
            if is_orchestrator
            else DELEGATE_BLOCKED_TOOLS
        )

        # T1.2: enforce blocklist regardless of allowlist mode
        if allowed is not None:
            overlap = allowed & effective_blocked
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
                allowed = all_names - effective_blocked
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

        # PR-E: file-coordination for concurrent siblings.
        # Acquire per-path locks BEFORE the child runs. Released on exit
        # (success or exception). Empty paths list = no-op.
        raw_paths = call.arguments.get("paths") or []
        if not isinstance(raw_paths, list):
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: 'paths' must be a list of strings (got {type(raw_paths).__name__})",
                is_error=True,
            )

        # Round 2B P-9: forked-context snapshot. When the caller asks for
        # ``forked_context=true`` we hand the child the tail of the parent's
        # recent message history, so it can answer questions that need that
        # context without re-fetching everything via tools.
        #
        # Boundary safety is delegated to ``CompactionEngine._safe_split_index``
        # — that helper walks backwards until splitting will not orphan a
        # ``tool_use`` from its ``tool_result``. The Anthropic API HTTP-400s
        # if a ``tool_use`` is sent without its matching ``tool_result``, so
        # this is correctness, not stylistic.
        child_initial_messages: list = []
        if call.arguments.get("forked_context"):
            from opencomputer.agent.compaction import (  # noqa: PLC0415
                CompactionEngine,
            )

            parent_msgs = list(self._current_runtime.parent_messages or ())
            if parent_msgs:
                # ``__new__`` skips ``__init__`` (which wants a provider) so
                # we can reuse the boundary algorithm without standing up a
                # real engine. ``_safe_split_index`` itself does not touch
                # ``self`` — see opencomputer/agent/compaction.py.
                _engine = CompactionEngine.__new__(CompactionEngine)
                preserve_recent = 5
                safe_idx = _engine._safe_split_index(parent_msgs, preserve_recent)
                seed = parent_msgs[safe_idx:]
                # Filter out system messages — child has its own.
                child_initial_messages = [m for m in seed if m.role != "system"]

        # Lazy import to avoid circular dependency on coordinator at module load
        from opencomputer.tools.delegation_coordinator import (  # noqa: PLC0415
            DelegationLockTimeout,
            get_default_coordinator,
        )

        # T1.1: child runtime gets incremented depth.
        # P-9: also clear ``parent_messages`` — the snapshot has been consumed
        # into ``child_initial_messages``. The child's own loop will rewrite
        # ``parent_messages`` with ITS message history when it dispatches its
        # own delegate calls; leaving the parent's snapshot in place would
        # leak parent context into a grandchild.
        child_runtime = dataclasses.replace(
            self._current_runtime,
            delegation_depth=self._current_runtime.delegation_depth + 1,
            parent_messages=(),
        )

        subagent_loop = self._factory()
        # T70 — share the parent's CredentialPool with the child so a
        # quarantined key in one is quarantined for both. Best-effort:
        # skip if either provider isn't pool-aware. Lookup is defensive
        # in case the factory returned a mock without these attrs.
        try:
            parent_loop_for_pool = getattr(self._factory, "__self__", None)
            parent_provider = getattr(parent_loop_for_pool, "provider", None)
            child_provider = getattr(subagent_loop, "provider", None)
            if parent_provider is not None and child_provider is not None:
                inherit_credential_pool(parent_provider, child_provider)
        except Exception:  # noqa: BLE001 — never break delegation on this
            pass
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
            new_model_cfg = child_cfg.model
            # Hermes parity (2026-05-08): apply DelegationConfig overrides if
            # the parent's loop config has any non-None field. None values
            # inherit the parent's provider/model. ``base_url`` and ``api_key``
            # require plugin-level wiring (provider plugins consume their own
            # env vars / config); v1 swaps env vars at run-time so the active
            # provider picks them up. Read DelegationConfig from the PARENT
            # (not the child) — the override is configured by the user on the
            # parent loop and flows down to children at delegation time.
            parent_loop_for_delegation = getattr(self._factory, "__self__", None)
            delegation = None
            if parent_loop_for_delegation is not None and hasattr(
                parent_loop_for_delegation, "config"
            ):
                delegation = getattr(
                    parent_loop_for_delegation.config.loop, "delegation", None
                )
            if delegation is not None:
                replace_kwargs: dict = {}
                if delegation.model:
                    replace_kwargs["model"] = delegation.model
                if delegation.provider:
                    replace_kwargs["provider"] = delegation.provider
                if replace_kwargs:
                    new_model_cfg = dataclasses.replace(
                        child_cfg.model, **replace_kwargs
                    )
                if delegation.base_url or delegation.api_key:
                    import os as _os
                    if delegation.base_url:
                        _os.environ["OPENCOMPUTER_DELEGATION_BASE_URL"] = delegation.base_url
                    if delegation.api_key:
                        _os.environ["OPENCOMPUTER_DELEGATION_API_KEY"] = delegation.api_key
            subagent_loop.config = dataclasses.replace(
                child_cfg,
                loop=new_loop_cfg,
                model=new_model_cfg,
            )
        # III.1: push the allowlist onto the child BEFORE it runs. ``None``
        # is also explicitly assigned so callers who re-use a loop factory
        # don't inherit a stale allowlist from a prior delegation.
        subagent_loop.allowed_tools = allowed

        # Hermes parity (2026-05-08): SubagentRegistry — register the child so
        # `oc agents running` / `oc agents kill <id>` can see/cancel it.
        # Best-effort: registry import / register failure must not break
        # delegation. Update the record on completion/failure in the
        # try/except below.
        _registry_record = None
        try:
            from opencomputer.agent.subagent_registry import SubagentRegistry
            _registry_record = SubagentRegistry.instance().register(
                parent_id=self._current_runtime.custom.get("agent_id")
                if self._current_runtime.custom
                else None,
                goal=task,
            )
        except Exception:
            _registry_record = None

        coordinator = get_default_coordinator()
        try:
            async with coordinator.acquire_paths(raw_paths):
                # Propagate the parent's runtime context — plan mode, yolo mode,
                # etc. must apply to subagents too, otherwise delegating becomes
                # an escape hatch.
                # III.5: pass the template's system_prompt to the child loop.
                # When ``template is None`` the kwarg is ``None`` and the child
                # builds its usual declarative + skills + memory + SOUL prompt
                # (existing behavior). With a template, the template BODY is the
                # whole prompt — no further injection on top.
                result = await subagent_loop.run_conversation(
                    user_message=task,
                    runtime=child_runtime,   # ← was self._current_runtime
                    system_prompt_override=(
                        template.system_prompt if template is not None else None
                    ),
                    initial_messages=child_initial_messages or None,
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

                # T3.2 (PR-8): publish DelegationCompleteEvent so MemoryBridge
                # subscribers (and any other bus listener) can react. Best-effort.
                try:
                    from opencomputer.ingestion.bus import default_bus as _bus
                    from plugin_sdk.ingestion import DelegationCompleteEvent

                    _child_outcome = "failure" if result.final_message.content is None else "success"
                    _bus.publish(DelegationCompleteEvent(
                        session_id=call.id,  # parent tool-call id as session context
                        source="agent_loop",
                        parent_session_id="",  # parent session_id unavailable here; set to empty
                        child_session_id=result.session_id,
                        child_outcome=_child_outcome,
                    ))
                except Exception:
                    pass

                # Hermes parity: mark the subagent record completed.
                if _registry_record is not None:
                    try:
                        from datetime import UTC
                        from datetime import datetime as _dt

                        from opencomputer.agent.subagent_registry import SubagentRegistry
                        SubagentRegistry.instance().update(
                            _registry_record.agent_id,
                            state="completed",
                            ended_at=_dt.now(UTC),
                        )
                    except Exception:
                        pass

                return ToolResult(
                    tool_call_id=call.id,
                    content=result.final_message.content,
                )
        except DelegationLockTimeout as exc:
            # Hermes parity: mark the subagent record failed on lock timeout.
            if _registry_record is not None:
                try:
                    from datetime import UTC
                    from datetime import datetime as _dt

                    from opencomputer.agent.subagent_registry import SubagentRegistry
                    SubagentRegistry.instance().update(
                        _registry_record.agent_id,
                        state="failed",
                        ended_at=_dt.now(UTC),
                        error=str(exc)[:200],
                    )
                except Exception:
                    pass
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {exc}",
                is_error=True,
            )
        except BaseException as exc:
            # Hermes parity: mark the subagent record failed on any other error.
            # Re-raise after recording (don't swallow asyncio.CancelledError etc.).
            if _registry_record is not None:
                try:
                    from datetime import UTC
                    from datetime import datetime as _dt

                    from opencomputer.agent.subagent_registry import SubagentRegistry
                    SubagentRegistry.instance().update(
                        _registry_record.agent_id,
                        state="failed",
                        ended_at=_dt.now(UTC),
                        error=str(exc)[:200],
                    )
                except Exception:
                    pass
            raise

    async def _execute_batch(
        self, call_id: str, tasks: list[dict[str, object]]
    ) -> ToolResult:
        """Hermes parity (2026-05-08): tasks=[...] parallel batch dispatch.

        Each task is a dict with at least ``goal`` (Hermes naming). We map
        ``goal`` → OC's single-task ``task`` field and re-enter ``execute``
        per child via an asyncio.gather under a semaphore sized to
        ``LoopConfig.max_concurrent_children`` (env override:
        ``DELEGATION_MAX_CONCURRENT_CHILDREN``).

        Batches larger than the cap return a tool error rather than silently
        truncating — matches the Hermes documented behavior.
        """
        import asyncio as _asyncio
        import os as _os

        if not isinstance(tasks, list) or not tasks:
            return ToolResult(
                tool_call_id=call_id,
                content="Error: `tasks` must be a non-empty list.",
                is_error=True,
            )

        # Resolve cap (parent_loop.config takes precedence; env var is the
        # operational override; default 3 mirrors Hermes).
        parent_loop = getattr(self._factory, "__self__", None)
        cap = 3
        if parent_loop is not None and hasattr(parent_loop, "config"):
            cap = getattr(parent_loop.config.loop, "max_concurrent_children", 3)
        env_cap = _os.environ.get("DELEGATION_MAX_CONCURRENT_CHILDREN", "").strip()
        if env_cap:
            try:
                cap = max(1, int(env_cap))
            except ValueError:
                pass

        if len(tasks) > cap:
            return ToolResult(
                tool_call_id=call_id,
                content=(
                    f"Error: batch size {len(tasks)} exceeds "
                    f"max_concurrent_children={cap}. Submit fewer tasks per call "
                    f"or raise LoopConfig.max_concurrent_children."
                ),
                is_error=True,
            )

        sem = _asyncio.Semaphore(cap)

        async def _run_one(idx: int, spec: dict[str, object]) -> str:
            async with sem:
                # Hermes uses `goal`; OC's existing single-task uses `task`.
                # Map goal → task internally; preserve `toolsets` → `allowed_tools`.
                goal = (spec.get("goal") or spec.get("task") or "")
                if not isinstance(goal, str) or not goal.strip():
                    return f"## Task {idx + 1}: ERROR\nempty/invalid goal in batch entry"
                ctx = (spec.get("context") or "").strip() if isinstance(spec.get("context"), str) else ""
                # Inline the optional context block into the task text.
                task_text = f"{goal.strip()}\n\nContext:\n{ctx}" if ctx else goal.strip()
                toolsets = spec.get("toolsets")
                child_args: dict[str, object] = {"task": task_text}
                if isinstance(toolsets, list):
                    child_args["allowed_tools"] = toolsets
                child_call = ToolCall(
                    id=f"{call_id}-batch-{idx}",
                    name="delegate",
                    arguments=child_args,
                )
                res = await self.execute(child_call)
                return res.content or ""

        results = await _asyncio.gather(
            *[_run_one(i, t) for i, t in enumerate(tasks)],
            return_exceptions=True,
        )
        output_lines: list[str] = []
        any_error = False
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                any_error = True
                output_lines.append(
                    f"## Task {i + 1}: ERROR\n{type(r).__name__}: {r}"
                )
            else:
                output_lines.append(f"## Task {i + 1}\n{r}")
        return ToolResult(
            tool_call_id=call_id,
            content="\n\n".join(output_lines),
            is_error=any_error,
        )


def inherit_credential_pool(parent_provider, child_provider) -> None:
    """T70 — share the parent's CredentialPool with the subagent's provider.

    Without inheritance, the child instantiates a fresh pool from
    config — quarantine state, current key selection, and JWT refresh
    timing all diverge. Sharing the pool object unifies them.

    Skip rules (in order):
      1. Either provider lacks ``_credential_pool`` (not a pool-aware
         provider) — leave both alone.
      2. Parent has no pool (single-key provider, no rotation needed).
      3. Child already has its own pool (operator opted out by passing
         a fresh pool when constructing the child explicitly).
      4. Provider classes differ (OpenAI parent must not hand keys to
         an Anthropic child — they're scoped per provider).
    """
    if not hasattr(parent_provider, "_credential_pool") or not hasattr(
        child_provider, "_credential_pool"
    ):
        return
    parent_pool = parent_provider._credential_pool
    if parent_pool is None:
        return
    if child_provider._credential_pool is not None:
        return
    if type(parent_provider) is not type(child_provider):
        return
    child_provider._credential_pool = parent_pool


__all__ = [
    "DelegateTool",
    "DELEGATE_BLOCKED_TOOLS",
    "inherit_credential_pool",
]
