"""Wave 3 (2026-05-08) — custom_providers wiring.

Bridges the typed :class:`CustomProvider` config schema (declared in
``agent/config.py``) into a runtime provider instance the agent loop
can dispatch through.

Two-layer design:

* :func:`parse_custom_model_spec` — pure parser for the
  ``custom:<name>:<model_id>`` form used by ``/model`` slash dispatch
  and ``oc model`` CLI subcommands. Splits on the first ``:`` after
  ``custom:`` so model ids containing colons (``qwen3.5:27b``) survive.

* :func:`build_custom_provider` — factory that resolves a
  :class:`CustomProvider` config entry into a fresh provider instance.
  ``api_mode`` selects the wire shape:

  - ``"openai"`` — instantiate :class:`OpenAIProvider`.
  - ``"anthropic"`` — instantiate :class:`AnthropicProvider`.
  - ``"auto"`` (default) — probe the endpoint's ``/v1/models``
    once, classify the response shape, and cache the inferred mode
    on the in-memory ``_PROBE_CACHE`` for the lifetime of the
    process so subsequent ``/model`` swaps and fallback routing
    don't re-probe. Probe failure (network error, non-200, opaque
    body) falls through to ``openai`` — the safe default since most
    OpenAI-compat endpoints (Ollama, vLLM, llama.cpp, LM Studio,
    LiteLLM proxies) match that shape.
"""

from __future__ import annotations

import logging
import os

from opencomputer.agent.config import Config, CustomProvider

LOG = logging.getLogger(__name__)

#: Per-process cache of probed api_modes keyed by base_url. Populated
#: on first ``api_mode='auto'`` resolution and reused for every
#: subsequent ``build_custom_provider`` call against the same URL —
#: avoids hammering ``/v1/models`` on every ``/model`` swap or
#: fallback dispatch. Entries don't expire; restart the process to
#: re-probe.
_PROBE_CACHE: dict[str, str] = {}


def _probe_api_mode(base_url: str, *, timeout: float = 5.0) -> str:
    """GET ``<base_url>/models`` and classify the response shape.

    Returns ``"openai"`` (response has a ``data`` array) or
    ``"anthropic"`` (response has a ``models`` array with ``type``
    fields). Falls back to ``"openai"`` on any error — most local
    endpoints are OpenAI-compatible, so this is the safe default.
    """
    cached = _PROBE_CACHE.get(base_url)
    if cached:
        return cached
    try:
        import httpx

        url = f"{base_url.rstrip('/')}/models"
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code == 200:
            body = resp.json()
            if isinstance(body, dict):
                if "data" in body and isinstance(body["data"], list):
                    _PROBE_CACHE[base_url] = "openai"
                    return "openai"
                if "models" in body and isinstance(body["models"], list):
                    # Anthropic /v1/models shape: each entry has 'type': 'model'
                    if any(
                        isinstance(m, dict) and m.get("type") == "model"
                        for m in body["models"]
                    ):
                        _PROBE_CACHE[base_url] = "anthropic"
                        return "anthropic"
        LOG.info(
            "api_mode probe at %s returned status=%s; defaulting to openai",
            url, resp.status_code,
        )
    except Exception as e:  # noqa: BLE001
        LOG.info("api_mode probe at %s failed (%s); defaulting to openai", base_url, e)
    _PROBE_CACHE[base_url] = "openai"
    return "openai"


def parse_custom_model_spec(spec: str) -> tuple[str, str]:
    """Parse ``custom:<name>:<model_id>`` into ``(name, model_id)``.

    The model id may itself contain colons (Ollama tag form
    ``qwen3.5:27b``). We split on the FIRST colon after the
    ``custom:`` prefix; everything after that is the model id verbatim.

    Raises:
        ValueError: when ``spec`` doesn't match the expected shape.
    """
    if not spec.startswith("custom:"):
        raise ValueError(
            f"expected 'custom:<name>:<model_id>' spec, got {spec!r}"
        )
    rest = spec.removeprefix("custom:")
    name, sep, model_id = rest.partition(":")
    if not sep or not name or not model_id:
        raise ValueError(
            f"expected 'custom:<name>:<model_id>' spec, got {spec!r}"
        )
    return name, model_id


def _resolve_api_key(cp: CustomProvider) -> str:
    """Inline ``api_key`` wins; ``key_env`` next; empty string fallback.

    Empty strings let local servers (Ollama, llama.cpp without auth)
    work — the OpenAI SDK accepts ``api_key=""`` without raising,
    AsyncOpenAI sends a ``Bearer `` header that local servers ignore.
    """
    if cp.api_key:
        return cp.api_key
    if cp.key_env:
        value = os.environ.get(cp.key_env, "")
        if not value:
            LOG.warning(
                "custom_providers[%r]: env var %r is unset; "
                "first request will fail with auth error if endpoint requires a key",
                cp.name, cp.key_env,
            )
        return value
    return ""


def build_custom_provider(name: str, config: Config):
    """Construct a provider instance for ``custom_providers[name=<name>]``.

    Args:
        name: the custom_provider entry name (no ``custom:`` prefix).
        config: the active :class:`Config` (carries the
            ``custom_providers`` tuple).

    Returns:
        A fresh provider instance — :class:`OpenAIProvider` for
        ``api_mode in {auto, openai}``, :class:`AnthropicProvider`
        for ``api_mode == anthropic``.

    Raises:
        ValueError: when no ``custom_providers`` entry matches ``name``.
        RuntimeError: when the chosen provider class is missing from the
            plugin registry (e.g. user disabled the bundled extension).
    """
    matching = [cp for cp in config.custom_providers if cp.name == name]
    if not matching:
        available = ", ".join(cp.name for cp in config.custom_providers)
        raise ValueError(
            f"no custom_provider named {name!r} — available: "
            f"{available or '(none configured)'}"
        )
    cp = matching[0]
    api_key = _resolve_api_key(cp)

    # Resolve the provider class via the plugin registry — keeps us
    # inside the SDK boundary (no direct extensions/* imports here).
    from opencomputer.plugins.registry import registry

    # Wave 3 — resolve ``api_mode='auto'`` via lazy /v1/models probe.
    # Cached per base_url for the lifetime of the process so subsequent
    # /model swaps and fallback dispatch don't re-probe.
    effective_mode = cp.api_mode
    if effective_mode == "auto":
        effective_mode = _probe_api_mode(cp.base_url)

    target_name = "anthropic" if effective_mode == "anthropic" else "openai"
    provider_cls = registry.providers.get(target_name)
    if provider_cls is None:
        raise RuntimeError(
            f"custom_provider {name!r} needs the {target_name!r} provider "
            f"plugin enabled to dispatch (api_mode={cp.api_mode!r})"
        )
    # Bundled openai-provider raises if api_key is empty; pass a
    # sentinel that local servers ignore. Real keys always win.
    effective_key = api_key or "no-key-required"
    return provider_cls(api_key=effective_key, base_url=cp.base_url)


__all__ = ["parse_custom_model_spec", "build_custom_provider"]
