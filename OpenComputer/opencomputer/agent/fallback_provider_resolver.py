"""Wave 3 (2026-05-08) — resolve FallbackProvider entries to live providers.

Bridges the typed :class:`opencomputer.agent.config.FallbackProvider`
config schema into a list of ``(provider_instance, model)`` pairs the
fallback router can iterate.

Each entry's ``provider`` field is one of:
* a registered bundled provider name (``openrouter``, ``anthropic``,
  ``openai``, ``deepseek``, ...) — looked up via the plugin registry
  the same way ``provider_swap.lookup_provider`` does.
* ``custom:<name>`` — references an entry under
  :attr:`Config.custom_providers`; dispatched via
  :func:`opencomputer.agent.custom_provider_client.build_custom_provider`.

Construction errors (missing API key env var, unknown provider) log
+ skip the entry rather than raising, so a single broken fallback
doesn't poison the whole chain.
"""

from __future__ import annotations

import logging

from opencomputer.agent.config import Config, FallbackProvider

LOG = logging.getLogger(__name__)


def _build_one(fp: FallbackProvider, config: Config):
    if fp.provider.startswith("custom:"):
        from opencomputer.agent.custom_provider_client import build_custom_provider

        cp_name = fp.provider.removeprefix("custom:")
        return build_custom_provider(cp_name, config)

    from opencomputer.plugins.registry import registry

    provider_cls = registry.providers.get(fp.provider)
    if provider_cls is None:
        raise RuntimeError(
            f"fallback_providers entry references unregistered provider "
            f"{fp.provider!r}; registered: {sorted(registry.providers.keys())}"
        )
    kwargs: dict = {}
    if fp.base_url:
        kwargs["base_url"] = fp.base_url
    if fp.key_env:
        import os
        v = os.environ.get(fp.key_env, "")
        if v:
            kwargs["api_key"] = v
    return provider_cls(**kwargs)


def build_fallback_provider_chain(
    fallback_providers: tuple[FallbackProvider, ...],
    config: Config,
) -> tuple[tuple[object, str], ...]:
    """Resolve each entry to ``(provider_instance, model)``; drop failures.

    Failures are logged but don't propagate — a single broken fallback
    entry should not prevent valid ones from being tried.
    """
    out: list[tuple[object, str]] = []
    for fp in fallback_providers:
        try:
            inst = _build_one(fp, config)
        except Exception as e:  # noqa: BLE001
            LOG.warning(
                "fallback_providers entry provider=%r model=%r failed to "
                "construct: %s; skipping",
                fp.provider, fp.model, e,
            )
            continue
        out.append((inst, fp.model))
    return tuple(out)


__all__ = ["build_fallback_provider_chain"]
