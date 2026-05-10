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
import hashlib
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from opencomputer.agent.cheap_route import should_route_cheap
from opencomputer.agent.compaction import CompactionEngine
from opencomputer.agent.config import Config
from opencomputer.agent.episodic import EpisodicMemory
from opencomputer.agent.injection import engine as injection_engine
from opencomputer.agent.loop_safety import LoopAbortError, LoopDetector
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.memory_bridge import MemoryBridge
from opencomputer.agent.memory_context import MemoryContext
from opencomputer.agent.prompt_builder import PromptBuilder, load_workspace_context
from opencomputer.agent.reviewer import PostResponseReviewer
from opencomputer.agent.state import SessionDB
from opencomputer.agent.step import StepOutcome
from opencomputer.agent.subdirectory_hints import SubdirectoryHintTracker
from opencomputer.agent.tool_guardrails import (
    ToolLoopGuard,
    ToolLoopGuardrailError,
)
from opencomputer.agent.tool_ordering import sort_tools_for_request
from opencomputer.tools.bash_safety import detect_destructive_with_context
from opencomputer.tools.memory_tool import MemoryTool
from opencomputer.tools.registry import registry
from opencomputer.tools.session_search_tool import SessionSearchTool
from plugin_sdk.core import Message, StopReason, ToolCall
from plugin_sdk.injection import InjectionContext
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext
from plugin_sdk.tool_matcher import ToolPattern as _ToolPattern
from plugin_sdk.tool_matcher import matches as _pattern_matches
from plugin_sdk.tool_matcher import parse as _parse_pattern

_log = logging.getLogger("opencomputer.agent.loop")


def _load_custom_personalities() -> dict[str, str]:
    """Read ``agent.personalities`` from the active profile config.

    Returns ``{}`` on any error so an unparseable config never breaks
    the prompt build. Called once per ``build()`` invocation; PyYAML
    loads from disk are sub-millisecond on the small profile config so
    no caching is needed.
    """
    try:
        from pathlib import Path

        from opencomputer.agent.profile_yaml import get_custom_personalities

        home = os.environ.get(
            "OPENCOMPUTER_HOME",
            str(Path.home() / ".opencomputer"),
        )
        profile = os.environ.get("OPENCOMPUTER_PROFILE", "default")
        return get_custom_personalities(Path(home) / profile / "config.yaml")
    except Exception as exc:  # noqa: BLE001 — never break prompt build
        _log.debug("loop: failed to load custom personalities — %s", exc)
        return {}


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
    stop_reason: StopReason | None = None
    """The terminal stop reason of the final step. ``None`` only when the
    loop exited via the no-message early return path (no model call
    made). Additive field (2026-05-02 Opus 4.7 migration) — existing
    callers that ignore it continue to work unchanged."""


#: Synthetic tool name used for Hybrid skill dispatch wrap. Must match
#: the ``name`` returned by :class:`SkillTool.schema`. Pulled into a
#: constant so a future tool rename surfaces here as a single-place
#: edit rather than a silent breakage.
SKILL_TOOL_NAME = "Skill"


#: Cache of provider-method signatures so we don't re-introspect on
#: every turn. Keyed by ``(class_qualname, method_name)`` (stable across
#: bound-method ``id`` churn) and bounded at 64 entries to prevent any
#: long-running daemon from leaking memory through the cache.
#: Audit BLOCKER 3 (post-PR review): keying by ``id(method)`` was wrong
#: because bound methods get a fresh ``id`` per attribute access; the
#: cache leaked unboundedly AND ``id`` reuse after GC could return stale
#: signatures from an entirely different method.
_PROVIDER_SIG_CACHE: dict[tuple[str, str], frozenset[str]] = {}
_PROVIDER_SIG_CACHE_MAX = 64


def _maybe_split_system_kwargs(
    method: Any,
    *,
    base_system: str,
    injected_system: str,
    session_id: str | None,
) -> dict[str, Any]:
    """Return only the split-system kwargs that ``method`` accepts.

    The 2026-05-05 cache-correctness fix added ``base_system``,
    ``injected_system``, and ``session_id`` as new kwargs on
    ``BaseProvider.complete``/``stream_complete``. Stub providers in
    tests and 3rd-party plugins that haven't adopted the new signature
    would otherwise raise ``TypeError``. This helper introspects the
    target method once (cached by class+name) and only passes kwargs
    the method actually accepts.
    """
    import inspect as _inspect
    import logging as _logging

    # Stable key: ``(class_qualname, method_name)`` survives bound-method
    # id churn. Falls back to ``("unknown", repr(method))`` for anything
    # that isn't an attribute of a class (functools.partial, lambdas).
    self_obj = getattr(method, "__self__", None)
    func_name = getattr(method, "__name__", "")
    if self_obj is not None and func_name:
        cls = type(self_obj)
        cache_key = (f"{cls.__module__}.{cls.__qualname__}", func_name)
    else:
        cache_key = ("unknown", repr(method))

    accepted = _PROVIDER_SIG_CACHE.get(cache_key)
    if accepted is None:
        try:
            sig = _inspect.signature(method)
            accepted = frozenset(sig.parameters)
            # Methods declared with **kwargs accept anything; treat as
            # accepting all the new kwargs.
            if any(
                p.kind == _inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            ):
                accepted = frozenset(
                    list(accepted)
                    + ["base_system", "injected_system", "session_id"]
                )
        except (ValueError, TypeError):
            # Rare — typically C-implemented callables. Log so a silently
            # broken provider surfaces in logs rather than dropping
            # session_id forever.
            _logging.getLogger(__name__).warning(
                "inspect.signature failed for %r; split-system kwargs "
                "will not be forwarded for this provider.",
                cache_key,
            )
            accepted = frozenset()
        # Bound size — drop oldest insertion order (Python 3.7+ dict
        # iteration is insertion-ordered).
        if len(_PROVIDER_SIG_CACHE) >= _PROVIDER_SIG_CACHE_MAX:
            _PROVIDER_SIG_CACHE.pop(next(iter(_PROVIDER_SIG_CACHE)))
        _PROVIDER_SIG_CACHE[cache_key] = accepted

    out: dict[str, Any] = {}
    if "base_system" in accepted:
        out["base_system"] = base_system
    if "injected_system" in accepted:
        out["injected_system"] = injected_system
    if "session_id" in accepted:
        out["session_id"] = session_id
    return out


def _make_cancelled_result(call: ToolCall, partial_stdout: str = "") -> Any:
    """Build a ToolResult marking a tool as cancelled mid-flight by /steer.

    PR-A Feature 1 helper. For Bash with non-empty captured stdout, the
    partial output is included so the model can use it on replan; for
    every other tool, a bare cancellation marker is emitted.

    Imported lazily from plugin_sdk inside the body to avoid widening
    the module-level import set (ToolResult is already imported within
    ``_dispatch_tool_calls`` for the same reason).
    """
    from plugin_sdk.core import ToolResult

    if call.name == "Bash" and partial_stdout:
        content = (
            f"<INTERRUPTED-BY-STEER> partial stdout:\n{partial_stdout}\n"
            "(remaining work cancelled by user steer)"
        )
    else:
        content = (
            f"<INTERRUPTED-BY-STEER> tool '{call.name}' cancelled by "
            "user steer; no partial output captured"
        )
    return ToolResult(
        call_id=call.id or "",
        content=content,
        is_error=False,
    )


def _wrap_skill_result_as_tool_messages(
    *,
    skill_name: str,
    args: str,
    result,  # SlashCommandResult — typed loosely to avoid import cycle
) -> list[Message]:
    """Hybrid dispatch — wrap a skill-source slash result as a synthetic
    ``Skill`` tool_use + tool_result message pair.

    Returns an empty list when ``result.source != "skill"`` so the caller
    falls through to the existing user/assistant emission for command
    results.

    The model receives the SKILL body as a tool_result on the next turn
    — exactly the shape it would see if it had auto-invoked SkillTool.
    Claude-Code parity for the dispatch path.

    Trade-off note: an alternative was to discard ``result.output`` and
    let the agent invoke ``SkillTool`` naturally on the next turn. We
    synthesize both halves instead because (a) the fallback already
    loaded SKILL.md — re-loading is wasteful — and (b) the natural-
    invoke path requires intercepting model output to inject a tool_use,
    a much uglier control-flow change than this branch.
    """
    import secrets

    if getattr(result, "source", "command") != "skill":
        return []
    call_id = f"toolu_skill_{secrets.token_hex(6)}"
    tool_call = ToolCall(
        id=call_id,
        name=SKILL_TOOL_NAME,
        arguments={"name": skill_name, "args": args or ""},
    )
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[tool_call],
    )
    tool_message = Message(
        role="tool",
        content=result.output,
        tool_call_id=call_id,
        name=SKILL_TOOL_NAME,
    )
    return [assistant, tool_message]


def apply_inject_contexts(
    messages: list[Message], contexts: list[str]
) -> list[Message]:
    """Append ``contexts`` (joined with double newlines) to ``messages``.

    Used by the PRE_LLM_CALL fire-point in :meth:`AgentLoop._call_provider`
    to inject text returned by shell hooks via ``HookDecision.inject_context``.

    Mutation rules:

    * If ``contexts`` is empty or ``messages`` is empty → return
      ``messages`` unchanged.
    * If the last message is a plain user message (``role="user"``,
      no ``tool_call_id``, no ``tool_calls``) → return a new list with
      the last entry replaced by a copy whose ``content`` has the
      injected text appended (separated by a blank line).
    * Otherwise (last message is assistant / tool / contains tool
      linkage) → return a new list with one extra trailing ``user``
      message holding the injected text. Anthropic's API tolerates
      a trailing user message after a tool result; the next provider
      call sees a clean turn boundary.

    Pure function. Idempotent — calling with the same ``contexts``
    twice produces the same final-content (because the second call's
    content already starts with the injected text, but a real loop
    invocation only fires this once per turn).

    Why a separate helper rather than inline: testability. The full
    AgentLoop is heavy; a pure helper lets us unit-test the load-
    bearing mutation in isolation against the Hermes Doc-2 G4 spec.
    """
    if not contexts or not messages:
        return messages
    from dataclasses import replace as _dc_replace
    joined = "\n\n".join(contexts)
    last = messages[-1]
    if last.role == "user" and not last.tool_calls and not last.tool_call_id:
        new_content = (
            last.content + "\n\n" + joined
            if last.content
            else joined
        )
        return list(messages[:-1]) + [_dc_replace(last, content=new_content)]
    return list(messages) + [Message(role="user", content=joined)]


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


def _maybe_run_auto_prune(db: SessionDB, cfg: Config) -> None:
    """At AgentLoop startup, opportunistically delete stale sessions.

    No-op when both ``auto_prune_days`` and ``auto_prune_untitled_days``
    are zero (the default — auto-prune is opt-in). Logs the count to
    stderr when something was actually pruned so the operator notices.
    """
    sc = cfg.session
    if sc.auto_prune_days <= 0 and sc.auto_prune_untitled_days <= 0:
        return
    deleted = db.auto_prune(
        older_than_days=sc.auto_prune_days,
        untitled_days=sc.auto_prune_untitled_days,
        min_messages=sc.auto_prune_min_messages,
    )
    if deleted:
        import sys as _sys

        print(f"[oc] auto-pruned {deleted} stale session(s)", file=_sys.stderr)


