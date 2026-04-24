"""InjectionEngine — queries all registered DynamicInjectionProviders per turn.

Deterministic ordering (priority asc, then provider_id asc) so repeated turns
with the same state produce the same system prompt. Critical for prompt cache
stability on the LLM side.

Providers run concurrently via ``asyncio.gather``. ``collect_all`` is the
modern async entry point; ``compose`` is the async convenience wrapper that
joins results with a separator. A sync ``collect`` shim is preserved for the
legacy test suite — it uses ``asyncio.run`` internally and will raise if
called from inside a running event loop.

Providers register via `opencomputer.plugins.loader.PluginAPI.register_injection_provider`.
"""

from __future__ import annotations

import asyncio
import logging

from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

_log = logging.getLogger("opencomputer.agent.injection")


class InjectionEngine:
    """Singleton registry + composer for injection providers."""

    def __init__(self) -> None:
        self._providers: dict[str, DynamicInjectionProvider] = {}

    def register(self, provider: DynamicInjectionProvider) -> None:
        pid = provider.provider_id
        if pid in self._providers:
            raise ValueError(f"Injection provider '{pid}' already registered")
        self._providers[pid] = provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def providers(self) -> list[DynamicInjectionProvider]:
        return list(self._providers.values())

    def _ordered(self) -> list[DynamicInjectionProvider]:
        """Deterministic ordering: priority asc, then provider_id asc."""
        return sorted(
            self._providers.values(),
            key=lambda p: (p.priority, p.provider_id),
        )

    async def collect_all(self, ctx: InjectionContext) -> list[str]:
        """Gather every provider concurrently, return non-empty injections in order.

        Providers run via ``asyncio.gather(..., return_exceptions=True)`` so a
        single misbehaving provider never blocks the turn. Exceptions are
        logged at DEBUG and that provider's contribution is dropped — the
        order of surviving contributions is preserved.
        """
        ordered = self._ordered()
        if not ordered:
            return []

        results = await asyncio.gather(
            *(p.collect(ctx) for p in ordered),
            return_exceptions=True,
        )

        out: list[str] = []
        for provider, res in zip(ordered, results, strict=True):
            if isinstance(res, BaseException):
                _log.debug(
                    "injection provider %r raised; skipping",
                    provider.provider_id,
                    exc_info=res,
                )
                continue
            if res and res.strip():
                out.append(res.strip())
        return out

    async def compose(self, ctx: InjectionContext, separator: str = "\n\n") -> str:
        """Convenience: ``collect_all()`` + join with ``separator``."""
        return separator.join(await self.collect_all(ctx))

    def collect(self, ctx: InjectionContext) -> list[str]:
        """Sync shim over ``collect_all`` for callers outside an event loop.

        This exists solely so pre-refactor callers (unit tests, occasional
        synchronous diagnostic hooks) keep working. It will raise
        ``RuntimeError`` if called while an event loop is already running —
        use ``await collect_all(ctx)`` from async contexts.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            raise RuntimeError(
                "InjectionEngine.collect() cannot be called from within a running "
                "event loop — use `await engine.collect_all(ctx)` instead."
            )
        return asyncio.run(self.collect_all(ctx))


#: Global singleton (matches tool_registry pattern)
engine = InjectionEngine()


__all__ = ["InjectionEngine", "engine"]
