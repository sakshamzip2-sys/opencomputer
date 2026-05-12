"""Wave 3 follow-up (2026-05-08) — dynamic context-window probe chain.

True model-agnostic context-window resolution. The static
:data:`compaction.DEFAULT_CONTEXT_WINDOWS` table is a useful default
but goes stale every time a vendor ships a new model. The user
shouldn't need a code change to see a correct token-bar for a
brand-new model.

Resolution chain (used by :func:`compaction.context_window_with_overrides`):

  1. ``Config.model_context_overrides[<model>]`` — explicit per-call user
     override (highest priority — set this in config.yaml when probe
     results conflict with documented vendor data).
  2. ``CustomProvider.models[<id>].context_length`` — scoped per-named-
     endpoint override.
  3. **Probe cache** — values fetched from upstream APIs, persisted at
     ``<profile_home>/.context_window_cache.json`` so subsequent calls
     are O(1). 24-hour TTL.
  4. **Multi-source probe chain** (in order; first hit wins):
     a. OpenRouter ``/v1/models`` — free, unauthenticated, returns
        ``context_length`` per model for thousands of models.
     b. Ollama ``/api/show`` — for local Ollama models, reads num_ctx
        from the model card. Tried only when an Ollama-style endpoint
        is plausibly running (probe is fail-soft).
     c. Anthropic ``/v1/models`` — when ``ANTHROPIC_API_KEY`` is set
        and the model id starts with ``claude-``.
     d. models.dev community registry — JSON catalog at
        ``https://models.dev/api/models.json`` covering 3800+ models
        across 100+ providers; free + unauthenticated.
  5. Static :data:`DEFAULT_CONTEXT_WINDOWS` table.
  6. Family-prefix rules.
  7. 64k conservative default.

Every probe is **fail-soft**: a network outage / 4xx / parse error
falls through to the next source. The chain never raises; the worst
case is a 64k default.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

#: Persistent cache TTL — 24h. Fresh enough that vendor-side context
#: bumps propagate within a day, slow enough that we don't re-hit the
#: probe endpoints on every CLI invocation.
CACHE_TTL_SECONDS: float = 24 * 60 * 60

#: HTTP timeout for any single probe. Short — we never want a slow
#: vendor endpoint to delay status-line render.
PROBE_TIMEOUT_SECONDS: float = 5.0

#: In-memory cache, mirrors the persistent on-disk cache. Lazy-loaded
#: on first probe call. Keys are ``f"{provider}:{model}"``; values are
#: dicts ``{"context_length": int, "fetched_at": float}``.
_MEM_CACHE: dict[str, dict[str, Any]] | None = None


def _cache_path() -> Path:
    """Return the persistent cache path under the active profile home."""
    # Function-level import keeps this module importable without
    # initializing the agent.config module's profile machinery.
    from opencomputer.agent.config import _home

    return _home() / ".context_window_cache.json"


def _load_cache() -> dict[str, dict[str, Any]]:
    """Load the persistent cache (or initialize empty)."""
    global _MEM_CACHE
    if _MEM_CACHE is not None:
        return _MEM_CACHE
    path = _cache_path()
    if not path.exists():
        _MEM_CACHE = {}
        return _MEM_CACHE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _MEM_CACHE = data
        else:
            LOG.warning("context-window cache at %s is malformed; ignoring", path)
            _MEM_CACHE = {}
    except (OSError, json.JSONDecodeError) as e:
        LOG.warning("failed to load context-window cache at %s: %s", path, e)
        _MEM_CACHE = {}
    return _MEM_CACHE


def _save_cache() -> None:
    """Persist the in-memory cache to disk (best-effort, fail-soft)."""
    if _MEM_CACHE is None:
        return
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_MEM_CACHE, sort_keys=True, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        LOG.warning("failed to save context-window cache to %s: %s", path, e)


def _cache_key(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def _cache_get(provider: str, model: str) -> int | None:
    """Look up a cached context length; return None if missing or stale."""
    cache = _load_cache()
    entry = cache.get(_cache_key(provider, model))
    if not entry:
        return None
    fetched_at = entry.get("fetched_at", 0.0)
    if time.time() - fetched_at > CACHE_TTL_SECONDS:
        return None
    ctx = entry.get("context_length")
    return int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else None


def _cache_put(provider: str, model: str, context_length: int) -> None:
    """Insert a probe result into both in-memory and on-disk caches."""
    cache = _load_cache()
    cache[_cache_key(provider, model)] = {
        "context_length": int(context_length),
        "fetched_at": time.time(),
    }
    _save_cache()


def cache_context_window(
    model: str,
    context_length: int,
    *,
    provider_hint: str = "any",
) -> None:
    """Public wrapper for callers that already have vendor catalog metadata."""
    if not model or context_length <= 0:
        return
    _cache_put(provider_hint or "any", model, int(context_length))


def cached_context_window(model: str, *, provider_hint: str = "") -> int | None:
    """Return a cached context window without performing any network probe."""
    if not model:
        return None
    if provider_hint:
        cached = _cache_get(provider_hint, model)
        if cached is not None:
            return cached
    return _cache_get("any", model)


def reset_cache() -> None:
    """Drop the in-memory cache. Used by tests."""
    global _MEM_CACHE
    _MEM_CACHE = None


# ─── individual probe functions ──────────────────────────────────────


def _probe_openrouter(model: str) -> int | None:
    """Look up ``model`` in OpenRouter's catalog.

    OpenRouter's ``/v1/models`` endpoint is free and unauthenticated;
    each entry has a ``context_length`` field. Covers thousands of
    models across all major providers (anthropic/, openai/, google/,
    qwen/, deepseek/, ...).
    """
    try:
        import httpx

        resp = httpx.get(
            "https://openrouter.ai/api/v1/models",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        models = data.get("data") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return None
        # OR ids look like ``anthropic/claude-opus-4-7``. Match the
        # bare suffix too (``claude-opus-4-7``) so users running a
        # provider-direct setup also benefit.
        candidates = {model}
        if "/" in model:
            candidates.add(model.split("/", 1)[1])
        for entry in models:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if not isinstance(entry_id, str):
                continue
            entry_suffix = entry_id.split("/", 1)[-1]
            if entry_id in candidates or entry_suffix in candidates:
                ctx = entry.get("context_length")
                if isinstance(ctx, (int, float)) and ctx > 0:
                    return int(ctx)
        return None
    except Exception as e:  # noqa: BLE001
        LOG.debug("OpenRouter context-window probe failed: %s", e)
        return None


def _probe_ollama(model: str) -> int | None:
    """Query a local Ollama server's ``/api/show`` for a model card.

    Skipped when ``OLLAMA_HOST`` is unset and ``localhost:11434``
    isn't responsive — no point in a network roundtrip when the
    user isn't running Ollama.
    """
    base = os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
    try:
        import httpx

        resp = httpx.post(
            f"{base.rstrip('/')}/api/show",
            json={"name": model},
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        # Ollama exposes context length in two places we need to check:
        #   model_info["<arch>.context_length"] — the model's training-
        #     time max (preferred — accurate even when num_ctx is unset).
        #   parameters string — "num_ctx <int>" line, set via Modelfile.
        if isinstance(data, dict):
            info = data.get("model_info")
            if isinstance(info, dict):
                for key, value in info.items():
                    if key.endswith(".context_length") and isinstance(value, (int, float)):
                        return int(value)
            params = data.get("parameters")
            if isinstance(params, str):
                for line in params.splitlines():
                    line = line.strip()
                    if line.startswith("num_ctx"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[-1].isdigit():
                            return int(parts[-1])
        return None
    except Exception as e:  # noqa: BLE001
        LOG.debug("Ollama context-window probe failed: %s", e)
        return None


def _probe_anthropic(model: str) -> int | None:
    """Probe Anthropic's ``/v1/models`` for a model's metadata.

    Anthropic doesn't currently expose ``max_input_tokens`` in their
    public ``/v1/models`` schema, but we look anyway — if they add
    it, this picks it up automatically. Returns None when the field
    isn't present.
    """
    if not model.startswith("claude-"):
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import httpx

        resp = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        for entry in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(entry, dict):
                continue
            if entry.get("id") == model:
                # Future-proof: check several keys Anthropic might use.
                for key in ("max_input_tokens", "context_length", "context_window"):
                    v = entry.get(key)
                    if isinstance(v, (int, float)) and v > 0:
                        return int(v)
                return None
        return None
    except Exception as e:  # noqa: BLE001
        LOG.debug("Anthropic context-window probe failed: %s", e)
        return None


def _probe_models_dev(model: str) -> int | None:
    """Look up ``model`` in the models.dev community registry.

    models.dev maintains a free JSON catalog of 3800+ model entries
    across 100+ providers. We fetch the full catalog once per 24h
    (cached on disk) and search by id.
    """
    try:
        import httpx

        resp = httpx.get(
            "https://models.dev/api.json",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        # Schema: { "<provider>": { "models": { "<model_id>": {...} } } }
        candidates = {model}
        if "/" in model:
            candidates.add(model.split("/", 1)[1])
        for provider_data in data.values():
            if not isinstance(provider_data, dict):
                continue
            models = provider_data.get("models")
            if not isinstance(models, dict):
                continue
            for model_id, info in models.items():
                if model_id in candidates and isinstance(info, dict):
                    for key in ("context_length", "context_window", "input_context", "limit"):
                        v = info.get(key)
                        if isinstance(v, dict):
                            v = v.get("context") or v.get("input")
                        if isinstance(v, (int, float)) and v > 0:
                            return int(v)
        return None
    except Exception as e:  # noqa: BLE001
        LOG.debug("models.dev context-window probe failed: %s", e)
        return None


# ─── orchestrator ────────────────────────────────────────────────────


def probe_context_window(
    model: str,
    *,
    provider_hint: str = "",
    use_cache: bool = True,
) -> int | None:
    """Run the multi-source probe chain for ``model``.

    Returns the discovered context length in tokens, or ``None`` if
    every source missed (caller falls back to the static table /
    family-prefix rules / conservative default).

    Cached results are returned without re-probing for
    ``CACHE_TTL_SECONDS``. Pass ``use_cache=False`` to force a fresh
    probe (used by ``oc model probe`` CLI).

    The chain is fail-soft: every probe catches its own exceptions,
    so a hung or broken upstream never breaks the resolver.
    """
    cache_key_provider = provider_hint or "any"
    if use_cache:
        cached = _cache_get(cache_key_provider, model)
        if cached is not None:
            return cached
    # Probe in priority order; first hit wins.
    for probe in (_probe_openrouter, _probe_ollama, _probe_anthropic, _probe_models_dev):
        result = probe(model)
        if result is not None:
            _cache_put(cache_key_provider, model, result)
            return result
    return None


__all__ = [
    "CACHE_TTL_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "cache_context_window",
    "cached_context_window",
    "probe_context_window",
    "reset_cache",
]
