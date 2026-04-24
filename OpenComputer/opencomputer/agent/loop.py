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
from opencomputer.agent.prompt_builder import PromptBuilder
from opencomputer.agent.reviewer import PostResponseReviewer
from opencomputer.agent.state import SessionDB
from opencomputer.agent.step import StepOutcome
from opencomputer.agent.tool_ordering import sort_tools_for_request
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
        self.compaction = CompactionEngine(
            provider=provider,
            model=config.model.model,
            disabled=compaction_disabled,
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

        # Phase 10f.H: memory context + bridge. Bridge wraps an optional
        # external MemoryProvider (Honcho, Mem0, etc.) with exception safety;
        # None = built-in memory only. Tools receive the context at init so
        # they can read/write MEMORY.md, USER.md, and SessionDB without
        # reaching into globals.
        self._current_session_id: str = ""
        self.memory_context = MemoryContext(
            manager=self.memory,
            db=self.db,
            session_id_provider=lambda: self._current_session_id,
            provider=None,  # plugin registration flips this later
        )
        self.memory_bridge = MemoryBridge(self.memory_context)

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

    # ─── the loop ──────────────────────────────────────────────────

    async def run_conversation(
        self,
        user_message: str,
        session_id: str | None = None,
        system_override: str | None = None,
        runtime: RuntimeContext | None = None,
        stream_callback=None,
    ) -> ConversationResult:
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
        else:
            messages = self.db.get_messages(sid)

        # Phase 12b6 D8: slash-command dispatch. If the user's message maps
        # to a registered command, handle it inline. When the command's
        # handled=True, return early — no LLM call for this turn. When
        # handled=False (rare: e.g. /plan sets a flag, then chat continues),
        # fall through to the normal loop.
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
            self.db.append_message(sid, user_msg)
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
        if system_override is not None:
            base_system = system_override
        else:
            snapshot = self._prompt_snapshots.get(sid)
            if snapshot is None:
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
                snapshot = self.prompt_builder.build(
                    skills=skills,
                    declarative_memory=declarative,
                    user_profile=user_profile,
                    soul=soul,
                    memory_char_limit=self.config.memory.memory_char_limit,
                    user_char_limit=self.config.memory.user_char_limit,
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

        # Append user message + persist
        user_msg = Message(role="user", content=user_message)
        messages.append(user_msg)
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

        for _iter in range(self.config.loop.max_iterations):
            iterations += 1

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
                result = await self.compaction.maybe_run(messages, self._last_input_tokens)
                if result.did_compact:
                    messages = result.messages
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
            )
            self._last_input_tokens = step.input_tokens
            total_input += step.input_tokens
            total_output += step.output_tokens

            if not step.should_continue:
                # No tool calls — safe to persist the assistant message alone. (PR #1)
                messages.append(step.assistant_message)
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
                self.db.end_session(sid)
                return ConversationResult(
                    final_message=step.assistant_message,
                    messages=messages,
                    session_id=sid,
                    iterations=iterations,
                    input_tokens=total_input,
                    output_tokens=total_output,
                )

            # Push the current runtime to DelegateTool so subagents inherit it
            try:
                from opencomputer.tools.delegate import DelegateTool

                DelegateTool.set_runtime(self._runtime)
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
            turn_messages: list[Message] = [step.assistant_message, *tool_results]
            messages.extend(turn_messages)
            self.db.append_messages_batch(sid, turn_messages)

        # Budget exhausted
        final = Message(
            role="assistant",
            content="[loop iteration budget exhausted — agent did not finish]",
        )
        messages.append(final)
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
            resp = await self.provider.complete(
                model=model_name,
                messages=wire_messages,
                system=system,
                tools=tool_schemas,
                max_tokens=self.config.model.max_tokens,
                temperature=self.config.model.temperature,
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

        # Fire PreToolUse hooks first (blocking). Determine which calls are blocked.
        from opencomputer.hooks.engine import engine as hook_engine
        from plugin_sdk.core import ToolResult
        from plugin_sdk.hooks import HookContext, HookEvent

        blocked: dict[str, str] = {}  # call.id → block reason
        for c in calls:
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
            if c.id in blocked:
                return ToolResult(
                    tool_call_id=c.id,
                    content=f"[blocked by PreToolUse hook: {blocked[c.id]}]",
                    is_error=True,
                )
            if allow is not None and not self._is_call_allowed_for_dispatch(
                c.name, c.arguments, _allow_names, _allow_patterns
            ):
                return ToolResult(
                    tool_call_id=c.id,
                    content=(
                        f"Error: tool {c.name!r} is not allowed in this "
                        "subagent (not in the allowlist)."
                    ),
                    is_error=True,
                )
            return await registry.dispatch(
                c,
                session_id=session_id,
                turn_index=turn_index,
                demand_tracker=self.demand_tracker,
            )

        if self.config.loop.parallel_tools and self._all_parallel_safe(calls):
            results = await asyncio.gather(*(_run_one(c) for c in calls))
        else:
            results = [await _run_one(c) for c in calls]

        return [
            Message(
                role="tool",
                content=r.content,
                tool_call_id=r.tool_call_id,
                name=next((c.name for c in calls if c.id == r.tool_call_id), None),
            )
            for r in results
        ]

    def _all_parallel_safe(self, calls: list[ToolCall]) -> bool:
        """Only parallelize when every tool in the batch declared parallel_safe."""
        for c in calls:
            tool = registry.get(c.name)
            if tool is None or not tool.parallel_safe:
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


__all__ = ["AgentLoop", "ConversationResult", "merge_adjacent_user_messages"]
