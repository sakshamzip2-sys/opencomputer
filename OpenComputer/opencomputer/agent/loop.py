"""
The agent loop — THE while loop.

Kept intentionally small (target <500 lines). All the architectural ideas
we studied condense to this:
    1. user message arrives
    2. loop:
         call LLM with current messages + tool schemas
         if response has tool_calls:
             dispatch them in parallel (where safe), append results
             continue
         else:
             break — this is the final answer
    3. persist the conversation to SQLite
    4. return the final message
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from opencomputer.agent.cheap_route import should_route_cheap
from opencomputer.agent.compaction import CompactionEngine
from opencomputer.agent.config import Config
from opencomputer.agent.episodic import EpisodicMemory
from opencomputer.agent.injection import engine as injection_engine
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.memory_bridge import MemoryBridge
from opencomputer.agent.memory_context import MemoryContext
from opencomputer.agent.prompt_builder import PromptBuilder, load_workspace_context
from opencomputer.agent.reviewer import PostResponseReviewer
from opencomputer.agent.state import SessionDB
from opencomputer.agent.step import StepOutcome
from opencomputer.agent.subdirectory_hints import SubdirectoryHintTracker
from opencomputer.agent.tool_ordering import sort_tools_for_request
from opencomputer.tools.bash_safety import detect_destructive
from opencomputer.tools.memory_tool import MemoryTool
from opencomputer.tools.registry import registry
from opencomputer.tools.session_search_tool import SessionSearchTool
from plugin_sdk.core import Message, StopReason, ToolCall
from plugin_sdk.injection import InjectionContext
from plugin_sdk.provider_contract import BaseProvider
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext
from plugin_sdk.tool_matcher import ToolPattern as _ToolPattern
from plugin_sdk.tool_matcher import matches as _pattern_matches
from plugin_sdk.tool_matcher import parse as _parse_pattern

_log = logging.getLogger("opencomputer.agent.loop")


class LoopTimeout(Exception):  # noqa: N818 — public name is the load-bearing one (no Error suffix per project style)
    """Base class for agent-loop wall-clock timeout exceptions.

    Round 2B P-3 — split into two concrete subclasses so callers can
    distinguish "no progress for a while" (``InactivityTimeout``) from
    "absolute cap exceeded" (``IterationTimeout``). Catching ``LoopTimeout``
    handles both. Both raise out of ``run_conversation``; the in-flight
    iteration's tool calls are NOT awaited to completion (asyncio shields
    nothing here on purpose — the user wants to bail).
    """


class InactivityTimeout(LoopTimeout):  # noqa: N818
    """No LLM/tool activity for ``LoopConfig.inactivity_timeout_s`` seconds.

    The activity timer resets on every successful LLM round-trip and
    every tool dispatch (whether the tool succeeded or raised). Streaming
    output that never finishes a request will eventually trip this; the
    common case it catches is a hung provider call or a hook that swallows
    progress without surfacing it.
    """


class IterationTimeout(LoopTimeout):  # noqa: N818
    """Absolute wall-clock cap from loop entry exceeded.

    Independent of activity — even an agent that's busy the whole time
    will trip this once ``LoopConfig.iteration_timeout_s`` has elapsed
    since ``run_conversation`` was entered. Defends against pathological
    fast-iteration loops (1000 sub-second tool calls in a row would never
    trip ``InactivityTimeout``).
    """


class _NoOpDemandTracker:
    """Fallback when the real ``PluginDemandTracker`` can't be constructed.

    Preserves the contract ``dispatch`` expects (a
    ``record_tool_not_found(tool, session, turn)`` callable) so the loop
    doesn't have to null-check. Any call is a silent no-op.
    """

    def record_tool_not_found(
        self, tool_name: str, session_id: str, turn_index: int
    ) -> None:
        return None


@dataclass(slots=True)
class ConversationResult:
    """What a full run_conversation call returns."""

    final_message: Message
    messages: list[Message]
    session_id: str
    iterations: int
    input_tokens: int
    output_tokens: int


def merge_adjacent_user_messages(messages: list[Message]) -> list[Message]:
    """Merge consecutive text-only user messages into one, joining with ``"\\n\\n"``.

    IV.3 — normalize-history injection merging. Mirrors Kimi CLI's
    ``normalize_history`` (``sources/kimi-cli/src/kimi_cli/soul/
    dynamic_injection.py:40-66``): when multiple dynamic-injection
    providers fire in a single turn and each appends a standalone user
    message, the API sees N consecutive user messages instead of one.
    Merging at the API-call boundary saves tokens and improves
    prompt-cache hit rate.

    Merge rules — both messages must satisfy ALL of:
      * ``role == "user"``
      * no ``tool_call_id`` (OpenComputer keeps tool results under
        ``role="tool"``, but defensive: if any adapter put one on a
        user message, don't merge — it would break the tool_use /
        tool_result pair linkage that Anthropic 400s on)
      * no ``tool_calls`` (not expected on user messages, but again
        defensive — merging would drop the linkage)

    Pure function, no side effects. Idempotent — running it twice
    produces the same list as running it once.
    """
    if not messages:
        return []

    def _mergeable(m: Message) -> bool:
        return (
            m.role == "user"
            and m.tool_call_id is None
            and not m.tool_calls
        )

    out: list[Message] = []
    for m in messages:
        if out and _mergeable(out[-1]) and _mergeable(m):
            prev = out[-1]
            merged_content = (prev.content or "") + "\n\n" + (m.content or "")
            out[-1] = Message(
                role="user",
                content=merged_content,
                tool_call_id=None,
                tool_calls=None,
                name=prev.name or m.name,
                reasoning=prev.reasoning or m.reasoning,
            )
        else:
            out.append(m)
    return out


#: Max number of per-session frozen system prompts retained in memory. Long-running
#: gateway daemons can accumulate many session_ids; this cap bounds the growth
#: without compromising the prompt-cache invariant (any evicted session will
#: simply rebuild on its next turn — a one-time cost, not a per-turn cost).
DEFAULT_PROMPT_SNAPSHOT_CACHE_MAX = 256


#: II.2 — Tool names that MUST NEVER run in parallel, regardless of their
#: per-tool ``parallel_safe`` flag. These are tools whose side-effects can
#: race even when two invocations look independent: arbitrary shell
#: commands (``Bash``), user-facing prompts (``AskUserQuestion``), plan-mode
#: state transitions (``ExitPlanMode``), and mutable-state TODO writes.
#:
#: This is the first of two layers stacked on top of the existing
#: ``parallel_safe`` flag. The flag is a hint from the plugin author; this
#: frozenset is a core-level guarantee that catches plugin-author mistakes
#: (e.g. a plugin marking its Bash tool parallel_safe=True).
#:
#: Mirrors Hermes's ``_NEVER_PARALLEL_TOOLS`` at
#: ``sources/hermes-agent/run_agent.py`` line 217.
HARDCODED_NEVER_PARALLEL: frozenset[str] = frozenset({
    "Bash",
    "AskUserQuestion",
    "ExitPlanMode",
    "TodoWrite",
})

#: II.2 — Tool names whose parallel-safety depends on whether their args
#: point to the same path. Two ``Edit`` calls on different files are safe
#: to run in parallel; two on the same file must run sequentially (the
#: second's ``old_string`` search is invalidated by the first's write).
#:
#: Path lookup walks a prioritized arg list —
#: ``file_path`` → ``path`` → ``pattern`` — taking whichever is present.
#: Duplicate paths within a single tool name reject the batch from parallel.
#:
#: Mirrors Hermes's ``_PATH_SCOPED_TOOLS`` at
#: ``sources/hermes-agent/run_agent.py`` line 235.
PATH_SCOPED: frozenset[str] = frozenset({
    "Edit",
    "MultiEdit",
    "Write",
    "NotebookEdit",
})


def _extract_scoped_path(args: dict[str, Any]) -> Any:
    """Return the first recognizable path-ish arg for a PATH_SCOPED tool.

    Walks ``file_path``, ``path``, ``pattern`` in priority order. Returns
    ``None`` if none are present — callers treat that as "can't prove
    paths are distinct; reject parallel" (conservative default).
    """
    return args.get("file_path") or args.get("path") or args.get("pattern")


class AgentLoop:
    """The single while-loop that runs the agent."""

    def __init__(
        self,
        provider: BaseProvider,
        config: Config,
        db: SessionDB | None = None,
        memory: MemoryManager | None = None,
        prompt_builder: PromptBuilder | None = None,
        compaction_disabled: bool = False,
        prompt_snapshot_cache_max: int = DEFAULT_PROMPT_SNAPSHOT_CACHE_MAX,
        episodic_disabled: bool = False,
        reviewer_disabled: bool = False,
        is_reviewer: bool = False,
        allowed_tools: frozenset[str] | None = None,
        consent_gate: Any = None,  # F1: opencomputer.agent.consent.ConsentGate | None
    ) -> None:
        self.provider = provider
        self.config = config
        # III.1 tool allowlist. ``None`` = full registry (existing behavior);
        # a concrete frozenset = filter both the schemas handed to the
        # provider and the dispatch path. Applied per-loop (not per-turn),
        # set at construction time or mutated by a caller (e.g. DelegateTool)
        # before the loop runs. Mirrors Claude Code's frontmatter
        # ``allowed-tools:`` concept applied to OpenComputer's actual
        # tool-dispatching surface (subagent spawn). See
        # sources/claude-code/plugins/code-review/commands/code-review.md.
        self.allowed_tools: frozenset[str] | None = allowed_tools
        # Default runtime context — overwritten by ``run_conversation`` on
        # every turn. Declared here so direct callers of ``_dispatch_tool_calls``
        # (tests, harness hooks) don't hit AttributeError before the first run.
        self._runtime: RuntimeContext = DEFAULT_RUNTIME_CONTEXT
        # Round 2B P-3: wall-clock timeout bookkeeping. Re-initialised at the
        # top of each ``run_conversation`` call; declared here so direct
        # callers of ``_dispatch_tool_calls`` (tests, harness hooks) that
        # bypass ``run_conversation`` don't hit AttributeError when the
        # per-call activity bump fires.
        self._loop_started_at: float = time.monotonic()
        self._last_activity_at: float = self._loop_started_at
        self.db = db or SessionDB(config.session.db_path)
        self.memory = memory or MemoryManager(
            declarative_path=config.memory.declarative_path,
            skills_path=config.memory.skills_path,
            user_path=config.memory.user_path,
            soul_path=config.memory.soul_path,
            memory_char_limit=config.memory.memory_char_limit,
            user_char_limit=config.memory.user_char_limit,
        )
        self.prompt_builder = prompt_builder or PromptBuilder()

        # Phase 10f.H: memory context + bridge. Bridge wraps an optional
        # external MemoryProvider (Honcho, Mem0, etc.) with exception safety;
        # None = built-in memory only. Tools receive the context at init so
        # they can read/write MEMORY.md, USER.md, and SessionDB without
        # reaching into globals.
        # NOTE: constructed BEFORE CompactionEngine so we can pass the bridge
        # reference to it for PR-6 T2.2 on_pre_compress wiring.
        self._current_session_id: str = ""
        #: Path A.1 (2026-04-27): the persona id picked by the classifier
        #: for the most recent prompt-build. Used by base.j2 to apply
        #: persona-specific Jinja conditionals (e.g. softening "no filler"
        #: rules under the companion persona).
        self._active_persona_id: str = ""
        self.memory_context = MemoryContext(
            manager=self.memory,
            db=self.db,
            session_id_provider=lambda: self._current_session_id,
            provider=None,  # plugin registration flips this later
        )
        self.memory_bridge = MemoryBridge(self.memory_context)

        # Round 2B P-8 — wire the bg-notify session provider so the
        # coding-harness ``StartProcess`` tool can stamp the active session
        # onto each watcher task. Idempotent across multiple AgentLoop
        # instantiations: the latest constructor wins, which matches how
        # the rest of the registry singletons behave.
        try:
            from opencomputer.agent.bg_notify import set_session_id_provider as _set_bg_provider

            _set_bg_provider(lambda: self._current_session_id)
        except Exception:  # noqa: BLE001 — never break agent startup
            _log.debug("bg_notify provider wiring skipped", exc_info=True)

        # Tier-A item 10 — resolve the context-engine strategy via the
        # registry. ``LoopConfig.context_engine`` defaults to
        # ``"compressor"`` (the existing CompactionEngine), so production
        # behavior is unchanged. A plugin that registered an alternative
        # engine + a profile that selects it will resolve through the
        # registry. Unknown name → fall back to CompactionEngine so a
        # misconfigured profile still boots (the registry's ``build``
        # logs a warning in that case).
        from opencomputer.agent import context_engine_registry as _ctx_registry

        engine_name = getattr(config.loop, "context_engine", "compressor")
        self.compaction = _ctx_registry.build(
            engine_name,
            provider=provider,
            model=config.model.model,
            disabled=compaction_disabled,
            memory_bridge=self.memory_bridge,
        ) or CompactionEngine(
            provider=provider,
            model=config.model.model,
            disabled=compaction_disabled,
            memory_bridge=self.memory_bridge,
        )
        # Phase 11d: third-pillar episodic memory. Records one event per
        # completed turn for cross-session "remind me" queries via FTS5.
        self._episodic = None if episodic_disabled else EpisodicMemory(db=self.db)
        # Phase 12a: post-response reviewer. Fires after each END_TURN return
        # to opportunistically file the turn's takeaway into MEMORY.md. Never
        # blocks the user-facing return. is_reviewer=True suppresses the
        # spawn entirely so a reviewer agent doesn't trigger another reviewer.
        self._is_reviewer = is_reviewer
        self._reviewer = (
            None
            if reviewer_disabled or is_reviewer
            else PostResponseReviewer(memory=self.memory, is_reviewer=False)
        )
        self._last_input_tokens = 0

        # F1 (Sub-project F): optional consent gate. When set, every tool
        # call with declared capability_claims is checked BEFORE PreToolUse
        # hooks fire — gate-before-hook invariant keeps plugins from
        # pre-empting the security boundary. When None, gate is disabled
        # (back-compat: tools without claims are unaffected either way).
        self._consent_gate = consent_gate

        # TS-T5: progressive subdirectory hint discovery. Watches tool
        # calls for paths into NEW subdirectories and lazily loads
        # ``OPENCOMPUTER.md`` / ``AGENTS.md`` / ``CLAUDE.md`` /
        # ``.cursorrules`` from those dirs. The startup CWD is pre-marked
        # (its hints are already in the system prompt via
        # ``load_workspace_context``) so we never duplicate-load it.
        # Hints get appended to the relevant tool result — NOT the system
        # prompt — to keep Anthropic's prefix cache hot.
        self._subdir_tracker = SubdirectoryHintTracker(working_dir=os.getcwd())

        # Register agent-facing memory tools in the global registry. Safe to
        # call repeatedly — the registry's .register() is idempotent on
        # same-instance re-registration; on different instances it replaces.
        try:
            registry.register(MemoryTool(self.memory_context))
            registry.register(SessionSearchTool(self.memory_context))
        except Exception:
            # Registry may disallow re-registration under a different name.
            # Defensive: don't blow up AgentLoop construction over this.
            pass

        # Phase 12b.5 Task E3: demand tracker for "plugins raising their hand"
        # when the agent calls tools it doesn't have. Wired into dispatch;
        # surfaces via ``opencomputer plugin demand`` (E5) and
        # ``opencomputer plugin enable`` (E4). Wrapped in a broad try/except
        # because the agent loop MUST work even if demand infrastructure
        # blows up (bad manifest, unreadable profile.yaml, etc.).
        self.demand_tracker: Any = self._build_demand_tracker(config)
        # Per-session frozen system prompt. LRU-evicted once cache is full, so
        # long-running daemons don't retain snapshots for abandoned sessions
        # forever. Memory edits mid-session go to disk immediately but do NOT
        # mutate this snapshot — that's the invariant that keeps the prefix
        # cache hot on turn 2+. Compaction invalidates only the suffix.
        # Source: hermes-agent tools/memory_tool.py:_system_prompt_snapshot
        # (freeze) + agent/prompt_builder.py:_SKILLS_PROMPT_CACHE (LRU).
        self._prompt_snapshots: OrderedDict[str, str] = OrderedDict()
        self._prompt_snapshot_cache_max = prompt_snapshot_cache_max

        # B3 auto-collection: subscribe to F2 bus iff <_home() / "evolution" / "enabled"> exists
        try:
            from opencomputer.evolution.trajectory import bootstrap_if_enabled
            self._evolution_subscription = bootstrap_if_enabled()
        except Exception:  # never break agent startup over an evolution bug
            self._evolution_subscription = None

        # Phase 3.F — when system-control is on at construction time,
        # attach the structured-logger bus listener so SignalEvents are
        # mirrored to ``agent.log``. Best-effort: a missing system_control
        # attribute on legacy Configs (rare) is fine; a broken attach
        # never breaks the loop.
        try:
            if getattr(getattr(config, "system_control", None), "enabled", False):
                from opencomputer.system_control.bus_listener import (
                    attach_to_bus as _sc_attach,
                )

                _sc_attach()
        except Exception as e:  # noqa: BLE001 — defensive
            _log.warning("system-control attach_to_bus skipped: %s", e)

    # ─── the loop ──────────────────────────────────────────────────

    async def run_conversation(
        self,
        user_message: str,
        session_id: str | None = None,
        system_override: str | None = None,
        runtime: RuntimeContext | None = None,
        stream_callback=None,
        system_prompt_override: str | None = None,
        initial_messages: list[Message] | None = None,
        images: list[str] | None = None,
    ) -> ConversationResult:
        """Run the agent loop until the model stops calling tools.

        Parameters
        ----------
        system_prompt_override:
            III.5 — when set, bypass the normal PromptBuilder pipeline and
            use this string verbatim as the system prompt. Skills /
            declarative memory / USER.md / SOUL.md are NOT injected — the
            template author owns the full prompt. Used by
            :class:`opencomputer.tools.delegate.DelegateTool` when the
            ``agent`` parameter resolves to a registered
            :class:`~opencomputer.agent.agent_templates.AgentTemplate`.

            Distinct from ``system_override`` (pre-existing): that kwarg
            also bypasses PromptBuilder but was never adopted by
            DelegateTool. Treat ``system_prompt_override`` as the newer,
            named-template path; ``system_override`` remains for direct
            callers that want a raw swap. When both are set,
            ``system_prompt_override`` wins (it's the III.5 semantic).
        initial_messages:
            Round 2B P-9 — pre-seed a fresh session's history with these
            messages BEFORE ``user_message`` is appended. Only honoured
            for new sessions (``session_id`` not present in the DB);
            existing sessions keep their persisted history. Used by
            :class:`opencomputer.tools.delegate.DelegateTool` to fork the
            parent's recent context into a delegated child. Seeded
            messages are persisted so resume-from-disk reproduces the
            same starting state.
        """
        sid = session_id or str(uuid.uuid4())
        self._runtime = runtime or DEFAULT_RUNTIME_CONTEXT
        # Expose current session id to memory tools via the context provider.
        self._current_session_id = sid

        # If this is a fresh session, create it in the DB and seed history from disk.
        existing = self.db.get_session(sid) if session_id else None
        if existing is None:
            self.db.create_session(
                session_id=sid,
                platform="cli",
                model=self.config.model.model,
            )
            messages: list[Message] = []
            # Round 2B P-9: optional pre-seed for forked-context delegations.
            # ``initial_messages`` is only honoured for fresh sessions to keep
            # resume-from-disk deterministic. Seeded messages are persisted so
            # the on-disk session matches in-memory state.
            if initial_messages:
                messages.extend(initial_messages)
                self.db.append_messages_batch(sid, list(initial_messages))
        else:
            messages = self.db.get_messages(sid)

        # Phase 12b6 D8: slash-command dispatch. If the user's message maps
        # to a registered command, handle it inline. When the command's
        # handled=True, return early — no LLM call for this turn. When
        # handled=False (rare: e.g. /plan sets a flag, then chat continues),
        # fall through to the normal loop.
        #
        # V3.A-T10: importing ``slash_commands`` registers built-in
        # (non-plugin) commands like ``/scrape`` into the same dict the
        # dispatcher reads from below. The import is idempotent.
        from opencomputer.agent import slash_commands as _builtin_slash  # noqa: F401
        from opencomputer.agent.slash_dispatcher import dispatch as _slash_dispatch
        from opencomputer.plugins.registry import registry as _plugin_registry

        _slash_result = await _slash_dispatch(
            user_message,
            _plugin_registry.slash_commands,
            self._runtime,
        )
        if _slash_result is not None and _slash_result.handled:
            user_msg = Message(role="user", content=user_message)
            assistant_msg = Message(
                role="assistant", content=_slash_result.output
            )
            messages.append(user_msg)
            messages.append(assistant_msg)
            self._emit_before_message_write(session_id=sid, message=user_msg)
            self.db.append_message(sid, user_msg)
            self._emit_before_message_write(session_id=sid, message=assistant_msg)
            self.db.append_message(sid, assistant_msg)
            self.db.end_session(sid)
            return ConversationResult(
                final_message=assistant_msg,
                messages=messages,
                session_id=sid,
                iterations=0,
                input_tokens=0,
                output_tokens=0,
            )

        # System prompt is frozen per session: built once on the first turn,
        # then reused verbatim so the prefix cache hits on turn 2+. Memory
        # edits during a session do NOT retrigger a rebuild — that's the
        # invariant that makes hermes's prompt_cache ~10× cheaper than
        # per-turn rebuilds.
        # III.5: ``system_prompt_override`` wins over ``system_override``
        # (and both win over the PromptBuilder path). Template-authored
        # prompts are treated as rendered-Jinja strings: declarative /
        # skills / memory / SOUL injection OFF — the body is assumed
        # intentional.
        if system_prompt_override is not None:
            base_system = system_prompt_override
        elif system_override is not None:
            base_system = system_override
        else:
            snapshot = self._prompt_snapshots.get(sid)
            if snapshot is None:
                # Round 2A P-1: BEFORE_PROMPT_BUILD — observers know a fresh
                # system prompt is about to be assembled. Fired BEFORE the
                # build call so handlers can be sure they're seeing every
                # session's first turn (subsequent turns hit the cache and
                # never reach this branch). modified_message support for
                # appending a system reminder is documented in the SDK; the
                # loop does NOT consume it today (template author owns the
                # body). A future PR can splice modified_message into the
                # rendered snapshot per the plan.
                from opencomputer.hooks.engine import engine as _hook_engine_pb
                from plugin_sdk.hooks import HookContext as _HookContextPB
                from plugin_sdk.hooks import HookEvent as _HookEventPB

                _hook_engine_pb.fire_and_forget(
                    _HookContextPB(
                        event=_HookEventPB.BEFORE_PROMPT_BUILD,
                        session_id=sid,
                        runtime=self._runtime,
                    )
                )
                skills = self.memory.list_skills()
                # Phase 10f.C: read MEMORY.md + USER.md and render them into
                # the FROZEN base prompt. Mid-session edits don't rebuild
                # this — that's the prefix-cache invariant.
                declarative = self.memory.read_declarative()
                user_profile = self.memory.read_user()
                # Phase 14.F / C3: per-profile personality from SOUL.md.
                # Joins the same frozen-prompt lane so drift only lands on
                # the next session's rebuild, preserving prefix-cache hits.
                soul = self.memory.read_soul()
                # Layered Awareness MVP — pre-format the top-K user-model
                # facts block from the F4 graph. Empty string on a fresh
                # profile (no bootstrap yet) → ``base.j2`` omits the
                # section. Computed inside the ``snapshot is None`` branch
                # so it runs ONCE per session and lands on the frozen
                # base prompt, preserving prefix-cache hits on turn 2+.
                # A graph read failure must NEVER break agent startup,
                # so swallow exceptions and degrade to "no facts".
                try:
                    user_facts = self.prompt_builder.build_user_facts()
                except Exception:  # noqa: BLE001 — defensive: never break loop
                    _log.debug("build_user_facts failed; degrading to empty", exc_info=True)
                    user_facts = ""
                # V3.A-T8 — workspace context loader. Walk up from cwd to
                # discover OPENCOMPUTER.md / CLAUDE.md / AGENTS.md and inject
                # them into the FROZEN base prompt. Computed once per session
                # so prefix-cache hits on turn 2+ stay valid; mid-session
                # edits to those files don't reflect until the next session.
                # A file-read failure must NEVER break agent startup, so any
                # exception degrades to "no workspace context".
                try:
                    workspace_context = load_workspace_context()
                except Exception:  # noqa: BLE001 — defensive: never break loop
                    _log.debug(
                        "load_workspace_context failed; degrading to empty",
                        exc_info=True,
                    )
                    workspace_context = ""
                # V2.C-T5 — persona auto-classifier overlay. Runs once per
                # session (same lane as user_facts / workspace_context) so
                # the resulting overlay lands on the frozen base prompt
                # and prefix-cache hits on turn 2+ stay valid. Classifier
                # failure degrades to "" — agent startup must NEVER break
                # over a persona miss.
                try:
                    persona_overlay = self._build_persona_overlay(sid)
                except Exception:  # noqa: BLE001 — defensive: never break loop
                    _log.debug(
                        "_build_persona_overlay failed; degrading to empty",
                        exc_info=True,
                    )
                    persona_overlay = ""
                # PR-6 T2.1: use build_with_memory so ambient memory blocks
                # from active providers are appended under '## Memory context'.
                # Falls back to the sync build() path if ambient blocks are
                # disabled or no bridge is wired. The snapshot is still frozen
                # per session — ambient blocks are evaluated once at session
                # start and cached, matching the prefix-cache invariant.
                snapshot = await self.prompt_builder.build_with_memory(
                    skills=skills,
                    declarative_memory=declarative,
                    user_profile=user_profile,
                    soul=soul,
                    user_facts=user_facts,
                    memory_char_limit=self.config.memory.memory_char_limit,
                    user_char_limit=self.config.memory.user_char_limit,
                    memory_bridge=self.memory_bridge,
                    session_id=sid,
                    enable_ambient_blocks=self.config.memory.enable_ambient_blocks,
                    max_ambient_block_chars=self.config.memory.max_ambient_block_chars,
                    workspace_context=workspace_context,
                    persona_overlay=persona_overlay,
                    active_persona_id=self._active_persona_id,
                )
                # Evict the least-recently-used snapshot if the cache is full
                # BEFORE inserting, so we never exceed the cap even transiently.
                while len(self._prompt_snapshots) >= self._prompt_snapshot_cache_max:
                    self._prompt_snapshots.popitem(last=False)
                self._prompt_snapshots[sid] = snapshot
            else:
                # Cache hit — mark this session as most-recently-used
                self._prompt_snapshots.move_to_end(sid)
            base_system = snapshot

        # Compute the 1-indexed turn number for this session. IV.2: providers
        # use this to throttle heavy content (plan/review reminders flip from
        # FULL to SPARSE after the first turn, with a FULL refresh every 5th
        # turn). Count user messages already in history; the user message
        # we're about to append is turn ``N+1``.
        turn_index = sum(1 for m in messages if m.role == "user") + 1

        # Collect dynamic injections (plan_mode, yolo_mode, etc. from plugins).
        # ``compose`` is async — providers gather concurrently (IV.1 refactor).
        inj_ctx = InjectionContext(
            messages=tuple(messages),
            runtime=self._runtime,
            session_id=sid,
            turn_index=turn_index,
        )
        injected = await injection_engine.compose(inj_ctx)
        system = base_system + ("\n\n" + injected if injected else "")

        # Append user message + persist. ``images`` (TUI image-paste) is
        # threaded onto Message.attachments; the provider converts to
        # multimodal content blocks at request time. Note: SessionDB
        # doesn't yet persist attachments — image paths are turn-scoped
        # only, won't survive session resume. Acceptable since the user
        # can re-paste; documented as a known limitation.
        user_msg = Message(
            role="user", content=user_message, attachments=list(images or [])
        )
        messages.append(user_msg)
        self._emit_before_message_write(session_id=sid, message=user_msg)
        self.db.append_message(sid, user_msg)
        # Track where this turn's messages start so episodic recording can
        # walk only the new tool messages (not the whole prior history).
        turn_start_index = len(messages) - 1

        # Phase 12b1 A7: MemoryBridge prefetch. Ask the external memory
        # provider (Honcho, Mem0, etc.) for any context worth injecting
        # this turn. The bridge is exception-safe and guards on
        # runtime.agent_context — a cron/flush turn short-circuits without
        # touching the provider. Result (if any) is appended to the
        # per-turn ``system`` variable; ``_prompt_snapshots[sid]`` stays
        # frozen so the prefix cache keeps hitting on turn 2+.
        prefetched = await self.memory_bridge.prefetch(
            query=user_message,
            turn_index=turn_start_index,
            runtime=self._runtime,
        )
        if prefetched:
            system = system + "\n\n## Relevant memory\n\n" + prefetched

        total_input = 0
        total_output = 0
        iterations = 0

        # Round 2B P-3: wall-clock timeouts. ``_loop_started_at`` is fixed at
        # entry; ``_last_activity_at`` is bumped on every LLM call return and
        # tool dispatch. Both use ``time.monotonic()`` so a system-clock
        # adjustment mid-loop (NTP slew, manual ``date -s ...``) cannot mask
        # an inactivity stall or trigger a spurious timeout. Stored as
        # instance attrs so ``_dispatch_tool_calls`` can refresh activity
        # without threading another arg through every call site.
        self._loop_started_at = time.monotonic()
        self._last_activity_at = self._loop_started_at

        for _iter in range(self.config.loop.max_iterations):
            iterations += 1

            # Round 2B P-3: enforce both timeouts at the top of each iteration.
            # Inactivity check first (the more useful signal); absolute cap
            # second. Both raise out of run_conversation — no synthetic
            # assistant message: the caller (CLI / gateway) decides how to
            # surface the timeout to the user.
            now = time.monotonic()
            if now - self._last_activity_at > self.config.loop.inactivity_timeout_s:
                raise InactivityTimeout(
                    f"no LLM/tool activity for "
                    f"{self.config.loop.inactivity_timeout_s}s "
                    f"(last activity {now - self._last_activity_at:.1f}s ago)"
                )
            if now - self._loop_started_at > self.config.loop.iteration_timeout_s:
                raise IterationTimeout(
                    f"loop wall-clock cap of "
                    f"{self.config.loop.iteration_timeout_s}s exceeded "
                    f"(elapsed {now - self._loop_started_at:.1f}s)"
                )

            # T3.2 (PR-8): publish TurnStartEvent at the top of each iteration.
            # Best-effort + exception-isolated so a broken bus never stalls the loop.
            try:
                from opencomputer.ingestion.bus import default_bus as _bus
                from plugin_sdk.ingestion import TurnStartEvent

                _bus.publish(TurnStartEvent(
                    session_id=sid,
                    source="agent_loop",
                    turn_index=iterations,
                ))
            except Exception:  # noqa: BLE001
                pass

            # D6 cheap-route gating: on iteration 0 only, if cheap_model is
            # configured AND the heuristic fires, pass the cheap model to
            # the provider for this turn. Subsequent iterations revert to
            # the main model — cheap models often have capability gaps
            # that cascade once tools start firing.
            model_for_turn = self.config.model.model
            cheap = self.config.model.cheap_model
            if (
                cheap is not None
                and _iter == 0
                and should_route_cheap(user_message)
            ):
                _log.debug(
                    "cheap-route fired: routing first turn to %s (msg len=%d)",
                    cheap,
                    len(user_message),
                )
                model_for_turn = cheap

            # P-2 (round 2a): mid-run /steer nudge. Between turns means
            # after the previous iteration's tool dispatch but before the
            # next LLM request — i.e. _iter > 0 (the first iteration's
            # context is the user's original message, no nudge needed).
            # Latest-wins is enforced inside SteerRegistry.submit; here
            # we just consume + append a synthetic user message so the
            # next ``_run_one_step`` call sees it. The format string is
            # centralised in ``opencomputer.agent.steer.format_nudge_message``
            # so CLI / wire / Telegram acknowledgements stay in sync.
            if _iter > 0:
                try:
                    from opencomputer.agent.steer import (
                        default_registry as _steer_registry,
                    )
                    from opencomputer.agent.steer import (
                        format_nudge_message as _format_nudge,
                    )

                    nudge = _steer_registry.consume(sid)
                    if nudge:
                        nudge_msg = Message(
                            role="user",
                            content=_format_nudge(nudge),
                        )
                        messages.append(nudge_msg)
                        # Persist so a resumed session sees the same
                        # context (the nudge was already promised to
                        # the user; replaying without it would silently
                        # change the next turn's semantics).
                        self.db.append_message(sid, nudge_msg)
                        _log.debug(
                            "steer: applied pending nudge for session %s "
                            "(len=%d)",
                            sid,
                            len(nudge),
                        )
                except Exception:  # noqa: BLE001 — never break the loop
                    _log.warning(
                        "steer: consume failed for session %s — continuing",
                        sid,
                        exc_info=True,
                    )

            # Round 2B P-8 — drain pending background-process exit notices
            # for this session and inject them as system messages so the
            # next provider call sees the completion. Drained on EVERY
            # iteration (including iter 0) because a long-running bg proc
            # may finish during the user's typing window and we want the
            # very first model turn to know about it. Persist so a resumed
            # session keeps the bg-exit context visible.
            try:
                from opencomputer.agent.bg_notify import (
                    drain_for_session as _drain_bg,
                )

                bg_notices = _drain_bg(sid)
                for body in bg_notices:
                    bg_msg = Message(role="system", content=body)
                    messages.append(bg_msg)
                    self.db.append_message(sid, bg_msg)
                if bg_notices:
                    _log.debug(
                        "bg-notify: applied %d pending bg exit notice(s) for session %s",
                        len(bg_notices),
                        sid,
                    )
            except Exception:  # noqa: BLE001 — never break the loop
                _log.warning(
                    "bg-notify: drain failed for session %s — continuing",
                    sid,
                    exc_info=True,
                )

            # Compaction check — uses REAL measured tokens from prior turn.
            # First iteration (no prior measurement) skips the check.
            if self._last_input_tokens > 0:
                # D7: emit PreCompact hook BEFORE actually compacting so
                # plugins can observe / log / modify behavior pre-summary.
                if self.compaction.should_compact(self._last_input_tokens):
                    from opencomputer.hooks.engine import engine as _hook_engine
                    from plugin_sdk.hooks import HookContext, HookEvent

                    _hook_engine.fire_and_forget(
                        HookContext(
                            event=HookEvent.PRE_COMPACT,
                            session_id=sid,
                            runtime=self._runtime,
                        )
                    )
                    # Round 2A P-1: BEFORE_COMPACTION carries the messages
                    # snapshot the summariser is about to consume. Distinct
                    # from PRE_COMPACT (kept for back-compat) — the new event
                    # exposes the actual context to handlers.
                    _hook_engine.fire_and_forget(
                        HookContext(
                            event=HookEvent.BEFORE_COMPACTION,
                            session_id=sid,
                            runtime=self._runtime,
                            messages=list(messages),
                        )
                    )
                result = await self.compaction.maybe_run(messages, self._last_input_tokens)
                if result.did_compact:
                    messages = result.messages
                    # Round 2A P-1: AFTER_COMPACTION fires only when
                    # compaction actually ran (did_compact=True). The handler
                    # sees the post-compaction message list (synthetic
                    # summary + recent block).
                    from opencomputer.hooks.engine import engine as _hook_engine_ac
                    from plugin_sdk.hooks import (
                        HookContext as _HookContextAC,
                    )
                    from plugin_sdk.hooks import HookEvent as _HookEventAC

                    _hook_engine_ac.fire_and_forget(
                        _HookContextAC(
                            event=_HookEventAC.AFTER_COMPACTION,
                            session_id=sid,
                            runtime=self._runtime,
                            messages=list(messages),
                        )
                    )
                    # Re-collect injections with the new message list. Reuse
                    # the same ``turn_index`` computed at turn-start — the
                    # logical turn number doesn't change just because we
                    # summarized earlier history; throttling decisions must
                    # stay consistent for this turn.
                    inj_ctx = InjectionContext(
                        messages=tuple(messages),
                        runtime=self._runtime,
                        session_id=sid,
                        turn_index=turn_index,
                    )
                    injected = await injection_engine.compose(inj_ctx)
                    system = base_system + ("\n\n" + injected if injected else "")

            step = await self._run_one_step(
                messages=messages,
                system=system,
                stream_callback=stream_callback,
                model=model_for_turn,
                session_id=sid,
            )
            # Round 2B P-3: a returned LLM response is activity. Bump BEFORE
            # the early-return path below so an end-turn turn that took 290s
            # still resets the timer for any caller that resumes the same
            # AgentLoop on the same session.
            self._last_activity_at = time.monotonic()
            self._last_input_tokens = step.input_tokens
            total_input += step.input_tokens
            total_output += step.output_tokens

            if not step.should_continue:
                # No tool calls — safe to persist the assistant message alone. (PR #1)
                messages.append(step.assistant_message)
                self._emit_before_message_write(
                    session_id=sid, message=step.assistant_message
                )
                self.db.append_message(sid, step.assistant_message)
                # Record an episodic event for this completed turn — pass the
                # tool messages this turn produced so file paths get extracted. (PR #6)
                if self._episodic is not None:
                    try:
                        turn_tool_msgs = [
                            m for m in messages[turn_start_index:] if m.role == "tool"
                        ]
                        existing_count = len(self.db.list_episodic(session_id=sid, limit=10_000))
                        self._episodic.record_turn(
                            session_id=sid,
                            turn_index=existing_count,
                            user_message=user_message,
                            assistant_message=step.assistant_message,
                            tool_messages=turn_tool_msgs,
                        )
                    except Exception:  # noqa: BLE001
                        # Episodic recording is best-effort; never fail the turn.
                        pass
                # Phase 12a: spawn the post-response reviewer fire-and-forget.
                # The user-facing return is NOT awaited on this — if review
                # crashes or takes long, the turn is unaffected.
                if self._reviewer is not None and step.assistant_message.content:
                    try:
                        self._reviewer.spawn_review(
                            user_message=user_message,
                            assistant_message=step.assistant_message.content,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                # Phase 12b1 A7: notify the external memory provider that
                # a turn completed. Bridge is fire-and-forget (exceptions
                # swallowed internally) and guards on runtime.agent_context
                # — symmetric with prefetch above. Only called on END_TURN;
                # never on max-iterations exhaustion or exception exits,
                # because a half-finished turn would confuse the provider.
                await self.memory_bridge.sync_turn(
                    user=user_message,
                    assistant=step.assistant_message.content or "",
                    turn_index=turn_start_index,
                    runtime=self._runtime,
                )
                # TS-T6: kick off async title generation after the first
                # user→assistant exchange. Daemon thread, fire-and-forget;
                # ``maybe_auto_title`` self-skips on later turns and on
                # already-titled sessions, so we can call it
                # unconditionally here without checking the turn index.
                try:
                    from opencomputer.agent.title_generator import maybe_auto_title

                    maybe_auto_title(
                        session_db=self.db,
                        session_id=sid,
                        user_message=user_message,
                        assistant_response=step.assistant_message.content or "",
                        conversation_history=messages,
                    )
                except Exception:  # noqa: BLE001 — title gen is best-effort
                    pass
                self.db.end_session(sid)
                return ConversationResult(
                    final_message=step.assistant_message,
                    messages=messages,
                    session_id=sid,
                    iterations=iterations,
                    input_tokens=total_input,
                    output_tokens=total_output,
                )

            # Push the current runtime to DelegateTool so subagents inherit it.
            # Round 2B P-9: also snapshot ``messages`` onto the runtime so a
            # delegate tool_use with ``forked_context=true`` can seed the
            # child loop with the parent's recent conversation. Snapshot is
            # taken BEFORE the assistant message containing the delegate
            # tool_use is appended, so the snapshot ends at a clean
            # turn-boundary (no orphan tool_use).
            try:
                import dataclasses as _dc

                from opencomputer.tools.delegate import DelegateTool

                DelegateTool.set_runtime(
                    _dc.replace(self._runtime, parent_messages=tuple(messages))
                )
            except Exception:
                pass  # delegate tool may not be registered yet in some contexts

            # Dispatch tools BEFORE persisting the assistant message. If we saved
            # it first and then got cancelled mid-dispatch, the DB would hold a
            # tool_use with no matching tool_result — Anthropic 400s on resume.
            # Atomic batch persist below restores the invariant.
            tool_results = await self._dispatch_tool_calls(
                step.assistant_message.tool_calls or [],
                session_id=sid,
                turn_index=iterations,
            )
            # Round 2B P-3: tool dispatch finished — count both successful and
            # error results as activity (the agent did *something*, that's
            # what the inactivity timer cares about). ``_dispatch_tool_calls``
            # also bumps per-call internally so a long parallel batch keeps
            # the timer fresh between calls.
            self._last_activity_at = time.monotonic()
            turn_messages: list[Message] = [step.assistant_message, *tool_results]
            messages.extend(turn_messages)
            for _msg in turn_messages:
                self._emit_before_message_write(session_id=sid, message=_msg)
            self.db.append_messages_batch(sid, turn_messages)

        # Budget exhausted
        final = Message(
            role="assistant",
            content="[loop iteration budget exhausted — agent did not finish]",
        )
        messages.append(final)
        self._emit_before_message_write(session_id=sid, message=final)
        self.db.append_message(sid, final)
        self.db.end_session(sid)
        return ConversationResult(
            final_message=final,
            messages=messages,
            session_id=sid,
            iterations=iterations,
            input_tokens=total_input,
            output_tokens=total_output,
        )

    # ─── V2.C-T5 persona auto-classifier ───────────────────────────

    def _build_persona_overlay(self, session_id: str) -> str:
        """Run the persona classifier and return the matched persona's overlay.

        V2.C-T5 — invoked once per session in the same lane as
        ``user_facts`` / ``workspace_context`` so the resulting overlay
        lands on the FROZEN base prompt and the prefix cache stays warm.

        Pulls a SIMPLIFIED context for V2.C: foreground app via
        ``osascript`` (macOS only, "" elsewhere), current hour, last 10
        recent file paths from the session message log (best effort), and
        the last 3 user messages. Any failure degrades to ``""`` (no
        persona section in the prompt) — startup must NEVER break over a
        classifier issue. V2.D may swap in a richer context source.
        """
        import datetime as _dt

        from opencomputer.awareness.personas._foreground import (
            detect_frontmost_app,
        )
        from opencomputer.awareness.personas.classifier import (
            ClassificationContext,
            classify,
        )
        from opencomputer.awareness.personas.registry import get_persona

        try:
            foreground_app = detect_frontmost_app()
        except Exception:  # noqa: BLE001 — defensive: never break loop
            foreground_app = ""

        try:
            hour = _dt.datetime.now().hour
        except Exception:  # noqa: BLE001 — defensive: never break loop
            hour = 12

        recent_files: tuple[str, ...] = ()
        last_user_messages: tuple[str, ...] = ()
        try:
            messages = self.db.get_messages(session_id)
        except Exception:  # noqa: BLE001 — defensive: never break loop
            messages = []
        if messages:
            # Best-effort extraction of file paths from tool calls and
            # user messages — V2.C ships with a simple heuristic (look for
            # path-like strings in tool args). Empty tuple is fine if
            # nothing matches.
            file_paths: list[str] = []
            user_texts: list[str] = []
            for msg in messages:
                if msg.role == "user" and isinstance(msg.content, str):
                    user_texts.append(msg.content)
                tool_calls = getattr(msg, "tool_calls", None) or ()
                for tc in tool_calls:
                    args = getattr(tc, "arguments", None)
                    if isinstance(args, dict):
                        for v in args.values():
                            if (
                                isinstance(v, str)
                                and ("/" in v or "." in v)
                                and len(v) < 512
                            ):
                                file_paths.append(v)
            recent_files = tuple(file_paths[-10:])
            last_user_messages = tuple(user_texts[-3:])

        try:
            ctx = ClassificationContext(
                foreground_app=foreground_app,
                time_of_day_hour=hour,
                recent_file_paths=recent_files,
                last_messages=last_user_messages,
            )
            result = classify(ctx)
            persona = get_persona(result.persona_id)
        except Exception:  # noqa: BLE001 — defensive: never break loop
            _log.debug("persona classifier failed; degrading to empty", exc_info=True)
            return ""
        if persona is None:
            return ""
        # V2.C-T5: stash the active persona id for the prompt builder so
        # base.j2 can apply persona-specific Jinja conditionals (Path A.2:
        # the "no filler / no hedging / not a chat toy" rules are dropped
        # when active_persona == "companion" so the companion overlay's
        # warm-but-honest register isn't fighting the action-bias rules).
        self._active_persona_id = str(result.persona_id)
        overlay = persona.get("system_prompt_overlay", "") or ""
        overlay = str(overlay).strip()

        # Path A.3 (2026-04-27): when companion is the active persona,
        # peek the most-recent unconsumed Life-Event firing and append
        # it as a "RECENT LIFE EVENT" anchor. The reflective lane needs
        # real anchors to land — without them, the companion has nothing
        # specific to point at when asked "how are you?". The firing's
        # ``hint_text`` is concrete and actionable.
        if result.persona_id == "companion":
            try:
                from opencomputer.awareness.life_events.registry import (
                    get_global_registry,
                )

                firing = get_global_registry().peek_most_recent_firing()
                if firing is not None and firing.hint_text:
                    overlay = (
                        overlay
                        + "\n\n## RECENT LIFE EVENT (anchor for the companion)\n\n"
                        + f"Detected pattern: {firing.pattern_id} "
                        + f"(confidence {firing.confidence:.0%}, "
                        + f"{firing.evidence_count} evidence items)\n"
                        + f"Hint: {firing.hint_text}\n\n"
                        + "When the user asks how you are, you can use this as "
                        + "a real anchor — e.g. 'I keep thinking about what you "
                        + "mentioned earlier' or naming the pattern by its "
                        + "felt shape. Don't over-reference it; use it once "
                        + "naturally if it fits, or ignore if the moment "
                        + "doesn't call for it."
                    )
            except Exception:  # noqa: BLE001 — degrade silently
                _log.debug(
                    "companion life-event peek failed; degrading to bare overlay",
                    exc_info=True,
                )

        return overlay

    # ─── PR-6 T2.3 session lifecycle ───────────────────────────────

    async def aclose(self, session_id: str | None = None) -> None:
        """Clean shutdown. Fires memory-provider on_session_end hooks.

        PR-6 of 2026-04-25 Hermes parity plan. Wires the on_session_end hook
        that was defined in plugin_sdk/memory.py but never invoked.

        Parameters
        ----------
        session_id:
            Explicit session to close. If omitted, uses ``_current_session_id``
            (the most recently active session). If neither is set, the call is
            a no-op with respect to session-end hooks (bridge still shuts down
            cleanly via ``shutdown_all`` at process exit).
        """
        sid = session_id or self._current_session_id
        if sid and self.memory_bridge is not None:
            try:
                await self.memory_bridge.fire_session_end(sid)
            except Exception:
                _log.exception(
                    "AgentLoop.aclose: fire_session_end failed for session %s", sid
                )

    # ─── allowlist helpers ─────────────────────────────────────────

    def _split_allowlist(self) -> tuple[frozenset[str], list[_ToolPattern]]:
        """Split ``self.allowed_tools`` into (bare-names, parsed-patterns).

        Bare names (plain tool identifiers like ``"Read"``) stay in a
        frozenset for O(1) lookup — the III.1 shape. Entries containing
        parens or a trailing ``*`` are parsed into ``ToolPattern`` values
        and matched per-call in the dispatch path.

        Malformed entries are silently ignored: a broken allowlist
        shouldn't take down an otherwise valid subagent delegation.
        The test suite asserts parser-level rejection separately via
        ``tool_matcher.parse`` unit tests.
        """
        assert self.allowed_tools is not None
        names: set[str] = set()
        patterns: list[_ToolPattern] = []
        for entry in self.allowed_tools:
            if "(" in entry or "*" in entry:
                try:
                    patterns.append(_parse_pattern(entry))
                except ValueError:
                    _log.warning(
                        "allowed_tools: ignoring malformed entry %r", entry
                    )
            else:
                names.add(entry.strip())
        return frozenset(names), patterns

    def _is_tool_name_allowed_for_schemas(
        self,
        tool_name: str,
        names: frozenset[str],
        patterns: list[_ToolPattern],
    ) -> bool:
        """True if ``tool_name`` is allowed by any allowlist entry for the
        purpose of exposing its schema to the provider.

        For arg-patterned entries (e.g. ``Bash(git:*)``) the schema IS
        surfaced so the model can discover the tool exists — dispatch
        filters the specific arg shape. Without this, the model would
        never see Bash in the tools list and couldn't call it at all.
        """
        if tool_name in names:
            return True
        for p in patterns:
            if p.is_prefix:
                if tool_name.startswith(p.tool_name):
                    return True
            elif p.arg_pattern is not None:
                # Arg-patterned: surface the schema if the tool name matches.
                if tool_name == p.tool_name:
                    return True
            else:
                # Bare name in pattern (rare, shouldn't happen after split)
                if tool_name == p.tool_name:
                    return True
        return False

    def _is_call_allowed_for_dispatch(
        self,
        tool_name: str,
        tool_args: dict,
        names: frozenset[str],
        patterns: list[_ToolPattern],
    ) -> bool:
        """True if a specific tool call (name + args) passes the allowlist.

        Bare names match first (O(1)). Otherwise iterate patterns until
        one returns True.
        """
        if tool_name in names:
            return True
        return any(_pattern_matches(p, tool_name, tool_args) for p in patterns)

    def _filtered_schemas(self) -> list:
        """Return registry schemas filtered by ``self.allowed_tools``.

        * ``allowed_tools is None`` → full registry (existing behavior).
        * ``allowed_tools`` concrete (possibly empty) → only schemas whose
          ``name`` is allowed by at least one bare name or pattern entry.

        III.1/III.2 applies to BOTH the schemas handed to the provider AND
        the dispatch path — otherwise the model sees tool X, calls it, and
        we'd silently run it because only schemas were filtered.
        """
        all_schemas = registry.schemas()
        if self.allowed_tools is None:
            return all_schemas
        names, patterns = self._split_allowlist()
        return [
            s
            for s in all_schemas
            if self._is_tool_name_allowed_for_schemas(s.name, names, patterns)
        ]

    # ─── one step ──────────────────────────────────────────────────

    async def _run_one_step(
        self,
        *,
        messages: list[Message],
        system: str,
        stream_callback=None,
        model: str | None = None,
        session_id: str = "",
    ) -> StepOutcome:
        """One LLM call + classification of the result.

        If `stream_callback` is provided, stream_complete is used and each
        text chunk is passed to the callback synchronously.

        ``model`` overrides ``config.model.model`` for this turn only —
        used by the cheap-route gate on iteration 0. ``None`` = use the
        config default.
        """
        model_name = model if model is not None else self.config.model.model
        tool_schemas = sort_tools_for_request(self._filtered_schemas())
        # IV.3: normalize the message list right before the wire call.
        # If multiple providers somehow stacked standalone user messages
        # earlier this turn, collapse adjacent text-only users into one
        # so the API sees a clean sequence. No-op in the common case.
        wire_messages = merge_adjacent_user_messages(messages)

        # Round 2A P-1: PRE_LLM_CALL — fire-and-forget so handlers can read
        # the message list and model name before we hit the wire. Hook returns
        # are intentionally ignored: this is an observation event, not a gate
        # (use PreToolUse if you want to block).
        from opencomputer.hooks.engine import engine as _hook_engine
        from plugin_sdk.hooks import HookContext as _HookContext
        from plugin_sdk.hooks import HookEvent as _HookEvent

        _hook_engine.fire_and_forget(
            _HookContext(
                event=_HookEvent.PRE_LLM_CALL,
                session_id=session_id,
                runtime=self._runtime,
                messages=list(wire_messages),
                model=model_name,
            )
        )

        if stream_callback is not None:
            final_response = None
            async for event in self.provider.stream_complete(
                model=model_name,
                messages=wire_messages,
                system=system,
                tools=tool_schemas,
                max_tokens=self.config.model.max_tokens,
                temperature=self.config.model.temperature,
            ):
                if event.kind == "text_delta":
                    stream_callback(event.text)
                elif event.kind == "done":
                    final_response = event.response
            if final_response is None:
                raise RuntimeError("stream ended without a 'done' event")
            resp = final_response
        else:
            # G.31 — wrap the provider call in the fallback router so
            # transient failures (429 / 5xx / connection refused) walk
            # the configured ``fallback_models`` chain before raising.
            from opencomputer.agent.fallback import call_with_fallback

            async def _do_call(active_model: str):
                return await self.provider.complete(
                    model=active_model,
                    messages=wire_messages,
                    system=system,
                    tools=tool_schemas,
                    max_tokens=self.config.model.max_tokens,
                    temperature=self.config.model.temperature,
                )

            resp = await call_with_fallback(
                _do_call,
                primary_model=model_name,
                fallback_models=self.config.model.fallback_models,
            )

        stop_reason_map = {
            "end_turn": StopReason.END_TURN,
            "tool_use": StopReason.TOOL_USE,
            "max_tokens": StopReason.MAX_TOKENS,
            "stop_sequence": StopReason.END_TURN,
        }
        stop = stop_reason_map.get(resp.stop_reason, StopReason.END_TURN)

        # If the model called tools, even if the raw stop_reason was "end_turn",
        # we need to continue so the model can process results.
        if resp.message.tool_calls and stop == StopReason.END_TURN:
            stop = StopReason.TOOL_USE

        # II.6: pull reasoning-chain metadata off the ProviderResponse onto
        # the assistant message. Providers that don't surface reasoning
        # (standard Opus/Sonnet, stock OpenAI chat) return ``None`` for
        # these fields; the reconstructed Message stays functionally
        # identical. For reasoning-capable providers (OpenAI o1/o3, Nous,
        # OpenRouter unified, Anthropic extended thinking), SessionDB's
        # ``append_message`` persists the fields so the next turn can
        # replay them — matches Hermes v6 schema intent.
        msg = resp.message
        resp_reasoning = getattr(resp, "reasoning", None)
        resp_reasoning_details = getattr(resp, "reasoning_details", None)
        resp_codex_items = getattr(resp, "codex_reasoning_items", None)
        if (
            resp_reasoning is not None
            or resp_reasoning_details is not None
            or resp_codex_items is not None
        ):
            # Prefer the provider-level fields; only fall back to
            # message-level ones if the provider already attached them
            # (some providers populate Message.reasoning directly).
            msg = Message(
                role=msg.role,
                content=msg.content,
                tool_call_id=msg.tool_call_id,
                tool_calls=msg.tool_calls,
                name=msg.name,
                reasoning=resp_reasoning if resp_reasoning is not None else msg.reasoning,
                reasoning_details=(
                    resp_reasoning_details
                    if resp_reasoning_details is not None
                    else msg.reasoning_details
                ),
                codex_reasoning_items=(
                    resp_codex_items
                    if resp_codex_items is not None
                    else msg.codex_reasoning_items
                ),
            )

        # Round 2A P-1: POST_LLM_CALL — observers see the response message and
        # token usage. Same fire-and-forget contract as PRE_LLM_CALL.
        _hook_engine.fire_and_forget(
            _HookContext(
                event=_HookEvent.POST_LLM_CALL,
                session_id=session_id,
                runtime=self._runtime,
                message=msg,
                messages=list(wire_messages),
                model=model_name,
            )
        )

        return StepOutcome(
            stop_reason=stop,
            assistant_message=msg,
            tool_calls_made=len(msg.tool_calls or []),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    # ─── tool dispatch ─────────────────────────────────────────────

    async def _dispatch_tool_calls(
        self, calls: list[ToolCall], session_id: str = "", turn_index: int = 0
    ) -> list[Message]:
        """Run all tool calls — in parallel where safe — and return result Messages.

        Fires PreToolUse hooks before each tool runs. If a hook blocks, the tool
        is skipped and an error ToolResult is synthesized. Runtime context flows
        to hooks so plan_mode_block etc. can read it.
        """
        if not calls:
            return []

        # F1: consent gate fires BEFORE any PreToolUse hook. Plugin-registered
        # hooks cannot pre-empt or bypass this check. Only tools that declare
        # capability_claims are gated; un-declared tools pass through (same
        # behavior as before F1). Bypass via OPENCOMPUTER_CONSENT_BYPASS=1.
        from opencomputer.hooks.engine import engine as hook_engine
        from plugin_sdk.core import ToolResult
        from plugin_sdk.hooks import HookContext, HookEvent

        blocked: dict[str, str] = {}  # call.id → block reason

        if self._consent_gate is not None:
            from opencomputer.agent.consent.bypass import BypassManager
            from plugin_sdk.consent import ConsentTier
            if not BypassManager.is_active():
                for c in calls:
                    tool = registry.get(c.name)
                    if tool is None:
                        continue
                    claims = getattr(tool, "capability_claims", ())
                    for claim in claims:
                        scope = _extract_scope(c)
                        decision = self._consent_gate.check(
                            claim, scope=scope, session_id=session_id,
                        )
                        # Round 2a P-5 — when consent is denied for a
                        # Tier-2 (PER_ACTION) claim AND the gate has a
                        # channel-side prompt handler bound (Telegram
                        # adapter wired in by the gateway), pause the
                        # dispatch and ask the user via inline buttons.
                        # The handler delivers the prompt; the gate
                        # blocks until ``resolve_pending`` is called or
                        # the 5-minute timeout elapses (auto-deny per
                        # L3). Tier-0/1/3 claims keep the legacy
                        # behavior — no prompt, just deny.
                        if (
                            not decision.allowed
                            and claim.tier_required == ConsentTier.PER_ACTION
                            and self._consent_gate._prompt_handler is not None
                            and session_id is not None
                        ):
                            try:
                                approval = await self._consent_gate.request_approval(
                                    claim=claim,
                                    scope=scope,
                                    session_id=session_id,
                                )
                            except Exception as exc:  # noqa: BLE001
                                _log.warning(
                                    "consent request_approval raised for "
                                    "session=%s capability=%s: %s",
                                    session_id, claim.capability_id, exc,
                                )
                                approval = None
                            if approval is not None and approval.allowed:
                                # User approved — re-check (in case
                                # ``allow_always`` persisted a grant)
                                # and proceed with this claim.
                                decision = approval
                            else:
                                # User denied or timed out — fall
                                # through to the deny path below.
                                if approval is not None:
                                    decision = approval
                        if not decision.allowed:
                            blocked[c.id] = f"consent denied: {decision.reason}"
                            break

        # Fire PreToolUse hooks next (blocking). Determine which calls are blocked.
        for c in calls:
            if c.id in blocked:
                continue  # already blocked by consent gate; skip hook dispatch
            ctx = HookContext(
                event=HookEvent.PRE_TOOL_USE,
                session_id=session_id,
                tool_call=c,
                runtime=self._runtime,
            )
            decision = await hook_engine.fire_blocking(ctx)
            if decision is not None and decision.decision == "block":
                blocked[c.id] = decision.reason or "blocked by hook"

        # III.1/III.2: gate dispatch on the allowlist too. Filtering only
        # the provider-facing schemas isn't enough — a model could still
        # emit a tool_use block for a disallowed name (e.g. recovered from
        # earlier history before the allowlist was in effect). Refuse here
        # so the subagent can't escape its blast-radius budget. Pattern
        # entries (e.g. ``Bash(git:*)``) check the actual call args.
        allow = self.allowed_tools
        if allow is not None:
            _allow_names, _allow_patterns = self._split_allowlist()
        else:
            _allow_names, _allow_patterns = frozenset(), []

        async def _run_one(c: ToolCall):
            import time as _time
            start = _time.monotonic()
            if c.id in blocked:
                result = ToolResult(
                    tool_call_id=c.id,
                    content=f"[blocked by PreToolUse hook: {blocked[c.id]}]",
                    is_error=True,
                )
                self._emit_tool_call_event(
                    call=c,
                    outcome="blocked",
                    duration_seconds=_time.monotonic() - start,
                    session_id=session_id,
                )
                return result
            if allow is not None and not self._is_call_allowed_for_dispatch(
                c.name, c.arguments, _allow_names, _allow_patterns
            ):
                result = ToolResult(
                    tool_call_id=c.id,
                    content=(
                        f"Error: tool {c.name!r} is not allowed in this "
                        "subagent (not in the allowlist)."
                    ),
                    is_error=True,
                )
                self._emit_tool_call_event(
                    call=c,
                    outcome="blocked",
                    duration_seconds=_time.monotonic() - start,
                    session_id=session_id,
                )
                return result
            try:
                result = await registry.dispatch(
                    c,
                    session_id=session_id,
                    turn_index=turn_index,
                    demand_tracker=self.demand_tracker,
                )
            except asyncio.CancelledError:
                self._emit_tool_call_event(
                    call=c,
                    outcome="cancelled",
                    duration_seconds=_time.monotonic() - start,
                    session_id=session_id,
                )
                raise
            except Exception as _exc:
                # Round 2B P-3: a tool that raised is still activity — the
                # agent did *something*. Bump before re-raising so the next
                # iteration's inactivity check measures from the right point.
                self._last_activity_at = _time.monotonic()
                self._emit_tool_call_event(
                    call=c,
                    outcome="failure",
                    duration_seconds=_time.monotonic() - start,
                    session_id=session_id,
                    exception=_exc,
                )
                raise
            else:
                # Round 2B P-3: per-call activity bump. Long parallel batches
                # (gather of 10 tools that take 30s each) keep the inactivity
                # timer fresh as each call settles, not just at batch end.
                self._last_activity_at = _time.monotonic()
                outcome = (
                    "failure" if getattr(result, "is_error", False) else "success"
                )
                self._emit_tool_call_event(
                    call=c,
                    outcome=outcome,
                    duration_seconds=_time.monotonic() - start,
                    session_id=session_id,
                    result=result if outcome == "failure" else None,
                )
                # Round 2A P-1: TRANSFORM_TOOL_RESULT — handlers may rewrite
                # the result text the model is about to see. This is a
                # blocking hook because the rewrite must complete before the
                # tool message is constructed. A handler returning
                # ``modified_message`` replaces ``result.content`` verbatim.
                # No handler / pass / empty modified_message → unchanged.
                result = await _maybe_transform_tool_result(
                    result=result,
                    call=c,
                    session_id=session_id,
                    runtime=self._runtime,
                )
                # Round 2A P-1: TRANSFORM_TERMINAL_OUTPUT — same shape but
                # scoped to Bash-style tools. Streaming-bash hasn't landed
                # yet, so this fires once with the full ToolResult content
                # rather than per stream-chunk; the handler contract is
                # identical and a future PR can move the emit point into a
                # streaming bash adapter without breaking handlers.
                # TODO: relocate to streaming bash chunks once that infra exists.
                if c.name == "Bash":
                    result = await _maybe_transform_terminal_output(
                        result=result,
                        call=c,
                        session_id=session_id,
                        runtime=self._runtime,
                    )
                return result

        if self.config.loop.parallel_tools and self._all_parallel_safe(calls):
            results = await asyncio.gather(*(_run_one(c) for c in calls))
        else:
            results = [await _run_one(c) for c in calls]

        # TS-T5: subdirectory hint discovery. Append project context files
        # (OPENCOMPUTER.md / AGENTS.md / CLAUDE.md) to the matching tool's
        # result content when the tool's args reference a NEW directory.
        # Done BEFORE spillover so any hints that grow a result past the
        # per-tool budget still get persisted to disk by Layer 2 below.
        # Frozen-dataclass ToolResult forces a rebuild — same idiom as
        # the spillover layer that follows. Errors are swallowed; hint
        # discovery must never break the dispatch path.
        _call_by_id = {c.id: c for c in calls}
        hinted_results: list[ToolResult] = []
        for r in results:
            try:
                c = _call_by_id.get(r.tool_call_id)
                if c is not None:
                    hints = self._subdir_tracker.check_tool_call(
                        c.name, dict(c.arguments or {})
                    )
                    if hints:
                        r = ToolResult(
                            tool_call_id=r.tool_call_id,
                            content=(r.content or "") + hints,
                            is_error=r.is_error,
                        )
            except Exception:  # noqa: BLE001 — never break dispatch
                _log.debug("subdir hint discovery skipped", exc_info=True)
            hinted_results.append(r)
        results = hinted_results

        # TS-T2: 3-level overflow defense. Layer 2 fires per-result with the
        # tool name so per-tool thresholds (and pinned ``Read``=inf) apply.
        # Layer 3 then runs over the batch in dict form to handle the
        # "many medium-sized results combine to overflow" case. Both layers
        # are idempotent against already-persisted blocks.
        from opencomputer.agent.tool_result_storage import (
            enforce_turn_budget as _enforce_turn_budget,
        )
        from opencomputer.agent.tool_result_storage import (
            maybe_persist_tool_result as _maybe_persist_tool_result,
        )

        _name_by_id = {c.id: c.name for c in calls}
        # Layer 2 — per-result spillover.
        adjusted: list[ToolResult] = []
        for r in results:
            tool_name = _name_by_id.get(r.tool_call_id, "")
            new_content = _maybe_persist_tool_result(
                content=r.content or "",
                tool_name=tool_name,
                tool_use_id=r.tool_call_id,
            )
            if new_content != r.content:
                # ``ToolResult`` is frozen+slots — rebuild via the constructor.
                r = ToolResult(
                    tool_call_id=r.tool_call_id,
                    content=new_content,
                    is_error=r.is_error,
                )
            adjusted.append(r)

        # Layer 3 — per-turn aggregate budget. Operates over plain dicts and
        # mutates them in place; we copy back into ToolResult objects.
        tool_message_dicts: list[dict] = [
            {"content": r.content, "tool_call_id": r.tool_call_id} for r in adjusted
        ]
        _enforce_turn_budget(tool_message_dicts)
        adjusted = [
            ToolResult(
                tool_call_id=r.tool_call_id,
                content=tool_message_dicts[i]["content"],
                is_error=r.is_error,
            )
            if tool_message_dicts[i]["content"] != r.content
            else r
            for i, r in enumerate(adjusted)
        ]

        return [
            Message(
                role="tool",
                content=r.content,
                tool_call_id=r.tool_call_id,
                name=_name_by_id.get(r.tool_call_id),
            )
            for r in adjusted
        ]

    def _emit_before_message_write(
        self, *, session_id: str, message: Message
    ) -> None:
        """Round 2A P-1: BEFORE_MESSAGE_WRITE — fires before each db persist.

        Observation hook only (fire-and-forget). Returns are ignored: this is
        the bookkeeping seam for memory backends and audit loggers, not a
        veto point. See P-14 (trajectory export) for the consumer.
        """
        try:
            from opencomputer.hooks.engine import engine as _hook_engine
            from plugin_sdk.hooks import HookContext as _HookContext
            from plugin_sdk.hooks import HookEvent as _HookEvent

            _hook_engine.fire_and_forget(
                _HookContext(
                    event=_HookEvent.BEFORE_MESSAGE_WRITE,
                    session_id=session_id,
                    runtime=self._runtime,
                    message=message,
                )
            )
        except Exception:  # noqa: BLE001 — never break the loop over a hook
            _log.warning("BEFORE_MESSAGE_WRITE emit failed", exc_info=True)

    def _emit_tool_call_event(
        self,
        *,
        call: ToolCall,
        outcome: str,
        duration_seconds: float,
        session_id: str,
        exception: BaseException | None = None,
        result: Any | None = None,
    ) -> None:
        """Publish a :class:`ToolCallEvent` after a tool call settles.

        Phase 3.A / F2 — emits to :data:`opencomputer.ingestion.bus.default_bus`
        AFTER the existing ``PostToolUse``-eligible path runs. This is
        the thin publisher wiring that Session B's B3 trajectory
        subscriber depends on.

        T3.1 (PR-8): when outcome=="failure", captures error_class and
        error_message_preview (truncated to 200 chars per privacy rule)
        into event.metadata so the reflection LLM can learn from failures.

        Exception-isolated: a broken bus MUST NOT break the agent loop.
        Import is lazy (inside the function) so a hypothetical import
        failure can't take down ``_dispatch_tool_calls`` either — the
        warning is logged and dispatch continues.
        """
        try:
            from opencomputer.ingestion.bus import default_bus
            from plugin_sdk.ingestion import ToolCallEvent

            # T3.1: build error metadata when the outcome is a failure.
            # Privacy rule: truncate to 200 chars (same limit as TrajectoryEvent).
            metadata: dict[str, Any] = {}
            if outcome == "failure":
                if exception is not None:
                    metadata["error_class"] = type(exception).__name__
                    metadata["error_message_preview"] = str(exception)[:200]
                elif result is not None and getattr(result, "is_error", False):
                    content_str = str(getattr(result, "content", ""))[:200]
                    if content_str:
                        metadata["error_message_preview"] = content_str

            event = ToolCallEvent(
                session_id=session_id or None,
                source="agent_loop",
                tool_name=call.name,
                arguments=dict(call.arguments or {}),
                outcome=outcome,  # type: ignore[arg-type]
                duration_seconds=max(0.0, duration_seconds),
                metadata=metadata,
            )
            default_bus.publish(event)
        except Exception:  # noqa: BLE001 — bus must never break the loop
            _log.warning(
                "bus: ToolCallEvent publish failed for tool=%s — continuing",
                call.name,
                exc_info=True,
            )

        # Tier-A item 11: write a row to ``tool_usage`` for the insights
        # CLI. Separate try/except — one of {bus publish, telemetry write}
        # failing must not break the other or the loop.
        try:
            if session_id:
                # ``self.config.model.name`` is the *configured* model;
                # the actual per-turn model lives in ``_last_model`` when
                # the cheap-route or auxiliary client overrides for a
                # specific turn (Item 15 wires this fully). Best-effort.
                model_for_row = (
                    getattr(self, "_last_model", None)
                    or getattr(self.config.model, "name", None)
                )
                self.db.record_tool_usage(
                    session_id=session_id,
                    tool=call.name,
                    outcome=outcome,
                    duration_ms=max(0.0, duration_seconds) * 1000.0,
                    model=model_for_row,
                )
        except Exception:  # noqa: BLE001 — never break the loop
            _log.debug(
                "tool_usage record failed for tool=%s — continuing",
                call.name,
                exc_info=True,
            )

    def _all_parallel_safe(self, calls: list[ToolCall]) -> bool:
        """Decide whether a batch of tool calls is safe to run in parallel.

        Three-layer gate (II.2 — mirrors Hermes's ``_should_parallelize_tool_batch``
        at ``sources/hermes-agent/run_agent.py`` line 267):

        1. **Hardcoded-never name check.** Any tool in
           :data:`HARDCODED_NEVER_PARALLEL` forces sequential, regardless of
           its plugin-declared ``parallel_safe`` flag. Catches plugin-author
           flag mistakes and tools whose side-effects can race.

        2. **Per-tool flag check** (backwards compat). An unregistered tool
           or one with ``parallel_safe=False`` forces sequential.

        3. **Path-scope check.** For tools in :data:`PATH_SCOPED`, extract
           the first recognizable path arg (``file_path``/``path``/``pattern``).
           Duplicate paths within a single tool name reject parallel —
           concurrent writes to the same file can collide, and two ``Edit``
           calls on the same file have an ordering dependency.

        4. **Bash destructive-command scan.** If any ``Bash`` call's
           ``command`` arg matches a pattern in
           :mod:`opencomputer.tools.bash_safety`, reject parallel. (Bash
           is also in the hardcoded-never set above, so this layer is
           defence-in-depth: if a future refactor drops Bash from
           HARDCODED_NEVER_PARALLEL, this still catches ``rm -rf /``.)

        Empty input returns True (no-op is trivially parallel-safe).
        """
        # Layer 1 + 2: name whitelist + per-tool flag.
        for c in calls:
            if c.name in HARDCODED_NEVER_PARALLEL:
                return False
            tool = registry.get(c.name)
            if tool is None or not tool.parallel_safe:
                return False

        # Layer 3: path-scope dedup. Per-tool-name buckets so ``Edit`` vs
        # ``Write`` on the same path are tracked separately — matches
        # Hermes's ``reserved_paths`` semantics. A None path means the
        # call has no recognizable path arg; we can't prove paths differ,
        # so conservative default: reject parallel. Otherwise check for
        # duplicate paths within the same tool name.
        path_by_name: dict[str, list[Any]] = {}
        for c in calls:
            if c.name in PATH_SCOPED:
                p = _extract_scoped_path(c.arguments)
                if p is None:
                    return False
                path_by_name.setdefault(c.name, []).append(p)
        for paths in path_by_name.values():
            if len(set(paths)) < len(paths):
                return False

        # Layer 4: Bash destructive-command scan. ``Bash`` is also in the
        # hardcoded-never set above, so in practice we've already returned
        # False. This remains so that a future loosening of
        # HARDCODED_NEVER_PARALLEL (e.g. allowing read-only Bash) still
        # catches ``rm -rf /`` shapes.
        for c in calls:
            if c.name == "Bash":
                cmd = c.arguments.get("command")
                if isinstance(cmd, str) and detect_destructive(cmd) is not None:
                    return False

        return True

    # ─── E3: demand tracker construction ───────────────────────────

    @staticmethod
    def _default_search_paths() -> list:
        """Canonical plugin search paths — thin wrapper that silences failures.

        Delegates to ``opencomputer.plugins.discovery.standard_search_paths``
        (single source of truth). Demand-tracker construction must never
        crash the agent, so exceptions are swallowed here — the base
        function intentionally doesn't swallow them.
        """
        try:
            from opencomputer.plugins.discovery import standard_search_paths

            return standard_search_paths()
        except Exception:  # noqa: BLE001
            _log.debug("demand_tracker: search-path resolution failed", exc_info=True)
            return []

    def _active_profile_plugins(self) -> frozenset[str] | None:
        """Best-effort read of the active profile's enabled plugin set.

        Returns ``None`` on any failure so the tracker falls back to
        "no filter" (record signals for every matching candidate). A
        concrete frozenset means "these plugins are already enabled; skip
        them when recording signals".
        """
        try:
            from opencomputer.agent.config import _home
            from opencomputer.agent.profile_config import load_profile_config

            cfg = load_profile_config(_home())
            enabled = cfg.enabled_plugins
            if enabled == "*":
                # Wildcard = "all plugins allowed" — treat as "no specific
                # filter" so the tracker records for any matching candidate.
                return None
            assert isinstance(enabled, frozenset)
            return enabled
        except Exception:  # noqa: BLE001
            _log.debug("demand_tracker: profile-config read failed", exc_info=True)
            return None

    def _build_demand_tracker(self, cfg: Any) -> Any:
        """Construct the real tracker, or fall back to a no-op shim."""
        try:
            from opencomputer.plugins.demand_tracker import PluginDemandTracker
            from opencomputer.plugins.discovery import discover

            search_paths = self._default_search_paths()
            return PluginDemandTracker(
                db_path=cfg.session.db_path,
                discover_fn=lambda: discover(search_paths),
                active_profile_plugins=self._active_profile_plugins(),
            )
        except Exception:  # noqa: BLE001
            _log.debug(
                "demand_tracker: construction failed; falling back to no-op",
                exc_info=True,
            )
            return _NoOpDemandTracker()


async def _maybe_transform_tool_result(
    *,
    result: Any,
    call: ToolCall,
    session_id: str,
    runtime: RuntimeContext,
) -> Any:
    """Round 2A P-1: invoke TRANSFORM_TOOL_RESULT and apply ``modified_message``.

    Returns either the original ``result`` or a new
    :class:`~plugin_sdk.core.ToolResult` whose ``content`` is the handler's
    rewrite. Failures are isolated — any exception in a handler leaves the
    original result untouched (the engine logs it).
    """
    from opencomputer.hooks.engine import engine as _hook_engine
    from plugin_sdk.core import ToolResult as _ToolResult
    from plugin_sdk.hooks import HookContext as _HookContext
    from plugin_sdk.hooks import HookEvent as _HookEvent

    ctx = _HookContext(
        event=_HookEvent.TRANSFORM_TOOL_RESULT,
        session_id=session_id,
        tool_call=call,
        tool_result=result,
        runtime=runtime,
    )
    decision = await _hook_engine.fire_blocking(ctx)
    if decision is None or not decision.modified_message:
        return result
    # Rewrite the content; preserve everything else on the result.
    return _ToolResult(
        tool_call_id=result.tool_call_id,
        content=decision.modified_message,
        is_error=getattr(result, "is_error", False),
    )


async def _maybe_transform_terminal_output(
    *,
    result: Any,
    call: ToolCall,
    session_id: str,
    runtime: RuntimeContext,
) -> Any:
    """Round 2A P-1: invoke TRANSFORM_TERMINAL_OUTPUT for Bash-like tools.

    Same contract as :func:`_maybe_transform_tool_result` but uses the
    ``streamed_chunk`` field on HookContext so handlers can distinguish
    "this is a terminal stream chunk" from "this is a structured tool
    result". A handler returning ``modified_message`` replaces the chunk.
    """
    from opencomputer.hooks.engine import engine as _hook_engine
    from plugin_sdk.core import ToolResult as _ToolResult
    from plugin_sdk.hooks import HookContext as _HookContext
    from plugin_sdk.hooks import HookEvent as _HookEvent

    ctx = _HookContext(
        event=_HookEvent.TRANSFORM_TERMINAL_OUTPUT,
        session_id=session_id,
        tool_call=call,
        tool_result=result,
        streamed_chunk=getattr(result, "content", "") or "",
        runtime=runtime,
    )
    decision = await _hook_engine.fire_blocking(ctx)
    if decision is None or not decision.modified_message:
        return result
    return _ToolResult(
        tool_call_id=result.tool_call_id,
        content=decision.modified_message,
        is_error=getattr(result, "is_error", False),
    )


def _extract_scope(call: ToolCall) -> str | None:
    """F1: extract a scope-like argument from a tool call for gate matching.

    Heuristic: look for common scope-ish keys (path, file, file_path, url,
    directory, cwd). Plugin authors should use one of these if they want
    scope-level grant granularity. F1 MVP — more formal scope-extractor
    hooks arrive in a follow-up.
    """
    args = call.arguments or {}
    for key in ("path", "file_path", "file", "url", "directory", "dir", "cwd"):
        v = args.get(key)
        if isinstance(v, str) and v:
            return v
    return None


__all__ = [
    "AgentLoop",
    "ConversationResult",
    "HARDCODED_NEVER_PARALLEL",
    "InactivityTimeout",
    "IterationTimeout",
    "LoopTimeout",
    "PATH_SCOPED",
    "merge_adjacent_user_messages",
]
