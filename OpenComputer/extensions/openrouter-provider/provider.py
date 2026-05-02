"""OpenRouter provider — thin subclass of OpenAIProvider.

OpenRouter is OpenAI-wire-compatible; the only delta is the API-key env var
and the base URL. We subclass and override ``_api_key_env`` so the parent's
existing env-lookup at ``OpenAIProvider.__init__`` reads
``OPENROUTER_API_KEY`` instead of ``OPENAI_API_KEY``. We pass ``api_key=``
and ``base_url=`` through to the parent's kwargs directly.

CredentialPool rotation works unchanged because the parent's comma-split
happens on whatever ``api_key`` value we pass in
(``OPENROUTER_API_KEY="key1,key2"`` flows through to the parent's
``_split_keys_into_pool`` path).

Env vars:
  OPENROUTER_API_KEY   — required; key from https://openrouter.ai/keys
  OPENROUTER_BASE_URL  — optional override (default: openrouter.ai)
"""
from __future__ import annotations

import importlib.util as _importlib_util
import os
from pathlib import Path

# Load extensions/openai-provider/provider.py under a unique module name to
# avoid the sys.modules['provider'] collision that happens when two
# providers both do `from provider import OpenAIProvider`. The collision
# manifests as "cannot import name 'OpenAIProvider' from partially
# initialized module 'provider'" when a test process loads this and another
# provider (e.g. zai-provider) — see test_openrouter_inherits_vision_from_openai.
_OPENAI_PROVIDER_DIR = Path(__file__).resolve().parent.parent / "openai-provider"
_spec = _importlib_util.spec_from_file_location(
    "_oai_base_for_openrouter", str(_OPENAI_PROVIDER_DIR / "provider.py")
)
_mod = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OpenAIProvider = _mod.OpenAIProvider

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIProvider):
    """OpenAI-compatible provider routed through OpenRouter.

    Override ``_api_key_env`` so the parent's env lookup at
    ``OpenAIProvider.__init__`` reads ``OPENROUTER_API_KEY``. Override
    ``default_model`` to a sensible OpenRouter-shaped id (users can still
    pick any model OpenRouter exposes via ``model:`` in config.yaml).
    """

    name = "openrouter"
    default_model = "openai/gpt-4o-mini"
    _api_key_env: str = "OPENROUTER_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # Pre-check the env var so the error message names OpenRouter, not
        # OpenAI (the parent's RuntimeError says "Export OPENAI_API_KEY").
        if not (api_key or os.environ.get(self._api_key_env)):
            raise RuntimeError(
                f"{self._api_key_env} is not set. "
                "Get a free key at https://openrouter.ai/keys."
            )
        # Resolve the base URL with our own env precedence so the parent
        # doesn't pull from OPENAI_BASE_URL by accident.
        resolved_base = (
            base_url
            or os.environ.get("OPENROUTER_BASE_URL")
            or DEFAULT_OPENROUTER_BASE_URL
        )
        super().__init__(api_key=api_key, base_url=resolved_base)

    @property
    def capabilities(self):  # type: ignore[override]
        """OpenRouter passes the upstream's usage payload through verbatim,
        so the cache-token extractor must handle either the Anthropic
        shape (``cache_creation_input_tokens`` / ``cache_read_input_tokens``)
        or the OpenAI shape (``prompt_tokens_details.cached_tokens``).
        Reasoning resend is False — even when OpenRouter routes to
        Anthropic, the upstream's signed thinking blocks aren't surfaced
        through OpenRouter's OpenAI-compatible response shape today.
        """
        from typing import Any as _Any

        from plugin_sdk import CacheTokens, ProviderCapabilities

        def _extract(usage: _Any) -> CacheTokens:
            # Prefer Anthropic-shape fields (more specific); fall back to
            # OpenAI-shape.
            anth_read = getattr(usage, "cache_read_input_tokens", None)
            anth_write = getattr(usage, "cache_creation_input_tokens", None)
            if anth_read is not None or anth_write is not None:
                return CacheTokens(
                    read=int(anth_read or 0),
                    write=int(anth_write or 0),
                )
            details = getattr(usage, "prompt_tokens_details", None)
            cached = 0
            if details is not None:
                cached = int(getattr(details, "cached_tokens", 0) or 0)
            return CacheTokens(read=cached, write=0)

        return ProviderCapabilities(
            requires_reasoning_resend_in_tool_cycle=False,
            reasoning_block_kind=None,
            extracts_cache_tokens=_extract,
            min_cache_tokens=lambda _model: 1024,
            supports_long_ttl=False,
        )