def _apply_pending_profile_swap(
    runtime: object,
    *,
    memory: object,
    prompt_snapshots: dict | None,
    sid: str | None,
) -> str | None:
    """Apply a queued profile swap at turn entry.

    Sequence:
      1. Consume ``pending_profile_id`` (delegates to _profile_swap helper).
      2. If a swap occurred, rebind ``memory`` to the new profile_home.
      3. Evict the prompt-cache snapshot for ``sid`` so the next turn
         rebuilds the system prompt against the new SOUL.md/MEMORY.md.

    Returns the new active profile id, or None if no swap occurred.

    Plan 1 of 3 — see docs/superpowers/specs/2026-05-01-profile-ui-port-design.md.
    """
    from opencomputer.cli_ui._profile_swap import (
        consume_pending_profile_swap,
        init_active_profile_id,
    )
    from opencomputer.profiles import get_profile_dir

    init_active_profile_id(runtime)
    new_id = consume_pending_profile_swap(runtime)
    if new_id is None:
        return None

    # Rebind memory pointers to the new profile's home directory.
    # get_profile_dir() returns ~/.opencomputer/profiles/<name>/ for named
    # profiles and ~/.opencomputer/ for "default".
    new_home_root = get_profile_dir(None if new_id == "default" else new_id)
    new_home = new_home_root / "home"
    if memory is not None and hasattr(memory, "rebind_to_profile"):
        try:
            memory.rebind_to_profile(new_home)
        except Exception:  # noqa: BLE001 — don't roll back the user-visible swap
            _log.warning(
                "profile swap to %r succeeded but memory rebind failed; "
                "MEMORY/SOUL/USER will continue reading the previous profile "
                "until next session restart",
                new_id,
                exc_info=True,
            )

    # Evict the cached prompt snapshot for this session so the next turn
    # rebuilds against the new memory pointers.
    if prompt_snapshots is not None and sid is not None:
        prompt_snapshots.pop(sid, None)

    return new_id


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
        # ACP depth: tool_callback fires on tool start/complete. Re-set by
        # run_conversation; declared here so _dispatch_tool_calls callers that
        # bypass run_conversation don't hit AttributeError.
        self._tool_callback: Any = None
        # Wave 5 T17 closure — lazy session creation. ``_session_ensured`` is
        # the set of session_ids whose DB row has been written this loop's
        # lifetime; ``_pending_session_meta`` holds the platform/model/cwd
        # captured at run_conversation entry, applied lazily on first
        # ``_ensure_session_persisted`` call. Per-AgentLoop set (not per-sid)
        # so subagent recursion via DelegateTool doesn't leak across instances.
        self._session_ensured: set[str] = set()
        self._pending_session_meta: dict[str, dict[str, Any]] = {}
        self.db = db or SessionDB(config.session.db_path)
        # Opt-in: prune stale sessions per config.session.auto_prune_*.
        # Default disabled (auto_prune_days=0); never deletes anything
        # unless the operator explicitly opts in via config.yaml.
        _maybe_run_auto_prune(self.db, config)
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
        #: Hermes-parity Tier S (2026-04-30): set by
        #: :meth:`request_force_compaction` (e.g. via ``/compress`` slash);
        #: consumed once at the start of the next iteration of
        #: ``run_conversation``, then auto-cleared.
        self._force_compact_next_turn: bool = False
        #: v3.1 (2026-04-30): count persona flips within the current
        #: session. Reset on each ``run_conversation`` entry. Drives the
        #: ``suggest_profile_suggest_command`` Learning Moment (≥3 flips
        #: ⇒ surface ``/profile-suggest`` once per profile).
        self._persona_flips_in_session: int = 0
        #: Persona-uplift (2026-04-29): cached foreground-app value with
        #: 30s TTL so per-turn re-classification doesn't spawn osascript
        #: every turn. Empty string is a valid cache state.
        self._foreground_app_cache: str = ""
        self._foreground_app_cache_at: float = 0.0
        #: Stability gate state for re-classification: track candidate
        #: persona id + how many consecutive turns it has been seen.
        self._pending_persona_id: str = ""
        self._pending_persona_count: int = 0
        #: Cooldown counter — reset to 0 on a confirmed persona flip.
        #: Increments on every reclassify call. We refuse to flip again
        #: until this exceeds the cooldown threshold (3) — prevents
        #: thrash when the user briefly Cmd-Tabs between apps. The
        #: dirty-flag path (slash-command override) bypasses this
        #: cooldown so an explicit user choice always wins.
        self._reclassify_calls_since_flip: int = 999
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

        # Hermes B4 follow-up — record compaction LLM calls into ``llm_calls``
        # so insights reflects the *full* conversation cost, not just the
        # user-visible reply. Closure reads ``self._current_session_id``
        # at call-time so it picks up the active session even though
        # CompactionEngine is constructed once.
        def _record_compaction_usage(usage: Any) -> None:
            try:
                from opencomputer.agent.usage_pricing import record_call_from_usage

                provider_name = getattr(provider, "name", "") or type(
                    provider
                ).__name__.lower().replace("provider", "")
                record_call_from_usage(
                    db=self.db,
                    session_id=self._current_session_id or "",
                    provider=provider_name,
                    model=config.model.model,
                    usage=usage,
                    batch=False,
                )
            except Exception:  # noqa: BLE001
                pass  # best-effort telemetry

        engine_name = getattr(config.loop, "context_engine", "compressor")
        self.compaction = _ctx_registry.build(
            engine_name,
            provider=provider,
            model=config.model.model,
            disabled=compaction_disabled,
            memory_bridge=self.memory_bridge,
            usage_recorder=_record_compaction_usage,
        ) or CompactionEngine(
            provider=provider,
            model=config.model.model,
            disabled=compaction_disabled,
            memory_bridge=self.memory_bridge,
            usage_recorder=_record_compaction_usage,
            custom_providers=config.custom_providers,
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

        # OpenClaw 1.C — sliding-window repetition detector. Frames are
        # keyed on (session_id, delegation_depth) so a delegated subagent
        # (which gets a fresh AgentLoop in practice — verified Phase 0.8)
        # can't poison the parent's window even if a future refactor
        # hot-paths a single LoopDetector across loops. Default thresholds
        # are permissive; healthy sessions never trip.
        self._loop_detector = LoopDetector()

        # Wave-5 T1 — Hermes-port tool-loop guard. Detects identical
        # tool-name+args repetition within a turn and either warns
        # (default 10) or hard-stops (default 25). Distinct from
        # LoopDetector above (which is sliding-window across recent calls);
        # the guard catches deterministic tight loops the detector misses.
        # Reset by ``run_conversation`` at the start of each user turn.
        _guard_cfg = getattr(config.loop, "tool_guardrail", None)
        self._tool_guard = ToolLoopGuard(
            warn_at=getattr(_guard_cfg, "warn_at", 10) if _guard_cfg else 10,
            stop_at=getattr(_guard_cfg, "stop_at", 25) if _guard_cfg else 25,
            enabled=getattr(_guard_cfg, "enabled", True) if _guard_cfg else True,
        )

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
        thinking_callback=None,
        tool_callback=None,
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
        # Hermes-followup 2026-05-07 — publish (session_id, db) on
        # ContextVars so auxiliary callers (title-gen daemon thread,
        # judge-reviewer, dreaming, recall-synthesizer, aux_llm)
        # can record their LLM cost into ``llm_calls`` without
        # signature changes. Daemon threads spawned via copy_context()
        # inherit these.
        try:
            from opencomputer.agent.usage_pricing import set_active_session

            set_active_session(sid, self.db)
        except Exception:  # noqa: BLE001
            pass
        # Item 2 fix (2026-05-02): reset pause_turn counter per conversation.
        # Without this, a long-lived AgentLoop (gateway/daemon mode) handling
        # multiple sequential conversations would leak the counter — session B
        # could start with the cap already 1 or 2 and force premature END_TURN.
        self._pause_turn_count = 0

        # E7 (2026-05-04) — USER_PROMPT_SUBMIT fires once per inbound user
        # message so observers can scan natural-language intent (e.g.
        # PluginDemandTracker.scan_user_prompt records demand for plugins
        # the user mentions but hasn't enabled). Fire-and-forget — never
        # blocks the loop, never crashes it on hook init failure.
        try:
            from opencomputer.hooks.engine import engine as _hook_engine_ups
            from plugin_sdk.core import Message as _MessageUPS
            from plugin_sdk.hooks import HookContext as _HookContextUPS
            from plugin_sdk.hooks import HookEvent as _HookEventUPS

            _hook_engine_ups.fire_and_forget(
                _HookContextUPS(
                    event=_HookEventUPS.USER_PROMPT_SUBMIT,
                    session_id=sid,
                    message=_MessageUPS(role="user", content=user_message),
                    runtime=self._runtime,
                )
            )
        except Exception as _exc:  # noqa: BLE001 — never crash the loop
            _ups_log = logging.getLogger("opencomputer.agent.loop")
            # Promoted from DEBUG (2026-05-10): user-defined hooks
            # subscribed to USER_PROMPT_SUBMIT silently never fire if
            # this swallows. Keep non-fatal but visible.
            _ups_log.warning(
                "USER_PROMPT_SUBMIT fire failed for session %s: %s "
                "(loop continues; subscribed hooks did NOT fire this turn)",
                sid,
                _exc,
            )

        # OpenClaw 1.C — push the (session_id, delegation_depth) frame for
        # the repetition detector. Idempotent: re-entering the same session
        # (resume mid-stream after an exception) keeps existing history so
        # a model that was already looping doesn't get a clean slate.
        # Stash both keys on ``self`` so internal helpers don't have to
        # thread them through every call signature; ``finally`` pops the
        # frame at the bottom of this method.
        _loop_depth = self._runtime.delegation_depth
        self._loop_detector_session_id = sid
        self._loop_detector_depth = _loop_depth
        self._loop_detector.push_frame(sid, _loop_depth)

        # T1 of auto-skill-evolution plan — anchor session wall-clock start
        # so SessionEndEvent.duration_seconds is meaningful from every exit
        # path (including the slash-command early return below). Set BEFORE
        # any branching so a thrown exception in setup still carries a
        # sensible duration.
        _session_started_at = time.monotonic()
        _session_had_errors = False
        _session_end_reason = "completed"
        _session_iterations = 0

        # If this is a fresh session, capture the metadata for lazy persistence
        # and seed history from disk. Wave 5 T17 closure: the DB row is no
        # longer eagerly written here — it's deferred until the first
        # ``append_message`` / ``append_messages_batch`` callsite (via the
        # ``_ensure_session_persisted`` gate). A conversation that returns
        # before any message persists (slash-command-only turn, or Ctrl-C
        # before any reply) leaves no row.
        existing = self.db.get_session(sid) if session_id else None
        if existing is None:
            self._pending_session_meta[sid] = {
                "platform": "cli",
                "model": self.config.model.model,
                "cwd": os.getcwd(),  # Plan 3 — profile-analysis cwd-pattern signal
            }
            messages: list[Message] = []
            # Round 2B P-9: optional pre-seed for forked-context delegations.
            # ``initial_messages`` is only honoured for fresh sessions to keep
            # resume-from-disk deterministic. Seeded messages are persisted so
            # the on-disk session matches in-memory state.
            if initial_messages:
                messages.extend(initial_messages)
                self._ensure_session_persisted(sid)
                self._persist_messages_batch(sid, list(initial_messages))
        else:
            # Existing session — already in DB, just track as ensured.
            self._session_ensured.add(sid)
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
        from opencomputer.agent.slash_skill_fallback import make_skill_fallback
        from opencomputer.plugins.registry import registry as _plugin_registry

        # Tier 2.A — /<skill-name> auto-dispatch: when /foo doesn't match
        # a registered slash command, the dispatcher's fallback resolves
        # 'foo' as a skill id/name and returns its body inline.
        # Tier 2.A — Batch 2: plumb session-state references into
        # runtime.custom so slash commands like /branch /title /history
        # /save /agents can read the active session_id and a SessionDB
        # handle without needing a separate dispatcher signature change.
        # ``custom`` is a mutable dict on the otherwise-frozen
        # RuntimeContext, so we rebuild ``self._runtime`` with a copy
        # rather than mutating in place — otherwise loops that started
        # with ``DEFAULT_RUNTIME_CONTEXT`` (no explicit runtime passed,
        # e.g. test fixtures, scripted callers) would silently scribble
        # ``session_id`` / ``session_db`` onto the module-level
        # singleton and pollute every later consumer.
        # 2026-05-08 — also publish ``model_id`` and ``session_started_at``
        # so the bottom-bar status line (``cli_ui.status_line``) can read
        # them O(1) per keystroke without recomputing. ``session_started_at``
        # only seeds the first time it appears so a multi-turn chat REPL
        # keeps the original anchor and ``elapsed`` stays monotonic across
        # turns; ``model_id`` refreshes every turn to track mid-session
        # ``/model`` swaps.
        _new_custom = {
            **self._runtime.custom,
            "session_id": sid,
            "session_db": self.db,
            "model_id": self.config.model.model,
            # Wave 3 (2026-05-08) — publish per-model context-window
            # override sources so the status line bar can resolve the
            # correct max-context for *any* model the user runs,
            # without a hardcoded 200K assumption.
            "model_context_overrides": dict(getattr(self.config, "model_context_overrides", {}) or {}),
            "custom_providers": tuple(getattr(self.config, "custom_providers", ()) or ()),
        }
        if "session_started_at" not in _new_custom:
            _new_custom["session_started_at"] = _session_started_at
        self._runtime = replace(
            self._runtime,
            custom=_new_custom,
        )

        _slash_result = await _slash_dispatch(
            user_message,
            _plugin_registry.slash_commands,
            self._runtime,
            fallback=make_skill_fallback(self.memory),
        )
        if _slash_result is not None and _slash_result.handled:
            # Always emit the user message first.
            user_msg = Message(role="user", content=user_message)
            messages.append(user_msg)
            self._emit_before_message_write(session_id=sid, message=user_msg)
            self._persist_message(sid, user_msg)

            # Hybrid dispatch — skill-source result becomes a synthetic
            # SkillTool tool_use + tool_result pair so the model sees the
            # skill body as authoritative tool output. Command-source
            # result emits the standard assistant text reply + ends the
            # session (existing behavior preserved).
            from opencomputer.agent.slash_dispatcher import parse_slash

            parsed = parse_slash(user_message)
            skill_name = parsed[0] if parsed else ""
            args_str = parsed[1] if parsed else ""
            wrap = _wrap_skill_result_as_tool_messages(
                skill_name=skill_name, args=args_str, result=_slash_result
            )
            if wrap:
                # Skill — append the assistant tool_use + tool result, but
                # DO NOT end the session: fall through to the normal agent
                # loop so the model takes a turn on the skill content.
                for m in wrap:
                    messages.append(m)
                    self._emit_before_message_write(session_id=sid, message=m)
                    self._persist_message(sid, m)
                # Allow the loop to continue past this branch — the model
                # response from the next iteration is the assistant's
                # reply on top of the tool_result. We do NOT call
                # self.db.end_session(sid) here.
            else:
                # Command — preserve the original behavior.
                assistant_msg = Message(
                    role="assistant", content=_slash_result.output
                )
                messages.append(assistant_msg)
                self._emit_before_message_write(session_id=sid, message=assistant_msg)
                self._persist_message(sid, assistant_msg)
                self.db.end_session(sid)
                # OpenClaw 1.C — slash-command path bypasses the iteration
                # loop and its finally-block, so pop the detector frame here
                # to keep frame state symmetric with push_frame above.
                try:
                    self._loop_detector.pop_frame(sid, _loop_depth)
                except Exception:  # noqa: BLE001 — never break a slash-command return
                    _log.debug("loop_detector.pop_frame failed (slash path)", exc_info=True)
                # T1 of auto-skill-evolution plan — slash-command path is a
                # session terminal too; emit so subscribers see consistent
                # session-end coverage.
                await self._emit_session_end_event(
                    session_id=sid,
                    end_reason="completed",
                    turn_count=0,
                    duration_seconds=time.monotonic() - _session_started_at,
                    had_errors=False,
                )
                return ConversationResult(
                    final_message=assistant_msg,
                    messages=messages,
                    session_id=sid,
                    iterations=0,
                    input_tokens=0,
                    output_tokens=0,
                )

        # Plan 1 of 3 — UI port: apply queued profile swap (Ctrl+P or
        # /persona slash command). Idempotent on no-pending.
        # Placed AFTER slash-command early-return guards so the swap only
        # runs on turns that actually proceed to a model call.
        # ``_session_id`` is not stored on self; the local ``sid`` is used
        # directly. ``_current_session_id`` mirrors it but is set at line
        # 577 — using ``sid`` here is canonical and avoids any race.
        _apply_pending_profile_swap(
            self._runtime,
            memory=getattr(self, "memory", None),
            prompt_snapshots=getattr(self, "_prompt_snapshots", None),
            sid=sid,
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
                # Persona-uplift (2026-04-29): pass the just-arrived
                # ``user_message`` so initial classification sees the
                # current turn's content. Without this, _build_persona_overlay
                # classifies on the empty session-start history (likely
                # "coding" from foreground app) and then the per-turn
                # _maybe_reclassify_persona — which does see the user
                # message — reclassifies to e.g. "companion" and evicts
                # the just-built snapshot. Threading user_message in
                # keeps the two classifications consistent on turn 1.
                try:
                    persona_overlay = self._build_persona_overlay(
                        sid, user_message=user_message
                    )
                except Exception:  # noqa: BLE001 — defensive: never break loop
                    _log.debug(
                        "_build_persona_overlay failed; degrading to empty",
                        exc_info=True,
                    )
                    persona_overlay = ""
                # Prompt C (2026-04-28) — read the user's stated tone
                # preference from the F4 graph (the bootstrap quick-
                # interview's question 3 is persisted as a preference
                # node with a ``tone_preference:`` prefix). Same lane
                # as user_facts / persona_overlay so it lands on the
                # FROZEN base and prefix-cache stays warm. Empty string
                # when the user skipped the bootstrap question — base.j2
                # omits the ``<user-tone>`` block accordingly.
                try:
                    user_tone = self.prompt_builder.build_user_tone()
                except Exception:  # noqa: BLE001 — defensive: never break loop
                    _log.debug(
                        "build_user_tone failed; degrading to empty",
                        exc_info=True,
                    )
                    user_tone = ""
                # PR-6 T2.1: use build_with_memory so ambient memory blocks
                # from active providers are appended under '## Memory context'.
                # Falls back to the sync build() path if ambient blocks are
                # disabled or no bridge is wired. The snapshot is still frozen
                # per session — ambient blocks are evaluated once at session
                # start and cached, matching the prefix-cache invariant.
                from plugin_sdk import effective_permission_mode as _epm

                # 2026-05-10 — Pinned files (Optimize Grade E mitigation).
                # Render once per snapshot rebuild (not per turn) so the
                # prefix cache stays warm; the config tuple is part of the
                # snapshot key implicitly via the resulting prompt text.
                _pinned_files_block = ""
                try:
                    from opencomputer.agent.pinned_files import (
                        render_pinned_files_block,
                    )
                    _pcfg = getattr(self.config, "prompt", None)
                    _pinned_paths = getattr(_pcfg, "pinned_files", ()) if _pcfg else ()
                    _pinned_max = (
                        getattr(_pcfg, "max_total_bytes", 200_000)
                        if _pcfg else 200_000
                    )
                    if _pinned_paths:
                        _pinned_files_block = render_pinned_files_block(
                            _pinned_paths, max_total_bytes=_pinned_max
                        )
                except Exception:  # noqa: BLE001 — pinning never breaks the loop
                    _log.warning(
                        "pinned_files: render failed (skipping injection)",
                        exc_info=True,
                    )

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
                    permission_mode=_epm(self._runtime).value if self._runtime else "default",
                    personality=(
                        self._runtime.custom.get("personality", "")
                        if self._runtime else ""
                    ),
                    custom_personalities=_load_custom_personalities(),
                    persona_overlay=persona_overlay,
                    active_persona_id=self._active_persona_id,
                    user_tone=user_tone,
                    persona_preferred_tone=getattr(
                        self, "_active_persona_preferred_tone", ""
                    ),
                    pinned_files_block=_pinned_files_block,
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
        # Bug 1 fix (2026-05-05): keep ``base_system`` (frozen, gets the
        # cache marker) and ``injected`` (per-turn, never marked) as
        # SEPARATE strings. Providers that accept the new kwargs
        # (Anthropic) split into 2 system content blocks so the cached
        # prefix matches turn-to-turn regardless of injection volatility.
        # The concatenated form is preserved as ``system`` for legacy
        # providers / call sites that only know about a single string.
        #
        # ``injected_volatile`` accumulates ALL per-turn content that
        # gets appended below (memory prefetch, active memory, channel
        # prompt, channel skill bodies). It joins the injection-engine
        # output and is what we pass as ``injected_system`` to the
        # provider — so split-aware providers see ALL the volatile
        # content as un-marked tail, not just the engine compose result.
        # Audit BLOCKER 1 (post-PR review): without this, prefetched +
        # channel content that gets appended to ``system`` below was
        # being silently dropped by Anthropic's split-system path.
        # Track memory/channel content SEPARATELY from the engine-
        # compose result so the post-compaction recompose path (line
        # ~1500) can rebuild ``system`` + ``injected_volatile`` from
        # the new engine compose without losing memory/channel content.
        # Audit MAJOR 6 fix (post-PR review): pre-fix, the recompose
        # rebuilt ``system = base_system + injected`` and silently
        # dropped memory + channel content for legacy ``system=``
        # callers on retries.
        volatile_memory_blocks: list[str] = []
        injected_volatile = injected or ""
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
        self._persist_message(sid, user_msg)

        # BEFORE_TASK — blocking hook fired after the user message lands and
        # before any per-turn machinery starts. The social-traces plugin
        # (docs/plans/social-traces-plugin.md) uses this seam to query the
        # trace network and inject a matching <trace>...</trace> block as a
        # <system-reminder> user message. Slash-command-only turns bail
        # before reaching this point and skip the fire entirely — the seam
        # is "before the agent starts a real task" by design.
        #
        # Contract: a handler returning a HookDecision with non-empty
        # ``modified_message`` causes that text to be appended as a
        # <system-reminder> user message. ``decision="block"`` follows the
        # existing PreToolUse semantics — caller responsibility, but for
        # BEFORE_TASK we treat block the same as a hard inject (the reason
        # text becomes the reminder body) so a misuse can't wedge the loop.
        # Any failure (import, handler crash, malformed decision) is logged
        # and ignored — BEFORE_TASK must never break a normal turn.
        try:
            from opencomputer.hooks.engine import engine as _hook_engine_bt
            from plugin_sdk.hooks import HookContext as _HookContextBT
            from plugin_sdk.hooks import HookEvent as _HookEventBT

            _bt_decision = await _hook_engine_bt.fire_blocking(
                _HookContextBT(
                    event=_HookEventBT.BEFORE_TASK,
                    session_id=sid,
                    runtime=self._runtime,
                    message=user_msg,
                )
            )
        except Exception:  # noqa: BLE001 — never let BEFORE_TASK break the loop
            # Promoted from DEBUG (2026-05-10): BEFORE_TASK is the
            # primary user-extension point for pre-turn behaviors;
            # silent failure here = subscribers never run.
            _log.warning(
                "BEFORE_TASK fire failed for session %s (loop continues; "
                "subscribed hooks did NOT fire this turn)",
                sid,
                exc_info=True,
            )
            _bt_decision = None

        if (
            _bt_decision is not None
            and _bt_decision.decision != "pass"
            and _bt_decision.modified_message
        ):
            _bt_reminder = Message(
                role="user",
                content=f"<system-reminder>{_bt_decision.modified_message}</system-reminder>",
            )
            messages.append(_bt_reminder)
            # Persist so a resumed session sees the same injected context;
            # mirrors the loop-detector reminder path at lines ~1907-1920.
            self._emit_before_message_write(session_id=sid, message=_bt_reminder)
            self._persist_message(sid, _bt_reminder)

        # Persona-uplift (2026-04-29): per-turn re-classification with
        # stability gate + cooldown. Pass the in-memory ``messages`` list
        # so the helper doesn't re-read SQLite. On a confirmed flip the
        # snapshot for ``sid`` is evicted; the NEXT turn rebuilds the
        # system prompt with the new overlay. Defensive: never raises.
        try:
            self._maybe_reclassify_persona(sid, messages=messages)
        except Exception:  # noqa: BLE001
            _log.debug(
                "_maybe_reclassify_persona raised (suppressed)", exc_info=True
            )
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
            block = "## Relevant memory\n\n" + prefetched
            volatile_memory_blocks.append(block)
            system = system + "\n\n" + block
            injected_volatile = (
                injected_volatile + "\n\n" + block if injected_volatile else block
            )

        # v1.1 plan-3 M6.3 — MEMORY.md hybrid retrieval (BM25 + vector via RRF).
        # Order per the M6.1 brainstorm carry-forward note:
        #   [base + injected mode] + [Honcho prefetch] (above)
        #                         + [MEMORY.md retrieval]   ← THIS BLOCK
        #                         + [SessionDB FTS5 active memory]  (below)
        # Honcho first because its corpus is most variable; MEMORY.md
        # second because it changes only on explicit Memory tool writes;
        # FTS5 active memory third because it's per-session-episodic and
        # most volatile.  Default ON; gracefully degrades when MEMORY.md
        # is empty or the active provider lacks embeddings.
        if getattr(self.config.memory, "memory_md_retrieval_enabled", True):
            try:
                from opencomputer.agent.memory_md_retrieval import (
                    MemoryMdRetriever,
                )

                # The active provider's embed() is the embed_fn.  When the
                # provider lacks one (raises EmbeddingsUnsupportedError),
                # the retriever falls back to BM25-only with a one-time
                # WARNING log.
                embed_fn = None
                provider = getattr(self, "provider", None)
                if provider is not None and hasattr(provider, "embed"):
                    embed_fn = provider.embed

                retriever = MemoryMdRetriever(
                    self.memory,
                    embed_fn=embed_fn,
                    per_source_k=int(
                        getattr(
                            self.config.memory,
                            "memory_md_retrieval_per_source_k",
                            20,
                        )
                    ),
                    top_k=int(
                        getattr(
                            self.config.memory,
                            "memory_md_retrieval_top_k",
                            5,
                        )
                    ),
                )
                hits = await retriever.retrieve(user_message)
                md_block = retriever.inject_block(hits)
                if md_block:
                    volatile_memory_blocks.append(md_block)
                    system = system + "\n\n" + md_block
                    injected_volatile = (
                        injected_volatile + "\n\n" + md_block
                        if injected_volatile
                        else md_block
                    )
            except Exception as exc:  # noqa: BLE001 — never crash the loop on retrieval
                _log.warning("MEMORY.md retrieval failed: %s", exc)

        # OpenClaw 1.B-alt — local-FTS5 proactive recall prepend.
        # Composes with Honcho prefetch above + MEMORY.md retrieval block;
        # gated by config flag (default OFF).
        # Both append to the per-turn ``system`` so the prefix cache stays warm.
        if getattr(self.config.memory, "active_memory_enabled", False):
            from opencomputer.agent.active_memory import (
                ActiveMemoryConfig,
                ActiveMemoryInjector,
            )

            am_block = ActiveMemoryInjector(
                self.db,
                config=ActiveMemoryConfig(
                    enabled=True,
                    top_n=int(getattr(self.config.memory, "active_memory_top_n", 3)),
                ),
            ).recall_block(user_message)
            if am_block:
                block = "## Active memory\n\n" + am_block
                volatile_memory_blocks.append(block)
                system = system + "\n\n" + block
                injected_volatile = (
                    injected_volatile + "\n\n" + block
                    if injected_volatile
                    else block
                )

        # Hermes channel-port (PR 5): per-channel ephemeral system
        # prompt + auto-loaded skills, threaded in via
        # ``RuntimeContext.custom`` by ``Dispatch._build_channel_runtime``.
        # Lives on the per-turn ``system`` lane (NOT the FROZEN base) so
        # different DM-topics within the same chat don't poison each
        # other's prefix cache. Empty / missing keys are no-ops —
        # default (CLI / un-channelled) callers see exactly the
        # pre-PR-5 prompt.
        channel_prompt = self._runtime.custom.get("channel_prompt")
        if isinstance(channel_prompt, str) and channel_prompt.strip():
            block = "## Channel prompt\n\n" + channel_prompt.strip()
            volatile_memory_blocks.append(block)
            system = system + "\n\n" + block
            injected_volatile = (
                injected_volatile + "\n\n" + block if injected_volatile else block
            )
        channel_skill_bodies = self._runtime.custom.get("channel_skill_bodies")
        if channel_skill_bodies:
            blocks: list[str] = []
            for entry in channel_skill_bodies:
                # Tolerate both the canonical ``(skill_id, body)`` shape
                # and a bare ``body`` string for resilience against
                # third-party adapters that bypass the helper.
                # NOTE: distinct loop variables (``_sid``, ``_body``) so
                # the outer ``sid`` (session id) is not shadowed.
                if isinstance(entry, tuple) and len(entry) == 2:
                    _sid, _body = entry
                    blocks.append(f"### {_sid}\n\n{_body}")
                elif isinstance(entry, str) and entry.strip():
                    blocks.append(entry)
            if blocks:
                block = "## Channel skills (auto-loaded)\n\n" + "\n\n".join(blocks)
                volatile_memory_blocks.append(block)
                system = system + "\n\n" + block
                injected_volatile = (
                    injected_volatile + "\n\n" + block
                    if injected_volatile
                    else block
                )

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
        self._tool_callback = tool_callback  # ACP depth: fire on tool start/complete

        # Wave-5 T1 — clear the tool-loop guard's per-turn streak so a
        # repeated call from the previous turn doesn't pre-poison this one.
        self._tool_guard.reset()

        # T1 of auto-skill-evolution plan — wrap iteration loop +
        # budget-exhausted exit in try/except/finally so the agent
        # loop emits a SessionEndEvent at every terminal point
        # (END_TURN, budget-exhausted, timeout, cancellation, error).
        # Bus failure is swallowed inside _emit_session_end_event so
        # a broken bus cannot break the loop's return path.
        try:
            for _iter in range(self.config.loop.max_iterations):
                iterations += 1

                # 2026-05-08 — Hermes Doc-2 gateway hooks: agent:step.
                # Fires once per tool-calling iteration (one per LLM turn
                # within a multi-step session). Fire-and-forget so a slow
                # filesystem hook can't stall the loop. Only fires when
                # the gateway hook engine is available — this keeps the
                # CLI-only path (no gateway) free of an extra import.
                try:
                    from opencomputer.gateway.event_hooks import (
                        AGENT_STEP as _GW_AGENT_STEP,
                    )
                    from opencomputer.gateway.event_hooks import (
                        engine as _gw_hooks_engine_step,
                    )
                    if _gw_hooks_engine_step.hooks():
                        # Tool names from the most recent assistant
                        # message — empty until the LLM has called tools.
                        _last_tools: list[str] = []
                        for _m in reversed(messages):
                            if getattr(_m, "role", None) == "assistant":
                                _last_tools = [
                                    tc.name for tc in (
                                        getattr(_m, "tool_calls", None) or []
                                    )
                                ]
                                break
                        asyncio.create_task(
                            _gw_hooks_engine_step.fire(
                                _GW_AGENT_STEP,
                                {
                                    "session_id": sid,
                                    "iteration": iterations,
                                    "tool_names": _last_tools,
                                },
                            ),
                            name=f"gw-hook-agent-step-{iterations}",
                        )
                except Exception:  # noqa: BLE001 — never break the loop
                    pass

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
                            default_buffer as _steer_buffer,
                        )
                        from opencomputer.agent.steer import (
                            default_registry as _steer_registry,
                        )
                        from opencomputer.agent.steer import (
                            format_nudge_message as _format_nudge,
                        )

                        # PR-A Feature 1: peek cancel flag BEFORE consuming
                        # any state — drives the <USER-INTERRUPT> vs
                        # <USER-NUDGE> prefix decision.
                        _cancel_was_set = (
                            _steer_registry.has_cancel_listener(sid)
                            and _steer_registry.cancel_event(sid).is_set()
                        )
                        nudge = _steer_registry.consume(sid)
                        # Drain any inbound messages buffered during the
                        # cancel-pending window. Merge with the explicit
                        # nudge — explicit text wins position, buffered
                        # follow with '---' separator.
                        buffered = _steer_buffer.drain(sid)
                        if nudge and buffered:
                            merged: str | None = f"{nudge}\n---\n{buffered}"
                        elif nudge:
                            merged = nudge
                        elif buffered:
                            merged = buffered
                        else:
                            merged = None

                        if merged:
                            if _cancel_was_set:
                                _steer_registry.reset_cancel(sid)
                            nudge_msg = Message(
                                role="user",
                                content=_format_nudge(
                                    merged,
                                    was_interrupted=_cancel_was_set,
                                ),
                            )
                            messages.append(nudge_msg)
                            # Persist so a resumed session sees the same
                            # context (the nudge was already promised to
                            # the user; replaying without it would silently
                            # change the next turn's semantics).
                            self._persist_message(sid, nudge_msg)
                            _log.debug(
                                "steer: applied %s nudge for session %s "
                                "(len=%d, buffered_extras=%s)",
                                "interrupt" if _cancel_was_set else "pending",
                                sid,
                                len(merged),
                                "yes" if buffered else "no",
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
                        self._persist_message(sid, bg_msg)
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

                # /compress slash (2026-04-30): user-requested force-compact
                # consumed once at the start of any iteration, not just when
                # the threshold is hit. The flag is set via
                # ``request_force_compaction()`` and cleared after one use.
                _force_compact = bool(
                    getattr(self, "_force_compact_next_turn", False),
                )
                if _force_compact:
                    self._force_compact_next_turn = False

                # Compaction check — uses REAL measured tokens from prior turn.
                # First iteration (no prior measurement) skips the check
                # unless the user explicitly forced compaction.
                if self._last_input_tokens > 0 or _force_compact:
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
                    result = await self.compaction.maybe_run(
                        messages,
                        self._last_input_tokens,
                        force=_force_compact,
                    )
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
                        # Audit MAJOR 6 fix (post-PR review): rebuild
                        # BOTH ``system`` and ``injected_volatile`` to
                        # preserve memory + channel content
                        # (``volatile_memory_blocks``) alongside the
                        # newly-recomposed engine output. Pre-fix,
                        # legacy ``system=`` callers retried after
                        # compaction with memory + channel content
                        # silently dropped.
                        parts: list[str] = []
                        if injected:
                            parts.append(injected)
                        parts.extend(volatile_memory_blocks)
                        injected_volatile = "\n\n".join(parts) if parts else ""
                        system = base_system + (
                            "\n\n" + injected_volatile if injected_volatile else ""
                        )

                step = await self._run_one_step(
                    messages=messages,
                    system=system,
                    base_system=base_system,
                    injected_system=injected_volatile,
                    stream_callback=stream_callback,
                    thinking_callback=thinking_callback,
                    model=model_for_turn,
                    session_id=sid,
                )

                # Server-tool work paused: re-send so the provider continues.
                # Cap at 3 to prevent pathological loops on broken server tools.
                if step.stop_reason == StopReason.PAUSE_TURN:
                    self._pause_turn_count = (
                        getattr(self, "_pause_turn_count", 0) + 1
                    )
                    if self._pause_turn_count >= 3:
                        _log.warning(
                            "pause_turn cap (3) exceeded — forcing END_TURN. "
                            "A server tool may be stuck in a re-send loop.",
                        )
                        from dataclasses import replace as _dc_replace
                        step = _dc_replace(step, stop_reason=StopReason.END_TURN)
                        self._pause_turn_count = 0
                    else:
                        if step.assistant_message is not None:
                            messages.append(step.assistant_message)
                        continue
                else:
                    self._pause_turn_count = 0

                # Subsystem A — Context-full retry: compaction + retry once.
                # Provider-agnostic: any provider that maps its context-
                # exhaustion stop reason to StopReason.CONTEXT_FULL benefits.
                if (
                    step.stop_reason == StopReason.CONTEXT_FULL
                    and self.compaction is not None
                ):
                    try:
                        cresult = await self.compaction.maybe_run(
                            messages, step.input_tokens, force=True,
                        )
                        if cresult.did_compact:
                            messages = cresult.messages
                            step = await self._run_one_step(
                                messages=messages,
                                system=system,
                                base_system=base_system,
                                injected_system=injected,
                                stream_callback=stream_callback,
                                thinking_callback=thinking_callback,
                                model=model_for_turn,
                                session_id=sid,
                            )
                    except Exception:  # noqa: BLE001
                        pass

                # Empty end_turn retry: per Anthropic Doc 3, models can
                # return 2-3 empty tokens with stop_reason=end_turn after
                # tool results when text is appended in the same content
                # block, OR when the model considers itself done. This
                # looks like the agent hung. One-shot recovery: inject
                # a synthetic "Please continue." into the wire-only
                # message list and retry. Synthetic prompt is NOT
                # persisted to SessionDB.
                if (
                    step.stop_reason == StopReason.END_TURN
                    and not (step.assistant_message.content or "").strip()
                    and not step.assistant_message.tool_calls
                    and not step.assistant_message.reasoning
                ):
                    retry_messages = list(messages) + [
                        Message(role="user", content="Please continue."),
                    ]
                    try:
                        step = await self._run_one_step(
                            messages=retry_messages,
                            system=system,
                            base_system=base_system,
                            injected_system=injected,
                            stream_callback=stream_callback,
                            thinking_callback=thinking_callback,
                            model=model_for_turn,
                            session_id=sid,
                        )
                    except Exception:  # noqa: BLE001
                        # Retry crashed — accept the original empty turn.
                        pass

                # max_tokens + tool_use retry: when max_tokens is hit
                # DURING a tool_use block, the model emits a partial
                # tool call (truncated arguments) the dispatcher can't
                # execute. Per Anthropic Doc 3 fix-it pattern: retry
                # with doubled max_tokens (capped at 64k) once. Provider-
                # agnostic via canonical StopReason.MAX_TOKENS + presence
                # of tool_calls.
                if (
                    step.stop_reason == StopReason.MAX_TOKENS
                    and step.assistant_message.tool_calls
                ):
                    current_mt = self.config.model.max_tokens
                    lifted_mt = min(current_mt * 2, 64_000)
                    if lifted_mt > current_mt:
                        try:
                            step = await self._run_one_step(
                                messages=messages,
                                system=system,
                                base_system=base_system,
                                injected_system=injected,
                                stream_callback=stream_callback,
                                thinking_callback=thinking_callback,
                                model=model_for_turn,
                                session_id=sid,
                                max_tokens_override=lifted_mt,
                            )
                        except Exception:  # noqa: BLE001
                            # Retry crashed — accept original outcome.
                            pass

                # Round 2B P-3: a returned LLM response is activity. Bump BEFORE
                # the early-return path below so an end-turn turn that took 290s
                # still resets the timer for any caller that resumes the same
                # AgentLoop on the same session.
                self._last_activity_at = time.monotonic()
                self._last_input_tokens = step.input_tokens
                total_input += step.input_tokens
                total_output += step.output_tokens

                # OpenClaw 1.C — record assistant text into the repetition
                # detector. Only record non-empty text; an assistant turn
                # whose only payload is a tool_call has empty content and
                # would otherwise hash to a single constant that flags
                # every multi-tool-call session as a "repeat".
                _assistant_text = step.assistant_message.content or ""
                if _assistant_text.strip():
                    _text_hash = hashlib.sha256(
                        _assistant_text.encode("utf-8"),
                    ).hexdigest()[:16]
                    self._loop_detector.record_assistant_text(
                        sid, _loop_depth, _text_hash,
                    )
                    if self._loop_detector.must_stop(sid, _loop_depth):
                        raise LoopAbortError(
                            self._loop_detector.warning(sid, _loop_depth)
                            or "loop detector aborted",
                        )

                # PR #221 follow-up Item 2 — persist the per-turn deltas onto
                # the ``sessions`` row so ``/usage`` (and any future analytics)
                # can read real cumulative counts. ``add_tokens`` is a no-op
                # when both deltas are zero, so providers that don't surface
                # ``Usage`` produce no UPDATE traffic. Wrapped defensively:
                # an account-level SQLite error must never wedge the loop.
                try:
                    self.db.add_tokens(
                        sid,
                        step.input_tokens,
                        step.output_tokens,
                        cache_read_tokens=step.cache_read_tokens,
                        cache_write_tokens=step.cache_write_tokens,
                    )
                except Exception:  # noqa: BLE001
                    _log.debug(
                        "session token accumulation failed for sid=%s — continuing",
                        sid,
                        exc_info=True,
                    )

                # 2026-05-02: surface cumulative cache reads / writes on
                # runtime.custom so the ``/usage`` slash command can render
                # them. Defensive int-coerce — runtime.custom is shared
                # state and another component might have stomped a non-int
                # value; in that case start fresh.
                try:
                    _cur_cr = self._runtime.custom.get("session_cache_read")
                    _cur_cw = self._runtime.custom.get("session_cache_write")
                    self._runtime.custom["session_cache_read"] = (
                        int(_cur_cr) if isinstance(_cur_cr, int) else 0
                    ) + step.cache_read_tokens
                    self._runtime.custom["session_cache_write"] = (
                        int(_cur_cw) if isinstance(_cur_cw, int) else 0
                    ) + step.cache_write_tokens
                except Exception:  # noqa: BLE001
                    pass

                # 2026-05-08: token + cost accumulation for the bottom-bar
                # status line. ``session_tokens_in/out`` and
                # ``session_cost_usd`` are also consumed by ``/usage``,
                # which currently shows "(not tracked)" because nothing
                # writes them. Compaction's pricing helper handles the
                # provider/model lookup; an unknown model returns ``None``
                # and we leave ``session_cost_usd`` as-is rather than
                # zeroing it (sticky display).
                try:
                    _cur_in = self._runtime.custom.get("session_tokens_in")
                    _cur_out = self._runtime.custom.get("session_tokens_out")
                    self._runtime.custom["session_tokens_in"] = (
                        int(_cur_in) if isinstance(_cur_in, int) else 0
                    ) + int(step.input_tokens or 0)
                    self._runtime.custom["session_tokens_out"] = (
                        int(_cur_out) if isinstance(_cur_out, int) else 0
                    ) + int(step.output_tokens or 0)
                except Exception:  # noqa: BLE001
                    pass

                try:
                    from opencomputer.cost_guard.pricing import (
                        compute_call_cost as _ccc,
                    )

                    _step_cost = _ccc(
                        provider=getattr(
                            self.provider, "name", type(self.provider).__name__,
                        ),
                        model=self.config.model.model,
                        input_tokens=int(step.input_tokens or 0),
                        output_tokens=int(step.output_tokens or 0),
                    )
                    if _step_cost is not None:
                        _cur_cost = self._runtime.custom.get("session_cost_usd")
                        _cur_cost_f = (
                            float(_cur_cost)
                            if isinstance(_cur_cost, (int, float))
                            else 0.0
                        )
                        self._runtime.custom["session_cost_usd"] = (
                            _cur_cost_f + float(_step_cost)
                        )
                except Exception:  # noqa: BLE001
                    pass

                if not step.should_continue:
                    # No tool calls — safe to persist the assistant message alone. (PR #1)
                    # Passive education hook (2026-04-28): build a tail-clause
                    # "learning moment" reveal if any registered moment fires.
                    # Cap, dedup, and severity all enforced inside select_reveal.
                    # Best-effort — never raises into the loop.
                    final_assistant_msg = step.assistant_message
                    try:
                        from dataclasses import replace as _replace

                        from opencomputer.agent.config import _home as _profile_home_fn
                        from opencomputer.awareness.learning_moments import (
                            Context as _LMCtx,
                        )
                        from opencomputer.awareness.learning_moments import (
                            maybe_seed_returning_user as _seed_returning,
                        )
                        from opencomputer.awareness.learning_moments import (
                            select_reveal as _select_reveal,
                        )

                        _ph = _profile_home_fn()
                        _total_sessions = self.db.count_sessions()
                        _seed_returning(_ph, _total_sessions)
                        try:
                            _mem_text = self.config.memory.declarative_path.read_text(
                                encoding="utf-8",
                            )
                        except (OSError, UnicodeError):
                            _mem_text = ""
                        # vibe_log lives in PR #205 — degrade gracefully
                        # if this branch is rebased onto a base without it.
                        # Returning [] here just means the
                        # vibe_first_nonneutral moment never fires (the
                        # other two moments don't depend on vibe_log).
                        try:
                            _vibe_rows = self.db.list_vibe_log_for_session(sid)
                        except AttributeError:
                            _vibe_rows = []

                        # v2 fields: USER.md text + days_since_first_session.
                        try:
                            _user_md = self.config.memory.user_path.read_text(
                                encoding="utf-8",
                            )
                        except (OSError, UnicodeError):
                            _user_md = ""
                        try:
                            _first_session_ts = self.db.first_session_started_at()
                        except (AttributeError, Exception):  # noqa: BLE001
                            _first_session_ts = None
                        if _first_session_ts:
                            import time as _ttt
                            _days_since_first = max(
                                0.0, (_ttt.time() - _first_session_ts) / 86400.0,
                            )
                        else:
                            _days_since_first = 0.0

                        # v3 fields (2026-04-30) — slash-command suggestions.
                        # All wrapped defensively; any failure leaves the
                        # field at its zero/empty default and the moment
                        # silently no-ops on that field.
                        import os as _os_mod_lm

                        from plugin_sdk import (
                            effective_permission_mode as _eff_mode_lm,
                        )
                        try:
                            _perm_mode_str = (
                                _eff_mode_lm(self._runtime).name
                                if self._runtime else "DEFAULT"
                            )
                        except Exception:  # noqa: BLE001
                            _perm_mode_str = "DEFAULT"
                        # Count edits in assistant messages added since
                        # the most recent user message — this is "how
                        # many edits did the assistant make in the turn
                        # the user is now responding to?"
                        _edit_tool_names = {"Edit", "MultiEdit", "Write"}
                        _recent_edit_count = 0
                        for _msg in reversed(messages):
                            if _msg.role == "user":
                                break
                            if _msg.role == "assistant" and _msg.tool_calls:
                                for _tc in _msg.tool_calls:
                                    if _tc.name in _edit_tool_names:
                                        _recent_edit_count += 1
                        # Cumulative session tokens — read from the
                        # ``sessions`` row (state.add_tokens populates it
                        # each step). Default 0 if row missing.
                        try:
                            _sess_row = self.db.get_session(sid) or {}
                            _session_token_total = (
                                int(_sess_row.get("input_tokens", 0) or 0)
                                + int(_sess_row.get("output_tokens", 0) or 0)
                            )
                        except Exception:  # noqa: BLE001
                            _session_token_total = 0
                        _has_openai = bool(
                            _os_mod_lm.environ.get("OPENAI_API_KEY"),
                        )

                        # Default-arg binding pins the closure values to
                        # this iteration of the outer ``while iterations``
                        # loop — without it ruff B023 (and reality) flags
                        # v3.1 (2026-04-30) — profile-suggest moment fields.
                        _flips_count = self._persona_flips_in_session
                        _profile_name = "default"
                        try:
                            import os as _os_lm31
                            from pathlib import Path as _Path_lm31
                            _env_home = _os_lm31.environ.get("OPENCOMPUTER_HOME")
                            if _env_home:
                                _parts = _Path_lm31(_env_home).resolve().parts
                                if "profiles" in _parts:
                                    _idx = _parts.index("profiles")
                                    if _idx + 1 < len(_parts):
                                        _profile_name = _parts[_idx + 1]
                        except Exception:  # noqa: BLE001
                            _profile_name = "default"

                        # the late-bound capture as a footgun.
                        def _build_lm_ctx(
                            _ph_=_ph,
                            _mem_text_=_mem_text,
                            _vibe_rows_=_vibe_rows,
                            _total_sessions_=_total_sessions,
                            _sid_=sid,
                            _user_msg_=user_message or "",
                            _user_md_=_user_md,
                            _days_=_days_since_first,
                            _perm_=_perm_mode_str,
                            _edit_count_=_recent_edit_count,
                            _tokens_=_session_token_total,
                            _has_openai_=_has_openai,
                            _flips_=_flips_count,
                            _profile_=_profile_name,
                        ) -> _LMCtx:
                            return _LMCtx(
                                session_id=_sid_,
                                profile_home=_ph_,
                                user_message=_user_msg_,
                                memory_md_text=_mem_text_,
                                vibe_log_session_count_total=len(_vibe_rows_),
                                vibe_log_session_count_noncalm=sum(
                                    1 for r in _vibe_rows_
                                    if r.get("vibe") != "calm"
                                ),
                                sessions_db_total_sessions=_total_sessions_,
                                user_md_text=_user_md_,
                                days_since_first_session=_days_,
                                permission_mode_str=_perm_,
                                recent_edit_count_this_turn=_edit_count_,
                                session_token_total=_tokens_,
                                has_openai_key=_has_openai_,
                                persona_flips_in_session=_flips_,
                                current_profile_name=_profile_,
                            )

                        _reveal = _select_reveal(
                            ctx_builder=_build_lm_ctx, profile_home=_ph,
                        )
                        if _reveal:
                            final_assistant_msg = _replace(
                                step.assistant_message,
                                content=(step.assistant_message.content or "") + _reveal,
                            )
                    except Exception:  # noqa: BLE001 — never break the turn
                        # Promoted from DEBUG (2026-05-10): learning
                        # moments are user-visible suggestions; silent
                        # failure = user never sees the hint they would
                        # have benefited from.
                        _log.warning(
                            "learning_moments hook failed for session %s "
                            "(loop continues; user did NOT see the "
                            "contextual suggestion this turn)",
                            sid,
                            exc_info=True,
                        )

                    messages.append(final_assistant_msg)
                    self._emit_before_message_write(
                        session_id=sid, message=final_assistant_msg
                    )
                    self._persist_message(sid, final_assistant_msg)
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
                    # Auto-titler intentionally DISABLED (2026-04-28).
                    # The cheap-LLM call frequently returned a generic
                    # "Hello! I'm Claude, an AI assistant made by
                    # Anthropic..." greeting as the "title", which the
                    # new corner indicator (PR #214) then showed above
                    # the input — bad UX (user feedback Image #12).
                    # Until we have a more reliable summarizing prompt
                    # or a smaller dedicated title model, titles are
                    # only set via explicit ``/rename``. The corner
                    # indicator hides itself when no title is present,
                    # so fresh sessions show no clutter.
                    pass
                    # Wave 5 T2 closure — Ralph-loop continuation gate. If
                    # an active /goal exists and isn't satisfied, fold the
                    # continuation prompt back into a recursive
                    # ``run_conversation`` so the next turn proceeds with
                    # all the existing turn machinery (compaction, hooks,
                    # tool dispatch, runtime). Stack depth bounded by
                    # ``goal.budget`` (default 20) — well below recursion
                    # limit. Skipped entirely when no goal is active.
                    _continuation_prompt: str | None = None
                    try:
                        _last_text = (
                            final_assistant_msg.content
                            if isinstance(final_assistant_msg.content, str)
                            else ""
                        )
                        _continuation_prompt = await self._maybe_continue_goal(
                            sid, _last_text,
                        )
                    except Exception:  # noqa: BLE001 — gate must never break the turn
                        _continuation_prompt = None
                    if _continuation_prompt:
                        # Don't end_session; the continuation re-enters the
                        # loop on the same sid and ConversationResult flows
                        # from the eventual non-continuation END_TURN.
                        return await self.run_conversation(
                            user_message=_continuation_prompt,
                            session_id=sid,
                            system_override=system_override,
                            runtime=runtime,
                            stream_callback=stream_callback,
                            thinking_callback=thinking_callback,
                            tool_callback=tool_callback,
                            system_prompt_override=system_prompt_override,
                        )
                    self.db.end_session(sid)
                    # M4.4: clear any active skill tool filter on END_TURN so
                    # subsequent turns aren't constrained.
                    try:
                        from opencomputer.agent.skill_tools_filter import (
                            clear_active_filter,
                        )
                        clear_active_filter()
                    except Exception:  # noqa: BLE001
                        pass
                    # 2026-05-08 — Hermes Doc-2 parity: TRANSFORM_LLM_OUTPUT
                    # fires once per turn after the final response is
                    # assembled, before delivery. Handlers may return
                    # ``HookDecision(decision="rewrite", rewritten_text=...)``
                    # to replace the response delivered to the channel /
                    # console. The persisted DB content is the original
                    # (rewriting is "for delivery only" — symmetric with
                    # TRANSFORM_TOOL_RESULT). Fail-open: any exception
                    # leaves the original content intact.
                    try:
                        _final_text = (
                            final_assistant_msg.content
                            if isinstance(final_assistant_msg.content, str)
                            else ""
                        )
                        if _final_text:
                            from opencomputer.hooks.engine import (
                                engine as _hook_engine_xllm,
                            )
                            from plugin_sdk.hooks import (
                                HookContext as _HookContextXllm,
                            )
                            from plugin_sdk.hooks import (
                                HookEvent as _HookEventXllm,
                            )

                            _decision = await _hook_engine_xllm.fire_blocking(
                                _HookContextXllm(
                                    event=_HookEventXllm.TRANSFORM_LLM_OUTPUT,
                                    session_id=sid,
                                    response_text=_final_text,
                                    model=self.config.model.model,
                                    runtime=self._runtime,
                                )
                            )
                            if (
                                _decision is not None
                                and _decision.decision == "rewrite"
                                and _decision.rewritten_text
                            ):
                                from dataclasses import replace as _replace_

                                final_assistant_msg = _replace_(
                                    final_assistant_msg,
                                    content=_decision.rewritten_text,
                                )
                    except Exception:  # noqa: BLE001 — never break delivery
                        _log.debug(
                            "TRANSFORM_LLM_OUTPUT hook failed", exc_info=True
                        )

                    # Fire-and-forget auto-titler. Skips internally if the
                    # session already has a title or this isn't the first
                    # exchange. Without this wire-in the picker shows
                    # "(untitled · ID)" forever — see TS-T6 lineage.
                    #
                    # ``maybe_auto_title`` counts user-role messages to
                    # gate "is this the first exchange?". Tool-result
                    # messages also have role="user" in the canonical
                    # Anthropic shape (content is a list of tool_result
                    # blocks), so we filter them out before counting —
                    # otherwise a first turn with multiple tool calls
                    # would spuriously trip the >2 cutoff.
                    try:
                        from opencomputer.agent.title_generator import (
                            maybe_auto_title,
                        )

                        _final_text_for_title = ""
                        if isinstance(final_assistant_msg.content, str):
                            _final_text_for_title = final_assistant_msg.content
                        elif isinstance(final_assistant_msg.content, list):
                            _parts: list[str] = []
                            for _part in final_assistant_msg.content:
                                if isinstance(_part, dict) and _part.get("type") == "text":
                                    _parts.append(_part.get("text", ""))
                            _final_text_for_title = "".join(_parts)

                        _real_user_history = [
                            _m
                            for _m in messages
                            if getattr(_m, "role", None) == "user"
                            and isinstance(getattr(_m, "content", None), str)
                        ]
                        maybe_auto_title(
                            self.db,
                            sid,
                            user_message,
                            _final_text_for_title,
                            _real_user_history,
                        )
                    except Exception:  # noqa: BLE001 — auto-title is best-effort
                        # Promoted from DEBUG (2026-05-10): the entire
                        # reason this code path exists is so the
                        # session gets a non-empty title in the resume
                        # picker. Silent failure here = user keeps
                        # seeing "(untitled · ID)" forever.
                        _log.warning(
                            "maybe_auto_title spawn failed for session %s "
                            "(loop continues; this session will remain "
                            "untitled in `oc resume` picker)",
                            sid,
                            exc_info=True,
                        )

                    return ConversationResult(
                        final_message=final_assistant_msg,
                        messages=messages,
                        session_id=sid,
                        iterations=iterations,
                        input_tokens=total_input,
                        output_tokens=total_output,
                        stop_reason=step.stop_reason,
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

                # Hermes spec parity (2026-05-08): CronTool reads ``cron_session``
                # from runtime to block recursive cron management. Mirror the
                # DelegateTool runtime-propagation pattern.
                try:
                    from opencomputer.tools.cron_tool import CronTool
                    CronTool.set_runtime(self._runtime)
                except Exception:
                    pass  # cron tool may not be registered in some contexts

                # v1.1 plan-2 M5.2 (2026-05-09): snapshot the message
                # history BEFORE dispatching this tool block so a later
                # `oc session rewind --mode conv_only` can restore state
                # at this exact point. Best-effort — checkpoint failures
                # never wedge the loop. Skipped on the first iteration
                # if the session is fresh (no messages yet — nothing to
                # restore TO).
                if step.assistant_message.tool_calls and messages:
                    try:
                        from opencomputer.agent.checkpoint_manager import (
                            CheckpointManager,
                        )

                        _cp_mgr = CheckpointManager(self.db)
                        _msg_dicts = [
                            _msg_to_dict(m) for m in messages
                        ]
                        _cp_mgr.create(
                            session_id=sid,
                            messages=_msg_dicts,
                            label=f"before tool_use turn={iterations}",
                        )
                    except Exception:  # noqa: BLE001 — never wedge the loop
                        # Promoted from _log.debug (2026-05-10): silent-debug
                        # swallowing meant prompt_checkpoints stayed empty in
                        # the user's DB for weeks with zero diagnostic. WARNING
                        # is still non-fatal but visible at the default log
                        # level so the failure mode surfaces in production.
                        _log.warning(
                            "M5.2: checkpoint create failed for session %s "
                            "(suppressed; agent loop continues). "
                            "`oc session rewind --mode conv_only` will have "
                            "no rollback point for this turn.",
                            sid,
                            exc_info=True,
                        )

                # Dispatch tools BEFORE persisting the assistant message. If we saved
                # it first and then got cancelled mid-dispatch, the DB would hold a
                # tool_use with no matching tool_result — Anthropic 400s on resume.
                # Atomic batch persist below restores the invariant.
                # 2026-05-10 — bind turn_index to the ContextVar so tools
                # like RecallTool can attribute rows to (session_id,
                # turn_index) without plumbing it through ToolCall args.
                # session_id is already bound at session-creation time
                # by SessionDB.create_session.
                try:
                    from opencomputer.observability.logging_config import (
                        set_turn_index,
                    )

                    set_turn_index(int(iterations))
                except Exception:  # noqa: BLE001 — never break dispatch
                    _log.debug(
                        "set_turn_index failed; recall citations may "
                        "miss turn attribution this iteration",
                        exc_info=True,
                    )
                tool_results = await self._dispatch_tool_calls(
                    step.assistant_message.tool_calls or [],
                    session_id=sid,
                    turn_index=iterations,
                )
                # T1 of auto-skill-evolution plan — observe is_error flags
                # so SessionEndEvent.had_errors reflects the truth even when
                # the loop terminates cleanly via END_TURN.
                if any(getattr(r, "is_error", False) for r in tool_results):
                    _session_had_errors = True

                # v1.1 plan-2 M5.4 follow-up (2026-05-09): if any of the
                # just-dispatched tool calls was ExitPlanMode AND it left
                # a next_mode proposal in the slot, mutate this loop's
                # RuntimeContext.permission_mode now so subsequent turns
                # in the same session pick up the new mode without the
                # user having to run `/exit-plan <mode>` manually.
                # ``keep`` means stay in plan mode — leave runtime alone.
                _exit_plan_called = any(
                    (getattr(_tc, "name", "") == "ExitPlanMode")
                    for _tc in (step.assistant_message.tool_calls or [])
                )
                if _exit_plan_called:
                    self._maybe_apply_exit_plan_proposal()
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
                self._persist_messages_batch(sid, turn_messages)

                # OpenClaw 1.C — record each tool call into the repetition
                # detector AFTER dispatch (we want to see the args the agent
                # actually executed, including any TRANSFORM_TOOL_RESULT
                # mutations). On flag: append a single ``<system-reminder>``
                # to the user-side of the conversation so the next LLM call
                # sees it. On must-stop: raise ``LoopAbortError`` — the outer
                # except handler below converts it into a clean final
                # message rather than letting the model spin.
                _flagged_this_turn = False
                for _tc in step.assistant_message.tool_calls or []:
                    try:
                        _args_blob = json.dumps(
                            _tc.arguments or {}, sort_keys=True, default=str,
                        )
                    except (TypeError, ValueError):
                        # Args contained something json can't serialise even
                        # with default=str — fall back to repr so we still
                        # get a stable hash. Repetition detection on
                        # un-hashable args is best-effort by definition.
                        _args_blob = repr(_tc.arguments)
                    _args_hash = hashlib.sha256(
                        _args_blob.encode("utf-8"),
                    ).hexdigest()[:16]
                    self._loop_detector.record_tool_call(
                        sid, _loop_depth, _tc.name, _args_hash,
                    )
                    if self._loop_detector.must_stop(sid, _loop_depth):
                        raise LoopAbortError(
                            self._loop_detector.warning(sid, _loop_depth)
                            or "loop detector aborted",
                        )
                    if (
                        not _flagged_this_turn
                        and self._loop_detector.flagged(sid, _loop_depth)
                    ):
                        _flagged_this_turn = True
                if _flagged_this_turn:
                    _warning = self._loop_detector.warning(sid, _loop_depth)
                    _reminder = Message(
                        role="user",
                        content=f"<system-reminder>{_warning}</system-reminder>",
                    )
                    messages.append(_reminder)
                    # Persist so a resumed session sees the same context;
                    # silently dropping the nudge would let the model
                    # repeat the same loop on next start.
                    self._emit_before_message_write(
                        session_id=sid, message=_reminder,
                    )
                    self._persist_message(sid, _reminder)

            # Budget exhausted
            final = Message(
                role="assistant",
                content="[loop iteration budget exhausted — agent did not finish]",
            )
            messages.append(final)
            self._emit_before_message_write(session_id=sid, message=final)
            self._persist_message(sid, final)
            self.db.end_session(sid)
            return ConversationResult(
                final_message=final,
                messages=messages,
                session_id=sid,
                iterations=iterations,
                input_tokens=total_input,
                output_tokens=total_output,
                stop_reason=StopReason.BUDGET_EXHAUSTED,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            _session_end_reason = "cancelled"
            raise
        except LoopTimeout:
            _session_end_reason = "timeout"
            _session_had_errors = True
            raise
        except LoopAbortError as exc:
            # OpenClaw 1.C — anti-loop / repetition detector signalled
            # ``must_stop()``. Surface a single clean assistant message
            # rather than re-raising so CLI/gateway callers don't have
            # to special-case a new exception type. Persist the synthetic
            # assistant turn so a resumed session sees the same final
            # state. ``end_reason`` flags this as an error-class exit so
            # the SessionEndEvent reflects truth.
            _session_end_reason = "loop_aborted"
            _session_had_errors = True
            final = Message(
                role="assistant",
                content=f"Agent loop stopped: {exc}",
            )
            messages.append(final)
            self._emit_before_message_write(session_id=sid, message=final)
            self._persist_message(sid, final)
            self.db.end_session(sid)
            return ConversationResult(
                final_message=final,
                messages=messages,
                session_id=sid,
                iterations=iterations,
                input_tokens=total_input,
                output_tokens=total_output,
                stop_reason=StopReason.ERROR,
            )
        except Exception:
            _session_end_reason = "error"
            _session_had_errors = True
            raise
        finally:
            _session_iterations = iterations
            # OpenClaw 1.C — pop the detector frame so a long-running
            # daemon doesn't accumulate one frame per session id forever.
            # ``pop_frame`` is safe-on-absent so a path that never made it
            # past ``push_frame`` (rare; only if the push itself raised)
            # still tears down cleanly.
            try:
                self._loop_detector.pop_frame(sid, _loop_depth)
            except Exception:  # noqa: BLE001 — never let teardown break the loop
                _log.debug("loop_detector.pop_frame failed", exc_info=True)
            await self._emit_session_end_event(
                session_id=sid,
                end_reason=_session_end_reason,
                turn_count=_session_iterations,
                duration_seconds=time.monotonic() - _session_started_at,
                had_errors=_session_had_errors,
            )

    # ─── V2.C-T5 persona auto-classifier ───────────────────────────

    def _build_persona_overlay(
        self, session_id: str, user_message: str = ""
    ) -> str:
        """Run the persona classifier and return the matched persona's overlay.

        V2.C-T5 — invoked once per session in the same lane as
        ``user_facts`` / ``workspace_context`` so the resulting overlay
        lands on the FROZEN base prompt and the prefix cache stays warm.

        Pulls a SIMPLIFIED context for V2.C: foreground app via
        ``osascript`` (macOS only, "" elsewhere), current hour, last 10
        recent file paths from the session message log (best effort), and
        the last 3 user messages. ``user_message`` (the just-arrived
        turn's content) is appended to the message list so initial
        classification sees the same content as per-turn re-classification
        — see persona-uplift 2026-04-29 for the asymmetry that prompted
        this. Any failure degrades to ``""`` (no persona section in the
        prompt) — startup must NEVER break over a classifier issue. V2.D
        may swap in a richer context source.
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

        # Persona-uplift (2026-04-29): user override wins over the
        # auto-classifier. ``runtime.custom["persona_id_override"]`` is
        # set by the ``/persona-mode <id>`` slash command. An invalid id
        # (e.g. user-deleted persona) falls through to the classifier
        # path so the agent never wedges over a bad override.
        override_id = ""
        rt = getattr(self, "_runtime", None)
        if rt is not None:
            override_id = str(
                rt.custom.get("persona_id_override", "") or ""
            ).strip()

        if override_id:
            override_persona = get_persona(override_id)
            if override_persona is not None:
                self._active_persona_id = str(override_id)
                if rt is not None:
                    rt.custom["active_persona_id"] = self._active_persona_id
                self._active_persona_preferred_tone = str(
                    override_persona.get("preferred_tone", "") or ""
                ).strip()
                overlay = override_persona.get("system_prompt_overlay", "") or ""
                return str(overlay).strip()
            # Invalid override id — log and fall through. We do NOT
            # clear the override; the user can fix or `/persona-mode auto`.
            _log.debug(
                "persona override id %r not found; falling through to classifier",
                override_id,
            )

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

        # Persona-uplift (2026-04-29): append the just-arrived user
        # message so the initial classification sees this turn's content.
        # Without this, _build_persona_overlay (snapshot-build path) and
        # _maybe_reclassify_persona (per-turn path) disagree on turn 1
        # and cause an immediate snapshot eviction.
        if user_message:
            last_user_messages = tuple(
                list(last_user_messages) + [user_message]
            )[-3:]

        # v2 fields (2026-05-01) — window title + profile_home for
        # priors lookup. Best-effort.
        try:
            from opencomputer.awareness.personas._foreground import (
                detect_window_title,
            )
            window_title = detect_window_title()
        except Exception:  # noqa: BLE001
            window_title = ""
        try:
            from opencomputer.agent.config import _home as _resolve_home_v2
            profile_home_v2 = str(_resolve_home_v2())
        except Exception:  # noqa: BLE001
            profile_home_v2 = ""

        try:
            ctx = ClassificationContext(
                foreground_app=foreground_app,
                time_of_day_hour=hour,
                recent_file_paths=recent_files,
                last_messages=last_user_messages,
                window_title=window_title,
                profile_home=profile_home_v2,
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
        # PR-5: mirror into runtime.custom so the TUI mode badge can surface
        # the active persona without needing a reference to the loop. Use
        # getattr defensively — some test fixtures construct AgentLoop-like
        # objects (or use mocks) without going through __init__.
        _rt = getattr(self, "_runtime", None)
        if _rt is not None and self._active_persona_id:
            _rt.custom["active_persona_id"] = self._active_persona_id
        # Prompt C follow-up (2026-04-28): expose the persona's
        # ``preferred_tone`` so prompt assembly can render it as a
        # ``<persona-tone>`` block (suppressed when user_tone is set —
        # user wins, code-level enforcement). Empty string when the
        # YAML has no field.
        self._active_persona_preferred_tone = str(
            persona.get("preferred_tone", "") or ""
        ).strip()
        overlay = persona.get("system_prompt_overlay", "") or ""
        overlay = str(overlay).strip()

        # Path A.3 (2026-04-27): when companion is the active persona,
        # peek the most-recent unconsumed Life-Event firing and append
        # it as a "RECENT LIFE EVENT" anchor. The reflective lane needs
        # real anchors to land — without them, the companion has nothing
        # specific to point at when asked "how are you?". The firing's
        # ``hint_text`` is concrete and actionable.
        # Path A.4 (2026-04-27, generalised 2026-04-28): vibe classification
        # runs on EVERY user turn regardless of active persona. The verdict
        # is persisted on ``sessions.vibe`` (most-recent) AND appended to
        # ``vibe_log`` (per-turn, with classifier_version) so:
        #   1. companion overlay still has continuity for "you sounded
        #      frustrated yesterday";
        #   2. offline analysis has a real corpus to A/B future classifier
        #      backends (regex vs embedding vs LLM) against the production
        #      baseline. Previously this entire branch was gated behind
        #      ``persona_id == "companion"`` which meant 100% NULL on
        #      non-companion sessions — i.e. zero evidence to learn from.
        try:
            from opencomputer.agent.vibe_classifier import classify_vibe

            if last_user_messages:
                current_vibe = classify_vibe(list(last_user_messages))
                self.db.set_session_vibe(session_id, current_vibe)
                self.db.record_vibe(
                    session_id,
                    current_vibe,
                    classifier_version="regex_v1",
                )
        except Exception:  # noqa: BLE001 — degrade silently
            # Promoted from DEBUG (2026-05-10): vibe_log table is the
            # data source for emotion/topic analytics; silent failure =
            # 0 rows growing, so analytics dashboards lie.
            _log.warning(
                "vibe-classify / per-turn log failed for session %s "
                "(loop continues; vibe_log row NOT written for this turn)",
                session_id,
                exc_info=True,
            )

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

        # Cross-persona previous-session vibe anchor (Prompt A, 2026-04-28).
        # The current session's vibe is set above unconditionally; this
        # block adds the *cross-session* anchor — what state the user was
        # carrying in from a prior session within the last ~72h. It used
        # to be gated to ``persona_id == "companion"`` but every persona
        # benefits from the continuity. Framing branches on persona so the
        # companion overlay's reflective register is preserved.
        #
        # Signal gate: skip the anchor entirely when the prior vibe was
        # ``"calm"`` — calm is the regex classifier's default fallback, so
        # injecting a "recent user state: calm" block adds noise without
        # signal. Worth re-thinking once a real-confidence backend ships.
        try:
            import time as _time2

            rows = self.db.list_recent_session_vibes(limit=10)
            cutoff = _time2.time() - (72 * 3600)
            prev = next(
                (
                    r for r in rows
                    if r.get("id") != session_id
                    and (r.get("vibe_updated") or 0) >= cutoff
                ),
                None,
            )
            if prev is not None and prev.get("vibe") and prev.get("vibe") != "calm":
                age_hours = (
                    _time2.time() - float(prev.get("vibe_updated") or 0)
                ) / 3600.0
                age_str = (
                    f"{age_hours:.0f}h ago"
                    if age_hours >= 1
                    else "less than an hour ago"
                )
                title = prev.get("title") or "(untitled session)"
                prev_vibe = prev.get("vibe")
                if result.persona_id == "companion":
                    overlay = (
                        overlay
                        + "\n\n## PREVIOUS-SESSION VIBE (anchor for the companion)\n\n"
                        + "User's apparent emotional state in their last "
                        + f"different session ({age_str}, '{title}'): "
                        + f"**{prev_vibe}**.\n\n"
                        + "If the user's tone now is markedly different, you "
                        + "can naturally reference the shift — 'you sounded "
                        + f"{prev_vibe} last we talked, this feels "
                        + "different — what changed?'. Don't force it; use "
                        + "only when the contrast is obvious."
                    )
                else:
                    overlay = (
                        overlay
                        + "\n\n## Recent user state\n\n"
                        + "User's apparent emotional state in their last "
                        + f"different session ({age_str}): **{prev_vibe}**.\n\n"
                        + "Useful background context only — don't reference "
                        + "it explicitly unless the current turn makes the "
                        + "contrast genuinely relevant."
                    )
        except Exception:  # noqa: BLE001 — degrade silently
            _log.debug(
                "previous-vibe lookup failed",
                exc_info=True,
            )

        # ─── Mechanism B (2026-04-28): learning-moment system-prompt overlay ──
        # Same lane as the persona overlay — fires once per profile,
        # ever, when a SYSTEM_PROMPT-surface moment matches at session
        # start. The text becomes a context anchor the LLM may weave
        # in if natural. Best-effort; no-op when nothing fires.
        try:
            from opencomputer.agent.config import _home as _profile_home_fn
            from opencomputer.awareness.learning_moments import (
                Context as _LMCtx,
            )
            from opencomputer.awareness.learning_moments import (
                select_system_prompt_overlay as _select_overlay,
            )

            _ph = _profile_home_fn()
            _total = self.db.count_sessions()
            _hits = self._compute_cross_session_topic_hits(session_id)

            # v3 (2026-04-30) — fields needed by mechanism-B v3 moments
            # (suggest_voice_for_voice_user, suggest_persona_for_companion_signals,
            # suggest_personality_after_friction). All best-effort.
            import os as _os_mod_b

            from plugin_sdk import (
                effective_permission_mode as _eff_mode_b,
            )
            try:
                _perm_b = (
                    _eff_mode_b(self._runtime).name
                    if self._runtime else "DEFAULT"
                )
            except Exception:  # noqa: BLE001
                _perm_b = "DEFAULT"
            try:
                _vibe_rows_b = self.db.list_vibe_log_for_session(session_id)
            except AttributeError:
                _vibe_rows_b = []
            _has_openai_b = bool(_os_mod_b.environ.get("OPENAI_API_KEY"))

            def _build_b_ctx(
                _ph_=_ph,
                _sid_=session_id,
                _total_=_total,
                _hits_=_hits,
                _user_msg_=user_message or "",
                _perm_=_perm_b,
                _vibe_rows_=_vibe_rows_b,
                _has_openai_=_has_openai_b,
            ) -> _LMCtx:
                return _LMCtx(
                    session_id=_sid_,
                    profile_home=_ph_,
                    user_message=_user_msg_,
                    memory_md_text="",
                    vibe_log_session_count_total=len(_vibe_rows_),
                    vibe_log_session_count_noncalm=sum(
                        1 for r in _vibe_rows_
                        if r.get("vibe") != "calm"
                    ),
                    sessions_db_total_sessions=_total_,
                    cross_session_topic_hits=_hits_,
                    permission_mode_str=_perm_,
                    has_openai_key=_has_openai_,
                )

            lm_overlay = _select_overlay(
                ctx_builder=_build_b_ctx, profile_home=_ph,
            )
            if lm_overlay:
                overlay = (
                    overlay
                    + "\n\n## CROSS-SESSION CONTEXT (learning-moment anchor)\n\n"
                    + lm_overlay
                )
        except Exception:  # noqa: BLE001 — never break loop on overlay miss
            # Promoted from DEBUG (2026-05-10): mechanism-B injects
            # learning-moments overlay into the system prompt; silent
            # failure means user never sees /skill, /commit etc. nudges.
            _log.warning(
                "learning_moments mechanism-B failed for session %s "
                "(loop continues; overlay NOT injected this turn)",
                session_id,
                exc_info=True,
            )

        return overlay

    # ─── Persona-uplift 2026-04-29 — adaptive classifier ──────────

    def _cached_foreground_app(self, now: float | None = None) -> str:
        """Return foreground app name with a 30-second TTL cache.

        Per-turn re-classification calls this on every user turn; the
        underlying ``detect_frontmost_app()`` spawns ``osascript`` with a
        2-second timeout which is too slow to run unconditionally.
        ``now`` is for testing — production callers omit it.
        """
        import time as _time

        from opencomputer.awareness.personas._foreground import (
            detect_frontmost_app,
        )

        if now is None:
            now = _time.monotonic()
        # 2026-05-01 — TTL dropped 30s → 5s. Old TTL meant alt-tab from
        # VS Code → trading app and immediate next message still saw the
        # stale "VS Code" classification for up to half a minute. 5s is
        # the sweet spot: short enough to catch app switches, long enough
        # to avoid spamming osascript at sub-second cadence during rapid
        # turns.
        if (
            self._foreground_app_cache_at != 0.0
            and now - self._foreground_app_cache_at < 5.0
        ):
            return self._foreground_app_cache
        try:
            value = detect_frontmost_app()
        except Exception:  # noqa: BLE001 — defensive: never break loop
            value = ""
        self._foreground_app_cache = value
        self._foreground_app_cache_at = now
        return value

    def _recent_user_messages(
        self, session_id: str, messages: list | None = None
    ) -> tuple[str, ...]:
        """Return the last 3 user-message contents for classifier context.

        Accepts ``messages`` from the caller (the loop already holds the
        in-memory list) to avoid re-reading the SQLite session DB. When
        ``messages`` is None, falls back to ``db.get_messages``.
        """
        if messages is None:
            try:
                messages = self.db.get_messages(session_id)
            except Exception:  # noqa: BLE001 — defensive
                return ()
        texts = [
            m.content for m in messages
            if getattr(m, "role", "") == "user"
            and isinstance(getattr(m, "content", None), str)
        ]
        return tuple(texts[-3:])

    def _maybe_reclassify_persona(
        self, session_id: str, messages: list | None = None
    ) -> None:
        """Per-turn re-classification with stability gate + cooldown.

        Called from the user-turn boundary in :meth:`run_conversation`
        AFTER the user message is persisted. ``messages`` is the
        in-memory message list the loop already holds; we accept it so
        we don't re-read from SQLite. ``messages=None`` falls back to
        ``db.get_messages(session_id)``.

        Behavior:
        - The slash-command dirty flag (``runtime.custom["_persona_dirty"]``)
          forces a snapshot evict regardless. Set by ``/persona-mode``;
          the slash-command path always wins.
        - When ``runtime.custom["persona_id_override"]`` is set, skip
          re-classification entirely.
        - Otherwise classify, apply stability gate (2 consecutive
          same-id matches OR confidence >= 0.85), then a cooldown gate
          (no flip within 3 reclassify calls of the last flip).
        - On a confirmed flip: update ``_active_persona_id``, mirror to
          ``runtime.custom``, reset pending + cooldown counters, evict
          ``_prompt_snapshots[session_id]``, and log at DEBUG level.

        Defensive: any failure is caught and logged; the active persona
        is left unchanged. The agent loop must NEVER break over a
        re-classification miss.
        """
        import datetime as _dt

        from opencomputer.awareness.personas.classifier import (
            ClassificationContext,
            classify,
        )

        rt = getattr(self, "_runtime", None)

        # Honour the slash-command dirty flag — the user just set or
        # cleared an override, snapshot must be rebuilt next turn even
        # if the active persona id didn't change. This bypasses the
        # cooldown — an explicit user choice always wins.
        if rt is not None and rt.custom.pop("_persona_dirty", False):
            try:
                self._prompt_snapshots.pop(session_id, None)
            except Exception:  # noqa: BLE001
                _log.debug(
                    "snapshot evict on _persona_dirty failed", exc_info=True
                )

        # Override-locked: skip the classifier entirely.
        if rt is not None and rt.custom.get("persona_id_override"):
            return

        # Cooldown bookkeeping happens BEFORE classify (so even a no-op
        # call increments). Cap at a large number to avoid overflow on
        # ultra-long sessions; any value >= 3 satisfies the threshold.
        self._reclassify_calls_since_flip = min(
            self._reclassify_calls_since_flip + 1, 1_000_000
        )

        # v2 fields (2026-05-01) — window title + profile_home for priors.
        try:
            from opencomputer.awareness.personas._foreground import (
                detect_window_title,
            )
            window_title_rc = detect_window_title()
        except Exception:  # noqa: BLE001
            window_title_rc = ""
        try:
            from opencomputer.agent.config import _home as _resolve_home_rc
            profile_home_rc = str(_resolve_home_rc())
        except Exception:  # noqa: BLE001
            profile_home_rc = ""

        try:
            ctx = ClassificationContext(
                foreground_app=self._cached_foreground_app(),
                time_of_day_hour=_dt.datetime.now().hour,
                recent_file_paths=(),  # not used for re-classification
                last_messages=self._recent_user_messages(session_id, messages),
                window_title=window_title_rc,
                profile_home=profile_home_rc,
            )
            result = classify(ctx)
        except Exception:  # noqa: BLE001 — defensive: never break loop
            _log.debug("re-classify failed; persona unchanged", exc_info=True)
            return

        # Already in the same persona — reset gate, done.
        if result.persona_id == self._active_persona_id:
            self._pending_persona_id = ""
            self._pending_persona_count = 0
            return

        # Stability gate: 2 consecutive matches required, OR confidence
        # >= 0.92 short-circuits (very strong signal).
        # 2026-05-01 — bumped 0.85 → 0.92 for v2 multi-signal classifier:
        # v2 reports higher confidence values for multi-signal hits, so
        # 0.85 was too easy a bar. 0.92 means "trading app + content
        # match" or "two strong signals" — those should short-circuit;
        # single-signal emotion detection (0.9) should still go through
        # the 2-consecutive-turns stability gate.
        flip_now = result.confidence >= 0.92
        if not flip_now:
            if result.persona_id == self._pending_persona_id:
                self._pending_persona_count += 1
                if self._pending_persona_count >= 2:
                    flip_now = True
            else:
                self._pending_persona_id = result.persona_id
                self._pending_persona_count = 1

        if not flip_now:
            return

        # Cooldown gate: refuse to flip again within 3 reclassify calls
        # of the last flip. Prevents thrashing when the user briefly
        # Cmd-Tabs between apps.
        if self._reclassify_calls_since_flip < 3:
            return

        prev = self._active_persona_id
        self._active_persona_id = result.persona_id
        self._pending_persona_id = ""
        self._pending_persona_count = 0
        self._reclassify_calls_since_flip = 0
        # v3.1 (2026-04-30): only count meaningful flips — first
        # classification (prev empty → set) doesn't count, only later
        # changes between two non-empty persona ids. Use getattr-default
        # so existing test fixtures that build a partial AgentLoop don't
        # trip an AttributeError on this counter.
        if prev and prev != result.persona_id:
            self._persona_flips_in_session = (
                getattr(self, "_persona_flips_in_session", 0) + 1
            )
        if rt is not None:
            rt.custom["active_persona_id"] = self._active_persona_id

        # Evict snapshot so the next turn rebuilds with the new overlay.
        try:
            self._prompt_snapshots.pop(session_id, None)
        except Exception:  # noqa: BLE001 — defensive
            _log.debug("snapshot evict on flip failed", exc_info=True)

        _log.debug(
            "persona_classifier.flip session=%s from=%s to=%s reason=%s",
            session_id,
            prev or "(unset)",
            self._active_persona_id,
            result.reason,
        )

    def _compute_cross_session_topic_hits(
        self, session_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """Pre-compute (topic, episodic_session_id) hits for Context.

        Looks at episodic events from the last 14 days that are NOT
        from the current session. Returns up to 3 hits as
        (topic_summary, session_id) tuples. Empty tuple on any error
        — the predicate handles ``len(hits) == 0`` gracefully.

        This is a session-start computation (called once from
        ``_build_persona_overlay``); the per-turn cost is zero.
        """
        try:
            import time as _t
            cutoff = _t.time() - (14 * 24 * 3600)
            rows = self.db.list_episodic(session_id=None, limit=50)
            hits: list[tuple[str, str]] = []
            seen_sessions: set[str] = set()
            for r in rows:
                if r.get("session_id") == session_id:
                    continue
                if float(r.get("timestamp", 0)) < cutoff:
                    continue
                summary = (r.get("summary") or "").strip()
                sid = r.get("session_id") or ""
                if not summary or sid in seen_sessions:
                    continue
                seen_sessions.add(sid)
                hits.append((summary[:80], sid))
                if len(hits) >= 3:
                    break
            return tuple(hits)
        except Exception:  # noqa: BLE001
            return ()

    # ─── T1 of auto-skill-evolution plan: SessionEndEvent emission ─

    async def _maybe_continue_goal(
        self, sid: str, last_assistant_text: str
    ) -> str | None:
        """Ralph-loop continuation gate (Kanban-Goals v2 wiring).

        Reads the active goal (if any), asks the auxiliary judge whether
        it's satisfied, persists the structured rationale on the goal
        row, and returns a continuation user-prompt to feed the next
        turn — or ``None`` to exit normally.

        The judge fails OPEN (treated as not-done) so a flaky aux model
        never wedges progress; ``goal.budget`` is the real backstop.

        Banner emission (continue / achieved / pause_budget) is delegated
        to :attr:`goal_banner_callback` if set by the host (CLI input
        loop); gateway path leaves it ``None`` until that wiring lands.

        Returns:
            Continuation prompt string when the loop should re-enter;
            ``None`` when the goal is unset, paused, satisfied, or
            budget-exhausted.
        """
        import dataclasses

        from opencomputer.agent.goal import (
            JudgeVerdict,
            build_continuation_prompt,
            judge_goal,
        )

        goal = self.db.get_session_goal(sid)
        if goal is None:
            return None
        if not goal.active:
            return None  # paused — neither banner nor continuation
        if goal.budget_exhausted():
            # Active goal but already at budget — fire pause banner
            # (idempotent; user sees it until they /goal resume or
            # /goal clear) and stop without judging or bumping further.
            stale = JudgeVerdict(
                done=False,
                reason=(
                    goal.last_judge_reason or "budget reached"
                ),
            )
            self._fire_goal_banner(
                sid, kind="pause_budget", verdict=stale, goal=goal,
            )
            return None
        try:
            verdict = await judge_goal(
                goal_text=goal.text,
                last_response=last_assistant_text or "",
            )
        except Exception:  # noqa: BLE001 — fail-open
            verdict = JudgeVerdict(
                done=False, reason="(judge raised inside loop)"
            )

        if verdict.done:
            self._fire_goal_banner(
                sid, kind="achieved", verdict=verdict, goal=goal,
            )
            self.db.clear_session_goal(sid)
            return None

        new_turns = goal.turns_used + 1
        self.db.update_session_goal(
            sid, turns_used=new_turns, last_judge_reason=verdict.reason,
        )
        next_goal = dataclasses.replace(
            goal, turns_used=new_turns, last_judge_reason=verdict.reason,
        )
        self._fire_goal_banner(
            sid, kind="continue", verdict=verdict, goal=next_goal,
        )
        return build_continuation_prompt(goal.text)

    def _fire_goal_banner(
        self,
        sid: str,
        *,
        kind: str,
        verdict: object,
        goal: object,
    ) -> None:
        """Best-effort goal-banner emission.

        Two registration surfaces:

        - ``self._goal_banner_callbacks: dict[sid → cb]`` (preferred) —
          per-session callbacks installed by the gateway around each
          ``run_conversation`` so banners reach the correct chat when
          one AgentLoop serves multiple sessions concurrently. Managed
          via :meth:`set_goal_banner_callback` / :meth:`clear_goal_banner_callback`.
        - ``self.goal_banner_callback`` (legacy / CLI) — single global
          callback. Set directly by the CLI input loop where one
          console serves the only session. Falls through when no
          per-sid entry exists.

        Banner errors are swallowed — UX must never wedge the loop.
        Callbacks should accept
        ``cb(*, session_id: str, kind: str, verdict: JudgeVerdict, goal: GoalState)``.
        """
        per_session = getattr(self, "_goal_banner_callbacks", None)
        cb = None
        if per_session is not None:
            cb = per_session.get(sid)
        if cb is None:
            cb = getattr(self, "goal_banner_callback", None)
        if cb is None:
            return
        try:
            cb(session_id=sid, kind=kind, verdict=verdict, goal=goal)
        except Exception:  # noqa: BLE001
            pass

    def set_goal_banner_callback(self, sid: str, cb) -> None:
        """Register a per-session goal-banner callback.

        Used by the gateway to route banners to the right chat when one
        AgentLoop serves multiple sessions concurrently. Pair with
        :meth:`clear_goal_banner_callback` in a try/finally.
        """
        if not hasattr(self, "_goal_banner_callbacks"):
            self._goal_banner_callbacks = {}
        self._goal_banner_callbacks[sid] = cb

    def clear_goal_banner_callback(self, sid: str) -> None:
        """Drop a per-session callback. Idempotent."""
        bag = getattr(self, "_goal_banner_callbacks", None)
        if bag is not None:
            bag.pop(sid, None)

    def _ensure_session_persisted(self, sid: str) -> None:
        """Lazy-write the session row on first persistence demand.

        Wave 5 T17 closure. ``run_conversation`` captures session metadata
        in :attr:`_pending_session_meta` instead of eagerly calling
        ``db.create_session``. The first ``_persist_message`` /
        ``_persist_messages_batch`` for a fresh sid triggers this gate,
        which calls ``db.ensure_session`` (idempotent INSERT OR IGNORE).
        Subsequent calls within the same loop are no-ops via
        :attr:`_session_ensured`.
        """
        if sid in self._session_ensured:
            return
        meta = self._pending_session_meta.get(sid, {})
        self.db.ensure_session(
            sid,
            platform=meta.get("platform", "cli"),
            model=meta.get("model", ""),
            cwd=meta.get("cwd"),
        )
        self._session_ensured.add(sid)

    def _persist_message(self, sid: str, msg: Message) -> int:
        """Append a single message — ensures the session row first."""
        self._ensure_session_persisted(sid)
        return self.db.append_message(sid, msg)

    def _persist_messages_batch(self, sid: str, msgs: list[Message]) -> list[int]:
        """Append a batch of messages — ensures the session row first."""
        self._ensure_session_persisted(sid)
        return self.db.append_messages_batch(sid, msgs)

    async def _emit_session_end_event(
        self,
        *,
        session_id: str,
        end_reason: str,
        turn_count: int,
        duration_seconds: float,
        had_errors: bool,
    ) -> None:
        """Publish a :class:`SessionEndEvent` on the typed bus.

        Wrapped in a broad try/except so a bus failure (subscribe-error,
        broken default_bus, mid-shutdown loop close) cannot break the
        loop's own return path. Best-effort; warnings logged.

        T1 of 2026-04-27 auto-skill-evolution plan.

        2026-04-28: also dispatches Mechanism C (session-end
        reflection) for the learning-moments registry. If a moment
        with ``Surface.SESSION_END`` matches, the reflection text is
        appended as a final assistant message on the session. Best-
        effort and gated on the same caps + dedup as the other
        surfaces.
        """
        try:
            from opencomputer.ingestion.bus import default_bus as _bus
            from plugin_sdk.ingestion import SessionEndEvent as _SessionEndEvent

            await _bus.apublish(
                _SessionEndEvent(
                    session_id=session_id,
                    source="agent_loop",
                    end_reason=end_reason,
                    turn_count=turn_count,
                    duration_seconds=duration_seconds,
                    had_errors=had_errors,
                )
            )
        except Exception:  # noqa: BLE001 — bus failure must not break the loop
            _log.warning(
                "bus: SessionEndEvent publish failed for session=%s — continuing",
                session_id,
                exc_info=True,
            )

        # ─── Mechanism C: session-end reflection ────────────────────
        # Only dispatch on clean ends (``completed``). Skip cancels,
        # errors, timeouts — emitting "that session felt stuck" after
        # a cancellation would be off-key.
        if end_reason != "completed":
            return
        try:
            from opencomputer.agent.config import _home as _profile_home_fn
            from opencomputer.awareness.learning_moments import (
                Context as _LMCtx,
            )
            from opencomputer.awareness.learning_moments import (
                select_session_end_reflection as _select_session_end,
            )
            from plugin_sdk.core import Message as _Msg

            _ph = _profile_home_fn()
            try:
                _vibe_rows = self.db.list_vibe_log_for_session(session_id)
            except AttributeError:
                _vibe_rows = []
            _stuck_or_frustrated = sum(
                1 for r in _vibe_rows
                if r.get("vibe") in ("stuck", "frustrated")
            )
            _fraction = (
                _stuck_or_frustrated / len(_vibe_rows)
                if _vibe_rows
                else 0.0
            )

            # v3 (2026-04-30) — session-end token total + has_openai for
            # mechanism-C moments (suggest_skill_save_after_long_session
            # uses turn_count only, but populating these makes the
            # Context complete for any future C-surface moments).
            import os as _os_mod_c
            try:
                _sess_row_c = self.db.get_session(session_id) or {}
                _tokens_c = (
                    int(_sess_row_c.get("input_tokens", 0) or 0)
                    + int(_sess_row_c.get("output_tokens", 0) or 0)
                )
            except Exception:  # noqa: BLE001
                _tokens_c = 0
            _has_openai_c = bool(_os_mod_c.environ.get("OPENAI_API_KEY"))

            def _build_session_end_ctx(
                _ph_=_ph,
                _sid_=session_id,
                _fraction_=_fraction,
                _turns_=turn_count,
                _tokens_=_tokens_c,
                _has_openai_=_has_openai_c,
            ) -> _LMCtx:
                return _LMCtx(
                    session_id=_sid_,
                    profile_home=_ph_,
                    user_message="",
                    memory_md_text="",
                    vibe_log_session_count_total=len(_vibe_rows),
                    vibe_log_session_count_noncalm=0,
                    sessions_db_total_sessions=self.db.count_sessions(),
                    vibe_stuck_or_frustrated_fraction=_fraction_,
                    turn_count=_turns_,
                    session_token_total=_tokens_,
                    has_openai_key=_has_openai_,
                )

            reflection = _select_session_end(
                ctx_builder=_build_session_end_ctx, profile_home=_ph,
            )
            if reflection:
                final = _Msg(role="assistant", content=reflection)
                self._persist_message(session_id, final)
        except Exception:  # noqa: BLE001 — reflections are non-load-bearing
            _log.debug(
                "learning_moments: session-end reflection failed for %s",
                session_id,
                exc_info=True,
            )

    # ─── Hermes-parity Tier S (2026-04-30): /compress entry point ──

    def request_force_compaction(self) -> None:
        """Request that the next iteration force-compact the conversation.

        Called by the ``/compress`` slash command. The flag is consumed
        once at the start of the next ``run_conversation`` iteration,
        bypassing the input-token threshold so compaction runs even
        before context is "full".
        """
        self._force_compact_next_turn = True

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

        Item 3 (2026-05-02): each schema is augmented with the originating
        tool's ``strict_mode`` so the provider-format conversion can emit
        ``strict: true`` to Anthropic. ToolSchema is frozen+slots; we use
        ``dataclasses.replace`` to set the field. Calls ``registry.schemas()``
        (preserves existing tests that mock that method) and then resolves
        each schema's tool via ``registry.get(name)`` to read strict_mode.
        """
        from dataclasses import replace as _dc_replace

        from plugin_sdk.tool_contract import ToolSchema as _ToolSchema

        def _maybe_strict(schema: _ToolSchema) -> _ToolSchema:
            tool = registry.get(schema.name)
            strict = bool(getattr(tool, "strict_mode", False)) if tool else False
            if strict and not schema.strict:
                return _dc_replace(schema, strict=True)
            return schema

        all_schemas = [_maybe_strict(s) for s in registry.schemas()]
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
        base_system: str = "",
        injected_system: str = "",
        stream_callback=None,
        thinking_callback=None,
        model: str | None = None,
        session_id: str = "",
        max_tokens_override: int | None = None,
    ) -> StepOutcome:
        """One LLM call + classification of the result.

        If `stream_callback` is provided, stream_complete is used and each
        text chunk is passed to the callback synchronously.

        ``model`` overrides ``config.model.model`` for this turn only —
        used by the cheap-route gate on iteration 0. ``None`` = use the
        config default.

        ``max_tokens_override`` overrides ``config.model.max_tokens`` for
        this call only — used by the max_tokens+tool_use retry path
        (2026-05-02) to lift the ceiling without mutating the frozen
        ``ModelConfig`` dataclass.

        Resolves any user-defined alias (``config.model.model_aliases``)
        to its canonical id before the provider call so users can write
        ``model: fast`` in config and have it map to the configured target.
        """
        from opencomputer.agent.model_resolver import resolve_model

        raw_model = model if model is not None else self.config.model.model

        # Phase 3 (2026-05-06 — S3 leftover from OpenClaw deep-comparison) —
        # BEFORE_MODEL_RESOLVE fire-and-forget hook. Handlers see the raw
        # alias text (pre-resolve) and may redirect the resolution by
        # returning ``HookDecision(decision="rewrite", modified_message="<new-alias>")``.
        # Distinct from PRE_LLM_CALL which fires post-resolve.
        try:
            from opencomputer.hooks.engine import engine as _hook_engine_bmr
            from plugin_sdk.hooks import HookContext as _BmrCtx
            from plugin_sdk.hooks import HookEvent as _BmrEvent

            decision = await _hook_engine_bmr.fire_blocking(
                _BmrCtx(
                    event=_BmrEvent.BEFORE_MODEL_RESOLVE,
                    session_id=self.session_id or "",
                    pre_resolve_model=raw_model,
                    model=raw_model,
                )
            )
            if (
                decision is not None
                and getattr(decision, "decision", "pass") == "rewrite"
                and getattr(decision, "modified_message", None)
            ):
                raw_model = decision.modified_message
        except Exception as _e:  # noqa: BLE001 — hook failure must never wedge resolve
            _log.debug("BEFORE_MODEL_RESOLVE hook raised, ignoring: %r", _e)

        model_name = resolve_model(
            raw_model, getattr(self.config.model, "model_aliases", None) or {}
        )
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

        _pre_llm_ctx = _HookContext(
            event=_HookEvent.PRE_LLM_CALL,
            session_id=session_id,
            runtime=self._runtime,
            messages=list(wire_messages),
            model=model_name,
        )

        # 2026-05-08 G4 — Hermes Doc-2 shell-hook context injection.
        # Run blocking-eligible PRE_LLM_CALL handlers (settings/shell hooks
        # registered with fire_and_forget=False) and collect any
        # ``inject_context`` strings they returned. Plugin handlers (default
        # fire_and_forget=True) keep flowing through fire_and_forget below;
        # their existing semantics are preserved.
        try:
            _injected_contexts = await _hook_engine.collect_inject_contexts(
                _pre_llm_ctx
            )
        except Exception:  # noqa: BLE001 — fail-open: never wedge the loop
            _injected_contexts = []
        if _injected_contexts:
            wire_messages = apply_inject_contexts(wire_messages, _injected_contexts)
            # Update the ctx we'll pass to fire_and_forget so plugin
            # observers see the post-injection message list.
            _pre_llm_ctx = _HookContext(
                event=_HookEvent.PRE_LLM_CALL,
                session_id=session_id,
                runtime=self._runtime,
                messages=list(wire_messages),
                model=model_name,
            )

        _hook_engine.fire_and_forget(_pre_llm_ctx)

        # Tier 2.A — /reasoning + /fast slash commands wrote flags to
        # runtime.custom; translate to provider kwargs. Only pass
        # ``runtime_extras=`` when non-empty so stub providers in tests
        # (and 3rd-party plugins) that don't accept the kwarg still work.
        from opencomputer.agent.runtime_flags import runtime_flags_from_custom
        _runtime_extras = runtime_flags_from_custom(self._runtime.custom)
        # Subsystem B (2026-05-02) — apply per-context effort policy
        # when the user hasn't set ``reasoning_effort`` via ``/reasoning``.
        # Subagents → low, voice mode → low, Sonnet 4.6 → medium,
        # Opus 4.7 → xhigh. User-set values always win (we only fill in
        # when None). Provider-agnostic: works for any provider whose
        # ``*_kwargs_from_runtime`` accepts ``reasoning_effort``.
        if _runtime_extras.get("reasoning_effort") is None:
            from opencomputer.agent.effort_policy import recommended_effort
            _policy_default = recommended_effort(
                runtime=self._runtime,
                model=model_name,
            )
            if _policy_default is not None:
                _runtime_extras["reasoning_effort"] = _policy_default
        # Only pass ``runtime_extras=`` when at least one flag is non-None
        # so stub providers in tests (and 3rd-party plugins that haven't
        # adopted the kwarg) keep working.
        _has_extras = any(v is not None for v in _runtime_extras.values())
        _extra_kwargs: dict[str, Any] = (
            {"runtime_extras": _runtime_extras} if _has_extras else {}
        )
        if stream_callback is not None:
            final_response = None
            # Only pass split-system kwargs to providers that accept them.
            # Stub providers in tests + 3rd-party plugins that haven't
            # adopted the new signature would otherwise raise TypeError.
            _split_kwargs = _maybe_split_system_kwargs(
                self.provider.stream_complete,
                base_system=base_system,
                injected_system=injected_system,
                session_id=session_id,
            )
            stream_source = self.provider.stream_complete(
                model=model_name,
                messages=wire_messages,
                system=system,
                tools=tool_schemas,
                max_tokens=max_tokens_override or self.config.model.max_tokens,
                temperature=self.config.model.temperature,
                **_split_kwargs,
                **_extra_kwargs,
            )
            # Phase B (model-agnostic thinking): when the provider does
            # NOT have native thinking for this model AND the user has
            # effort > "none", wrap the stream so <think>...</think>
            # tags emitted by the model in plain text deltas are
            # transparently extracted and re-emitted as thinking_delta
            # events. The matching system-prompt instruction is added
            # by ThinkingInjector. Native-thinking providers skip this
            # wrap entirely and use their existing thinking_delta path.
            #
            # Read flags directly from self._runtime.custom — they're
            # internal plumbing, not something the provider needs to
            # know about (so they're not in runtime_flags_from_custom's
            # allowlist).
            _eff = str(_runtime_extras.get("reasoning_effort") or "medium").lower()
            _native = bool(
                self._runtime.custom.get(
                    "_provider_supports_native_thinking", False
                )
            )
            if _eff != "none" and not _native:
                from opencomputer.agent.thinking_parser import ThinkingTagsParser
                stream_source = ThinkingTagsParser().wrap(stream_source)
            # Wave 3 (2026-05-08) — wrap with the streaming-stall watchdog
            # when the provider opts in via stale_timeout_seconds. Catches
            # LLM-side hangs on alive HTTP connections (common on local
            # model servers under memory pressure). The wrap is a no-op
            # pass-through when stale_timeout_seconds is None.
            _stale_timeout = getattr(self.provider, "stale_timeout_seconds", None)
            # Strict numeric check — test stubs that return MagicMock for
            # arbitrary attribute access would otherwise tank here when
            # asyncio.wait_for tries to compare MagicMock to int.
            if isinstance(_stale_timeout, (int, float)) and _stale_timeout > 0:
                from opencomputer.agent.stream_watchdog import stream_with_watchdog
                stream_source = stream_with_watchdog(
                    stream_source,
                    stale_timeout_seconds=float(_stale_timeout),
                    provider_name=getattr(self.provider, "name", "?"),
                )
            # Phase 5 (2026-05-07) — partial-message recovery wiring (A4).
            # Accumulate the streamed text as it arrives so we can attempt
            # recovery if the stream is interrupted (network drop, gateway
            # restart, upstream timeout) before a 'done' event arrives.
            _partial_buffer: list[str] = []
            try:
                async for event in stream_source:
                    if event.kind == "text_delta":
                        _partial_buffer.append(event.text)
                        stream_callback(event.text)
                    elif event.kind == "thinking_delta":
                        if thinking_callback is not None:
                            thinking_callback(event.text)
                    elif event.kind == "done":
                        final_response = event.response
            except (asyncio.CancelledError, GeneratorExit):
                # Stream was cancelled — propagate without recovery.
                raise
            except Exception as _stream_exc:  # noqa: BLE001 — recovery-only path
                if final_response is None and _partial_buffer:
                    from opencomputer.gateway.replay_sanitizer import (
                        recover_partial_assistant,
                    )

                    partial = "".join(_partial_buffer)
                    _result = recover_partial_assistant(partial)
                    _log.warning(
                        "stream interrupted (%s); partial-recovery=%s reason=%s",
                        type(_stream_exc).__name__,
                        _result.status,
                        _result.reason,
                    )
                    # When recoverable AND nothing has been emitted yet that
                    # would conflict with re-emitting the trimmed text,
                    # the loop's caller (e.g. gateway dispatch) gets the
                    # exception and decides whether to surface the partial.
                    # We attach the recovery result to the exception for
                    # callers who care; raise the original exception to
                    # preserve existing semantics.
                    _stream_exc.partial_recovery = _result  # type: ignore[attr-defined]
                raise
            if final_response is None:
                raise RuntimeError("stream ended without a 'done' event")
            resp = final_response
        else:
            # G.31 — wrap the provider call in the fallback router so
            # transient failures (429 / 5xx / connection refused) walk
            # the configured ``fallback_models`` chain before raising.
            #
            # Wave 3 (2026-05-08) — extended with cross-provider chain.
            # When ``Config.fallback_providers`` is populated, after the
            # primary's ``fallback_models`` exhaust we try each
            # provider+model pair in turn (per-turn scoped — primary
            # restored on the next user turn).
            from opencomputer.agent.fallback import (
                call_with_fallback,
                call_with_provider_fallback,
            )

            _split_kwargs = _maybe_split_system_kwargs(
                self.provider.complete,
                base_system=base_system,
                injected_system=injected_system,
                session_id=session_id,
            )
            async def _do_call(active_model: str):
                return await self.provider.complete(
                    model=active_model,
                    messages=wire_messages,
                    system=system,
                    tools=tool_schemas,
                    max_tokens=self.config.model.max_tokens,
                    temperature=self.config.model.temperature,
                    **_split_kwargs,
                    **_extra_kwargs,
                )

            cross_chain = getattr(self.config, "fallback_providers", ())
            if cross_chain:
                from opencomputer.agent.fallback_provider_resolver import (
                    build_fallback_provider_chain,
                )

                provider_chain = build_fallback_provider_chain(
                    cross_chain,
                    self.config,
                )

                async def _cross_call(prov, active_model: str):
                    sub_split = _maybe_split_system_kwargs(
                        prov.complete,
                        base_system=base_system,
                        injected_system=injected_system,
                        session_id=session_id,
                    )
                    return await prov.complete(
                        model=active_model,
                        messages=wire_messages,
                        system=system,
                        tools=tool_schemas,
                        max_tokens=self.config.model.max_tokens,
                        temperature=self.config.model.temperature,
                        **sub_split,
                        **_extra_kwargs,
                    )

                resp = await call_with_provider_fallback(
                    _do_call,
                    _cross_call,
                    primary_model=model_name,
                    fallback_models=self.config.model.fallback_models,
                    provider_chain=provider_chain,
                )
            else:
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
            # Server-tool work paused; loop re-sends.
            "pause_turn": StopReason.PAUSE_TURN,
            # Model refused; surface as final, no retry.
            "refusal": StopReason.REFUSAL,
            # Context window exceeded — Subsystem A retry-with-compaction.
            "model_context_window_exceeded": StopReason.CONTEXT_FULL,
        }
        stop = stop_reason_map.get(resp.stop_reason, StopReason.END_TURN)

        # Refusal: ensure the user sees something, even if the model
        # emitted no text. Anthropic returns stop_reason=refusal when its
        # safety filter declines a request — sometimes with a brief
        # explanation, sometimes empty. Today we silently map to END_TURN,
        # leaving the user staring at an empty assistant turn.
        if stop == StopReason.REFUSAL:
            existing = (resp.message.content or "").strip()
            new_content = (
                f"_Claude declined to respond._\n\n{existing}"
                if existing
                else "_Claude declined to respond._"
            )
            resp = ProviderResponse(
                message=Message(
                    role=resp.message.role,
                    content=new_content,
                    tool_calls=resp.message.tool_calls,
                ),
                stop_reason=resp.stop_reason,
                usage=resp.usage,
            )

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

        # Hermes B4: per-call cost recording. Best-effort — telemetry must
        # not wedge the loop, so swallow any exception. Idempotency is
        # guaranteed by placement: we land here only on successful provider
        # response; retries raise before this point.
        try:
            from opencomputer.agent.usage_pricing import record_call_from_usage

            provider_name = getattr(self.provider, "name", "") or type(
                self.provider
            ).__name__.lower().replace("provider", "")
            record_call_from_usage(
                db=self.db,
                session_id=session_id or "",
                provider=provider_name,
                model=model_name,
                usage=resp.usage,
                batch=False,
            )
        except Exception:  # noqa: BLE001
            _log.debug("usage_pricing.record_call_from_usage swallowed", exc_info=True)

        return StepOutcome(
            stop_reason=stop,
            assistant_message=msg,
            tool_calls_made=len(msg.tool_calls or []),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_tokens=resp.usage.cache_read_tokens,
            cache_write_tokens=resp.usage.cache_write_tokens,
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

        # v1.1 plan-2 M4.4 hard enforcement (2026-05-09): when an inline
        # SkillTool has set an active tool filter, block any call whose
        # tool name isn't in the skill's allowlist. The Skill itself
        # is implicitly allowed (so the skill body's request to read
        # other tools doesn't recursively self-block).
        try:
            from opencomputer.agent.skill_tools_filter import (
                get_active_filter,
                is_tool_allowed,
            )

            _skill_filter = get_active_filter()
        except Exception:  # noqa: BLE001
            _skill_filter = None
        if _skill_filter is not None:
            for c in calls:
                if c.name == "Skill":
                    # Always allow re-invoking the Skill tool (lets the
                    # model swap to a different skill if needed).
                    continue
                allowed, reason = is_tool_allowed(c.name)
                if not allowed and reason is not None:
                    blocked.setdefault(c.id, reason)

        # v1.1 plan-3 M9.2 (2026-05-09) — auto-mode tool-call classifier.
        # When the session is in permission_mode=auto, every pending tool
        # call passes through ToolCallClassifier.classify BEFORE the
        # F1 consent gate fires. The classifier sees only:
        #   - the user's verbatim messages
        #   - the assistant's pre-tool-call free-form text
        #   - the tool_use requests already made (NOT their results)
        #   - the pending call's name + summarized args
        # tool_result content is structurally invisible — see
        # opencomputer/agent/tool_call_classifier.py for the security
        # contract (poison-resistance assertion in
        # _build_classifier_input).
        #
        # Composition with consent gate (M9.5):
        #   - Decision.BLOCK → tool blocked outright (this dispatch loop
        #     surfaces the block reason; consent gate doesn't run).
        #   - Decision.ALLOW → continue to consent gate (consent gate
        #     can still deny — auto mode does not skip the gate).
        #   - Decision.ASK → continue to consent gate. Per-call Tier-2
        #     prompts already exist for tools with capability_claims;
        #     this path treats the classifier's "I'm uncertain" the
        #     same way as a Tier-2 claim that needs user confirmation.
        #
        # Fail-closed: any classifier error returns BLOCK with
        # failed_closed=True. A wedged auxiliary provider must never
        # silently fall through to ALLOW.
        try:
            from plugin_sdk import effective_permission_mode as _epm_m92

            _mode = _epm_m92(self._runtime)
            _is_auto = str(_mode).endswith("auto")
        except Exception:  # noqa: BLE001
            _is_auto = False

        if _is_auto and calls:
            try:
                from opencomputer.agent.tool_call_classifier import (
                    Decision as _M92Decision,
                )
                from opencomputer.agent.tool_call_classifier import (
                    ToolCallClassifier as _M92Classifier,
                )

                # Build the prior tool_use list from the persisted history.
                _prior_calls: list[ToolCall] = []
                _user_msgs: list[Message] = []
                try:
                    _hist = list(self.db.get_messages(session_id)) if session_id else []
                    for _m in _hist:
                        if _m.role in ("user", "assistant", "system"):
                            _user_msgs.append(_m)
                        if _m.tool_calls:
                            _prior_calls.extend(_m.tool_calls)
                except Exception:  # noqa: BLE001
                    pass

                _classifier = _M92Classifier()
                # M9.3: per-session block budget — pause auto mode after
                # 3 consecutive blocks or 20 total. M9.4: every classifier
                # decision lands in the existing F1 HMAC-chained audit log
                # via audit_classifier_decision (no-op when no logger
                # available — defensive).
                from opencomputer.agent.tool_call_classifier import (
                    audit_classifier_decision as _m94_audit,
                )
                from opencomputer.agent.tool_call_classifier import (
                    is_paused as _m93_is_paused,
                )
                from opencomputer.agent.tool_call_classifier import (
                    record_classifier_decision as _m93_record,
                )

                # M9.3: if budget already tripped on a prior turn, every
                # classifier decision is treated as ASK (forces consent
                # gate to handle). User must `oc resume` to clear the
                # budget before auto mode resumes.
                _budget_paused = _m93_is_paused(session_id) if session_id else False

                for _c in calls:
                    # Skip if already blocked by skill_filter or future
                    # checks above (don't waste classifier calls).
                    if _c.id in blocked:
                        continue
                    _decision = await _classifier.classify(
                        user_messages=_user_msgs,
                        tool_calls_so_far=_prior_calls,
                        pending=_c,
                    )
                    # M9.4: record EVERY decision (allow + block + ask)
                    # into the audit chain. Best-effort — auditing
                    # failure must not break dispatch.
                    if session_id:
                        try:
                            _audit_logger = getattr(
                                self._consent_gate, "_audit_logger", None
                            )
                            _m94_audit(
                                _audit_logger, session_id, _c, _decision,
                            )
                        except Exception:  # noqa: BLE001
                            pass

                    if _decision.decision == _M92Decision.BLOCK:
                        _block_msg = (
                            f"Auto-mode classifier blocked this call: "
                            f"{_decision.rationale}"
                        )
                        if _decision.failed_closed:
                            _block_msg += " (fail-closed)"
                        blocked.setdefault(_c.id, _block_msg)

                    # M9.3: update per-session budget. Returns True iff
                    # this decision tripped the budget threshold (3
                    # consecutive or 20 total blocks).
                    if session_id:
                        _budget_tripped = _m93_record(session_id, _decision)
                        if _budget_tripped:
                            _log.warning(
                                "M9.3 block budget tripped for session %s — "
                                "pausing auto mode (run `oc resume` to "
                                "clear). Subsequent calls require explicit "
                                "PER_ACTION approval until then.",
                                session_id,
                            )
                            # Mutate the runtime so the next consent gate
                            # check sees default mode (PER_ACTION
                            # prompts). Preserves the user's original
                            # intent — they DID toggle auto on; we're
                            # temporarily downgrading until they confirm
                            # via `oc resume`.
                            try:
                                self._runtime.custom["permission_mode"] = "default"
                                self._runtime.custom.pop("yolo_session", None)
                                self._runtime.custom["m9_3_paused_session"] = session_id
                            except Exception:  # noqa: BLE001
                                pass
                            _budget_paused = True
                    # ALLOW + ASK both fall through to the consent gate
                    # below. ASK semantics map onto consent gate's
                    # PER_ACTION prompt for any tool with
                    # capability_claims; tools without claims have no
                    # interactive surface and effectively allow.

                # M9.3: if the budget was ALREADY paused entering this
                # dispatch, force every call into the consent gate
                # PER_ACTION path by treating un-blocked calls as ASK
                # (which the gate already handles). Implementation:
                # leave runtime in default mode (already done by the
                # block-trip path above). If this dispatch never tripped
                # but the session is already paused from a prior turn,
                # do the same flip here so the consent gate sees
                # default mode.
                if _budget_paused and session_id:
                    try:
                        self._runtime.custom["permission_mode"] = "default"
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as e:  # noqa: BLE001
                _log.warning(
                    "M9.2 classifier path raised — falling through to "
                    "consent gate without classification: %s", e,
                )

        if self._consent_gate is not None:
            from opencomputer.agent.consent.bypass import BypassManager
            from plugin_sdk.consent import ConsentTier
            if not BypassManager.is_active(self._runtime):
                # Hermes parity: pre-consent Tirith scan. For tools that
                # ship a "command" or "code" argument, we scan it BEFORE
                # the consent gate fires so any findings can surface in
                # the user prompt. block verdicts skip the consent gate
                # entirely; warn verdicts pass findings through to the
                # prompt; allow verdicts are no-ops.
                tirith_per_call: dict[str, tuple[str, str]] = {}
                # call.id -> (action, findings_text)
                _TIRITH_TARGETS: dict[str, str] = {
                    "ExecuteCode": "code",
                    "Bash": "command",
                }
                for c in calls:
                    arg_key = _TIRITH_TARGETS.get(c.name)
                    if arg_key is None:
                        continue
                    try:
                        target = (c.arguments or {}).get(arg_key) or ""
                    except (AttributeError, TypeError):
                        target = ""
                    if not isinstance(target, str) or not target.strip():
                        continue
                    try:
                        from opencomputer.security.tirith import (
                            check_command as _tirith_check,
                        )
                        from opencomputer.security.tirith import (
                            format_findings_for_user as _format_findings,
                        )
                        verdict = await asyncio.to_thread(_tirith_check, target)
                    except Exception:  # noqa: BLE001 — never let scan break dispatch
                        continue
                    if verdict.action in ("warn", "block"):
                        text = _format_findings(verdict) or verdict.summary or ""
                        if text:
                            tirith_per_call[c.id] = (verdict.action, text)

                for c in calls:
                    tool = registry.get(c.name)
                    if tool is None:
                        continue
                    claims = getattr(tool, "capability_claims", ())
                    # Pre-emptive Tirith block — refuse before consent
                    # gate fires. Mirrors BashTool's in-tool block path
                    # but at the dispatch layer so even tools without
                    # claims get the same protection.
                    if c.id in tirith_per_call and tirith_per_call[c.id][0] == "block":
                        blocked[c.id] = (
                            "Tirith pre-exec scan blocked: "
                            + tirith_per_call[c.id][1]
                        )
                        continue
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
                            # Hermes parity: thread Tirith warn-findings
                            # into the prompt so the user decides with
                            # full security context.
                            findings_text = None
                            if c.id in tirith_per_call:
                                findings_text = tirith_per_call[c.id][1]
                            try:
                                approval = await self._consent_gate.request_approval(
                                    claim=claim,
                                    scope=scope,
                                    session_id=session_id,
                                    tirith_findings_text=findings_text,
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

        # PR-A Feature 3: ACP per-session denylist gate. Fires AFTER
        # consent-gate (which is non-bypassable security) and BEFORE
        # PreToolUse hooks (which are application policy). A tool that
        # is denied by the IDE-side ``setSessionPermissions`` is short-
        # circuited just like a consent-denied tool — the model sees the
        # denied marker as a tool result and can replan without invoking
        # the tool again. Race-safe: this check runs once per dispatch
        # entry; any update to ``acp_denied_tools`` affects only the
        # next dispatch.
        _acp_denied = getattr(self._runtime, "acp_denied_tools", frozenset())
        if _acp_denied:
            for c in calls:
                if c.id in blocked:
                    continue
                if c.name in _acp_denied:
                    blocked[c.id] = (
                        f"ACP denylist: tool '{c.name}' is denied for "
                        "this session"
                    )

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

        # Wave-5 T1 — Hermes-port tool-loop guard. Observe each call after
        # consent + PreToolUse hooks have spoken (so a tight loop the user
        # is about to deny doesn't trip the guard) but before dispatch.
        # On stop: mark every remaining call blocked with the guard's
        # reason — the loop converts blocked tool_uses to error
        # tool_results so the protocol invariant (every tool_use has a
        # matching tool_result) survives. On warn: log the message
        # (kept off the wire so the model isn't influenced mid-turn).
        for c in calls:
            if c.id in blocked:
                continue
            try:
                verdict = self._tool_guard.observe(
                    {"name": c.name, "arguments": c.arguments or {}},
                )
            except ToolLoopGuardrailError as exc:
                _log.warning("tool guardrail stop: %s", exc)
                # Block this call AND every subsequent unblocked call —
                # we want one clean turn-end, not a cascade of stops.
                _stop_reason = str(exc)
                for _later in calls:
                    if _later.id not in blocked:
                        blocked[_later.id] = f"tool guardrail: {_stop_reason}"
                break
            if verdict.level == "warn":
                _log.warning("%s", verdict.message)

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
            if self._tool_callback is not None:
                try:
                    self._tool_callback("start", c.name, c.id or "", c.arguments)
                except Exception:
                    pass
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
                # Wave 5 T15 — Hermes-port duration_ms (59b56d445).
                # Capture the dispatch latency once so both POST_TOOL_USE
                # and TRANSFORM_TOOL_RESULT see the same value.
                _duration_ms = max(
                    0, int((_time.monotonic() - start) * 1000),
                )
                # Wave 5 T15 — fire POST_TOOL_USE with duration_ms so
                # plugins can build per-tool latency dashboards.
                # Wrapped defensively — a hook crash must never replace
                # the tool result the model is about to see.
                try:
                    from opencomputer.hooks.engine import (
                        engine as _post_hook_engine,
                    )
                    from plugin_sdk.hooks import HookContext as _HookContextPost
                    from plugin_sdk.hooks import HookEvent as _HookEventPost

                    _post_hook_engine.fire_and_forget(_HookContextPost(
                        event=_HookEventPost.POST_TOOL_USE,
                        session_id=session_id,
                        tool_call=c,
                        tool_result=result,
                        runtime=self._runtime,
                        duration_ms=_duration_ms,
                    ))
                except Exception:  # noqa: BLE001
                    pass
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
                    duration_ms=_duration_ms,
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
                if self._tool_callback is not None:
                    try:
                        self._tool_callback(
                            "complete", c.name, c.id or "",
                            getattr(result, "content", str(result)),
                        )
                    except Exception:
                        pass
                return result

        # PR-A Feature 1: cancel-aware dispatch. The SteerRegistry's
        # per-session cancel event lets /steer interrupt async-yielding
        # tools mid-flight; the agent loop emits <INTERRUPTED-BY-STEER>
        # placeholders in their slots so the model sees the interruption
        # and the next-iteration consume injects the steer text as a
        # <USER-INTERRUPT> nudge.
        from opencomputer.agent.steer import (
            default_registry as _steer_registry_for_dispatch,
        )

        _cancel_event = (
            _steer_registry_for_dispatch.cancel_event(session_id)
            if session_id
            else None
        )
        # Stale-event clear: if a previous turn ended without consuming,
        # the event may still be set. Clear before this dispatch so we
        # only react to mid-dispatch fires.
        if _cancel_event is not None and _cancel_event.is_set():
            _cancel_event.clear()

        if self.config.loop.parallel_tools and self._all_parallel_safe(calls):
            _tasks = [asyncio.create_task(_run_one(c)) for c in calls]
            _watchers: list[asyncio.Task] = list(_tasks)
            _cancel_watcher: asyncio.Task | None = None
            if _cancel_event is not None:
                _cancel_watcher = asyncio.create_task(_cancel_event.wait())
                _watchers.append(_cancel_watcher)

            await asyncio.wait(
                _watchers,
                return_when=(
                    asyncio.ALL_COMPLETED
                    if _cancel_event is None
                    else asyncio.FIRST_COMPLETED
                ),
            )

            if _cancel_event is not None and _cancel_event.is_set():
                # Steer fired mid-dispatch — cancel pending tools cooperatively.
                _pending_count = sum(1 for t in _tasks if not t.done())
                _log.info(
                    "steer cancel fired mid-dispatch: cancelling "
                    "%d pending tool(s)",
                    _pending_count,
                )
                for _t in _tasks:
                    if not _t.done():
                        _t.cancel()
                # Brief wait so cooperative cancel can produce partial
                # output (Bash captured stdout, etc.).
                try:
                    await asyncio.wait(_tasks, timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
                results = []
                for _c, _t in zip(calls, _tasks, strict=True):
                    if _t.done() and not _t.cancelled():
                        try:
                            results.append(_t.result())
                        except Exception:  # noqa: BLE001
                            # Pull any stashed partial-output off the task
                            # (Bash sets _pr_a_partial_stdout on its
                            # CancelledError handler).
                            partial = getattr(_t, "_pr_a_partial_stdout", "")
                            results.append(
                                _make_cancelled_result(_c, partial_stdout=partial)
                            )
                    else:
                        partial = getattr(_t, "_pr_a_partial_stdout", "")
                        results.append(
                            _make_cancelled_result(_c, partial_stdout=partial)
                        )
                # NOTE: don't reset_cancel here — between-turn consume
                # peeks the flag to decide <USER-INTERRUPT> vs <USER-NUDGE>.
            else:
                # Normal completion — cancel the watcher, await any tasks
                # still pending (FIRST_COMPLETED may have left some).
                if _cancel_watcher is not None and not _cancel_watcher.done():
                    _cancel_watcher.cancel()
                _still_pending = [t for t in _tasks if not t.done()]
                if _still_pending:
                    await asyncio.gather(*_still_pending, return_exceptions=False)
                results = [t.result() for t in _tasks]
        else:
            # Serial path: check cancel event between calls.
            results = []
            for _c in calls:
                if _cancel_event is not None and _cancel_event.is_set():
                    results.append(_make_cancelled_result(_c))
                    continue
                results.append(await _run_one(_c))

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
                if isinstance(cmd, str) and detect_destructive_with_context(cmd) is not None:
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
            # Promoted from DEBUG (2026-05-10): plugin-demand tracking
            # silently degrades to empty when search paths can't resolve;
            # user's `oc plugin demand` shows 0 rows for plugins they're
            # actually demanding.
            _log.warning(
                "demand_tracker: search-path resolution failed "
                "(plugin demand tracking degraded; `oc plugin demand` "
                "may show fewer signals than reality)",
                exc_info=True,
            )
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
            # Promoted from DEBUG (2026-05-10): same observability gap
            # as search-path failure above — silently disables the
            # active-plugin filter, so demand tracking conflates
            # already-enabled plugins with truly-missing ones.
            _log.warning(
                "demand_tracker: profile-config read failed "
                "(plugin-demand active-plugin filter disabled; "
                "`oc plugin demand` may flag plugins that are already enabled)",
                exc_info=True,
            )
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

    def _maybe_apply_exit_plan_proposal(self) -> None:
        """v1.1 plan-2 M5.4 follow-up — consume the ExitPlanMode proposal slot.

        Called after every loop iteration that dispatched an
        ``ExitPlanMode`` tool call. Reads the process-wide proposal slot
        set by the tool's ``execute()`` (when the agent passed
        ``next_mode``), and mutates ``self._runtime`` to switch the
        permission_mode for the rest of this session.

        - ``next_mode == "keep"`` → leave runtime alone (agent stays in
          plan mode and continues iterating).
        - ``next_mode in {"auto","acceptEdits","manual"}`` → rebuild
          runtime with that mode and clear plan_mode.
        - No proposal → no-op (the agent called ExitPlanMode without
          a next_mode suggestion).

        Slot reads from :mod:`opencomputer.agent.exit_plan_proposal`
        (a core module so the tool's writer and this reader share
        identity even when the tool's file is loaded under a
        synthetic plugin-loader name).
        """
        from opencomputer.agent.exit_plan_proposal import pop_last_proposal

        try:
            proposal = pop_last_proposal()
        except Exception:  # noqa: BLE001
            _log.debug("M5.4: pop_last_proposal failed", exc_info=True)
            return

        if proposal is None:
            return
        if proposal.next_mode == "keep":
            _log.info(
                "M5.4: ExitPlanMode proposal next_mode=keep — staying in plan mode"
            )
            return
        if proposal.next_mode not in ("auto", "acceptEdits", "manual"):
            _log.warning(
                "M5.4: ExitPlanMode proposal had unknown next_mode=%r; ignoring",
                proposal.next_mode,
            )
            return

        try:
            self._runtime = replace(
                self._runtime,
                plan_mode=False,
                permission_mode=proposal.next_mode,
            )
            _log.info(
                "M5.4: applied ExitPlanMode proposal — permission_mode now %r",
                proposal.next_mode,
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "M5.4: failed to mutate runtime for next_mode=%r",
                proposal.next_mode,
                exc_info=True,
            )


def _msg_to_dict(msg: Any) -> dict[str, Any]:
    """v1.1 plan-2 M5.2 (2026-05-09) — minimal Message-to-dict for checkpoints.

    Strips fields that aren't JSON-serialisable (large bytes, custom
    objects). Captures role + content + tool_calls + tool_call_id +
    name so a restored session can recreate a working message list.
    """
    out: dict[str, Any] = {
        "role": getattr(msg, "role", "user"),
        "content": getattr(msg, "content", ""),
    }
    if getattr(msg, "tool_calls", None):
        try:
            out["tool_calls"] = [
                {
                    "id": getattr(tc, "id", ""),
                    "name": getattr(tc, "name", ""),
                    "arguments": getattr(tc, "arguments", {}) or {},
                }
                for tc in msg.tool_calls
            ]
        except Exception:  # noqa: BLE001
            out["tool_calls"] = []
    if getattr(msg, "tool_call_id", None):
        out["tool_call_id"] = msg.tool_call_id
    if getattr(msg, "name", None):
        out["name"] = msg.name
    return out


async def _maybe_transform_tool_result(
    *,
    result: Any,
    call: ToolCall,
    session_id: str,
    runtime: RuntimeContext,
    duration_ms: int | None = None,
) -> Any:
    """Round 2A P-1: invoke TRANSFORM_TOOL_RESULT and apply ``modified_message``.

    Returns either the original ``result`` or a new
    :class:`~plugin_sdk.core.ToolResult` whose ``content`` is the handler's
    rewrite. Failures are isolated — any exception in a handler leaves the
    original result untouched (the engine logs it).

    Wave 5 T15 — ``duration_ms`` is the tool's dispatch latency, forwarded
    to the hook context so plugins can build latency dashboards without
    instrumenting every tool individually.
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
        duration_ms=duration_ms,
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
