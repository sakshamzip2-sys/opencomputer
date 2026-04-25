"""
MemoryProvider — external memory plugins (Honcho, Mem0, Cognee, etc.).

A memory provider plugs in alongside the built-in MEMORY.md + USER.md +
SQLite FTS5 baseline to provide deeper user understanding, semantic
search, or graph-based recall. Only ONE external provider can be active
at a time; the baseline is always on.

Providers own their own cadence (via ``turn_index``): core passes the
current turn index, provider returns ``None`` when not its turn. This
keeps cadence config out of core — providers like Honcho that batch
dialectic reasoning every N turns handle that themselves.

Failure semantics:
  - ``health_check`` + ``prefetch`` failure -> bridge disables provider
    for the session, agent keeps working on baseline.
  - ``sync_turn`` failure -> silently swallowed (fire-and-forget).
  - Provider must NEVER raise out of ``handle_tool_call`` — return a
    ``ToolResult(is_error=True)`` instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import ToolSchema


class MemoryProvider(ABC):
    """Pluggable external memory backend.

    Concrete examples (opt-in plugins):
      - HonchoSelfHostedProvider — Theory-of-Mind user modelling.
      - Mem0Provider — fact extraction + semantic search.
      - CogneeProvider — knowledge-graph / graph-completion retrieval.

    ``provider_id`` must be a stable, namespaced string (e.g.
    ``"memory-honcho:self-hosted"``). Core uses it for logging and
    one-at-a-time registration enforcement.

    ``provider_priority`` (default 100) orders providers when their tools
    or injections might compete. Lower runs first.
    """

    provider_priority: int = 100

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique id, namespaced, e.g. ``"memory-honcho:self-hosted"``."""
        ...

    @abstractmethod
    def tool_schemas(self) -> list[ToolSchema]:
        """Return tool schemas this provider exposes to the agent.

        Tool names MUST be namespaced (e.g. ``honcho_search``,
        ``honcho_profile``) to avoid collisions with core tools or other
        providers.
        """
        ...

    @abstractmethod
    async def handle_tool_call(self, call: ToolCall) -> ToolResult:
        """Execute one of this provider's tool calls.

        MUST NOT raise. Errors are surfaced via
        ``ToolResult(is_error=True, content=...)``.
        """
        ...

    @abstractmethod
    async def prefetch(self, query: str, turn_index: int) -> str | None:
        """Return context to inject this turn, or ``None`` if not this turn.

        Provider owns cadence — e.g. Honcho may only return content every
        ``contextCadence`` turns. Core calls this once per turn before the
        LLM request; the result goes into per-turn injection.
        """
        ...

    @abstractmethod
    async def sync_turn(self, user: str, assistant: str, turn_index: int) -> None:
        """Notify provider that a turn completed. Fire-and-forget from core.

        Implementations should do their own batching / async work. Raising
        here is allowed but will be silently swallowed by the bridge.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Probe the backend. Return True if ready, False to disable for session.

        Called once on session start with a 2-second timeout from the
        bridge. Timeouts count as False.
        """
        ...

    # ─── optional lifecycle hooks (defaults are no-ops) ────────────

    async def on_session_start(self, session_id: str) -> None:
        """Optional: called when a new session begins. Default: no-op."""
        return None

    async def on_session_end(self, session_id: str) -> None:
        """Optional: called when a session ends. Default: no-op."""
        return None

    async def shutdown(self) -> None:
        """Optional: close resources on process exit. Default: no-op.

        Called once at CLI / gateway shutdown via
        :meth:`opencomputer.agent.memory_bridge.MemoryBridge.shutdown_all`.
        Implementations SHOULD close any httpx clients and flush pending
        writes. MUST be idempotent — ``shutdown_all`` may be invoked more
        than once in edge cases (``atexit`` + explicit cleanup path).

        Mirrors Hermes' ``AIAgent.shutdown_memory_provider`` hook at
        ``sources/hermes-agent/run_agent.py:3445-3470``. See II.5 in the
        2026-04-24 reference-parity plan.
        """
        return None

    async def system_prompt_block(self, *, session_id: str | None = None) -> str | None:
        """Optional: contribute a section to the agent's system prompt.

        Default: no-op (returns None). Providers that override this should
        return a 200-1500 char string that summarizes their relevant memory
        for the current session. The bridge aggregates blocks from all active
        providers; the prompt builder appends them under '## Memory context'.

        PR-6 of 2026-04-25 Hermes parity plan. Mirrors Hermes
        MemoryProvider.system_prompt_block from
        sources/hermes-agent/agent/memory_provider.py.

        PRIVACY NOTE: returned text is sent to the LLM provider on every
        turn. Providers should respect privacy + token budget; default cap
        is enforced by config (MemoryConfig.max_ambient_block_chars).
        """
        return None

    async def on_pre_compress(self, messages: list) -> str | None:
        """Optional: extract key facts BEFORE the loop compacts the message
        history. Default: no-op (returns None).

        Return value is wrapped in <KEY-FACTS-DO-NOT-SUMMARIZE>...</KEY-FACTS-
        DO-NOT-SUMMARIZE> markers and prepended to the compaction summary so
        important facts survive summarization.

        PR-6 of 2026-04-25 Hermes parity plan.
        """
        return None

    # ─── T3.2 PR-8: bus-driven lifecycle hooks (no-op defaults) ───────

    async def on_turn_start(self, *, session_id: str | None, turn_index: int) -> None:
        """Optional: react to each turn start (e.g. trigger a fresh prefetch).

        Called by MemoryBridge when a TurnStartEvent arrives on the bus.
        Default: no-op. Implementing this avoids polling — the provider
        is notified exactly once per agent-loop iteration.

        PR-8 of Hermes parity plan.
        """
        return None

    async def on_delegation(
        self,
        *,
        parent_session_id: str,
        child_session_id: str,
        child_outcome: str,
    ) -> None:
        """Optional: react to subagent delegation completion.

        Called by MemoryBridge when a DelegationCompleteEvent arrives on
        the bus. ``child_outcome`` is one of "success", "failure", "error".
        Default: no-op. Useful for flushing per-session state or triggering
        cross-session summarisation.

        PR-8 of Hermes parity plan.
        """
        return None

    async def on_memory_write(
        self,
        *,
        action: str,
        target: str,
        content_size: int,
    ) -> None:
        """Optional: observe declarative-memory writes (audit pattern).

        Called by MemoryBridge when a MemoryWriteEvent arrives on the bus.
        ``action`` is "append" | "replace" | "remove". ``target`` is the
        file name (e.g. "MEMORY.md" / "USER.md"). ``content_size`` is the
        total byte count AFTER the write — NOT the delta, NOT the content.
        Default: no-op.

        PR-8 of Hermes parity plan.
        """
        return None


__all__ = ["MemoryProvider"]
