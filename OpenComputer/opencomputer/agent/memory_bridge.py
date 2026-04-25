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

    Class-level shutdown registry (II.5)
    ─────────────────────────────────────
    Every non-``None`` provider that gets wrapped by a bridge is tracked in
    ``_SHUTDOWN_REGISTRY`` so :meth:`shutdown_all` (invoked from the CLI's
    ``atexit`` hook) can flush pending writes + close httpx clients for
    every provider the process ever saw — regardless of how many
    ``AgentLoop`` / ``MemoryBridge`` instances were constructed.

    Registration is deduplicated by object identity: wrapping the same
    provider in two bridges registers it once, so ``shutdown_all`` never
    double-closes (which would blow up on a closed httpx client).

    Mirrors Hermes' ``AIAgent.shutdown_memory_provider`` +
    ``_run_cleanup`` atexit hook at ``sources/hermes-agent/cli.py:717-723``.
    """

    #: Ordered registry of providers awaiting shutdown. Insertion order is
    #: preserved (Python dict guarantees) so ``shutdown_all`` drains in
    #: registration order — deterministic across runs. Keys are providers,
    #: values are unused. ``dict`` over ``set`` for ordered iteration.
    _SHUTDOWN_REGISTRY: dict[Any, None] = {}

    #: Tracks providers we've already shut down so a second ``shutdown_all``
    #: call is a clean no-op (idempotent atexit).
    _SHUTDOWN_COMPLETED: set[int] = set()

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        provider = getattr(ctx, "provider", None)
        if provider is not None:
            # Register for atexit shutdown. Dedup by identity — wrapping
            # the same provider twice must not cause a double-close.
            type(self)._SHUTDOWN_REGISTRY[provider] = None

    # ─── II.5 shutdown lifecycle ───────────────────────────────────

    @classmethod
    def _registered_providers(cls) -> list[Any]:
        """Test helper — snapshot the current shutdown registry in order."""
        return list(cls._SHUTDOWN_REGISTRY.keys())

    @classmethod
    def _reset_shutdown_registry(cls) -> None:
        """Test helper — clear the registry + completion tracker.

        Production code MUST NOT call this. It exists so tests can run in
        isolation without bleeding registered providers across cases.
        """
        cls._SHUTDOWN_REGISTRY.clear()
        cls._SHUTDOWN_COMPLETED.clear()

    @classmethod
    async def shutdown_all(cls) -> None:
        """Await ``shutdown()`` on every registered provider.

        Semantics:
          * Drains in registration order — deterministic.
          * Uses ``asyncio.gather(..., return_exceptions=True)`` so one
            provider raising MUST NOT stop others from shutting down.
          * Idempotent: providers that already shut down are skipped, so
            calling ``shutdown_all`` twice does not re-invoke
            ``shutdown`` on any provider.
          * Returns cleanly if the registry is empty.
        """
        pending = [
            p for p in cls._SHUTDOWN_REGISTRY if id(p) not in cls._SHUTDOWN_COMPLETED
        ]
        if not pending:
            return
        # Mark before awaiting — otherwise a concurrent second call could
        # double-schedule the same provider. ``id`` rather than the
        # object itself because providers don't need to be hashable (the
        # registry dict already stores them as keys, so they are, but
        # the completion set is cheaper keyed by ``id``).
        for provider in pending:
            cls._SHUTDOWN_COMPLETED.add(id(provider))
        results = await asyncio.gather(
            *(cls._safe_shutdown(p) for p in pending),
            return_exceptions=True,
        )
        for provider, res in zip(pending, results, strict=False):
            if isinstance(res, BaseException):
                logger.warning(
                    "Memory provider %s shutdown raised: %s",
                    getattr(provider, "provider_id", "<unknown>"),
                    res,
                )

    @staticmethod
    async def _safe_shutdown(provider: Any) -> None:
        """Call ``provider.shutdown`` if defined; otherwise a no-op.

        Catches ``AttributeError`` so providers that pre-date II.5 (e.g.
        stubs from third-party plugins built against an older plugin_sdk)
        don't crash the atexit path just because they lack ``shutdown``.
        The base class supplies a default no-op, so this is strictly a
        backwards-compat belt-and-braces.
        """
        shutdown_fn = getattr(provider, "shutdown", None)
        if shutdown_fn is None:
            return
        await shutdown_fn()

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

    # ─── T3.2 PR-8: bus subscription ──────────────────────────────────

    def register_with_bus(self, bus=None):
        """T3.2: subscribe MemoryBridge to F2 bus events that drive provider hooks.

        Subscribes to:
          - ``turn_start``          → provider.on_turn_start
          - ``delegation_complete`` → provider.on_delegation
          - ``memory_write``        → provider.on_memory_write

        Returns a list of 3 :class:`~opencomputer.ingestion.bus.Subscription`
        handles for clean unregistration (each has an ``unsubscribe()`` method).

        Each handler is exception-isolated per provider so a bad provider
        never disrupts other subscribers or the main loop.

        If ``bus`` is ``None``, uses the module-level default bus singleton.
        """
        if bus is None:
            from opencomputer.ingestion.bus import get_default_bus
            bus = get_default_bus()
        subs = [
            bus.subscribe("turn_start", self._on_turn_start_event),
            bus.subscribe("delegation_complete", self._on_delegation_event),
            bus.subscribe("memory_write", self._on_memory_write_event),
        ]
        return subs

    def _on_turn_start_event(self, event) -> None:
        """Bus handler: fan out TurnStartEvent to provider.on_turn_start."""
        import asyncio as _asyncio

        provider = self._provider
        if provider is None or self._is_disabled():
            return
        try:
            coro = provider.on_turn_start(
                session_id=event.session_id,
                turn_index=event.turn_index,
            )
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                _asyncio.run(coro)
        except Exception:
            logger.exception(
                "memory provider %s on_turn_start failed",
                getattr(provider, "provider_id", repr(provider)),
            )

    def _on_delegation_event(self, event) -> None:
        """Bus handler: fan out DelegationCompleteEvent to provider.on_delegation."""
        import asyncio as _asyncio

        provider = self._provider
        if provider is None or self._is_disabled():
            return
        try:
            coro = provider.on_delegation(
                parent_session_id=event.parent_session_id,
                child_session_id=event.child_session_id,
                child_outcome=event.child_outcome,
            )
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                _asyncio.run(coro)
        except Exception:
            logger.exception(
                "memory provider %s on_delegation failed",
                getattr(provider, "provider_id", repr(provider)),
            )

    def _on_memory_write_event(self, event) -> None:
        """Bus handler: fan out MemoryWriteEvent to provider.on_memory_write."""
        import asyncio as _asyncio

        provider = self._provider
        if provider is None or self._is_disabled():
            return
        try:
            coro = provider.on_memory_write(
                action=event.action,
                target=event.target,
                content_size=event.content_size,
            )
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                _asyncio.run(coro)
        except Exception:
            logger.exception(
                "memory provider %s on_memory_write failed",
                getattr(provider, "provider_id", repr(provider)),
            )

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

    # ─── PR-6 T2.1 / T2.2 / T2.3 lifecycle collectors ─────────────

    def _iter_active_providers(self):
        """Yield the single active provider, if any and not disabled.

        The current bridge architecture supports one provider per context.
        Tests may inject a list via ``bridge._registered_providers`` to
        exercise multi-provider aggregation paths; this helper handles both
        shapes so tests and production code use the same collector methods.
        """
        # Test shim: if _registered_providers was monkey-patched onto the
        # instance (as the test suite does), iterate it directly.
        instance_override = self.__dict__.get("_registered_providers")
        if instance_override is not None:
            yield from instance_override
            return
        # Production path: single provider from context.
        provider = self._provider
        if provider is not None and not self._is_disabled():
            yield provider

    async def collect_system_prompt_blocks(
        self,
        *,
        session_id: str | None = None,
        max_per_block: int = 800,
    ) -> str:
        """Aggregate all active providers' system_prompt_block. PR-6 T2.1.

        Per-provider failures are logged + isolated (one bad provider doesn't
        poison the others). Each block is truncated to max_per_block chars
        before joining. Returns '' if no providers contribute or feature is off.
        """
        blocks = []
        for provider in self._iter_active_providers():
            try:
                block = await provider.system_prompt_block(session_id=session_id)
            except Exception:
                logger.exception(
                    "memory provider %s system_prompt_block failed",
                    getattr(provider, "provider_id", repr(provider)),
                )
                continue
            if block:
                text = block.strip()
                if len(text) > max_per_block:
                    text = text[:max_per_block] + "…[truncated]"
                blocks.append(f"### From {provider.provider_id}\n\n{text}")
        return "\n\n".join(blocks)

    async def collect_pre_compress(self, messages: list) -> str:
        """Aggregate all active providers' on_pre_compress. PR-6 T2.2.

        Returned chunks are wrapped in <KEY-FACTS-DO-NOT-SUMMARIZE> markers
        by the caller. Per-provider failures isolated.
        """
        chunks = []
        for provider in self._iter_active_providers():
            try:
                chunk = await provider.on_pre_compress(messages)
            except Exception:
                logger.exception(
                    "memory provider %s on_pre_compress failed",
                    getattr(provider, "provider_id", repr(provider)),
                )
                continue
            if chunk:
                chunks.append(chunk.strip())
        return "\n\n".join(chunks)

    async def fire_session_end(self, session_id: str) -> None:
        """Iterate active providers' on_session_end. PR-6 T2.3.

        The hook has been defined in plugin_sdk/memory.py since II.5 but
        was never actually invoked from the agent loop. This wires it.
        Per-provider failures isolated.
        """
        for provider in self._iter_active_providers():
            try:
                await provider.on_session_end(session_id)
            except Exception:
                logger.exception(
                    "memory provider %s on_session_end failed",
                    getattr(provider, "provider_id", repr(provider)),
                )
