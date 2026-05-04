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

# Wave 5 T5 — OpenRouter response-cache (Hermes 457c7b76c). Distinct
# from prompt-caching: the response-cache caches the entire LLM response
# across identical requests on OpenRouter's edge.
_DEFAULT_RESPONSE_CACHE_TTL_S: int = 300  # 5 min — Hermes default
_MIN_TTL_S: int = 1
_MAX_TTL_S: int = 86400  # 24 h


def build_or_headers(cfg: dict | None = None) -> dict[str, str]:
    """Build OpenRouter outbound HTTP headers from the runtime config.

    Adds the response-cache headers when ``openrouter.response_cache``
    is True (default). TTL is read from
    ``openrouter.response_cache_ttl`` and clamped to
    ``[_MIN_TTL_S, _MAX_TTL_S]``. Caller layers these on top of any
    provider-internal default headers.
    """
    or_cfg = (cfg or {}).get("openrouter") or {}
    headers: dict[str, str] = {}
    if or_cfg.get("response_cache", True):
        headers["X-OpenRouter-Cache"] = "1"
        try:
            ttl = int(or_cfg.get("response_cache_ttl", _DEFAULT_RESPONSE_CACHE_TTL_S))
        except (TypeError, ValueError):
            ttl = _DEFAULT_RESPONSE_CACHE_TTL_S
        ttl = max(_MIN_TTL_S, min(_MAX_TTL_S, ttl))
        headers["X-OpenRouter-Cache-TTL"] = str(ttl)
    return headers


def parse_cache_status(response_headers: dict[str, str] | None) -> str:
    """Read the ``X-OpenRouter-Cache-Status`` response header.

    Tolerates missing header (default ``MISS``) and case-mismatched
    headers since httpx-style mappings are case-insensitive but tests
    sometimes pass raw lowercase dicts.
    """
    if not response_headers:
        return "MISS"
    return (
        response_headers.get("X-OpenRouter-Cache-Status")
        or response_headers.get("x-openrouter-cache-status")
        or "MISS"
    )


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
        # Wave 5 T5 closure — wire build_or_headers into the request path.
        # Read the OC config (fail-soft if unreadable); rebuild the
        # AsyncOpenAI client with default_headers carrying the
        # X-OpenRouter-Cache + X-OpenRouter-Cache-TTL values so the
        # OpenRouter edge cache is activated. Response-header parsing
        # (parse_cache_status) is deferred — would need
        # client.with_raw_response, a non-trivial refactor of the
        # OpenAI parent's SDK call sites.
        try:
            cache_headers = build_or_headers(self._load_or_cfg())
        except Exception:  # noqa: BLE001 — never let cfg-read break provider init
            cache_headers = {}
        if cache_headers:
            from openai import AsyncOpenAI as _AsyncOpenAI

            self.client = _AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base or resolved_base,
                default_headers=cache_headers,
            )
        # Stash for tests / observability.
        self._or_cache_headers: dict[str, str] = cache_headers

    @staticmethod
    def _load_or_cfg() -> dict:
        """Best-effort read of the OC config so build_or_headers can see
        ``openrouter.response_cache`` / ``response_cache_ttl``. Falls back
        to {} on any error so provider construction never breaks because
        of a missing or malformed config file."""
        try:
            from opencomputer.agent.config_store import load_config

            cfg = load_config()
            # Config is a typed dataclass; reconstruct the dict shape
            # build_or_headers expects (it reads cfg["openrouter"]).
            or_section = getattr(cfg, "openrouter", None)
            if or_section is None:
                return {}
            return {
                "openrouter": {
                    "response_cache": getattr(or_section, "response_cache", True),
                    "response_cache_ttl": getattr(
                        or_section, "response_cache_ttl", _DEFAULT_RESPONSE_CACHE_TTL_S,
                    ),
                }
            }
        except Exception:  # noqa: BLE001
            # config_store missing the openrouter section is normal pre-Wave-5;
            # default to caching enabled at the spec's default TTL.
            return {"openrouter": {"response_cache": True}}

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
