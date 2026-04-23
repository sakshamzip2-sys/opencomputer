"""Exception-safe orchestrator between AgentLoop and optional MemoryProvider.

Design goals:
  - A broken provider MUST NOT crash the loop.
  - Provider failure is tracked; after 3 consecutive exceptions the provider
    is disabled for the session and subsequent calls short-circuit.
  - ``sync_turn`` is fire-and-forget: exceptions are swallowed silently.
  - ``prefetch`` returns ``None`` on failure so the caller can treat missing
    context uniformly.
  - ``check_health`` gates the first-use path; a failed health check disables
    the provider for the rest of the session.

Failure state is kept in ``MemoryContext._failure_state`` so the bridge itself
stays stateless and can be constructed per-call if needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from plugin_sdk.runtime_context import RuntimeContext

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_S = 2.0
_CONSECUTIVE_FAILURE_LIMIT = 3

#: Contexts that must NOT spin external memory providers. Referenced by both
#: :meth:`MemoryBridge.prefetch` and :meth:`MemoryBridge.sync_turn` so the
#: guard is symmetric — a cron turn that completes won't call
#: ``provider.sync_turn`` just because it skipped ``prefetch``.
_BATCH_CONTEXTS: frozenset[str] = frozenset({"cron", "flush"})

#: Follow-up #28 — cap on prefetch output. Keeps context-window bloat bounded
#: and prevents the prefix cache from churning when Honcho returns large
#: recall blobs. Provider output longer than this is truncated by keeping
#: the latest ``MAX_PREFETCH_CHARS - 40`` chars (recency-weighted) and
#: prepending a truncation marker. 2000 is big enough that most recall
#: payloads fit unchanged and small enough that the worst case is ~1 KB of
#: context, not 10 KB.
MAX_PREFETCH_CHARS: int = 2000

#: Marker string prepended to truncated prefetch output. Deliberately short
#: (~30 chars including the newline) to leave room in the budget for
#: content. The 40-char headroom in the slice computation accounts for this
#: marker plus any downstream formatting.
_TRUNCATION_MARKER: str = "[…earlier recall truncated…]\n"


class MemoryBridge:
    """Thin shim around an optional ``MemoryProvider``.

    The bridge is cheap to construct; create one per ``AgentLoop`` instance
    and reuse it across turns. All public methods are safe to call when no
    provider is registered (no-op fast path).
    """

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    # ─── helpers ────────────────────────────────────────────────────

    @property
    def _provider(self) -> Any | None:
        return getattr(self._ctx, "provider", None)

    def _is_disabled(self) -> bool:
        return bool(self._ctx._failure_state.get("disabled", False))

    def _disable(self, reason: str) -> None:
        if not self._is_disabled():
            logger.warning(
                "Memory provider %s disabled for session: %s",
                getattr(self._provider, "provider_id", "<unknown>"),
                reason,
            )
        self._ctx._failure_state["disabled"] = True

    def _record_failure(self, where: str, exc: BaseException) -> None:
        state = self._ctx._failure_state
        count = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = count
        logger.debug(
            "Memory provider failure in %s: %s (%d consecutive)",
            where,
            exc,
            count,
        )
        if count >= _CONSECUTIVE_FAILURE_LIMIT:
            self._disable(f"{_CONSECUTIVE_FAILURE_LIMIT} consecutive failures in {where}")

    def _record_success(self) -> None:
        self._ctx._failure_state["consecutive_failures"] = 0

    # ─── public API ────────────────────────────────────────────────

    async def check_health(self) -> bool:
        """Probe the provider. Returns True if healthy (or no provider).

        On failure, disables the provider for the rest of the session.
        """
        provider = self._provider
        if provider is None:
            return True
        if self._is_disabled():
            return False
        try:
            result = await asyncio.wait_for(provider.health_check(), timeout=_HEALTH_TIMEOUT_S)
            if not result:
                self._disable("health check returned False")
                return False
            return True
        except (TimeoutError, Exception) as exc:
            self._disable(f"health check raised: {exc}")
            return False

    async def prefetch(
        self,
        query: str,
        turn_index: int,
        runtime: RuntimeContext | None = None,
    ) -> str | None:
        """Ask the provider for context to inject this turn.

        Returns ``None`` if no provider, provider disabled, or provider fails.

        When ``runtime.agent_context`` is ``"cron"`` or ``"flush"`` the guard
        short-circuits to ``None`` without touching the provider — those batch
        contexts must not spin up external memory stacks. See Hermes' same
        guard at ``sources/hermes-agent/plugins/memory/honcho/__init__.py:279-286``.
        """
        if runtime is not None and runtime.agent_context in _BATCH_CONTEXTS:
            return None  # cron guard — don't spin external provider for batch jobs
        provider = self._provider
        if provider is None or self._is_disabled():
            return None
        try:
            result = await provider.prefetch(query, turn_index)
            self._record_success()
            # Follow-up #28 — cap oversize recall payloads. Keep the TAIL so
            # recency is preserved (recent memory matters more than old).
            # Applies only to truthy strings — None / empty values fall
            # through unchanged so the caller sees a uniform "no content"
            # signal.
            if isinstance(result, str) and len(result) > MAX_PREFETCH_CHARS:
                tail = result[-(MAX_PREFETCH_CHARS - 40):]
                return _TRUNCATION_MARKER + tail
            return result
        except Exception as exc:
            self._record_failure("prefetch", exc)
            return None

    async def sync_turn(
        self,
        user: str,
        assistant: str,
        turn_index: int,
        runtime: RuntimeContext | None = None,
    ) -> None:
        """Notify the provider that a turn completed. Fire-and-forget.

        Exceptions are swallowed silently — sync_turn must never propagate
        failures into the agent loop. Respects the same cron/flush guard as
        :meth:`prefetch` so a batch turn doesn't spin the provider on write
        just because it skipped the read.
        """
        if runtime is not None and runtime.agent_context in _BATCH_CONTEXTS:
            return  # cron guard — symmetric with prefetch
        provider = self._provider
        if provider is None or self._is_disabled():
            return
        try:
            await provider.sync_turn(user, assistant, turn_index)
        except Exception as exc:
            logger.debug("sync_turn swallowed exception: %s", exc)

    def get_tool_schemas(self) -> list:
        """Return provider-specific tool schemas, or empty list."""
        provider = self._provider
        if provider is None or self._is_disabled():
            return []
        try:
            return list(provider.tool_schemas())
        except Exception as exc:
            logger.warning("tool_schemas() raised: %s", exc)
            return []

    async def handle_tool_call(self, call: Any) -> Any | None:
        """Route a tool call to the provider. Returns None if not provider-owned."""
        provider = self._provider
        if provider is None or self._is_disabled():
            return None
        try:
            return await provider.handle_tool_call(call)
        except Exception as exc:
            self._record_failure("handle_tool_call", exc)
            from plugin_sdk.core import ToolResult

            return ToolResult(
                tool_call_id=getattr(call, "id", ""),
                content=f"Memory provider error: {exc}",
                is_error=True,
            )
