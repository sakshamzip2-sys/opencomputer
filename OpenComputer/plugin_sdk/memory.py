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


__all__ = ["MemoryProvider"]
