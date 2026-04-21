"""
InjectionEngine — queries all registered DynamicInjectionProviders per turn.

Deterministic ordering (priority asc, then provider_id asc) so repeated turns
with the same state produce the same system prompt. Critical for prompt cache
stability on the LLM side.

Providers register via `opencomputer.plugins.loader.PluginAPI.register_injection_provider`.
"""

from __future__ import annotations

from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext


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

    def collect(self, ctx: InjectionContext) -> list[str]:
        """Call each provider, return non-empty injections in deterministic order."""
        # Deterministic: sort by (priority, provider_id). Same inputs → same output.
        ordered = sorted(
            self._providers.values(),
            key=lambda p: (p.priority, p.provider_id),
        )
        out: list[str] = []
        for p in ordered:
            try:
                text = p.collect(ctx)
            except Exception:  # noqa: BLE001 — providers never break the loop
                continue
            if text and text.strip():
                out.append(text.strip())
        return out

    def compose(self, ctx: InjectionContext, separator: str = "\n\n") -> str:
        """Convenience: collect() + join."""
        return separator.join(self.collect(ctx))


#: Global singleton (matches tool_registry pattern)
engine = InjectionEngine()


__all__ = ["InjectionEngine", "engine"]
