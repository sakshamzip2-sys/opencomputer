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
  Picks ``OpenAIProvider`` for ``api_mode in {auto, openai}`` and
  ``AnthropicProvider`` for ``api_mode == anthropic``. ``api_mode=auto``
  is currently shipped as a synonym for ``openai`` — the eager probe
  was descoped from this PR (would have added per-call latency or a
  startup race condition); the lazy-probe-on-first-call path is a
  follow-up. Most custom endpoints (Ollama, vLLM, llama.cpp, LM Studio,
  LiteLLM proxies) are OpenAI-shaped so the synonym is correct in
  practice; users with Anthropic-shaped proxies set ``api_mode:
  anthropic`` explicitly.
"""

from __future__ import annotations

import logging
import os

from opencomputer.agent.config import Config, CustomProvider

LOG = logging.getLogger(__name__)


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

    target_name = "anthropic" if cp.api_mode == "anthropic" else "openai"
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
