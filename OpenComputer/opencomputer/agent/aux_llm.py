"""Auxiliary LLM helpers — provider-agnostic, with fallback (T68).

Auxiliary code paths (the /btw slash command, vision_analyze tool, batch
processing, profile bootstrap LLM extractor) historically called
Anthropic directly via :mod:`opencomputer.agent.anthropic_client`. That
broke the model-agnosticism contract: a user with only an OpenAI / Groq /
Ollama key got working chat but no /btw, no vision, no profile bootstrap.

This module routes those auxiliary calls through whatever provider the
user configured (``config.model.provider``), the same way the chat loop
+ ``recall_synthesizer`` + ``title_generator`` do.

Two entry points:

  - :func:`complete_text` — plain text in, plain text out
  - :func:`complete_vision` — text + image, returns text (raises if
    the configured provider doesn't expose vision via its
    ``complete()`` API; the caller decides whether to fall back)

Both inherit the user's auth + base URL config because the configured
provider plugin already knows them.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from opencomputer.agent.agent_cache import (
    DEFAULT_AUX_RESPONSE_CACHE_MAX,
    AgentCache,
    aux_response_signature,
)

_log = logging.getLogger("opencomputer.agent.aux_llm")

#: Module-level singleton response cache (M1.3, 2026-05-09). Opt-in
#: via ``use_cache=True`` on :func:`complete_text`. Sized for ~2MB
#: worst case (256 entries × ~8KB each at typical max_tokens).
_AUX_RESPONSE_CACHE: AgentCache = AgentCache(max_size=DEFAULT_AUX_RESPONSE_CACHE_MAX)

#: Telemetry counters for ``oc usage`` integration / debug surfaces.
#: Best-effort — never crash the call path.
_AUX_CACHE_STATS: dict[str, int] = {"hits": 0, "misses": 0}


def aux_cache_stats() -> dict[str, int]:
    """Return a snapshot of aux-LLM response cache hit/miss counts.

    M1.3 — exposed so ``oc usage`` (or any debug surface) can render
    the cache effectiveness without grovelling into module internals.
    Returns a fresh dict so callers can mutate it safely.
    """
    return dict(_AUX_CACHE_STATS)


def clear_aux_response_cache() -> None:
    """Empty the aux-LLM response cache. Intended for tests + ``oc admin reset``."""
    _AUX_RESPONSE_CACHE.clear()
    _AUX_CACHE_STATS["hits"] = 0
    _AUX_CACHE_STATS["misses"] = 0


def _resolve_provider() -> Any:
    """Resolve the user's configured provider plugin instance.

    Mirrors :func:`opencomputer.agent.title_generator._resolve_cheap_provider`
    so all auxiliary paths inherit the same auth + base URL config without
    new setup.
    """
    from opencomputer.agent.config import default_config as _dc
    from opencomputer.plugins.registry import registry as plugin_registry

    cfg = _dc()
    provider_cls = plugin_registry.providers.get(cfg.model.provider)
    if provider_cls is None:
        raise RuntimeError(
            f"provider {cfg.model.provider!r} not registered; cannot run auxiliary call"
        )
    return provider_cls() if isinstance(provider_cls, type) else provider_cls


def _resolve_fallback_provider(fp: Any) -> Any:
    """T68 — resolve a :class:`FallbackProvider` to a provider instance.

    Looks up the provider class in the plugin registry by ``fp.provider``,
    instantiates it. ``fp.base_url`` / ``fp.key_env`` are advisory — the
    provider class is expected to read its own env config, but callers
    can override via attributes when needed.
    """
    from opencomputer.plugins.registry import registry as plugin_registry

    provider_cls = plugin_registry.providers.get(fp.provider)
    if provider_cls is None:
        raise RuntimeError(
            f"fallback provider {fp.provider!r} not registered"
        )
    return provider_cls() if isinstance(provider_cls, type) else provider_cls


def default_config():  # re-export so tests can monkeypatch this single name
    from opencomputer.agent.config import default_config as _dc

    return _dc()


_TRANSIENT_AUX_MARKERS: tuple[str, ...] = (
    "rate limit",
    "ratelimit",
    "rate_limit",
    "429",
    "503",
    "502",
    "504",
    "connection reset",
    "connection error",
    "timeout",
    "temporarily unavailable",
)


def _is_transient_aux(exc: BaseException) -> bool:
    msg = (str(exc) or "").lower()
    return any(marker in msg for marker in _TRANSIENT_AUX_MARKERS)


def _resolve_default_model() -> str:
    """Pick a sensible model name to send to the configured provider.

    Auxiliary calls don't dictate the model — they piggyback on whichever
    model the user already runs chat on. Provider-side logic decides
    whether the model accepts the call (e.g. Ollama provider routes any
    model name to its local daemon; OpenAI rejects unknown names).
    """
    from opencomputer.agent.config import default_config

    cfg = default_config()
    return cfg.model.name


async def complete_text(
    *,
    messages: list[dict[str, Any]],
    system: str = "",
    max_tokens: int = 2048,
    temperature: float = 2.0,
    model: str | None = None,
    use_cache: bool = False,
) -> str:
    """Run a single text completion through the configured provider.

    ``messages`` is a list of dicts with ``role`` and ``content`` keys
    (the plain Anthropic / OpenAI shape). Returns the assistant text.

    Raises ``RuntimeError`` if no provider is configured. Surfaces the
    underlying provider error otherwise — callers should wrap in their
    own try/except if they want to convert SDK errors into user-facing
    text.

    ``use_cache=True`` (M1.3, 2026-05-09) memoizes responses keyed on
    ``(provider, model, system, messages, max_tokens, temperature)``.
    Opt-in: callers MUST verify their prompt is deterministic (same
    inputs always produce a semantically identical answer) before
    enabling. Smart-mode security assessments at temperature=0.0 are
    the canonical use case. Cache misses always hit the upstream
    provider; the result is then stored.
    """
    from plugin_sdk.core import Message

    sdk_messages = [
        Message(role=m["role"], content=m.get("content"))
        for m in messages
    ]
    resolved_model = model or _resolve_default_model()

    if use_cache:
        # We need the provider name for the cache key — resolve here
        # (cheap, registry lookup) so the key is provider-aware. A
        # cache miss falls through to the same _aux_call_with_fallback
        # path as the non-cached case so fallback behavior is identical.
        try:
            provider_name = default_config().model.provider
        except Exception:  # noqa: BLE001 — fall back to non-cached path
            provider_name = ""
        if provider_name:
            cache_key = aux_response_signature(
                provider_name=provider_name,
                model=resolved_model,
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            cached = _AUX_RESPONSE_CACHE.get(cache_key)
            if cached is not None:
                _AUX_CACHE_STATS["hits"] += 1
                return cached
            _AUX_CACHE_STATS["misses"] += 1

    async def _attempt(prov: Any, model_name: str) -> Any:
        return await prov.complete(
            model=model_name,
            messages=sdk_messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    result = await _aux_call_with_fallback(_attempt, resolved_model)

    if use_cache:
        try:
            provider_name = default_config().model.provider
        except Exception:  # noqa: BLE001
            provider_name = ""
        if provider_name:
            cache_key = aux_response_signature(
                provider_name=provider_name,
                model=resolved_model,
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            _AUX_RESPONSE_CACHE.put(cache_key, result)

    return result


async def _aux_call_with_fallback(attempt, primary_model: str) -> str:
    """T68 — shared fallback driver for aux LLM calls.

    Tries the configured provider; on transient failure walks the
    ``fallback_providers`` chain. Records cost on every success.
    Returns the assistant text. Raises the last error after
    chain exhaustion. Used by complete_text / complete_vision /
    complete_video so all three share one fallback contract.
    """
    primary = _resolve_provider()
    last_exc: BaseException | None = None
    try:
        resp = await attempt(primary, primary_model)
        _record_aux_cost(primary, primary_model, resp)
        return resp.message.content if resp and resp.message else ""
    except Exception as exc:  # noqa: BLE001
        last_exc = exc
        if not _is_transient_aux(exc):
            raise

    cfg = default_config()
    for fp in getattr(cfg, "fallback_providers", ()) or ():
        try:
            backup = _resolve_fallback_provider(fp)
            backup_model = fp.model or primary_model
            resp = await attempt(backup, backup_model)
            _record_aux_cost(backup, backup_model, resp)
            return resp.message.content if resp and resp.message else ""
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_transient_aux(exc):
                raise
    assert last_exc is not None
    raise last_exc


def complete_text_sync(
    *,
    messages: list[dict[str, Any]],
    system: str = "",
    max_tokens: int = 2048,
    temperature: float = 2.0,
    model: str | None = None,
    use_cache: bool = False,
) -> str:
    """Sync wrapper for :func:`complete_text`.

    Use this from sync code paths (profile bootstrap, batch fallback) that
    don't already have an event loop. Internally drives the async call
    with ``asyncio.run`` — same pattern as ``title_generator.call_llm``.
    """
    return asyncio.run(
        complete_text(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
            use_cache=use_cache,
        )
    )


async def complete_vision(
    *,
    image_base64: str,
    mime_type: str,
    prompt: str,
    max_tokens: int = 2048,
    model: str | None = None,
) -> str:
    """Run a vision completion (text + image) through the configured provider.

    T68 — same fallback chain as :func:`complete_text`. A transient
    failure on the primary provider walks the configured
    ``fallback_providers`` chain. Non-transient errors short-circuit
    immediately so the existing "vision not available on <provider>"
    UX is preserved.
    """
    from plugin_sdk.core import Message

    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": image_base64,
            },
        },
        {"type": "text", "text": prompt},
    ]
    resolved_model = model or _resolve_default_model()

    async def _attempt(prov: Any, model_name: str) -> Any:
        return await prov.complete(
            model=model_name,
            messages=[Message(role="user", content=content)],
            max_tokens=max_tokens,
        )

    return await _aux_call_with_fallback(_attempt, resolved_model)


async def complete_video(
    *,
    video_base64: str,
    mime_type: str,
    prompt: str,
    max_tokens: int = 2048,
    model: str | None = None,
) -> str:
    """Run a video completion (text + base64 video) through the configured provider.

    T68 — same fallback chain as :func:`complete_text` and :func:`complete_vision`.
    """
    from plugin_sdk.core import Message

    data_url = f"data:{mime_type};base64,{video_base64}"
    content = [
        {"type": "video_url", "video_url": {"url": data_url}},
        {"type": "text", "text": prompt},
    ]
    resolved_model = model or _resolve_default_model()

    async def _attempt(prov: Any, model_name: str) -> Any:
        return await prov.complete(
            model=model_name,
            messages=[Message(role="user", content=content)],
            max_tokens=max_tokens,
        )

    return await _aux_call_with_fallback(_attempt, resolved_model)


def _record_aux_cost(provider: Any, model: str, resp: Any) -> None:
    """Hermes-followup 2026-05-07 — record aux LLM call into active session.

    Best-effort. No-op when no session is active. Centralised so the
    three aux_llm callers stay one-liners.
    """
    try:
        from opencomputer.agent.usage_pricing import record_response_for_provider

        record_response_for_provider(provider=provider, model=model, response=resp)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "aux_cache_stats",
    "clear_aux_response_cache",
    "complete_text",
    "complete_text_sync",
    "complete_video",
    "complete_vision",
]
