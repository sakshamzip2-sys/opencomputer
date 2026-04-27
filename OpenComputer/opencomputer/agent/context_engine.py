"""Pluggable context-management ABC.

Both Hermes-agent and OpenClaw converged on the same lifecycle shape for
context-window management: ``on_session_start`` → ``update_from_response``
→ ``should_compress`` / ``compress`` → ``on_session_end``. This module
exposes that contract so OpenComputer can grow alternative strategies
(LCM-style topic DAGs, semantic-clustering compaction, third-party
research engines) without touching the agent loop.

The default implementation is ``ContextCompressor`` (formerly
``CompactionEngine``) — aux-LLM summarization with safe boundary
splitting around tool_use/tool_result pairs. The agent loop wires
whichever engine ``opencomputer/agent/context_engine_registry.get(name)``
resolves to; ``LoopConfig.context_engine`` (default ``"compressor"``)
selects it.

Adding a new engine: subclass :class:`ContextEngine`, decorate the
module with the registry's ``register`` helper, and ship it as a plugin
under ``extensions/context-engine-<name>/``. The plugin loader pulls
it in at startup.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from plugin_sdk.core import Message


@dataclass(slots=True)
class ContextEngineResult:
    """Outcome of a single :meth:`ContextEngine.compress` call.

    Mirrors :class:`opencomputer.agent.compaction.CompactionResult` so
    existing call sites keep their shape; new engines can return the
    same record without depending on the compaction module.
    """

    messages: list[Message]
    did_compress: bool = False
    degraded: bool = False
    reason: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


class ContextEngine(ABC):
    """Lifecycle ABC for context-window management strategies.

    Concrete engines own the decision of WHEN to compress (e.g. token
    threshold, message-count threshold, topic-shift heuristic) and HOW
    to compress (LLM summarization, semantic clustering, hard truncation).

    All methods are async-safe — implementations may make I/O calls.
    Default implementations are no-ops so an engine can opt out of
    lifecycle stages it doesn't need.
    """

    #: Stable identifier the registry looks up by (e.g. ``"compressor"``).
    #: Overridden by subclasses; the registry uses this as the lookup key.
    name: str = "abstract"

    # ─── token-tracking attributes (read by the agent loop) ─────────

    #: Most recent prompt / completion token counts. The agent loop
    #: writes these from each ProviderResponse so engines that gate on
    #: token usage (like ``ContextCompressor``) can decide cheaply.
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0

    #: Threshold the engine considers "full". Agent loop reads this for
    #: telemetry / hooks; the ABC doesn't enforce its semantics.
    threshold_tokens: int = 0

    #: When True, the engine is mid-compress; the agent loop must NOT
    #: fire hooks or run injection providers (would recurse).
    in_progress: bool = False

    # ─── lifecycle ──────────────────────────────────────────────────

    async def on_session_start(  # noqa: B027 — concrete no-op default
        self, *, session_id: str, model: str, messages: list[Message]
    ) -> None:
        """Called once when a new session opens. Default: no-op."""

    async def update_from_response(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Record the most recent provider usage. Default: store on self."""
        self.last_prompt_tokens = prompt_tokens
        self.last_completion_tokens = completion_tokens

    @abstractmethod
    def should_compress(self, *, last_input_tokens: int) -> bool:
        """Decide whether the next turn should run :meth:`compress`."""

    @abstractmethod
    async def compress(
        self,
        *,
        messages: list[Message],
        last_input_tokens: int,
    ) -> ContextEngineResult:
        """Return a (possibly compressed) message list + metadata."""

    async def on_session_end(  # noqa: B027 — concrete no-op default
        self, *, session_id: str
    ) -> None:
        """Called when the session closes. Default: no-op."""


__all__ = ["ContextEngine", "ContextEngineResult"]
